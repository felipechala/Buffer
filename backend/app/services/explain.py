"""Optional LLM explainer for a Buffer decision.

The decision itself is always computed by the deterministic engine. This module only rewrites the
already-computed facts into 2-3 plain-English sentences. Safety rails:

  * the model sees a compact JSON payload and is told to use ONLY its numbers and dates;
  * `ground()` extracts every $amount and date from the reply and rejects it if any is not in the payload;
  * missing key, CASHGUARD_EXPLAIN=0, any exception, timeout, refusal or grounding failure -> the
    deterministic template is returned (`source="template"`). Nothing here ever raises into decide().
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from .decision import Decision

log = logging.getLogger("cashguard.explain")

DEFAULT_MODEL = "claude-haiku-5-5"
TIMEOUT_S = 2.5
MAX_TOKENS = 400

SYSTEM_PROMPT = (
    "You explain a cash-management decision to a finance user. Write 2-3 short plain-English sentences. "
    "Use ONLY the dollar amounts and dates that appear in the JSON payload; never invent, round, sum or "
    "convert numbers or dates. Write dollar amounts like $40,000 and dates like Nov 22. If the decision is "
    "'allow', say it is safe. Otherwise say what breaks and why, then the cheapest fix. "
    "No preamble, no bullet points, no markdown."
)


class Explanation(BaseModel):
    text: str
    source: Literal["llm", "template"]
    model: str | None = None
    latency_ms: int = 0
    reason: str | None = None          # why the template was used (diagnostics only)


# ------------------------------------------------------------------ payload
def _usd(cents: int) -> float:
    return round(cents / 100, 2)


def build_payload(decision: "Decision") -> dict[str, Any]:
    """Compact, number-complete JSON the model may quote from."""
    return payload_from_record(decision.model_dump(mode="json", exclude={"impact"}))


def payload_from_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Same payload from a JSON-dumped Decision (e.g. the persisted AnalysisRun result)."""
    fixes = []
    for f in rec["fixes"][:3]:
        fixes.append({
            "kind": f["kind"], "pay_date": f["pay_date"], "delay_days": f["delay_days"],
            "cost_usd": _usd(f["cost_cents"]), "summary": f["summary"],
            "actions": [{k: (_usd(v) if k.endswith("_cents") else v)
                         for k, v in a.items()
                         if k in ("kind", "amount_cents", "from_account", "to_account", "account", "line_id",
                                  "dispatch_date", "date", "pay_date", "fee_cents")} for a in f["actions"]]})
    payload: dict[str, Any] = {
        "decision": rec["decision"],
        "payment": {"payee": rec["payee"], "amount_usd": _usd(rec["amount_cents"]),
                    "requested_date": rec["requested_date"]},
        "breaks": [{"account": b["account"], "obligation": b["obligation"], "date": b["date"],
                    "shortfall_usd": _usd(b["shortfall_cents"])} for b in rec["breaks"]],
        "scenario": rec["breaks"][0]["scenario"]["label"] if rec["breaks"] else None,
        "fixes": fixes,
        "binding_constraints": rec["constraint_report"][:5],
    }
    return payload


# ------------------------------------------------------------------ grounding
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_AMOUNT_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|thousand|million)?\b", re.IGNORECASE)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MD_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                    re.IGNORECASE)
_MULT = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}


def extract_amounts(text: str) -> list[float]:
    out = []
    for num, suffix in _AMOUNT_RE.findall(text):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(v * _MULT.get(suffix.lower(), 1))
    return out


def extract_dates(text: str) -> tuple[set[str], set[tuple[int, int]]]:
    """(ISO dates, (month, day) pairs) mentioned in the text."""
    isos = {m.group(0) for m in _ISO_RE.finditer(text)}
    mds = {(_MONTHS[m.group(1).lower()], int(m.group(2))) for m in _MD_RE.finditer(text)}
    return isos, mds


