"""plan_synthesis_flow — five-phase orchestration that produces a
draft long/medium/short plan from current state + agent fleet review.

Triggers (one of):
  - scheduled (monthly_cycle on the 1st)
  - check_in (user-initiated via /api/advisor/check-in)
  - quarterly (extra prompt weight on medium horizon)
  - annual   (extra prompt weight on long horizon)

Phases:
  1. Analyst reports (parallel) — 9 specialists run concurrently
  2. Researcher debate (per-horizon) — 3 horizons in parallel
  3. Synthesizer — produces the three HorizonSection drafts
  4. Risk team review — plan-level verdict
  5. Fund manager integrity check — green-lights as role=draft

Per spec §4. Output: a new role='draft' PlanVersion row.

Idempotency: if a draft already exists for the user, it is moved to
role='superseded' and a fresh draft is written.

Phase implementations are pluggable (each has a default that calls
the existing fleet agents with plan-revision prompts; tests stub them).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import (
    PlanSynthesisOutput,
    SynthesisInputs,
)
from argosy.logging import get_logger
from argosy.state.models import PlanVersion
from argosy.state.queries import get_active_baseline, get_current_plan, get_pending_draft

log = get_logger(__name__)


Trigger = Literal["scheduled", "check_in", "quarterly", "annual"]


class NoBaselineError(Exception):
    """Raised when a user has no active baseline plan."""


@dataclass
class SynthesisResult:
    decision_run_id: str
    draft_id: int


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def run_synthesis(
    session: Session,
    *,
    user_id: str,
    trigger: Trigger,
    guidance: str = "",
) -> SynthesisResult:
    """Execute the 5-phase synthesis. Writes a role='draft' row.

    Args:
        guidance: optional free-text from the user's check-in to weight
            the synthesis (e.g. "weight tax analyst more heavily").
    """
    baseline = get_active_baseline(session, user_id)
    if baseline is None:
        raise NoBaselineError(f"user {user_id!r} has no active baseline plan")

    prior_current = get_current_plan(session, user_id)
    decision_run_id = f"plan-synth-{uuid.uuid4().hex[:12]}"
    log.info(
        "plan_synthesis.start",
        user_id=user_id,
        trigger=trigger,
        decision_run_id=decision_run_id,
    )

    # Idempotency: demote any existing draft.
    existing = get_pending_draft(session, user_id)
    if existing is not None:
        existing.role = "superseded"
        existing.superseded_at = datetime.now(timezone.utc)
        session.commit()
        log.info(
            "plan_synthesis.demoted_existing_draft",
            superseded_id=existing.id,
            user_id=user_id,
        )

    # Phase 1: analyst reports.
    analyst_reports_text = _run_phase_1_analysts(
        session=session, user_id=user_id, baseline=baseline,
        prior_current=prior_current, decision_run_id=decision_run_id,
        guidance=guidance,
    )

    # Assemble inputs for Phases 2+.
    portfolio_summary = _assemble_portfolio_summary(session=session, user_id=user_id)
    fills_summary = _assemble_fills_summary(session=session, user_id=user_id)

    # Phase 2: per-horizon debates.
    debate_outcomes_text = _run_phase_2_debates(
        session=session, user_id=user_id,
        analyst_reports_text=analyst_reports_text,
        baseline=baseline, prior_current=prior_current,
        decision_run_id=decision_run_id, trigger=trigger,
    )

    # Phase 3: synthesize.
    output: PlanSynthesisOutput = _run_phase_3_synthesizer(
        session=session, user_id=user_id,
        baseline=baseline, prior_current=prior_current,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_summary=portfolio_summary,
        fills_summary=fills_summary,
        decision_run_id=decision_run_id,
    )

    # Phase 4: risk team plan-level review.
    risk_verdict = _run_phase_4_risk(
        session=session, user_id=user_id, draft_output=output,
        analyst_reports_text=analyst_reports_text,
        decision_run_id=decision_run_id,
    )

    # Phase 5: fund manager integrity check.
    approved = _run_phase_5_fund_manager(
        session=session, user_id=user_id, draft_output=output,
        risk_verdict=risk_verdict, decision_run_id=decision_run_id,
    )
    if not approved:
        log.error("plan_synthesis.fm_rejected",
                  user_id=user_id, decision_run_id=decision_run_id)
        raise RuntimeError("fund manager rejected synthesized plan")

    # Persist as role='draft'.
    inputs = output.inputs.model_copy(update={
        "baseline_id": baseline.id,
        "prior_current_id": prior_current.id if prior_current else None,
        "decision_run_id": decision_run_id,
    })

    draft = PlanVersion(
        user_id=user_id,
        role="draft",
        version_label=f"synth-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
        source_path="",
        raw_markdown="",
        decision_run_id=decision_run_id,
        derived_from_id=baseline.id,
        horizon_long_json=output.long.model_dump_json(),
        horizon_medium_json=output.medium.model_dump_json(),
        horizon_short_json=output.short.model_dump_json(),
        horizon_long_md=_horizon_md(output.long),
        horizon_medium_md=_horizon_md(output.medium),
        horizon_short_md=_horizon_md(output.short),
        synthesis_inputs_json=inputs.model_dump_json(),
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)

    log.info("plan_synthesis.draft_persisted",
             user_id=user_id, draft_id=draft.id, decision_run_id=decision_run_id)
    return SynthesisResult(decision_run_id=decision_run_id, draft_id=draft.id)


# ----------------------------------------------------------------------
# Phase implementations (default — call existing fleet agents)
# ----------------------------------------------------------------------


def _run_phase_1_analysts(*, session, user_id, baseline, prior_current,
                           decision_run_id, guidance) -> str:
    """Run all 9 analysts in parallel against current state.

    Returns a concatenated string of analyst reports for downstream
    phases. Each agent's full structured output is also written to
    agent_reports stamped with decision_run_id.

    For Wave 2 first cut, this delegates to existing analyst agents
    (news/macro/concentration/plan_critique/tax/fx/sentiment/technical/
    fundamentals). We do not re-implement them. The plan-revision shape
    of their inputs/outputs is the same — they read state, produce
    structured reports.

    Tests monkeypatch this whole function to a stub.
    """
    # Wave 2 implementation note: this function will be expanded to call
    # each analyst's run_sync method in parallel via concurrent.futures
    # and concatenate their .output.model_dump_json() outputs. For the
    # initial scaffold we return a TODO marker — the real wiring lands
    # alongside Phase 3 agent-fleet readiness.
    log.info("plan_synthesis.phase_1_stub", user_id=user_id, decision_run_id=decision_run_id)
    return (
        "(Phase 1 analyst reports — wired against the live fleet "
        "in Phase 3 of SDD; see plan task 2.6 phase-stub note.)"
    )


def _run_phase_2_debates(*, session, user_id, analyst_reports_text,
                         baseline, prior_current, decision_run_id, trigger) -> str:
    """Run bull/bear/facilitator per-horizon (parallel across horizons)."""
    log.info("plan_synthesis.phase_2_stub", user_id=user_id, decision_run_id=decision_run_id)
    return (
        "(Phase 2 debate outcomes per horizon — wired against the "
        "researcher debate flow once SDD Phase 3 is complete.)"
    )


def _run_phase_3_synthesizer(*, session, user_id, baseline, prior_current,
                             analyst_reports_text, debate_outcomes_text,
                             portfolio_summary, fills_summary,
                             decision_run_id) -> PlanSynthesisOutput:
    """Default Phase 3: call PlanSynthesizerAgent."""
    # ADAPTATION: spec wrote PlanSynthesizerAgent() but BaseAgent.__init__
    # requires user_id as a mandatory keyword argument.
    agent = PlanSynthesizerAgent(user_id=user_id)
    baseline_md = baseline.distillate_rendered or "(no distillate available)"
    prior_md = ""
    if prior_current:
        prior_md = "\n\n".join(filter(None, [
            prior_current.horizon_long_md,
            prior_current.horizon_medium_md,
            prior_current.horizon_short_md,
        ]))
    result = agent.run_sync(
        baseline_distillate_md=baseline_md,
        prior_current_md=prior_md,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
    )
    return result.output  # type: ignore[attr-defined]


def _run_phase_4_risk(*, session, user_id, draft_output: PlanSynthesisOutput,
                      analyst_reports_text: str, decision_run_id: str) -> str:
    """Run aggressive/neutral/conservative risk officers against the draft.

    Plan-level verdicts (not per-trade). Returns a consolidated text.
    Stubbed for tests.
    """
    log.info("plan_synthesis.phase_4_stub", user_id=user_id, decision_run_id=decision_run_id)
    return "(Phase 4 risk verdict — wired against the existing risk team flow.)"


def _run_phase_5_fund_manager(*, session, user_id,
                              draft_output: PlanSynthesisOutput,
                              risk_verdict: str, decision_run_id: str) -> bool:
    """Final integrity check. Returns True to green-light."""
    log.info("plan_synthesis.phase_5_stub", user_id=user_id, decision_run_id=decision_run_id)
    return True


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _horizon_md(section) -> str:
    """Render a HorizonSection to a markdown view used by the UI side sheet."""
    lines = [f"# {section.horizon.title()} horizon — status: {section.status}"]
    lines.append("")
    if section.posture:
        lines.append(f"**Posture.** {section.posture}")
        lines.append("")
    if section.targets:
        lines.append("## Targets")
        for t in section.targets:
            lines.append(
                f"- **{t.label}**: {t.value} {t.unit} "
                f"(stated {t.stated_at.isoformat()}; revisit {t.revisit_after.isoformat()})"
                f" — {t.rationale}" if t.rationale else ""
            )
        lines.append("")
    if section.themes:
        lines.append("## Themes")
        for th in section.themes:
            lines.append(f"- **{th.label}** ({th.direction}) — {th.rationale}")
        lines.append("")
    if section.actions:
        lines.append("## Actions")
        for a in section.actions:
            trigger = f" [{a.trigger_or_date}]" if a.trigger_or_date else ""
            lines.append(f"- **{a.label}**{trigger}: {a.detail} — {a.rationale}")
        lines.append("")
    if section.horizon == "short" and section.speculative_candidates:
        lines.append("## Speculative candidates")
        for sc in section.speculative_candidates:
            lines.append(
                f"- **{sc.ticker}**: max ${sc.suggested_position_usd:,.0f} "
                f"(= {sc.suggested_position_pct_of_net_worth*100:.2f}% NW) · "
                f"{sc.thesis_summary} · exit: {sc.exit_trigger}"
            )
        lines.append("")
    if section.deltas_from_prior:
        lines.append("## Deltas vs. prior current")
        for d in section.deltas_from_prior:
            lines.append(
                f"- [{d.change_kind}] {d.summary} ({d.item_kind} `{d.item_id}`)"
            )
        lines.append("")
    if section.rationale:
        lines.append("## Rationale")
        lines.append(section.rationale)
    return "\n".join(lines).rstrip() + "\n"


def _assemble_portfolio_summary(*, session, user_id) -> str:
    """Build a compact portfolio-state summary for synthesis input.

    Wave 2: read latest TSV/CSV ingest + IBKR positions per SDD §8.
    Tests stub this.
    """
    return "(portfolio snapshot — wired against existing positions ingest)"


def _assemble_fills_summary(*, session, user_id) -> str:
    """Last 90 days of fills + decisions, summarized."""
    return "(fills summary — wired against fills + proposals tables)"


__all__ = [
    "NoBaselineError",
    "SynthesisResult",
    "Trigger",
    "run_synthesis",
]
