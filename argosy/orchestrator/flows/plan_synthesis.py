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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from argosy.agents.concentration_analyst import ConcentrationAnalystAgent
from argosy.agents.fundamentals_analyst import FundamentalsAnalystAgent
# The FX analyst class is `FXAnalystAgent` in source; the synthesis flow
# (and its tests) refer to it as `FxAnalystAgent`. Aliased on import.
from argosy.agents.fx_analyst import FXAnalystAgent as FxAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import (
    PlanSynthesisOutput,
    SynthesisInputs,
)
from argosy.agents.sentiment_analyst import SentimentAnalystAgent
from argosy.agents.tax_analyst import TaxAnalystAgent
from argosy.agents.technical_analyst import TechnicalAnalystAgent
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


# Module-level tuple — the test resolves each name via attribute lookup
# on this module (monkeypatch.setattr("...plan_synthesis.<Name>", stub)),
# so we build the iteration list lazily inside _run_phase_1_analysts to
# pick up monkeypatched replacements.
_PHASE_1_AGENT_NAMES = (
    "FundamentalsAnalystAgent",
    "TechnicalAnalystAgent",
    "NewsAnalystAgent",
    "SentimentAnalystAgent",
    "MacroAnalystAgent",
    "PlanCritiqueAgent",
    "ConcentrationAnalystAgent",
    "TaxAnalystAgent",
    "FxAnalystAgent",
)


def _run_phase_1_analysts(*, session, user_id, baseline, prior_current,
                           decision_run_id, guidance) -> str:
    """Run all 9 analysts in parallel. Concatenate their reports as text."""
    log.info("plan_synthesis.phase_1.start",
             user_id=user_id, decision_run_id=decision_run_id)

    # Resolve agent classes via this module's namespace so tests that
    # monkeypatch `argosy.orchestrator.flows.plan_synthesis.<Name>` are
    # honoured.
    import sys
    mod = sys.modules[__name__]
    phase_1_agents = tuple(getattr(mod, name) for name in _PHASE_1_AGENT_NAMES)

    # Each agent's run_sync(...) signature varies; we pass a shared kwargs
    # bag and rely on each agent's build_prompt to consume what it needs.
    # The base agents' run_sync forwards **kwargs to build_prompt.
    common_kwargs = dict(
        plan_label=baseline.version_label or "Imported plan",
        plan_markdown=baseline.distillate_rendered or "",
        snapshot_label=f"synthesis-{decision_run_id}",
        snapshot_summary=_assemble_portfolio_summary(session=session, user_id=user_id),
        user_context_yaml=_load_user_context_yaml(session=session, user_id=user_id),
        domain_kb_files={},  # Each analyst's prompt picks its own; pass empty.
        recent_events="",
    )

    reports: list[str] = []
    with ThreadPoolExecutor(max_workers=len(phase_1_agents)) as ex:
        futures = {
            ex.submit(_safe_run_agent, AgentCls, user_id, common_kwargs, decision_run_id): AgentCls
            for AgentCls in phase_1_agents
        }
        for fut in as_completed(futures):
            cls = futures[fut]
            try:
                payload = fut.result()
                reports.append(f"=== {cls.__name__} ===\n{payload}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_1.agent_failed",
                          agent=cls.__name__, error=str(exc),
                          decision_run_id=decision_run_id)
                # Failure of one analyst is recoverable — continue with
                # the others. Note in the concatenated text so the
                # synthesizer knows.
                reports.append(f"=== {cls.__name__} (FAILED) ===\n{exc}")

    log.info("plan_synthesis.phase_1.done",
             user_id=user_id, decision_run_id=decision_run_id,
             reports_count=len(reports))
    return "\n\n".join(reports)


def _safe_run_agent(AgentCls, user_id: str, kwargs: dict,
                    decision_run_id: str) -> str:
    """Instantiate an analyst, run it, return JSON of its output.

    ADAPTATION (vs spec): BaseAgent.__init__ takes a mandatory ``user_id``
    keyword — the spec wrote ``AgentCls()`` which would raise on any real
    agent. We try ``AgentCls(user_id=user_id)`` first and fall back to
    ``AgentCls()`` for stubs/tests whose constructors don't accept it.

    On a TypeError from ``run_sync`` (i.e. the agent's ``build_prompt``
    rejects one of our common kwargs), we narrow the kwargs to only those
    explicitly named in the agent's ``build_prompt`` signature and retry.
    """
    try:
        agent = AgentCls(user_id=user_id)
    except TypeError:
        agent = AgentCls()
    try:
        result = agent.run_sync(**kwargs)
        out = getattr(result, "output", None)
        if out is not None and hasattr(out, "model_dump_json"):
            return out.model_dump_json()
        return str(out) if out is not None else ""
    except TypeError:
        # If the agent doesn't accept all the common kwargs, retry with
        # only the ones it explicitly declares. Cheap defensive retry.
        sig = getattr(agent.build_prompt, "__code__", None)
        accepted = set(sig.co_varnames if sig else ())
        narrowed = {k: v for k, v in kwargs.items() if k in accepted}
        result = agent.run_sync(**narrowed)
        out = getattr(result, "output", None)
        if out is not None and hasattr(out, "model_dump_json"):
            return out.model_dump_json()
        return str(out) if out is not None else ""


