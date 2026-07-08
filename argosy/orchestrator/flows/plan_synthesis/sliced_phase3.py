"""Sliced FULL phase-3 synthesis — two-stage skeleton + parallel expansion.

Design: docs/design/sliced_full_synthesis.md (third in the series after
corrective_resynthesis.md and corrective_patch_synthesis.md).

Stage A: one small skeleton call decides everything cross-cutting; the
deterministic skeleton gate (``argosy/quality/skeleton_gate.py``) runs
BEFORE fan-out (ONE retry with violations fed back, then a loud abort that
the ``run_synthesis`` wrapper degrades to the monolith).

Stage B: six parallel expansion calls (3 horizons + 3 per-horizon section
batches), each with its own transient-retry envelope; a dead slice never
kills siblings, and every completed slice persists IMMEDIATELY as a
``decision_phases`` sub-checkpoint keyed by the skeleton's sha256 — a run
retry re-runs ONLY dead slices.

Assembly: deterministic and lock-enforcing — every skeleton-locked field is
byte-restored regardless of what a slice emitted (the patch-mode honesty
guarantee); roster entries a slice omitted fail assembly LOUDLY; invented
items are dropped. Downstream (rewriter, speculation cap, phases 4/4.5/5,
reader, gates, corrections-check) is byte-for-byte today's pipeline and is
never told the artifact was sliced.

Selection lives in ``run_synthesis``: precedence corrective-PATCH >
sliced-FULL (``ARGOSY_SLICED_SYNTH=1``, default OFF) > monolith; any
stage-A/assembly exception degrades to the monolith; a dead slice after
retries fails phase 3 as today (``SliceExpansionError`` propagates) with
sub-checkpoints intact for resume.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from argosy.agents.base import AgentReport
from argosy.agents.plan_skeleton_synthesizer import PlanSkeleton
from argosy.agents.plan_slice_synthesizer import build_slice_shared_prefix
from argosy.agents.plan_synthesizer_types import (
    Action,
    Delta,
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SynthesisInputs,
    Theme,
)
from argosy.logging import get_logger
from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
    _build_resolved_numbers_block,
    _build_source_reliability_preamble,
    _decision_run_int,
    _pkg_build_prior_items_index,
    _sha256_text,
)
from argosy.quality.patch_reachability import HORIZONS, item_slug, parse_plan_item_ref
from argosy.quality.skeleton_gate import check_skeleton

log = get_logger(__name__)

# Slice names, fixed order: 3 horizon slices + up to 3 section batches.
_SECTION_SLICE_PREFIX = "sections_"


class SkeletonGateError(RuntimeError):
    """Skeleton failed the deterministic gate after the ONE retry.

    Stage-A failure — ``run_synthesis`` degrades to the monolith."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__(
            f"skeleton gate failed after retry: {len(violations)} "
            f"violation(s): " + "; ".join(violations[:5])
        )
        self.violations = violations


class SlicedAssemblyError(RuntimeError):
    """Deterministic assembly found a loud hole (omitted roster entry /
    invalid slice output). Stage-C failure — degrades to the monolith."""


class SliceExpansionError(RuntimeError):
    """One or more expansion slices died after their retry envelopes.

    This does NOT degrade to the monolith: phase 3 fails as today, but the
    completed siblings' sub-checkpoints are already persisted, so a resume
    re-runs only the dead slices (design §2.B)."""

    def __init__(self, failures: dict[str, str]) -> None:
        super().__init__(
            "sliced phase-3 expansion failed for slice(s): "
            + ", ".join(f"{k} ({v[:120]})" for k, v in sorted(failures.items()))
        )
        self.failures = failures


def _slice_retries() -> int:
    try:
        return int(os.environ.get("ARGOSY_SLICED_SLICE_RETRIES", "2"))
    except (TypeError, ValueError):
        return 2


def _coverage_floor() -> int:
    try:
        return int(os.environ.get("ARGOSY_SKELETON_COVERAGE_FLOOR", "12"))
    except (TypeError, ValueError):
        return 12


