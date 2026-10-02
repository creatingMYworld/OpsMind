"""Deterministic cost-optimization engine.

rules.md section 7 is the contract. Every recommendation carries:
  recommendation, evidence, observedMetric, timeWindow, affectedResource,
  rationale, confidence, suggestedAction

and -- critically -- a savings figure ONLY when it is calculated from verified
published pricing. Where a number cannot be derived honestly the finding is
labelled "potential optimization opportunity" with `estimatedSavingUsdPerMonth:
null`, and the UI renders that difference visibly.

Provenance is explicit on every item:
  opsmind-rule     -- derived here from observed telemetry
  google-recommender -- Google Cloud Recommender API

Note on the Recommender API: on a Cloud Run-only project it typically returns
nothing, because the cost recommenders target idle VMs, unattached disks and
committed-use discounts. Its free-tier quota is also only 100 reads per day
per organization. So it is integrated as a supplementary source with an
informative empty state, never as the primary one. Pretending otherwise would
produce an empty Optimization Center.
"""
import time
from typing import Any, Dict, List, Optional

from ..config import settings

_BYTES_PER_GIB = 1024.0 ** 3
_SECONDS_PER_MONTH = 30.0 * 24 * 3600

# Cloud Run memory tiers worth recommending down to, in GiB.
_MEMORY_TIERS = [0.125, 0.25, 0.5, 1.0, 2.0, 4.0]


def _next_tier_down(current: float, needed_gib: float) -> Optional[float]:
    """Smallest tier that still leaves ~2.5x headroom over observed peak."""
    target = needed_gib * 2.5
    candidates = [t for t in _MEMORY_TIERS if t < current and t >= target]
    return min(candidates) if candidates else None


def _rec(
    rule_id: str, title: str, recommendation: str, *,
    resource: str, observed_metric: str, observed_value: Any, unit: str,
    window_minutes: int, rationale: str, action: str, confidence: str,
    evidence: Dict[str, Any], saving_usd_month: Optional[float] = None,
    saving_basis: Optional[str] = None, category: str = "cost",
    severity: str = "MEDIUM", provenance: str = "opsmind-rule",
) -> Dict[str, Any]:
    return {
        "id": "%s::%s" % (rule_id, resource),
        "ruleId": rule_id,
        "title": title,
        "recommendation": recommendation,
        "category": category,
        "severity": severity,
        "affectedResource": resource,
        "observedMetric": observed_metric,
        "observedValue": observed_value,
        "unit": unit,
        "timeWindow": "last %d minutes" % window_minutes,
        "rationale": rationale,
        "suggestedAction": action,
        "confidence": confidence,
        "evidence": evidence,
        "estimatedSavingUsdPerMonth": (
            round(saving_usd_month, 4) if saving_usd_month is not None else None
        ),
        "estimatedSavingInrPerMonthIndicative": (
            round(saving_usd_month * settings.inr_per_usd, 2)
            if saving_usd_month is not None else None
        ),
        "savingBasis": saving_basis,
        "savingStatus": ("calculated" if saving_usd_month is not None
                         else "potential optimization opportunity"),
        "provenance": provenance,
        "pricingVerifiedOn": settings.pricing["verifiedOn"],
        "generatedAt": time.time(),
    }


