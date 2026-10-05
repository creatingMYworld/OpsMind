"""Cloud Monitoring collector -- the NEAR-REAL-TIME (platform truth) tier.

This is the second of the platform's three latency tiers:

  LIVE (1-5s)          structured logs + service heartbeats
  NEAR REAL TIME       <- this module: Cloud Run system metrics
  AUTHORITATIVE        billed cost from Cloud Billing export

Why both this and heartbeats? They measure different things and they are both
honest about it. Heartbeats are the process's own view, available in seconds.
These are the PLATFORM's view -- the same numbers Google bills from, including
instance count and billable time, which a process cannot see about itself.
They lag by minutes: Cloud Run samples roughly every 60 seconds and then takes
a few more minutes to become readable through the API. The dashboard labels
each panel with its tier so that lag reads as a documented property rather
than a bug.

Cost: Google Cloud system metrics are NON-CHARGEABLE to read. This collector
adds nothing to the bill. Polling faster than 60s would add API calls for no
new data, which is why the interval is pinned at 60s.
"""
import threading
import time
from typing import Any, Dict, List, Optional

from ..config import settings

# metric type -> (how to align, how to reduce across series, unit, label)
_METRICS = [
    {
        "key": "cpuUtilisation",
        "type": "run.googleapis.com/container/cpu/utilizations",
        "aligner": "ALIGN_PERCENTILE_95",
        "reducer": "REDUCE_MAX",
        "unit": "ratio",
        "label": "Container CPU utilisation (p95)",
    },
    {
        "key": "memoryUtilisation",
        "type": "run.googleapis.com/container/memory/utilizations",
        "aligner": "ALIGN_PERCENTILE_95",
        "reducer": "REDUCE_MAX",
        "unit": "ratio",
        "label": "Container memory utilisation (p95)",
    },
    {
        "key": "instanceCount",
        "type": "run.googleapis.com/container/instance_count",
        "aligner": "ALIGN_MEAN",
        "reducer": "REDUCE_SUM",
        "unit": "instances",
        "label": "Active instance count",
    },
    {
        "key": "requestCount",
        "type": "run.googleapis.com/request_count",
        "aligner": "ALIGN_RATE",
        "reducer": "REDUCE_SUM",
        "unit": "req/s",
        "label": "Request rate",
    },
    {
        "key": "requestLatencyP95",
        "type": "run.googleapis.com/request_latencies",
        "aligner": "ALIGN_PERCENTILE_95",
        "reducer": "REDUCE_MAX",
        "unit": "ms",
        "label": "Request latency (p95)",
    },
    {
        "key": "billableInstanceTime",
        "type": "run.googleapis.com/container/billable_instance_time",
        "aligner": "ALIGN_RATE",
        "reducer": "REDUCE_SUM",
        "unit": "s/s",
        "label": "Billable instance time",
    },
]



def _service_filter(metric_type: str, services: List[str]) -> str:
    """A Cloud Monitoring filter for one metric across several services.

    Monitoring and Logging do NOT share a filter language, which is easy to
    miss because they sit next to each other in the console. Logging accepts
    `field=("a" OR "b")`; Monitoring rejects it with

        400 The right-hand side of a comparison contains an unsupported value

    and the whole NEAR_REAL_TIME tier goes quiet while everything else keeps
    working -- so the dashboard looks fine and simply has no CPU or memory.
    Spelling the comparisons out individually is unambiguous and avoids
    depending on `one_of()` being supported.
    """
    names = [s for s in dict.fromkeys(services) if s]
    if not names:
        return 'metric.type="%s" AND resource.type="cloud_run_revision"' % metric_type
    clause = " OR ".join('resource.labels.service_name="%s"' % n for n in names)
    return ('metric.type="%s" AND resource.type="cloud_run_revision" AND (%s)'
            % (metric_type, clause))


