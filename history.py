"""Persistent historical rollups in Firestore.

The live path does not change. Cloud Logging and Cloud Monitoring still feed
the in-memory `Store`, and every real-time view still reads from it. This
module adds one thing on the side: a small daily summary, written periodically,
so the dashboard can answer "is today worse than yesterday?" -- a question the
in-memory working set can never answer, because it holds minutes, not days.

Three rules it follows:

* **Aggregates only.** Raw log lines stay in Cloud Logging, which is already a
  durable, queryable, cheaper store for them. One document per project per day
  holds counters and averages. A busy day is a few kilobytes.
* **It can never break live monitoring.** Every Firestore call is wrapped, the
  writer runs on its own task, and a failure is recorded and surfaced rather
  than raised. If Firestore is unreachable the dashboard carries on exactly as
  it did before this module existed.
* **It never invents history.** Two days of data are needed for a comparison.
  With less, the reader says so and the UI shows that it is still collecting.

Writes are transactional -- read the day, add the new window in Python, write
the whole document back. With more than one instance running, two concurrent
Increment calls over a watermark could double-count a window; a transaction
cannot. It also keeps nested per-service maps simple, since Firestore field
paths would otherwise need escaping for the hyphens in service names.
"""
import datetime
import threading
import time
from typing import Any, Dict, List, Optional

from .config import settings

_client = None
_client_error: Optional[str] = None
_lock = threading.Lock()
_last: Dict[str, Any] = {"at": None, "ok": None, "error": None,
                         "minutesWritten": 0, "writes": 0}


def _tz() -> datetime.timezone:
    return datetime.timezone(
        datetime.timedelta(minutes=settings.history_tz_offset_minutes))


def day_key(ts: Optional[float] = None) -> str:
    """The calendar day a timestamp belongs to, in the configured offset.

    UTC would roll the day over at 05:30 local for an asia-south1 deployment,
    so "yesterday" would not mean what the person reading it means.
    """
    when = datetime.datetime.fromtimestamp(ts or time.time(), _tz())
    return when.strftime("%Y-%m-%d")


def enabled() -> bool:
    return settings.history_enabled


def client() -> Any:
    """The Firestore client, or None. Built once, failures remembered."""
    global _client, _client_error
    if not enabled():
        return None
    with _lock:
        if _client is not None or _client_error is not None:
            return _client
        try:
            from google.cloud import firestore
            _client = firestore.Client(
                project=settings.project_id or None,
                database=settings.firestore_database)
        except Exception as exc:  # noqa: BLE001 - never break the live path
            _client_error = "%s: %s" % (type(exc).__name__, exc)
        return _client


def _doc_id(project: str, date: str) -> str:
    return "%s__%s" % (project or "unknown", date)


# --- collecting one window from the live store -----------------------------

