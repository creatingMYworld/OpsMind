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
import re
import secrets
import threading
import time
from html import escape as _esc
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import accounts
from .collectors import local as local_collector
from .collectors.evaluator import evaluator
from . import history
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
#
# A signed-in account is the other way through the gate. Creating one needs the
# token (see accounts.py), so accounts widen nothing: they are a way back in
# without keeping the token in a bookmark. The sign-in and sign-up pages and
# the /api/v1/auth/ endpoints are open, because they are how a person gets in;
# each endpoint checks for itself what it needs.
TOKEN_COOKIE = "opsmind_token"
# The product pages carry no telemetry -- they are marketing copy, and the one
# dynamic call on the landing page (/api/v1/meta) stays gated and fails closed,
# keeping its static text. Leaving them shut made the logo on /signin a dead
# loop: it links to /, which bounced straight back to /signin. The dashboard
# and the whole API are unaffected.
_OPEN_PATHS = ("/healthz", "/readyz", "/internal/ingest", "/favicon.ico",
               "/signin", "/signup", "/", "/about", "/how-it-works")
_OPEN_PREFIXES = ("/static/", "/api/v1/auth/")


def _supplied_token(request: Request) -> str:
    return (request.headers.get("x-opsmind-token")
            or request.query_params.get("token", "")
            or request.cookies.get(TOKEN_COOKIE, ""))


def _is_token(value: str) -> bool:
    # Bytes, so a non-ASCII value is a failed match rather than a TypeError.
    return bool(settings.dashboard_token) and secrets.compare_digest(
        (value or "").encode("utf-8"), settings.dashboard_token.encode("utf-8"))


def _session(request: Request) -> Optional[Dict[str, Any]]:
    """Who signed in, from the cookie alone. Cheap enough for every request."""
    return accounts.read_session(request.cookies.get(accounts.SESSION_COOKIE, ""))


def _account_user(request: Request) -> Optional[Dict[str, Any]]:
    """The signed-in person, if their account still exists. For pages.

    The cookie proves who signed in; the lookup catches an account that has
    since gone (a memory-held one after a restart), which would otherwise
    show an avatar for nobody. If the store cannot be reached, the signed
    cookie is trusted rather than locking everyone out.
    """
    user = _session(request)
    if not user:
        return None
    try:
        return user if accounts.get(user["id"]) is not None else None
    except accounts.AccountsUnavailable:
        return user


def _https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "") or request.url.scheme
    return proto == "https"


@app.middleware("http")
async def guard(request: Request, call_next):
    if not settings.dashboard_token:
        return await call_next(request)

    path = request.url.path
    is_open = path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)
    if (not is_open and not _is_token(_supplied_token(request))
            and _session(request) is None):
        # An unauthenticated page request should land somewhere a person can
        # act on: the sign-in page, which brings them back here afterwards.
        if request.headers.get("accept", "").startswith("text/html"):
            return RedirectResponse("/signin?next=" + quote(path, safe="/"),
                                    status_code=303)
        return JSONResponse(status_code=401,
                            content={"error": "invalid or missing token"})

    response = await call_next(request)
    # Exchange a query-string token for a cookie, so the page's own fetches
    # are authenticated and the token stops appearing in every URL. Open
    # paths too: /signup?token=... hides the access-code field, so the sign-up
    # that follows has to carry the token some other way.
    if (_is_token(request.query_params.get("token", ""))
            and not request.cookies.get(TOKEN_COOKIE)):
        response.set_cookie(
            TOKEN_COOKIE, settings.dashboard_token, httponly=True,
            samesite="lax", secure=_https(request), max_age=12 * 3600, path="/")
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
    if history.enabled():
        _history_writer.start()


