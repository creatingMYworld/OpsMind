"""OpsMind -- the observability portal.

This is the product Use Case 2 asks for: a cloud-based log monitoring and
visualization solution with a dashboard that tracks cloud resource usage and
suggests cost-saving actions.

It is a SEPARATE deployable from CogniKart. The two never talk directly in the
deployed architecture -- they meet only through Google Cloud:

    CogniKart (4 Cloud Run services) --stdout--> Cloud Logging
                                                 Cloud Monitoring
                                                      |
                                       OpsMind reads via the GCP APIs
                                                      |
                                             DevOps engineer's browser

One process serves both the JSON API and the dashboard UI, which means the
browser never holds a GCP credential: every call to Google Cloud happens
server-side using the Cloud Run runtime service account via Application
Default Credentials.
"""
import asyncio
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .collectors import local as local_collector
from .collectors.evaluator import evaluator
from .config import settings
from .engine import cost as cost_engine
from .engine import kpi as kpi_engine
from .engine import actions as actions_engine
from .engine import patterns as patterns_engine
from .engine import recommend as recommend_engine
from .engine.incidents import manager as incident_manager
from .engine.rules import ruleset
from .store import store

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")

# OpsMind deliberately has no endpoint that changes anything, anywhere. It
# holds read-only IAM roles and exposes a read-only API: it observes and
# advises, and cannot act on -- or break -- the application it watches. Fault
# injection lives in CogniKart's own Scenario Lab, which is where it belongs.

app = FastAPI(
    title="OpsMind",
    version="1.0.0",
    description="Cloud log monitoring, resource visibility and cost "
                "optimization portal (Cognizant GCP Hackathon, Use Case 2).",
)

_started_at = time.time()


# --- auth -----------------------------------------------------------------
# OpsMind can read your project's logs, so a public URL without a token would
# expose them. Two things are deliberately NOT gated:
#
#   /static/*   the dashboard's own HTML, CSS and JavaScript. It is program
#               code, not data, and gating it breaks the page it belongs to --
#               the browser fetches those URLs without the query string the
#               token arrived on, so every asset 401s and the page renders
#               unstyled and inert.
#   /healthz    Cloud Run's liveness checks, which carry no credentials.
#
# A token presented once in the query string is exchanged for a cookie, so the
# page's own API calls authenticate without the token travelling in every URL.
TOKEN_COOKIE = "opsmind_token"
_OPEN_PATHS = ("/healthz", "/readyz", "/internal/ingest", "/favicon.ico")
_OPEN_PREFIXES = ("/static/",)


def _supplied_token(request: Request) -> str:
    return (request.headers.get("x-opsmind-token")
            or request.query_params.get("token", "")
            or request.cookies.get(TOKEN_COOKIE, ""))


@app.middleware("http")
async def guard(request: Request, call_next):
    if not settings.dashboard_token:
        return await call_next(request)

    path = request.url.path
    if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
        return await call_next(request)

    supplied = _supplied_token(request)
    if not secrets.compare_digest(supplied, settings.dashboard_token):
        # An unauthenticated page request should land somewhere a person can
        # act on, not a JSON error they cannot read.
        if request.headers.get("accept", "").startswith("text/html"):
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8>"
                "<title>OpsMind</title>"
                "<body style=\"font:16px/1.6 system-ui;background:#1a0b2e;"
                "color:#ede9fe;display:grid;place-items:center;height:100vh;"
                "margin:0;text-align:center\">"
                "<div><h1 style=\"margin:0 0 10px\">OpsMind</h1>"
                "<p style=\"color:#c9bfe4\">This dashboard needs an access token.<br>"
                "Open it as <code>?token=YOUR_TOKEN</code>.</p></div>",
                status_code=401)
        return JSONResponse(status_code=401,
                            content={"error": "invalid or missing token"})

    response = await call_next(request)
    # Exchange a query-string token for a cookie, so the page's own fetches
    # are authenticated and the token stops appearing in every URL.
    if request.query_params.get("token") and not request.cookies.get(TOKEN_COOKIE):
        proto = request.headers.get("x-forwarded-proto", "") or request.url.scheme
        response.set_cookie(
            TOKEN_COOKIE, settings.dashboard_token, httponly=True,
            samesite="lax", secure=(proto == "https"), max_age=12 * 3600, path="/")
    return response


