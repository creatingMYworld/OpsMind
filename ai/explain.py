"""Grounded incident explanation. Narration only -- never a data source.

The rules this enforces (rules.md section 7, prd.md goal 7):

1. The model never queries anything. engine/incidents.evidence() builds the
   bundle deterministically and that bundle is the model's entire world.
2. The model may not introduce a number. After generation we extract every
   numeric token from the response and check it against the numbers present in
   the evidence. Unsupported figures are reported in `unsupportedNumbers` and
   the response is marked `grounded: false` so the UI can flag it. This is a
   cheap, mechanical check -- and it is the difference between "we told it not
   to hallucinate" and "we verify that it did not".
3. If Gemini is disabled, unavailable, slow or errors, a deterministic
   template narrative is returned instead, built from the same bundle. The
   demo cannot break because an API call failed.
4. Explanations are cached per (incidentId, triggerCount) so a judge clicking
   twice gets the same answer instantly, and so rehearsal pre-warms the cache.

Cost at demo scale: a ~4k token bundle with a ~400 token answer on Gemini 2.5
Flash is well under one US cent per explanation.
"""
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import settings

_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()
_client = None
_client_error: Optional[str] = None

SYSTEM_INSTRUCTION = """You are an SRE assistant embedded in a cloud \
observability platform. You will receive a JSON evidence bundle about one \
production incident.

Hard rules:
- Use ONLY facts present in the evidence bundle. Never introduce a number, \
service name, error code or metric that is not in the bundle.
- Never state a root cause as certain. Say what the evidence supports and \
name the strongest competing explanation if one exists.
- Quote specific evidence when you make a claim (metric name and value).
- If the evidence is insufficient for some part of the answer, say so plainly.
- Be concise and operational. An on-call engineer is reading this at 3am.

Answer with these five headings exactly, each 1-3 sentences:
WHAT HAPPENED
EVIDENCE
LIKELY CAUSE
IMPACT
WHAT TO CHECK NEXT"""


def _get_client() -> Tuple[Optional[Any], Optional[str]]:
    """Lazily construct the Vertex AI client. Import is deferred so the
    platform runs with google-genai absent or AI disabled."""
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client, _client_error
    if not settings.ai_enabled:
        _client_error = "AI_ENABLED is false"
        return None, _client_error
    if not settings.project_id:
        _client_error = "GOOGLE_CLOUD_PROJECT is not set"
        return None, _client_error
    try:
        from google import genai
        _client = genai.Client(
            vertexai=True,
            project=settings.project_id,
            location=settings.ai_location,
        )
        return _client, None
    except Exception as exc:  # noqa: BLE001
        _client_error = "%s: %s" % (type(exc).__name__, exc)
        return None, _client_error


# --- numeric grounding check ---------------------------------------------
_NUM_TOKEN = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers_in(obj: Any, acc: Optional[Set[str]] = None) -> Set[str]:
    """Every numeric value appearing anywhere in the evidence, as strings."""
    if acc is None:
        acc = set()
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc.add(_canon(obj))
        return acc
    if isinstance(obj, str):
        for m in _NUM_TOKEN.findall(obj):
            acc.add(_canon(m))
        return acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            _numbers_in(k, acc)
            _numbers_in(v, acc)
        return acc
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, acc)
        return acc
    return acc


