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
                   decision_run: DecisionRun, guidance: str) -> None:
    """Run Phase 3 only with the user's amendment as guidance."""
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
            horizon_long_md=_horizon_md_user(output.long),
            horizon_medium_md=_horizon_md_user(output.medium),
            horizon_short_md=_horizon_md_user(output.short),
            horizon_long_md_audit=_horizon_md_audit(output.long),
            horizon_medium_md_audit=_horizon_md_audit(output.medium),
            horizon_short_md_audit=_horizon_md_audit(output.short),
            synthesis_inputs_json=inputs.model_dump_json(),
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
