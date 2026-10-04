"""The product guide: a question-answerer about OpsMind itself.

Not a general chatbot. It answers one thing -- what this product is, what each
view shows, which Google Cloud services it uses and how -- and it answers from
a manifest **generated from the running application**, not a hand-written
description that drifts out of date the moment a view is renamed.

Two properties follow from that:

* It cannot describe a view that does not exist, because the view list comes
  from the app's own routes and nav.
* It reports the live configuration -- which project is being watched, whether
  the AI is enabled -- rather than what the configuration was when someone last
  edited a paragraph.

Like the incident explainer, a model only ever rewrites findings assembled
here. With no model configured the deterministic answer is returned instead,
which is a real answer rather than an apology: the manifest sections are
written to be read directly.
"""
import os
import re
import time
from typing import Any, Dict, List, Optional

from ..config import settings

SYSTEM_INSTRUCTION = """You explain a product called OpsMind to someone \
evaluating it. You will receive a JSON manifest describing exactly what it \
does, generated from the running application.

Hard rules:
- Answer ONLY from the manifest. Never invent a feature, view, metric, service \
or number that is not in it.
- If the manifest does not cover the question, say so plainly and name the \
closest thing it does cover.
- Be concrete and brief: three or four sentences, or a short list. The reader \
wants to know what the product does, not to be sold to.
- Never claim OpsMind can change or fix anything. It is read-only."""


_VIEW_NOTES = {
    "overview":
        "A ranked queue of what to do now above every chart, then health "
        "score, traffic and errors over time, modeled spend, a service "
        "table and the checkout funnel.",
    "incidents":
        "Threshold crossings grouped by cause rather than listed per "
        "episode, split into breaching now and resolved. Expanding one "
        "gives impact in both cloud cost and revenue at risk, a suspected "
        "root cause with the dependency citations behind it, top errors, "
        "a sample trace across services, and an explanation.",
    "insights":
        "Every log line clustered by message template, and series that "
        "have departed from their own recent baseline. A fixed threshold "
        "cannot say what is unusual for a particular service; this can.",
    "services":
        "Per-workload health: requests, server and client errors, latency "
        "percentiles, CPU, memory against what is provisioned, instance "
        "count and log volume.",
    "performance":
        "Latency by endpoint rather than by service: the slowest routes, "
        "their p95, their error rate, and how many requests crossed the "
        "slow threshold. Minute buckets keep only overall percentiles, so "
        "this is computed from individual requests.",
    "logs":
        "Streaming structured logs, filtered on the server by severity, "
        "service, status class, route, event and free text, so the count, "
        "the rows and the live stream always agree. A row opens the full "
        "entry and its trace across every service it touched. Choosing an "
        "error filter reveals deterministic error grouping in place, with "
        "server faults kept distinct from client mistakes.",
    "resources":
        "CPU, memory and instance counts, with the live tier from service "
        "heartbeats shown separately from the slower Cloud Monitoring tier.",
    "cost":
        "Modeled spend by driver, the free-tier position, the billed tier, "
        "and optimization recommendations carrying evidence, a confidence "
        "level and a copyable command.",
    "alerts":
        "Editable thresholds. This is where 'set alert thresholds' is "
        "implemented inside the product rather than in a console.",
    "setup":
        "What is feeding the dashboard: source, project, watched services, "
        "the three latency tiers and collector health.",
}

_NAV_RE = re.compile(r'data-view="([a-z-]+)"[^>]*>([^<]*)')