def _walk(obj: Any, amounts: set[float], isos: set[str], mds: set[tuple[int, int]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and (k.endswith("_usd") or k == "amount_usd"):
                amounts.add(float(v))
            else:
                _walk(v, amounts, isos, mds)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, amounts, isos, mds)
    elif isinstance(obj, str):
        isos.update(m.group(0) for m in _ISO_RE.finditer(obj))
        amounts.update(extract_amounts(obj))        # amounts quoted inside summaries / constraint text
        mds.update(extract_dates(obj)[1])


def ground(text: str, payload: dict[str, Any]) -> bool:
    """True iff every $amount (within $1) and every date in `text` appears in `payload`."""
    amounts: set[float] = set()
    isos: set[str] = set()
    mds: set[tuple[int, int]] = set()
    _walk(payload, amounts, isos, mds)
    for iso in isos:
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        mds.add((d.month, d.day))
    for v in extract_amounts(text):
        if not any(abs(v - a) <= 1.0 for a in amounts):
            return False
    t_isos, t_mds = extract_dates(text)
    if any(i not in isos for i in t_isos):
        return False
    # an ISO date in the text also yields no (month, day) match, so only check the textual forms
    return all(md in mds for md in t_mds)


# ------------------------------------------------------------------ llm call
_cache: dict[str, Explanation] = {}
_cache_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("CASHGUARD_EXPLAIN", "1") != "0" and bool(os.getenv("ANTHROPIC_API_KEY"))


_client: Any = None                       # one shared client: one import, one connection pool
_client_lock = threading.Lock()


def _build_client():
    import anthropic
    return anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=0)


def _shared_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = _build_client()
        return _client


def _get_client():  # separated so tests can inject a fake
    return _shared_client()


def prewarm() -> None:
    """Import anthropic and build the shared client on a daemon thread so the first explanation request
    does not pay the import + TLS setup cost. Sends no API request. No-op when the explainer is disabled.
    Never raises."""
    try:
        if not enabled():
            return
        threading.Thread(target=_warm, name="explain-prewarm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _warm() -> None:
    try:
        _shared_client()
    except Exception as exc:  # noqa: BLE001
        log.debug("explainer prewarm skipped (%s)", type(exc).__name__)


def _template(template: str, reason: str, started: float) -> Explanation:
    return Explanation(text=template, source="template", reason=reason,
                       latency_ms=int((time.perf_counter() - started) * 1000))


def explain(payload: dict[str, Any], template: str = "") -> Explanation:
    """One short completion, grounded; falls back to `template` on any problem. Never raises."""
    started = time.perf_counter()
    template = template or _fallback_text(payload)
    try:
        if os.getenv("CASHGUARD_EXPLAIN", "1") == "0":
            return _template(template, "disabled", started)
        if not os.getenv("ANTHROPIC_API_KEY"):
            return _template(template, "no_api_key", started)
        model = os.getenv("CASHGUARD_EXPLAIN_MODEL", DEFAULT_MODEL)
        client = _get_client()
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": json.dumps(payload, sort_keys=True)}])
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            return _template(template, "refusal", started)
        if stop != "end_turn":                      # max_tokens => cut-off text; never show it
            return _template(template, "truncated" if stop == "max_tokens" else f"stop_reason:{stop}", started)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        if not text:
            return _template(template, "empty", started)
        if not ground(text, payload):
            log.warning("explanation failed grounding; using template")
            return _template(template, "ungrounded", started)
        return Explanation(text=text, source="llm", model=model,
                           latency_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 - timeouts, API errors, import errors: never break a decision
        log.warning("explainer unavailable (%s); using template", type(exc).__name__)
        return _template(template, type(exc).__name__, started)


def _fallback_text(payload: dict[str, Any]) -> str:
    pay = payload.get("payment", {})
    return f"Decision for {pay.get('payee', 'payment')}: {payload.get('decision', 'review')}."


def _remember(digest: str, result: Explanation) -> None:
    """Only successful LLM explanations are cached. Template fallbacks (truncated, timeout, ungrounded,
    disabled) are never stored, so a bad reply cannot stick for the life of the process. The cache is
    in-memory only and is empty again after a restart."""
    if result.source == "llm":
        with _cache_lock:
            _cache[digest] = result


def explain_decision(decision: "Decision", template: str, digest: str) -> Explanation:
    """Cached (by input digest) entry point used by decide(). Never raises."""
    try:
        with _cache_lock:
            hit = _cache.get(digest)
        if hit is not None:
            return hit
        result = explain(build_payload(decision), template)
        _remember(digest, result)
        return result
    except Exception as exc:  # noqa: BLE001
        return Explanation(text=template, source="template", reason=type(exc).__name__)


def explain_record(rec: dict[str, Any]) -> Explanation:
    """Entry point for GET /v1/runs/{id}/explanation: `rec` is a persisted Decision (JSON). Cached by
    input_digest; the template (rec["explanation"]) on any failure. Never raises."""
    template = str(rec.get("explanation", ""))
    try:
        digest = rec["provenance"]["input_digest"]
        with _cache_lock:
            hit = _cache.get(digest)
        if hit is not None:
            return hit
        result = explain(payload_from_record(rec), template)
        _remember(digest, result)
        return result
    except Exception as exc:  # noqa: BLE001
        return Explanation(text=template, source="template", reason=type(exc).__name__)
