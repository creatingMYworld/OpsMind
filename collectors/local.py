"""Local-mode collector: CogniKart POSTs its log lines straight to us.

This is the development and fallback path. The entries are the exact JSON
objects the services wrote to stdout, so they go through the same normalizer
as Cloud Logging entries and every engine downstream is unaware of the
difference.
"""
from typing import Any, Dict, List

from ..engine.normalize import normalize_local
from ..store import store


def ingest(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    records = []
    bad = 0
    for raw in entries or []:
        try:
            records.append(normalize_local(raw))
        except Exception:  # a malformed line must not reject the batch
            bad += 1
    added = store.add_many(records)
    return {"received": len(entries or []), "accepted": added,
            "rejected": bad, "source": "local"}