def _views() -> List[Dict[str, str]]:
    """What the dashboard actually offers, read from its own nav.

    The list of views is parsed out of index.html rather than written here, so
    a view that is renamed, added or merged away changes this answer without
    anyone remembering to edit it. That is the point: the guide must not be
    able to describe a view that is not in the product. It previously could --
    it described an Errors view for a while after that view was merged into
    Logs, and knew nothing about Performance.

    The descriptions are authored and keyed by view id. A view with no
    description is still listed and labelled as undescribed, because dropping
    it would be the same drift in a quieter form.
    """
    index = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "static", "index.html")
    try:
        with open(index, encoding="utf-8") as fh:
            html = fh.read()
    except OSError:
        # Without the page there is nothing to be authoritative about. Say so
        # rather than falling back to a list that may be wrong.
        return []

    views: List[Dict[str, str]] = []
    for vid, label in _NAV_RE.findall(html):
        views.append({
            "view": " ".join(label.split()) or vid,
            "id": vid,
            "shows": _VIEW_NOTES.get(
                vid, "In the dashboard; not yet described in this guide."),
        })
    return views


def _gcp_services() -> List[Dict[str, str]]:
    return [
        {"service": "Cloud Logging", "role":
            "The source of truth. Structured logs are read through the Logging "
            "API; nothing is installed in the application to make that happen.",
         "chargeable": "Querying your own logs is free. Ingestion is billed "
                       "beyond 50 GiB per project per month."},
        {"service": "Cloud Monitoring", "role":
            "Platform truth: Cloud Run CPU, memory, instance count and billable "
            "time.",
         "chargeable": "Reading Google Cloud system metrics is not chargeable."},
        {"service": "Cloud Run", "role":
            "Hosts both OpsMind and the application it watches. Scales to zero.",
         "chargeable": "Generous free tier; this deployment sits inside it."},
        {"service": "Cloud Resource Manager", "role":
            "Lists the real projects this installation can see, so the project "
            "picker is not a configured list.",
         "chargeable": "No charge."},
        {"service": "Cloud Build and Artifact Registry", "role":
            "Build the container from source and store the image.",
         "chargeable": "Free daily build allowance; the image is small."},
        {"service": "Vertex AI (Gemini)", "role":
            "Optional. Narrates evidence that the deterministic engines have "
            "already assembled. It never computes a number.",
         "chargeable": "Per token. One explanation costs well under a cent."},
    ]


def _honesty() -> List[str]:
    return [
        "Live cost is modeled -- measured usage multiplied by Google's "
        "published list prices -- and is labelled modeled, never billed.",
        "A savings figure appears only when it can be calculated from verified "
        "pricing. Otherwise the recommendation says 'potential optimization "
        "opportunity' and shows no number.",
        "Any explanation is checked: every numeric token in it is matched "
        "against the evidence it was given, and unsupported figures are "
        "flagged rather than printed quietly.",
        "OpsMind is read-only. It holds read-only IAM roles and exposes no "
        "endpoint that changes anything in your project.",
        "Error grouping and anomaly detection are arithmetic, not machine "
        "learning. The same window always produces the same answer.",
    ]