# --- lifecycle ------------------------------------------------------------
_rule_warnings: List[Dict[str, Any]] = []


@app.on_event("startup")
def _startup() -> None:
    evaluator.start()
    try:
        _rule_warnings.extend(ruleset.validate_metrics(store))
    except Exception:
        pass
    if settings.data_source == "gcp":
        from .collectors.gcp_logs import collector as log_collector
        from .collectors.gcp_metrics import collector as metric_collector
        log_collector.start()
        metric_collector.start()


# --- health ---------------------------------------------------------------
@app.get("/healthz", include_in_schema=False)
def healthz() -> Dict[str, Any]:
    return {"status": "ok", "service": "opsmind",
            "uptimeS": round(time.time() - _started_at, 1)}


@app.get("/readyz", include_in_schema=False)
def readyz() -> Dict[str, Any]:
    return {"status": "ready"}


# --- ingestion (local mode only) ------------------------------------------
@app.post("/internal/ingest", include_in_schema=False)
def ingest(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Local-mode log sink.

    In GCP mode this endpoint is unused: logs arrive by polling the Cloud
    Logging API instead. It exists so the whole portal is demonstrable with no
    GCP project at all, which makes development and rehearsal independent of
    network and credentials.
    """
    if settings.data_source != "local":
        return {"ignored": True,
                "reason": "DATA_SOURCE=gcp; logs are read from Cloud Logging"}
    return local_collector.ingest(payload.get("entries") or [])


# --- meta -----------------------------------------------------------------
@app.get("/api/v1/meta")
def meta() -> Dict[str, Any]:
    collectors: Dict[str, Any] = {"evaluator": evaluator.status()}
    if settings.data_source == "gcp":
        from .collectors.gcp_logs import collector as lc
        from .collectors.gcp_metrics import collector as mc
        collectors["logs"] = lc.status()
        collectors["metrics"] = mc.status()
    else:
        collectors["logs"] = {
            "collector": "local-ingest", "running": True, "tier": "LIVE",
            "note": "CogniKart posts log lines directly; set DATA_SOURCE=gcp "
                    "to read Cloud Logging instead.",
        }
    return {
        "product": "OpsMind",
        "observes": "CogniKart",
        "uptimeS": round(time.time() - _started_at, 1),
        "config": settings.as_dict(),
        "store": store.stats(),
        "collectors": collectors,
        "tiers": [
            {"tier": "LIVE", "latency": "1-5 s",
             "source": "structured logs + service heartbeats",
             "carries": "log stream, error counts, request rate, "
                        "app-measured latency, self-reported CPU/memory"},
            {"tier": "NEAR_REAL_TIME", "latency": "1-5 min",
             "source": "Cloud Monitoring system metrics",
             "carries": "platform CPU/memory utilisation, instance count, "
                        "billable instance time"},
            {"tier": "AUTHORITATIVE", "latency": "hours to ~1 day",
             "source": "Cloud Billing export to BigQuery",
             "carries": "actual billed cost by service and SKU"},
        ],
        "ruleWarnings": _rule_warnings + ruleset.validate_metrics(store),
    }


# --- overview -------------------------------------------------------------
@app.get("/api/v1/overview")
def overview(window: int = Query(15, ge=1, le=180)) -> Dict[str, Any]:
    data = kpi_engine.overview(store, window_minutes=window)
    data["cost"] = cost_engine.rate(store, window_minutes=min(window, 30))
    # The overview's cost chart needs a time axis, so attach the per-minute
    # modeled series here too rather than making the dashboard issue a second
    # round trip for the same numbers.
    data["cost"]["series"] = _cost_series(window)
    data["incidents"] = {
        "open": [i.to_dict() for i in incident_manager.open_incidents()[:5]],
        "openCount": len(incident_manager.open_incidents()),
    }
    recs = recommend_engine.generate(store, window_minutes=max(window, 15))
    data["recommendations"] = {"top": recs[:3], "total": len(recs)}

    # The action queue sits above every chart on the Overview, so it ships with
    # the overview payload rather than costing the dashboard a second request.
    anomaly_list = patterns_engine.anomalies(
        store, window_minutes=max(window, 20))["anomalies"]
    open_incs = incident_manager.open_incidents()
    inc_dicts = []
    for inc in open_incs:
        d = inc.to_dict(store)
        d["correlation"] = incident_manager.correlate(inc, store)
        inc_dicts.append(d)
    data["actions"] = actions_engine.build(
        store, incidents=inc_dicts, recommendations=recs,
        anomalies=anomaly_list,
        free_tier=cost_engine.free_tier_position(store, window_minutes=max(window, 15)),
        limit=6,
    )
    data["anomalies"] = anomaly_list[:4]
    data["dataSource"] = settings.data_source
    return data


@app.get("/api/v1/patterns")
def patterns(window: int = Query(30, ge=1, le=180),
             limit: int = Query(20, ge=1, le=60)) -> Dict[str, Any]:
    """Every log line clustered by message template, not just the failures."""
    return patterns_engine.log_patterns(store, window_minutes=window, limit=limit)


@app.get("/api/v1/anomalies")
def anomalies(window: int = Query(60, ge=5, le=180),
              sigma: float = Query(2.5, ge=1.0, le=6.0)) -> Dict[str, Any]:
    """Series departing from their own recent baseline.

    A fixed threshold cannot answer "is this unusual for this service?" --
    a service that normally serves 2 req/min jumping to 20 is a tenfold change
    no threshold would catch.
    """
    return patterns_engine.anomalies(store, window_minutes=window, sigma=sigma)


@app.get("/api/v1/actions")
def actions(window: int = Query(15, ge=1, le=180)) -> Dict[str, Any]:
    """The ranked queue: what someone should do right now."""
    incs = [i.to_dict(store) for i in incident_manager.all_incidents(limit=20)]
    for d, inc in zip(incs, incident_manager.all_incidents(limit=20)):
        if inc.status == "OPEN":
            d["correlation"] = incident_manager.correlate(inc, store)
    return actions_engine.build(
        store,
        incidents=incs,
        recommendations=recommend_engine.generate(store, window_minutes=max(window, 15)),
        anomalies=patterns_engine.anomalies(store, window_minutes=max(window, 20))["anomalies"],
        free_tier=cost_engine.free_tier_position(store, window_minutes=max(window, 15)),
    )


@app.get("/api/v1/funnel")
def funnel(window: int = Query(30, ge=1, le=180)) -> Dict[str, Any]:
    return kpi_engine.funnel(store, window_minutes=window)


# --- logs -----------------------------------------------------------------
@app.get("/api/v1/logs")
def logs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    severity: Optional[str] = None,
    service: Optional[str] = None,
    q: Optional[str] = None,
    event: Optional[str] = None,
    errorCode: Optional[str] = None,
    trace: Optional[str] = None,
    sinceS: Optional[int] = Query(None, ge=1, le=86400),
    includeHeartbeats: bool = False,
) -> Dict[str, Any]:
    services = [s for s in (service or "").split(",") if s.strip()] or None
    result = store.logs(
        limit=limit, offset=offset, min_severity=severity, services=services,
        q=q, event=event, error_code=errorCode, trace=trace,
        since=(time.time() - sinceS) if sinceS else None,
        include_heartbeats=includeHeartbeats,
    )
    result["dataSource"] = settings.data_source
    result["tier"] = "LIVE"
    return result


@app.get("/api/v1/logs/stream")
async def logs_stream(
    request: Request,
    severity: Optional[str] = None,
    service: Optional[str] = None,
    q: Optional[str] = None,
) -> StreamingResponse:
    """Server-Sent Events: new log lines pushed as they arrive.

    SSE rather than WebSocket because the data is strictly one-directional,
    it is plain HTTP (so it traverses Cloud Run with no extra configuration),
    and browsers reconnect automatically with no client library.
    """
    services = [s for s in (service or "").split(",") if s.strip()] or None

    async def gen():
        cursor = time.time()
        last_beat = 0.0
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            batch = store.logs(limit=80, min_severity=severity,
                               services=services, q=q, since=cursor)
            entries = list(reversed(batch["entries"]))  # oldest first
            if entries:
                cursor = max(e["ts"] for e in entries) + 1e-6
                yield "event: logs\ndata: %s\n\n" % json.dumps(entries, default=str)
            now = time.time()
            if now - last_beat > 5:
                last_beat = now
                yield "event: stats\ndata: %s\n\n" % json.dumps({
                    "ts": now,
                    "store": store.stats(),
                    "openIncidents": len(incident_manager.open_incidents()),
                    "evaluator": {"evaluations": evaluator.evaluations,
                                  "lastBreachCount": evaluator.last_breach_count},
                }, default=str)
            await asyncio.sleep(1.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.get("/api/v1/trace/{trace_id}")
def trace(trace_id: str) -> Dict[str, Any]:
    entries = store.trace(trace_id)
    if not entries:
        raise HTTPException(status_code=404, detail="trace not found in buffer")
    span = entries[-1]["ts"] - entries[0]["ts"] if len(entries) > 1 else 0.0
    return {
        "traceId": trace_id,
        "entryCount": len(entries),
        "services": sorted({e["service"] for e in entries}),
        "spanSeconds": round(span, 3),
        "entries": entries,
    }


# --- errors / services / metrics ------------------------------------------
@app.get("/api/v1/errors")
def errors(window: int = Query(60, ge=1, le=180),
           limit: int = Query(25, ge=1, le=100)) -> Dict[str, Any]:
    groups = store.error_groups(window_minutes=window, limit=limit)
    buckets = store.series(window_minutes=window)
    return {
        "windowMinutes": window,
        "groups": groups,
        "totals": {
            "errors5xx": sum(b.get("errors5xx", 0) for b in buckets),
            "errors4xx": sum(b.get("errors4xx", 0) for b in buckets),
            "requests": sum(b.get("requests", 0) for b in buckets),
        },
        "groupingKey": "service + errorCode + errorClass + route + "
                       "normalized message (deterministic, no ML)",
    }


@app.get("/api/v1/routes")
def routes(window: int = Query(60, ge=1, le=180),
           slow_ms: int = Query(500, ge=50, le=10000)) -> Dict[str, Any]:
    """Slowest endpoints and the slow-request count, from individual requests."""
    data = store.route_stats(window_minutes=window, slow_ms=slow_ms)
    data["windowMinutes"] = window
    return data


@app.get("/api/v1/services")
def services(window: int = Query(15, ge=1, le=180)) -> Dict[str, Any]:
    return {"windowMinutes": window,
            "services": store.service_summary(window_minutes=window),
            "heartbeats": store.heartbeats()}


@app.get("/api/v1/metrics/series")
def metrics_series(window: int = Query(60, ge=1, le=180),
                   service: Optional[str] = None) -> Dict[str, Any]:
    return {
        "windowMinutes": window,
        "service": service,
        "tier": "LIVE",
        "source": "structured logs + heartbeats",
        "points": store.series(window_minutes=window, service=service),
    }


@app.get("/api/v1/metrics/platform")
def metrics_platform() -> Dict[str, Any]:
    """Cloud Monitoring tier. Explicitly separate from the LIVE tier."""
    if settings.data_source != "gcp":
        return {
            "tier": "NEAR_REAL_TIME", "available": False,
            "reason": "DATA_SOURCE=local. Cloud Monitoring metrics require a "
                      "deployed project; set DATA_SOURCE=gcp on Cloud Run.",
            "metrics": {},
        }
    from .collectors.gcp_metrics import collector as mc
    snap = mc.snapshot()
    snap["available"] = bool(snap.get("metrics"))
    return snap


# --- alerts ---------------------------------------------------------------
@app.get("/api/v1/alerts")
def alerts() -> Dict[str, Any]:
    rules = ruleset.list()
    breaches = {b.key: b.to_dict() for b in ruleset.evaluate(store)}
    for r in rules:
        r["currentlyBreaching"] = [
            v for k, v in breaches.items() if k.startswith(r["id"] + "::")
        ]
        r["breachCount"] = len(r["currentlyBreaching"])
    return {
        "rules": rules,
        "sources": [
            {"source": "fast-path",
             "description": "Evaluated by OpsMind against the real log stream "
                            "every %.0fs. Detects in seconds."
                            % settings.rules_eval_interval_s,
             "latency": "seconds"},
            {"source": "cloud-monitoring",
             "description": "Google Cloud Monitoring alerting policy. The "
                            "GCP-native, durable alert; survives an OpsMind "
                            "restart and notifies out of band. Create one per "
                            "docs/DEPLOY.md step 9.",
             "latency": "1-3 minutes"},
        ],
        "note": "Both read real telemetry. Thresholds below are editable, "
                "which is the brief's 'set alert thresholds' step implemented "
                "inside the product.",
    }


@app.patch("/api/v1/alerts/{rule_id}")
def update_alert(rule_id: str,
                 payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    rule = ruleset.update(
        rule_id,
        threshold=payload.get("threshold"),
        enabled=payload.get("enabled"),
        window_minutes=payload.get("windowMinutes"),
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="no such rule")
    return {"updated": True, "rule": rule.to_dict()}


# --- incidents ------------------------------------------------------------
@app.get("/api/v1/incidents")
def incidents(window: int = Query(360, ge=5, le=1440)) -> Dict[str, Any]:
    """Incidents rolled up by scope: what is breaching now, what resolved.

    Grouping by scope rather than listing every episode is deliberate. A
    threshold crossed four times in an hour is one thing to fix; four rows is
    the alert fatigue that makes people stop reading the page.
    """
    data = incident_manager.grouped(store, window_minutes=window)
    for g in data["breaching"]:
        inc = incident_manager.get(g["primaryIncidentId"])
        if inc is not None:
            g["correlation"] = incident_manager.correlate(inc, store)
    root = None
    if data["breaching"]:
        root = (data["breaching"][0].get("correlation") or {}).get(
            "suspectedRootCauseService")
    data["suspectedRootCauseService"] = root
    data["dataSources"] = [
        {"label": "Operational telemetry", "freshness": "near real-time",
         "tier": "LIVE", "ok": True},
        {"label": "Billing", "freshness": "daily export",
         "tier": "AUTHORITATIVE",
         "ok": False, "note": cost_engine.billed_status()["reason"]},
    ]
    return data


@app.get("/api/v1/incidents/{incident_id}")
def incident_detail(incident_id: str) -> Dict[str, Any]:
    inc = incident_manager.get(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail="no such incident")
    data = inc.to_dict(store)
    data["evidence"] = incident_manager.evidence(inc, store)
    return data


@app.post("/api/v1/incidents/{incident_id}/analyze")
def analyze_incident(incident_id: str,
                     refresh: bool = False) -> Dict[str, Any]:
    """Grounded explanation. Falls back to a deterministic narrative built
    from the same evidence whenever Gemini is disabled or unavailable."""
    inc = incident_manager.get(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail="no such incident")
    from .ai import explain as explainer
    evidence = incident_manager.evidence(inc, store)
    result = explainer.explain(evidence, force_refresh=refresh)
    result["incidentId"] = incident_id
    result["evidence"] = evidence
    return result


# --- cost & optimization --------------------------------------------------
# --- projects -------------------------------------------------------------
@app.get("/api/v1/projects")
def projects(refresh: bool = False) -> Dict[str, Any]:
    """Google Cloud projects this service account can see.

    Real projects, read through the Cloud Resource Manager API -- not a
    configured list. Each carries whether OpsMind can actually read its logs,
    because being able to see a project and being able to read it are different
    permissions and conflating them produces a picker full of dead entries.
    """
    from .collectors.gcp_projects import directory
    return directory.list_projects(refresh=refresh)


@app.get("/api/v1/projects/{project_id}/access")
def project_access(project_id: str, force: bool = False) -> Dict[str, Any]:
    """Probe whether this project's logs are readable: one tiny Cloud Logging
    call, which is more conclusive than inferring it from an IAM policy."""
    # Local mode has one pseudo-project fed by direct ingest. It is not a
    # Google Cloud project, so probing Cloud Logging for it can only fail and
    # would tell the user to grant IAM on a project that does not exist.
    if settings.data_source != "gcp" and project_id == "local":
        return {"projectId": "local", "at": time.time(), "connected": True,
                "hasRecentLogs": store.stats().get("bufferedEntries", 0) > 0,
                "reason": "Receiving logs by direct ingest.", "howToFix": None}
    from .collectors.gcp_projects import directory
    return directory.check_access(project_id, force=force)


@app.post("/api/v1/projects/select")
def select_project(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Point OpsMind at a different project.

    One project at a time, by design: switching re-targets the collectors and
    clears the working set rather than keeping several in memory. Nothing is
    lost -- Cloud Logging holds the history and the buffer refills within a
    poll or two.
    """
    project_id = (payload.get("projectId") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="projectId is required")

    previous = settings.active_project
    active = settings.set_active_project(project_id)
    cleared = store.reset(reason="switched from %s to %s" % (previous, active))

    if settings.data_source == "gcp":
        from .collectors.gcp_logs import collector as lc
        from .collectors.gcp_metrics import collector as mc
        lc.retarget()
        mc.retarget()
    incident_manager.__init__()   # incidents belong to the project they came from

    return {
        "activeProject": active,
        "previousProject": previous,
        "cleared": cleared,
        "note": "The working set was cleared and the collectors re-pointed. "
                "Charts refill from Cloud Logging within a poll or two.",
    }


@app.get("/api/v1/notifications")
def notifications(limit: int = Query(20, ge=1, le=60)) -> Dict[str, Any]:
    """Recent incident transitions, newest first.

    Only transitions: an incident opening or resolving is news, an incident
    continuing to breach is not. Re-notifying every evaluation is how an alert
    feed becomes something people mute.
    """
    items: List[Dict[str, Any]] = []
    for t in reversed(evaluator.recent_transitions):
        for inc_id in t.get("opened", []):
            inc = incident_manager.get(inc_id)
            if inc:
                items.append({
                    "ts": t["ts"], "kind": "opened", "severity": inc.severity,
                    "title": inc.title(), "summary": inc.summary(),
                    "incidentId": inc.id, "service": inc.service,
                    "episode": inc.episode,
                })
        for inc_id in t.get("resolved", []):
            inc = incident_manager.get(inc_id)
            if inc:
                items.append({
                    "ts": t["ts"], "kind": "resolved", "severity": inc.severity,
                    "title": inc.title(), "summary": "Stopped breaching.",
                    "incidentId": inc.id, "service": inc.service,
                    "episode": inc.episode,
                })
    items.sort(key=lambda x: x["ts"], reverse=True)
    unread = sum(1 for i in items if i["kind"] == "opened")
    return {"notifications": items[:limit], "total": len(items),
            "unread": unread,
            "note": "Transitions only. A condition that keeps breaching is one "
                    "notification, not one per evaluation."}


@app.get("/api/v1/cost")
def cost(window: int = Query(15, ge=1, le=180)) -> Dict[str, Any]:
    data = cost_engine.rate(store, window_minutes=window)
    data["series"] = _cost_series(window)
    return data


def _cost_series(window: int) -> List[Dict[str, Any]]:
    """Per-minute modeled cost, so the cost chart has a time axis."""
    out = []
    for b in store.series(window_minutes=window):
        usage = {"requests": float(b.get("requests", 0)),
                 "logBytes": float(b.get("logBytes", 0)),
                 "requestSeconds": 0.0}
        n = b.get("requests", 0) or 0
        p50, p95 = b.get("p50LatencyMs"), b.get("p95LatencyMs")
        if p50 is not None:
            est = p50 if p95 is None else (0.75 * p50 + 0.25 * p95)
            usage["requestSeconds"] = (est / 1000.0) * n
        priced = cost_engine._price_service("__all__", usage, 60.0)
        out.append({"ts": b["ts"], "minute": b["minute"],
                    "usdPerHour": priced["usdPerHour"]})
    return out


@app.get("/api/v1/cost/freetier")
def cost_freetier(window: int = Query(30, ge=1, le=180)) -> Dict[str, Any]:
    return cost_engine.free_tier_position(store, window_minutes=window)


@app.get("/api/v1/cost/billed")
def cost_billed() -> Dict[str, Any]:
    return cost_engine.billed_status()


@app.get("/api/v1/recommendations")
def recommendations(window: int = Query(30, ge=1, le=180)) -> Dict[str, Any]:
    recs = recommend_engine.generate(store, window_minutes=window)
    calculated = [r for r in recs if r["estimatedSavingUsdPerMonth"] is not None]
    return {
        "windowMinutes": window,
        "recommendations": recs,
        "count": len(recs),
        "totalCalculatedSavingUsdPerMonth": round(
            sum(r["estimatedSavingUsdPerMonth"] for r in calculated), 4),
        "googleRecommender": recommend_engine.google_recommender_status(),
        "note": "Savings are shown only where calculated from published list "
                "prices; everything else is labelled a potential opportunity.",
    }


# --- dashboard ------------------------------------------------------------
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


def _asset_version() -> str:
    """A fingerprint of the dashboard assets.

    Appended to the script and stylesheet URLs so a redeploy cannot leave a
    browser running yesterday's JavaScript against today's API. Without it the
    page is cached indefinitely and a user sees a half-updated dashboard with
    no obvious way to fix it.
    """
    newest = 0.0
    for name in ("app.js", "styles.css", "index.html",
                 "landing.html", "landing.css", "landing.js",
                 "how-it-works.html", "about.html"):
        path = os.path.join(_STATIC, name)
        if os.path.exists(path):
            newest = max(newest, os.path.getmtime(path))
    return hashlib.sha1(str(newest).encode()).hexdigest()[:10]


def _page(filename: str) -> Any:
    path = os.path.join(_STATIC, filename)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    v = _asset_version()
    for asset in ("app.js", "styles.css", "landing.js", "landing.css"):
        html = html.replace("/static/" + asset, "/static/%s?v=%s" % (asset, v))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def landing() -> Any:
    """The product page. Deliberately a separate document from the dashboard:
    it has its own visual language and should not pay for the dashboard's
    JavaScript before anyone has pressed anything."""
    page = _page("landing.html")
    return page if page is not None else dashboard()


@app.get("/how-it-works", include_in_schema=False)
def how_it_works() -> Any:
    return _page("how-it-works.html") or landing()


@app.get("/about", include_in_schema=False)
def about() -> Any:
    return _page("about.html") or landing()


@app.get("/app", include_in_schema=False)
def dashboard() -> Any:
    index = os.path.join(_STATIC, "index.html")
    if not os.path.exists(index):
        return JSONResponse({
            "product": "OpsMind",
            "status": "API running; dashboard assets not found",
            "api": "/docs",
        })
    return _page("index.html")
