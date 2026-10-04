"""In-memory telemetry store: a bounded working set, not a database.

Why no database: Cloud Logging is already the durable, queryable store of
record. Duplicating the log stream into Postgres would add a continuously
billing resource, a schema to migrate and a second source of truth, and would
buy the demo nothing. What the platform needs is a fast working set for the
live view plus pre-aggregated per-minute counters for charts and rules -- both
of which are a few MB of RAM.

The trade-off, stated plainly: a platform restart loses the working set and
incident history. In GCP mode the logs themselves are never lost (Cloud
Logging holds 30 days) and the buffer refills from the Logging API on the next
poll. Persisting incidents to Firestore is a roadmap item, not a hidden gap.

Everything here is guarded by one lock: the collectors write from background
threads while HTTP handlers read.
"""
import hashlib
import math
import random
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from .config import settings
from .engine.normalize import SEVERITY_RANK, Record

_LATENCY_RESERVOIR = 300  # per bucket; reservoir-sampled for unbiased percentiles


def _minute(ts: float) -> int:
    return int(ts // 60)


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


class Bucket:
    """One minute of aggregates for one service."""

    __slots__ = (
        "minute", "service", "lines", "logBytes", "requests", "errors5xx",
        "errors4xx", "sevCounts", "latencies", "latencyCount", "checkoutsStarted",
        "checkoutsConfirmed", "checkoutsFailed", "revenueConfirmedInr",
        "revenueFailedInr", "paymentAttempts", "retries", "cpuSamples",
        "rssSamples", "inflightMax", "instances", "errorCodes",
    )

    def __init__(self, minute: int, service: str) -> None:
        self.minute = minute
        self.service = service
        self.lines = 0
        self.logBytes = 0
        self.requests = 0
        self.errors5xx = 0
        self.errors4xx = 0
        self.sevCounts: Dict[str, int] = {}
        self.latencies: List[float] = []
        self.latencyCount = 0
        self.checkoutsStarted = 0
        self.checkoutsConfirmed = 0
        self.checkoutsFailed = 0
        self.revenueConfirmedInr = 0.0
        self.revenueFailedInr = 0.0
        self.paymentAttempts = 0
        self.retries = 0
        self.cpuSamples: List[float] = []
        self.rssSamples: List[float] = []
        self.inflightMax = 0
        self.instances: set = set()
        self.errorCodes: Dict[str, int] = {}

    def observe(self, rec: Record) -> None:
        self.lines += 1
        self.logBytes += rec.sizeBytes or 0
        self.sevCounts[rec.severity] = self.sevCounts.get(rec.severity, 0) + 1
        if rec.instanceId:
            self.instances.add(rec.instanceId)

        if rec.is_heartbeat:
            if rec.cpuPct is not None:
                self.cpuSamples.append(rec.cpuPct)
            if rec.rssMb is not None:
                self.rssSamples.append(rec.rssMb)
            if rec.inflight is not None:
                self.inflightMax = max(self.inflightMax, rec.inflight)
            return

        # Only the middleware's http.request.* lines represent a served
        # request. Counting business events as requests would double-count.
        if rec.event.startswith("http.request") and rec.httpStatus is not None:
            self.requests += 1
            if rec.is_server_error:
                self.errors5xx += 1
            elif rec.is_client_error:
                self.errors4xx += 1
            if rec.latencyMs is not None:
                self.latencyCount += 1
                if len(self.latencies) < _LATENCY_RESERVOIR:
                    self.latencies.append(float(rec.latencyMs))
                else:
                    j = random.randint(0, self.latencyCount - 1)
                    if j < _LATENCY_RESERVOIR:
                        self.latencies[j] = float(rec.latencyMs)
            if rec.route == "/payments/authorize":
                self.paymentAttempts += 1

        if rec.errorCode:
            self.errorCodes[rec.errorCode] = self.errorCodes.get(rec.errorCode, 0) + 1
        if rec.retryCount:
            self.retries += int(rec.retryCount)

        if rec.event == "checkout.started":
            self.checkoutsStarted += 1
        elif rec.event == "checkout.confirmed":
            self.checkoutsConfirmed += 1
            self.revenueConfirmedInr += rec.cartValueInr or 0.0
        elif rec.event == "checkout.failed":
            self.checkoutsFailed += 1
            self.revenueFailedInr += rec.cartValueInr or 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "minute": self.minute,
            "ts": self.minute * 60,
            "service": self.service,
            "lines": self.lines,
            "logBytes": self.logBytes,
            "requests": self.requests,
            "errors5xx": self.errors5xx,
            "errors4xx": self.errors4xx,
            "errorRate": round(self.errors5xx / float(self.requests), 4) if self.requests else 0.0,
            "severity": dict(self.sevCounts),
            "errorCodes": dict(self.errorCodes),
            "p50LatencyMs": _r(percentile(self.latencies, 50)),
            "p95LatencyMs": _r(percentile(self.latencies, 95)),
            "p99LatencyMs": _r(percentile(self.latencies, 99)),
            "maxLatencyMs": _r(max(self.latencies) if self.latencies else None),
            "checkoutsStarted": self.checkoutsStarted,
            "checkoutsConfirmed": self.checkoutsConfirmed,
            "checkoutsFailed": self.checkoutsFailed,
            "revenueConfirmedInr": round(self.revenueConfirmedInr, 2),
            "revenueFailedInr": round(self.revenueFailedInr, 2),
            "paymentAttempts": self.paymentAttempts,
            "retries": self.retries,
            "cpuPctAvg": _r(sum(self.cpuSamples) / len(self.cpuSamples) if self.cpuSamples else None),
            "cpuPctMax": _r(max(self.cpuSamples) if self.cpuSamples else None),
            "rssMbAvg": _r(sum(self.rssSamples) / len(self.rssSamples) if self.rssSamples else None),
            "rssMbMax": _r(max(self.rssSamples) if self.rssSamples else None),
            "inflightMax": self.inflightMax,
            "instanceCount": len(self.instances),
        }


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), 2)


