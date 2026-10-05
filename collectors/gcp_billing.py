"""The authoritative cost tier: Cloud Billing export in BigQuery.

This is the slowest of the three tiers and the only one that is not an
estimate. Google writes usage and cost rows to a BigQuery dataset several
times a day, with up to roughly 24 hours of latency and several days of
backfill, so it can never answer "what is this incident costing right now" --
that is what the modeled tier is for. What it *can* do is check the modeled
number against Google's own invoice, which turns "we computed a figure" into
"we computed a figure and verified it".

Three rules, because a cost query that is itself expensive would be an
embarrassing thing to ship in a cost-optimization tool:

* **Never on the request path.** Results are cached and refreshed on a timer;
  a page load never triggers a query.
* **Always filtered on the partition column.** These tables are partitioned on
  _PARTITIONTIME; an unfiltered scan reads the whole table every time.
* **Always capped.** Every job sets maximum_bytes_billed, so a mistake fails
  loudly instead of quietly spending money.

It reports honestly in every state it can be in: not configured, configured
but empty, or populated. "Configured but empty" is the normal state for about
a day after enabling the export, and saying so is more useful than an error.
"""
import threading
import time
from typing import Any, Dict, List, Optional

from ..config import settings

# A day of rows for a handful of Cloud Run services is a few hundred KB. A
# 200 MB cap is far above that and far below anything that could cost money.
_MAX_BYTES = 200 * 1024 * 1024
_CACHE_TTL_S = 900.0

_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_client = None
_client_error: Optional[str] = None
_table_cache: Dict[str, Any] = {"at": 0.0, "name": None}


def enabled() -> bool:
    return bool(settings.billing_export_dataset) and settings.data_source == "gcp"


def _get_client() -> Any:
    global _client, _client_error
    with _lock:
        if _client is not None or _client_error is not None:
            return _client
        try:
            from google.cloud import bigquery
            _client = bigquery.Client(project=settings.project_id or None)
        except Exception as exc:  # noqa: BLE001 - never break the live path
            _client_error = "%s: %s" % (type(exc).__name__, exc)
        return _client


def _find_table() -> Optional[str]:
    """The export table, discovered rather than configured.

    Its name carries the billing account id, which nobody should have to copy
    into an environment variable. The detailed (resource-level) table is
    preferred because it carries per-SKU rows, which is what reconciliation
    needs; the standard table is the fallback.
    """
    if settings.billing_export_table:
        return settings.billing_export_table
    now = time.time()
    if _table_cache["name"] and now - _table_cache["at"] < 3600:
        return _table_cache["name"]
    client = _get_client()
    if client is None:
        return None
    try:
        dataset = "%s.%s" % (settings.project_id, settings.billing_export_dataset)
        names = [t.table_id for t in client.list_tables(dataset)]
    except Exception:  # noqa: BLE001
        return None
    detailed = [n for n in names if n.startswith("gcp_billing_export_resource_v1_")]
    standard = [n for n in names if n.startswith("gcp_billing_export_v1_")]
    chosen = (sorted(detailed) or sorted(standard) or [None])[0]
    if chosen:
        _table_cache.update({"at": now, "name": chosen})
    return chosen


def _run(sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    from google.cloud import bigquery

    client = _get_client()
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=_MAX_BYTES,
        use_query_cache=True))
    return [dict(row) for row in job.result(timeout=settings.billing_query_timeout_s)]


def _query(days: int) -> Dict[str, Any]:
    from google.cloud import bigquery

    table = _find_table()
    if not table:
        return {"state": "no_table", "table": None}

    full = "`%s.%s.%s`" % (settings.project_id, settings.billing_export_dataset, table)
    params = [bigquery.ScalarQueryParameter("days", "INT64", days)]

    # _PARTITIONTIME keeps the scan to the days asked for. usage_start_time is
    # the business date and is NOT the partition column; filtering only on it
    # would read the whole table.
    totals = _run(
        "SELECT COUNT(*) AS rows_seen, "
        "       SUM(cost) AS cost, "
        "       MIN(usage_start_time) AS earliest, "
        "       MAX(usage_start_time) AS latest, "
        "       ANY_VALUE(currency) AS currency "
        "FROM %s "
        "WHERE _PARTITIONTIME >= TIMESTAMP_SUB("
        "  TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY), INTERVAL @days DAY)" % full,
        params)
    row = totals[0] if totals else {}
    if not row or not row.get("rows_seen"):
        return {"state": "empty", "table": table}

    by_service = _run(
        "SELECT service.description AS service, SUM(cost) AS cost "
        "FROM %s "
        "WHERE _PARTITIONTIME >= TIMESTAMP_SUB("
        "  TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY), INTERVAL @days DAY) "
        "GROUP BY service ORDER BY cost DESC LIMIT 12" % full, params)

    return {
        "state": "ok",
        "table": table,
        "rowsSeen": int(row.get("rows_seen") or 0),
        "windowDays": days,
        "totalCost": round(float(row.get("cost") or 0.0), 6),
        "currency": row.get("currency") or "USD",
        "earliest": str(row.get("earliest")) if row.get("earliest") else None,
        "latest": str(row.get("latest")) if row.get("latest") else None,
        "byService": [{"service": r["service"],
                       "cost": round(float(r["cost"] or 0.0), 6)}
                      for r in by_service],
    }


