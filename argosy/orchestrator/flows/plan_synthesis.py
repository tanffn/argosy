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
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import get_active_baseline, get_current_plan, get_pending_draft

log = get_logger(__name__)


Trigger = Literal["scheduled", "check_in", "quarterly", "annual"]


def _emit_event(event_type: str, payload: dict) -> None:
    """Best-effort fire-and-forget publish from sync code (M2 fix).

    Delegates to ``publish_event_threadsafe`` which centralises the
    sync→async bridge and uses a threading.Lock so it is safe when called
    from asyncio.to_thread worker threads (monthly_cycle path).
    Any failure is swallowed — synthesis must never break because of a
    flaky event subscriber.
    """
    from argosy.api.events import publish_event_threadsafe

    publish_event_threadsafe(event_type, payload)


class NoBaselineError(Exception):
    """Raised when a user has no active baseline plan."""


@dataclass
class SynthesisResult:
    decision_run_id: int
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

    # Open a real DecisionRun row so PlanVersion.decision_run_id (Integer FK)
    # is valid and the SDD §6.11 audit lineage is real, not fictitious.
    # ticker="(plan)" and tier="T3" are sentinels for plan-revision runs
    # (distinct from per-trade runs which carry the actual ticker + tier).
    decision_run = DecisionRun(
        user_id=user_id,
        ticker="(plan)",
        tier="T3",
        decision_kind="plan_revision",
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    session.add(decision_run)
    session.commit()
    session.refresh(decision_run)
    decision_run_id: int = decision_run.id  # integer PK — used for PlanVersion FK

    # String-form audit token for agent_reports.decision_id (String column).
    # Kept separate so the agent_reports audit trail has a human-readable ref.
    decision_audit_token: str = f"plan-synth-{decision_run_id}"

    log.info(
        "plan_synthesis.start",
        user_id=user_id,
        trigger=trigger,
        decision_run_id=decision_run_id,
    )
    _emit_event("plan.draft.started", {"user_id": user_id, "trigger": trigger})

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
    # Phases 1-5 receive the string audit token (used for log annotations and
    # agent_reports.decision_id which is a String column). The integer FK is
    # only written to PlanVersion and SynthesisInputs below.
    analyst_reports_text = _run_phase_1_analysts(
        session=session, user_id=user_id, baseline=baseline,
        prior_current=prior_current, decision_run_id=decision_audit_token,
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
        decision_run_id=decision_audit_token, trigger=trigger,
    )

    # Phase 3: synthesize.
    output: PlanSynthesisOutput = _run_phase_3_synthesizer(
        session=session, user_id=user_id,
        baseline=baseline, prior_current=prior_current,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_summary=portfolio_summary,
        fills_summary=fills_summary,
        decision_run_id=decision_audit_token,
    )

    # Phase 4: risk team plan-level review.
    risk_verdict = _run_phase_4_risk(
        session=session, user_id=user_id, draft_output=output,
        analyst_reports_text=analyst_reports_text,
        decision_run_id=decision_audit_token,
    )

    # Phase 5: fund manager integrity check.
    approved = _run_phase_5_fund_manager(
        session=session, user_id=user_id, draft_output=output,
        risk_verdict=risk_verdict, decision_run_id=decision_audit_token,
    )
    if not approved:
        log.error("plan_synthesis.fm_rejected",
                  user_id=user_id, decision_run_id=decision_run_id)
        raise RuntimeError("fund manager rejected synthesized plan")

    # Persist as role='draft'.
    # decision_run_id here is the integer PK — aligns with the Integer FK on
    # plan_versions.decision_run_id and satisfies Postgres type checking.
    inputs = output.inputs.model_copy(update={
        "baseline_id": baseline.id,
        "prior_current_id": prior_current.id if prior_current else None,
        "decision_run_id": decision_run_id,  # int
    })

    draft = PlanVersion(
        user_id=user_id,
        role="draft",
        version_label=f"synth-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
        source_path="",
        raw_markdown="",
        decision_run_id=decision_run_id,  # int FK
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

    # Stamp the DecisionRun row as finished — provides the audit lineage
    # SDD §6.11 promises: you can reconstruct the full synthesis by joining
    # plan_versions.decision_run_id → decision_runs.id.
    decision_run.finished_at = datetime.now(timezone.utc)
    decision_run.status = "completed"
    session.commit()

    log.info("plan_synthesis.draft_persisted",
             user_id=user_id, draft_id=draft.id, decision_run_id=decision_run_id)
    _emit_event("plan.draft.completed", {"user_id": user_id, "draft_id": draft.id})
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
    """Plan-level risk verdict from three perspectives + facilitator merge.

    Runs the aggressive / neutral / conservative ``RiskOfficerAgent`` in
    parallel (one verdict per perspective) and then asks the
    ``RiskFacilitatorAgent`` to consolidate. Returns a single text blob
    that the synthesizer's downstream steps embed in the draft transcript.

    ADAPTATIONS vs spec:
      * Spec used ``RiskOfficerAgent(stance=...)``; the actual class is
        constructed as ``RiskOfficerAgent(user_id=..., perspective=...)``
        (kwarg renamed; ``user_id`` now mandatory). ``_make_risk_officer``
        normalises the kwarg.
      * Spec called the officer with ``run_sync(draft_plan=...,
        analyst_reports=...)``; the actual ``build_prompt`` signature is
        ``(proposal, analyst_reports: list[dict], user_constraints,
        risk_caps, prior_rounds, round_index, n_max)``. We adapt by
        passing the draft plan as the ``proposal`` dict and wrapping the
        upstream concatenated text in a single synthetic analyst report
        dict (mirroring the Phase-2 adaptation).
      * Spec called the facilitator with ``run_sync(draft_plan=...,
        risk_reviews=...)``; the actual ``build_prompt`` signature is
        ``(verdicts: list[dict], rounds_run: int)``. We parse each
        per-perspective JSON output back into a dict and pass it through;
        on parse failure we fall back to a sentinel dict so the
        facilitator at least sees the perspective.
    """
    log.info("plan_synthesis.phase_4.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    raw_outputs: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_run_one_risk_perspective,
                      stance=stance, user_id=user_id,
                      draft_output=draft_output,
                      analyst_reports_text=analyst_reports_text,
                      decision_run_id=decision_run_id): stance
            for stance in ("aggressive", "neutral", "conservative")
        }
        for fut in as_completed(futures):
            stance = futures[fut]
            try:
                payload = fut.result()
                raw_outputs[stance] = payload
                parts.append(f"=== Risk {stance} ===\n{payload}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_4.risk_failed",
                          stance=stance, decision_run_id=decision_run_id,
                          error=str(exc))
                parts.append(f"=== Risk {stance} (FAILED) ===\n{exc}")

    # Facilitator merge.
    from argosy.agents.risk_facilitator import RiskFacilitatorAgent
    try:
        facilitator = RiskFacilitatorAgent(user_id=user_id)
    except TypeError:
        # Fallback for stubbed/legacy constructors that don't accept user_id.
        facilitator = RiskFacilitatorAgent()  # type: ignore[call-arg]

    # Build a list[dict] of verdicts for the real facilitator signature.
    # Best-effort JSON parse; on failure the facilitator still sees the
    # perspective so it can ESCALATE rather than silently drop the voice.
    verdicts: list[dict] = []
    for stance, raw in raw_outputs.items():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed.setdefault("perspective", stance)
                parsed.setdefault("round_index", 1)
                verdicts.append(parsed)
            else:
                verdicts.append({"perspective": stance, "round_index": 1,
                                 "verdict": "ESCALATE", "raw": raw})
        except (ValueError, TypeError):
            verdicts.append({"perspective": stance, "round_index": 1,
                             "verdict": "ESCALATE", "raw": raw})

    try:
        merged = facilitator.run_sync(verdicts=verdicts, rounds_run=1)
        merged_out = getattr(merged, "output", merged)
        merged_text = (
            merged_out.model_dump_json()
            if hasattr(merged_out, "model_dump_json") else str(merged_out)
        )
        parts.append(f"=== Risk facilitator verdict ===\n{merged_text}")
    except Exception as exc:  # noqa: BLE001
        log.error("plan_synthesis.phase_4.facilitator_failed",
                  decision_run_id=decision_run_id, error=str(exc))
        parts.append(f"=== Risk facilitator (FAILED) ===\n{exc}")

    return "\n\n".join(parts)


def _make_risk_officer(stance: str, *, user_id: str | None = None):
    """Return a ``RiskOfficerAgent`` configured for the requested stance.

    ADAPTATION: the real ``RiskOfficerAgent.__init__`` signature is
    ``(user_id: str, perspective: Perspective)`` — the spec used a
    ``stance`` kwarg with no ``user_id``. We accept ``stance`` as a
    positional argument (to match the test stub
    ``_fake_officer(stance)``) and translate to ``perspective``;
    ``user_id`` is keyword-only because the test monkeypatches this
    helper and never passes one.
    """
    from argosy.agents.risk_officer import RiskOfficerAgent
    return RiskOfficerAgent(user_id=user_id or "system", perspective=stance)  # type: ignore[arg-type]


def _run_one_risk_perspective(*, stance: str, user_id: str,
                              draft_output: PlanSynthesisOutput,
                              analyst_reports_text: str,
                              decision_run_id: str) -> str:
    """Run one risk-officer perspective and return its output as text.

    ADAPTATION: ``_make_risk_officer`` is a documented monkeypatch seam
    used by tests; the spec test's stub has signature ``(stance)`` only,
    so we attempt the keyword-arg path (``user_id``) first and fall
    back to the bare positional form when the patched stub doesn't
    accept it. Mirrors the ``_safe_run_agent`` retry pattern used in
    Phase 1.
    """
    try:
        officer = _make_risk_officer(stance, user_id=user_id)
    except TypeError:
        officer = _make_risk_officer(stance)

    # Real RiskOfficerAgent.build_prompt expects:
    #   proposal: dict, analyst_reports: list[dict], user_constraints: str,
    #   risk_caps: dict, prior_rounds: list[dict], round_index: int, n_max: int
    # The test stub accepts **kw, so it ignores anything we pass.
    try:
        proposal = json.loads(draft_output.model_dump_json())
    except (ValueError, TypeError):
        proposal = {"raw": str(draft_output)}

    analyst_reports_payload = [{
        "agent_role": "phase_1_aggregated",
        "report_text": analyst_reports_text,
    }]

    result = officer.run_sync(
        proposal=proposal,
        analyst_reports=analyst_reports_payload,
        user_constraints="",
        risk_caps={},
        prior_rounds=[],
        round_index=1,
        n_max=1,
    )
    out = getattr(result, "output", result)
    return out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)


