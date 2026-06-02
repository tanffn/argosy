"""Invariant validator for ``PlanLanguageRewriter``.

Phase 2 of docs/plans/argosy-comprehensive-plan-integration.md.

The rewriter is allowed to rephrase prose fields (label / detail /
rationale / posture) but MUST NOT touch structured fields (item_id,
numeric Target values, units, dates, cited_sources, deltas, etc.).
This validator runs immediately after the rewriter and emits a
``GateViolation`` for every divergence. Any violation aborts the
synthesis cycle — the spec's "structure preservation" rule is
load-bearing.

It also re-runs ``check_history_leak`` and ``check_jargon_leak`` on
every rewritten prose field — if the rewriter introduced new leaks
(or failed to clean existing ones), the violation is reported with
the same banlist the Phase-0 gate uses.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from argosy.quality.gate_types import GateCheck, GateViolation
from argosy.quality.plan_output_gate import (
    check_history_leak,
    check_jargon_leak,
)

if TYPE_CHECKING:
    from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput


HORIZONS: tuple[str, ...] = ("long", "medium", "short")


# HorizonSection-level fields that must be bit-equal between input and output.
PRESERVED_HORIZON_FIELDS: tuple[str, ...] = (
    "horizon",
    "freshness_expected",
    "status",
    "cited_sources",
)

# Per-target fields the rewriter must not touch (numeric + dates +
# attribution + structured pointer).
PRESERVED_TARGET_FIELDS: tuple[str, ...] = (
    "value",
    "unit",
    "stated_at",
    "revisit_after",
    "source_section",
)

# Per-theme fields preserved bit-for-bit.
PRESERVED_THEME_FIELDS: tuple[str, ...] = (
    "direction",
    "cited_sources",
)

# Per-action fields preserved bit-for-bit (the trigger_or_date stays
# in the structured slot even though its prose paraphrase may land in
# `detail` / `rationale`).
PRESERVED_ACTION_FIELDS: tuple[str, ...] = (
    "horizon_kind",
    "trigger_or_date",
    "cited_sources",
)


def _section(plan: PlanSynthesisOutput, horizon: str) -> Any:
    """Return the HorizonSection for the named horizon. Lifted out
    so test stubs can swap it for a SimpleNamespace tree."""
    return getattr(plan, horizon)


def _bit_equal(a: Any, b: Any) -> bool:
    """True if two Pydantic / dataclass / scalar values are bit-equal.

    Falls back to ``model_dump()`` for Pydantic models so two
    instances that mutate-share but render identical compare equal.
    """
    if hasattr(a, "model_dump") and hasattr(b, "model_dump"):
        return a.model_dump() == b.model_dump()
    return a == b


def _check_prose(text: str | None, locator: str) -> list[GateViolation]:
    """Run jargon + history gates on a single rewritten prose field.

    Returns violations bound to the locator so the orchestrator can
    point to which field broke the contract.
    """
    if not text:
        return []
    out: list[GateViolation] = []
    for v in check_history_leak(text):
        out.append(
            GateViolation(
                check=GateCheck.HISTORY_LEAK,
                detail=v.detail,
                locator=locator,
            )
        )
    for v in check_jargon_leak(text):
        out.append(
            GateViolation(
                check=GateCheck.JARGON_LEAK,
                detail=v.detail,
                locator=locator,
            )
        )
    return out


def _check_horizon_counts_and_preserved(
    horizon_name: str,
    before: Any,
    after: Any,
) -> list[GateViolation]:
    """Section-level count + preserved-field checks for one horizon."""
    violations: list[GateViolation] = []
    list_attrs = (
        "targets",
        "themes",
        "actions",
        "speculative_candidates",
        "deltas_from_prior",
    )
    for attr in list_attrs:
        b = getattr(before, attr, []) or []
        a = getattr(after, attr, []) or []
        if len(b) != len(a):
            violations.append(
                GateViolation(
                    check=GateCheck.JARGON_LEAK,  # invariant-class violation
                    detail=(
                        f"rewriter changed {horizon_name}.{attr} count: "
                        f"{len(b)} -> {len(a)} (structural preservation)"
                    ),
                    locator=f"{horizon_name}.{attr}",
                )
            )
    for f in PRESERVED_HORIZON_FIELDS:
        if not _bit_equal(getattr(before, f, None), getattr(after, f, None)):
            violations.append(
                GateViolation(
                    check=GateCheck.JARGON_LEAK,
                    detail=(
                        f"rewriter modified preserved field "
                        f"{horizon_name}.{f}"
                    ),
                    locator=f"{horizon_name}.{f}",
                )
            )
    # speculative_candidates is preserved as a whole subtree — any
    # divergence in any item (when counts match) is a violation.
    bsc = getattr(before, "speculative_candidates", []) or []
    asc = getattr(after, "speculative_candidates", []) or []
    if len(bsc) == len(asc):
        for i, (b, a) in enumerate(zip(bsc, asc)):
            if not _bit_equal(b, a):
                violations.append(
                    GateViolation(
                        check=GateCheck.JARGON_LEAK,
                        detail=(
                            f"rewriter modified speculative_candidates"
                            f"[{i}] (subtree preserved bit-for-bit)"
                        ),
                        locator=f"{horizon_name}.speculative_candidates[{i}]",
                    )
                )
    # deltas_from_prior is preserved as a whole subtree too.
    bdp = getattr(before, "deltas_from_prior", []) or []
    adp = getattr(after, "deltas_from_prior", []) or []
    if len(bdp) == len(adp):
        for i, (b, a) in enumerate(zip(bdp, adp)):
            if not _bit_equal(b, a):
                violations.append(
                    GateViolation(
                        check=GateCheck.JARGON_LEAK,
                        detail=(
                            f"rewriter modified deltas_from_prior[{i}]"
                        ),
                        locator=f"{horizon_name}.deltas_from_prior[{i}]",
                    )
                )
    return violations


def _check_per_item_preserved(
    horizon_name: str,
    before: Any,
    after: Any,
) -> list[GateViolation]:
    """Per-item preserved-field checks (positional zip; counts
    already validated by caller)."""
    violations: list[GateViolation] = []
    # Targets — numeric + dates + attribution
    for i, (bt, at) in enumerate(
        zip(getattr(before, "targets", []) or [], getattr(after, "targets", []) or [])
    ):
        for f in PRESERVED_TARGET_FIELDS:
            if not _bit_equal(getattr(bt, f, None), getattr(at, f, None)):
                label = getattr(bt, "label", f"target[{i}]")
                violations.append(
                    GateViolation(
                        check=GateCheck.JARGON_LEAK,
                        detail=(
                            f"rewriter modified preserved field "
                            f"{horizon_name}.target[{label!r}].{f}"
                        ),
                        locator=f"{horizon_name}.targets[{i}].{f}",
                    )
                )
    # Themes — direction, cited_sources
    for i, (bth, ath) in enumerate(
        zip(getattr(before, "themes", []) or [], getattr(after, "themes", []) or [])
    ):
        for f in PRESERVED_THEME_FIELDS:
            if not _bit_equal(getattr(bth, f, None), getattr(ath, f, None)):
                violations.append(
                    GateViolation(
                        check=GateCheck.JARGON_LEAK,
                        detail=(
                            f"rewriter modified preserved field "
                            f"{horizon_name}.theme[{i}].{f}"
                        ),
                        locator=f"{horizon_name}.themes[{i}].{f}",
                    )
                )
    # Actions — horizon_kind, trigger_or_date, cited_sources
    for i, (ba, aa) in enumerate(
        zip(getattr(before, "actions", []) or [], getattr(after, "actions", []) or [])
    ):
        for f in PRESERVED_ACTION_FIELDS:
            if not _bit_equal(getattr(ba, f, None), getattr(aa, f, None)):
                violations.append(
                    GateViolation(
                        check=GateCheck.JARGON_LEAK,
                        detail=(
                            f"rewriter modified preserved field "
                            f"{horizon_name}.action[{i}].{f}"
                        ),
                        locator=f"{horizon_name}.actions[{i}].{f}",
                    )
                )
    return violations


def _check_rewritten_prose(
    horizon_name: str,
    after: Any,
) -> list[GateViolation]:
    """Scan every rewritten prose field for residual jargon / history.

    Covers all paths enumerated in plan v3.1 §5.2:
    posture, rationale, theme.label/rationale, action.label/detail/rationale,
    target.label/rationale. Phase 3 will add section.title / body_md.
    """
    violations: list[GateViolation] = []
    base = f"{horizon_name}"
    violations.extend(
        _check_prose(getattr(after, "posture", None), f"{base}.posture")
    )
    violations.extend(
        _check_prose(getattr(after, "rationale", None), f"{base}.rationale")
    )
    for i, t in enumerate(getattr(after, "themes", []) or []):
        violations.extend(
            _check_prose(getattr(t, "label", None), f"{base}.themes[{i}].label")
        )
        violations.extend(
            _check_prose(getattr(t, "rationale", None), f"{base}.themes[{i}].rationale")
        )
    for i, a in enumerate(getattr(after, "actions", []) or []):
        violations.extend(
            _check_prose(getattr(a, "label", None), f"{base}.actions[{i}].label")
        )
        violations.extend(
            _check_prose(getattr(a, "detail", None), f"{base}.actions[{i}].detail")
        )
        violations.extend(
            _check_prose(getattr(a, "rationale", None), f"{base}.actions[{i}].rationale")
        )
    for i, t in enumerate(getattr(after, "targets", []) or []):
        violations.extend(
            _check_prose(getattr(t, "label", None), f"{base}.targets[{i}].label")
        )
        violations.extend(
            _check_prose(getattr(t, "rationale", None), f"{base}.targets[{i}].rationale")
        )
    # Phase 3: when Section / SectionEvidence land on PlanSynthesisOutput,
    # extend here to cover section.title + section.body_md. The hasattr
    # guard below keeps this validator Phase-3-ready without crashing
    # on today's HorizonSection shape.
    sections = getattr(after, "sections", None)
    if sections:
        for s in sections:
            sid = getattr(s, "section_id", "<unknown>")
            for f in ("title", "body_md"):
                violations.extend(
                    _check_prose(getattr(s, f, None), f"section[{sid}].{f}")
                )
    return violations


def validate_rewriter_invariants(
    before: PlanSynthesisOutput,
    after: PlanSynthesisOutput,
) -> list[GateViolation]:
    """Compare a rewriter input/output pair against the §5.2 contract.

    Returns ``[]`` when the rewriter respected every invariant.
    Otherwise returns one ``GateViolation`` per drift. The caller
    (orchestrator) aborts the synthesis cycle on any violation.
    """
    violations: list[GateViolation] = []
    for horizon in HORIZONS:
        b = _section(before, horizon)
        a = _section(after, horizon)
        violations.extend(_check_horizon_counts_and_preserved(horizon, b, a))
        violations.extend(_check_per_item_preserved(horizon, b, a))
        violations.extend(_check_rewritten_prose(horizon, a))
    # Phase 3 evidence subtree preserved (no-op until the field lands).
    for horizon in HORIZONS:
        b = _section(before, horizon)
        a = _section(after, horizon)
        if hasattr(b, "evidence") and hasattr(a, "evidence"):
            if not _bit_equal(getattr(b, "evidence"), getattr(a, "evidence")):
                violations.append(
                    GateViolation(
                        check=GateCheck.JARGON_LEAK,
                        detail=f"{horizon}.evidence subtree modified",
                        locator=f"{horizon}.evidence",
                    )
                )
    # PlanSynthesisOutput.inputs is structured provenance (baseline_id,
    # prior_current_id, snapshot_id, fill_ids, agent_report_ids,
    # debate_outcome_ids, decision_run_id) — the rewriter MUST NOT
    # touch it. Preserved bit-for-bit.
    if not _bit_equal(
        getattr(before, "inputs", None),
        getattr(after, "inputs", None),
    ):
        violations.append(
            GateViolation(
                check=GateCheck.JARGON_LEAK,
                detail="rewriter modified PlanSynthesisOutput.inputs (provenance)",
                locator="inputs",
            )
        )
    # Phase 3: top-level `sections: list[Section]` shape. When that
    # field lands on PlanSynthesisOutput, the rewriter must preserve
    # section_id / horizon / evidence per-section bit-for-bit and may
    # only rewrite section.title / section.body_md. The prose scan in
    # `_check_rewritten_prose` already covers title/body_md for the
    # after side; this block adds the structural-preservation half so
    # count, ids, and evidence subtree are bit-equal across the pair.
    before_sections = getattr(before, "sections", None)
    after_sections = getattr(after, "sections", None)
    if before_sections is not None or after_sections is not None:
        before_sections = before_sections or []
        after_sections = after_sections or []
        if len(before_sections) != len(after_sections):
            violations.append(
                GateViolation(
                    check=GateCheck.JARGON_LEAK,
                    detail=(
                        f"rewriter changed top-level sections count: "
                        f"{len(before_sections)} -> {len(after_sections)}"
                    ),
                    locator="sections",
                )
            )
        else:
            for i, (bs, as_) in enumerate(zip(before_sections, after_sections)):
                for f in ("section_id", "horizon"):
                    if not _bit_equal(getattr(bs, f, None), getattr(as_, f, None)):
                        violations.append(
                            GateViolation(
                                check=GateCheck.JARGON_LEAK,
                                detail=(
                                    f"rewriter modified preserved field "
                                    f"sections[{i}].{f}"
                                ),
                                locator=f"sections[{i}].{f}",
                            )
                        )
                # SectionEvidence preserved subtree.
                if hasattr(bs, "evidence") and hasattr(as_, "evidence"):
                    if not _bit_equal(
                        getattr(bs, "evidence"),
                        getattr(as_, "evidence"),
                    ):
                        violations.append(
                            GateViolation(
                                check=GateCheck.JARGON_LEAK,
                                detail=(
                                    f"sections[{i}].evidence subtree "
                                    "modified (preserved bit-for-bit)"
                                ),
                                locator=f"sections[{i}].evidence",
                            )
                        )
    return violations


__all__ = ["validate_rewriter_invariants"]
