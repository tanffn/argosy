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


def create_refinement_draft(
    session,
    user_id: str,
    sleeve_overrides: dict[str, float],
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
    merged: dict[str, float] = {**existing, **sleeve_overrides}

    # ---- 3. Validate merged overrides (validate-on-write) -------------------
    # ``build_target_allocation`` is pure and raises ValueError on bad labels or
    # sum > 100.  We call it here for validation ONLY — the doc is built in step 4.
    build_target_allocation(authored_overrides=merged)

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
            authored_overrides=merged,
        )
        _assert_conserving_glide(doc)
        resolved_doc_json = doc.model_dump_json()

    # ---- 5. Create the draft PlanVersion ------------------------------------
    merged_json = json.dumps(merged)
    version_label = (
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
