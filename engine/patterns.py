"""Log pattern clustering and statistical anomaly detection.

Two jobs the error view does not cover:

**Patterns.** Error grouping (engine/incidents, store.error_groups) only looks
at warnings and errors. A great deal is learnable from the shape of *normal*
traffic too -- which events dominate, which routes are hot, what changed. This
clusters every log line by message template, not just the failures.

**Anomalies.** A threshold answers "is this above 5?". It cannot answer "is
this unusual *for this service*?". A service that normally serves two requests
a minute jumping to twenty is a 10x change that no fixed threshold would catch,
while a service that normally serves two thousand dipping to one thousand is
invisible to one too. Comparing against each series' own recent baseline
catches both.

Everything here is deterministic arithmetic. No model, no learned weights --
the same window always produces the same findings, and every finding carries
the evidence that produced it.
"""
import math
import re
import time
from typing import Any, Dict, List, Optional

# Masking order matters: quoted strings and measurements are replaced before
# bare numbers, or the number rule would eat the digits inside them first.
_MASKS = (
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{4,}\b", re.I), "<id>"),
    (re.compile(r"\b(?:ord|req|sess|txn|user|sku)[_-][A-Za-z0-9]+\b", re.I), "<id>"),
    # Order ids are bare 8-character uppercase hex. Unmasked they produced one
    # "pattern" per order, which is the opposite of clustering.
    (re.compile(r"\b[0-9A-F]{8}\b"), "<id>"),
    # Identifiers with digits embedded in a word, like ORD00042X, have no word
    # boundary before the digits so the bare-number rule below cannot see them.
    # Four digits minimum, deliberately: three would swallow SHA256 and HTTP2xx.
    (re.compile(r"\b[A-Za-z]+\d{4,}[A-Za-z0-9]*\b"), "<id>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hash>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|kb|mb|gb|gib|mib|%)\b", re.I), "<measure>"),
    (re.compile(r"\b[A-Z]{2,4}-[A-Z]{3,5}-\d+\b"), "<sku>"),
    # A status code is the one number that must survive: "-> 200 in" and
    # "-> 502 in" are different patterns, and collapsing them would hide
    # exactly the distinction a reader is looking for.
    (re.compile(r"(?<!-> )\b\d[\d,._]*\b"), "<n>"),
)


def template_of(message: str) -> str:
    """Collapse a message to its template by masking the parts that vary.

    "order ord_7a3f failed after 1423ms" and "order ord_91bc failed after 88ms"
    are one pattern seen twice, not two events seen once. Without this, a
    "most frequent message" list is just a list of distinct identifiers.
    """
    out = str(message or "")
    for rx, token in _MASKS:
        out = rx.sub(token, out)
    return re.sub(r"\s+", " ", out).strip()[:160]