def status(days: int = 7, force: bool = False) -> Dict[str, Any]:
    """The billed tier, cached. Safe to call from a request handler."""
    base: Dict[str, Any] = {
        "tier": "AUTHORITATIVE",
        "kind": "billed",
        "dataset": settings.billing_export_dataset or None,
        "latencyCharacteristics": (
            "Exported several times a day; rows typically appear within about "
            "24 hours and backfill over a few days. Never real-time."),
    }
    if not enabled():
        return dict(base, available=False, configured=False, reason=(
            "Billing export is not configured for this deployment. Set "
            "BILLING_EXPORT_DATASET once the export is writing to BigQuery."
            if settings.data_source == "gcp" else
            "Running on local data, so there is no billing account to read."))

    now = time.time()
    if not force and _cache["value"] and now - _cache["at"] < _CACHE_TTL_S:
        return dict(_cache["value"], cached=True)

    if _get_client() is None:
        return dict(base, available=False, configured=True,
                    reason="Could not reach BigQuery.", error=_client_error)
    try:
        got = _query(days)
    except Exception as exc:  # noqa: BLE001 - a cost panel must not 500
        return dict(base, available=False, configured=True,
                    reason="The billing export query failed.",
                    error="%s: %s" % (type(exc).__name__, exc))

    if got["state"] == "no_table":
        out = dict(base, available=False, configured=True, reason=(
            "The dataset exists but Google has not created the export table "
            "yet. That table appears within a few hours of enabling the "
            "export."))
    elif got["state"] == "empty":
        out = dict(base, available=False, configured=True, table=got["table"],
                   reason=(
            "The export table exists and is empty. Billing rows land with "
            "roughly a day of latency, so this is the expected state for "
            "about a day after enabling the export. Nothing is estimated to "
            "fill the gap."))
    else:
        out = dict(base, available=True, configured=True, **{
            k: v for k, v in got.items() if k != "state"})
    out["fetchedAt"] = now
    _cache.update({"at": now, "value": out})
    return dict(out, cached=False)


def reconcile(modeled_usd_per_hour: Optional[float], days: int = 7) -> Dict[str, Any]:
    """Modeled cost against billed cost over the same window.

    The honest version of "our live number is accurate": it only produces a
    figure when there is billed data to check against, and says why when there
    is not. The modeled rate is extrapolated across the window, which assumes
    the current rate held -- stated here rather than buried, because for a
    demo project driven by a load generator in bursts it very often did not.
    """
    billed = status(days=days)
    out: Dict[str, Any] = {"windowDays": days,
                           "modeledUsdPerHour": modeled_usd_per_hour,
                           "billedAvailable": bool(billed.get("available"))}
    if not billed.get("available"):
        return dict(out, accuracyPct=None,
                    reason=billed.get("reason") or "No billed data yet.")
    if not modeled_usd_per_hour:
        return dict(out, accuracyPct=None,
                    reason="No modeled rate to compare against yet.")

    modeled_total = modeled_usd_per_hour * 24 * days
    billed_total = float(billed.get("totalCost") or 0.0)
    if billed_total <= 0:
        return dict(out, accuracyPct=None, billedTotal=billed_total,
                    reason="Billed total is zero for this window.")
    error_pct = abs(modeled_total - billed_total) / billed_total * 100.0
    return dict(out,
                modeledTotal=round(modeled_total, 4),
                billedTotal=round(billed_total, 4),
                currency=billed.get("currency"),
                accuracyPct=round(max(0.0, 100.0 - error_pct), 1),
                caveat=(
                    "The modeled rate is extrapolated across %d days, which "
                    "assumes the observed rate held throughout. Traffic driven "
                    "by a load generator in bursts will not satisfy that, so "
                    "read this as an order-of-magnitude check." % days))
