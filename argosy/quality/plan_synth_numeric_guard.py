"""Deterministic PRE-ACCEPTANCE guard for ``plan_synthesizer`` output.

Upstream counterpart to ``argosy.quality.fact_tokenizer`` (the post-hoc pass
that rewrites/report a FINISHED draft). This module runs the SAME call
BEFORE the draft is ever accepted: after the synthesizer returns, scan every
prose field for a bare numeric literal that corresponds to a registered
``{{fact:<key>}}`` concept (see ``argosy.quality.fact_registry.FACT_DISPLAY``)
and, if found, REJECT and re-call the synthesizer with a corrective
instruction naming the offending literal and the token it should have used.
Bounded retries (default 2); if the model still fails after the retry
budget, the caller gets the violations back instead of an infinite loop or a
silent accept.

Detection is NOT reinvented here. It reuses
``argosy.quality.fact_tokenizer.tokenize_text`` — the concept-anchored
(phrase anchor + clause-bounded proximity + false-positive guards) matcher
already built and tested for the post-hoc pass, itself modelled on
``argosy.services.assembled_artifact._extract_prose_nvda_values``. Two
outcomes from that call both count as a violation HERE (even though
``tokenize_text`` only treats one of them as a reportable violation):

  * a literal that MATCHES the canonical value — ``tokenize_text`` silently
    rewrites it to the token (that is correct behaviour for the post-hoc
    pass, which must ship SOMETHING). Pre-acceptance, this is still the
    exact sin instruction 1 forbids: the model typed digits instead of the
    token, and an unrelated re-roll of that same section will re-sample a
    DIFFERENT digit string next time. Reject and ask for the token.
  * a literal anchored to the concept that DIFFERS from canonical — the
    invented-number / drifted-number class (the "209,389 NIS margin", the
    9,417-vs-9,479 share count). ``tokenize_text`` already surfaces this as
    ``GateCheck.FACT_LITERAL_DRIFT``; reuse the same finding.

A concept with no ``AnchorSpec`` in ``fact_tokenizer.DEFAULT_ANCHORS`` is
never scanned — under-reach is the safe default (a blind "every number" scan
would false-positive on sleeve allocations, tax rates, ages, years, which
legitimately appear as digits and have no canonical key).

This module never rewrites the model's prose and never calls an LLM itself
— ``scan_output`` / ``build_corrective_feedback`` are pure and deterministic;
``synthesize_with_numeric_guard`` is the only piece that drives the retry
loop, and it does so by re-invoking the SAME ``PlanSynthesizerAgent.run``
the caller would otherwise call directly.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from argosy.quality.fact_tokenizer import DEFAULT_ANCHORS, AnchorSpec, tokenize_text

if TYPE_CHECKING:  # pragma: no cover — typing only
    from argosy.agents.base import AgentReport
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
    from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers

# fact_tokenizer's GateViolation.detail is formatted as:
#   "literal `<lit>` near concept `<key>` diverges from canonical <rendered>
#    — surfaced, NOT auto-corrected"
# (see fact_tokenizer.tokenize_text). Parsed defensively — if the format
# ever changes, the key/literal fall back to "?" and the raw detail string
# is still carried in the message, so nothing is silently dropped.
_DETAIL_KEY_RE = re.compile(r"near concept `([^`]+)`")
_DETAIL_LITERAL_RE = re.compile(r"literal `([^`]+)`")


@dataclass(frozen=True)
class NumericLiteralFinding:
    """One place the draft wrote digits where a ``{{fact:key}}`` token
    belonged."""

    locator: str
    key: str
    literal: str
    kind: str  # "typed_literal" (matched canonical) | "drift" (wrong value)
    message: str


# ---------------------------------------------------------------------------
# Field enumeration — every prose surface the model can hand-type digits
# into. Deliberately excludes FactClaim.value / SynthTarget.value (typed
# numeric fields the schema REQUIRES to carry a number; that is structured
# data, not prose the model chose to type digits into) and excludes
# citation extracts (verbatim source text, must not be rewritten/flagged).
# ---------------------------------------------------------------------------


def _iter_text_fields(output: "PlanSynthesisOutput") -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for hname in ("long", "medium", "short"):
        hz = getattr(output, hname, None)
        if hz is None:
            continue
        fields.append((f"{hname}.posture", hz.posture or ""))
        fields.append((f"{hname}.rationale", hz.rationale or ""))
        for i, t in enumerate(hz.targets):
            fields.append((f"{hname}.targets[{i}].rationale", t.rationale or ""))
        for i, th in enumerate(hz.themes):
            fields.append((f"{hname}.themes[{i}].rationale", th.rationale or ""))
        for i, a in enumerate(hz.actions):
            fields.append((f"{hname}.actions[{i}].detail", a.detail or ""))
            fields.append((f"{hname}.actions[{i}].rationale", a.rationale or ""))
            fields.append((f"{hname}.actions[{i}].how_to", a.how_to or ""))
            fields.append((f"{hname}.actions[{i}].done_when", a.done_when or ""))
        for i, d in enumerate(hz.deltas_from_prior):
            fields.append((f"{hname}.deltas_from_prior[{i}].summary", d.summary or ""))
            fields.append((f"{hname}.deltas_from_prior[{i}].rationale", d.rationale or ""))
    for i, s in enumerate(getattr(output, "sections", []) or []):
        loc = f"sections[{i}]({s.section_id}/{s.horizon})"
        fields.append((f"{loc}.title", s.title or ""))
        fields.append((f"{loc}.body_md", s.body_md or ""))
    return fields


def scan_output(
    output: "PlanSynthesisOutput",
    resolved: "ResolvedPlanNumbers",
    *,
    anchors: tuple["AnchorSpec", ...] = DEFAULT_ANCHORS,
) -> list[NumericLiteralFinding]:
    """Scan every prose field of a synthesizer draft for a bare numeric
    literal anchored to a registered fact concept. Empty list == clean
    (either no anchored literal was written, or every anchored literal was
    already emitted as a ``{{fact:key}}`` token)."""
    findings: list[NumericLiteralFinding] = []
    for locator, text in _iter_text_fields(output):
        if not text or not _has_bare_digit(text):
            # Cheap skip: a field with no digits at all can't match either
            # rule family below. Not required for correctness (tokenize_text
            # is idempotent/safe on such text) — pure micro-optimisation for
            # the common case (most rationale strings carry no numbers).
            continue
        result = tokenize_text(text, resolved, anchors=anchors, horizon=locator)
        for key, literal in result.substitutions:
            findings.append(
                NumericLiteralFinding(
                    locator=locator,
                    key=key,
                    literal=literal,
                    kind="typed_literal",
                    message=(
                        f"[{locator}] literal `{literal}` equals the canonical "
                        f"value of `{key}` but was typed as digits instead of "
                        f"the token `{{{{fact:{key}}}}}` — replace the digits "
                        "with the token."
                    ),
                )
            )
        for v in result.violations:
            key_m = _DETAIL_KEY_RE.search(v.detail)
            lit_m = _DETAIL_LITERAL_RE.search(v.detail)
            key = key_m.group(1) if key_m else "?"
            literal = lit_m.group(1) if lit_m else "?"
            findings.append(
                NumericLiteralFinding(
                    locator=locator,
                    key=key,
                    literal=literal,
                    kind="drift",
                    message=(
                        f"[{locator}] {v.detail} — replace the literal with "
                        f"`{{{{fact:{key}}}}}`."
                    ),
                )
            )
    return findings


_ANY_DIGIT_RE = re.compile(r"\d")


def _has_bare_digit(text: str) -> bool:
    return bool(_ANY_DIGIT_RE.search(text))


def build_corrective_feedback(findings: list[NumericLiteralFinding]) -> str:
    """Render ``findings`` into the ``numeric_literal_feedback`` block that
    ``PlanSynthesizerAgent.build_prompt`` injects on the next retry."""
    if not findings:
        return ""
    lines = [
        "Your previous draft wrote raw digits for figures that MUST be "
        "emitted as `{{fact:<key>}}` tokens instead (see the TOKEN EMISSION "
        "rule + the DERIVED HEADLINE NUMBERS block). Every occurrence below "
        "must be fixed by replacing the literal with its token; do not "
        "change anything else in the surrounding sentence:",
    ]
    for f in findings:
        lines.append(f"  - {f.message}")
    lines.append(
        "Re-emit the FULL corrected JSON object (every field, not just the "
        "fixed ones). No occurrence of a keyed concept may appear as "
        "hand-typed digits anywhere in the response."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reject -> retry driver.
# ---------------------------------------------------------------------------


@dataclass
class NumericGuardResult:
    """Outcome of the reject/retry loop."""

    output: "PlanSynthesisOutput"
    report: "AgentReport"
    all_reports: list["AgentReport"] = field(default_factory=list)
    # Findings against the FINAL returned draft. Empty means it came back
    # clean (on the first attempt or after a successful retry).
    findings: list[NumericLiteralFinding] = field(default_factory=list)
    attempts: int = 1
    # False when the retry budget was exhausted and `findings` is still
    # non-empty — the caller must surface this as a violation, not accept
    # silently and not retry forever.
    accepted: bool = True


async def synthesize_with_numeric_guard(
    agent: "PlanSynthesizerAgent",
    *,
    resolved: "ResolvedPlanNumbers",
    max_retries: int = 2,
    anchors: tuple["AnchorSpec", ...] = DEFAULT_ANCHORS,
    **build_kwargs: Any,
) -> NumericGuardResult:
    """Call ``agent.run(**build_kwargs)``, scan the draft, and reject+retry
    (bounded by ``max_retries``) while a bare numeric literal for a keyed
    concept remains. Never loops unboundedly; never rewrites the model's
    prose itself — accept or reject-with-reason only."""
    reports: list["AgentReport"] = []
    feedback = str(build_kwargs.pop("numeric_literal_feedback", "") or "")
    findings: list[NumericLiteralFinding] = []
    for attempt in range(max_retries + 1):
        kwargs = dict(build_kwargs)
        kwargs["numeric_literal_feedback"] = feedback
        report = await agent.run(**kwargs)
        reports.append(report)
        output = report.output
        findings = scan_output(output, resolved, anchors=anchors)
        if not findings:
            return NumericGuardResult(
                output=output, report=report, all_reports=reports,
                findings=[], attempts=attempt + 1, accepted=True,
            )
        feedback = build_corrective_feedback(findings)
    # Retry budget exhausted and the draft still has findings — surface
    # rather than silently accepting or looping forever.
    return NumericGuardResult(
        output=reports[-1].output, report=reports[-1], all_reports=reports,
        findings=findings, attempts=len(reports), accepted=False,
    )


def synthesize_with_numeric_guard_sync(
    agent: "PlanSynthesizerAgent",
    *,
    resolved: "ResolvedPlanNumbers",
    max_retries: int = 2,
    anchors: tuple["AnchorSpec", ...] = DEFAULT_ANCHORS,
    **build_kwargs: Any,
) -> NumericGuardResult:
    """Synchronous wrapper — mirrors ``BaseAgent.run_sync``. Cannot be
    called from inside a running event loop."""
    return asyncio.run(
        synthesize_with_numeric_guard(
            agent, resolved=resolved, max_retries=max_retries,
            anchors=anchors, **build_kwargs,
        )
    )


__all__ = [
    "NumericLiteralFinding",
    "NumericGuardResult",
    "scan_output",
    "build_corrective_feedback",
    "synthesize_with_numeric_guard",
    "synthesize_with_numeric_guard_sync",
]
