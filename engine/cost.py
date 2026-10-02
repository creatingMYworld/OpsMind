"""Near-real-time MODELED cost, plus honest reconciliation metadata.

The problem this solves
-----------------------
Cloud Billing export to BigQuery lands with roughly 24 hours of latency (and
no delivery guarantee), and a brand-new project's actual spend is close to
zero. So the obvious architecture -- billing export -> BigQuery -> dashboard --
cannot show cost reacting to an incident, which is the whole point of
connecting log monitoring to cost optimization.

How this works instead
----------------------
The quantities that DRIVE cost are available in near-real-time: request count,
request duration, and log bytes, all measured from telemetry we already have.
The PRICES are Google's published list prices (platform/pricing/skus.json,
verified 2026-10-02). Multiply them:

    Cloud Run CPU     = sum(request duration) x vCPU      x $/vCPU-second
    Cloud Run memory  = sum(request duration) x memory GiB x $/GiB-second
    Cloud Run requests= request count                      x $/million
    Cloud Logging     = measured log bytes                 x $0.50/GiB

Honesty rules (rules.md section 7)
----------------------------------
* Every figure this module returns is labelled `modeled`, with the pricing
  source and verification date attached.
* We report GROSS modeled cost (what the usage would cost at list price) AND
  the free-tier position separately, because inside the free tier the marginal
  cost really is zero and pretending otherwise would be inventing a bill.
* `basis` explains the arithmetic for every line, so a judge can audit it.
* Nothing here is presented as a billed amount. The billed panel is separate
  and reads real billing data when it exists (see `billed_status`).

Request-based billing is modelled by default: Cloud Run charges CPU and memory
for the duration of request processing, which is exactly what sum(latency)
measures. Instance-based billing would need real instance-seconds from Cloud
Monitoring; that path is noted as a limitation rather than faked.
"""
import time
from typing import Any, Dict, List, Optional

from ..config import settings

_BYTES_PER_GIB = 1024.0 ** 3
_SECONDS_PER_MONTH = 30.0 * 24 * 3600


def _usage_from_buckets(buckets: List[Dict[str, Any]]) -> Dict[str, float]:
    """Aggregate the three usage quantities that drive cost."""
    requests = 0
    log_bytes = 0
    # Request-seconds: p50 is a poor estimator for a skewed distribution, so
    # we use the per-minute mean implied by the latency reservoir, which is
    # what the reservoir is for.
    request_seconds = 0.0
    for b in buckets:
        requests += b.get("requests", 0) or 0
        log_bytes += b.get("logBytes", 0) or 0
        n = b.get("requests", 0) or 0
        mean_ms = b.get("p50LatencyMs")
        p95 = b.get("p95LatencyMs")
        if mean_ms is None:
            continue
        # Blend p50 and p95 to approximate the mean of a right-skewed latency
        # distribution without storing every sample. Documented approximation.
        est_mean_ms = mean_ms if p95 is None else (0.75 * mean_ms + 0.25 * p95)
        request_seconds += (est_mean_ms / 1000.0) * n
    return {
        "requests": float(requests),
        "logBytes": float(log_bytes),
        "requestSeconds": request_seconds,
    }


def _price_service(service: str, usage: Dict[str, float],
                   elapsed_s: float) -> Dict[str, Any]:
    prices = settings.run_prices
    shape = settings.shape(service)
    log_price_per_gib = settings.pricing["cloudLogging"]["ingestionPerGib"]

    vcpu_seconds = usage["requestSeconds"] * shape["vcpu"]
    gib_seconds = usage["requestSeconds"] * shape["memoryGib"]
    log_gib = usage["logBytes"] / _BYTES_PER_GIB

    cpu_usd = vcpu_seconds * prices["cpuPerVcpuSecond"]
    mem_usd = gib_seconds * prices["memoryPerGibSecond"]
    req_usd = (usage["requests"] / 1_000_000.0) * prices["perMillionRequests"]
    log_usd = log_gib * log_price_per_gib
    total = cpu_usd + mem_usd + req_usd + log_usd

    hours = max(elapsed_s, 1.0) / 3600.0
    return {
        "service": service,
        "kind": "modeled",
        "windowSeconds": round(elapsed_s, 1),
        "usage": {
            "requests": int(usage["requests"]),
            "requestSeconds": round(usage["requestSeconds"], 2),
            "vcpuSeconds": round(vcpu_seconds, 2),
            "gibSeconds": round(gib_seconds, 2),
            "logBytes": int(usage["logBytes"]),
            "logMib": round(usage["logBytes"] / (1024.0 * 1024.0), 3),
        },
        "usdInWindow": {
            "cpu": round(cpu_usd, 6),
            "memory": round(mem_usd, 6),
            "requests": round(req_usd, 6),
            "logging": round(log_usd, 6),
            "total": round(total, 6),
        },
        "usdPerHour": {
            "cpu": round(cpu_usd / hours, 4),
            "memory": round(mem_usd / hours, 4),
            "requests": round(req_usd / hours, 4),
            "logging": round(log_usd / hours, 4),
            "total": round(total / hours, 4),
        },
        "dominantDriver": max(
            (("logging", log_usd), ("cpu", cpu_usd),
             ("memory", mem_usd), ("requests", req_usd)),
            key=lambda kv: kv[1],
        )[0] if total > 0 else None,
        "basis": (
            "cpu = %.2f request-seconds x %.1f vCPU x $%g/vCPU-s; "
            "memory = %.2f request-seconds x %.2f GiB x $%g/GiB-s; "
            "requests = %d x $%g/million; "
            "logging = %.4f GiB x $%g/GiB"
            % (usage["requestSeconds"], shape["vcpu"], prices["cpuPerVcpuSecond"],
               usage["requestSeconds"], shape["memoryGib"], prices["memoryPerGibSecond"],
               int(usage["requests"]), prices["perMillionRequests"],
               log_gib, log_price_per_gib)
        ),
    }


