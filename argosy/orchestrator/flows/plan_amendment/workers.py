"""Plan amendment workers — Medium (Phase 3 only) + Large (full synthesis).

Both are sync functions; the dispatcher invokes them via asyncio.to_thread
so the event loop stays free during the synthesis run.

Each worker:
  1. Checks the DecisionRun's status — bails if 'cancelled'.
  2. Runs the work (Phase 3 only for medium; run_synthesis for large).
  3. Applies the speculation cap post-filter (Wave 3 layer 2).
  4. Persists role=draft PlanVersion (medium); large persists via run_synthesis itself.
  5. Stamps DecisionRun finished_at + status='completed'.
  6. Emits plan.amendment.completed via publish_event_threadsafe.

On exception: stamps status='failed' + error_message, emits plan.amendment.failed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput
from argosy.api.events import publish_event_threadsafe
from argosy.config import get_user_agent_settings, load_speculation_cap
from argosy.logging import get_logger
from argosy.orchestrator.flows.plan_synthesis import (
    _enforce_speculation_cap,
    _horizon_md_audit,
    _horizon_md_user,
    render_plan_appendices,
    run_synthesis,
)
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import (
    get_active_baseline,
    get_current_plan,
    get_pending_draft,
)

log = get_logger(__name__)


def _run_phase_3_synthesizer(*, user_id, baseline_distillate_md, prior_current_md,
                             guidance, portfolio_summary, fills_summary,
                             speculation_cap_pct, speculation_cap_concurrent,
                             prior_items_index=None,
                             ) -> PlanSynthesisOutput:
    """Direct-invoke PlanSynthesizerAgent; skip Phases 1/2/4/5.

    Indirection point so tests can monkeypatch.

    Takes already-rendered markdown strings (not the ORM rows) so tests
    can assert on the inputs the synthesizer would actually see.

    ``prior_items_index`` is required for ID-stability across amendments
    after Phase 1 of the integration plan (the prior-plan body block was
    dropped from the synth prompt; the items index is the surviving
    channel through which the model preserves item_ids on revision).
    """
    agent = PlanSynthesizerAgent(user_id=user_id)
    result = agent.run_sync(
        baseline_distillate_md=baseline_distillate_md or "(no distillate available)",
        prior_current_md=prior_current_md,
        prior_items_index=prior_items_index or [],
        analyst_reports_text=f"(amendment guidance: {guidance})",
        debate_outcomes_text="(skipped — medium-tier amendment)",
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
        speculation_cap_pct=speculation_cap_pct,
        speculation_cap_concurrent=speculation_cap_concurrent,
    )
    return result.output  # type: ignore[attr-defined]


def _render_prior_current_md(prior_current) -> str:
    """Concatenate the three horizon markdown sections of a current plan row.

    Returns ``""`` if the row is None or has no rendered markdown yet.
    Falls back to the JSON column when the markdown column is empty so the
    synthesizer still has the prior posture to anchor on.
    """
    if prior_current is None:
        return ""
    parts: list[str] = []
    for md_attr, json_attr in (
        ("horizon_long_md", "horizon_long_json"),
        ("horizon_medium_md", "horizon_medium_json"),
        ("horizon_short_md", "horizon_short_json"),
    ):
        md_val = getattr(prior_current, md_attr, None)
        if md_val:
            parts.append(md_val)
            continue
        json_val = getattr(prior_current, json_attr, None)
        if json_val:
            parts.append(json_val)
    return "\n\n".join(parts)


def _medium_worker(*, session: Session, user_id: str,
                   decision_run: DecisionRun, guidance: str,
                   freeze_except: set[str] | tuple[str, ...] | None = None,
                   freeze_baseline_plan_id: int | None = None,
                   ) -> None:
    """Run Phase 3 only with the user's amendment as guidance.

    ``freeze_except`` (slugs / normalized-heading keys, see
    ``section_freeze.py``): when provided, every horizon section NOT
    named here is restored to the prior current plan's text verbatim
    after re-synthesis, and any section the model dropped that WAS
    named here is restored too (never lose a requested section). When
    ``None`` (the default), behaviour is unchanged from before this
    param existed — the synthesizer's full re-roll is persisted as-is.

    ``freeze_baseline_plan_id``: which ``PlanVersion`` row to freeze
    against. Freezing against ``get_current_plan`` (``role='current'``)
    is WRONG whenever the plan actually being amended is a later draft
    that has already superseded the current row's content (measured:
    role='current' was plan 92 from 2026-07-13 while live drafts were
    106/107 — freezing against 92 would silently revert everything
    93->106 added, including the entire ``fi_bridge`` section). When
    given, that plan id (scoped to ``user_id`` — never cross-tenant) is
    loaded and used as the freeze baseline instead of ``prior_current``.
    A missing or cross-tenant id is a data-integrity problem, not
    something to paper over — it raises loudly rather than silently
    falling back to ``prior_current``. When ``freeze_except`` is
    provided but ``freeze_baseline_plan_id`` is None, behaviour falls
    back to ``prior_current`` as before, but a warning is logged naming
    the plan id actually used so a stale-baseline freeze is visible.
    """
    # Cancellation pre-check.
    session.refresh(decision_run)
    if decision_run.status == "cancelled":
        log.info("plan_amendment.medium.cancelled_before_start",
                 decision_run_id=decision_run.id)
        return

    publish_event_threadsafe("plan.amendment.started", {
        "user_id": user_id,
        "decision_run_id": decision_run.id,
        "tier": "medium",
        "eta_seconds": 30,
    })

    try:
        baseline = get_active_baseline(session, user_id)
        if baseline is None:
            raise RuntimeError(f"no active baseline for user {user_id!r}")
        prior_current = get_current_plan(session, user_id)

        # Reuse synthesis-flow placeholder helpers; they're documented stubs.
        portfolio_summary = "(amendment-flow placeholder; see plan_synthesis._assemble_portfolio_summary)"
        fills_summary = "(amendment-flow placeholder)"

        # Cap.
        try:
            cap = load_speculation_cap(
                user_id=user_id, agent_settings=get_user_agent_settings(user_id),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_amendment.medium.cap_load_failed",
                        user_id=user_id, error=str(exc))
            from argosy.config import SpeculationCap
            cap = SpeculationCap()

        # Phase 1 of the integration plan dropped the prior-plan body
        # from the synth user-prompt. The amendment path now must
        # supply ``prior_items_index`` directly so the synthesizer can
        # still preserve item_ids across revisions (otherwise the
        # amendment re-synth has no ID-stability signal at all — the
        # main flow builds this list at plan_synthesis/orchestrator.py
        # via ``_pkg_build_prior_items_index``; we reuse the helper).
        from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
            _pkg_build_prior_items_index,
        )
        prior_items_index = _pkg_build_prior_items_index(
            session, user_id=user_id, prior_current=prior_current,
        )

        output = _run_phase_3_synthesizer(
            user_id=user_id,
            baseline_distillate_md=baseline.distillate_rendered or "",
            prior_current_md=_render_prior_current_md(prior_current),
            prior_items_index=prior_items_index,
            guidance=guidance,
            portfolio_summary=portfolio_summary, fills_summary=fills_summary,
            speculation_cap_pct=cap.max_pct_of_net_worth,
            speculation_cap_concurrent=cap.max_concurrent_positions,
        )

        # Phase 2 — translate jargon-heavy prose to household English
        # BEFORE the speculation-cap post-filter. Without this, medium
        # amendments would persist horizon MD that still names agent
        # classes and uses substrate terminology — the main synthesis
        # flow runs this between phase 3 and cap enforcement.
        from argosy.orchestrator.flows.plan_synthesis import (
            _run_plan_language_rewriter,
        )
        output = _run_plan_language_rewriter(
            output=output,
            user_id=user_id,
            decision_run_id=decision_run.id,
        )

        # Layer 2 post-filter.
        output = _enforce_speculation_cap(
            output,
            max_pct_of_net_worth=cap.max_pct_of_net_worth,
            max_concurrent_positions=cap.max_concurrent_positions,
        )

        # Cancellation re-check before persisting.
        session.refresh(decision_run)
        if decision_run.status == "cancelled":
            log.info("plan_amendment.medium.cancelled_before_persist",
                     decision_run_id=decision_run.id)
            return

        # Idempotency: demote any pending draft. Held in the SAME commit
        # as the new draft INSERT so a failure between this UPDATE and
        # the INSERT can never strand the prior draft as superseded
        # without a successor. The explicit ``session.flush()`` after the
        # UPDATE ensures the partial unique index
        # ``uq_plan_versions_draft_per_user`` sees the demote before the
        # INSERT lands (statement-level enforcement on SQLite + Postgres).
        existing_draft = get_pending_draft(session, user_id)
        if existing_draft is not None:
            existing_draft.role = "superseded"
            existing_draft.superseded_at = datetime.now(timezone.utc)
            session.flush()

        inputs = output.inputs.model_copy(update={
            "baseline_id": baseline.id,
            "prior_current_id": prior_current.id if prior_current else None,
            "decision_run_id": decision_run.id,
        })
        # v4 block B1 — assemble the plan-doc appendices once and append
        # to ``horizon_long_md`` (parity with the synthesis flow at
        # argosy/orchestrator/flows/plan_synthesis/orchestrator.py).
        _long_md = _horizon_md_user(output.long)
        _appendices = render_plan_appendices(
            output,
            session=session,
            decision_run_id=decision_run.id,
        )
        if _appendices:
            _long_md = _long_md.rstrip() + "\n\n" + _appendices
        _medium_md = _horizon_md_user(output.medium)
        _short_md = _horizon_md_user(output.short)

        # Section freeze — restore untouched sections to the prior
        # current plan's verbatim text so a narrowly-scoped amendment
        # cannot silently rewrite/rename/drop sections nobody asked to
        # change (measured regression: plan 106 -> 107 lost
        # cover_assumptions/fi_bridge/monte_carlo outright). Runs BEFORE
        # the fact-tokenizer pass below so tokenization still sees the
        # MERGED text — a frozen section may cite a figure the
        # amendment changed elsewhere, and that drift must still be
        # caught, not hidden by the restore.
        if freeze_except is not None:
            from argosy.orchestrator.flows.plan_amendment.section_freeze import (
                merge_frozen_sections,
            )

            if freeze_baseline_plan_id is not None:
                _freeze_baseline = session.get(PlanVersion, freeze_baseline_plan_id)
                if _freeze_baseline is None or _freeze_baseline.user_id != user_id:
                    raise RuntimeError(
                        f"freeze_baseline_plan_id={freeze_baseline_plan_id!r} not found "
                        f"for user {user_id!r} — refusing to freeze against a stale, "
                        "missing, or cross-tenant plan"
                    )
                log.info(
                    "plan_amendment.medium.freeze_baseline_explicit",
                    decision_run_id=decision_run.id,
                    freeze_baseline_plan_id=_freeze_baseline.id,
                )
            else:
                _freeze_baseline = prior_current
                if _freeze_baseline is not None:
                    log.warning(
                        "plan_amendment.medium.freeze_baseline_defaulted_to_prior_current",
                        decision_run_id=decision_run.id,
                        freeze_baseline_plan_id=_freeze_baseline.id,
                    )

            _freeze_allow = set(freeze_except)
            if _freeze_baseline is not None:
                _long_md, _freeze_notes_long = merge_frozen_sections(
                    _freeze_baseline.horizon_long_md or "", _long_md, allow=_freeze_allow,
                )
                _medium_md, _freeze_notes_medium = merge_frozen_sections(
                    _freeze_baseline.horizon_medium_md or "", _medium_md, allow=_freeze_allow,
                )
                _short_md, _freeze_notes_short = merge_frozen_sections(
                    _freeze_baseline.horizon_short_md or "", _short_md, allow=_freeze_allow,
                )
            else:
                _freeze_notes_long = _freeze_notes_medium = _freeze_notes_short = []
            for _horizon_name, _notes in (
                ("long", _freeze_notes_long),
                ("medium", _freeze_notes_medium),
                ("short", _freeze_notes_short),
            ):
                if _notes:
                    log.info(
                        "plan_amendment.medium.section_freeze",
                        decision_run_id=decision_run.id,
                        horizon=_horizon_name,
                        notes=_notes,
                    )

        # Tokenize-canonical-figures pass (fix/tokenize-canonical-figures):
        # the medium amendment path re-synthesizes from a narrow guidance
        # prompt and does NOT go through run_synthesis's in-stage gate, so
        # it needs its OWN call here — otherwise a medium amendment is the
        # exact "narrowly-scoped amendment re-rolls the whole section"
        # scenario the pass exists for. Best-effort: never fails the
        # amendment on a tokenize error.
        try:
            from argosy.quality.fact_tokenizer import tokenize_bodies
            from argosy.services.plan_numeric_resolver import resolve_plan_numbers

            # include_canonical_ages=True: matches the ONE production
            # convention every user-facing {{fact:}} renderer uses
            # (fact_token_render.render_fact_tokens, instage_gate's resolve
            # wrapper) — without it concentration.nvda_cap_pct resolves to the
            # concentration analyst's separate MIN-over-constraints floor
            # instead of the doc-anchored settled binding cap the prose
            # actually cites, and the tokenizer cries wolf on legitimate
            # cap/target literals.
            _tok_resolved = resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=decision_run.id,
                include_canonical_ages=True,
            )
            _tok_bodies, _tok_violations, _tok_subs = tokenize_bodies(
                {"long": _long_md, "medium": _medium_md, "short": _short_md},
                _tok_resolved,
            )
            if _tok_violations:
                log.warning(
                    "plan_amendment.medium.fact_literal_drift",
                    decision_run_id=decision_run.id,
                    violations=[v.detail for v in _tok_violations],
                )
            if _tok_subs:
                _long_md = _tok_bodies["long"]
                _medium_md = _tok_bodies["medium"]
                _short_md = _tok_bodies["short"]
        except Exception as exc:  # noqa: BLE001 — best-effort, never abort
            log.warning(
                "plan_amendment.medium.fact_tokenizer_failed",
                decision_run_id=decision_run.id, error=str(exc),
            )

        # T1.5 — persist the canonical TargetAllocationDoc. Best-effort + never
        # fatal, but NOT silently-NULL: a transient build failure carries forward
        # the prior CURRENT plan's doc so the amendment draft is never un-anchored
        # (the regression behind the draft-36 422).
        # Carry authored overrides forward from the prior current plan so durable
        # sleeve targets survive re-synthesis (migration 0076).
        from argosy.services.target_allocation_doc import (
            inherit_overrides_from_parent,
            resolve_target_allocation_json,
        )

        _prior_overrides_json = inherit_overrides_from_parent(prior_current) if prior_current else None
        _authored_overrides: dict | None = (
            json.loads(_prior_overrides_json) if _prior_overrides_json else None
        )
        _target_allocation_json = resolve_target_allocation_json(
            session, user_id, decision_run.id, datetime.now(timezone.utc).date(),
            authored_overrides=_authored_overrides,
        )

        draft = PlanVersion(
            user_id=user_id, role="draft",
            version_label=f"amend-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
            source_path="", raw_markdown="",
            decision_run_id=decision_run.id,
            derived_from_id=baseline.id,
            horizon_long_json=output.long.model_dump_json(),
            horizon_medium_json=output.medium.model_dump_json(),
            horizon_short_json=output.short.model_dump_json(),
            # Phase 1 — user-facing vs audit split. See render.py docstring.
            # v4 — long_md additionally carries the appendix block.
            horizon_long_md=_long_md,
            horizon_medium_md=_medium_md,
            horizon_short_md=_short_md,
            horizon_long_md_audit=_horizon_md_audit(output.long),
            horizon_medium_md_audit=_horizon_md_audit(output.medium),
            horizon_short_md_audit=_horizon_md_audit(output.short),
            synthesis_inputs_json=inputs.model_dump_json(),
            target_allocation_json=_target_allocation_json,
            # Durable authored overrides inherited from the prior current plan
            # (migration 0076) so a refined sleeve target survives re-synthesis.
            target_allocation_overrides_json=_prior_overrides_json,
            sections_json=json.dumps(
                [s.model_dump(mode="json") for s in output.sections]
            ),
        )
        session.add(draft)
        decision_run.finished_at = datetime.now(timezone.utc)
        decision_run.status = "completed"
        session.commit()
        session.refresh(draft)

        # Provenance Wave C — record medium-amendment synthesis phase.
        # Best-effort: must never fail the underlying flow.
        try:
            import asyncio
            from argosy.agents.fund_manager import (
                FundManagerPlanRevisionDecision,
            )
            from argosy.services.negotiation_recorder import (
                record_negotiation_phase,
            )

            verdict = FundManagerPlanRevisionDecision(
                approved=True,
                reasons=[
                    f"medium amendment synthesized; draft_id={draft.id}",
                ],
                cited_sources=["docs/design/SDD.md#§6.13"],
            )
            asyncio.run(record_negotiation_phase(
                user_id=user_id, decision_run_id=decision_run.id,
                kind="amend_synth", started_at=decision_run.started_at,
                agent_report_ids=[], verdict=verdict,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plan_amendment.medium.record_phase_failed",
                decision_run_id=decision_run.id, error=str(exc),
            )

        publish_event_threadsafe("plan.amendment.completed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "medium",
            "draft_id": draft.id,
        })
        publish_event_threadsafe("plan.draft.completed", {
            "user_id": user_id,
            "draft_id": draft.id,
        })
    except Exception as exc:  # noqa: BLE001
        log.error("plan_amendment.medium.failed",
                  decision_run_id=decision_run.id, error=str(exc))
        session.refresh(decision_run)
        # I2: merge error into notes_json rather than clobbering the
        # original message+intent the dispatcher wrote (we want replay).
        try:
            existing_notes = json.loads(decision_run.notes_json or "{}")
        except (ValueError, TypeError):
            existing_notes = {}
        existing_notes["error"] = str(exc)
        decision_run.notes_json = json.dumps(existing_notes)
        decision_run.status = "failed"
        decision_run.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.failed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "medium",
            "error": str(exc),
        })


def _large_worker(*, session: Session, user_id: str,
                  decision_run: DecisionRun, guidance: str) -> None:
    """Delegate to run_synthesis (full 5-phase) with guidance.

    Reuses the worker's own DecisionRun row for synthesis (via
    `existing_decision_run_id`) so chat-turn → amendment row → draft is
    a single audit chain instead of two independent rows.
    """
    session.refresh(decision_run)
    if decision_run.status == "cancelled":
        log.info("plan_amendment.large.cancelled_before_start",
                 decision_run_id=decision_run.id)
        return

    publish_event_threadsafe("plan.amendment.started", {
        "user_id": user_id,
        "decision_run_id": decision_run.id,
        "tier": "large",
        "eta_seconds": 900,  # 15 min nominal
    })

    try:
        result = run_synthesis(
            session, user_id=user_id, trigger="check_in", guidance=guidance,
            existing_decision_run_id=decision_run.id,
        )

        # I5: cancellation can land mid-synthesis (~15 min window). Re-fetch
        # before stamping completed; if the row was cancelled while we were
        # running, leave the synthesis-produced draft as-is (forensic
        # value) but DO NOT overwrite the cancelled status.
        session.refresh(decision_run)
        if decision_run.status == "cancelled":
            log.info("plan_amendment.large.cancelled_during_run",
                     decision_run_id=decision_run.id)
            publish_event_threadsafe("plan.amendment.cancelled", {
                "user_id": user_id,
                "decision_run_id": decision_run.id,
                "tier": "large",
            })
            return

        decision_run.finished_at = datetime.now(timezone.utc)
        decision_run.status = "completed"
        session.commit()

        publish_event_threadsafe("plan.amendment.completed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "large",
            "draft_id": result.draft_id,
        })
    except Exception as exc:  # noqa: BLE001
        log.error("plan_amendment.large.failed",
                  decision_run_id=decision_run.id, error=str(exc))
        session.refresh(decision_run)
        # I2: merge error into notes_json rather than clobbering the
        # original message+intent the dispatcher wrote (we want replay).
        try:
            existing_notes = json.loads(decision_run.notes_json or "{}")
        except (ValueError, TypeError):
            existing_notes = {}
        existing_notes["error"] = str(exc)
        decision_run.notes_json = json.dumps(existing_notes)
        decision_run.status = "failed"
        decision_run.finished_at = datetime.now(timezone.utc)
        session.commit()
        publish_event_threadsafe("plan.amendment.failed", {
            "user_id": user_id,
            "decision_run_id": decision_run.id,
            "tier": "large",
            "error": str(exc),
        })


__all__ = ["_medium_worker", "_large_worker", "_run_phase_3_synthesizer"]
