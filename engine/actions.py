"""The ranked action queue: what someone should do right now.

A dashboard full of charts asks the reader to work out what matters. This
inverts that -- the top of the Overview is an ordered list of things to do,
above every chart, and each entry answers four questions:

    what is wrong        the finding, in one line
    why it matters       the consequence, not a restatement of the metric
    first step           a concrete next action, not "investigate"
    how you'll know      the signal that tells you it worked

Two ranking decisions are worth stating, because both are deliberate and both
differ from sorting by severity:

**Priority is not severity.** A CRITICAL that has already resolved ranks below
a MEDIUM that is still firing, because only one of them needs a person now.
Severity describes the condition; priority describes the urgency of acting.

**One action per rule, not per episode.** If a threshold has been crossed four
times in an hour that is one thing to fix, not four things to read. Listing it
four times is exactly the alert fatigue that makes people stop reading alerts.

Everything here is derived from findings the deterministic engines already
produced. This module ranks and phrases; it does not detect.
"""
import time
from typing import Any, Dict, List, Optional

_SEVERITY_WEIGHT = {"CRITICAL": 100.0, "HIGH": 70.0, "MEDIUM": 40.0, "LOW": 15.0}

# A resolved condition still deserves a place in the queue -- it may need a
# follow-up -- but nowhere near the top.
_RESOLVED_MULTIPLIER = 0.2
_STALE_AFTER_S = 900.0


def _action(*, key: str, kind: str, title: str, why: str, first_step: str,
            verify: str, severity: str, firing: bool, evidence: Dict[str, Any],
            link: Optional[Dict[str, str]] = None,
            started_at: Optional[float] = None,
            revenue_inr: float = 0.0, cost_per_hour: float = 0.0,
            saving_per_month: float = 0.0) -> Dict[str, Any]:
    weight = _SEVERITY_WEIGHT.get(severity, 20.0)
    if not firing:
        weight *= _RESOLVED_MULTIPLIER

    # Impact nudges priority but never dominates it: a cheap outage still
    # outranks an expensive inefficiency.
    if revenue_inr > 0:
        weight += min(25.0, revenue_inr / 20000.0 * 25.0)
    if cost_per_hour > 0:
        weight += min(15.0, cost_per_hour * 3.0)
    if saving_per_month > 0:
        weight += min(10.0, saving_per_month * 2.0)

    # A condition firing for a long time slowly rises: nobody has dealt with it.
    if firing and started_at:
        mins = (time.time() - started_at) / 60.0
        weight += min(10.0, mins / 6.0)

    return {
        "key": key, "kind": kind, "title": title, "whyItMatters": why,
        "firstStep": first_step, "howYouWillKnow": verify,
        "severity": severity, "firing": firing,
        "priority": round(weight, 1),
        "startedAt": started_at,
        "ageMinutes": round((time.time() - started_at) / 60.0, 1) if started_at else None,
        "evidence": evidence,
        "link": link,
        "impact": {
            "revenueAtRiskInr": round(revenue_inr, 2) if revenue_inr else None,
            "cloudCostPerHourUsd": round(cost_per_hour, 4) if cost_per_hour else None,
            "savingPerMonthUsd": round(saving_per_month, 4) if saving_per_month else None,
        },
    }