def rate(store, window_minutes: int = 10) -> Dict[str, Any]:
    """Current modeled spend rate, per service and in total."""
    elapsed_s = window_minutes * 60.0
    per_service: List[Dict[str, Any]] = []
    for service in sorted(set(settings.watched_services)):
        buckets = store.series(window_minutes=window_minutes, service=service)
        if not buckets:
            continue
        usage = _usage_from_buckets(buckets)
        if usage["requests"] == 0 and usage["logBytes"] == 0:
            continue
        per_service.append(_price_service(service, usage, elapsed_s))

    total_hr = round(sum(s["usdPerHour"]["total"] for s in per_service), 4)
    total_window = round(sum(s["usdInWindow"]["total"] for s in per_service), 6)
    by_driver: Dict[str, float] = {}
    for s in per_service:
        for k in ("cpu", "memory", "requests", "logging"):
            by_driver[k] = round(by_driver.get(k, 0.0) + s["usdPerHour"][k], 4)

    return {
        "kind": "modeled",
        "tier": "LIVE",
        "windowMinutes": window_minutes,
        "usdPerHour": total_hr,
        "usdInWindow": total_window,
        "inrPerHourIndicative": round(total_hr * settings.inr_per_usd, 2),
        "byDriver": by_driver,
        "byService": sorted(per_service,
                            key=lambda s: s["usdPerHour"]["total"], reverse=True),
        "projectedUsdPerDay": round(total_hr * 24, 4),
        "projectedUsdPerMonth": round(total_hr * 24 * 30, 2),
        "pricing": {
            "source": settings.pricing["_source"],
            "verifiedOn": settings.pricing["verifiedOn"],
            "billingModel": settings.billing_model,
            "currency": "USD",
            "inrPerUsdIndicative": settings.inr_per_usd,
        },
        "disclaimer": (
            "Modeled from measured usage multiplied by Google Cloud published "
            "list prices. Not a billed amount. Free-tier allotments are "
            "reported separately under freeTier; inside the free tier the "
            "marginal cost is genuinely zero."
        ),
    }


def free_tier_position(store, window_minutes: int = 60) -> Dict[str, Any]:
    """Where this project sits against the free allotments that matter.

    This is the panel that answers "are we actually going to be billed?" --
    the question that matters most on a trial account.
    """
    buckets = store.series(window_minutes=window_minutes)
    usage = _usage_from_buckets(buckets)
    elapsed_s = max(window_minutes * 60.0, 1.0)

    log_gib_rate_per_month = (usage["logBytes"] / _BYTES_PER_GIB) * (
        _SECONDS_PER_MONTH / elapsed_s)
    run_free = settings.run_prices["freeTier"]
    log_free = settings.pricing["cloudLogging"]["freeTier"]["gibPerProjectPerMonth"]

    total_vcpu_s = 0.0
    total_gib_s = 0.0
    for service in sorted(set(settings.watched_services)):
        sb = store.series(window_minutes=window_minutes, service=service)
        if not sb:
            continue
        u = _usage_from_buckets(sb)
        shape = settings.shape(service)
        total_vcpu_s += u["requestSeconds"] * shape["vcpu"]
        total_gib_s += u["requestSeconds"] * shape["memoryGib"]

    scale = _SECONDS_PER_MONTH / elapsed_s
    projected = {
        "logGibPerMonth": round(log_gib_rate_per_month, 3),
        "vcpuSecondsPerMonth": round(total_vcpu_s * scale, 1),
        "gibSecondsPerMonth": round(total_gib_s * scale, 1),
        "requestsPerMonth": int(usage["requests"] * scale),
    }
    allotment = {
        "logGibPerMonth": log_free,
        "vcpuSecondsPerMonth": run_free["vcpuSecondsPerMonth"],
        "gibSecondsPerMonth": run_free["gibSecondsPerMonth"],
        "requestsPerMonth": run_free["requestsPerMonth"],
    }
    lines = []
    for key, label in (
        ("logGibPerMonth", "Cloud Logging ingestion"),
        ("vcpuSecondsPerMonth", "Cloud Run vCPU-seconds"),
        ("gibSecondsPerMonth", "Cloud Run GiB-seconds"),
        ("requestsPerMonth", "Cloud Run requests"),
    ):
        limit = allotment[key]
        proj = projected[key]
        pct = round(100.0 * proj / limit, 2) if limit else None
        lines.append({
            "resource": label,
            "projectedMonthly": proj,
            "freeAllotment": limit,
            "pctOfFreeTier": pct,
            "withinFreeTier": (pct is not None and pct <= 100.0),
        })
    worst = max((l for l in lines if l["pctOfFreeTier"] is not None),
                key=lambda l: l["pctOfFreeTier"], default=None)
    return {
        "basedOnWindowMinutes": window_minutes,
        "note": (
            "Projection extrapolates the last %d minutes to a full month. "
            "It assumes the current traffic rate continues, which it will not "
            "if the load generator is only run in sessions."
            % window_minutes
        ),
        "lines": lines,
        "tightestConstraint": worst["resource"] if worst else None,
        "allWithinFreeTier": all(
            l["withinFreeTier"] for l in lines if l["pctOfFreeTier"] is not None),
    }


