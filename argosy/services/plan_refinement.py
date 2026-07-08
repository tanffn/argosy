"""Service helpers for the living-plan refinement / apply path.

``create_refinement_draft`` is the single entry point that APPLIES a
set of sleeve-target overrides by creating a staged DRAFT PlanVersion.
It is intentionally narrow:

  - Only allocation sleeve-target overrides (dict[label, pct]) are accepted.
  - The draft carries the full merged overrides AND the resolved
    ``target_allocation_json`` so every surface can project from it.
  - Promotion is NEVER performed here; that remains the gated
    ``POST /api/plan/draft/{id}/accept`` path.
  - Validate-on-write: ``resolve_target_allocation_json`` must succeed
    before any DB write occurs.  A ValueError from the engine propagates
    as a clear HTTPException(400) at the route layer.

Design mirrors argosy/orchestrator/flows/plan_amendment/dispatcher.py's
small-amendment draft-creation shape (role='draft', derived_from_id,
carry horizon_*/sections_json from current, decision_run_id=None for a
scoped edit without an agent run).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from argosy.logging import get_logger

log = get_logger(__name__)


def _fixed_sleeves_from_current(current) -> tuple[float, tuple]:
    """Reconstruct the team-sourced HIGH-GROWTH sleeve (pct, instruments) from the
    current plan's stored ``target_allocation_json`` so a scoped refinement draft
    CARRIES it instead of silently dropping it.

    Why: ``build_target_allocation`` only emits the high-growth class when
    ``high_growth_pct`` is passed explicitly; the sleeve is team-sourced (not an
    engine constant), so the stored doc of the plan being refined is its source
    of truth. Without this, any refinement on top of a plan carrying the sleeve
    (e.g. v64's 5% moonshot basket) would emit a draft WITHOUT it — a silent,
    unauthored plan change. Returns ``(0.0, ())`` when the current plan has no
    high-growth class (byte-identical legacy behaviour).
    """
    from argosy.services.allocation_plan import HIGH_GROWTH_SIGMA_CLASS
    from argosy.services.target_allocation_doc import AllocationInstrument

    raw = getattr(current, "target_allocation_json", None)
    if not raw:
        return 0.0, ()
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return 0.0, ()
    for c in doc.get("classes") or []:
        if c.get("sigma_class") == HIGH_GROWTH_SIGMA_CLASS:
            instruments = tuple(
                AllocationInstrument(
                    symbol=i.get("symbol", ""),
                    role=i.get("role", "primary"),
                    weight_within_class_pct=float(i.get("weight_within_class_pct") or 0.0),
                    rationale=i.get("rationale", "") or "",
                    domicile=i.get("domicile"),
                    # Durable per-instrument monitoring metadata must survive a
                    # refinement draft too — dropping it here would silently
                    # strip the recorded invalidation conditions off the
                    # moonshot names (the sleeve most in need of them).
                    exit_triggers=list(i.get("exit_triggers") or []),
                    review_on=i.get("review_on"),
                )
                for i in (c.get("instruments") or [])
                if i.get("symbol")
            )
            return float(c.get("target_pct") or 0.0), instruments
    return 0.0, ()


def create_refinement_draft(
    session,
    user_id: str,
    sleeve_overrides: dict[str, float],
    alternatives_sleeve: "object | None" = None,
    high_growth_instruments_override: "tuple | None" = None,
    version_label: str | None = None,
) -> "object":
    """Create and persist a staged draft PlanVersion carrying ``sleeve_overrides``.

    Steps
    -----
    1. Load the current plan (role='current' or baseline fallback).
    2. Merge ``sleeve_overrides`` onto the current plan's existing
       ``target_allocation_overrides_json`` — new edits win per label.
    3. Validate the merged overrides by attempting
       ``build_target_allocation(authored_overrides=merged)`` (pure, no DB).
       If the engine raises ValueError (unknown label / sum > 100) we
       re-raise as a ``ValueError`` so the route can return 400 BEFORE
       any write.
    4. Resolve the full ``target_allocation_json`` for the draft via
       ``build_target_allocation_doc`` wired to the current plan's
       decision_run_id (or 0 when absent).  When the composition is absent
       (no snapshot), the doc is left as None rather than falling back to
       a stale doc that would lack the override.  Raises on doc-build
       failure so a draft is never committed without the applied override.
    5. Persist a new PlanVersion(role='draft') with:
         derived_from_id  = current.id
         target_allocation_overrides_json = merged (JSON)
         target_allocation_json           = resolved doc JSON (may be
                                            the current plan's doc when the
                                            fresh build fails transiently)
         horizon_*_json / _md             = copied from current
         decision_run_id                  = None (no agent run for a scoped edit)
    6. Commit and return the new PlanVersion row.

    Raises
    ------
    RuntimeError
        When the user has no current plan to base the draft on.
    ValueError
        When ``sleeve_overrides`` contains an unknown label or causes the
        override-sum to exceed 100.  Raised BEFORE any DB write (validate-on-write).
    """
    from argosy.services.allocation_plan import build_target_allocation
    from argosy.services.target_allocation_doc import (
        build_target_allocation_doc,
        _prior_glide_q0,
        load_full_book_today_composition,
        _deconcentration_quarters,
        _assert_conserving_glide,
    )
    from argosy.state.models import PlanVersion
    from argosy.state.queries import get_current_plan

    # ---- 1. Load current plan -----------------------------------------------
    current = get_current_plan(session, user_id)
    if current is None:
        raise RuntimeError(
            f"user {user_id!r} has no current plan; cannot create a refinement draft"
        )

    # ---- 2. Merge overrides -------------------------------------------------
    existing: dict[str, float] = {}
    raw = getattr(current, "target_allocation_overrides_json", None)
    if raw:
        try:
            existing = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning(
                "plan_refinement.bad_existing_overrides",
                user_id=user_id,
                plan_version_id=current.id,
            )
    # Normalize legacy sleeve-label keys (pre-relabel durable overrides) to the
    # current canonical labels BEFORE merging, so (a) a relabel never breaks a
    # stored override row and (b) the persisted merged JSON is label-migrated
    # rather than carrying both the legacy and current key for one sleeve.
    from argosy.services.allocation_plan import normalize_override_labels
    from argosy.services.target_allocation_doc import (
        INSTRUMENT_META_OVERRIDE_KEY,
        split_instrument_meta,
    )

    # Per-instrument metadata (exit triggers / review anchors) rides the same
    # overrides JSON under a reserved key; it is NOT a sleeve label, so split it
    # out before label normalization/validation and re-attach it to the merged
    # persist so a refinement never drops it.
    existing_sleeves, instrument_meta = split_instrument_meta(existing)
    merged: dict[str, float] = {
        **normalize_override_labels(existing_sleeves or {}),
        **normalize_override_labels(sleeve_overrides),
    }

    # ---- 3. Validate merged overrides (validate-on-write) -------------------
    # ``build_target_allocation`` is pure and raises ValueError on bad labels or
    # sum > 100.  We call it here for validation ONLY — the doc is built in step 4.
    # The current plan's team-sourced high-growth sleeve is carried through BOTH
    # the validation and the doc build (see _fixed_sleeves_from_current) so the
    # refinement edits sleeve targets without dropping the sleeve.
    hg_pct, hg_instruments = _fixed_sleeves_from_current(current)
    # A team-sourced re-composition of the high-growth sleeve (e.g. the x10
    # asymmetry re-sourcing) replaces the carried instruments while keeping the
    # sleeve's target_pct from the current plan. Only honoured when the current
    # plan actually carries the sleeve — an override can't conjure a sleeve.
    if high_growth_instruments_override is not None and hg_pct > 0:
        hg_instruments = tuple(high_growth_instruments_override)
    build_target_allocation(
        authored_overrides=merged,
        alternatives_sleeve=alternatives_sleeve,
        high_growth_pct=hg_pct,
        high_growth_instruments=hg_instruments,
    )

    # ---- 4. Resolve target_allocation_json for the new draft ----------------
    decision_run_id = getattr(current, "decision_run_id", None) or 0
    today = datetime.now(timezone.utc).date()

    resolved_doc_json: str | None = None
    comp = load_full_book_today_composition(session, user_id, decision_run_id)
    if comp is None:
        comp = _prior_glide_q0(session, user_id)
    if comp is not None:
        # If build_target_allocation_doc fails AFTER the validation in step 3
        # succeeded, fail loud — a silent carry-forward would produce a draft
        # whose target_allocation_json lacks the override entirely.
        quarters = _deconcentration_quarters(session, user_id, today)
        doc = build_target_allocation_doc(
            today=today,
            today_composition=comp,
            quarters=quarters,
            authored_overrides=(
                {**merged, INSTRUMENT_META_OVERRIDE_KEY: instrument_meta}
                if instrument_meta else merged
            ),
            alternatives_sleeve=alternatives_sleeve,
            high_growth_pct=hg_pct,
            high_growth_instruments=hg_instruments,
        )
        _assert_conserving_glide(doc)
        resolved_doc_json = doc.model_dump_json()

    # ---- 5. Create the draft PlanVersion ------------------------------------
    merged_json = json.dumps(
        {**merged, INSTRUMENT_META_OVERRIDE_KEY: instrument_meta}
        if instrument_meta else merged
    )
    version_label = version_label or (
        f"refinement-draft-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')}"
    )
    draft = PlanVersion(
        user_id=user_id,
        role="draft",
        version_label=version_label,
        # raw_markdown + source_path: carry from current; the prose hasn't changed.
        source_path=getattr(current, "source_path", "") or "",
        raw_markdown=getattr(current, "raw_markdown", "") or "",
        # Lineage
        derived_from_id=current.id,
        decision_run_id=None,  # scoped edit — no agent run
        # Allocation
        target_allocation_overrides_json=merged_json,
        target_allocation_json=resolved_doc_json,
        # Carry horizon sections unchanged
        horizon_long_json=current.horizon_long_json,
        horizon_medium_json=current.horizon_medium_json,
        horizon_short_json=current.horizon_short_json,
        horizon_long_md=current.horizon_long_md,
        horizon_medium_md=current.horizon_medium_md,
        horizon_short_md=current.horizon_short_md,
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    log.info(
        "plan_refinement.draft_created",
        user_id=user_id,
        draft_id=draft.id,
        derived_from_id=current.id,
        merged_overrides=merged,
    )
    return draft


__all__ = ["create_refinement_draft"]