def manifest(store=None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything the guide is allowed to know, assembled from the live app."""
    from ..engine.rules import ruleset

    data: Dict[str, Any] = {
        "product": "OpsMind",
        "whatItIs":
            "A cloud log monitoring, resource visibility and cost optimization "
            "portal for Google Cloud. It reads Cloud Logging and Cloud "
            "Monitoring and turns them into incidents that carry a named cause, "
            "a cost and the revenue they put at risk.",
        "whyItExists":
            "Logs, metrics and cost already exist in Google Cloud, on three "
            "screens with three different time axes. Nothing joins them up, and "
            "no native surface connects an incident to what it costs or to the "
            "business it affected.",
        "views": _views(),
        "googleCloudServices": _gcp_services(),
        "latencyTiers": [
            {"tier": "LIVE", "latency": "1-5 seconds",
             "source": "structured logs and service heartbeats"},
            {"tier": "NEAR REAL TIME", "latency": "1-5 minutes",
             "source": "Cloud Monitoring system metrics"},
            {"tier": "AUTHORITATIVE", "latency": "hours to about a day",
             "source": "Cloud Billing export"},
        ],
        "alertRules": [
            {"name": r["name"], "category": r["category"],
             "threshold": r["threshold"], "unit": r["unit"],
             "why": r["rationale"]}
            for r in ruleset.list()
        ],
        "honestyGuarantees": _honesty(),
        "multiProject":
            "OpsMind watches one project at a time and lists the real projects "
            "it can see. Seeing a project and reading its logs are different "
            "permissions, so each project shows whether logs are actually "
            "readable, with the command that would grant access if not.",
        "configuration": {
            "dataSource": settings.data_source,
            "activeProject": settings.active_project or settings.project_id or None,
            "watchedServices": settings.watched_services,
            "aiEnabled": settings.ai_enabled,
            "aiModel": settings.ai_model if settings.ai_enabled else None,
            "pricingVerifiedOn": settings.pricing.get("verifiedOn"),
        },
    }
    if store is not None:
        try:
            s = store.stats()
            data["currentlyHolding"] = {
                "bufferedEntries": s["bufferedEntries"],
                "errorGroups": s["errorGroups"],
                "ingestedTotal": s["ingestedTotal"],
            }
        except Exception:
            pass
    if extra:
        data.update(extra)
    return data


# --- deterministic answering ---------------------------------------------
_STOP = frozenset("""a an and are as at be by can could do does doing done for from get
give got has have how in into is it its just like make makes making many much of on or
over run runs see show shows so some tell that the their them then there these this those
to use used uses using want what when where which who why will with work works would you
your me my our us explain about also any all more most need needs thing things""".split())

# Phrasings that clearly aim at one topic but share no words with it. A small
# explicit map is honest about what it is -- a lookup -- and beats pretending
# the term overlap alone understood the question.
_INTENT = {
    "honesty guarantees": ("refuse", "invent", "invented", "fabricate", "honest",
                           "honesty", "trust", "fake", "lie", "accurate",
                           "guarantee", "hallucinate", "credible"),
    "latency tiers": ("fresh", "freshness", "stale", "delay", "lag", "realtime",
                      "real-time", "tier", "tiers", "latency", "behind"),
    "multiple projects": ("project", "projects", "multi", "tenant", "switch",
                          "another", "other"),
    "current configuration": ("configured", "configuration", "setting",
                              "settings", "right now", "currently", "setup"),
    "alert thresholds": ("alert", "alerts", "threshold", "thresholds", "rule",
                         "rules", "notify", "notification"),
    "why it exists": ("problem", "gap", "point", "purpose", "instead",
                      "different", "better", "console"),
}


def _terms(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP]


def _sections(man: Dict[str, Any]) -> List[Dict[str, str]]:
    """The manifest flattened into answerable chunks."""
    out = [
        {"topic": "what OpsMind is", "body": man["whatItIs"]},
        {"topic": "why it exists", "body": man["whyItExists"]},
        {"topic": "multiple projects", "body": man["multiProject"]},
        {"topic": "latency tiers", "body": "; ".join(
            "%s (%s) from %s" % (t["tier"], t["latency"], t["source"])
            for t in man["latencyTiers"])},
        {"topic": "honesty guarantees", "body": " ".join(man["honestyGuarantees"])},
    ]
    for v in man["views"]:
        out.append({"topic": "the %s view" % v["view"], "body": v["shows"]})
    for s in man["googleCloudServices"]:
        out.append({"topic": "Google Cloud: %s" % s["service"],
                    "body": "%s %s" % (s["role"], s["chargeable"])})
    if man["alertRules"]:
        out.append({"topic": "alert thresholds", "body":
                    "There are %d editable thresholds: %s."
                    % (len(man["alertRules"]),
                       ", ".join(r["name"] for r in man["alertRules"]))})
    cfg = man["configuration"]
    out.append({"topic": "current configuration", "body":
                "Reading from %s, watching %d service(s)%s. AI narration is %s."
                % (cfg["dataSource"], len(cfg["watchedServices"] or []),
                   (", project %s" % cfg["activeProject"]) if cfg["activeProject"] else "",
                   "on" if cfg["aiEnabled"] else "off")})
    return out


def answer_deterministically(question: str, man: Dict[str, Any],
                             limit: int = 3) -> Dict[str, Any]:
    """Rank manifest sections by term overlap and return the best few.

    Not a fallback apology: the sections are written to be read directly, so
    this is a real answer even with no model configured.
    """
    terms = _terms(question)
    lowered = (question or "").lower()
    scored = []
    for sec in _sections(man):
        topic = sec["topic"].lower()
        hay = topic + " " + sec["body"].lower()

        # A hit in the topic is worth far more than one in the body: every
        # section mentions "logs" somewhere, only one is *about* them.
        score = sum(4 for t in terms if t in topic)
        score += sum(1 for t in terms if t in hay)
        for cue in _INTENT.get(sec["topic"], ()):
            if cue in lowered:
                score += 5

        # Require a distinctive word to have matched, not only short filler.
        # Without this, "Does it use Kubernetes?" scored on "use" and returned
        # three confident paragraphs about something else entirely.
        distinctive = [t for t in terms if len(t) >= 4 and t in hay]
        if score and (distinctive or any(c in lowered
                                         for cues in _INTENT.values() for c in cues)):
            scored.append((score, sec))
    scored.sort(key=lambda x: -x[0])

    # A weak best match is worse than admitting the question is not covered.
    if scored and scored[0][0] < 4:
        scored = []

    if not scored:
        topics = ", ".join(s["topic"] for s in _sections(man)[:8])
        return {
            "answer": "That is not something this guide covers. It can describe "
                      "what OpsMind is, what each view shows, which Google Cloud "
                      "services it uses and how it is configured right now. "
                      "Try: %s." % topics,
            "matched": [],
        }
    # Only keep runners-up that are genuinely close to the best match, so a
    # precise question gets one precise answer rather than three.
    best = scored[0][0]
    picked = [s for sc, s in scored[:limit] if sc >= best * 0.55]
    body = "\n\n".join("**%s** — %s" % (s["topic"], s["body"]) for s in picked)
    return {"answer": body, "matched": [s["topic"] for s in picked]}


# --- public entry point ---------------------------------------------------
def ask(question: str, store=None) -> Dict[str, Any]:
    started = time.time()
    question = (question or "").strip()
    man = manifest(store)

    if not question:
        return {"answer": "Ask what OpsMind does, what a particular view shows, "
                          "which Google Cloud services it uses, or how it is "
                          "configured right now.",
                "provider": "deterministic", "matched": [], "latencyMs": 0}

    fallback = answer_deterministically(question, man)

    if not settings.ai_enabled:
        return dict(fallback, provider="deterministic", grounded=True,
                    latencyMs=int((time.time() - started) * 1000),
                    note="Answered from the generated capability manifest. "
                         "Enable Vertex AI for a conversational rewrite of the "
                         "same material.")
    try:
        from google import genai
        from google.genai import types as gt
        import json as _json

        client = genai.Client(vertexai=True, project=settings.project_id,
                              location=settings.ai_location)
        resp = client.models.generate_content(
            model=settings.ai_model,
            contents="Question: %s\n\nManifest:\n```json\n%s\n```"
                     % (question, _json.dumps(man, indent=1, default=str)),
            config=gt.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2, max_output_tokens=420),
        )
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            raise RuntimeError("empty response")
        return {"answer": text, "provider": "vertex-ai", "model": settings.ai_model,
                "grounded": True, "matched": fallback["matched"],
                "latencyMs": int((time.time() - started) * 1000),
                "deterministicAlternative": fallback["answer"]}
    except Exception as exc:  # noqa: BLE001 -- the guide must always answer
        return dict(fallback, provider="deterministic", grounded=True,
                    latencyMs=int((time.time() - started) * 1000),
                    fallbackReason="%s: %s" % (type(exc).__name__, exc))


def suggestions() -> List[str]:
    return [
        "What is OpsMind?",
        "What does the Incidents view show?",
        "Which Google Cloud services does it use?",
        "How does it work out what an incident cost?",
        "Can it watch more than one project?",
        "What are the three latency tiers?",
        "What will it refuse to make up?",
    ]