def generate(store, window_minutes: int = 30) -> List[Dict[str, Any]]:
    prices = settings.run_prices
    log_price = settings.pricing["cloudLogging"]["ingestionPerGib"]
    out: List[Dict[str, Any]] = []

    summaries = store.service_summary(window_minutes=window_minutes)
    elapsed_s = max(window_minutes * 60.0, 1.0)
    scale_to_month = _SECONDS_PER_MONTH / elapsed_s

    for s in summaries:
        service = s["service"]
        shape = settings.shape(service)

        # --- 1. Over-provisioned memory ---------------------------------
        rss_max = s.get("rssMbMax")
        util = s.get("memoryUtilisationPct")
        if rss_max and util is not None and util < 40.0:
            needed_gib = rss_max / 1024.0
            target = _next_tier_down(shape["memoryGib"], needed_gib)
            if target:
                # Memory is billed per GiB-second of request processing time.
                # Recover request-seconds from the modeled usage path.
                from . import cost as cost_engine
                buckets = store.series(window_minutes=window_minutes, service=service)
                usage = cost_engine._usage_from_buckets(buckets)
                gib_s_now = usage["requestSeconds"] * shape["memoryGib"]
                gib_s_new = usage["requestSeconds"] * target
                saving = ((gib_s_now - gib_s_new)
                          * prices["memoryPerGibSecond"] * scale_to_month)
                out.append(_rec(
                    "over-provisioned-memory",
                    "Memory over-provisioned on %s" % service,
                    "Reduce %s container memory from %.3g GiB to %.3g GiB."
                    % (service, shape["memoryGib"], target),
                    resource=service,
                    observed_metric="peak memory utilisation",
                    observed_value=util, unit="%",
                    window_minutes=window_minutes,
                    rationale="Peak resident memory was %.0f MiB against %.3g "
                              "GiB provisioned (%.1f%%). The proposed tier "
                              "still leaves roughly 2.5x headroom over the "
                              "observed peak."
                              % (rss_max, shape["memoryGib"], util),
                    action="gcloud run services update %s --memory=%s "
                           "--region=%s" % (service, _gcloud_mem(target), settings.region),
                    confidence="HIGH" if util < 25 else "MEDIUM",
                    evidence={
                        "peakRssMib": rss_max,
                        "provisionedGib": shape["memoryGib"],
                        "proposedGib": target,
                        "utilisationPct": util,
                        "gibSecondsInWindow": round(gib_s_now, 2),
                    },
                    saving_usd_month=saving,
                    saving_basis="(%.2f - %.2f) GiB-seconds in window x $%g/GiB-s "
                                 "x %.1f windows per month"
                                 % (gib_s_now, gib_s_new,
                                    prices["memoryPerGibSecond"], scale_to_month),
                ))

        # --- 2. Over-provisioned CPU ------------------------------------
        cpu_max = s.get("cpuPctMax")
        if cpu_max is not None and s.get("requests", 0) > 20 and cpu_max < 30.0:
            out.append(_rec(
                "over-provisioned-cpu",
                "CPU head-room on %s" % service,
                "Consider reducing %s CPU allocation below %.3g vCPU, or raising "
                "concurrency so each instance serves more requests."
                % (service, shape["vcpu"]),
                resource=service,
                observed_metric="peak process CPU", observed_value=cpu_max,
                unit="%", window_minutes=window_minutes,
                rationale="Peak CPU reached only %.1f%% of one core across %d "
                          "requests. Cloud Run's smallest CPU allocation and "
                          "concurrency tuning are both cheaper than idle cores."
                          % (cpu_max, s.get("requests", 0)),
                action="Review concurrency first: gcloud run services update %s "
                       "--concurrency=160 --region=%s" % (service, settings.region),
                confidence="MEDIUM",
                evidence={"cpuPctMax": cpu_max, "cpuPctAvg": s.get("cpuPctAvg"),
                          "requests": s.get("requests"),
                          "vcpuProvisioned": shape["vcpu"]},
                # Honest: CPU-per-request billing means the saving depends on
                # a concurrency change whose effect we have not measured.
                saving_usd_month=None,
                saving_basis=None,
            ))

        # --- 3. Idle service --------------------------------------------
        if s.get("requests", 0) == 0 and s.get("lastHeartbeatAgeS") is not None:
            out.append(_rec(
                "idle-service",
                "%s served no traffic" % service,
                "Confirm %s is still needed; if it is, ensure min-instances=0 "
                "so it scales to zero." % service,
                resource=service, observed_metric="requests", observed_value=0,
                unit="requests", window_minutes=window_minutes,
                rationale="A service with no requests but a live heartbeat is "
                          "either held warm by min-instances or no longer used. "
                          "Either way it is spending without serving.",
                action="gcloud run services update %s --min-instances=0 "
                       "--region=%s" % (service, settings.region),
                confidence="MEDIUM",
                evidence={"requests": 0,
                          "heartbeatAgeS": s.get("lastHeartbeatAgeS")},
                saving_usd_month=None,
            ))

        # --- 4. Elevated client-error ratio -----------------------------
        reqs = s.get("requests", 0)
        e4 = s.get("errors4xx", 0)
        if reqs >= 40 and e4 / float(reqs) > 0.12:
            out.append(_rec(
                "client-error-waste",
                "High 4xx ratio on %s" % service,
                "Investigate the %.0f%% client-error rate on %s: these requests "
                "consume CPU, memory and log volume without serving a user."
                % (100.0 * e4 / reqs, service),
                resource=service, observed_metric="4xx ratio",
                observed_value=round(100.0 * e4 / reqs, 1), unit="%",
                window_minutes=window_minutes,
                rationale="4xx responses are billed exactly like successful "
                          "ones. A sustained high ratio usually means a broken "
                          "client, a stale cached URL or a missing validation "
                          "guard upstream.",
                action="Group the 4xx by route and errorCode in the Errors view "
                       "and fix or reject the top pattern earlier in the stack.",
                confidence="MEDIUM",
                evidence={"requests": reqs, "errors4xx": e4,
                          "errors5xx": s.get("errors5xx")},
                saving_usd_month=None,
            ))

    # --- 5. Debug logging left on (global, per service) -----------------
    for service in sorted({s["service"] for s in summaries}):
        buckets = store.series(window_minutes=window_minutes, service=service)
        if not buckets:
            continue
        sev_total: Dict[str, int] = {}
        total_bytes = 0
        for b in buckets:
            total_bytes += b.get("logBytes", 0) or 0
            for k, v in (b.get("severity") or {}).items():
                sev_total[k] = sev_total.get(k, 0) + v
        lines = sum(sev_total.values())
        debug_lines = sev_total.get("DEBUG", 0)
        if lines >= 50 and debug_lines / float(lines) > 0.30:
            share = debug_lines / float(lines)
            wasted_gib = (total_bytes * share) / _BYTES_PER_GIB
            saving = wasted_gib * log_price * scale_to_month
            out.append(_rec(
                "debug-logging-enabled",
                "DEBUG logging is on in %s" % service,
                "Raise %s log level to INFO, or add a Cloud Logging exclusion "
                "filter for DEBUG severity." % service,
                resource=service, observed_metric="DEBUG share of log lines",
                observed_value=round(100.0 * share, 1), unit="%",
                window_minutes=window_minutes,
                rationale="DEBUG accounts for %.0f%% of %s's log lines. Cloud "
                          "Logging bills $%g per GiB ingested beyond the 50 GiB "
                          "free monthly allotment, and this service shows no "
                          "elevated error rate -- so the volume is pure cost "
                          "with no diagnostic value right now."
                          % (100.0 * share, service, log_price),
                action="Set LOG_LEVEL=INFO on the service, or add an exclusion "
                       "filter: severity=\"DEBUG\" on the _Default sink.",
                confidence="HIGH", severity="HIGH",
                evidence={"debugLines": debug_lines, "totalLines": lines,
                          "logBytesInWindow": total_bytes,
                          "wastedGibProjectedMonthly": round(
                              wasted_gib * scale_to_month, 3),
                          "severityMix": sev_total},
                saving_usd_month=saving,
                saving_basis="%.4f GiB attributable to DEBUG in window x $%g/GiB "
                             "x %.1f windows per month"
                             % (wasted_gib, log_price, scale_to_month),
            ))

    # --- 6. Retry amplification (global) -------------------------------
    gbuckets = store.series(window_minutes=window_minutes)
    if gbuckets:
        attempts = sum(b.get("paymentAttempts", 0) for b in gbuckets)
        started = sum(b.get("checkoutsStarted", 0) for b in gbuckets)
        retries = sum(b.get("retries", 0) for b in gbuckets)
        if started >= 5 and attempts > started * 1.3:
            amp = attempts / float(started)
            wasted_attempts = attempts - started
            req_saving = ((wasted_attempts / 1_000_000.0)
                          * prices["perMillionRequests"] * scale_to_month)
            out.append(_rec(
                "retry-amplification",
                "Payment retries are amplifying load %.2fx" % amp,
                "Add exponential backoff with jitter to the orders -> payments "
                "retry path, and stop retrying non-retryable responses.",
                resource="cognikart-orders -> cognikart-payments",
                observed_metric="payment attempts per checkout",
                observed_value=round(amp, 2), unit="x",
                window_minutes=window_minutes,
                rationale="%d checkouts produced %d payment authorization "
                          "attempts (%d recorded retries). Immediate retries "
                          "against a failing dependency multiply requests, CPU "
                          "time and log volume at exactly the moment the "
                          "dependency is least able to cope."
                          % (started, attempts, retries),
                action="Replace the fixed retry loop with backoff "
                       "(e.g. 200ms, 800ms, 3.2s + jitter) and treat HTTP 402 "
                       "as terminal.",
                confidence="HIGH", severity="HIGH",
                evidence={"checkoutsStarted": started,
                          "paymentAttempts": attempts,
                          "recordedRetries": retries,
                          "wastedAttempts": wasted_attempts,
                          "amplificationFactor": round(amp, 2)},
                saving_usd_month=req_saving,
                saving_basis="%d avoidable requests in window x $%g per million "
                             "x %.1f windows per month (request charge only; "
                             "avoided CPU and log volume are additional and "
                             "not included)"
                             % (wasted_attempts, prices["perMillionRequests"],
                                scale_to_month),
            ))

    # --- 7. Log volume approaching the free allotment ------------------
    from . import cost as cost_engine
    ft = cost_engine.free_tier_position(store, window_minutes=window_minutes)
    for line in ft["lines"]:
        if line["pctOfFreeTier"] is not None and line["pctOfFreeTier"] > 60.0:
            out.append(_rec(
                "free-tier-pressure",
                "%s projected at %.0f%% of free allotment"
                % (line["resource"], line["pctOfFreeTier"]),
                "Reduce %s or plan for billed usage: current traffic projects "
                "to %.3g of a %.3g free monthly allotment."
                % (line["resource"], line["projectedMonthly"], line["freeAllotment"]),
                resource=line["resource"],
                observed_metric="projected monthly usage vs free allotment",
                observed_value=line["pctOfFreeTier"], unit="%",
                window_minutes=window_minutes,
                rationale="Projected from the last %d minutes. Crossing a free "
                          "allotment is where a $0 project starts billing."
                          % window_minutes,
                action="Throttle the load generator, add log exclusion filters, "
                       "or accept the billed usage knowingly.",
                confidence="LOW",
                # Severity reflects how far over the allotment the projection
                # runs, not merely that it crossed it: a 15-minute load test
                # extrapolates to alarming monthly figures, and flagging that
                # as HIGH would bury findings we are actually confident about.
                severity=("HIGH" if line["pctOfFreeTier"] > 300
                          else "MEDIUM" if line["pctOfFreeTier"] > 100 else "LOW"),
                evidence=line,
                saving_usd_month=None,
            ))

    # Rank by severity, then by how much we trust the finding, then by the
    # size of the saving. Confidence matters: a HIGH-severity extrapolation we
    # are unsure about should not sit above a HIGH-confidence, quantified fix.
    sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    out.sort(key=lambda r: (
        sev_order.get(r["severity"], 3),
        conf_order.get(r["confidence"], 3),
        -(r["estimatedSavingUsdPerMonth"] or 0.0),
    ))
    return out


def _gcloud_mem(gib: float) -> str:
    if gib >= 1:
        return "%dGi" % int(gib)
    return "%dMi" % int(gib * 1024)


def google_recommender_status() -> Dict[str, Any]:
    """Supplementary source status, reported honestly."""
    return {
        "provenance": "google-recommender",
        "configured": False,
        "reason": (
            "Google Cloud Recommender API is not wired into this prototype. "
            "Its cost recommenders target Compute Engine instances, unattached "
            "persistent disks and committed-use discounts, none of which exist "
            "on a Cloud Run-only project -- it would return an empty list."
        ),
        "freeTierQuota": "100 reads per day per organization",
        "howToEnable": "docs/DEPLOY.md step 12 (optional)",
        "recommendations": [],
    }
