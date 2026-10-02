"""Turn a raw log entry into one canonical Record, whatever its source.

Two inputs, one output shape:

  local mode -- the exact JSON object CogniKart wrote to stdout, POSTed to
                /internal/ingest.
  gcp mode   -- a google.cloud.logging entry, where Cloud Logging has already
                promoted severity/timestamp/trace/httpRequest out of the
                payload into entry attributes.

Everything downstream (grouping, rules, incidents, cost, KPIs) consumes only
Record, so neither mode gets special-cased past this file. That is what makes
the two modes genuinely equivalent rather than one being a pale imitation.
"""
import datetime
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

SEVERITY_RANK = {"DEBUG": 0, "INFO": 1, "NOTICE": 1, "WARNING": 2, "ERROR": 3,
                 "CRITICAL": 4, "ALERT": 4, "EMERGENCY": 4}

# Normalize numbers, ids and hex out of messages so that logically identical
# errors collapse into one group. "order ord_7a3f failed" and
# "order ord_91bc failed" must group together.
_NUM_RE = re.compile(r"\d+")
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_ID_RE = re.compile(r"\b(?:ord|req|sess|txn|SKU)[_-][A-Za-z0-9]+\b")


@dataclass
class Record:
    """One canonical log line. Field names match the CogniKart log schema."""

    ts: float                               # epoch seconds
    severity: str
    service: str
    message: str
    event: str = ""
    serviceRole: str = ""
    serviceVersion: str = ""
    environment: str = ""
    route: Optional[str] = None
    httpStatus: Optional[int] = None
    latencyMs: Optional[int] = None
    errorCode: Optional[str] = None
    errorClass: Optional[str] = None
    trace: Optional[str] = None
    spanId: Optional[str] = None
    requestId: Optional[str] = None
    sessionId: Optional[str] = None
    # Identity. userIdHash is a salted pseudonym, never an email: it is what
    # makes "who signed in" and "who was affected" answerable without holding
    # PII in the monitoring system.
    userIdHash: Optional[str] = None
    actorRole: Optional[str] = None
    orderId: Optional[str] = None
    orderStatus: Optional[str] = None
    productId: Optional[str] = None
    units: Optional[int] = None
    paymentOutcomeRequested: Optional[str] = None
    failureReason: Optional[str] = None
    cartValueInr: Optional[float] = None
    skuCount: Optional[int] = None
    retryCount: Optional[int] = None
    dependency: Optional[str] = None
    dependencyLatencyMs: Optional[int] = None
    stockShortfall: Optional[bool] = None
    instanceId: Optional[str] = None
    # Heartbeat-only fields (event == service.heartbeat)
    cpuPct: Optional[float] = None
    rssMb: Optional[float] = None
    inflight: Optional[int] = None
    logBytesTotal: Optional[int] = None
    # Provenance and bookkeeping
    insertId: Optional[str] = None   # Cloud Logging's unique per-entry id
    source: str = "local"
    sizeBytes: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_server_error(self) -> bool:
        return (self.httpStatus or 0) >= 500

    @property
    def is_client_error(self) -> bool:
        return 400 <= (self.httpStatus or 0) < 500

    @property
    def is_heartbeat(self) -> bool:
        return self.event == "service.heartbeat"

    def normalized_message(self) -> str:
        m = _ID_RE.sub("<id>", self.message or "")
        m = _HEX_RE.sub("<hex>", m)
        m = _NUM_RE.sub("<n>", m)
        return m.strip()[:160]

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not include_raw:
            d.pop("raw", None)
        return {k: v for k, v in d.items() if v is not None}


