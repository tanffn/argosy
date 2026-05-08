"""plan_synthesis orchestrator — five-phase entry point and phase implementations.

Calling convention for monkeypatch compatibility
-------------------------------------------------
All helpers that tests may monkeypatch are called via the package namespace
(``_pkg.<name>``) rather than as bare module-level names.  This means when a
test does::

    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", ...)

the attribute on the *package* ``__init__`` is replaced, and the call site
here resolves through that same namespace — so the patch takes effect.

Import: ``from argosy.orchestrator.flows import plan_synthesis as _pkg``
is performed lazily inside each function to avoid circular-import risk
(the package's ``__init__`` imports from this module).
"""

from __future__ import annotations

import inspect
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import cast

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
from argosy.orchestrator.flows.plan_synthesis._types import (
    NoBaselineError,
    SynthesisResult,
    Trigger,
)
from argosy.agents.sentiment_analyst import SentimentAnalystAgent
from argosy.agents.tax_analyst import TaxAnalystAgent
from argosy.agents.technical_analyst import TechnicalAnalystAgent
from argosy.logging import get_logger
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import get_active_baseline, get_current_plan, get_pending_draft

log = get_logger(__name__)


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


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def run_synthesis(
    session: Session,
    *,
    user_id: str,
    trigger: Trigger,
    guidance: str = "",
    existing_decision_run_id: int | None = None,
):
    """Execute the 5-phase synthesis. Writes a role='draft' row.

    Args:
        guidance: optional free-text from the user's check-in to weight
            the synthesis (e.g. "weight tax analyst more heavily").
        existing_decision_run_id: when set, reuse this DecisionRun row
            for audit lineage instead of opening a fresh one. Used by
            the plan_amendment_chat large worker so a single decision_run
            spans amendment dispatch + synthesis (no smeared lineage
            across two unrelated rows). When None, behaves as before:
            opens a fresh `decision_kind='plan_revision'` row.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    baseline = get_active_baseline(session, user_id)
    if baseline is None:
        raise NoBaselineError(f"user {user_id!r} has no active baseline plan")

    prior_current = get_current_plan(session, user_id)

    if existing_decision_run_id is not None:
        # Reuse the caller's row (e.g. plan_amendment_chat large worker)
        # so the lineage chat-turn → DecisionRun → draft is a single
        # path, not two rows tied together by convention. The caller is
        # responsible for stamping the row's started_at + decision_kind.
        decision_run = session.get(DecisionRun, existing_decision_run_id)
        if decision_run is None:
            raise RuntimeError(
                f"existing_decision_run_id={existing_decision_run_id} not found",
            )
        decision_run_id = decision_run.id
    else:
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
        decision_run_id = decision_run.id  # integer PK — used for PlanVersion FK

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
    analyst_reports_text = _pkg._run_phase_1_analysts(
        session=session, user_id=user_id, baseline=baseline,
        prior_current=prior_current, decision_run_id=decision_audit_token,
        guidance=guidance,
    )

    # Assemble inputs for Phases 2+.
    portfolio_summary = _pkg._assemble_portfolio_summary(session=session, user_id=user_id)
    fills_summary = _pkg._assemble_fills_summary(session=session, user_id=user_id)

    # Phase 2: per-horizon debates.
    debate_outcomes_text = _pkg._run_phase_2_debates(
        session=session, user_id=user_id,
        analyst_reports_text=analyst_reports_text,
        baseline=baseline, prior_current=prior_current,
        decision_run_id=decision_audit_token, trigger=trigger,
    )

    # Wave 3 / Task 3.2: load the per-user speculation cap so we can both
    # (a) tell the synthesizer prompt about it and (b) post-validate the
    # candidates the model emits.  Failure to load a cap is non-fatal —
    # we fall back to ``SpeculationCap()`` defaults (0.1% NW, 3 positions),
    # which the validator still applies.  Speculation caps must NEVER
    # silently disable themselves on a config blip.
    from argosy.config import SpeculationCap, get_user_agent_settings, load_speculation_cap
    try:
        cap = load_speculation_cap(
            user_id=user_id,
            agent_settings=get_user_agent_settings(user_id),
        )
    except Exception as exc:  # noqa: BLE001
        # Logs and events are complementary: the warning lives in stderr /
        # log aggregation for ops, and the structured event lets a UI
        # subscriber raise a user-visible alert ("we couldn't load your
        # speculation cap; reverting to defaults — please review your
        # agent_settings.yaml").  Wave 2 fix I3 introduced
        # ``publish_event_threadsafe`` precisely so synthesis (which can
        # run on a worker thread via monthly_cycle) can emit cleanly.
        log.warning(
            "plan_synthesis.speculation_cap_load_failed_using_default",
            user_id=user_id, error=str(exc),
        )
        _emit_event(
            "plan.synthesis.cap_load_failed",
            {"user_id": user_id, "error": str(exc)},
        )
        cap = SpeculationCap()  # conservative default — never disable the cap.

    # Phase 3: synthesize.
    output: PlanSynthesisOutput = _pkg._run_phase_3_synthesizer(
        session=session, user_id=user_id,
        baseline=baseline, prior_current=prior_current,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_summary=portfolio_summary,
        fills_summary=fills_summary,
        decision_run_id=decision_audit_token,
        speculation_cap_pct=cap.max_pct_of_net_worth,
        speculation_cap_concurrent=cap.max_concurrent_positions,
    )

    # Defense-in-depth: post-filter speculative candidates that breach
    # the cap or lack ``risk_ceiling_check``.  Resolved via the package
    # namespace so tests that monkeypatch ``flow._enforce_speculation_cap``
    # are honoured.
    output = _pkg._enforce_speculation_cap(
        output,
        max_pct_of_net_worth=cap.max_pct_of_net_worth,
        max_concurrent_positions=cap.max_concurrent_positions,
    )

    # Phase 4: risk team plan-level review.
    risk_verdict = _pkg._run_phase_4_risk(
        session=session, user_id=user_id, draft_output=output,
        analyst_reports_text=analyst_reports_text,
        decision_run_id=decision_audit_token,
    )

    # Phase 5: fund manager integrity check.
    approved = _pkg._run_phase_5_fund_manager(
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
        horizon_long_md=_pkg._horizon_md(output.long),
        horizon_medium_md=_pkg._horizon_md(output.medium),
        horizon_short_md=_pkg._horizon_md(output.short),
        synthesis_inputs_json=inputs.model_dump_json(),
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)

    # Stamp the DecisionRun row as finished — provides the audit lineage
    # SDD §6.11 promises: you can reconstruct the full synthesis by joining
    # plan_versions.decision_run_id → decision_runs.id.
    #
    # Skipped when the caller passed `existing_decision_run_id` (e.g. the
    # plan_amendment_chat large worker): the caller owns the row and may
    # need to re-check cancellation between synthesis-end and the
    # completed stamp. Stamping here would race that check.
    if existing_decision_run_id is None:
        decision_run.finished_at = datetime.now(timezone.utc)
        decision_run.status = "completed"
        session.commit()

    # Invalidate the home-brief cache so the "ready to review" draft bullet
    # surfaces immediately (within the same request cycle) rather than waiting
    # for the 30-minute TTL to expire.  Failure is swallowed — synthesis must
    # never abort because of a flaky cache layer.
    from argosy.adapters.data.cache import invalidate_home_brief
    invalidate_home_brief(user_id)

    log.info("plan_synthesis.draft_persisted",
             user_id=user_id, draft_id=draft.id, decision_run_id=decision_run_id)
    _emit_event("plan.draft.completed", {"user_id": user_id, "draft_id": draft.id})
    return SynthesisResult(decision_run_id=decision_run_id, draft_id=draft.id)


# ----------------------------------------------------------------------
# Phase implementations (default — call existing fleet agents)
# ----------------------------------------------------------------------


# Resolved at call time via the package's sys.modules entry so
# monkeypatch.setattr on the package-level names (used by tests) takes effect.
# Capturing the class refs in a tuple at import time would freeze them and
# bypass the patch.
# Names are alphabetical for deterministic log output.
_PHASE_1_AGENT_NAMES = (
    "ConcentrationAnalystAgent",
    "FxAnalystAgent",
    "FundamentalsAnalystAgent",
    "MacroAnalystAgent",
    "NewsAnalystAgent",
    "PlanCritiqueAgent",
    "SentimentAnalystAgent",
    "TaxAnalystAgent",
    "TechnicalAnalystAgent",
)


def _run_phase_1_analysts(*, session, user_id, baseline, prior_current,
                           decision_run_id, guidance) -> str:
    """Run all 9 analysts in parallel. Concatenate their reports as text."""
    log.info("plan_synthesis.phase_1.start",
             user_id=user_id, decision_run_id=decision_run_id)

    # Resolve agent classes via the *package* module (argosy.orchestrator.flows.plan_synthesis)
    # so tests that monkeypatch ``argosy.orchestrator.flows.plan_synthesis.<Name>`` are
    # honoured.  We cannot use sys.modules[__name__] here because __name__ is
    # the submodule (…plan_synthesis.orchestrator), not the package.
    _pkg_mod = sys.modules["argosy.orchestrator.flows.plan_synthesis"]
    phase_1_agents = tuple(getattr(_pkg_mod, name) for name in _PHASE_1_AGENT_NAMES)

    # Each agent's run_sync(...) signature varies; we pass a shared kwargs
    # bag and rely on each agent's build_prompt to consume what it needs.
    # The base agents' run_sync forwards **kwargs to build_prompt.
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    common_kwargs = dict(
        plan_label=baseline.version_label or "Imported plan",
        plan_markdown=baseline.distillate_rendered or "",
        snapshot_label=f"synthesis-{decision_run_id}",
        snapshot_summary=_pkg._assemble_portfolio_summary(session=session, user_id=user_id),
        user_context_yaml=_pkg._load_user_context_yaml(session=session, user_id=user_id),
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
        # inspect.signature gives only the declared parameters; co_varnames
        # would include all locals too and falsely accept them.
        sig = inspect.signature(agent.build_prompt)
        accepted = set(sig.parameters.keys())
        narrowed = {k: v for k, v in kwargs.items() if k in accepted}
        result = agent.run_sync(**narrowed)
        out = getattr(result, "output", None)
        if out is not None and hasattr(out, "model_dump_json"):
            return out.model_dump_json()
        return str(out) if out is not None else ""


def _run_phase_2_debates(*, session, user_id, analyst_reports_text,
                         baseline, prior_current, decision_run_id, trigger) -> str:
    """Run bull/bear/facilitator across all three horizons in parallel.

    Each horizon argues theses, not trades. Per-horizon facilitator
    extracts a structured DebateOutcome record.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    log.info("plan_synthesis.phase_2.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(
                _pkg._run_one_horizon_debate,
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
                             decision_run_id,
                             speculation_cap_pct: float | None = None,
                             speculation_cap_concurrent: int | None = None,
                             ) -> PlanSynthesisOutput:
    """Default Phase 3: call PlanSynthesizerAgent.

    ``speculation_cap_pct`` / ``speculation_cap_concurrent`` (Wave 3, Task
    3.2): when set, the synthesizer prompt includes a HARD CONSTRAINT
    block telling the model to keep speculative_candidates within those
    bounds.  Defense-in-depth: ``_enforce_speculation_cap`` re-validates
    after the model returns, so a model that fluffs the constraint cannot
    harm the user.  Both kwargs default to None for backwards compat with
    tests / call sites that don't load the cap.
    """
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
        speculation_cap_pct=speculation_cap_pct,
        speculation_cap_concurrent=speculation_cap_concurrent,
    )
    return result.output  # type: ignore[attr-defined]


