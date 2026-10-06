"""Threshold rules -- the FAST-PATH detector.

Relationship to Cloud Monitoring alerting policies
--------------------------------------------------
docs/prd.md section 5 says: do not build a custom alerting engine where a real
Cloud Monitoring policy is the more credible, GCP-native choice. This module
does not replace Monitoring alerting; it complements it, and the dashboard
labels both so the distinction is visible rather than glossed over:

  Cloud Monitoring policy  -- the GCP-native, durable alert. Survives a
                              platform restart, notifies out of band, and is
                              what you would run in production. Latency:
                              1-3 minutes (metric sampling + evaluation).
  Fast-path rule (here)    -- evaluates the SAME real log stream every few
                              seconds. Latency: seconds. It exists because a
                              live demo cannot stand three minutes of silence
                              after a failure is injected.

Both read real telemetry. Neither invents anything. You create the Monitoring
policy once (docs/DEPLOY.md step 9) and it appears in the Alerts view beside
these rules, each tagged with its source and observed latency.

Thresholds are user-editable at runtime via PATCH /api/v1/alerts/{id}, which
satisfies the brief's "set alert thresholds" step in the product itself.
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import settings

SCOPE_SERVICE = "service"
SCOPE_GLOBAL = "global"

CAT_APPLICATION = "application"
CAT_COST = "cost"
CAT_BUSINESS = "business"


@dataclass
class Rule:
    id: str
    name: str
    category: str
    scope: str
    metric: str
    comparator: str            # "gt" or "lt"
    threshold: float
    unit: str
    windowMinutes: int
    severity: str
    description: str
    rationale: str
    minRequests: int = 0
    enabled: bool = True
    source: str = "fast-path"
    # Consecutive clear evaluations required before auto-resolving. Stops an
    # incident flapping closed on a single quiet bucket.
    clearEvaluations: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "scope": self.scope, "metric": self.metric,
            "comparator": self.comparator, "threshold": self.threshold,
            "unit": self.unit, "windowMinutes": self.windowMinutes,
            "severity": self.severity, "minRequests": self.minRequests,
            "enabled": self.enabled, "source": self.source,
            "description": self.description, "rationale": self.rationale,
            "expectedDetectionLatencyS": settings.rules_eval_interval_s + 5,
        }


@dataclass
class Breach:
    rule: Rule
    scopeKey: str              # service name, or "__all__"
    observed: float
    threshold: float
    windowMinutes: int
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return "%s::%s" % (self.rule.id, self.scopeKey)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ruleId": self.rule.id,
            "ruleName": self.rule.name,
            "category": self.rule.category,
            "severity": self.rule.severity,
            "scope": self.scopeKey,
            "metric": self.rule.metric,
            "observed": self.observed,
            "threshold": self.threshold,
            "unit": self.rule.unit,
            "comparator": self.rule.comparator,
            "windowMinutes": self.windowMinutes,
            "evidence": self.evidence,
        }


def default_rules() -> List[Rule]:
    return [
        Rule(
            id="app-5xx-rate", name="5xx error rate", category=CAT_APPLICATION,
            scope=SCOPE_SERVICE, metric="errorRate5xx", comparator="gt",
            threshold=0.05, unit="ratio", windowMinutes=2, severity="HIGH",
            minRequests=8,
            description="Share of served requests returning 5xx, per service.",
            rationale="A 5xx is a server fault. 4xx is excluded because a bad "
                      "product id is a client mistake, not a service failure.",
        ),
        Rule(
            id="app-p95-latency", name="p95 request latency",
            category=CAT_APPLICATION, scope=SCOPE_SERVICE, metric="p95LatencyMs",
            comparator="gt", threshold=2000.0, unit="ms", windowMinutes=2,
            severity="HIGH", minRequests=5,
            description="95th percentile request latency, per service.",
            rationale="p95 rather than mean: the mean hides a tail that users "
                      "actually experience.",
        ),
        Rule(
            id="app-memory-util", name="Memory utilisation",
            category=CAT_APPLICATION, scope=SCOPE_SERVICE,
            metric="memoryUtilisationPct", comparator="gt", threshold=80.0,
            unit="%", windowMinutes=3, severity="MEDIUM",
            description="Peak resident memory against provisioned container memory.",
            rationale="Cloud Run kills a container that exceeds its memory "
                      "limit, so sustained high utilisation predicts restarts.",
        ),
        Rule(
            id="app-cpu-util", name="CPU utilisation",
            category=CAT_APPLICATION, scope=SCOPE_SERVICE, metric="cpuPctMax",
            comparator="gt", threshold=85.0, unit="%", windowMinutes=3,
            severity="MEDIUM",
            description="Peak process CPU, from service heartbeats.",
            rationale="Heartbeat CPU is seconds-fresh; Cloud Monitoring CPU "
                      "lags by minutes.",
        ),
        Rule(
            id="cost-log-volume", name="Log ingestion rate",
            category=CAT_COST, scope=SCOPE_GLOBAL, metric="logMibPerMin",
            comparator="gt", threshold=4.0, unit="MiB/min", windowMinutes=3,
            severity="MEDIUM",
            description="Measured log bytes per minute across all services.",
            rationale="Cloud Logging bills $0.50/GiB beyond 50 GiB free per "
                      "project per month. Log volume is the fastest-moving "
                      "cost driver in this architecture and the one most "
                      "likely to cause a surprise bill.",
        ),
        Rule(
            id="cost-rate-spike", name="Modeled cost rate increase",
            category=CAT_COST, scope=SCOPE_GLOBAL, metric="costIncreasePct",
            comparator="gt", threshold=60.0, unit="%", windowMinutes=5,
            severity="MEDIUM",
            description="Modeled spend rate versus the preceding baseline window.",
            rationale="GCP has no native per-service near-real-time cost "
                      "anomaly alert; billing data is a day late. This needs "
                      "our own baseline.",
        ),
        Rule(
            id="biz-checkout-success", name="Checkout success rate",
            category=CAT_BUSINESS, scope=SCOPE_GLOBAL,
            metric="checkoutSuccessRatePct", comparator="lt", threshold=80.0,
            unit="%", windowMinutes=3, severity="HIGH", minRequests=4,
            description="Confirmed checkouts as a share of started checkouts.",
            rationale="The business-impact signal. 'Checkout success fell to "
                      "31%' is a sharper statement of damage than '5xx rate "
                      "is 12%', and it is derived from the same log stream.",
        ),
    ]


class _OverrideStore:
    """Where edited thresholds survive a restart.

    One record holds every edited rule: {rule_id: {threshold, enabled,
    windowMinutes, updatedAt}}. Firestore on a deployed portal (one document,
    shared by every instance), a JSON file locally. Read-modify-write of the
    whole record keeps rule ids, which contain hyphens, out of Firestore field
    paths.
    """
    _DOC = "alert_rules"

    def __init__(self) -> None:
        self.backend = settings.alerts_backend
        self._client = None
        self._lock = threading.Lock()

    def _doc(self) -> Any:
        if self._client is None:
            from google.cloud import firestore
            self._client = firestore.Client(project=settings.project_id or None,
                                            database=settings.firestore_database)
        return self._client.collection(settings.alerts_collection).document(self._DOC)

    def load(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if self.backend == "firestore":
                snap = self._doc().get()
                return (snap.to_dict() or {}).get("rules", {}) if snap.exists else {}
            if self.backend == "file":
                if not os.path.exists(settings.alerts_file):
                    return {}
                with open(settings.alerts_file, encoding="utf-8") as f:
                    return json.load(f).get("rules", {})
            return {}

    def save(self, rule_id: str, values: Dict[str, Any]) -> None:
        with self._lock:
            if self.backend == "firestore":
                doc = self._doc()
                snap = doc.get()
                rules = (snap.to_dict() or {}).get("rules", {}) if snap.exists else {}
                rules[rule_id] = values
                doc.set({"rules": rules, "updatedAt": time.time()})
            elif self.backend == "file":
                rules = {}
                if os.path.exists(settings.alerts_file):
                    with open(settings.alerts_file, encoding="utf-8") as f:
                        rules = json.load(f).get("rules", {})
                rules[rule_id] = values
                tmp = settings.alerts_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"rules": rules}, f, indent=1)
                os.replace(tmp, settings.alerts_file)   # never a half-written file


class RuleSet:
    # How often an instance re-reads saved thresholds, so an edit made on one
    # Cloud Run instance reaches the others without a restart.
    SYNC_EVERY_S = 30.0

    def __init__(self) -> None:
        self._rules: Dict[str, Rule] = {r.id: r for r in default_rules()}
        self._clear_counts: Dict[str, int] = {}
        self._store = _OverrideStore()
        self._synced_at = 0.0
        self.persistence: Dict[str, Any] = {
            "backend": self._store.backend,
            "persistent": self._store.backend in ("firestore", "file"),
            "loadedAt": None, "lastError": None,
        }

    def sync(self, force: bool = False) -> None:
        """Apply saved thresholds. Never raises: a store that cannot be read
        leaves the current values in place and records why."""
        if not force and time.time() - self._synced_at < self.SYNC_EVERY_S:
            return
        self._synced_at = time.time()
        try:
            saved = self._store.load()
        except Exception as exc:  # noqa: BLE001
            self.persistence["lastError"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            return
        for rule_id, v in saved.items():
            rule = self._rules.get(rule_id)
            if not rule:
                continue
            if v.get("threshold") is not None:
                rule.threshold = float(v["threshold"])
            if v.get("enabled") is not None:
                rule.enabled = bool(v["enabled"])
            if v.get("windowMinutes") is not None:
                rule.windowMinutes = max(1, min(int(v["windowMinutes"]), 60))
        self.persistence.update(loadedAt=time.time(), lastError=None)

    def list(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._rules.values()]

    def get(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def update(self, rule_id: str, *, threshold: Optional[float] = None,
               enabled: Optional[bool] = None,
               window_minutes: Optional[int] = None) -> Optional[Rule]:
        """Change a rule and save it. The change is kept only if it was saved:
        an edit that silently vanished on the next restart is worse than one
        that visibly failed. Raises RuntimeError when the save fails."""
        rule = self._rules.get(rule_id)
        if not rule:
            return None
        before = (rule.threshold, rule.enabled, rule.windowMinutes)
        if threshold is not None:
            rule.threshold = float(threshold)
        if enabled is not None:
            rule.enabled = bool(enabled)
        if window_minutes is not None:
            rule.windowMinutes = max(1, min(int(window_minutes), 60))
        try:
            self._store.save(rule_id, {"threshold": rule.threshold, "enabled": rule.enabled,
                                       "windowMinutes": rule.windowMinutes,
                                       "updatedAt": time.time()})
        except Exception as exc:  # noqa: BLE001
            rule.threshold, rule.enabled, rule.windowMinutes = before
            self.persistence["lastError"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            raise RuntimeError(self.persistence["lastError"])
        self.persistence["lastError"] = None
        return rule

    # --- metric extraction -------------------------------------------------
    @staticmethod
    def _service_metrics(store, service: str, window: int) -> Dict[str, Any]:
        rows = store.service_summary(window_minutes=window)
        for r in rows:
            if r["service"] == service:
                return r
        return {}

    @staticmethod
    def _global_metrics(store, window: int) -> Dict[str, Any]:
        """Always returns the full key set, with None where there is no data.

        Returning {} on an empty window made rule metrics look non-existent to
        validate_metrics() and produced false "this rule can never fire"
        warnings. A stable key set is also easier to reason about: a metric
        that is None is "no data", not "no such metric".
        """
        buckets = store.series(window_minutes=window)
        log_bytes = sum(b.get("logBytes", 0) for b in buckets)
        started = sum(b.get("checkoutsStarted", 0) for b in buckets)
        confirmed = sum(b.get("checkoutsConfirmed", 0) for b in buckets)
        failed = sum(b.get("checkoutsFailed", 0) for b in buckets)
        settled = confirmed + failed
        return {
            "logMibPerMin": round(
                (log_bytes / (1024.0 * 1024.0)) / max(len(buckets), 1), 4)
            if buckets else None,
            "checkoutSuccessRatePct": round(100.0 * confirmed / settled, 2)
            if settled else None,
            "checkoutsSettled": settled,
            "checkoutsStarted": started,
            "checkoutsConfirmed": confirmed,
            "checkoutsFailed": failed,
            "revenueFailedInr": round(
                sum(b.get("revenueFailedInr", 0.0) for b in buckets), 2),
            "requests": sum(b.get("requests", 0) for b in buckets),
        }

    @staticmethod
    def _cost_increase_pct(store, window: int) -> Dict[str, Any]:
        from . import cost as cost_engine
        cur = cost_engine.rate(store, window_minutes=window)["usdPerHour"]
        all_b = store.series(window_minutes=settings.minute_buckets)
        if len(all_b) < window * 2:
            return {"costIncreasePct": None, "currentUsdPerHour": cur}
        now_m = int(time.time() // 60)
        prior = [b for b in all_b
                 if now_m - 2 * window < b["minute"] <= now_m - window]
        if not prior:
            return {"costIncreasePct": None, "currentUsdPerHour": cur}
        # Reuse the pricing path on the prior window for an apples-to-apples
        # comparison rather than eyeballing request counts.
        prior_usage = cost_engine._usage_from_buckets(prior)
        priced = cost_engine._price_service(
            "__baseline__", prior_usage, len(prior) * 60.0)
        base = priced["usdPerHour"]["total"]
        return {
            "costIncreasePct": round(100.0 * (cur - base) / base, 1)
            if base > 0 else None,
            "currentUsdPerHour": cur,
            "baselineUsdPerHour": round(base, 4),
        }

    # --- evaluation --------------------------------------------------------
    def evaluate(self, store) -> List[Breach]:
        self.sync()
        breaches: List[Breach] = []
        services = sorted(set(settings.watched_services))

        for rule in self._rules.values():
            if not rule.enabled:
                continue

            if rule.scope == SCOPE_SERVICE:
                for service in services:
                    m = self._service_metrics(store, service, rule.windowMinutes)
                    if not m:
                        continue
                    if rule.minRequests and (m.get("requests") or 0) < rule.minRequests:
                        continue
                    observed = m.get(rule.metric)
                    if observed is None:
                        continue
                    if self._breached(rule, float(observed)):
                        breaches.append(Breach(
                            rule=rule, scopeKey=service, observed=float(observed),
                            threshold=rule.threshold,
                            windowMinutes=rule.windowMinutes,
                            evidence={
                                "requests": m.get("requests"),
                                "errors5xx": m.get("errors5xx"),
                                "errors4xx": m.get("errors4xx"),
                                "p95LatencyMs": m.get("p95LatencyMs"),
                                "cpuPctMax": m.get("cpuPctMax"),
                                "rssMbMax": m.get("rssMbMax"),
                                "memoryUtilisationPct": m.get("memoryUtilisationPct"),
                                "instanceCount": m.get("instanceCount"),
                            },
                        ))
            else:
                m = dict(self._global_metrics(store, rule.windowMinutes))
                if rule.metric == "costIncreasePct":
                    m.update(self._cost_increase_pct(store, rule.windowMinutes))
                observed = m.get(rule.metric)
                if observed is None:
                    continue
                if rule.minRequests and (m.get("checkoutsSettled") or
                                         m.get("requests") or 0) < rule.minRequests:
                    continue
                if self._breached(rule, float(observed)):
                    breaches.append(Breach(
                        rule=rule, scopeKey="__all__", observed=float(observed),
                        threshold=rule.threshold,
                        windowMinutes=rule.windowMinutes, evidence=m,
                    ))
        return breaches

    def validate_metrics(self, store) -> List[Dict[str, Any]]:
        """Check that every enabled rule's metric name actually resolves.

        A rule whose metric key does not exist evaluates to None and silently
        never fires -- which is exactly how the 5xx rule was dead on arrival
        once. This runs at startup and is surfaced on /api/v1/meta so a broken
        rule is loud instead of invisible.
        """
        problems: List[Dict[str, Any]] = []
        service_keys = set()
        rows = store.service_summary(window_minutes=5)
        for r in rows:
            service_keys |= set(r.keys())
        global_keys = set(self._global_metrics(store, 5).keys()) | {"costIncreasePct"}

        for rule in self._rules.values():
            known = service_keys if rule.scope == SCOPE_SERVICE else global_keys
            # An empty store yields no keys; only flag when we have a sample.
            if known and rule.metric not in known:
                problems.append({
                    "ruleId": rule.id, "metric": rule.metric,
                    "scope": rule.scope,
                    "problem": "metric name not produced by the store; this "
                               "rule can never fire",
                    "availableMetrics": sorted(known),
                })
        return problems

    @staticmethod
    def _breached(rule: Rule, observed: float) -> bool:
        if rule.comparator == "gt":
            return observed > rule.threshold
        return observed < rule.threshold


ruleset = RuleSet()