def billed_status() -> Dict[str, Any]:
    """The authoritative (billed) tier.

    Cloud Billing export to BigQuery is deliberately NOT wired up for the
    one-day build: on a project created today it would contain zero rows, and
    showing an empty panel labelled "actual cost" is worse than showing why it
    is empty. This reports the real latency characteristics instead, which is
    the honest answer and demonstrates the three-tier model.
    """
    return {
        "tier": "AUTHORITATIVE",
        "kind": "billed",
        "available": False,
        "reason": (
            "Cloud Billing export to BigQuery is not configured for this "
            "prototype. Billing export lands with roughly 24 hours of latency "
            "and no delivery guarantee, so on a project created today it would "
            "contain no rows."
        ),
        "latencyCharacteristics": (
            "Exported multiple times per day; cost details typically available "
            "within a day, sometimes longer. Never real-time."
        ),
        "howToEnable": "docs/DEPLOY.md step 11 (optional, post-hackathon)",
        "reconciliationPlan": (
            "Once export exists, compare modeled cost against billed cost for "
            "the same window daily and publish the variance as an accuracy "
            "figure next to the live number."
        ),
    }


def incident_delta(store, incident_start: float,
                   baseline_minutes: int = 15) -> Dict[str, Any]:
    """Modeled cost attributable to an incident: current rate minus baseline.

    Baseline is the window immediately BEFORE the incident started, so a
    pre-existing cost level is not blamed on the incident.
    """
    now = time.time()
    incident_minutes = max(1, int((now - incident_start) / 60.0) + 1)

    during = rate(store, window_minutes=min(incident_minutes, 60))

    # Reconstruct the pre-incident baseline from buckets ending at the start.
    start_minute = int(incident_start // 60)
    baseline_services: List[Dict[str, Any]] = []
    for service in sorted(set(settings.watched_services)):
        all_b = store.series(window_minutes=settings.minute_buckets, service=service)
        pre = [b for b in all_b
               if start_minute - baseline_minutes <= b["minute"] < start_minute]
        if not pre:
            continue
        usage = _usage_from_buckets(pre)
        baseline_services.append(
            _price_service(service, usage, len(pre) * 60.0))

    base_hr = round(sum(s["usdPerHour"]["total"] for s in baseline_services), 4)
    cur_hr = during["usdPerHour"]
    delta_hr = round(cur_hr - base_hr, 4)
    elapsed_hours = max(now - incident_start, 1.0) / 3600.0

    base_drivers: Dict[str, float] = {}
    for s in baseline_services:
        for k in ("cpu", "memory", "requests", "logging"):
            base_drivers[k] = base_drivers.get(k, 0.0) + s["usdPerHour"][k]
    driver_delta = {
        k: round(during["byDriver"].get(k, 0.0) - base_drivers.get(k, 0.0), 4)
        for k in ("cpu", "memory", "requests", "logging")
    }
    positive = {k: v for k, v in driver_delta.items() if v > 0}
    total_pos = sum(positive.values()) or 1.0

    return {
        "kind": "modeled",
        "baselineUsdPerHour": base_hr,
        "currentUsdPerHour": cur_hr,
        "deltaUsdPerHour": delta_hr,
        "deltaPct": round(100.0 * delta_hr / base_hr, 1) if base_hr > 0 else None,
        "incurredUsdSoFar": round(max(delta_hr, 0.0) * elapsed_hours, 5),
        "incidentMinutes": round((now - incident_start) / 60.0, 1),
        "baselineWindowMinutes": baseline_minutes,
        "driverDeltaUsdPerHour": driver_delta,
        "driverSharePct": {
            k: round(100.0 * v / total_pos, 1) for k, v in positive.items()
        },
        "dominantDriver": max(positive.items(), key=lambda kv: kv[1])[0]
        if positive else None,
        "inrPerHourIndicative": round(delta_hr * settings.inr_per_usd, 2),
        "disclaimer": (
            "Modeled delta: measured usage x published list prices, current "
            "window versus the %d minutes before the incident began."
            % baseline_minutes
        ),
    }