# ---------------------------------------------------------------------------
# Prompt suffix builders (the VARYING part — everything shared is in the
# byte-identical prefix, see plan_slice_synthesizer.build_slice_shared_prefix)
# ---------------------------------------------------------------------------


def _prior_items_rows(prior_items_index: list[dict], horizon: str | None) -> str:
    rows = [
        it for it in prior_items_index
        if horizon is None or (it.get("horizon") or "") == horizon
    ]
    return "\n".join(
        f"  - {it.get('item_id', '?')}  ({it.get('item_kind', '?')})  "
        f"label={it.get('label', '')!r}  value={it.get('value', '')} "
        f"{it.get('unit', '')}"
        for it in rows
    ) or "  (none)"


def _corrective_slice_block(corrective_ctx, group: str) -> str:
    """Corrections/directives relevant to one slice group (``long`` /
    ``medium`` / ``short`` / ``sections``). Ref-parse deterministic:
    refless or unparseable corrections go to every slice; directives
    (apply-verbatim) go to every slice."""
    if corrective_ctx is None:
        return ""
    from argosy.services.corrective_context import _fmt_value

    lines: list[str] = []
    for c in corrective_ctx.corrections:
        parsed = parse_plan_item_ref(getattr(c, "plan_item_ref", "") or "")
        if parsed.group is not None and parsed.group != group:
            continue
        canon = "; ".join(
            f"{k} = {_fmt_value(v)} (derived-fact)"
            for k, v in (c.canonical_facts or [])
        ) or "(no canonical derived value on file)"
        wrong = "; ".join(
            _fmt_value(v) for v in (c.wrong_values or [])
        ) or "(none listed)"
        lines.append(
            f"[{c.index}] {c.severity} · {c.topic} · surface: "
            f"{c.plan_item_ref}\n"
            f"    wrong: {c.summary}\n"
            f"    canonical (state these): {canon}\n"
            f"    MUST BE ABSENT from your output: {wrong}"
        )
    for d in corrective_ctx.directives:
        entry = (
            f"[D{d.index}] proposal #{d.proposal_id} — {d.summary} "
            "(adjudicated verdict — apply verbatim, do not re-decide)"
        )
        if d.detail:
            entry += f"\n     {d.detail}"
        if d.superseded_values:
            entry += (
                "\n     MUST BE ABSENT (superseded figures): "
                + "; ".join(_fmt_value(v) for v in d.superseded_values)
            )
        lines.append(entry)
    return "\n".join(lines)


def _horizon_assignment_block(
    *,
    horizon: str,
    skeleton: PlanSkeleton,
    prior_items_index: list[dict],
    corrective_ctx,
) -> str:
    deltas = [d for d in skeleton.delta_roster if d.horizon == horizon]
    delta_lines = "\n".join(
        f"  - {d.item_id}  ({d.item_kind}, {d.change_kind}): {d.summary}"
        for d in deltas
    ) or "  (none — reproduce an empty deltas_from_prior list)"
    parts = [
        f"Expand horizon {horizon!r} into a full HorizonSection.\n"
        f"Its decided skeleton is the {horizon!r} field of the PLAN "
        "SKELETON above. Reproduce every locked field exactly; write the "
        "prose fields.\n"
        "Delta roster for this horizon (emit EXACTLY these in "
        "deltas_from_prior, filling rationale/prior/proposed/citations):\n"
        + delta_lines,
        "=== PRIOR ITEMS INDEX rows for this horizon [reference — item_id "
        "stability only] ===\n"
        + _prior_items_rows(prior_items_index, horizon),
    ]
    corr = _corrective_slice_block(corrective_ctx, horizon)
    if corr:
        parts.append(
            "=== CORRECTIONS RELEVANT TO THIS SLICE (already resolved in "
            "the skeleton's numbers — your prose must state the canonical "
            "values and never the wrong ones) ===\n" + corr
        )
    return "\n\n".join(parts)


