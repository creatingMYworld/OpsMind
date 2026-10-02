"""Overview KPIs: the numbers a judge should understand in under 30 seconds.

Two families, deliberately kept distinct in the response so the UI can label
their provenance:

  technical -- requests, error rate, latency, log volume. Measured.
  business  -- checkout funnel, revenue captured, revenue at risk. Measured
               from cartValueInr in the log stream.

The health score is a transparent weighted penalty, not a black box. It is
reported alongside the components that produced it so nobody has to trust it
blindly.
"""
import time
from typing import Any, Dict, List, Optional

from ..config import settings


def _pct(numerator: float, denominator: float) -> Optional[float]:
    return round(100.0 * numerator / denominator, 2) if denominator else None


def overview(store, window_minutes: int = 15) -> Dict[str, Any]:
    buckets = store.series(window_minutes=window_minutes)
    summaries = store.service_summary(window_minutes=window_minutes)

    requests = sum(b.get("requests", 0) for b in buckets)
    e5 = sum(b.get("errors5xx", 0) for b in buckets)
    e4 = sum(b.get("errors4xx", 0) for b in buckets)
    log_bytes = sum(b.get("logBytes", 0) for b in buckets)
    lines = sum(b.get("lines", 0) for b in buckets)

    started = sum(b.get("checkoutsStarted", 0) for b in buckets)
    confirmed = sum(b.get("checkoutsConfirmed", 0) for b in buckets)
    failed = sum(b.get("checkoutsFailed", 0) for b in buckets)
    settled = confirmed + failed
    rev_ok = round(sum(b.get("revenueConfirmedInr", 0.0) for b in buckets), 2)
    rev_risk = round(sum(b.get("revenueFailedInr", 0.0) for b in buckets), 2)

    lat: List[float] = []
    for s in summaries:
        if s.get("p95LatencyMs") is not None:
            lat.append(s["p95LatencyMs"])
    worst_p95 = max(lat) if lat else None

    error_rate = (e5 / float(requests)) if requests else 0.0
    minutes = max(len(buckets), 1)

    # Transparent health score: start at 100, subtract named penalties.
    penalties: List[Dict[str, Any]] = []
    score = 100.0
    if requests:
        p = min(50.0, error_rate * 100.0 * 5.0)
        if p > 0.5:
            penalties.append({"reason": "5xx error rate %.2f%%" % (error_rate * 100),
                              "penalty": round(p, 1)})
            score -= p
    if worst_p95 and worst_p95 > 1000:
        p = min(25.0, (worst_p95 - 1000) / 200.0)
        penalties.append({"reason": "worst service p95 latency %.0fms" % worst_p95,
                          "penalty": round(p, 1)})
        score -= p
    if settled and confirmed / float(settled) < 0.9:
        p = min(25.0, (0.9 - confirmed / float(settled)) * 100.0)
        penalties.append({"reason": "checkout success %.0f%%"
                                    % (100.0 * confirmed / settled),
                          "penalty": round(p, 1)})
        score -= p
    unhealthy = [s["service"] for s in summaries if not s.get("healthy")]
    score = max(0.0, round(score, 1))

    if score >= 90:
        status, label = "HEALTHY", "All systems normal"
    elif score >= 70:
        status, label = "DEGRADED", "Degraded performance"
    elif score >= 40:
        status, label = "IMPAIRED", "Service impairment"
    else:
        status, label = "CRITICAL", "Critical failure"

    return {
        "windowMinutes": window_minutes,
        "generatedAt": time.time(),
        "health": {
            "score": score,
            "status": status,
            "label": label,
            "penalties": penalties,
            "unhealthyServices": unhealthy,
            "basis": "100 minus weighted penalties for 5xx rate, worst-service "
                     "p95 latency and checkout success rate.",
        },
        "technical": {
            "kind": "measured",
            "requests": requests,
            "requestsPerMin": round(requests / float(minutes), 2),
            "errors5xx": e5,
            "errors4xx": e4,
            "errorRatePct": _pct(e5, requests) or 0.0,
            "clientErrorRatePct": _pct(e4, requests) or 0.0,
            "worstServiceP95Ms": worst_p95,
            "logLines": lines,
            "logMib": round(log_bytes / 1048576.0, 3),
            "logMibPerMin": round(log_bytes / 1048576.0 / minutes, 4),
            "servicesReporting": len(summaries),
        },
        "business": {
            "kind": "measured",
            "checkoutsStarted": started,
            "checkoutsConfirmed": confirmed,
            "checkoutsFailed": failed,
            "checkoutSuccessRatePct": _pct(confirmed, settled),
            "revenueCapturedInr": rev_ok,
            "revenueAtRiskInr": rev_risk,
            "avgOrderValueInr": round(rev_ok / confirmed, 2) if confirmed else None,
            "basis": "Derived from checkout.started / checkout.confirmed / "
                     "checkout.failed events and cartValueInr in the log stream.",
        },
        "services": summaries,
    }


def funnel(store, window_minutes: int = 30) -> Dict[str, Any]:
    """The browse -> cart -> checkout -> paid conversion funnel."""
    page = store.logs(limit=4000, since=time.time() - window_minutes * 60)
    counts: Dict[str, int] = {}
    for e in page["entries"]:
        ev = e.get("event", "")
        if ev in ("catalog.product.list", "catalog.product.view",
                  "cart.item.added", "checkout.started",
                  "checkout.confirmed", "checkout.failed"):
            counts[ev] = counts.get(ev, 0) + 1

    stages = [
        {"stage": "Browsed", "event": "catalog.product.list",
         "count": counts.get("catalog.product.list", 0)},
        {"stage": "Viewed product", "event": "catalog.product.view",
         "count": counts.get("catalog.product.view", 0)},
        {"stage": "Added to cart", "event": "cart.item.added",
         "count": counts.get("cart.item.added", 0)},
        {"stage": "Started checkout", "event": "checkout.started",
         "count": counts.get("checkout.started", 0)},
        {"stage": "Paid", "event": "checkout.confirmed",
         "count": counts.get("checkout.confirmed", 0)},
    ]
    top = stages[0]["count"] or 1
    for s in stages:
        s["pctOfTop"] = round(100.0 * s["count"] / top, 1)
    return {
        "windowMinutes": window_minutes,
        "stages": stages,
        "failedCheckouts": counts.get("checkout.failed", 0),
        "kind": "measured",
    }
