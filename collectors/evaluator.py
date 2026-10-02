"""Background rule evaluation -- the fast-path detector's clock.

Runs every few seconds, evaluates every enabled rule against the store, and
feeds breaches into the incident manager. Emits nothing to GCP and costs
nothing: it reads in-memory aggregates the collectors already built.
"""
import threading
import time
from typing import Any, Dict, List, Optional

from ..config import settings
from ..engine.incidents import manager
from ..engine.rules import ruleset
from ..store import store


class Evaluator:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.evaluations = 0
        self.last_eval_ts: float = 0.0
        self.last_breach_count = 0
        self.last_error: Optional[str] = None
        self.recent_transitions: List[Dict[str, Any]] = []
        self.on_change = None  # set by main.py to push SSE events

    def evaluate_once(self) -> Dict[str, Any]:
        breaches = ruleset.evaluate(store)
        result = manager.ingest(breaches, clear_required=3)
        self.evaluations += 1
        self.last_eval_ts = time.time()
        self.last_breach_count = len(breaches)
        if result["opened"] or result["resolved"]:
            entry = {"ts": time.time(), **result}
            self.recent_transitions.append(entry)
            self.recent_transitions = self.recent_transitions[-40:]
            if self.on_change:
                try:
                    self.on_change(entry)
                except Exception:
                    pass
        return {"breaches": len(breaches), **result}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.evaluate_once()
            except Exception as exc:  # noqa: BLE001
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self._stop.wait(settings.rules_eval_interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="rule-evaluator")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "intervalS": settings.rules_eval_interval_s,
            "evaluations": self.evaluations,
            "lastEvalAgeS": round(time.time() - self.last_eval_ts, 1)
            if self.last_eval_ts else None,
            "lastBreachCount": self.last_breach_count,
            "lastError": self.last_error,
            "recentTransitions": self.recent_transitions[-10:],
        }


evaluator = Evaluator()