def _enforce_speculation_cap(
    output: PlanSynthesisOutput,
    *,
    max_pct_of_net_worth: float,
    max_concurrent_positions: int,
) -> PlanSynthesisOutput:
    """Drop any speculative candidates that breach the cap.

    The synthesizer prompt is told the cap, but defense-in-depth: we
    enforce it here so a model that fluffs the constraint cannot harm
    the user.

    Drops candidates that:
      - exceed ``max_pct_of_net_worth``
      - lack ``risk_ceiling_check`` (i.e. the synthesizer didn't
        affirmatively confirm the position fits the user's bound)
    Also enforces the concurrent-position limit by truncating after
    ``max_concurrent_positions`` survivors (deterministic order — first
    candidates emitted by the model win).
    """
    if not output.short.speculative_candidates:
        return output

    kept = []
    for c in output.short.speculative_candidates:
        if c.suggested_position_pct_of_net_worth > max_pct_of_net_worth:
            log.warning(
                "plan_synthesis.speculative_dropped_over_cap",
                ticker=c.ticker,
                pct=c.suggested_position_pct_of_net_worth,
                cap=max_pct_of_net_worth,
            )
            continue
        if not c.risk_ceiling_check:
            log.warning(
                "plan_synthesis.speculative_dropped_no_ceiling_check",
                ticker=c.ticker,
            )
            continue
        kept.append(c)
        if len(kept) >= max_concurrent_positions:
            break

    if len(kept) == len(output.short.speculative_candidates):
        return output
    new_short = output.short.model_copy(update={"speculative_candidates": kept})
    return output.model_copy(update={"short": new_short})


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
    from argosy.orchestrator.flows import plan_synthesis as _pkg

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
    from argosy.agents.risk_officer import Perspective, RiskOfficerAgent
    return RiskOfficerAgent(user_id=user_id or "system", perspective=cast(Perspective, stance))


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

    Resolves ``_make_risk_officer`` through the package namespace so that
    ``monkeypatch.setattr(flow, "_make_risk_officer", ...)`` intercepts
    the call.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    try:
        officer = _pkg._make_risk_officer(stance, user_id=user_id)
    except TypeError:
        officer = _pkg._make_risk_officer(stance)

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


def _make_fund_manager(user_id: str | None = None):
    """Factory seam for the fund manager agent.

    Accepts an optional ``user_id`` so the real audit trail names the
    requesting user; falls back to ``"system"`` when not provided (e.g.
    scheduled runs or test monkeypatches).

    Test stubs use ``lambda *args, **kw: _FakeFM(...)`` so the optional
    argument is safely ignored without breaking the call site.

    ADAPTATION vs spec: ``BaseAgent.__init__`` requires ``user_id`` as a
    mandatory keyword (see Tasks 2.5/2.7-2.9), so ``FundManagerAgent()``
    with no args from the spec would TypeError. We pass a sentinel when
    no real user is available.
    """
    from argosy.agents.fund_manager import FundManagerAgent
    return FundManagerAgent(user_id=user_id or "system")


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
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    log.info("plan_synthesis.phase_5.start",
             user_id=user_id, decision_run_id=decision_run_id)
    fm = _pkg._make_fund_manager(user_id=user_id)
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
