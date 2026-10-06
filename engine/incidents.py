"""Incident lifecycle: breach -> incident -> timeline -> evidence -> impact.

An incident is a first-class object, not a red banner. Three design decisions
matter here:

1. ONE INCIDENT PER SCOPE, MANY RULES. A cascade trips the 5xx rule and the
   latency rule on the same service within seconds. Creating two incidents
   would be technically true and operationally useless, so rules attach to the
   existing incident for their scope and the timeline records when each one
   joined.

2. ROOT CAUSE FROM DEPENDENCY CITATIONS, NOT GUESSWORK. When orders fails, its
   error logs carry `dependency: cognikart-payments`. Counting those citations
   across the incident window identifies the suspected root cause
   deterministically -- no heuristics about service naming, no ML.

3. EVIDENCE IS BUILT BY THIS MODULE, NOT BY THE AI. The bundle below is
   assembled from the store with plain arithmetic. The Gemini explainer
   receives it and may only narrate it (see ai/explain.py). This is the
   boundary that keeps the AI from inventing numbers.
"""
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional

from ..config import settings
from . import cost as cost_engine
from .rules import Breach, SCOPE_GLOBAL

STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"

_SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


class Incident:
    def __init__(self, breach: Breach) -> None:
        self.id = "inc_" + uuid.uuid4().hex[:10]
        self.scopeKey = breach.scopeKey
        self.isGlobal = breach.scopeKey == "__all__"
        self.service = None if self.isGlobal else breach.scopeKey
        self.category = breach.rule.category
        self.severity = breach.rule.severity
        self.status = STATUS_OPEN
        self.startedAt = time.time()
        self.lastSeenAt = self.startedAt
        self.resolvedAt: Optional[float] = None
        self.episode = 1          # which firing this is for its scope
        self.triggers: Dict[str, Dict[str, Any]] = {}
        self.timeline: List[Dict[str, Any]] = []
        self.peakObserved: Dict[str, float] = {}
        self.clearStreak = 0
        # The breach was decided over the rule's window, which ends at
        # detection. Evidence (matching entries, before/during, impact) starts
        # there too: starting at detection leaves it empty for the first
        # minute and makes a fresh incident look like it cost nothing.
        self.evidenceFrom = self.startedAt - 60.0 * (breach.windowMinutes or 1)
        self._add_trigger(breach, first=True)

    # --- lifecycle ---------------------------------------------------------
    def _add_trigger(self, breach: Breach, first: bool = False) -> None:
        rid = breach.rule.id
        known = rid in self.triggers
        self.triggers[rid] = {
            "ruleId": rid,
            "ruleName": breach.rule.name,
            "metric": breach.rule.metric,
            "comparator": breach.rule.comparator,
            "threshold": breach.threshold,
            "unit": breach.rule.unit,
            "observed": breach.observed,
            "windowMinutes": breach.windowMinutes,
            "source": breach.rule.source,
            "category": breach.rule.category,
            "firstSeenAt": self.triggers.get(rid, {}).get("firstSeenAt", time.time()),
            "lastSeenAt": time.time(),
            "evidence": breach.evidence,
        }
        prev = self.peakObserved.get(breach.rule.metric)
        worse = (prev is None or
                 (breach.rule.comparator == "gt" and breach.observed > prev) or
                 (breach.rule.comparator == "lt" and breach.observed < prev))
        if worse:
            self.peakObserved[breach.rule.metric] = breach.observed

        if _SEVERITY_ORDER.get(breach.rule.severity, 0) > _SEVERITY_ORDER.get(self.severity, 0):
            self.severity = breach.rule.severity

        if not known:
            self.add_event(
                "detected" if first else "escalated",
                "%s breached: %s %s %s%s (observed %s%s)" % (
                    breach.rule.name, breach.rule.metric,
                    ">" if breach.rule.comparator == "gt" else "<",
                    _fmt(breach.threshold), breach.rule.unit,
                    _fmt(breach.observed), breach.rule.unit,
                ),
                source=breach.rule.source,
            )

    def add_event(self, kind: str, text: str, source: str = "platform") -> None:
        self.timeline.append({
            "ts": time.time(), "kind": kind, "text": text, "source": source,
        })
        # Bounded: an incident left open for hours must not grow without limit.
        if len(self.timeline) > 200:
            self.timeline = self.timeline[-200:]

    def touch(self, breach: Breach) -> None:
        self.lastSeenAt = time.time()
        self.clearStreak = 0
        self._add_trigger(breach)

    def mark_clear(self, required: int) -> bool:
        """Returns True when the incident has just resolved."""
        if self.status != STATUS_OPEN:
            return False
        self.clearStreak += 1
        if self.clearStreak >= required:
            self.status = STATUS_RESOLVED
            self.resolvedAt = time.time()
            self.add_event(
                "resolved",
                "all breached conditions clear for %d consecutive evaluations"
                % required,
            )
            return True
        return False

    # --- serialisation -----------------------------------------------------
    def to_dict(self, store=None) -> Dict[str, Any]:
        duration = (self.resolvedAt or time.time()) - self.startedAt
        d = {
            "id": self.id,
            "status": self.status,
            "severity": self.severity,
            "category": self.category,
            "scope": self.scopeKey,
            "service": self.service,
            "isGlobal": self.isGlobal,
            "startedAt": self.startedAt,
            "lastSeenAt": self.lastSeenAt,
            "resolvedAt": self.resolvedAt,
            "durationS": round(duration, 1),
            "episode": self.episode,
            "title": self.title(),
            "summary": self.summary(),
            "triggers": list(self.triggers.values()),
            "triggerCount": len(self.triggers),
            "peakObserved": {k: round(v, 4) for k, v in self.peakObserved.items()},
            "timeline": self.timeline,
        }
        if store is not None:
            d["impact"] = self.impact(store)
        return d

    def summary(self) -> str:
        """One line a reader can act on: what crossed, by how much.

        The title names the condition; this names the number. Together they
        answer "what is wrong" without opening anything.
        """
        if not self.triggers:
            return "A threshold was crossed."
        t = max(self.triggers.values(),
                key=lambda x: _SEVERITY_ORDER.get(self.severity, 0))
        unit = t.get("unit") or ""
        comparator = "exceeds" if t.get("comparator") == "gt" else "is below"
        observed, threshold = t.get("observed"), t.get("threshold")
        if unit == "ratio":
            return "%s is %.1f%%, which %s the %.0f%% threshold" % (
                t.get("ruleName", "The metric"), (observed or 0) * 100,
                comparator, (threshold or 0) * 100)
        return "%s of %s%s %s the %s%s threshold" % (
            t.get("ruleName", "The metric"), _fmt(observed), unit,
            comparator, _fmt(threshold), unit)

    def title(self) -> str:
        names = [t["ruleName"] for t in self.triggers.values()]
        where = self.service or "platform-wide"
        if len(names) == 1:
            return "%s on %s" % (names[0], where)
        return "%s and %d more condition(s) on %s" % (names[0], len(names) - 1, where)

    def impact(self, store) -> Dict[str, Any]:
        """Cost and business impact. Both modeled, both labelled."""
        cost = cost_engine.incident_delta(store, self.evidenceFrom)
        window = max(1, int((time.time() - self.evidenceFrom) / 60.0) + 1)
        buckets = store.series(window_minutes=min(window, settings.minute_buckets))
        since = [b for b in buckets if b["ts"] >= self.evidenceFrom - 60]
        revenue_failed = round(sum(b.get("revenueFailedInr", 0.0) for b in since), 2)
        confirmed = sum(b.get("checkoutsConfirmed", 0) for b in since)
        failed = sum(b.get("checkoutsFailed", 0) for b in since)
        settled = confirmed + failed
        return {
            "cost": cost,
            "business": {
                "kind": "measured",
                "revenueAtRiskInr": revenue_failed,
                "failedCheckouts": failed,
                "confirmedCheckouts": confirmed,
                "checkoutSuccessRatePct": round(100.0 * confirmed / settled, 1)
                if settled else None,
                "basis": "Sum of cartValueInr on checkout.failed events since "
                         "the incident began. Measured from logs, not modeled.",
            },
        }