def _canon(value: Any) -> str:
    """Canonical numeric string, so 4.0 / 4 / 4.00 compare equal."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return "%.4f" % f


def _derived_allowances(evidence: Dict[str, Any]) -> Set[str]:
    """Numbers a correct answer may legitimately derive from the evidence:
    rounded forms, simple ratios and percentage changes between before/during
    pairs. Without this the guard would flag honest arithmetic."""
    allowed: Set[str] = set()
    delta = evidence.get("metricsDelta") or {}
    for pair in delta.values():
        if not isinstance(pair, dict):
            continue
        b, d = pair.get("before"), pair.get("during")
        try:
            b = float(b)
            d = float(d)
        except (TypeError, ValueError):
            continue
        for v in (d - b, b - d):
            allowed.add(_canon(v))
            allowed.add(_canon(round(v)))
        if b:
            for v in (d / b, 100.0 * (d - b) / b, 100.0 * d / b):
                allowed.add(_canon(v))
                allowed.add(_canon(round(v)))
                allowed.add(_canon(round(v, 1)))
    # Rounded forms of every literal in the bundle.
    for s in list(_numbers_in(evidence)):
        try:
            f = float(s)
        except ValueError:
            continue
        for v in (round(f), round(f, 1), round(f, 2)):
            allowed.add(_canon(v))
    # Small integers are structural (headings, counts of list items, "1-3").
    for i in range(0, 101):
        allowed.add(str(i))
    return allowed


def check_grounding(text: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    supported = _numbers_in(evidence) | _derived_allowances(evidence)
    unsupported: List[str] = []
    for tok in _NUM_TOKEN.findall(text or ""):
        if _canon(tok) not in supported:
            unsupported.append(tok)
    # De-duplicate, preserve order.
    seen: Set[str] = set()
    uniq = [t for t in unsupported if not (t in seen or seen.add(t))]
    return {
        "grounded": not uniq,
        "unsupportedNumbers": uniq[:12],
        "checkedNumbers": len(_NUM_TOKEN.findall(text or "")),
        "method": "every numeric token in the response is matched against the "
                  "numbers present in (or simply derivable from) the evidence "
                  "bundle",
    }


# --- deterministic fallback ----------------------------------------------
def deterministic_narrative(evidence: Dict[str, Any]) -> str:
    d = evidence.get("metricsDelta") or {}
    corr = evidence.get("correlation") or {}
    impact = evidence.get("impact") or {}
    cost = (impact.get("cost") or {})
    biz = (impact.get("business") or {})
    triggers = evidence.get("triggers") or []
    errors = evidence.get("topErrors") or []

    def pair(name: str) -> Tuple[Optional[float], Optional[float]]:
        p = d.get(name) or {}
        return p.get("before"), p.get("during")

    e_before, e_during = pair("errors5xxPerMin")
    l_before, l_during = pair("p95LatencyMs")
    lv_before, lv_during = pair("logMibPerMin")
    r_before, r_during = pair("retriesPerMin")

    trig = ", ".join("%s (observed %s%s against threshold %s%s)" % (
        t["ruleName"], _n(t.get("observed")), t.get("unit", ""),
        _n(t.get("threshold")), t.get("unit", "")) for t in triggers[:3])

    lines = []
    lines.append("WHAT HAPPENED")
    lines.append(
        "%s was raised on %s. Triggered conditions: %s."
        % (evidence.get("title", "An incident"),
           evidence.get("affectedScope", "the platform"), trig or "n/a"))
    lines.append("")
    lines.append("EVIDENCE")
    ev = []
    if e_before is not None and e_during is not None:
        ev.append("5xx per minute moved from %s to %s" % (_n(e_before), _n(e_during)))
    if l_before is not None and l_during is not None:
        ev.append("p95 latency moved from %sms to %sms" % (_n(l_before), _n(l_during)))
    if r_before is not None and r_during is not None and (r_during or 0) > 0:
        ev.append("retries per minute moved from %s to %s" % (_n(r_before), _n(r_during)))
    if lv_before is not None and lv_during is not None:
        ev.append("log volume moved from %s to %s MiB/min" % (_n(lv_before), _n(lv_during)))
    if errors:
        ev.append("the most frequent error is %s on %s (%s occurrences)"
                  % (errors[0]["errorCode"], errors[0]["service"], _n(errors[0]["count"])))
    lines.append(("; ".join(ev) + ".") if ev else "Insufficient comparative data.")
    lines.append("")
    lines.append("LIKELY CAUSE")
    if corr.get("suspectedRootCauseService"):
        lines.append(
            "Evidence points to %s as the originating service, identified as "
            "the %s. %s"
            % (corr["suspectedRootCauseService"],
               corr.get("rootCauseBasis", "most-cited failing dependency"),
               "The affected service appears to be downstream of it rather "
               "than at fault itself."
               if corr.get("isLikelyDownstream") else ""))
    else:
        lines.append("No single originating service is identifiable from the "
                     "dependency citations in this window.")
    lines.append("")
    lines.append("IMPACT")
    imp = []
    if cost.get("deltaUsdPerHour") is not None:
        imp.append("modeled cloud spend is %s USD/hour above the pre-incident "
                   "baseline of %s USD/hour"
                   % (_n(cost["deltaUsdPerHour"]), _n(cost.get("baselineUsdPerHour"))))
        if cost.get("dominantDriver"):
            imp.append("the largest contributor is %s" % cost["dominantDriver"])
    if biz.get("revenueAtRiskInr"):
        imp.append("INR %s of checkout value has failed across %s checkouts"
                   % (_n(biz["revenueAtRiskInr"]), _n(biz.get("failedCheckouts"))))
    if biz.get("checkoutSuccessRatePct") is not None:
        imp.append("checkout success rate is %s percent"
                   % _n(biz["checkoutSuccessRatePct"]))
    lines.append(("; ".join(imp) + ".") if imp else "No measurable impact recorded yet.")
    lines.append("")
    lines.append("WHAT TO CHECK NEXT")
    nxt = []
    if corr.get("suspectedRootCauseService"):
        nxt.append("inspect %s directly rather than the service that alerted"
                   % corr["suspectedRootCauseService"])
    if r_during and r_during > (r_before or 0):
        nxt.append("confirm whether retry behaviour is amplifying load on the "
                   "failing dependency")
    if errors:
        nxt.append("open the %s error group and follow one sample trace end to end"
                   % errors[0]["errorCode"])
    nxt.append("compare against the Optimization Center for any standing "
               "recommendation on the affected service")
    lines.append("; ".join(nxt) + ".")
    return "\n".join(lines)


def _n(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return "%d" % round(f)
    if abs(f) < 0.01:
        return "%.4f" % f
    return "%.2f" % f


# --- public entry point ---------------------------------------------------
def explain(evidence: Dict[str, Any], *, force_refresh: bool = False) -> Dict[str, Any]:
    cache_key = "%s::%d" % (evidence.get("incidentId", "?"),
                            len(evidence.get("triggers") or []))
    if not force_refresh:
        with _cache_lock:
            hit = _cache.get(cache_key)
        if hit:
            out = dict(hit)
            out["cached"] = True
            return out

    started = time.time()
    fallback = deterministic_narrative(evidence)
    client, err = _get_client()

    if client is None:
        result = {
            "provider": "deterministic",
            "model": None,
            "narrative": fallback,
            "grounding": check_grounding(fallback, evidence),
            "fallbackUsed": True,
            "fallbackReason": err or "AI disabled",
            "latencyMs": int((time.time() - started) * 1000),
            "cached": False,
            "note": "Deterministic template narrative generated from the same "
                    "evidence bundle. Enable Gemini with AI_ENABLED=true.",
        }
        with _cache_lock:
            _cache[cache_key] = result
        return result

    try:
        from google.genai import types as gt

        prompt = (
            "Explain this incident using only the evidence below.\n\n"
            "```json\n%s\n```" % json.dumps(_trim(evidence), indent=1, default=str)
        )
        resp = client.models.generate_content(
            model=settings.ai_model,
            contents=prompt,
            config=gt.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2,
                max_output_tokens=700,
            ),
        )
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            raise RuntimeError("empty response from model")
        grounding = check_grounding(text, evidence)
        usage = getattr(resp, "usage_metadata", None)
        result = {
            "provider": "vertex-ai",
            "model": settings.ai_model,
            "narrative": text,
            "grounding": grounding,
            "fallbackUsed": False,
            "fallbackReason": None,
            "latencyMs": int((time.time() - started) * 1000),
            "cached": False,
            "tokens": {
                "input": getattr(usage, "prompt_token_count", None),
                "output": getattr(usage, "candidates_token_count", None),
            } if usage else None,
            "deterministicAlternative": fallback,
        }
    except Exception as exc:  # noqa: BLE001 - never let AI break the demo
        result = {
            "provider": "deterministic",
            "model": None,
            "narrative": fallback,
            "grounding": check_grounding(fallback, evidence),
            "fallbackUsed": True,
            "fallbackReason": "%s: %s" % (type(exc).__name__, exc),
            "latencyMs": int((time.time() - started) * 1000),
            "cached": False,
            "note": "Gemini call failed; deterministic narrative used instead.",
        }

    with _cache_lock:
        _cache[cache_key] = result
    return result


def _trim(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the prompt small and cheap: drop verbose, low-signal sections."""
    out = dict(evidence)
    out.pop("servicesObserved", None)
    out["timeline"] = (evidence.get("timeline") or [])[-10:]
    out["sampleTrace"] = (evidence.get("sampleTrace") or [])[:10]
    out["topErrors"] = (evidence.get("topErrors") or [])[:4]
    return out