def _sections_assignment_block(
    *,
    horizon: str,
    skeleton: PlanSkeleton,
    corrective_ctx,
) -> str:
    roster = [e for e in skeleton.section_roster if e.horizon == horizon]
    entry_lines = []
    for e in roster:
        entry_lines.append(f"  - ({e.section_id}, {e.horizon}): {e.one_line_thesis}")
        for kf in e.key_facts:
            entry_lines.append(f"      key fact: {kf}")
    parts = [
        f"Write the Section objects for horizon {horizon!r} — one per "
        "roster entry below, in this order (section_id/horizon verbatim):\n"
        + "\n".join(entry_lines),
    ]
    corr = _corrective_slice_block(corrective_ctx, "sections")
    if corr:
        parts.append(
            "=== CORRECTIONS RELEVANT TO THIS SLICE (already resolved in "
            "the skeleton's numbers — your prose must state the canonical "
            "values and never the wrong ones) ===\n" + corr
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Deterministic, lock-enforcing assembly (design §2.C)
# ---------------------------------------------------------------------------


def _pair_roster(
    roster: list, emitted: list, key_fn, *, what: str, slice_name: str,
) -> tuple[list[tuple[Any, Any]], int]:
    """Pair skeleton roster entries with slice-emitted items.

    Primary join: the deterministic key (label slug / item_id / ticker /
    section_id). Fallback: positional pairing when counts match — an
    adversarial slice may mutate join keys, but every LOCKED field is
    restored from the skeleton either way, so positional pairing only
    decides which PROSE attaches where. An omitted roster entry (count
    short and no key match) raises loudly — never a silent hole. Returns
    (pairs, dropped_invented_count)."""
    remaining = list(emitted)
    pairs: list[tuple[Any, Any]] = []
    missing: list[str] = []
    for r in roster:
        k = key_fn(r)
        match = next((e for e in remaining if key_fn(e) == k), None)
        if match is not None:
            remaining.remove(match)
            pairs.append((r, match))
        else:
            pairs.append((r, None))
            missing.append(str(k))
    if missing and len(emitted) == len(roster):
        pairs = list(zip(roster, emitted, strict=True))
        missing = []
        remaining = []
    if missing:
        raise SlicedAssemblyError(
            f"slice {slice_name!r}: {what} roster entr(y/ies) "
            f"{missing} omitted by the expansion output — assembly fails "
            "loudly on omitted roster entries"
        )
    if remaining:
        log.warning(
            "plan_synthesis.sliced_assembly_dropped_inventions",
            slice=slice_name, kind=what, dropped=len(remaining),
        )
    return pairs, len(remaining)


def _count_lock_diffs(locked: dict, emitted: dict) -> int:
    return sum(1 for k, v in locked.items() if emitted.get(k) != v)


def _assemble_horizon(
    skeleton: PlanSkeleton, horizon: str, em: HorizonSection,
) -> tuple[HorizonSection, int]:
    """Build one HorizonSection: locked fields byte-restored from the
    skeleton; prose/evidence fields taken from the expansion output."""
    sk = getattr(skeleton, horizon)
    restored = 0

    # Horizon-level locks.
    restored += _count_lock_diffs(
        {
            "horizon": sk.horizon,
            "status": sk.status,
            "freshness_expected": sk.freshness_expected,
        },
        {
            "horizon": em.horizon,
            "status": em.status,
            "freshness_expected": em.freshness_expected,
        },
    )

    # Targets — everything except rationale is locked.
    targets = []
    t_pairs, _ = _pair_roster(
        sk.targets, em.targets, lambda t: item_slug(t.label),
        what="target", slice_name=horizon,
    )
    for sk_t, em_t in t_pairs:
        restored += _count_lock_diffs(
            sk_t.model_dump(exclude={"rationale"}),
            em_t.model_dump(exclude={"rationale"}),
        )
        targets.append(sk_t.model_copy(update={"rationale": em_t.rationale}))

    # Themes — label/direction locked; rationale + citations expand.
    themes = []
    th_pairs, _ = _pair_roster(
        sk.theme_roster, em.themes, lambda t: item_slug(t.label),
        what="theme", slice_name=horizon,
    )
    for sk_t, em_t in th_pairs:
        restored += _count_lock_diffs(
            {"label": sk_t.label, "direction": sk_t.direction},
            {"label": em_t.label, "direction": em_t.direction},
        )
        themes.append(Theme(
            label=sk_t.label, direction=sk_t.direction,
            rationale=em_t.rationale, cited_sources=em_t.cited_sources,
        ))

    # Actions — label/kind/trigger locked; detail/rationale/how_to/
    # done_when/citations expand.
    actions = []
    a_pairs, _ = _pair_roster(
        sk.action_roster, em.actions, lambda a: item_slug(a.label),
        what="action", slice_name=horizon,
    )
    for sk_a, em_a in a_pairs:
        restored += _count_lock_diffs(
            {
                "label": sk_a.label,
                "horizon_kind": sk_a.horizon_kind,
                "trigger_or_date": sk_a.trigger_or_date,
            },
            {
                "label": em_a.label,
                "horizon_kind": em_a.horizon_kind,
                "trigger_or_date": em_a.trigger_or_date,
            },
        )
        actions.append(Action(
            label=sk_a.label, horizon_kind=sk_a.horizon_kind,
            trigger_or_date=sk_a.trigger_or_date,
            detail=em_a.detail, rationale=em_a.rationale,
            cited_sources=em_a.cited_sources,
            how_to=em_a.how_to, done_when=em_a.done_when,
        ))

    # Speculative candidates — ticker + every numeric field + the ceiling
    # check locked; thesis/exit_trigger/sourced_from expand.
    cands = []
    c_pairs, _ = _pair_roster(
        sk.speculative_candidates, em.speculative_candidates,
        lambda c: (c.ticker or "").upper(),
        what="speculative_candidate", slice_name=horizon,
    )
    _cand_expansion = {"thesis_summary", "exit_trigger", "sourced_from"}
    for sk_c, em_c in c_pairs:
        restored += _count_lock_diffs(
            sk_c.model_dump(exclude=_cand_expansion),
            em_c.model_dump(exclude=_cand_expansion),
        )
        cands.append(sk_c.model_copy(update={
            "thesis_summary": em_c.thesis_summary,
            "exit_trigger": em_c.exit_trigger,
            "sourced_from": em_c.sourced_from,
        }))

    # Deltas — the roster's structural fields locked; rationale/prior/
    # proposed/citations expand; accepted/user_edited pinned to defaults.
    delta_roster = [d for d in skeleton.delta_roster if d.horizon == horizon]
    deltas = []
    d_pairs, _ = _pair_roster(
        delta_roster, em.deltas_from_prior, lambda d: d.item_id,
        what="delta", slice_name=horizon,
    )
    for sk_d, em_d in d_pairs:
        restored += _count_lock_diffs(
            {
                "item_kind": sk_d.item_kind, "item_id": sk_d.item_id,
                "horizon": sk_d.horizon, "change_kind": sk_d.change_kind,
                "summary": sk_d.summary,
            },
            {
                "item_kind": em_d.item_kind, "item_id": em_d.item_id,
                "horizon": em_d.horizon, "change_kind": em_d.change_kind,
                "summary": em_d.summary,
            },
        )
        deltas.append(Delta(
            item_kind=sk_d.item_kind, item_id=sk_d.item_id,
            horizon=sk_d.horizon, change_kind=sk_d.change_kind,
            summary=sk_d.summary,
            rationale=em_d.rationale, prior=em_d.prior,
            proposed=em_d.proposed, cited_sources=em_d.cited_sources,
        ))

    assembled = HorizonSection(
        horizon=sk.horizon,
        freshness_expected=sk.freshness_expected,
        status=sk.status,
        posture=em.posture,
        targets=targets,
        themes=themes,
        actions=actions,
        speculative_candidates=cands,
        deltas_from_prior=deltas,
        rationale=em.rationale,
        cited_sources=em.cited_sources,
    )
    return assembled, restored


def _assemble_sliced_output(
    *,
    skeleton: PlanSkeleton,
    horizon_outputs: dict[str, HorizonSection],
    section_outputs: dict[str, list[Section]],
) -> tuple[PlanSynthesisOutput, int]:
    """Deterministic assembly (design §2.C). Returns
    ``(output, lock_restoration_count)``. Raises ``SlicedAssemblyError``
    on any omitted roster entry or invalid slice output."""
    import json as _json

    restored_total = 0
    horizon_updates: dict[str, HorizonSection] = {}
    for h in HORIZONS:
        em = horizon_outputs.get(h)
        if em is None:
            raise SlicedAssemblyError(f"horizon slice {h!r} output missing")
        assembled, restored = _assemble_horizon(skeleton, h, em)
        horizon_updates[h] = assembled
        restored_total += restored

    # Sections — pair per horizon batch, identity pinned to the roster.
    assembled_by_key: dict[tuple[str, str, int], Section] = {}
    for h in HORIZONS:
        roster = [e for e in skeleton.section_roster if e.horizon == h]
        emitted = list(section_outputs.get(h, []))
        if not roster:
            if emitted:
                log.warning(
                    "plan_synthesis.sliced_assembly_dropped_inventions",
                    slice=f"sections_{h}", kind="section",
                    dropped=len(emitted),
                )
            continue
        pairs, _ = _pair_roster(
            roster, emitted, lambda x: getattr(x, "section_id", None),
            what="section", slice_name=f"sections_{h}",
        )
        for i, (sk_s, em_s) in enumerate(pairs):
            restored_total += _count_lock_diffs(
                {"section_id": sk_s.section_id, "horizon": sk_s.horizon},
                {"section_id": em_s.section_id, "horizon": em_s.horizon},
            )
            assembled_by_key[(sk_s.section_id, sk_s.horizon, i)] = (
                em_s.model_copy(update={
                    "section_id": sk_s.section_id, "horizon": sk_s.horizon,
                })
            )

    sections: list[Section] = []
    counters: dict[tuple[str, str], int] = {}
    for e in skeleton.section_roster:
        i = counters.get((e.section_id, e.horizon), 0)
        counters[(e.section_id, e.horizon)] = i + 1
        s = assembled_by_key.get((e.section_id, e.horizon, i))
        if s is None:
            raise SlicedAssemblyError(
                f"section roster entry ({e.section_id}, {e.horizon}) has "
                "no assembled output"
            )
        sections.append(s)

    output = PlanSynthesisOutput(
        long=horizon_updates["long"],
        medium=horizon_updates["medium"],
        short=horizon_updates["short"],
        inputs=SynthesisInputs(),
        sections=sections,
    )
    # Whole-artifact pydantic round-trip — an assembly bug fails loud here,
    # not downstream (same discipline as _merge_patched_output).
    return (
        PlanSynthesisOutput.model_validate(
            _json.loads(output.model_dump_json())
        ),
        restored_total,
    )


# ---------------------------------------------------------------------------
# The phase-3 runner
# ---------------------------------------------------------------------------


def _run_phase_3_sliced(
    *,
    session,
    user_id: str,
    baseline,
    prior_current,
    analyst_reports_text: str,
    debate_outcomes_text: str,
    portfolio_summary: str,
    fills_summary: str,
    decision_run_id,
    decision_run_int: int | None = None,
    speculation_cap_pct: float | None = None,
    speculation_cap_concurrent: int | None = None,
    guidance: str = "",
    corrective_ctx=None,
) -> tuple[PlanSynthesisOutput, list[AgentReport], dict]:
    """Two-stage sliced phase 3 (design §2). Raises:

    * ``SkeletonGateError`` / ``SlicedAssemblyError`` / any other
      stage-A/assembly exception → the caller degrades to the monolith;
    * ``SliceExpansionError`` → phase 3 fails as today (the caller
      re-raises), with completed-slice sub-checkpoints intact for resume.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    drun_int = decision_run_int
    if drun_int is None:
        drun_int = _decision_run_int(decision_run_id)

    # Shared inputs (mirrors _run_phase_3_synthesizer).
    baseline_md = getattr(baseline, "distillate_rendered", None) or (
        "(no distillate available)"
    )
    prior_items_index = _pkg_build_prior_items_index(
        session, user_id=user_id, prior_current=prior_current,
    )
    reliability_preamble = _build_source_reliability_preamble(session, user_id)
    weighted_analyst_reports_text = reliability_preamble + analyst_reports_text
    resolved_numbers_block = _build_resolved_numbers_block(
        session, user_id=user_id, decision_run_id=decision_run_id,
    )

    def _resolve_manifest():
        try:
            from argosy.services.plan_numeric_resolver import (
                resolve_plan_numbers,
            )
            if drun_int is None:
                return None
            return resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=drun_int,
                include_canonical_ages=True,
            )
        except Exception as exc:  # noqa: BLE001 — gate degrades gracefully
            log.warning(
                "plan_synthesis.sliced_manifest_failed",
                user_id=user_id, error=str(exc),
            )
            return None

    manifest = _resolve_manifest()

    # Resume: previously-persisted sub-checkpoints for THIS decision run
    # (a prior attempt that died mid-fan-out). Best-effort.
    existing: dict[str, dict] = {}
    if drun_int is not None:
        try:
            existing = _pkg._load_phase3_sub_checkpoints(
                session, decision_run_id=drun_int,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plan_synthesis.sliced_checkpoint_load_failed",
                user_id=user_id, error=str(exc),
            )
            existing = {}

    collected: list[AgentReport] = []

    def _collect(report) -> None:
        if isinstance(report, AgentReport):
            collected.append(report)

    # ---- Stage A — skeleton (+ deterministic gate BEFORE any fan-out) ----
    skeleton: PlanSkeleton | None = None
    skeleton_resumed = False
    gate_attempts = 0
    first_violations: list[str] = []

    def _call_skeleton(violations_block: str = "") -> PlanSkeleton:
        agent = _pkg.PlanSkeletonSynthesizerAgent(user_id=user_id)
        report = agent.run_sync(
            baseline_distillate_md=baseline_md,
            analyst_reports_text=weighted_analyst_reports_text,
            debate_outcomes_text=debate_outcomes_text,
            portfolio_snapshot_summary=portfolio_summary,
            recent_fills_summary=fills_summary,
            speculation_cap_pct=speculation_cap_pct,
            speculation_cap_concurrent=speculation_cap_concurrent,
            prior_items_index=prior_items_index,
            user_directive=guidance,
            resolved_numbers_block=resolved_numbers_block,
            gate_violations_block=violations_block,
            decision_id=decision_run_id,
        )
        _collect(report)
        return report.output

    def _gate(sk: PlanSkeleton):
        corrections = []
        directives = []
        if corrective_ctx is not None:
            corrections = [c.check_payload() for c in corrective_ctx.corrections]
            directives = [d.check_payload() for d in corrective_ctx.directives]
        return check_skeleton(
            skeleton=sk,
            resolved=manifest,
            corrections=corrections,
            directives=directives,
            prior_item_ids={
                str(it.get("item_id"))
                for it in prior_items_index
                if it.get("item_id")
            },
            coverage_floor=_coverage_floor(),
            speculation_cap_pct=speculation_cap_pct,
            speculation_cap_concurrent=speculation_cap_concurrent,
        )

    # Resume a previously-persisted skeleton — but RE-GATE it (codex sliced
    # review blocker #1): the checkpoint was persisted post-gate, yet the
    # gate's INPUTS may have moved between attempts (fresh corrective
    # corrections, a newly-resolved manifest), an older/foreign row could
    # predate the gate, and the gate is deterministic + free. Fan-out must
    # NEVER start on a skeleton the gate has not passed in THIS run.
    cp = existing.get("skeleton")
    if cp and cp.get("skeleton_json"):
        try:
            candidate = PlanSkeleton.model_validate_json(cp["skeleton_json"])
            resumed_gate = _gate(candidate)
            if resumed_gate.passes:
                skeleton = candidate
                skeleton_resumed = True
                log.info(
                    "plan_synthesis.sliced_skeleton_resumed",
                    user_id=user_id, decision_run_id=decision_run_id,
                )
            else:
                log.warning(
                    "plan_synthesis.sliced_skeleton_checkpoint_regate_failed",
                    user_id=user_id, decision_run_id=decision_run_id,
                    violations=resumed_gate.violations,
                )
        except Exception as exc:  # noqa: BLE001 — corrupt checkpoint → fresh
            log.warning(
                "plan_synthesis.sliced_skeleton_checkpoint_corrupt",
                user_id=user_id, error=str(exc),
            )
            skeleton = None

    if skeleton is None:
        gate_attempts = 1
        skeleton = _call_skeleton()
        gate = _gate(skeleton)
        if not gate.passes:
            # ONE retry with the violations fed back (design §2.A), then a
            # loud abort — which run_synthesis degrades to the monolith.
            first_violations = list(gate.violations)
            log.warning(
                "plan_synthesis.sliced_skeleton_gate_retry",
                user_id=user_id, decision_run_id=decision_run_id,
                violations=len(gate.violations),
            )
            gate_attempts = 2
            skeleton = _call_skeleton(
                violations_block=gate.render_violations_block(),
            )
            gate = _gate(skeleton)
            if not gate.passes:
                log.error(
                    "plan_synthesis.sliced_skeleton_gate_failed",
                    user_id=user_id, decision_run_id=decision_run_id,
                    violations=gate.violations,
                )
                raise SkeletonGateError(gate.violations)

    skeleton_json = skeleton.model_dump_json()
    skeleton_hash = _sha256_text(skeleton_json)

    # Persist the (gate-passed) skeleton sub-checkpoint immediately.
    if not skeleton_resumed and drun_int is not None:
        _pkg._record_phase3_sub_checkpoint(
            user_id=user_id, decision_run_id=drun_int,
            suffix="skeleton",
            payload={
                "skeleton_json": skeleton_json,
                "skeleton_sha256": skeleton_hash,
                "gate_attempts": gate_attempts,
            },
        )

    # ---- Stage B — six-way parallel expansion --------------------------
    shared_prefix = build_slice_shared_prefix(
        user_directive=guidance,
        portfolio_snapshot_summary=portfolio_summary,
        analyst_reports_text=weighted_analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        resolved_numbers_block=resolved_numbers_block,
        skeleton_json=skeleton_json,
    )

    slice_names: list[str] = list(HORIZONS) + [
        f"{_SECTION_SLICE_PREFIX}{h}" for h in HORIZONS
        if any(e.horizon == h for e in skeleton.section_roster)
    ]

    def _call_slice(name: str):
        if name in HORIZONS:
            agent = _pkg.PlanHorizonSliceSynthesizerAgent(user_id=user_id)
            report = agent.run_sync(
                shared_prefix=shared_prefix,
                assignment_block=_horizon_assignment_block(
                    horizon=name, skeleton=skeleton,
                    prior_items_index=prior_items_index,
                    corrective_ctx=corrective_ctx,
                ),
                decision_id=decision_run_id,
            )
        else:
            h = name[len(_SECTION_SLICE_PREFIX):]
            agent = _pkg.PlanSectionBatchSliceSynthesizerAgent(
                user_id=user_id,
            )
            report = agent.run_sync(
                shared_prefix=shared_prefix,
                assignment_block=_sections_assignment_block(
                    horizon=h, skeleton=skeleton,
                    corrective_ctx=corrective_ctx,
                ),
                decision_id=decision_run_id,
            )
        return report

    max_retries = _slice_retries()

    def _run_with_retry(name: str):
        """Per-slice retry envelope (design §2.B): retries on the transient
        classes (sdk_timeout / malformed JSON / exit-1 all surface as
        exceptions from run_sync) ON TOP of the SDK's internal retries."""
        attempt = 0
        while True:
            try:
                return _call_slice(name), attempt
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt > max_retries:
                    raise
                log.warning(
                    "plan_synthesis.sliced_slice_retry",
                    user_id=user_id, decision_run_id=decision_run_id,
                    slice=name, attempt=attempt, error=str(exc)[:300],
                )

    def _parse_slice_output(name: str, output_json: str):
        if name in HORIZONS:
            return HorizonSection.model_validate_json(output_json)
        from argosy.agents.plan_slice_synthesizer import SectionBatch
        return SectionBatch.model_validate_json(output_json)

    results: dict[str, Any] = {}
    slice_provenance: dict[str, dict] = {}
    to_run: list[str] = []
    for name in slice_names:
        cp = existing.get(f"slice.{name}")
        if (
            cp
            and cp.get("skeleton_sha256") == skeleton_hash
            and cp.get("output_json")
        ):
            try:
                results[name] = _parse_slice_output(name, cp["output_json"])
                slice_provenance[name] = {
                    "sha256": _sha256_text(cp["output_json"]),
                    "retries": int(cp.get("retries") or 0),
                    "resumed": True,
                }
                log.info(
                    "plan_synthesis.sliced_slice_resumed",
                    user_id=user_id, decision_run_id=decision_run_id,
                    slice=name,
                )
                continue
            except Exception as exc:  # noqa: BLE001 — corrupt → re-run
                log.warning(
                    "plan_synthesis.sliced_slice_checkpoint_corrupt",
                    user_id=user_id, slice=name, error=str(exc),
                )
        to_run.append(name)

    failures: dict[str, str] = {}
    if to_run:
        log.info(
            "plan_synthesis.sliced_fanout_start",
            user_id=user_id, decision_run_id=decision_run_id,
            slices=to_run, resumed=sorted(results.keys()),
        )
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                ex.submit(_run_with_retry, name): name for name in to_run
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    report, retries = fut.result()
                except Exception as exc:  # noqa: BLE001 — dead slice; salvage siblings
                    failures[name] = str(exc)
                    log.error(
                        "plan_synthesis.sliced_slice_dead",
                        user_id=user_id, decision_run_id=decision_run_id,
                        slice=name, error=str(exc)[:500],
                    )
                    continue
                _collect(report)
                results[name] = report.output
                output_json = report.output.model_dump_json()
                slice_provenance[name] = {
                    "sha256": _sha256_text(output_json),
                    "retries": retries,
                    "resumed": False,
                }
                # Persist IMMEDIATELY — K-of-N partial completion is never
                # lost work again (design §2.B).
                if drun_int is not None:
                    _pkg._record_phase3_sub_checkpoint(
                        user_id=user_id, decision_run_id=drun_int,
                        suffix=f"slice.{name}",
                        payload={
                            "skeleton_sha256": skeleton_hash,
                            "output_json": output_json,
                            "retries": retries,
                            "slice": name,
                        },
                    )

    if collected:
        _pkg._persist_agent_reports(session, collected)

    if failures:
        raise SliceExpansionError(failures)

    # ---- Assembly — deterministic, lock-enforcing -----------------------
    horizon_outputs = {h: results[h] for h in HORIZONS}
    section_outputs = {
        h: list(results[f"{_SECTION_SLICE_PREFIX}{h}"].sections)
        for h in HORIZONS
        if f"{_SECTION_SLICE_PREFIX}{h}" in results
    }
    output, lock_restorations = _assemble_sliced_output(
        skeleton=skeleton,
        horizon_outputs=horizon_outputs,
        section_outputs=section_outputs,
    )

    provenance = {
        "skeleton_sha256": skeleton_hash,
        "skeleton_resumed": skeleton_resumed,
        "skeleton_gate": {
            "attempts": gate_attempts,
            "first_attempt_violations": first_violations,
        },
        "slices": slice_provenance,
        "lock_restorations": lock_restorations,
    }
    log.info(
        "plan_synthesis.sliced_phase_3_assembled",
        user_id=user_id, decision_run_id=decision_run_id,
        skeleton_sha256=skeleton_hash[:12],
        slices=sorted(slice_provenance.keys()),
        lock_restorations=lock_restorations,
    )
    return output, collected, provenance


__all__ = [
    "SkeletonGateError",
    "SliceExpansionError",
    "SlicedAssemblyError",
    "_assemble_sliced_output",
    "_run_phase_3_sliced",
]
