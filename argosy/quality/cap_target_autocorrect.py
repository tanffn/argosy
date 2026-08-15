"""Auto-correct the NVDA cap-vs-target incoherence IN-STAGE, before the FM /
whole-artifact reader ever see the draft (run-369 fix).

``coherence_gate.check_cap_target_coherence`` DETECTS a target-above-cap
incoherence; this module FIXES it — the "assert + auto-correct, do NOT block"
half of Ariel's ruling. It locates the offending sentence(s) in the rendered
horizon prose (the ones that cite BOTH the stale target figure and the
tighter cap figure) and asks the same cheap ``ProseEditorAgent`` the reader-
triggered surgical-reconcile loop uses (``argosy.agents.prose_editor``) to
reword them so the target is honestly described relative to THIS run's cap —
never introducing a new number, only rewording (identical safety contract to
``surgical_reconcile.py``).

Deliberately separate from ``surgical_reconcile.py``: that module fixes
findings the LLM whole-artifact READER cites (``reader_verdict.findings``,
which only exists after the reader has run). This module runs off the
DETERMINISTIC in-stage gate (``instage_gate.run_deterministic_gate_instage``),
so the fix lands before the FM/reader ever see the incoherent sentence — the
FM should not need to raise the objection in the first place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

log = __import__("logging").getLogger(__name__)

# Sentence-ish split — mirrors coherence_gate._SENTENCE_SPLIT_RE.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _num_variants(value: float) -> list[str]:
    """String forms of ``value`` a human-authored sentence is likely to use
    ("7", "7.0", "7%"-style bases without the percent sign — the caller's
    text already carries the '%')."""
    variants = {f"{value:g}", f"{value:.0f}", f"{value:.1f}"}
    return sorted(variants, key=len, reverse=True)


def find_incoherent_sentences(
    text: str, *, cap_pct: float, target_pct: float
) -> list[str]:
    """Sentences mentioning NVDA that cite BOTH the (stale) target figure near
    target language and the (tighter) cap figure near cap language — the exact
    class from run 369: "the 8% steering target sits ... inside the 7.0%
    binding instrument-level cap". Returns the verbatim sentence substrings so
    a caller can splice a correction back into the body text."""
    if not text or cap_pct is None or target_pct is None:
        return []
    hits: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(text):
        s = raw.strip()
        if not s or "nvda" not in s.lower():
            continue
        has_target = bool(
            re.search(r"target|steering|sleeve", s, re.IGNORECASE)
        ) and any(v in s for v in _num_variants(target_pct))
        has_cap = bool(
            re.search(r"cap\b|ceiling", s, re.IGNORECASE)
        ) and any(v in s for v in _num_variants(cap_pct))
        if has_target and has_cap:
            hits.append(s)
    return hits


@dataclass
class CapTargetAutoCorrection:
    corrected_bodies: dict
    edits: list = field(default_factory=list)  # [(horizon, before, after), ...]


def auto_correct_cap_target(
    *,
    bodies: dict[str, str],
    cap_pct: float,
    target_pct: float,
    editor: Callable[[str], str] | None = None,
) -> CapTargetAutoCorrection:
    """Reword every offending sentence in ``bodies`` (``{"long"/"medium"/
    "short": md}``) so the NVDA target is described honestly relative to this
    run's tighter cap. Pure best-effort: an editor failure or a rejected
    (number-injecting) edit leaves that sentence untouched — the deterministic
    gate stays WARNING-only either way, so a missed sentence never blocks."""
    from argosy.agents.prose_editor import correct_prose_site
    from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
        _edit_is_safe,
    )

    corrected = dict(bodies or {})
    edits: list[tuple[str, str, str]] = []
    binding = min(cap_pct, target_pct)
    canonical = (
        f"NVDA concentration cap this run (Argosy-derived, MIN over four "
        f"constraint caps) = {cap_pct:g}%. NVDA direct steering target "
        f"(allocation-derived) = {target_pct:g}%. A steering target cannot be "
        f"described as inside/below a smaller cap — state the BINDING "
        f"ceiling this run as {binding:g}% and describe the target as "
        f"capped at it, not the stale uncapped figure."
    )
    reason = (
        f"target {target_pct:g}% exceeds this run's binding cap {cap_pct:g}% "
        "— a steering target above its own ceiling is incoherent by "
        "construction"
    )
    allowed_numbers = frozenset(_num_variants(cap_pct)) | frozenset(_num_variants(target_pct))

    for horizon, body in corrected.items():
        if not body:
            continue
        for sentence in find_incoherent_sentences(body, cap_pct=cap_pct, target_pct=target_pct):
            fixed = correct_prose_site(
                fact_id="concentration.nvda_cap_target",
                canonical_value=canonical,
                offending_text=sentence,
                defect_reason=reason,
                editor=editor,
            )
            if fixed and fixed != sentence and _edit_is_safe(sentence, fixed, allowed_numbers):
                corrected[horizon] = corrected[horizon].replace(sentence, fixed, 1)
                edits.append((horizon, sentence, fixed))
            elif fixed and fixed != sentence:
                log.warning(
                    "cap_target_autocorrect.edit_rejected_unsafe horizon=%s", horizon,
                )

    return CapTargetAutoCorrection(corrected_bodies=corrected, edits=edits)


__all__ = [
    "CapTargetAutoCorrection",
    "auto_correct_cap_target",
    "find_incoherent_sentences",
]