def cache_stats() -> Dict[str, Any]:
    with _cache_lock:
        return {"cachedExplanations": len(_cache), "keys": list(_cache.keys())}


# --- cost summary ---------------------------------------------------------

COST_SYSTEM = """You are a FinOps assistant inside a cloud monitoring \
platform. You will receive a JSON bundle of modeled cloud spend and the \
optimization recommendations derived from it.

Hard rules:
- Use ONLY numbers present in the bundle. Never invent or extrapolate a figure.
- Three short sentences of plain prose. No headings, no bullet points, no
  markdown.
- Say where the money is going, which single action saves the most, and what
  to do first."""

_cost_cache: Dict[str, Dict[str, Any]] = {}
COST_CACHE_S = 120


def summarize_cost(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """A short written summary of the cost picture.

    Gemini when it is enabled and reachable, otherwise a deterministic
    sentence built from the same numbers. `ai` tells the UI which it got, so
    only genuinely generated text carries the AI tag.
    """
    key = json.dumps(ctx, sort_keys=True, default=str)
    hit = _cost_cache.get(key)
    if hit and time.time() - hit["at"] < COST_CACHE_S:
        return dict(hit["value"], cached=True)

    deterministic = _deterministic_cost_summary(ctx)
    client, err = _get_client()
    out: Dict[str, Any] = {
        "ai": False, "provider": "deterministic", "model": None,
        "summary": deterministic, "reason": err or "AI disabled",
    }
    if client is not None:
        try:
            from google.genai import types as gt
            resp = client.models.generate_content(
                model=settings.ai_model,
                contents="```json\n%s\n```" % json.dumps(ctx, indent=1, default=str),
                config=gt.GenerateContentConfig(
                    system_instruction=COST_SYSTEM,
                    temperature=0.2,
                    max_output_tokens=300,
                ),
            )
            text = (getattr(resp, "text", None) or "").strip()
            if text:
                out = {"ai": True, "provider": "vertex-ai",
                       "model": settings.ai_model, "summary": text,
                       "reason": None, "deterministicAlternative": deterministic}
        except Exception as exc:  # noqa: BLE001 - never let AI break the page
            out["reason"] = "%s: %s" % (type(exc).__name__, exc)

    out["cached"] = False
    _cost_cache[key] = {"at": time.time(), "value": out}
    return out


def _deterministic_cost_summary(ctx: Dict[str, Any]) -> str:
    cost = ctx.get("cost") or {}
    drivers = {k: (v or 0.0) for k, v in (cost.get("byDriver") or {}).items()}
    recs = ctx.get("recommendations") or []
    priced = [r for r in recs if r.get("estimatedSavingUsdPerMonth")]
    parts = ["Modeled spend is $%s/hour, about $%s a month at this rate."
             % (_n(cost.get("usdPerHour")), _n(cost.get("projectedUsdPerMonth")))]
    if drivers:
        top, val = max(drivers.items(), key=lambda kv: kv[1])
        total = sum(drivers.values()) or 1.0
        label = {"cpu": "CPU", "memory": "Memory", "requests": "Requests",
                 "logging": "Log ingestion"}.get(top, top)
        parts.append("%s is the largest driver at %.0f%% of the rate."
                     % (label, 100.0 * val / total))
    if priced:
        best = max(priced, key=lambda r: r["estimatedSavingUsdPerMonth"])
        parts.append("The largest calculated saving is $%s a month from \"%s\"."
                     % (_n(best["estimatedSavingUsdPerMonth"]), best.get("title", "")))
    elif recs:
        parts.append("%d recommendation(s) are open; none has a saving that can "
                     "be calculated from published prices yet." % len(recs))
    else:
        parts.append("No recommendation is open for this window.")
    return " ".join(parts)