def _f(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_latency(value: Any) -> Optional[int]:
    """Cloud Logging's httpRequest.latency is e.g. '4.213s' or a duration."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(float(value) * 1000)
    s = str(value).strip()
    if s.endswith("s"):
        try:
            return int(float(s[:-1]) * 1000)
        except ValueError:
            return None
    return None


def _iso_to_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.timestamp()
    s = str(value or "").strip()
    if not s:
        return 0.0
    s = s.replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _from_payload(payload: Dict[str, Any], *, severity: str, ts: float,
                  trace: Optional[str], span_id: Optional[str],
                  http_request: Optional[Dict[str, Any]], source: str,
                  size_bytes: int) -> Record:
    """Shared construction path for both modes."""
    http_request = http_request or {}
    status = _i(payload.get("httpStatus")) or _i(http_request.get("status"))
    latency = _i(payload.get("latencyMs"))
    if latency is None:
        latency = _parse_latency(http_request.get("latency"))

    return Record(
        ts=ts,
        severity=(severity or "INFO").upper(),
        service=str(payload.get("service") or "unknown"),
        serviceRole=str(payload.get("serviceRole") or ""),
        serviceVersion=str(payload.get("serviceVersion") or ""),
        environment=str(payload.get("environment") or ""),
        message=str(payload.get("message") or ""),
        event=str(payload.get("event") or ""),
        route=payload.get("route") or http_request.get("requestUrl"),
        httpStatus=status,
        latencyMs=latency,
        errorCode=payload.get("errorCode"),
        errorClass=payload.get("errorClass"),
        trace=trace,
        spanId=span_id,
        requestId=payload.get("requestId"),
        sessionId=payload.get("sessionId"),
        userIdHash=payload.get("userIdHash"),
        actorRole=payload.get("actorRole"),
        orderId=payload.get("orderId"),
        orderStatus=payload.get("orderStatus"),
        productId=payload.get("productId"),
        units=_i(payload.get("units")),
        paymentOutcomeRequested=payload.get("paymentOutcomeRequested"),
        failureReason=payload.get("failureReason"),
        cartValueInr=_f(payload.get("cartValueInr")),
        skuCount=_i(payload.get("skuCount")),
        retryCount=_i(payload.get("retryCount")),
        dependency=payload.get("dependency"),
        dependencyLatencyMs=_i(payload.get("dependencyLatencyMs")),
        stockShortfall=payload.get("stockShortfall"),
        instanceId=payload.get("instanceId"),
        cpuPct=_f(payload.get("cpuPct")),
        rssMb=_f(payload.get("rssMb")),
        inflight=_i(payload.get("inflight")),
        logBytesTotal=_i(payload.get("logBytesTotal")),
        source=source,
        sizeBytes=size_bytes,
        raw=payload,
    )


def normalize_local(entry: Dict[str, Any]) -> Record:
    """Normalize a CogniKart stdout JSON object received via /internal/ingest."""
    import json as _json

    size = len(_json.dumps(entry, separators=(",", ":"), default=str)) + 1
    return _from_payload(
        entry,
        severity=entry.get("severity", "INFO"),
        ts=_iso_to_epoch(entry.get("time") or entry.get("timestamp")),
        trace=entry.get("logging.googleapis.com/trace"),
        span_id=entry.get("logging.googleapis.com/spanId"),
        http_request=entry.get("httpRequest"),
        source="local",
        size_bytes=size,
    )


def normalize_gcp(entry: Any) -> Optional[Record]:
    """Normalize a google.cloud.logging entry.

    Only structured (jsonPayload) entries are of interest; Cloud Logging also
    carries text entries and request logs we do not need, and silently
    skipping them is correct rather than coercing them into a bad Record.
    """
    payload = getattr(entry, "payload", None)
    if not isinstance(payload, dict):
        return None

    http_request = getattr(entry, "http_request", None)
    if http_request is not None and not isinstance(http_request, dict):
        try:
            http_request = dict(http_request)
        except Exception:
            http_request = None

    resource_labels = {}
    resource = getattr(entry, "resource", None)
    if resource is not None:
        resource_labels = dict(getattr(resource, "labels", {}) or {})

    rec = _from_payload(
        payload,
        severity=str(getattr(entry, "severity", "INFO") or "INFO"),
        ts=_iso_to_epoch(getattr(entry, "timestamp", None)),
        trace=getattr(entry, "trace", None),
        span_id=getattr(entry, "span_id", None),
        http_request=http_request,
        source="gcloud-logging",
        size_bytes=0,
    )
    # insert_id is Cloud Logging's unique identifier for an entry. It is the
    # correct dedupe key when a poll window overlaps the previous one.
    rec.insertId = getattr(entry, "insert_id", None)
    # Fall back to the Cloud Run resource label when the payload omits the
    # service name (e.g. a log line we did not write ourselves).
    if rec.service == "unknown" and resource_labels.get("service_name"):
        rec.service = resource_labels["service_name"]
    if rec.sizeBytes == 0:
        import json as _json
        rec.sizeBytes = len(_json.dumps(payload, separators=(",", ":"), default=str)) + 1
    return rec
