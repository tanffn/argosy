"""Plan output gate — the 5 checks.

Operates on:
- Raw markdown text                (checks 1, 2)
- Structured `PlanSynthesisOutput` (checks 3, 4)
- Structured `PlanSynthesisOutput` + `PlanDistillate` (check 5)

The gate is the regression artifact for the integration plan: it
must fail RED on the persisted plan v20 fixture. Phases 1-4 land the
synth-side fixes that turn each check GREEN in turn.

Section / SectionEvidence / Citation / Assumption / FactClaim are
shipped in Phase 3 and live in `argosy.quality.expected_shapes`.
For Phase 0, the checks fall through gracefully when those fields
are absent on a `HorizonSection` (which is the v20 state) — in fact
absence IS the failure mode and is reported with a meaningful
violation.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from argosy.quality.canonical_sections import (
    CANONICAL_SECTION_IDS,
    DISTILLATE_FIELD_TO_SECTION_ID,
    MVP_COVERAGE_THRESHOLD,
)
from argosy.quality.coherence_gate import (
    check_cross_surface_coherence,
    check_fi_sufficiency_under_shock,
)
from argosy.quality.freshness_gate import check_input_freshness
from argosy.quality.gate_types import GateCheck, GateVerdict, GateViolation
from argosy.quality.numeric_source_gate import check_headline_numeric_source
from argosy.quality.regex_patterns import (
    HISTORY_LEAK_PATTERNS,
    JARGON_LEAK_PATTERNS,
)

if TYPE_CHECKING:
    # Avoid circular import — PlanDistillate / PlanSynthesisOutput
    # are only referenced for typing.
    from argosy.agents.plan_distiller_types import PlanDistillate
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
    )
    from argosy.services.assembled_artifact import AssembledArtifact
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check 1 — history_leak
# ---------------------------------------------------------------------------

def check_history_leak(text: str) -> list[GateViolation]:
    """Run the history-leak regex set against raw markdown.

    Returns one violation per match. v20 horizon markdown produces
    many matches because the synth prompt actively encourages
    revision-history narration.
    """
    violations: list[GateViolation] = []
    for pattern in HISTORY_LEAK_PATTERNS:
        for match in pattern.finditer(text):
            violations.append(
                GateViolation(
                    check=GateCheck.HISTORY_LEAK,
                    detail=(
                        f"matched `{match.group()}` "
                        f"(pattern: `{pattern.pattern[:60]}...`)"
                    ),
                    locator=f"offset={match.start()}",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Check 2 — jargon_leak
# ---------------------------------------------------------------------------

def check_jargon_leak(text: str) -> list[GateViolation]:
    """Run the jargon-leak regex set against raw markdown.

    Picks up internal agent class names, RED/YELLOW/GREEN grading,
    "substrate" jargon, "=== Cls ===" frame leakage.
    """
    violations: list[GateViolation] = []
    for pattern in JARGON_LEAK_PATTERNS:
        for match in pattern.finditer(text):
            violations.append(
                GateViolation(
                    check=GateCheck.JARGON_LEAK,
                    detail=(
                        f"matched `{match.group()}` "
                        f"(pattern: `{pattern.pattern[:60]}...`)"
                    ),
                    locator=f"offset={match.start()}",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Check 3 — section_coverage
# ---------------------------------------------------------------------------

def _collect_section_ids(synth: PlanSynthesisOutput) -> set[str]:
    """Return the set of canonical section_ids present across all
    sections in the synth output.

    Reads `PlanSynthesisOutput.sections` (flat list, Phase 3 shape —
    each Section carries its own `horizon`). Falls back to per-horizon
    `synth.long/medium/short.sections` for any future/transitional
    shape. Tolerates the v20 legacy shape (neither attribute present)
    by returning empty set."""
    return {
        sid
        for section in _iter_sections(synth)
        if isinstance(sid := getattr(section, "section_id", None), str)
    }


def check_section_coverage(
    synth: PlanSynthesisOutput,
    threshold: int = MVP_COVERAGE_THRESHOLD,
) -> list[GateViolation]:
    """Verify canonical section_id coverage across the three horizons.

    Failure modes:
    - Total covered count is below `threshold`.
    - One or more emitted section_ids are not canonical (typo'd).
    """
    violations: list[GateViolation] = []
    present = _collect_section_ids(synth)
    unknown = present - CANONICAL_SECTION_IDS.keys()
    missing = CANONICAL_SECTION_IDS.keys() - present

    if len(present) < threshold:
        violations.append(
            GateViolation(
                check=GateCheck.SECTION_COVERAGE,
                detail=(
                    f"coverage {len(present)}/{len(CANONICAL_SECTION_IDS)} "
                    f"below threshold {threshold}; "
                    f"missing: {sorted(missing)[:6]}"
                    f"{'...' if len(missing) > 6 else ''}"
                ),
            )
        )
    if unknown:
        violations.append(
            GateViolation(
                check=GateCheck.SECTION_COVERAGE,
                detail=f"unknown (non-canonical) section_ids emitted: {sorted(unknown)}",
            )
        )
    return violations


# ---------------------------------------------------------------------------
# Check 4 — evidence_per_section
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of",
    "in", "on", "at", "for", "and", "or", "but", "with", "by", "as",
    "this", "that", "it", "from", "has", "have", "had", "its", "their",
    "his", "her", "our", "we", "you", "they", "he", "she",
})


def _content_tokens(s: str) -> set[str]:
    """Lowercased word set minus stopwords and very short tokens.

    Used by the categorical/policy/qualitative fact-support gate.
    """
    return {
        w
        for w in s.lower().replace(",", " ").replace(".", " ").split()
        if w not in _STOPWORDS and len(w) > 2
    }


def _iter_sections(synth: PlanSynthesisOutput) -> list[Any]:
    """Flat list of all `Section`-like objects in the synth output.

    Reads `synth.sections` first (Phase 3 canonical shape: flat list
    with `Section.horizon` discriminator). Falls back to per-horizon
    `synth.long/medium/short.sections` for transitional shapes.
    Tolerates absent `sections` everywhere (v20 legacy) by returning
    empty list."""
    out: list[Any] = []
    # Phase 3 canonical: top-level flat list
    top_level: Any = getattr(synth, "sections", None)
    if top_level:
        out.extend(top_level)
        return out
    # Backward-compat: per-horizon list
    for horizon_name in ("long", "medium", "short"):
        horizon = getattr(synth, horizon_name, None)
        if horizon is None:
            continue
        sections: Any = getattr(horizon, "sections", None)
        if not sections:
            continue
        out.extend(sections)
    return out


def _validate_section_evidence(section: Any) -> list[GateViolation]:
    """Per-section evidence content gate.

    Validates the section's `evidence` field against the v3.1 contract.
    Fails the section if any of:
    - evidence absent or None
    - facts and missing_data both empty
    - any fact has no citation in source_span
    - numeric fact value not present in cite extract
    - categorical/policy/qualitative fact extract shares <3 content
      tokens with fact.text
    - citation kind in {inference, agent_baseline} but no assumptions
    - citation to concrete source (plan_doc / portfolio_snapshot /
      analyst_report) missing extract or extract <8 chars
    """
    sid = getattr(section, "section_id", "<unknown>")
    locator = f"section_id={sid}"
    evidence = getattr(section, "evidence", None)
    if evidence is None:
        return [
            GateViolation(
                check=GateCheck.EVIDENCE_PER_SECTION,
                detail=f"section '{sid}' has no evidence attribute",
                locator=locator,
            )
        ]

    facts = list(getattr(evidence, "facts", []) or [])
    citations = list(getattr(evidence, "source_span", []) or [])
    assumptions = list(getattr(evidence, "assumptions", []) or [])
    missing_data = list(getattr(evidence, "missing_data", []) or [])

    violations: list[GateViolation] = []

    # Rule: section must have facts OR missing_data (never silently empty)
    if not facts and not missing_data:
        violations.append(
            GateViolation(
                check=GateCheck.EVIDENCE_PER_SECTION,
                detail=(
                    f"section '{sid}' has neither facts nor missing_data — "
                    "silent empty is forbidden"
                ),
                locator=locator,
            )
        )

    # Rule: every FactClaim has at least one Citation; and every
    # Citation has a valid supports_fact_index pointing into facts.
    fact_indices_with_cite: set[int] = set()
    for ci, c in enumerate(citations):
        idx = getattr(c, "supports_fact_index", None)
        if not isinstance(idx, int) or idx < 0 or idx >= len(facts):
            violations.append(
                GateViolation(
                    check=GateCheck.EVIDENCE_PER_SECTION,
                    detail=(
                        f"section '{sid}' Citation[{ci}] has invalid "
                        f"supports_fact_index={idx!r} "
                        f"(facts has {len(facts)} entries)"
                    ),
                    locator=locator,
                )
            )
            continue
        fact_indices_with_cite.add(idx)
    for i, fact in enumerate(facts):
        # Rule: FactClaim.text must be ≥12 chars (no single-token claims)
        text = getattr(fact, "text", str(fact)) or ""
        if len(text.strip()) < 12:
            violations.append(
                GateViolation(
                    check=GateCheck.EVIDENCE_PER_SECTION,
                    detail=(
                        f"section '{sid}' FactClaim[{i}] text "
                        f"(`{text[:24]}`) is shorter than 12 chars — "
                        "single-token fluency forbidden"
                    ),
                    locator=locator,
                )
            )
        if i not in fact_indices_with_cite:
            violations.append(
                GateViolation(
                    check=GateCheck.EVIDENCE_PER_SECTION,
                    detail=(
                        f"section '{sid}' FactClaim[{i}] (`{text[:40]}`) "
                        "has no citation in source_span"
                    ),
                    locator=locator,
                )
            )

    # Rule: inference / agent_baseline citations require ≥1 assumption
    has_inference = any(
        getattr(c, "source_kind", None) in {"inference", "agent_baseline"}
        for c in citations
    )
    if has_inference and not assumptions:
        violations.append(
            GateViolation(
                check=GateCheck.EVIDENCE_PER_SECTION,
                detail=(
                    f"section '{sid}' uses inference/agent_baseline "
                    "citations but declares no assumptions"
                ),
                locator=locator,
            )
        )

    # Rule: concrete-source citations need extract ≥8 chars AND must
    # support the bound fact (token overlap for non-numeric, substring
    # match for numeric).
    for c in citations:
        kind = getattr(c, "source_kind", None)
        if kind not in {"plan_doc", "portfolio_snapshot", "analyst_report"}:
            continue
        extract = getattr(c, "extract", None) or ""
        if len(extract) < 8:
            violations.append(
                GateViolation(
                    check=GateCheck.EVIDENCE_PER_SECTION,
                    detail=(
                        f"section '{sid}' citation to {kind} missing or "
                        f"too-short extract (len={len(extract)}, need ≥8)"
                    ),
                    locator=locator,
                )
            )
            continue
        idx = getattr(c, "supports_fact_index", None)
        if not isinstance(idx, int) or idx < 0 or idx >= len(facts):
            continue
        fact = facts[idx]
        fkind = getattr(fact, "kind", None)
        value = getattr(fact, "value", None)
        text = getattr(fact, "text", "")
        if fkind == "numeric" and value is not None:
            # Numeric: value must appear in extract, tolerant of
            # locale-style formatting (commas, spaces, narrow no-break
            # space). Synth prompt explicitly allows "277000" vs
            # "277,000" interchangeably; gate must accept both so the
            # contract doesn't trip on formatting that the model was
            # told is fine.
            normalized_extract = (
                extract.replace(",", "")
                .replace(" ", "")
                .replace(" ", "")  # NBSP
                .replace(" ", "")  # narrow NBSP
            )
            if str(value) not in normalized_extract:
                violations.append(
                    GateViolation(
                        check=GateCheck.EVIDENCE_PER_SECTION,
                        detail=(
                            f"section '{sid}' numeric FactClaim[{idx}] "
                            f"value={value!r} not present in citation extract"
                        ),
                        locator=locator,
                    )
                )
        elif fkind in {"categorical", "policy", "qualitative"}:
            overlap = _content_tokens(text) & _content_tokens(extract)
            if len(overlap) < 3:
                violations.append(
                    GateViolation(
                        check=GateCheck.EVIDENCE_PER_SECTION,
                        detail=(
                            f"section '{sid}' {fkind} FactClaim[{idx}] "
                            f"extract shares only {len(overlap)} content "
                            f"tokens with fact text (need ≥3)"
                        ),
                        locator=locator,
                    )
                )

    return violations


def check_evidence_per_section(synth: PlanSynthesisOutput) -> list[GateViolation]:
    """Verify per-section evidence contract across all sections.

    Failure mode for v20: no `sections` attribute on HorizonSection —
    every horizon has zero sections, so the check reports
    "no sections present" once per horizon.
    """
    violations: list[GateViolation] = []
    sections = _iter_sections(synth)
    if not sections:
        # Phase 0 / v20: no sections to evaluate at all
        violations.append(
            GateViolation(
                check=GateCheck.EVIDENCE_PER_SECTION,
                detail=(
                    "PlanSynthesisOutput has no Section[] entries — "
                    "evidence contract requires structured sections "
                    "(Phase 3 adds the schema; v20 legacy shape has none)"
                ),
            )
        )
        return violations
    for section in sections:
        violations.extend(_validate_section_evidence(section))
    return violations


# ---------------------------------------------------------------------------
# Check 5 — distillate_section_binding
# ---------------------------------------------------------------------------

def check_distillate_section_binding(
    synth: PlanSynthesisOutput,
    distillate: PlanDistillate | None,
) -> list[GateViolation]:
    """For every non-empty distillate field bound to a section_id:
       (a) the section_id must appear in the synth output, AND
       (b) at least one citation in that section must have
           source_locator starting with `distillate.<field_name>`.

    If `distillate` is None, returns empty list (check is skipped —
    appropriate when no baseline plan was ingested).
    """
    if distillate is None:
        return []

    violations: list[GateViolation] = []
    # Group sections by section_id — the same section_id may legitimately
    # appear in multiple horizons (e.g. concentration shows in short,
    # medium, and long); a citation in ANY of them satisfies USE.
    sections_by_id: dict[str, list[Any]] = {}
    for section in _iter_sections(synth):
        sid = getattr(section, "section_id", None)
        if isinstance(sid, str):
            sections_by_id.setdefault(sid, []).append(section)

    for field_name, section_id in DISTILLATE_FIELD_TO_SECTION_ID.items():
        if section_id is None:
            continue  # ungated
        field_value = getattr(distillate, field_name, None)
        # treat empty list / empty dict / falsy as "not provided"
        if not field_value:
            continue
        # (a) section presence (in at least one horizon)
        if section_id not in sections_by_id:
            violations.append(
                GateViolation(
                    check=GateCheck.DISTILLATE_SECTION_BINDING,
                    detail=(
                        f"distillate.{field_name} is non-empty but bound "
                        f"section_id '{section_id}' is absent from synth output"
                    ),
                    locator=f"distillate.{field_name}",
                )
            )
            continue
        # (b) section USE — citation in ANY matching section across
        # horizons must reference the distillate field
        expected_prefix = f"distillate.{field_name}"
        has_citation = False
        any_evidence = False
        for section in sections_by_id[section_id]:
            evidence = getattr(section, "evidence", None)
            if evidence is None:
                continue
            any_evidence = True
            citations = list(getattr(evidence, "source_span", []) or [])
            if any(
                (getattr(c, "source_locator", "") or "").startswith(expected_prefix)
                for c in citations
            ):
                has_citation = True
                break
        if not any_evidence:
            violations.append(
                GateViolation(
                    check=GateCheck.DISTILLATE_SECTION_BINDING,
                    detail=(
                        f"distillate.{field_name} non-empty and section "
                        f"'{section_id}' present, but no horizon's instance "
                        "carries evidence (cannot verify USE)"
                    ),
                    locator=f"distillate.{field_name}",
                )
            )
            continue
        if not has_citation:
            violations.append(
                GateViolation(
                    check=GateCheck.DISTILLATE_SECTION_BINDING,
                    detail=(
                        f"distillate.{field_name} non-empty and section "
                        f"'{section_id}' present (in "
                        f"{len(sections_by_id[section_id])} horizon(s)), "
                        f"but no citation with source_locator "
                        f"'{expected_prefix}*' — field appears unused"
                    ),
                    locator=f"distillate.{field_name}",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Top-level: gate_plan_output
# ---------------------------------------------------------------------------

IPS_SUM_TOLERANCE_PCT = 1.0  # allow ±1pp for rounding across ~11 sleeves


def check_ips_allocation_sum(synth: PlanSynthesisOutput) -> list[GateViolation]:
    """The IPS (medium-horizon) allocatable sleeve targets must sum to ~100%.

    The investment-policy allocation is a mechanical 100% partition of the
    portfolio across sleeves (each a ``pct_of_portfolio`` target). Two failure
    modes this catches deterministically — instead of leaving it to an LLM
    reviewer to eyeball:
      - UNDER-allocation: an implicit/undeclared core sleeve (sleeves sum to
        ~51% — the FM-rejected draft 38).
      - OVER-allocation: a phase/floor POLICY descriptor emitted as a
        ``pct_of_portfolio`` target double-counts its component sleeves (e.g. a
        "defensive floor" line on top of the cash + bonds sleeves → ~108% —
        the FM-MISSED draft 39).

    Policy descriptors (the phase-aware defensive glide: "~8% floor now → ~15%
    at retirement onset") are POLICY, not allocatable sleeves — they must not
    carry the ``pct_of_portfolio`` unit in the IPS horizon (put them in themes /
    a note / a different unit). Any such descriptor here pushes the sum off 100
    and trips this gate, which is the intended behavior.

    Scoped to the medium horizon: it carries the full IPS allocation. The long
    horizon legitimately states a single forward policy point (e.g. "15% at
    retirement onset") and the short horizon carries none, so neither is a
    100% partition and neither is summed here.
    """
    medium = getattr(synth, "medium", None)
    targets = list(getattr(medium, "targets", []) or [])
    sleeve_pcts = [
        (t, float(t.value))
        for t in targets
        if getattr(t, "unit", None) == "pct_of_portfolio"
        and isinstance(getattr(t, "value", None), (int, float))
    ]
    if not sleeve_pcts:
        return []  # nothing to validate (no allocation declared this horizon)
    total = round(sum(v for _, v in sleeve_pcts), 2)
    if abs(total - 100.0) <= IPS_SUM_TOLERANCE_PCT:
        return []
    listing = "; ".join(f"{getattr(t, 'label', '?')}={v}" for t, v in sleeve_pcts)
    direction = "OVER-allocated (double-count / redundant descriptor)" if total > 100 else \
        "UNDER-allocated (implicit/undeclared sleeve)"
    return [GateViolation(
        check=GateCheck.IPS_ALLOCATION_SUM,
        detail=(
            f"IPS medium-horizon pct_of_portfolio sleeve targets sum to "
            f"{total}% (must be 100±{IPS_SUM_TOLERANCE_PCT}) — {direction}. "
            f"Sleeves: {listing}"
        ),
        locator="horizon=medium",
    )]


def gate_plan_output(
    horizon_text: dict[str, str],
    synth: PlanSynthesisOutput | None = None,
    distillate: PlanDistillate | None = None,
    *,
    coverage_threshold: int = MVP_COVERAGE_THRESHOLD,
    resolved: "ResolvedPlanNumbers | None" = None,
    artifact: "AssembledArtifact | None" = None,
    today: "date | None" = None,
    snapshot_date: "date | None" = None,
    analyst_report_dates: "dict[str, date] | None" = None,
) -> GateVerdict:
    """Run all gate checks and return an aggregate verdict.

    Args:
        horizon_text: dict mapping horizon name -> raw markdown text.
            Required for checks 1 + 2. Keys: 'long', 'medium', 'short'
            (any subset OK; missing keys skip the corresponding scan).
        synth: structured `PlanSynthesisOutput`. Required for checks
            3 + 4 + 5. If None, those checks are skipped.
        distillate: structured `PlanDistillate` (ingested baseline).
            Required for check 5. If None, check 5 is skipped.
        coverage_threshold: section_coverage threshold (defaults to
            MVP launch target of 12/18; promote to 18 at full ship).
        resolved: the deterministic resolver manifest for this plan's
            decision run. Required for check 6 (headline_numeric_source).
            If None, check 6 is skipped here — the caller is responsible
            for fail-closed handling in enforce mode (see
            `plan._run_plan_output_gate`).
        artifact: the assembled whole-artifact (every user-facing surface +
            a per-surface headline map). Required for the cross-surface
            coherence check (and the extraction-error fail-loud). Skipped
            when None.
        today: the run date the plan is produced for. Required for the
            input-freshness check; the check is skipped entirely when None.
        snapshot_date: the portfolio-snapshot capture date (for freshness).
        analyst_report_dates: ``{report_name: capture_date}`` for cached
            analyst outputs the run relies on (for freshness).

    Returns:
        GateVerdict with violations grouped by check.
    """
    verdict = GateVerdict()

    for horizon_name, text in horizon_text.items():
        if not text:
            continue
        verdict.extend(check_history_leak(text))
        verdict.extend(check_jargon_leak(text))

    if synth is not None:
        verdict.extend(check_section_coverage(synth, threshold=coverage_threshold))
        verdict.extend(check_evidence_per_section(synth))
        verdict.extend(check_distillate_section_binding(synth, distillate))
        verdict.extend(check_ips_allocation_sum(synth))

    # Check 6 — headline numeric source (#24). Only runs when the resolver
    # manifest is supplied. Fail-closed for a *missing* manifest in enforce
    # mode is the caller's responsibility (it owns the decision_run_id and
    # the enforce setting); here we simply skip when there is nothing to
    # validate against.
    if resolved is not None:
        verdict.extend(check_headline_numeric_source(horizon_text, resolved))

    # S22 — cross-surface coherence + extraction-error fail-loud. Runs only
    # when the assembled whole-artifact is supplied.
    if artifact is not None:
        verdict.extend(check_cross_surface_coherence(artifact))
        # Fail-loud: a per-surface headline extraction that COLLAPSED must
        # surface as a violation, not pass vacuously (a collapsed surface
        # contributes no concept and would otherwise skip the coherence check
        # silently — the exact false-negative this gate prevents).
        for surface, err in (getattr(artifact, "extraction_errors", None) or {}).items():
            verdict.add(
                GateViolation(
                    check=GateCheck.CROSS_SURFACE_COHERENCE,
                    detail=(
                        f"surface `{surface}` headline-extraction FAILED ({err}) "
                        "— a collapsed surface cannot be coherence-checked and "
                        "must not pass vacuously"
                    ),
                    locator=surface,
                )
            )

    # Task 4 — FI-sufficiency-under-NVDA-shock. Composes the synth's
    # sufficiency claim with the NVDA concentration tail. Runs only when the
    # resolver supplies all four shock inputs as RESOLVED values; any missing
    # input skips the check (a gate must never crash the synthesis).
    if resolved is not None:
        try:
            shock_inputs = _derive_shock_inputs(resolved)
            if shock_inputs is not None:
                from argosy.services.retirement.fi_shock import (
                    fi_sufficiency_under_shock,
                )

                shock_result = fi_sufficiency_under_shock(**shock_inputs)
                plan_text = "\n\n".join(t for t in horizon_text.values() if t)
                verdict.extend(
                    check_fi_sufficiency_under_shock(
                        shock_result=shock_result, plan_text=plan_text
                    )
                )
        except Exception as exc:  # noqa: BLE001 — a gate must not crash synthesis
            log.warning("gate_plan_output.fi_shock_skipped err=%s", exc)

    # Task 5 — input freshness (currency). Skipped entirely when `today` is
    # absent (without a run date there is no age to compute).
    if today is not None and (snapshot_date is not None or analyst_report_dates):
        verdict.extend(
            check_input_freshness(
                today=today,
                snapshot_date=snapshot_date,
                analyst_report_dates=analyst_report_dates or {},
            )
        )

    return verdict


def _derive_shock_inputs(resolved: "ResolvedPlanNumbers") -> dict | None:
    """Derive the four ``fi_sufficiency_under_shock`` inputs from the resolver.

    Returns a kwargs dict (``net_worth_nis``, ``nvda_value_nis``,
    ``perpetuity_base_nis``, ``fi_total_nis``) when every required value is
    RESOLVED, else None (the check is then skipped — never crashes).

    Units:
      - ``portfolio.net_worth_nis`` / ``retirement.fi_target_nis`` (the
        perpetuity base) / ``retirement.fi_total_capital_nis`` are NIS.
      - ``concentration.nvda_current_pct`` is a FRACTION (0–1), so NVDA market
        value = net_worth × fraction directly (no ÷100).
    """

    def _resolved(key: str) -> float | None:
        rv = resolved.get(key)
        if rv is None or rv.status != "resolved" or rv.value is None:
            return None
        return float(rv.value)

    net_worth = _resolved("portfolio.net_worth_nis")
    perpetuity_base = _resolved("retirement.fi_target_nis")
    fi_total = _resolved("retirement.fi_total_capital_nis")
    nvda_frac = _resolved("concentration.nvda_current_pct")
    if None in (net_worth, perpetuity_base, fi_total, nvda_frac):
        return None
    return {
        "net_worth_nis": net_worth,
        "nvda_value_nis": net_worth * nvda_frac,  # fraction → no ÷100
        "perpetuity_base_nis": perpetuity_base,
        "fi_total_nis": fi_total,
    }
