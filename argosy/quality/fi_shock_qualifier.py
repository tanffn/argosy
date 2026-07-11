"""Deterministic FI-shock / FX-shock qualifier for section body_md.

Owner-routed and surgical reconcile edit horizon markdown only; ``sections_json``
``Section.body_md`` can keep an unqualified "reached" claim that the horizon
pass already fixed. This module reuses the SAME detection idea as
``coherence_gate.check_fi_sufficiency_under_shock`` /
``fi_fx_shock_gate.check_fi_sufficiency_under_fx_shock`` and inserts a minimal
same-sentence caveat ("qualify, don't delete") so section surfaces cannot ship
the claim class the gates fail.

Pure — no I/O, no LLM. Safe to call on every assemble / reconcile persist.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Detection regexes mirror coherence_gate / fi_fx_shock_gate (duplicated so this
# module stays a standalone pure helper — same fail-loud sentence scope).
_REACHED_RE = re.compile(
    r"(?:"
    r"capital sufficiency\s*:?\s*reached"
    r"|sufficiency\s*:?\s*reached"
    r"|\bfi\b\s*:?\s*reached"
    r"|\bfi\b[^.!?]{0,60}\breached\b"
    r"|reached[^.!?]{0,60}financial independence"
    r"|financial independence[^.!?]{0,60}reached"
    r"|financially independent"
    r"|(?:full )?(?:financial|capital) sufficiency[^.!?]{0,40}(?:achieved|reached)"
    r")",
    re.IGNORECASE,
)
_SHOCK_QUALIFIER_RE = re.compile(
    r"(?:nvda[^.!?]{0,40}(?:shock|tail|drawdown|down|−30|-30|\d{1,2}%|mark)|"
    r"(?:shock|tail|drawdown|−30|-30|\d{1,2}%)[^.!?]{0,40}nvda|"
    r"only at the full nvda mark|at the full nvda mark|"
    r"robust to|conditional on the nvda)",
    re.IGNORECASE,
)
_FX_SHOCK_QUALIFIER_RE = re.compile(
    r"(?:"
    r"usd\s*/\s*(?:nis|ils)"
    r"|nis\s*/\s*usd"
    r"|\bfx\b"
    r"|currenc(?:y|ies)"
    r"|shekel|shekels"
    r"|exchange rate"
    r"|[-−]10%|[-−]0\.10"
    r")",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?:not|isn't|is not|won't|will not|not yet|no longer|below|short of|"
    r"fails? to|does not|doesn't|never|cannot|can't|can not)\b",
    re.IGNORECASE,
)
_SENTENCE_KEEP_RE = re.compile(r"([.!?\n]+)")

_NVDA_CLAUSE = " only at the full NVDA mark"
_FX_CLAUSE = " only at the current FX / currency mark"
_BOTH_CLAUSE = " only at the full NVDA mark and current FX / currency mark"


def shock_needs_qualifiers(
    *,
    shock_result: dict | None = None,
    fx_shock_result: dict | None = None,
) -> tuple[bool, bool]:
    """Return ``(need_nvda, need_fx)`` from shock-row break flags."""
    need_nvda = False
    if shock_result:
        row = shock_result.get("shock_0.30") or {}
        need_nvda = row.get("perpetuity_reached") is False
    need_fx = False
    if fx_shock_result:
        row = fx_shock_result.get("fx_shock_-0.10") or {}
        need_fx = row.get("total_reached") is False
    return need_nvda, need_fx


def qualify_reached_text(
    text: str,
    *,
    need_nvda: bool = False,
    need_fx: bool = False,
) -> str:
    """Insert same-sentence NVDA/FX caveats on unqualified 'reached' claims."""
    if not text or (not need_nvda and not need_fx):
        return text
    parts = _SENTENCE_KEEP_RE.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
            continue
        out.append(_qualify_sentence(part, need_nvda=need_nvda, need_fx=need_fx))
    return "".join(out)


def _qualify_sentence(
    sentence: str, *, need_nvda: bool, need_fx: bool
) -> str:
    if not sentence.strip():
        return sentence
    m = _REACHED_RE.search(sentence)
    if m is None:
        return sentence
    if _NEGATION_RE.search(sentence):
        return sentence
    add_nvda = need_nvda and not _SHOCK_QUALIFIER_RE.search(sentence)
    add_fx = need_fx and not _FX_SHOCK_QUALIFIER_RE.search(sentence)
    if not add_nvda and not add_fx:
        return sentence
    if add_nvda and add_fx:
        insert = _BOTH_CLAUSE
    elif add_nvda:
        insert = _NVDA_CLAUSE
    else:
        insert = _FX_CLAUSE
    return sentence[: m.end()] + insert + sentence[m.end() :]


def qualify_sections_json(
    sections_json: str | None,
    *,
    shock_result: dict | None = None,
    fx_shock_result: dict | None = None,
) -> tuple[str | None, int]:
    """Qualify each ``Section.body_md``; return ``(json, n_sections_edited)``.

    No-op when shocks do not break sufficiency, JSON is missing/malformed, or
    no body needs a caveat. Preserves section identity fields.
    """
    need_nvda, need_fx = shock_needs_qualifiers(
        shock_result=shock_result, fx_shock_result=fx_shock_result,
    )
    if not sections_json or (not need_nvda and not need_fx):
        return sections_json, 0
    try:
        sections = json.loads(sections_json)
    except (TypeError, ValueError):
        return sections_json, 0
    if not isinstance(sections, list):
        return sections_json, 0
    edited = 0
    for s in sections:
        if not isinstance(s, dict):
            continue
        body = s.get("body_md")
        if not isinstance(body, str) or not body:
            continue
        new_body = qualify_reached_text(
            body, need_nvda=need_nvda, need_fx=need_fx,
        )
        if new_body != body:
            s["body_md"] = new_body
            edited += 1
    if edited == 0:
        return sections_json, 0
    return json.dumps(sections), edited


def qualify_sections_json_from_resolved(
    sections_json: str | None,
    resolved: Any,
) -> tuple[str | None, int]:
    """Derive shock rows from ``resolved`` then qualify section bodies.

    Fail-soft: any derivation error returns the input unchanged.
    """
    if not sections_json or resolved is None:
        return sections_json, 0
    try:
        from argosy.services.retirement.fi_shock import (
            derive_fx_shock_inputs,
            derive_nvda_shock_inputs,
            fi_sufficiency_under_fx_shock,
            fi_sufficiency_under_shock,
        )

        shock_result = None
        fx_shock_result = None
        nvda_inputs = derive_nvda_shock_inputs(resolved)
        if nvda_inputs is not None:
            shock_result = fi_sufficiency_under_shock(**nvda_inputs)
        fx_inputs = derive_fx_shock_inputs(resolved)
        if fx_inputs is not None:
            fx_shock_result = fi_sufficiency_under_fx_shock(**fx_inputs)
        return qualify_sections_json(
            sections_json,
            shock_result=shock_result,
            fx_shock_result=fx_shock_result,
        )
    except Exception:  # noqa: BLE001 — assemble must not break on this
        return sections_json, 0


def section_bodies_plan_text(synth: Any) -> str:
    """Join ``Section.body_md`` for gate plan_text parity with horizons."""
    sections = getattr(synth, "sections", None) or []
    bodies = [
        (getattr(s, "body_md", None) or "").strip()
        for s in sections
    ]
    return "\n\n".join(b for b in bodies if b)
