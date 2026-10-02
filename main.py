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
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .collectors import local as local_collector
from .collectors.evaluator import evaluator
from .config import settings
from .engine import cost as cost_engine
from .engine import kpi as kpi_engine
from .engine import recommend as recommend_engine
from .engine.incidents import manager as incident_manager
from .engine.rules import ruleset
from .store import store

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")

# CogniKart gateway, used only to proxy demo/chaos controls from the dashboard.
# This is a demo convenience, not part of the monitoring data path: OpsMind
# reads telemetry from Google Cloud, never from CogniKart directly.
COGNIKART_GATEWAY_URL = os.environ.get(
    "COGNIKART_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")

app = FastAPI(
    title="OpsMind",
    version="1.0.0",
    description="Cloud log monitoring, resource visibility and cost "
                "optimization portal (Cognizant GCP Hackathon, Use Case 2).",
)

_started_at = time.time()


# --- auth -----------------------------------------------------------------
def _check_token(request: Request) -> None:
    """Optional shared-secret gate.

    OpsMind holds read access to your project's logs, so a public URL without
    a token would expose them. When DASHBOARD_TOKEN is unset the portal is
    open, which is fine locally and called out in docs/DEPLOY.md.
    """
    if not settings.dashboard_token:
        return
    supplied = (request.headers.get("x-opsmind-token")
                or request.query_params.get("token", ""))
    if supplied != settings.dashboard_token:
        raise HTTPException(status_code=401, detail="invalid or missing token")


@app.middleware("http")
async def guard(request: Request, call_next):
    open_paths = ("/healthz", "/readyz", "/internal/ingest", "/favicon.ico")
    if settings.dashboard_token and not request.url.path.startswith(open_paths):
        try:
            _check_token(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code,
                                content={"error": exc.detail})
    return await call_next(request)


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
        "demoGatewayConfigured": bool(COGNIKART_GATEWAY_URL),
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
    data["dataSource"] = settings.data_source
    return data


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
def incidents(status: Optional[str] = None,
              limit: int = Query(30, ge=1, le=100)) -> Dict[str, Any]:
    items = incident_manager.all_incidents(limit=limit)
    if status:
        items = [i for i in items if i.status == status.upper()]
    out = []
    for i in items:
        d = i.to_dict(store)
        # One cascade legitimately trips several rules on several services.
        # Correlation lets the UI present them as one story with a named root
        # cause instead of four unexplained red rows.
        if i.status == "OPEN":
            d["correlation"] = incident_manager.correlate(i, store)
        out.append(d)
    open_incs = incident_manager.open_incidents()
    root = None
    if open_incs:
        root = incident_manager.correlate(
            open_incs[-1], store).get("suspectedRootCauseService")
    return {
        "incidents": out,
        "openCount": len(open_incs),
        "suspectedRootCauseService": root,
    }


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


# --- demo control (proxy to CogniKart) ------------------------------------
@app.get("/api/v1/demo/scenarios")
async def demo_scenarios() -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(COGNIKART_GATEWAY_URL + "/api/admin/chaos/scenarios")
        return r.json()
    except Exception as exc:
        return {"scenarios": [], "error": "%s: %s" % (type(exc).__name__, exc),
                "gateway": COGNIKART_GATEWAY_URL}


@app.post("/api/v1/demo/scenario")
async def demo_apply(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Trigger a CogniKart chaos scenario from the dashboard.

    Demo convenience only. This is the one place OpsMind talks to CogniKart,
    and it carries no telemetry -- it exists so a presenter can inject a
    failure with one click instead of switching to a terminal.
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(COGNIKART_GATEWAY_URL + "/api/admin/chaos/scenario",
                             json=payload)
        return r.json()
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail="cannot reach CogniKart gateway at %s (%s)"
                                   % (COGNIKART_GATEWAY_URL, exc))


@app.get("/api/v1/demo/state")
async def demo_state() -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(COGNIKART_GATEWAY_URL + "/api/admin/chaos/state")
        return r.json()
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


# --- dashboard ------------------------------------------------------------
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> Any:
    index = os.path.join(_STATIC, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse({
        "product": "OpsMind",
        "status": "API running; dashboard assets not found",
        "api": "/docs",
    })