def _window(store, after_minute: int) -> Dict[str, Any]:
    """Aggregate every complete minute bucket newer than `after_minute`.

    The in-progress minute is excluded: it is still filling, and folding it in
    now and again later would count part of it twice.
    """
    now_minute = int(time.time() // 60)
    rows = store.series(window_minutes=settings.minute_buckets)
    fresh = [b for b in rows
             if b["minute"] > after_minute and b["minute"] < now_minute]
    if not fresh:
        return {}

    agg: Dict[str, Any] = {
        "minutes": len(fresh), "lastMinute": max(b["minute"] for b in fresh),
        "requests": 0, "errors5xx": 0, "errors4xx": 0, "lines": 0,
        "logBytes": 0, "checkoutsConfirmed": 0, "checkoutsFailed": 0,
        "revenueConfirmedInr": 0.0,
        "p95SumMs": 0.0, "p95Samples": 0,
        "cpuPctSum": 0.0, "cpuSamples": 0, "cpuPctMax": 0.0,
        "memMbSum": 0.0, "memSamples": 0, "memMbMax": 0.0,
        "instancesMax": 0,
    }
    for b in fresh:
        for k in ("requests", "errors5xx", "errors4xx", "lines", "logBytes",
                  "checkoutsConfirmed", "checkoutsFailed"):
            agg[k] += b.get(k) or 0
        agg["revenueConfirmedInr"] += b.get("revenueConfirmedInr") or 0.0
        if b.get("p95LatencyMs") is not None:
            agg["p95SumMs"] += b["p95LatencyMs"]; agg["p95Samples"] += 1
        if b.get("cpuPctAvg") is not None:
            agg["cpuPctSum"] += b["cpuPctAvg"]; agg["cpuSamples"] += 1
            agg["cpuPctMax"] = max(agg["cpuPctMax"], b.get("cpuPctMax") or 0.0)
        if b.get("rssMbAvg") is not None:
            agg["memMbSum"] += b["rssMbAvg"]; agg["memSamples"] += 1
            agg["memMbMax"] = max(agg["memMbMax"], b.get("rssMbMax") or 0.0)
        agg["instancesMax"] = max(agg["instancesMax"], b.get("instanceCount") or 0)

    # Per-service totals, so the history is not only a platform-wide number.
    per: Dict[str, Dict[str, float]] = {}
    for svc in sorted(set(settings.watched_services)):
        rows_s = [b for b in store.series(window_minutes=settings.minute_buckets,
                                          service=svc)
                  if b["minute"] > after_minute and b["minute"] < now_minute]
        if not rows_s:
            continue
        mem_gib = settings.shape(svc).get("memoryGib") or 0.5
        mem_avg = [b["rssMbAvg"] for b in rows_s if b.get("rssMbAvg") is not None]
        p95 = [b["p95LatencyMs"] for b in rows_s if b.get("p95LatencyMs") is not None]
        cpu = [b["cpuPctAvg"] for b in rows_s if b.get("cpuPctAvg") is not None]
        per[svc] = {
            "requests": sum(b.get("requests") or 0 for b in rows_s),
            "errors5xx": sum(b.get("errors5xx") or 0 for b in rows_s),
            "errors4xx": sum(b.get("errors4xx") or 0 for b in rows_s),
            "p95SumMs": round(sum(p95), 2), "p95Samples": len(p95),
            "cpuPctSum": round(sum(cpu), 2), "cpuSamples": len(cpu),
            "memPctSum": round(sum(m / (mem_gib * 1024.0) * 100.0
                                   for m in mem_avg), 2),
            "memSamples": len(mem_avg),
        }
    agg["services"] = per
    return agg


def _merge(doc: Dict[str, Any], add: Dict[str, Any],
           cost_usd_per_hour: Optional[float]) -> Dict[str, Any]:
    """Fold one window into a day document. Pure, so it is testable."""
    out = dict(doc) if doc else {}
    for k in ("requests", "errors5xx", "errors4xx", "lines", "logBytes",
              "checkoutsConfirmed", "checkoutsFailed", "minutes",
              "p95SumMs", "p95Samples", "cpuPctSum", "cpuSamples",
              "memMbSum", "memSamples", "revenueConfirmedInr"):
        out[k] = (out.get(k) or 0) + (add.get(k) or 0)
    for k in ("cpuPctMax", "memMbMax", "instancesMax"):
        out[k] = max(out.get(k) or 0, add.get(k) or 0)

    svcs = dict(out.get("services") or {})
    for name, row in (add.get("services") or {}).items():
        cur = dict(svcs.get(name) or {})
        for k, v in row.items():
            cur[k] = (cur.get(k) or 0) + v
        svcs[name] = cur
    out["services"] = svcs

    if cost_usd_per_hour is not None:
        out["costUsdPerHourSum"] = (out.get("costUsdPerHourSum") or 0.0) + cost_usd_per_hour
        out["costSamples"] = (out.get("costSamples") or 0) + 1
    out["lastMinute"] = max(out.get("lastMinute") or 0, add.get("lastMinute") or 0)
    return out


def write_rollup(store, cost_usd_per_hour: Optional[float] = None) -> Dict[str, Any]:
    """Fold everything since the stored watermark into today's document."""
    db = client()
    if db is None:
        return {"ok": False, "reason": _client_error or "history disabled"}

    project = settings.active_project or settings.project_id or "local"
    date = day_key()
    ref = db.collection(settings.history_collection).document(_doc_id(project, date))

    try:
        from google.cloud import firestore

        @firestore.transactional
        def _txn(txn):
            snap = ref.get(transaction=txn)
            doc = snap.to_dict() if snap.exists else {}
            add = _window(store, int(doc.get("lastMinute") or 0))
            if not add:
                return 0
            merged = _merge(doc, add, cost_usd_per_hour)
            merged.update({"projectId": project, "date": date,
                           "updatedAt": time.time()})
            txn.set(ref, merged)
            return add["minutes"]

        written = _txn(db.transaction())
        _last.update({"at": time.time(), "ok": True, "error": None,
                      "minutesWritten": written,
                      "writes": _last["writes"] + (1 if written else 0)})
        return {"ok": True, "minutesWritten": written, "date": date}
    except Exception as exc:  # noqa: BLE001 - history must never break live
        _last.update({"at": time.time(), "ok": False,
                      "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"ok": False, "reason": _last["error"]}


# --- reading ---------------------------------------------------------------

def _derive(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The comparable numbers for one day. None when the day has no data."""
    if not doc:
        return None
    p95_n = doc.get("p95Samples") or 0
    cpu_n = doc.get("cpuSamples") or 0
    mem_n = doc.get("memSamples") or 0
    cost_n = doc.get("costSamples") or 0
    errors = (doc.get("errors5xx") or 0) + (doc.get("errors4xx") or 0)
    requests = doc.get("requests") or 0
    return {
        "date": doc.get("date"),
        "minutesObserved": doc.get("minutes") or 0,
        "requests": requests,
        "errors": errors,
        "errors5xx": doc.get("errors5xx") or 0,
        "errors4xx": doc.get("errors4xx") or 0,
        "errorRatePct": round(100.0 * (doc.get("errors5xx") or 0) / requests, 2)
                        if requests else None,
        "p95LatencyMs": round(doc["p95SumMs"] / p95_n, 1) if p95_n else None,
        "cpuPct": round(doc["cpuPctSum"] / cpu_n, 1) if cpu_n else None,
        "memoryMb": round(doc["memMbSum"] / mem_n, 1) if mem_n else None,
        "instancesMax": doc.get("instancesMax") or 0,
        "logMib": round((doc.get("logBytes") or 0) / 1048576.0, 2),
        "checkoutsConfirmed": doc.get("checkoutsConfirmed") or 0,
        "checkoutsFailed": doc.get("checkoutsFailed") or 0,
        "modeledUsdPerHour": round(doc["costUsdPerHourSum"] / cost_n, 4)
                             if cost_n else None,
    }


_COMPARED = ("requests", "errors", "errors5xx", "p95LatencyMs", "cpuPct",
             "memoryMb", "logMib", "checkoutsConfirmed", "modeledUsdPerHour")


def _changes(today: Dict[str, Any], prior: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in _COMPARED:
        a, b = today.get(k), prior.get(k)
        if a is None or b is None:
            out[k] = {"today": a, "yesterday": b, "changePct": None,
                      "reason": "not measured on both days"}
            continue
        out[k] = {
            "today": a, "yesterday": b,
            "changePct": round(100.0 * (a - b) / b, 1) if b else None,
            "reason": None if b else "yesterday was zero, so there is no ratio",
        }
    return out


def _fetch(project: str, date: str) -> Optional[Dict[str, Any]]:
    db = client()
    if db is None:
        return None
    snap = db.collection(settings.history_collection).document(
        _doc_id(project, date)).get()
    return snap.to_dict() if snap.exists else None


def compare(project: Optional[str] = None) -> Dict[str, Any]:
    """Today against yesterday. Never fabricates a day that has no data."""
    project = project or settings.active_project or settings.project_id or "local"
    base = {"enabled": enabled(), "projectId": project,
            "collection": settings.history_collection,
            "timezoneOffsetMinutes": settings.history_tz_offset_minutes}
    if not enabled():
        return dict(base, available=False,
                    message="Historical data is not enabled. Set HISTORY_ENABLED=true.")
    if client() is None:
        return dict(base, available=False,
                    message="Historical data is being collected.",
                    error=_client_error)

    today_key = day_key()
    y_key = day_key(time.time() - 86400)
    try:
        today = _derive(_fetch(project, today_key))
        prior = _derive(_fetch(project, y_key))
    except Exception as exc:  # noqa: BLE001
        return dict(base, available=False,
                    message="Historical data is being collected.",
                    error="%s: %s" % (type(exc).__name__, exc))

    if today is None or prior is None:
        have = [d for d, v in ((today_key, today), (y_key, prior)) if v]
        return dict(base, available=False, today=today, yesterday=prior,
                    daysWithData=have,
                    message="Historical data is being collected. A comparison "
                            "needs a full day on both sides; nothing is "
                            "estimated to fill the gap.")
    return dict(base, available=True, today=today, yesterday=prior,
                metrics=_changes(today, prior),
                note="Each figure is the total or average over the minutes "
                     "actually observed that day, not a projection. Latency is "
                     "the mean of the per-minute p95 values.")


def recent(project: Optional[str] = None, days: int = 7) -> Dict[str, Any]:
    """The last N days, oldest first. Days with no document are omitted."""
    project = project or settings.active_project or settings.project_id or "local"
    if not enabled() or client() is None:
        return {"enabled": enabled(), "available": False, "days": [],
                "message": "Historical data is being collected."}
    out: List[Dict[str, Any]] = []
    now = time.time()
    try:
        for i in range(days - 1, -1, -1):
            row = _derive(_fetch(project, day_key(now - i * 86400)))
            if row:
                out.append(row)
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "available": False, "days": [],
                "message": "Historical data is being collected.",
                "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"enabled": True, "available": bool(out), "projectId": project,
            "days": out,
            "message": None if out else "Historical data is being collected."}


def record_incident(incident: Dict[str, Any]) -> bool:
    """Persist one incident so the history outlives a portal restart."""
    db = client()
    if db is None:
        return False
    project = settings.active_project or settings.project_id or "local"
    iid = str(incident.get("id") or incident.get("incidentId") or "")
    if not iid:
        return False
    try:
        db.collection(settings.history_events_collection).document(
            "%s__%s" % (project, iid)).set({
                "projectId": project,
                "date": day_key(incident.get("startedAt")),
                "incidentId": iid,
                "rule": incident.get("rule") or incident.get("ruleId"),
                "service": incident.get("service"),
                "severity": incident.get("severity"),
                "status": incident.get("status"),
                "startedAt": incident.get("startedAt"),
                "resolvedAt": incident.get("resolvedAt"),
                "peakValue": incident.get("peakValue"),
                "summary": incident.get("summary") or incident.get("title"),
                "updatedAt": time.time(),
            }, merge=True)
        return True
    except Exception:  # noqa: BLE001 - an unrecorded incident is not an outage
        return False


def status() -> Dict[str, Any]:
    """Surfaced on /api/v1/meta so the state is inspectable, not guessed."""
    return {
        "enabled": enabled(),
        "backend": "firestore" if enabled() else None,
        "database": settings.firestore_database if enabled() else None,
        "collection": settings.history_collection,
        "writeIntervalS": settings.history_write_interval_s,
        "timezoneOffsetMinutes": settings.history_tz_offset_minutes,
        "connected": bool(client()) if enabled() else False,
        "clientError": _client_error,
        "lastWrite": dict(_last),
    }