def log_patterns(store, window_minutes: int = 30, limit: int = 20,
                 min_share: float = 0.002) -> Dict[str, Any]:
    """Cluster every log line in the window by message template.

    `min_share` drops the long tail: a template seen once in ten thousand
    records is noise, and listing it dilutes the ones that matter.
    """
    page = store.logs(limit=5000, since=time.time() - window_minutes * 60,
                      include_heartbeats=False)
    entries = page["entries"]
    total = len(entries)
    if not total:
        return {"windowMinutes": window_minutes, "total": 0, "patterns": [],
                "method": "message template clustering (deterministic)"}

    groups: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        key = template_of(e.get("message"))
        if not key:
            continue
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "pattern": key, "count": 0, "services": {}, "severities": {},
                "events": {}, "firstSeen": e["ts"], "lastSeen": e["ts"],
                "sample": e.get("message"), "errorCodes": {},
                "latencies": [], "revenueInr": 0.0,
            }
        g["count"] += 1
        g["firstSeen"] = min(g["firstSeen"], e["ts"])
        g["lastSeen"] = max(g["lastSeen"], e["ts"])
        for field, bucket in (("service", "services"), ("severity", "severities"),
                              ("event", "events"), ("errorCode", "errorCodes")):
            v = e.get(field)
            if v:
                g[bucket][v] = g[bucket].get(v, 0) + 1
        if e.get("latencyMs") is not None and len(g["latencies"]) < 300:
            g["latencies"].append(float(e["latencyMs"]))
        if e.get("event") == "checkout.failed":
            g["revenueInr"] += float(e.get("cartValueInr") or 0.0)

    out: List[Dict[str, Any]] = []
    for g in groups.values():
        share = g["count"] / float(total)
        if share < min_share and g["count"] < 3:
            continue
        lat = sorted(g["latencies"])
        worst = max(g["severities"], key=lambda s: _SEV_RANK.get(s, 0)) if g["severities"] else "INFO"
        out.append({
            "pattern": g["pattern"],
            "sample": g["sample"],
            "count": g["count"],
            "sharePct": round(100.0 * share, 2),
            "severity": worst,
            "services": sorted(g["services"], key=g["services"].get, reverse=True),
            "topEvent": max(g["events"], key=g["events"].get) if g["events"] else None,
            "errorCodes": sorted(g["errorCodes"], key=g["errorCodes"].get, reverse=True)[:3],
            "firstSeen": g["firstSeen"],
            "lastSeen": g["lastSeen"],
            "p95LatencyMs": round(lat[int(len(lat) * 0.95) - 1], 1) if len(lat) > 1 else None,
            "revenueAtRiskInr": round(g["revenueInr"], 2) if g["revenueInr"] else None,
        })

    out.sort(key=lambda p: (_SEV_RANK.get(p["severity"], 0), p["count"]), reverse=True)
    return {
        "windowMinutes": window_minutes,
        "total": total,
        "distinctPatterns": len(groups),
        "patterns": out[:limit],
        "method": "message template clustering: identifiers, hashes, quoted "
                  "strings and measurements are masked, then identical "
                  "templates are counted. Deterministic -- no model involved.",
    }


