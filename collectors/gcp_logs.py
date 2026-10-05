"""Cloud Logging collector -- the real submission path.

Reads structured log entries written by CogniKart on Cloud Run via the Cloud
Logging API. No agent, no sidecar, no Pub/Sub: Cloud Run captured stdout
automatically, and this polls it back out.

Why polling rather than `entries.tail`
--------------------------------------
`tail_log_entries` is a long-lived bidirectional gRPC stream with genuinely
lower latency. On Cloud Run a long-lived stream is a reliability liability for
a demo -- it dies on instance recycling and needs reconnection logic. Polling
every few seconds is "real-time" for a human reading a log feed, and the gap
between 4s and sub-second is imperceptible in the UI. `tail` is a documented
roadmap item, not an oversight.

Cursor handling: Cloud Logging can deliver entries slightly out of order, so
the cursor is deliberately rewound a few seconds on each poll and the store's
dedupe key absorbs the overlap. Losing an entry is worse than seeing it twice.
"""
import datetime
import threading
import time
from typing import Any, Dict, Optional

from ..config import settings
from ..engine.normalize import normalize_gcp
from ..store import store

_CURSOR_REWIND_S = 20.0
_PAGE_SIZE = 500


class GcpLogCollector:
    def __init__(self) -> None:
        self._client = None
        self._project: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.cursor: Optional[datetime.datetime] = None
        self.last_poll_ts: float = 0.0
        self.last_poll_count: int = 0
        self.last_error: Optional[str] = None
        self.polls: int = 0
        self.entries_seen: int = 0

    # --- client ------------------------------------------------------------
    def _ensure_client(self) -> Any:
        """Rebuild the client whenever the active project changes.

        The client is bound to a project at construction, so holding one across
        a project switch would keep reading the project the user just left.
        """
        project = settings.active_project or settings.project_id
        if not project:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT is not set; cannot query Cloud Logging")
        if self._client is None or self._project != project:
            from google.cloud import logging as gcl
            self._client = gcl.Client(project=project)
            self._project = project
            self.cursor = None     # a new project starts its own history
        return self._client

    def retarget(self) -> None:
        """Drop the client and cursor so the next poll picks up the new project."""
        self._client = None
        self._project = None
        self.cursor = None
        self.last_error = None

    # --- filter ------------------------------------------------------------
    def build_filter(self, since: datetime.datetime) -> str:
        services = " OR ".join('"%s"' % s for s in settings.watched_services)
        return (
            'resource.type="cloud_run_revision"\n'
            'resource.labels.service_name=(%s)\n'
            'timestamp>="%s"\n'
            # Our own structured lines always carry an `event` field. This
            # excludes Cloud Run's platform request logs, which would otherwise
            # double-count every request.
            'jsonPayload.event:*'
            % (services, since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        )

    # --- one poll ----------------------------------------------------------
    def poll_once(self) -> Dict[str, Any]:
        client = self._ensure_client()
        now = datetime.datetime.now(datetime.timezone.utc)
        if self.cursor is None:
            # Cold start: pick up the last 10 minutes so the dashboard is not
            # blank while waiting for new traffic.
            since = now - datetime.timedelta(minutes=10)
        else:
            since = self.cursor - datetime.timedelta(seconds=_CURSOR_REWIND_S)

        from google.cloud.logging import ASCENDING

        iterator = client.list_entries(
            resource_names=["projects/%s" % (settings.active_project or settings.project_id)],
            filter_=self.build_filter(since),
            order_by=ASCENDING,
            page_size=_PAGE_SIZE,
            max_results=_PAGE_SIZE,
        )

        records = []
        newest = self.cursor
        count = 0
        for entry in iterator:
            count += 1
            rec = normalize_gcp(entry)
            if rec is None:
                continue
            records.append(rec)
            ts = getattr(entry, "timestamp", None)
            if ts is not None and (newest is None or ts > newest):
                newest = ts

        added = store.add_many(records)
        if newest is not None:
            self.cursor = newest
        self.polls += 1
        self.entries_seen += count
        self.last_poll_ts = time.time()
        self.last_poll_count = added
        self.last_error = None
        return {"fetched": count, "accepted": added,
                "cursor": self.cursor.isoformat() if self.cursor else None}

    # --- loop --------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                # rules.md section 2: never silently swallow a GCP error.
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                store.last_error = self.last_error
            self._stop.wait(settings.logs_poll_interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="gcp-log-collector")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        return {
            "collector": "cloud-logging",
            "running": self._thread is not None and self._thread.is_alive(),
            "pollIntervalS": settings.logs_poll_interval_s,
            "polls": self.polls,
            "entriesSeen": self.entries_seen,
            "lastPollAccepted": self.last_poll_count,
            "lastPollAgeS": round(time.time() - self.last_poll_ts, 1)
            if self.last_poll_ts else None,
            "cursor": self.cursor.isoformat() if self.cursor else None,
            "lastError": self.last_error,
            "tier": "LIVE",
            "expectedLatency": "a few seconds (log ingestion) + poll interval",
        }


collector = GcpLogCollector()