class ErrorGroup:
    """A deterministic error cluster.

    Grouping key is (service, errorCode, errorClass, route, normalized message)
    exactly as docs/prd.md section 6 specifies. No ML, no embeddings, no
    unearned claims -- identical inputs always produce identical groups.
    """

    __slots__ = ("key", "service", "errorCode", "errorClass", "route",
                 "pattern", "count", "firstSeen", "lastSeen", "samples",
                 "traces", "revenueAtRiskInr", "severity")

    def __init__(self, key: str, rec: Record) -> None:
        self.key = key
        self.service = rec.service
        self.errorCode = rec.errorCode or "UNCLASSIFIED"
        self.errorClass = rec.errorClass or ""
        self.route = rec.route or ""
        self.pattern = rec.normalized_message()
        self.severity = rec.severity
        self.count = 0
        self.firstSeen = rec.ts
        self.lastSeen = rec.ts
        self.samples: Deque[Dict[str, Any]] = deque(maxlen=5)
        self.traces: Deque[str] = deque(maxlen=10)
        self.revenueAtRiskInr = 0.0

    def observe(self, rec: Record) -> None:
        self.count += 1
        self.firstSeen = min(self.firstSeen, rec.ts)
        self.lastSeen = max(self.lastSeen, rec.ts)
        if SEVERITY_RANK.get(rec.severity, 1) > SEVERITY_RANK.get(self.severity, 1):
            self.severity = rec.severity
        self.samples.append(rec.to_dict())
        if rec.trace and rec.trace not in self.traces:
            self.traces.append(rec.trace)
        if rec.event == "checkout.failed":
            self.revenueAtRiskInr += rec.cartValueInr or 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "service": self.service,
            "errorCode": self.errorCode,
            "errorClass": self.errorClass,
            "route": self.route,
            "pattern": self.pattern,
            "severity": self.severity,
            "count": self.count,
            "firstSeen": self.firstSeen,
            "lastSeen": self.lastSeen,
            "ageS": round(time.time() - self.firstSeen, 1),
            "revenueAtRiskInr": round(self.revenueAtRiskInr, 2),
            "sampleTraces": list(self.traces)[:5],
            "samples": list(self.samples),
        }


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._logs: Deque[Record] = deque(maxlen=settings.log_buffer_size)
        self._buckets: "OrderedDict[Tuple[int, str], Bucket]" = OrderedDict()
        self._groups: Dict[str, ErrorGroup] = {}
        self._heartbeats: Dict[str, Record] = {}
        self._seen_ids: Deque[str] = deque(maxlen=20000)
        self._seen_set: set = set()
        self.ingested_total = 0
        self.dropped_duplicates = 0
        self.last_ingest_ts: float = 0.0
        self.last_error: Optional[str] = None

    # --- ingestion ---------------------------------------------------------
    def add_many(self, records: Iterable[Record]) -> int:
        added = 0
        with self._lock:
            for rec in records:
                if not rec or not rec.service:
                    continue
                dedupe = self._dedupe_key(rec)
                if dedupe in self._seen_set:
                    self.dropped_duplicates += 1
                    continue
                self._seen_set.add(dedupe)
                self._seen_ids.append(dedupe)
                if len(self._seen_ids) == self._seen_ids.maxlen:
                    # deque evicted the oldest; keep the set in step
                    pass
                while len(self._seen_set) > 20000:
                    try:
                        self._seen_set.discard(self._seen_ids.popleft())
                    except IndexError:
                        break

                if rec.ts <= 0:
                    rec.ts = time.time()
                self._logs.append(rec)
                self._bucket(rec).observe(rec)
                if rec.is_heartbeat:
                    self._heartbeats[rec.service] = rec
                self._maybe_group(rec)
                added += 1
                self.ingested_total += 1
            self.last_ingest_ts = time.time()
            self._evict_buckets()
        return added

    @staticmethod
    def _dedupe_key(rec: Record) -> str:
        """Identify a log entry uniquely enough to survive overlapping polls.

        In gcp mode the collector deliberately rewinds its cursor a few seconds
        on every poll (losing an entry is worse than seeing it twice), so the
        same entry really does arrive more than once and must be dropped.

        Cloud Logging stamps every entry with a unique `insert_id`, so use that
        when we have it. Otherwise fall back to a hash of the entry's content:
        keying on service+event+requestId+timestamp alone was too coarse --
        two distinct failed checkouts in the same second with no request id
        collapsed into one, silently undercounting errors and revenue at risk.
        """
        if rec.insertId:
            return "id:" + rec.insertId
        payload = "|".join((
            rec.service, rec.event, rec.requestId or "", rec.message or "",
            str(rec.httpStatus or ""), str(rec.latencyMs or ""),
            str(rec.orderId or ""), str(rec.cartValueInr or ""),
            "%.3f" % rec.ts,
        ))
        return "h:" + hashlib.blake2b(payload.encode("utf-8"),
                                      digest_size=16).hexdigest()

    def _bucket(self, rec: Record) -> Bucket:
        key = (_minute(rec.ts), rec.service)
        b = self._buckets.get(key)
        if b is None:
            b = Bucket(key[0], key[1])
            self._buckets[key] = b
        return b

    def _evict_buckets(self) -> None:
        horizon = _minute(time.time()) - settings.minute_buckets
        stale = [k for k in self._buckets if k[0] < horizon]
        for k in stale:
            self._buckets.pop(k, None)
        # Error groups older than the bucket horizon are no longer actionable.
        cutoff = time.time() - settings.minute_buckets * 60
        for k in [k for k, g in self._groups.items() if g.lastSeen < cutoff]:
            self._groups.pop(k, None)

    def _maybe_group(self, rec: Record) -> None:
        if SEVERITY_RANK.get(rec.severity, 1) < SEVERITY_RANK["WARNING"]:
            return
        if not (rec.errorCode or rec.is_server_error or rec.is_client_error
                or rec.event.endswith(".failed")):
            return
        key = "%s|%s|%s|%s|%s" % (
            rec.service, rec.errorCode or "UNCLASSIFIED", rec.errorClass or "",
            rec.route or "", rec.normalized_message(),
        )
        g = self._groups.get(key)
        if g is None:
            g = ErrorGroup(key, rec)
            self._groups[key] = g
        g.observe(rec)

    # --- queries -----------------------------------------------------------
    def logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        min_severity: Optional[str] = None,
        services: Optional[List[str]] = None,
        q: Optional[str] = None,
        event: Optional[str] = None,
        error_code: Optional[str] = None,
        trace: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        include_heartbeats: bool = False,
    ) -> Dict[str, Any]:
        rank = SEVERITY_RANK.get((min_severity or "").upper())
        needle = (q or "").lower().strip()
        svc = set(services or [])
        with self._lock:
            pool = list(self._logs)
        out: List[Record] = []
        for rec in reversed(pool):  # newest first
            if not include_heartbeats and rec.is_heartbeat:
                continue
            if rank is not None and SEVERITY_RANK.get(rec.severity, 1) < rank:
                continue
            if svc and rec.service not in svc:
                continue
            if event and rec.event != event:
                continue
            if error_code and rec.errorCode != error_code:
                continue
            if trace and (rec.trace or "").endswith(trace):
                pass
            elif trace:
                continue
            if since is not None and rec.ts < since:
                continue
            if until is not None and rec.ts > until:
                continue
            if needle:
                blob = "%s %s %s %s %s" % (
                    rec.message, rec.event, rec.service,
                    rec.errorCode or "", rec.route or "")
                if needle not in blob.lower():
                    continue
            out.append(rec)
        total = len(out)
        page = out[offset:offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "entries": [r.to_dict() for r in page],
        }

    def trace(self, trace_suffix: str) -> List[Dict[str, Any]]:
        """All entries sharing a trace, oldest first: the causal chain."""
        with self._lock:
            pool = [r for r in self._logs
                    if r.trace and r.trace.endswith(trace_suffix)]
        pool.sort(key=lambda r: r.ts)
        return [r.to_dict() for r in pool]

    def series(self, window_minutes: int = 60,
               service: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-minute aggregates, oldest first. service=None aggregates all."""
        now_m = _minute(time.time())
        start = now_m - window_minutes + 1
        with self._lock:
            buckets = [b for (m, s), b in self._buckets.items()
                       if m >= start and (service is None or s == service)]
        if service is not None:
            return [b.to_dict() for b in sorted(buckets, key=lambda b: b.minute)]

        merged: Dict[int, Bucket] = {}
        for b in buckets:
            agg = merged.get(b.minute)
            if agg is None:
                agg = Bucket(b.minute, "__all__")
                merged[b.minute] = agg
            agg.lines += b.lines
            agg.logBytes += b.logBytes
            agg.requests += b.requests
            agg.errors5xx += b.errors5xx
            agg.errors4xx += b.errors4xx
            agg.latencies.extend(b.latencies)
            agg.latencyCount += b.latencyCount
            agg.checkoutsStarted += b.checkoutsStarted
            agg.checkoutsConfirmed += b.checkoutsConfirmed
            agg.checkoutsFailed += b.checkoutsFailed
            agg.revenueConfirmedInr += b.revenueConfirmedInr
            agg.revenueFailedInr += b.revenueFailedInr
            agg.paymentAttempts += b.paymentAttempts
            agg.retries += b.retries
            agg.cpuSamples.extend(b.cpuSamples)
            agg.rssSamples.extend(b.rssSamples)
            agg.inflightMax = max(agg.inflightMax, b.inflightMax)
            agg.instances |= b.instances
            for k, v in b.sevCounts.items():
                agg.sevCounts[k] = agg.sevCounts.get(k, 0) + v
            for k, v in b.errorCodes.items():
                agg.errorCodes[k] = agg.errorCodes.get(k, 0) + v
        return [merged[m].to_dict() for m in sorted(merged)]

    def service_summary(self, window_minutes: int = 15) -> List[Dict[str, Any]]:
        now_m = _minute(time.time())
        start = now_m - window_minutes + 1
        with self._lock:
            by_service: Dict[str, List[Bucket]] = {}
            for (m, s), b in self._buckets.items():
                if m >= start:
                    by_service.setdefault(s, []).append(b)
            heartbeats = dict(self._heartbeats)

        out = []
        for service, buckets in by_service.items():
            requests = sum(b.requests for b in buckets)
            e5 = sum(b.errors5xx for b in buckets)
            e4 = sum(b.errors4xx for b in buckets)
            lat: List[float] = []
            for b in buckets:
                lat.extend(b.latencies)
            cpu = [v for b in buckets for v in b.cpuSamples]
            rss = [v for b in buckets for v in b.rssSamples]
            hb = heartbeats.get(service)
            shape = settings.shape(service)
            rss_max = max(rss) if rss else None
            out.append({
                "service": service,
                "windowMinutes": window_minutes,
                "requests": requests,
                "errors5xx": e5,
                "errors4xx": e4,
                "errorRate": round(e5 / float(requests), 4) if requests else 0.0,
                # Explicit alias: rules reference metrics by name, and
                # "errorRate" alone is ambiguous about which class of error it
                # counts. Both keys are emitted so a rule cannot silently look
                # up a missing metric and evaluate to None.
                "errorRate5xx": round(e5 / float(requests), 4) if requests else 0.0,
                "errorRate4xx": round(e4 / float(requests), 4) if requests else 0.0,
                "errors5xxPerMin": round(e5 / float(window_minutes), 2),
                "requestsPerMin": round(requests / float(window_minutes), 2),
                "p50LatencyMs": _r(percentile(lat, 50)),
                "p95LatencyMs": _r(percentile(lat, 95)),
                "p99LatencyMs": _r(percentile(lat, 99)),
                "logBytes": sum(b.logBytes for b in buckets),
                "logLines": sum(b.lines for b in buckets),
                "cpuPctAvg": _r(sum(cpu) / len(cpu) if cpu else None),
                "cpuPctMax": _r(max(cpu) if cpu else None),
                "rssMbMax": _r(rss_max),
                "memoryGibProvisioned": shape["memoryGib"],
                "memoryUtilisationPct": _r(
                    100.0 * (rss_max / 1024.0) / shape["memoryGib"]
                ) if rss_max else None,
                "vcpuProvisioned": shape["vcpu"],
                "instanceCount": len({i for b in buckets for i in b.instances}),
                "lastHeartbeatAgeS": round(time.time() - hb.ts, 1) if hb else None,
                "inflight": hb.inflight if hb else None,
                "healthy": (e5 / float(requests) if requests else 0.0) < 0.02,
            })
        return sorted(out, key=lambda d: d["service"])

    def error_groups(self, window_minutes: int = 60,
                     limit: int = 25) -> List[Dict[str, Any]]:
        cutoff = time.time() - window_minutes * 60
        with self._lock:
            groups = [g for g in self._groups.values() if g.lastSeen >= cutoff]
        groups.sort(key=lambda g: (g.count, g.lastSeen), reverse=True)
        return [g.to_dict() for g in groups[:limit]]

    def route_stats(self, window_minutes: int = 60, slow_ms: int = 500,
                    limit: int = 10) -> Dict[str, Any]:
        """Per-route latency and failures, from the raw log buffer.

        Minute buckets keep only overall percentiles, so "which endpoint is
        slow" has to come from individual requests. The buffer is bounded, so
        at high volume this describes the most recent requests in the window,
        and the response says how many it looked at.
        """
        cutoff = time.time() - window_minutes * 60
        with self._lock:
            pool = [r for r in self._logs
                    if r.ts >= cutoff and r.event.startswith("http.request")
                    and r.httpStatus is not None]
        by_route: Dict[str, Dict[str, Any]] = {}
        slow = 0
        for r in pool:
            key = r.route or "(unknown)"
            row = by_route.setdefault(key, {"route": key, "service": r.service,
                                            "count": 0, "errors5xx": 0, "lat": []})
            row["count"] += 1
            if r.is_server_error:
                row["errors5xx"] += 1
            if r.latencyMs is not None:
                row["lat"].append(float(r.latencyMs))
                if r.latencyMs > slow_ms:
                    slow += 1
        rows = []
        for row in by_route.values():
            lat = row.pop("lat")
            row["p95LatencyMs"] = _r(percentile(lat, 95)) if lat else None
            row["errorRate"] = round(row["errors5xx"] / float(row["count"]), 4)
            rows.append(row)
        rows.sort(key=lambda x: x["p95LatencyMs"] or 0, reverse=True)
        return {"routes": rows[:limit], "routeCount": len(rows),
                "slowRequests": slow, "slowThresholdMs": slow_ms,
                "sampled": len(pool)}

    def heartbeats(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._heartbeats.values()]

    def reset(self, reason: str = "") -> Dict[str, Any]:
        """Empty the working set.

        Called when the active project changes. The buffered entries belong to
        the project being left, and rendering them under a different project's
        name would be a lie that is very hard to spot. Cloud Logging holds the
        real history, so the buffer refills within a poll or two.
        """
        with self._lock:
            dropped = len(self._logs)
            self._logs.clear()
            self._buckets.clear()
            self._groups.clear()
            self._heartbeats.clear()
            self._seen_ids.clear()
            self._seen_set.clear()
            self.ingested_total = 0
            self.dropped_duplicates = 0
            self.last_ingest_ts = 0.0
            self.last_error = None
        return {"cleared": dropped, "reason": reason}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "bufferedEntries": len(self._logs),
                "bufferCapacity": self._logs.maxlen,
                "minuteBuckets": len(self._buckets),
                "errorGroups": len(self._groups),
                "ingestedTotal": self.ingested_total,
                "droppedDuplicates": self.dropped_duplicates,
                "lastIngestAgeS": round(time.time() - self.last_ingest_ts, 1)
                if self.last_ingest_ts else None,
                "lastError": self.last_error,
            }


store = Store()