def _make_fund_manager():
    """Factory seam for the fund manager agent.

    Zero-arg by design so tests can monkeypatch with a bare lambda.
    A hardcoded ``user_id="system"`` is used because the plan-revision
    integrity check is a system-level decision (not attributable to an
    individual user's run); the per-user audit trail is captured via the
    decision_run_id and the caller's ``user_id`` kwarg threaded into the
    log line below.

    ADAPTATION vs spec: ``BaseAgent.__init__`` requires ``user_id`` as a
    mandatory keyword (see Tasks 2.5/2.7-2.9), so ``FundManagerAgent()``
    with no args from the spec would TypeError. We pass a fixed sentinel
    here; the test's monkeypatched lambda ignores it entirely.
    """
    from argosy.agents.fund_manager import FundManagerAgent
    return FundManagerAgent(user_id="system")


def _run_phase_5_fund_manager(*, session, user_id,
                              draft_output: PlanSynthesisOutput,
                              risk_verdict: str, decision_run_id: str) -> bool:
    """Final integrity check.

    Validates:
      - distillate hard-constraints honored
      - three horizons cohere
      - every target has rationale + cited source
      - 'no_change' justified by evidence if claimed

    Returns True to green-light the draft, False to reject.
    """
    log.info("plan_synthesis.phase_5.start",
             user_id=user_id, decision_run_id=decision_run_id)
    fm = _make_fund_manager()
    result = fm.run_sync(
        decision_kind="plan_revision",
        draft_plan=draft_output.model_dump_json(),
        risk_verdict=risk_verdict,
    )
    out = result.output

    # The plan-revision path validates against FundManagerPlanRevisionDecision
    # which has a typed `approved` bool — read it directly when available.
    # Fall back to JSON parsing for test stubs that return generic objects.
    if hasattr(out, "approved"):
        approved = bool(out.approved)
    else:
        payload_text = out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
        try:
            payload = json.loads(payload_text)
            approved = bool(payload.get("approved", False))
        except (ValueError, TypeError):
            log.error("plan_synthesis.phase_5.payload_unparseable",
                      decision_run_id=decision_run_id, payload=payload_text)
            return False

    log.info("plan_synthesis.phase_5.verdict",
             user_id=user_id, decision_run_id=decision_run_id,
             approved=approved)
    return approved


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
            suffix = f" — {t.rationale}" if t.rationale else ""
            lines.append(
                f"- **{t.label}**: {t.value} {t.unit} "
                f"(stated {t.stated_at.isoformat()}; revisit {t.revisit_after.isoformat()})"
                f"{suffix}"
            )
        lines.append("")
    if section.themes:
        lines.append("## Themes")
        for th in section.themes:
            th_suffix = f" — {th.rationale}" if th.rationale else ""
            lines.append(f"- **{th.label}** ({th.direction}){th_suffix}")
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