def build(store, *, incidents: List[Dict[str, Any]],
          recommendations: List[Dict[str, Any]],
          anomalies: List[Dict[str, Any]],
          free_tier: Dict[str, Any],
          limit: int = 8) -> Dict[str, Any]:
    """Assemble and rank the queue from findings the engines already produced."""
    actions: List[Dict[str, Any]] = []
    seen_rules: set = set()

    # --- incidents: one action per CAUSE, not per rule or per service -----
    # A cascade trips several rules across several services. Grouping by rule
    # already beat one-row-per-episode, but it still produced five near
    # identical rows carrying the same root cause and the same impact. One
    # fault is one thing to do.
    clusters: Dict[str, Dict[str, Any]] = {}
    for inc in incidents:
        corr = inc.get("correlation") or {}
        cause = corr.get("suspectedRootCauseService") or inc.get("service") or "platform"
        c = clusters.setdefault(cause, {
            "cause": cause, "incidents": [], "triggers": {}, "services": set(),
            "firing": False, "severity": "LOW", "startedAt": None,
            "revenue": 0.0, "cost": 0.0, "failedCheckouts": 0,
        })
        c["incidents"].append(inc)
        if inc.get("service"):
            c["services"].add(inc["service"])
        if inc.get("status") == "OPEN":
            c["firing"] = True
        if _SEVERITY_WEIGHT.get(inc.get("severity", "LOW"), 0) > \
           _SEVERITY_WEIGHT.get(c["severity"], 0):
            c["severity"] = inc.get("severity", "LOW")
        started = inc.get("startedAt")
        if started and (c["startedAt"] is None or started < c["startedAt"]):
            c["startedAt"] = started

        impact = inc.get("impact") or {}
        biz = impact.get("business") or {}
        cost = impact.get("cost") or {}
        # Impact is platform-wide, so take the largest rather than summing --
        # adding it across incidents would multiply the same money.
        c["revenue"] = max(c["revenue"], float(biz.get("revenueAtRiskInr") or 0))
        c["cost"] = max(c["cost"], float(cost.get("deltaUsdPerHour") or 0))
        c["failedCheckouts"] = max(c["failedCheckouts"],
                                   int(biz.get("failedCheckouts") or 0))
        for trig in inc.get("triggers", []):
            rid = trig.get("ruleId")
            if rid and rid not in c["triggers"]:
                c["triggers"][rid] = trig

    for cause, c in clusters.items():
        key = "incident::%s" % cause
        seen_rules.add(key)
        rules = list(c["triggers"].values())
        affected = sorted(c["services"])
        downstream = [s for s in affected if s != cause]

        rule_names = ", ".join(t.get("ruleName", "?") for t in rules[:3])
        if len(rules) > 3:
            rule_names += " and %d more" % (len(rules) - 3)

        if downstream and cause in affected:
            title = "%s is failing, and taking %d other service(s) with it" % (
                cause, len(downstream))
        elif downstream:
            title = "%s is the suspected cause of failures in %s" % (
                cause, ", ".join(downstream))
        else:
            title = "%s: %s" % (cause, rule_names)

        why_parts = []
        if c["revenue"]:
            why_parts.append("%s of checkout value has failed across %d attempts"
                             % (_inr(c["revenue"]), c["failedCheckouts"]))
        if downstream:
            why_parts.append("the services that alerted loudest (%s) are "
                             "downstream, so fixing them will not help"
                             % ", ".join(downstream))
        if not why_parts:
            why_parts.append("%d condition(s) are outside their thresholds" % len(rules))

        actions.append(_action(
            key=key, kind="incident", title=title,
            why=". ".join(p[0].upper() + p[1:] for p in why_parts) + ".",
            first_step="Open %s and check its own error rate and latency before "
                       "touching anything downstream." % cause,
            verify="%s clears, and the %d dependent condition(s) resolve without "
                   "separate intervention." % (rule_names, len(rules)),
            severity=c["severity"], firing=c["firing"],
            started_at=c["startedAt"],
            evidence={
                "suspectedRootCause": cause,
                "affectedServices": affected,
                "downstreamServices": downstream,
                "conditions": [
                    {"rule": t.get("ruleName"), "metric": t.get("metric"),
                     "observed": t.get("observed"), "threshold": t.get("threshold"),
                     "unit": t.get("unit"), "source": t.get("source")}
                    for t in rules],
                "incidentIds": [i.get("id") for i in c["incidents"]],
                "incidentCount": len(c["incidents"]),
            },
            link={"view": "incidents",
                  "incident": (c["incidents"][0].get("id") if c["incidents"] else "")},
            revenue_inr=c["revenue"], cost_per_hour=c["cost"],
        ))

    # --- anomalies: real movement no fixed threshold would catch -----------
    for a in anomalies:
        key = "anomaly::%s" % a["metric"]
        if key in seen_rules:
            continue
        seen_rules.add(key)
        actions.append(_action(
            key=key, kind="anomaly",
            title="%s is %s %s%% against its own baseline"
                  % (a["label"], "up" if a["direction"] == "up" else "down",
                     abs(a["changePct"]) if a.get("changePct") is not None else "?"),
            why=a["whyItMatters"],
            first_step="Open Logs for this window and compare against the "
                       "preceding %d minutes." % a.get("baselineMinutes", 30),
            verify="The series returns to roughly %s %s."
                   % (_num(a["baselineMedian"]), a["unit"]),
            severity=a.get("severity", "MEDIUM"), firing=True,
            evidence={"zScore": a["zScore"], "baselineMedian": a["baselineMedian"],
                      "current": a["current"], "detail": a["evidence"]},
            link={"view": "insights"},
        ))

    # --- free-tier pressure: one action, not one per allotment -------------
    # Three Cloud Run allotments crossing at once is one fact about traffic
    # volume, not three things to do. Listing them separately pushed the actual
    # incident down the page.
    over = [l for l in free_tier.get("lines", [])
            if (l.get("pctOfFreeTier") or 0) > 100.0]
    if over:
        worst = max(over, key=lambda l: l["pctOfFreeTier"])
        names = ", ".join(l["resource"] for l in over)
        actions.append(_action(
            key="freetier", kind="cost",
            title="%s project%s past the free allotment"
                  % (names, "" if len(over) == 1 else " "),
            why="Crossing a free allotment is where a project that costs "
                "nothing starts costing something. The projection assumes the "
                "current rate continues around the clock, which it will not if "
                "traffic is only generated in sessions -- so treat this as a "
                "signal about the load generator before anything else.",
            first_step="Check the load generator is not still running, then "
                       "re-read this panel once traffic has settled.",
            verify="Every line in the free-tier panel falls back under 100%.",
            severity="MEDIUM" if worst["pctOfFreeTier"] < 300 else "HIGH",
            firing=True,
            evidence={"overAllotment": over,
                      "tightest": worst["resource"],
                      "worstPct": worst["pctOfFreeTier"]},
            link={"view": "cost"},
        ))
        seen_rules.add("freetier")

    # --- recommendations: standing inefficiencies -------------------------
    for rec in recommendations[:6]:
        # Free-tier pressure is already raised above, directly from the cost
        # engine. The recommendation engine reports the same finding, and
        # listing it twice is precisely the duplication this queue exists to
        # avoid.
        if rec.get("ruleId") == "free-tier-pressure":
            continue
        key = "rec::%s" % rec["id"]
        if key in seen_rules:
            continue
        seen_rules.add(key)
        saving = rec.get("estimatedSavingUsdPerMonth")
        actions.append(_action(
            key=key, kind="optimization", title=rec["title"],
            why=rec["rationale"],
            first_step=rec["suggestedAction"],
            verify="%s moves away from %s%s in this panel."
                   % (rec["observedMetric"], rec["observedValue"], rec["unit"]),
            severity=rec.get("severity", "MEDIUM"), firing=False,
            evidence={"observedMetric": rec["observedMetric"],
                      "observedValue": rec["observedValue"],
                      "confidence": rec["confidence"],
                      "savingStatus": rec["savingStatus"]},
            link={"view": "cost"},
            saving_per_month=float(saving or 0),
        ))

    actions.sort(key=lambda a: a["priority"], reverse=True)
    firing_now = [a for a in actions if a["firing"]]

    return {
        "actions": actions[:limit],
        "total": len(actions),
        "firingNow": len(firing_now),
        "topPriority": actions[0]["title"] if actions else None,
        "ranking": "Priority is not severity: a resolved CRITICAL ranks below a "
                   "firing MEDIUM, because only one of them needs someone now. "
                   "Business and cost impact nudge the order; a long-unaddressed "
                   "condition slowly rises. One action per rule, not per episode.",
    }


def _num(v: Any) -> str:
    if v is None:
        return "?"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return "%d" % round(f) if abs(f - round(f)) < 1e-9 else "%.2f" % f


def _inr(v: Any) -> str:
    try:
        return "INR %s" % format(int(float(v)), ",")
    except (TypeError, ValueError):
        return "INR ?"


def _mins(seconds: Any) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "a while"
    if s < 90:
        return "%d seconds" % round(s)
    return "%d minutes" % round(s / 60.0)