_SEV_RANK = {"DEBUG": 0, "INFO": 1, "NOTICE": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def _stats(values: List[float]) -> Dict[str, float]:
    n = len(values)
    if n == 0:
        return {"mean": 0.0, "stdev": 0.0, "n": 0}
    mean = sum(values) / n
    if n < 2:
        return {"mean": mean, "stdev": 0.0, "n": n}
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return {"mean": mean, "stdev": math.sqrt(var), "n": n}


# Each anomaly check: (key, label, bucket field, direction, unit, why it matters)
_SERIES = (
    ("requests", "Request rate", "requests", "both", "req/min",
     "A sharp change in traffic is either a real demand shift or something "
     "retrying. Both are worth knowing before the error rate moves."),
    ("errors5xx", "Server errors", "errors5xx", "up", "errors/min",
     "Server faults are the clearest signal that something is broken rather "
     "than merely busy."),
    ("errors4xx", "Client errors", "errors4xx", "up", "errors/min",
     "A jump in 4xx usually means a broken client or a stale cached URL, not "
     "a service fault -- but it still consumes capacity and log volume."),
    ("logBytes", "Log volume", "logBytes", "up", "bytes/min",
     "Log ingestion is the fastest-moving cost driver in this architecture "
     "and the one most likely to produce a surprise bill."),
    ("retries", "Retries", "retries", "up", "retries/min",
     "Retries multiply load against a dependency at exactly the moment it is "
     "least able to cope."),
    ("checkoutsFailed", "Failed checkouts", "checkoutsFailed", "up", "per min",
     "The business consequence of whatever else is happening."),
)


def anomalies(store, window_minutes: int = 60, baseline_minutes: int = 30,
              sigma: float = 2.5, min_baseline_points: int = 4) -> Dict[str, Any]:
    """Flag series whose recent values depart from their own recent baseline.

    The last few minutes are compared against the preceding `baseline_minutes`.
    A value is anomalous when it is both more than `sigma` standard deviations
    from that baseline mean AND a meaningful relative change -- the second test
    stops a dead-flat series flagging on a single unit of movement.
    """
    raw = store.series(window_minutes=window_minutes)

    # Two kinds of bucket have to go before any statistics are computed, and
    # leaving either in makes the detector useless:
    #
    #   the in-progress minute -- still filling, so it always reads low
    #   minutes with no traffic -- "no data", not "zero traffic". Including
    #       them inflated the standard deviation so far that a fivefold error
    #       spike sat inside one sigma.
    now_minute = int(time.time() // 60)
    buckets = [b for b in raw
               if b["minute"] < now_minute and (b.get("requests") or 0) > 0]

    if len(buckets) < min_baseline_points + 2:
        return {"windowMinutes": window_minutes, "anomalies": [],
                "bucketsWithTraffic": len(buckets),
                "note": "Not enough history yet. A baseline needs about %d "
                        "minutes of continuous traffic before departures from "
                        "it mean anything; there are %d so far."
                        % (min_baseline_points + 2, len(buckets))}

    recent_n = max(2, min(5, len(buckets) // 4))
    recent, baseline = buckets[-recent_n:], buckets[:-recent_n]

    # A gap between baseline and recent, so the beginning of an incident does
    # not end up in the baseline it is being compared against. Without it the
    # worst minute of a spike raises the mean it is supposed to stand out from.
    if len(baseline) > min_baseline_points:
        baseline = baseline[:-1]
    if len(baseline) > baseline_minutes:
        baseline = baseline[-baseline_minutes:]

    found: List[Dict[str, Any]] = []
    for key, label, field, direction, unit, why in _SERIES:
        base_vals = [float(b.get(field, 0) or 0) for b in baseline]
        cur_vals = [float(b.get(field, 0) or 0) for b in recent]
        if len(base_vals) < min_baseline_points:
            continue
        st = _stats(base_vals)
        cur = sum(cur_vals) / len(cur_vals)

        # A flat baseline has zero deviation, so sigma alone would flag any
        # movement at all. Fall back to a floor derived from the mean.
        spread = st["stdev"] if st["stdev"] > 0 else max(st["mean"] * 0.35, 1.0)
        z = (cur - st["mean"]) / spread if spread else 0.0
        if direction == "up" and z <= 0:
            continue
        if abs(z) < sigma:
            continue

        change = ((cur - st["mean"]) / st["mean"] * 100.0) if st["mean"] else None
        # Require a real relative move as well as a statistical one.
        if change is not None and abs(change) < 40:
            continue
        if st["mean"] < 0.5 and cur < 2:
            continue

        found.append({
            "metric": key, "label": label, "unit": unit,
            "direction": "up" if cur > st["mean"] else "down",
            "baselineMean": round(st["mean"], 2),
            "baselineStdev": round(st["stdev"], 2),
            "baselineMinutes": len(base_vals),
            "current": round(cur, 2),
            "changePct": round(change, 1) if change is not None else None,
            "zScore": round(z, 2),
            "severity": "HIGH" if abs(z) >= sigma * 2 else "MEDIUM",
            "whyItMatters": why,
            "evidence": "last %d minute(s) averaged %.2f %s against a %d-minute "
                        "baseline of %.2f (sd %.2f) -- %.1f standard deviations"
                        % (len(cur_vals), cur, unit, len(base_vals),
                           st["mean"], st["stdev"], z),
        })

    found.sort(key=lambda a: abs(a["zScore"]), reverse=True)
    return {
        "windowMinutes": window_minutes,
        "recentMinutes": recent_n,
        "sigma": sigma,
        "anomalies": found,
        "method": "each series compared against its own preceding baseline; "
                  "flagged when both the z-score exceeds %.1f and the relative "
                  "change exceeds 40%%. Deterministic." % sigma,
    }