class _HistoryWriter:
    """Folds the live store into Firestore on its own thread.

    Deliberately not on the request path and deliberately not awaited: a slow
    or unreachable Firestore must cost the dashboard nothing. Every failure is
    swallowed here and reported through /api/v1/meta instead.
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="history-writer")
        self._thread.start()

    def _loop(self) -> None:
        # One interval of grace, so the first write has real minutes to fold
        # rather than writing an almost-empty day the moment the portal boots.
        time.sleep(min(settings.history_write_interval_s, 60.0))
        while True:
            try:
                rate = cost_engine.rate(store, window_minutes=15)
                history.write_rollup(store, rate.get("usdPerHour"))
            except Exception:  # noqa: BLE001 - history must never break live
                pass
            time.sleep(max(settings.history_write_interval_s, 30.0))


_history_writer = _HistoryWriter()


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
    collectors["history"] = history.status()
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
    status: Optional[int] = Query(None, ge=1, le=5),
    route: Optional[str] = None,
    errorCode: Optional[str] = None,
    trace: Optional[str] = None,
    sinceS: Optional[int] = Query(None, ge=1, le=86400),
    includeHeartbeats: bool = False,
) -> Dict[str, Any]:
    services = [s for s in (service or "").split(",") if s.strip()] or None
    result = store.logs(
        limit=limit, offset=offset, min_severity=severity, services=services,
        q=q, event=event, status_class=status, route=route,
        error_code=errorCode, trace=trace,
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
    event: Optional[str] = None,
    status: Optional[int] = Query(None, ge=1, le=5),
    route: Optional[str] = None,
) -> StreamingResponse:
    """Server-Sent Events: new log lines pushed as they arrive.

    SSE rather than WebSocket because the data is strictly one-directional,
    it is plain HTTP (so it traverses Cloud Run with no extra configuration),
    and browsers reconnect automatically with no client library.
    """
    services = [s for s in (service or "").split(",") if s.strip()] or None

    async def gen():
        cursor = time.time()
        notified_to = cursor      # transitions after this have not been pushed
        last_beat = 0.0
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            batch = store.logs(limit=80, min_severity=severity,
                               services=services, q=q, event=event,
                               status_class=status, route=route, since=cursor)
            entries = list(reversed(batch["entries"]))  # oldest first
            if entries:
                cursor = max(e["ts"] for e in entries) + 1e-6
                yield "event: logs\ndata: %s\n\n" % json.dumps(entries, default=str)
            # An incident opening or resolving is pushed the moment the
            # evaluator records it, so the dashboard needs no polling to know,
            # and a background tab hears about it too.
            fresh = [t for t in evaluator.recent_transitions
                     if t.get("ts", 0) > notified_to]
            if fresh:
                notified_to = max(t["ts"] for t in fresh)
                yield "event: notify\ndata: %s\n\n" % json.dumps(
                    _notification_items(fresh), default=str)
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
           limit: int = Query(25, ge=1, le=100),
           severity: Optional[str] = None) -> Dict[str, Any]:
    groups = store.error_groups(window_minutes=window, limit=limit,
                                min_severity=severity)
    buckets = store.series(window_minutes=window)
    return {
        "windowMinutes": window,
        "groups": groups,
        "totals": {
            "errors5xx": sum(b.get("errors5xx", 0) for b in buckets),
            "errors4xx": sum(b.get("errors4xx", 0) for b in buckets),
            "requests": sum(b.get("requests", 0) for b in buckets),
        },
        "severityFloor": (severity or "").upper() or None,
        "groupingKey": "service + errorCode + errorClass + route + "
                       "normalized message (deterministic, no ML)",
    }


@app.get("/api/v1/routes")
def routes(window: int = Query(60, ge=1, le=180),
           slow_ms: int = Query(500, ge=50, le=10000),
           limit: int = Query(10, ge=1, le=200)) -> Dict[str, Any]:
    """Slowest endpoints and the slow-request count, from individual requests."""
    data = store.route_stats(window_minutes=window, slow_ms=slow_ms, limit=limit)
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


# --- product guide --------------------------------------------------------
@app.post("/api/v1/guide")
def guide(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """Answer a question about OpsMind itself.

    Grounded in a manifest generated from the running application, so it cannot
    describe a view that does not exist or report a configuration that is no
    longer true. Answers deterministically when no model is configured.
    """
    from .ai import guide as guide_engine
    return guide_engine.ask(payload.get("question", ""), store=store)


@app.get("/api/v1/guide/manifest")
def guide_manifest() -> Dict[str, Any]:
    """What the guide is allowed to know. Exposed so its grounding is
    inspectable rather than taken on trust."""
    from .ai import guide as guide_engine
    data = guide_engine.manifest(store)
    data["suggestions"] = guide_engine.suggestions()
    return data


@app.get("/api/v1/notifications")
def notifications(limit: int = Query(20, ge=1, le=60)) -> Dict[str, Any]:
    """Recent incident transitions, newest first.

    Only transitions: an incident opening or resolving is news, an incident
    continuing to breach is not. Re-notifying every evaluation is how an alert
    feed becomes something people mute.
    """
    items = _notification_items(evaluator.recent_transitions)
    unread = sum(1 for i in items if i["kind"] == "opened")
    return {"notifications": items[:limit], "total": len(items),
            "unread": unread,
            "note": "Transitions only. A condition that keeps breaching is one "
                    "notification, not one per evaluation."}


def _notification_items(transitions) -> List[Dict[str, Any]]:
    """Incident transitions as notification rows, newest first."""
    items: List[Dict[str, Any]] = []
    for t in reversed(transitions):
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
    return items


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
def cost_billed(days: int = Query(7, ge=1, le=60),
                refresh: bool = False) -> Dict[str, Any]:
    """The billed tier. `refresh=true` skips the 15-minute cache, which is
    what you want right after enabling an export and wondering whether rows
    have landed yet."""
    if refresh:
        from .collectors import gcp_billing
        return gcp_billing.status(days=days, force=True)
    return cost_engine.billed_status(days=days)


@app.get("/api/v1/cost/reconcile")
def cost_reconcile(days: int = Query(7, ge=1, le=60),
                   window: int = Query(60, ge=1, le=180)) -> Dict[str, Any]:
    """Modeled cost checked against Google's own billing export.

    Answers the obvious question about a modeled number -- is it right? --
    with a figure rather than an assurance, and says why it cannot when there
    is no billed data to check against.
    """
    from .collectors import gcp_billing

    rate = cost_engine.rate(store, window_minutes=window)
    return gcp_billing.reconcile(rate.get("usdPerHour"), days=days)


@app.get("/api/v1/cost/summary")
def cost_summary(window: int = Query(30, ge=1, le=180)) -> Dict[str, Any]:
    """A written summary of the cost picture, for the Optimization Center.

    Gemini writes it when AI is enabled; otherwise the same numbers are
    summarised deterministically and the response says `ai: false`, so the UI
    only tags text a model actually wrote.
    """
    from .ai import explain as explainer

    c = cost_engine.rate(store, window_minutes=window)
    recs = recommend_engine.generate(store, window_minutes=max(window, 15))
    ctx = {
        "cost": {k: c.get(k) for k in ("usdPerHour", "projectedUsdPerDay",
                                       "projectedUsdPerMonth", "byDriver")},
        "byService": [{"service": s["service"],
                       "usdPerHour": s["usdPerHour"]["total"]}
                      for s in c.get("byService", [])],
        "recommendations": [{k: r.get(k) for k in
                             ("title", "severity", "recommendation",
                              "estimatedSavingUsdPerMonth", "observedMetric",
                              "observedValue", "unit")} for r in recs],
    }
    return explainer.summarize_cost(ctx)


@app.get("/api/v1/history/compare")
def history_compare(project: Optional[str] = None) -> Dict[str, Any]:
    """Today against yesterday, from the Firestore daily rollups.

    Returns `available: false` with a message when there is not a full day on
    both sides. Nothing is estimated to fill a gap -- a comparison against a
    day that was not observed would be worse than no comparison.
    """
    return history.compare(project)


@app.get("/api/v1/history/days")
def history_days(project: Optional[str] = None,
                 days: int = Query(7, ge=2, le=30)) -> Dict[str, Any]:
    """The last N daily rollups, oldest first. Days with no data are omitted
    rather than zero-filled, because a day the portal was not running is not
    a day with no traffic."""
    return history.recent(project, days)


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
                 "how-it-works.html", "about.html",
                 "signin.html", "signup.html", "account.html", "auth.js"):
        path = os.path.join(_STATIC, name)
        if os.path.exists(path):
            newest = max(newest, os.path.getmtime(path))
    return hashlib.sha1(str(newest).encode()).hexdigest()[:10]


# The nav's sign-in button sits between these markers. For a signed-in person
# it is swapped for their avatar on the server, so the page never flashes the
# wrong one while a script works it out.
_NAV_AUTH = re.compile(r"<!--auth-->.*?<!--/auth-->", re.S)
# The sign-up page's access-code field, dropped when the person already
# arrived through the token link.
_ACCESS_CODE = re.compile(r"<!--access-code-->.*?<!--/access-code-->", re.S)


def _page(filename: str, request: Optional[Request] = None,
          strip_access_code: bool = False) -> Any:
    path = os.path.join(_STATIC, filename)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    v = _asset_version()
    for asset in ("app.js", "styles.css", "landing.js", "landing.css", "auth.js"):
        html = html.replace("/static/" + asset, "/static/%s?v=%s" % (asset, v))
    user = _account_user(request) if request is not None else None
    if user:
        current = ' aria-current="page"' if filename == "account.html" else ""
        html = _NAV_AUTH.sub(
            '<a class="nav-profile" href="/account" title="%s"%s>'
            '<span class="nav-avatar">%s</span></a>' % (
                _esc("Account · " + (user["name"] or user["email"])), current,
                _esc(user["initials"])),
            html)
    if strip_access_code:
        html = _ACCESS_CODE.sub("", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def landing(request: Request) -> Any:
    """The product page. Deliberately a separate document from the dashboard:
    it has its own visual language and should not pay for the dashboard's
    JavaScript before anyone has pressed anything."""
    page = _page("landing.html", request)
    return page if page is not None else dashboard(request)


@app.get("/how-it-works", include_in_schema=False)
def how_it_works(request: Request) -> Any:
    return _page("how-it-works.html", request) or landing(request)


@app.get("/about", include_in_schema=False)
def about(request: Request) -> Any:
    return _page("about.html", request) or landing(request)


# --- accounts -------------------------------------------------------------

def _safe_next(value: Optional[str]) -> str:
    """Only a path on this site: never a redirect somewhere else."""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


@app.get("/signin", include_in_schema=False)
def signin_page(request: Request, next_: str = Query("/", alias="next")) -> Any:
    if _account_user(request):
        return RedirectResponse(_safe_next(next_), status_code=303)
    return _page("signin.html")


@app.get("/signup", include_in_schema=False)
def signup_page(request: Request, next_: str = Query("/", alias="next")) -> Any:
    if _account_user(request):
        return RedirectResponse(_safe_next(next_), status_code=303)
    needs_code = bool(settings.dashboard_token) and not _is_token(
        _supplied_token(request))
    return _page("signup.html", strip_access_code=not needs_code)


@app.get("/account", include_in_schema=False)
def account_page(request: Request) -> Any:
    if _account_user(request) is None:
        return RedirectResponse("/signin?next=/account", status_code=303)
    return _page("account.html", request)


def _accounts_call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    except accounts.AccountsUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Accounts are unavailable right now (%s). See DEPLOY.md, "
                   "Part 10, to set up Firestore." % exc)


def _signed_in(response: JSONResponse, request: Request,
               row: Dict[str, Any]) -> JSONResponse:
    response.set_cookie(
        accounts.SESSION_COOKIE, accounts.issue_session(row), httponly=True,
        samesite="lax", secure=_https(request),
        max_age=accounts.SESSION_MAX_AGE_S, path="/")
    return response


def _require_session(request: Request) -> Dict[str, Any]:
    user = _session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in first.")
    return user


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@app.post("/api/v1/auth/signup")
def auth_signup(request: Request, body: Dict[str, Any] = Body(...)) -> Any:
    # The token is the invitation. Without it, sign-up would be a public door
    # to the project's logs.
    if (settings.dashboard_token and not _is_token(_supplied_token(request))
            and not _is_token(str(body.get("accessCode") or "").strip())):
        raise HTTPException(
            status_code=403,
            detail="That access code is not right. Ask whoever runs this "
                   "OpsMind workspace for it.")
    row = _accounts_call(accounts.sign_up, body.get("name"), body.get("email"),
                         body.get("password"))
    return _signed_in(JSONResponse({"user": accounts.public(row)}), request, row)


@app.post("/api/v1/auth/signin")
def auth_signin(request: Request, body: Dict[str, Any] = Body(...)) -> Any:
    row = _accounts_call(accounts.sign_in, body.get("email"),
                         body.get("password"), _client_ip(request))
    return _signed_in(JSONResponse({"user": accounts.public(row)}), request, row)


@app.post("/api/v1/auth/signout")
def auth_signout() -> Any:
    # Both cookies: signing out of a browser that also holds the token link's
    # cookie should leave it signed out, not quietly still let in.
    response = JSONResponse({"ok": True})
    response.delete_cookie(accounts.SESSION_COOKIE, path="/")
    response.delete_cookie(TOKEN_COOKIE, path="/")
    return response


@app.get("/api/v1/auth/me")
def auth_me(request: Request) -> Any:
    user = _session(request)
    store_status = _accounts_call(accounts.status)
    if not user:
        return {"user": None, "accounts": store_status}
    row = _accounts_call(accounts.get, user["id"])
    if row is None:
        # A valid cookie for an account that is gone: a memory-held account
        # after a restart. Say so and clear the cookie rather than pretend.
        response = JSONResponse({"user": None, "accounts": store_status})
        response.delete_cookie(accounts.SESSION_COOKIE, path="/")
        return response
    return {"user": accounts.public(row), "accounts": store_status}


@app.patch("/api/v1/auth/me")
def auth_update(request: Request, body: Dict[str, Any] = Body(...)) -> Any:
    user = _require_session(request)
    row = _accounts_call(accounts.rename, user["id"], body.get("name"))
    # Re-issued so the nav shows the new name on the next page.
    return _signed_in(JSONResponse({"user": accounts.public(row)}), request, row)


@app.post("/api/v1/auth/password")
def auth_password(request: Request, body: Dict[str, Any] = Body(...)) -> Any:
    user = _require_session(request)
    row = _accounts_call(accounts.change_password, user["id"],
                         body.get("currentPassword"), body.get("newPassword"))
    return _signed_in(JSONResponse({"user": accounts.public(row)}), request, row)


@app.get("/app", include_in_schema=False)
def dashboard(request: Request) -> Any:
    # Every Start button and Dashboard link lands here, so this is where
    # "sign in first" is decided -- for the page, gate or no gate. The API
    # keeps accepting the token as well, so scripts and the DEPLOY.md checks
    # still work without an account.
    if _account_user(request) is None:
        response = RedirectResponse("/signin?next=/app", status_code=303)
        response.delete_cookie(accounts.SESSION_COOKIE, path="/")
        return response
    index = os.path.join(_STATIC, "index.html")
    if not os.path.exists(index):
        return JSONResponse({
            "product": "OpsMind",
            "status": "API running; dashboard assets not found",
            "api": "/docs",
        })
    return _page("index.html")