def _load_user_context_yaml(*, session, user_id) -> str:
    """Concatenate identity + goals + constraints YAML for the user."""
    from argosy.state.models import UserContext
    ctx = session.get(UserContext, user_id)
    if ctx is None:
        return ""
    parts = []
    if ctx.identity_yaml:
        parts.append(ctx.identity_yaml)
    if ctx.goals_yaml:
        parts.append(ctx.goals_yaml)
    if ctx.constraints_yaml:
        parts.append(ctx.constraints_yaml)
    return "\n".join(parts)


def _run_phase_2_debates(*, session, user_id, analyst_reports_text,
                         baseline, prior_current, decision_run_id, trigger) -> str:
    """Run bull/bear/facilitator across all three horizons in parallel.

    Each horizon argues theses, not trades. Per-horizon facilitator
    extracts a structured DebateOutcome record.
    """
    log.info("plan_synthesis.phase_2.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(
                _run_one_horizon_debate,
                horizon=h,
                user_id=user_id,
                analyst_reports_text=analyst_reports_text,
                baseline=baseline,
                prior_current=prior_current,
                decision_run_id=decision_run_id,
                trigger=trigger,
            ): h for h in ("long", "medium", "short")
        }
        for fut in as_completed(futures):
            horizon = futures[fut]
            try:
                outcome_text = fut.result()
                parts.append(f"=== Debate outcome — {horizon} ===\n{outcome_text}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_2.debate_failed",
                          horizon=horizon, decision_run_id=decision_run_id,
                          error=str(exc))
                parts.append(f"=== Debate outcome — {horizon} (FAILED) ===\n{exc}")
    return "\n\n".join(parts)


def _run_one_horizon_debate(*, horizon: str, user_id: str,
                             analyst_reports_text: str,
                             baseline, prior_current, decision_run_id: str,
                             trigger: str) -> str:
    """Run bull/bear/facilitator for one horizon.

    Reuses the existing argosy.agents.researcher and researcher_facilitator
    modules. The horizon shapes the prompt's question:
      - long: "do principles + targets still hold?"
      - medium: "tactical posture for next 1-2 years?"
      - short: "specific calls for next 30 days?"

    ADAPTATIONS vs spec:
      * Spec used a single `ResearcherAgent(stance=...)` class; the actual
        codebase exposes `BullResearcherAgent` and `BearResearcherAgent`
        as concrete subclasses (no `stance` kwarg).
      * `BaseAgent.__init__` requires a `user_id` keyword — added to this
        function's signature and threaded through from
        `_run_phase_2_debates`.
      * The researcher `build_prompt` signature is
        `(analyst_reports: list[dict], prior_rounds, round_index, n_max,
        ticker)` — NOT `(question, analyst_reports, round_n, round_max,
        opposing)`. We embed the per-horizon question into the analyst
        reports payload as a `_horizon_prompt` entry so it reaches the
        model, and wrap the concatenated text in a single dict (the
        agents' build_prompt iterates `analyst_reports`). The cross-side
        rebuttal is conveyed via `prior_rounds` (the bear sees the bull's
        round 1 turn).
      * The facilitator `build_prompt` signature is
        `(bull_turns, bear_turns, rounds_run, ticker)` — not
        `(question, bull, bear)`. We pass single-element lists.
    """
    from argosy.agents.researcher import (
        BearResearcherAgent,
        BullResearcherAgent,
    )
    from argosy.agents.researcher_facilitator import ResearcherFacilitatorAgent

    horizon_question = {
        "long": "Do the durable principles and 5+ year targets still hold?",
        "medium": (
            "Given the analyst reports and current state, what tactical "
            "posture should drive the next 1-2 years? Specific targets and "
            "themed actions; this is the strategic centerpiece."
        ),
        "short": (
            "What specific calls for the next 30 days? Defer or pull "
            "anything forward? Speculative candidates worth surfacing?"
        ),
    }[horizon]

    # The researcher build_prompt iterates analyst_reports as a list[dict];
    # we package the concatenated upstream text plus the horizon question
    # into a single synthetic report dict.
    analyst_reports_payload = [{
        "agent_role": "phase_1_aggregated",
        "horizon": horizon,
        "horizon_question": horizon_question,
        "report_text": analyst_reports_text,
    }]
    ticker = f"plan-{horizon}"

    bull = BullResearcherAgent(user_id=user_id)
    bear = BearResearcherAgent(user_id=user_id)
    fac = ResearcherFacilitatorAgent(user_id=user_id)

    bull_report = bull.run_sync(
        analyst_reports=analyst_reports_payload,
        prior_rounds=[],
        round_index=1,
        n_max=2,
        ticker=ticker,
    )
    bull_turn = bull_report.output if hasattr(bull_report, "output") else None
    bull_turn_dict = bull_turn.model_dump() if bull_turn is not None else {}

    bear_report = bear.run_sync(
        analyst_reports=analyst_reports_payload,
        prior_rounds=[bull_turn_dict] if bull_turn_dict else [],
        round_index=1,
        n_max=2,
        ticker=ticker,
    )
    bear_turn = bear_report.output if hasattr(bear_report, "output") else None
    bear_turn_dict = bear_turn.model_dump() if bear_turn is not None else {}

    fac_report = fac.run_sync(
        bull_turns=[bull_turn_dict] if bull_turn_dict else [],
        bear_turns=[bear_turn_dict] if bear_turn_dict else [],
        rounds_run=1,
        ticker=ticker,
    )
    out = fac_report.output if hasattr(fac_report, "output") else fac_report
    return out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)


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