class GcpMetricCollector:
    def __init__(self) -> None:
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snapshot: Dict[str, Any] = {}
        self.last_poll_ts: float = 0.0
        self.last_error: Optional[str] = None
        self.polls: int = 0
        self.errors: Dict[str, str] = {}

    def _ensure_client(self) -> Any:
        if self._client is None:
            from google.cloud import monitoring_v3
            if not (settings.active_project or settings.project_id):
                raise RuntimeError(
                    "GOOGLE_CLOUD_PROJECT is not set; cannot query Cloud Monitoring")
            self._client = monitoring_v3.MetricServiceClient()
        return self._client

    def _query(self, spec: Dict[str, Any], window_minutes: int) -> List[Dict[str, Any]]:
        from google.cloud import monitoring_v3
        from google.protobuf import duration_pb2

        client = self._ensure_client()
        now = time.time()
        interval = monitoring_v3.TimeInterval({
            "end_time": {"seconds": int(now)},
            "start_time": {"seconds": int(now - window_minutes * 60)},
        })
        aggregation = monitoring_v3.Aggregation({
            "alignment_period": duration_pb2.Duration(seconds=60),
            "per_series_aligner": getattr(
                monitoring_v3.Aggregation.Aligner, spec["aligner"]),
            "cross_series_reducer": getattr(
                monitoring_v3.Aggregation.Reducer, spec["reducer"]),
            "group_by_fields": ["resource.labels.service_name"],
        })
        request = monitoring_v3.ListTimeSeriesRequest(
            name="projects/%s" % (settings.active_project or settings.project_id),
            filter=_service_filter(spec["type"], settings.watched_services),
            interval=interval,
            aggregation=aggregation,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        out: List[Dict[str, Any]] = []
        for series in client.list_time_series(request=request):
            service = dict(series.resource.labels or {}).get("service_name", "unknown")
            points = []
            for p in series.points:
                value = (p.value.double_value
                         if p.value.double_value else p.value.int64_value)
                points.append({
                    "ts": p.interval.end_time.timestamp(),
                    "value": float(value),
                })
            points.sort(key=lambda d: d["ts"])
            if not points:
                continue
            out.append({
                "service": service,
                "points": points,
                "latest": points[-1]["value"],
                "latestTs": points[-1]["ts"],
                "max": max(p["value"] for p in points),
            })
        return out

    def poll_once(self, window_minutes: int = 20) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        for spec in _METRICS:
            try:
                snapshot[spec["key"]] = {
                    "metricType": spec["type"],
                    "label": spec["label"],
                    "unit": spec["unit"],
                    "aligner": spec["aligner"],
                    "reducer": spec["reducer"],
                    "series": self._query(spec, window_minutes),
                }
            except Exception as exc:  # noqa: BLE001
                # Surface per-metric failures rather than failing the whole poll:
                # a metric may legitimately not exist yet on a new service.
                errors[spec["key"]] = "%s: %s" % (type(exc).__name__, exc)
        with self._lock:
            self._snapshot = snapshot
            self.errors = errors
            self.last_poll_ts = time.time()
            self.polls += 1
            self.last_error = next(iter(errors.values()), None)
        return {"metrics": len(snapshot), "errors": errors}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self._stop.wait(settings.metrics_poll_interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gcp-metric-collector")
        self._thread.start()

    def retarget(self) -> None:
        """Forget the previous project's series on a switch."""
        with self._lock:
            self._snapshot = {}
            self.errors = {}
            self.last_poll_ts = 0.0

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            age = (round(time.time() - self.last_poll_ts, 1)
                   if self.last_poll_ts else None)
            return {
                "tier": "NEAR_REAL_TIME",
                "source": "cloud-monitoring",
                "chargeable": False,
                "note": "Google Cloud system metrics are non-chargeable to "
                        "read. Sampled roughly every 60s with a few minutes "
                        "of visibility delay -- expect single-digit minutes "
                        "end to end.",
                "pollIntervalS": settings.metrics_poll_interval_s,
                "lastPollAgeS": age,
                "polls": self.polls,
                "errors": dict(self.errors),
                "metrics": dict(self._snapshot),
            }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "collector": "cloud-monitoring",
                "running": self._thread is not None and self._thread.is_alive(),
                "polls": self.polls,
                "lastPollAgeS": round(time.time() - self.last_poll_ts, 1)
                if self.last_poll_ts else None,
                "lastError": self.last_error,
                "metricErrors": dict(self.errors),
                "tier": "NEAR_REAL_TIME",
            }


collector = GcpMetricCollector()