def _fmt(v: float) -> str:
    if v is None:
        return "?"
    if abs(v) < 1 and v != 0:
        return "%.3f" % v
    if abs(v - round(v)) < 1e-9:
        return "%d" % round(v)
    return "%.2f" % v


class IncidentManager:
    def __init__(self) -> None:
        self._open: Dict[str, Incident] = {}        # scopeKey -> incident
        self._all: Dict[str, Incident] = {}         # id -> incident
        self._history: List[str] = []
        # A threshold crossed four times in an hour is one thing to fix, not
        # four things to read. Episodes are counted per scope so the UI can
        # group them and say so.
        self._episodes: Dict[str, int] = {}

    def ingest(self, breaches: List[Breach], clear_required: int = 3) -> Dict[str, Any]:
        seen_scopes = set()
        opened: List[str] = []
        for b in breaches:
            scope = b.scopeKey if b.rule.scope == SCOPE_GLOBAL else b.scopeKey
            # Global rules of different categories should not merge into one
            # incident -- a cost anomaly and a checkout-rate collapse are
            # different problems even though both are platform-wide.
            if b.rule.scope == SCOPE_GLOBAL:
                scope = "__all__::%s" % b.rule.category
            seen_scopes.add(scope)
            inc = self._open.get(scope)
            if inc is None:
                inc = Incident(b)
                inc.scopeKey = scope
                inc.isGlobal = scope.startswith("__all__")
                inc.service = None if inc.isGlobal else b.scopeKey
                self._episodes[scope] = self._episodes.get(scope, 0) + 1
                inc.episode = self._episodes[scope]
                self._open[scope] = inc
                self._all[inc.id] = inc
                self._history.append(inc.id)
                if len(self._history) > 300:
                    old = self._history.pop(0)
                    self._all.pop(old, None)
                opened.append(inc.id)
            else:
                inc.touch(b)

        resolved: List[str] = []
        for scope, inc in list(self._open.items()):
            if scope in seen_scopes:
                continue
            if inc.mark_clear(clear_required):
                resolved.append(inc.id)
                self._open.pop(scope, None)

        return {"opened": opened, "resolved": resolved,
                "openCount": len(self._open)}

    # --- queries -----------------------------------------------------------
    def open_incidents(self) -> List[Incident]:
        return sorted(self._open.values(), key=lambda i: i.startedAt, reverse=True)

    def all_incidents(self, limit: int = 50) -> List[Incident]:
        items = [self._all[i] for i in reversed(self._history) if i in self._all]
        return items[:limit]

    def get(self, incident_id: str) -> Optional[Incident]:
        return self._all.get(incident_id)

    def grouped(self, store, window_minutes: int = 360) -> Dict[str, Any]:
        """Incidents rolled up by scope, split into breaching and resolved.

        The page a responder actually wants answers three questions in order:
        what is wrong right now, what stopped by itself, and how often has this
        been happening. Listing every episode separately answers none of them.
        """
        cutoff = time.time() - window_minutes * 60
        by_scope: Dict[str, List[Incident]] = {}
        for inc in self._all.values():
            if inc.lastSeenAt < cutoff and inc.status != STATUS_OPEN:
                continue
            by_scope.setdefault(inc.scopeKey, []).append(inc)

        breaching: List[Dict[str, Any]] = []
        resolved: List[Dict[str, Any]] = []
        total_episodes = 0

        for scope, incs in by_scope.items():
            incs.sort(key=lambda i: i.startedAt)
            latest = incs[-1]
            firing = any(i.status == STATUS_OPEN for i in incs)
            total_episodes += len(incs)

            # "Breaching for" is time actually spent outside the threshold;
            # "across" is the span from the first episode to the last. They
            # differ whenever a condition has flapped, and the difference is
            # the useful part.
            breaching_s = sum((i.resolvedAt or time.time()) - i.startedAt for i in incs)
            across_s = (latest.resolvedAt or time.time()) - incs[0].startedAt

            severity = max((i.severity for i in incs),
                           key=lambda s: _SEVERITY_ORDER.get(s, 0))
            rule_windows = [t.get("windowMinutes") for i in incs
                            for t in i.triggers.values() if t.get("windowMinutes")]

            # Where this group's failure starts, so the page can show several
            # alerts with one root cause as one problem.
            root = self.correlate(latest, store).get("suspectedRootCauseService")                 if firing else None
            group = {
                "key": scope,
                "rootCause": root,
                "title": latest.title(),
                "summary": latest.summary(),
                "severity": severity,
                "status": "BREACHING" if firing else "RESOLVED",
                "episodes": len(incs),
                "service": latest.service,
                "category": latest.category,
                "breachingForS": round(breaching_s, 1),
                "acrossS": round(across_s, 1),
                "matchingEntries": self._matching_entries(store, latest, incs[0].evidenceFrom),
                "ruleWindowMinutes": min(rule_windows) if rule_windows else None,
                "startedAt": incs[0].startedAt,
                "lastSeenAt": latest.lastSeenAt,
                "resolvedAt": latest.resolvedAt,
                "primaryIncidentId": latest.id,
                "incidentIds": [i.id for i in incs],
                "conditions": sorted({t.get("ruleName", "?")
                                      for i in incs for t in i.triggers.values()}),
                "sparkline": self._sparkline(store, incs),
            }
            (breaching if firing else resolved).append(group)

        breaching.sort(key=lambda g: (-_SEVERITY_ORDER.get(g["severity"], 0),
                                      -g["breachingForS"]))
        resolved.sort(key=lambda g: -(g["resolvedAt"] or 0))

        return {
            "windowMinutes": window_minutes,
            "stats": {
                "breachingNow": len(breaching),
                "resolvedInWindow": len(resolved),
                "totalEpisodes": total_episodes,
                "distinctIncidents": len(by_scope),
                "critical": sum(1 for g in breaching + resolved
                                if g["severity"] in ("CRITICAL", "HIGH")),
            },
            "breaching": breaching,
            "resolved": resolved,
            "openCount": len(self._open),
        }

    @staticmethod
    def _matching_entries(store, incident: Incident, since: float) -> int:
        """How many log entries the rule's condition actually matched.

        Counting the evidence, not the alerts: a threshold crossing backed by
        three entries and one backed by three hundred are different problems.
        """
        services = [incident.service] if incident.service else None
        page = store.logs(limit=4000, min_severity="WARNING",
                          services=services, since=since)
        return page["total"]

    @staticmethod
    def _sparkline(store, incs: List[Incident], buckets: int = 24) -> List[float]:
        """Error rate per minute over the group's span, normalised 0-1."""
        start = incs[0].startedAt
        rows = store.series(window_minutes=max(2, int((time.time() - start) / 60) + 2),
                            service=incs[-1].service)
        vals = [float(r.get("errors5xx", 0) or 0) for r in rows][-buckets:]
        if not vals:
            return []
        peak = max(vals) or 1.0
        return [round(v / peak, 3) for v in vals]

    def correlate(self, incident: Incident, store,
                  window_s: float = 180.0) -> Dict[str, Any]:
        """Relate concurrent incidents and name a suspected root cause.

        Root cause is whichever service is most often cited as the FAILING
        DEPENDENCY in error logs during the incident window. In the cascade,
        orders' errors cite cognikart-payments, so payments is named -- and
        orders is correctly described as downstream rather than at fault.
        """
        related = [
            i for i in self._all.values()
            if i.id != incident.id
            and abs(i.startedAt - incident.startedAt) <= window_s
        ]
        page = store.logs(limit=1500, min_severity="WARNING",
                          since=incident.evidenceFrom - 30)
        citations: Counter = Counter()
        cited_by: Dict[str, Counter] = {}     # service -> dependencies it blames
        failing_services: Counter = Counter()
        for e in page["entries"]:
            if e.get("dependency") and (e.get("errorCode") or
                                        (e.get("httpStatus") or 0) >= 500):
                citations[e["dependency"]] += 1
                cited_by.setdefault(e.get("service"), Counter())[e["dependency"]] += 1
            if (e.get("httpStatus") or 0) >= 500:
                failing_services[e.get("service")] += 1

        # In a chain gateway -> orders -> payments, gateway blames orders and
        # orders blames payments: the most-cited service can be a messenger.
        # Follow the blame until reaching a service whose own errors cite no
        # failing dependency -- that one is where the failure starts.
        suspected = citations.most_common(1)[0][0] if citations else None
        chain = [suspected] if suspected else []
        while suspected in cited_by and len(chain) < 8:
            nxt = cited_by[suspected].most_common(1)[0][0]
            if nxt in chain:
                break
            suspected = nxt
            chain.append(nxt)
        if not suspected and failing_services:
            suspected = failing_services.most_common(1)[0][0]

        return {
            "suspectedRootCauseService": suspected,
            "rootCauseBasis": (
                ("failing dependency named in error logs during the incident "
                 "window (%d citations)" % citations[suspected]
                 + ("; followed the blame %s, and %s's own errors blame no "
                    "other service" % (" -> ".join(chain), suspected)
                    if len(chain) > 1 else ""))
                if suspected and citations[suspected]
                else "most 5xx responses during the incident window"
            ) if suspected else None,
            "dependencyCitations": dict(citations.most_common(5)),
            "blameChain": chain,
            "errorsByService": dict(failing_services.most_common(5)),
            "relatedIncidentIds": [i.id for i in related],
            "relatedIncidents": [
                {"id": i.id, "title": i.title(), "service": i.service,
                 "status": i.status, "startedAt": i.startedAt,
                 "category": i.category}
                for i in sorted(related, key=lambda x: x.startedAt)
            ],
            "isLikelyDownstream": bool(
                suspected and incident.service and suspected != incident.service),
        }

    def evidence(self, incident: Incident, store) -> Dict[str, Any]:
        """The deterministic evidence bundle.

        This is the ONLY thing the AI explainer ever sees. Every number in it
        is computed here from the store; the model adds no data.
        """
        now = time.time()
        inc_minutes = max(1, int((now - incident.evidenceFrom) / 60.0) + 1)
        start_minute = int(incident.evidenceFrom // 60)

        all_buckets = store.series(window_minutes=settings.minute_buckets)
        before = [b for b in all_buckets
                  if start_minute - 15 <= b["minute"] < start_minute]
        # The minute still filling reads low; leave it out once a complete
        # minute exists, or per-minute rates come out understated.
        now_minute = int(now // 60)
        during = [b for b in all_buckets if b["minute"] >= start_minute]
        if any(b["minute"] < now_minute for b in during):
            during = [b for b in during if b["minute"] < now_minute]

        def agg(rows: List[Dict[str, Any]], key: str) -> float:
            return sum(r.get(key, 0) or 0 for r in rows)

        def avg(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
            vals = [r.get(key) for r in rows if r.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        before_min = max(len(before), 1)
        during_min = max(len(during), 1)

        correlation = self.correlate(incident, store)
        groups = store.error_groups(window_minutes=inc_minutes + 2, limit=6)
        if incident.service:
            scoped = [g for g in groups if g["service"] == incident.service]
            groups = scoped or groups

        sample_trace: List[Dict[str, Any]] = []
        for g in groups:
            if g.get("sampleTraces"):
                sample_trace = store.trace(g["sampleTraces"][0].split("/")[-1])
                if sample_trace:
                    break

        return {
            "incidentId": incident.id,
            "title": incident.title(),
            "status": incident.status,
            "severity": incident.severity,
            "category": incident.category,
            "affectedScope": incident.service or "platform-wide",
            "detectedAt": incident.startedAt,
            "incidentMinutes": round((now - incident.startedAt) / 60.0, 1),
            "triggers": list(incident.triggers.values()),
            "timeline": incident.timeline,
            "correlation": correlation,
            "metricsDelta": {
                "requestsPerMin": {
                    "before": round(agg(before, "requests") / before_min, 2),
                    "during": round(agg(during, "requests") / during_min, 2),
                },
                "errors5xxPerMin": {
                    "before": round(agg(before, "errors5xx") / before_min, 2),
                    "during": round(agg(during, "errors5xx") / during_min, 2),
                },
                "p95LatencyMs": {
                    "before": avg(before, "p95LatencyMs"),
                    "during": avg(during, "p95LatencyMs"),
                },
                "logMibPerMin": {
                    "before": round(agg(before, "logBytes") / before_min / 1048576.0, 4),
                    "during": round(agg(during, "logBytes") / during_min / 1048576.0, 4),
                },
                "retriesPerMin": {
                    "before": round(agg(before, "retries") / before_min, 2),
                    "during": round(agg(during, "retries") / during_min, 2),
                },
                "paymentAttemptsPerMin": {
                    "before": round(agg(before, "paymentAttempts") / before_min, 2),
                    "during": round(agg(during, "paymentAttempts") / during_min, 2),
                },
                "instanceCountMax": {
                    "before": max([r.get("instanceCount", 0) for r in before] or [0]),
                    "during": max([r.get("instanceCount", 0) for r in during] or [0]),
                },
            },
            "topErrors": [
                {"errorCode": g["errorCode"], "errorClass": g["errorClass"],
                 "service": g["service"], "route": g["route"],
                 "count": g["count"], "pattern": g["pattern"]}
                for g in groups
            ],
            "impact": incident.impact(store),
            "sampleTrace": [
                {"service": e.get("service"), "event": e.get("event"),
                 "severity": e.get("severity"), "route": e.get("route"),
                 "httpStatus": e.get("httpStatus"), "latencyMs": e.get("latencyMs"),
                 "errorCode": e.get("errorCode"), "retryCount": e.get("retryCount"),
                 "dependency": e.get("dependency"), "message": e.get("message")}
                for e in sample_trace[:14]
            ],
            "servicesObserved": store.service_summary(window_minutes=inc_minutes + 1),
            "provenance": {
                "dataSource": settings.data_source,
                "note": "All figures computed deterministically from telemetry "
                        "by engine/incidents.py. Cost figures are modeled from "
                        "published list prices; business figures are measured "
                        "from logs.",
            },
        }


manager = IncidentManager()
