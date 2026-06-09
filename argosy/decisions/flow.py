"""Decision-flow orchestration (SDD §3, §10.3, Phase 3).

Composes the agents: analysts (already produced) → bull/bear debate →
trader → 3-perspective risk team → fund manager → proposal.

Tier-conditional steps per SDD §4.1 "Agents that run":

  T0: trader (Sonnet) + rule-based risk preflight (no LLM risk team)
  T1: + 1-round bull/bear + 1 risk perspective (neutral)
  T2: 9 analysts + 2-round debate + 3-perspective risk team + fund manager
  T3: T2 stack + plan-critique sign-off (RED gate) + 24h cooling-off
        marker + next-day re-check stub

Persistence: every step writes to `agent_reports`. The final outcome
(`ApprovedProposal` or `BlockedProposal`) writes to `proposals` and
links the producing `decision_runs` row to its `agent_reports` rows
via `decision_id` (we use the decision-run id as decision_id for the
agent_reports rows).

Mocking story: the flow takes agent-factory callables for every role,
so tests construct mocked subclasses without touching the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.agents.base import AgentReport
from argosy.agents.fund_manager import FundManagerAgent, FundManagerDecision
from argosy.agents.researcher import (
    BearResearcherAgent,
    BullResearcherAgent,
)
from argosy.agents.researcher_facilitator import (
    DebateOutcome,
    ResearcherFacilitatorAgent,
)
from argosy.agents.risk_facilitator import RiskFacilitatorAgent, RiskOutcome
from argosy.agents.risk_officer import (
    Perspective,
    RiskOfficerAgent,
)
from argosy.agents.trader import TraderAgent, TraderProposal
from argosy.api.events import publish_event
from argosy.decisions.proposals import Proposal, ProposalStatus
from argosy.decisions.tiers import Tier
from argosy.logging import get_logger
from argosy.services.negotiation_recorder import record_negotiation_phase
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionRun,
    Proposal as ProposalRow,
    ProposalHistory,
)


_log = get_logger("argosy.decisions.flow")


# ----------------------------------------------------------------------
# Outcome types
# ----------------------------------------------------------------------


@dataclass
class ApprovedProposal:
    """Fund manager green-lit. Flow returns this and a `Proposal` row."""

    proposal: Proposal
    fund_manager: FundManagerDecision
    risk_outcome: RiskOutcome | None
    debate_outcome: DebateOutcome | None
    decision_run_id: int


@dataclass
class BlockedProposal:
    """Fund manager blocked, or risk team rejected, or T3 plan-critique RED."""

    reason: str
    # Reasons the trade was blocked. ``trader_insufficient_data`` is
    # distinct from ``trader_hold``: HOLD means the analysis completed
    # and the recommendation is to wait; INSUFFICIENT_DATA means the
    # analysis couldn't complete because load-bearing inputs were
    # missing or flagged-unusable AFTER remediation.
    blocked_by: str  # 'fund_manager' | 'risk_team' | 'plan_critique_red' | 'trader_hold' | 'trader_insufficient_data'
    fund_manager: FundManagerDecision | None = None
    risk_outcome: RiskOutcome | None = None
    debate_outcome: DebateOutcome | None = None
    decision_run_id: int = 0


@dataclass
class FlowConfig:
    """Override knobs for tests / advanced runs."""

    debate_rounds_t1: int = 1
    debate_rounds_t2: int = 2
    debate_rounds_t3: int = 2
    cooling_off_hours: int | None = None  # None → read from agent_settings.tiers.cooling_off_hours_t3
    skip_persistence: bool = False  # tests may set True

    def resolve_cooling_off_hours(self, user_id: str, fallback: int = 24) -> int:
        """Resolved cooling-off hours, preferring agent_settings over default."""
        if self.cooling_off_hours is not None:
            return self.cooling_off_hours
        try:
            from argosy.agent_settings import load_agent_settings

            settings = load_agent_settings(user_id)
            return int(settings.tiers.cooling_off_hours_t3)
        except Exception:
            return fallback


# ----------------------------------------------------------------------
# Agent factory callables. Each takes user_id (and optionally tier) and
# returns the agent instance. Tests pass mock subclasses.
# ----------------------------------------------------------------------


_BullFactory = Callable[[str], BullResearcherAgent]
_BearFactory = Callable[[str], BearResearcherAgent]
_ResearcherFacFactory = Callable[[str], ResearcherFacilitatorAgent]
_TraderFactory = Callable[[str, str], TraderAgent]  # (user_id, tier)
_RiskFactory = Callable[[str, Perspective], RiskOfficerAgent]
_RiskFacFactory = Callable[[str], RiskFacilitatorAgent]
_FundFactory = Callable[[str], FundManagerAgent]


# ----------------------------------------------------------------------
# Flow
# ----------------------------------------------------------------------


@dataclass
class DecisionFlow:
    """Full decision team. Build with factories; call `.run(...)`."""

    user_id: str = "ariel"
    config: FlowConfig = field(default_factory=FlowConfig)
    settings: AgentSettings | None = None
    bull_factory: _BullFactory | None = None
    bear_factory: _BearFactory | None = None
    researcher_facilitator_factory: _ResearcherFacFactory | None = None
    trader_factory: _TraderFactory | None = None
    risk_officer_factory: _RiskFactory | None = None
    risk_facilitator_factory: _RiskFacFactory | None = None
    fund_manager_factory: _FundFactory | None = None

    def _settings(self) -> AgentSettings:
        if self.settings is None:
            self.settings = load_agent_settings(self.user_id)
        return self.settings

    def _bull(self) -> BullResearcherAgent:
        return (self.bull_factory or (lambda u: BullResearcherAgent(user_id=u)))(self.user_id)

    def _bear(self) -> BearResearcherAgent:
        return (self.bear_factory or (lambda u: BearResearcherAgent(user_id=u)))(self.user_id)

    def _researcher_fac(self) -> ResearcherFacilitatorAgent:
        return (
            self.researcher_facilitator_factory
            or (lambda u: ResearcherFacilitatorAgent(user_id=u))
        )(self.user_id)

    def _trader(self, tier: str) -> TraderAgent:
        if self.trader_factory is not None:
            return self.trader_factory(self.user_id, tier)
        return TraderAgent(user_id=self.user_id, tier=tier)

    def _risk_officer(self, perspective: Perspective) -> RiskOfficerAgent:
        if self.risk_officer_factory is not None:
            return self.risk_officer_factory(self.user_id, perspective)
        return RiskOfficerAgent(user_id=self.user_id, perspective=perspective)

    def _risk_fac(self) -> RiskFacilitatorAgent:
        return (
            self.risk_facilitator_factory
            or (lambda u: RiskFacilitatorAgent(user_id=u))
        )(self.user_id)

    def _fund_manager(self) -> FundManagerAgent:
        return (
            self.fund_manager_factory or (lambda u: FundManagerAgent(user_id=u))
        )(self.user_id)

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        ticker: str,
        tier: Tier,
        analyst_reports: list[AgentReport],
        positions_summary: str = "",
        plan_critique: dict | None = None,
        user_constraints: str = "",
        risk_caps: dict[str, Any] | None = None,
        account_class: Literal["main", "limited"] = "main",
        now: Callable[[], datetime] | None = None,
        decision_run_id: int | None = None,
        persist_input_analysts: bool = True,
        consult_mode: Literal["tactical_trade", "long_hold"] = "long_hold",
    ) -> ApprovedProposal | BlockedProposal:
        """Run the full pipeline for the given tier.

        ``decision_run_id`` + ``persist_input_analysts`` (codex BLOCKER #2
        on the per-ticker-analysts design — 2026-05-30):

        - When ``decision_run_id`` is supplied, ``_open_decision_run`` is
          skipped; the flow runs under the caller's pre-opened id. Used
          by ``argosy.decisions.per_ticker_analysts`` which opens the
          run before calling its 6 always-on analysts so the analyst
          rows + downstream phase rows all join under one id.
        - When ``persist_input_analysts=False``, the input analyst
          reports are assumed already persisted by the caller; the
          flow skips its own ``_persist_agent_reports`` call to avoid
          duplicate rows.

        Existing callers (monthly_cycle, amendment paths) keep the
        default behaviour: ``decision_run_id=None`` → open a fresh
        run; ``persist_input_analysts=True`` → persist as before.
        """
        risk_caps = risk_caps or {}
        clock = now or _utcnow

        analyst_dicts = [
            {"agent_role": r.agent_role, **r.output.model_dump()}
            for r in analyst_reports
        ]

        analysts_started_at = clock()
        if decision_run_id is None:
            decision_run_id = await self._open_decision_run(
                ticker=ticker, tier=tier, started_at=analysts_started_at
            )

        # Persist analyst reports + record the analysts phase — unless the
        # caller (e.g. per_ticker_analysts) already did both.
        if persist_input_analysts:
            analyst_ids = await self._persist_agent_reports(
                decision_run_id, analyst_reports
            )
            # Provenance Wave C — record analyst phase. No facilitator verdict;
            # the TL;DR will list the participating analysts and confidences.
            try:
                await record_negotiation_phase(
                    user_id=self.user_id, decision_run_id=decision_run_id,
                    kind="analysts", started_at=analysts_started_at,
                    finished_at=clock(),
                    agent_report_ids=analyst_ids, verdict=None,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning(
                    "decision_flow.record_phase_failed",
                    phase="analysts", run_id=decision_run_id, error=str(exc),
                )

        # ---------------- Researcher debate ----------------
        debate_outcome: DebateOutcome | None = None
        bull_turns: list[dict] = []
        bear_turns: list[dict] = []
        debate_ids: list[int] = []
        debate_side: dict[int, str] = {}
        debate_round: dict[int, int] = {}
        if tier in (Tier.T1, Tier.T2, Tier.T3):
            debate_started_at = clock()
            n_rounds = self._rounds_for(tier)
            for r_idx in range(1, n_rounds + 1):
                bull_agent = self._bull()
                prior = _interleave(bull_turns, bear_turns)
                bull_turn = await bull_agent.run(
                    analyst_reports=analyst_dicts,
                    prior_rounds=prior,
                    round_index=r_idx,
                    n_max=n_rounds,
                    ticker=ticker,
                )
                bull_ids = await self._persist_agent_reports(
                    decision_run_id, [bull_turn]
                )
                debate_ids.extend(bull_ids)
                for bid in bull_ids:
                    debate_side[bid] = "bull"
                    debate_round[bid] = r_idx
                bull_turns.append(bull_turn.output.model_dump())

                bear_agent = self._bear()
                prior = _interleave(bull_turns, bear_turns)
                bear_turn = await bear_agent.run(
                    analyst_reports=analyst_dicts,
                    prior_rounds=prior,
                    round_index=r_idx,
                    n_max=n_rounds,
                    ticker=ticker,
                )
                bear_ids = await self._persist_agent_reports(
                    decision_run_id, [bear_turn]
                )
                debate_ids.extend(bear_ids)
                for bid in bear_ids:
                    debate_side[bid] = "bear"
                    debate_round[bid] = r_idx
                bear_turns.append(bear_turn.output.model_dump())

            facilitator = self._researcher_fac()
            fac_report = await facilitator.run(
                bull_turns=bull_turns,
                bear_turns=bear_turns,
                rounds_run=n_rounds,
                ticker=ticker,
            )
            fac_ids = await self._persist_agent_reports(
                decision_run_id, [fac_report]
            )
            debate_ids.extend(fac_ids)
            debate_outcome = fac_report.output  # type: ignore[assignment]
            try:
                await record_negotiation_phase(
                    user_id=self.user_id, decision_run_id=decision_run_id,
                    kind="researcher_debate", started_at=debate_started_at,
                    finished_at=clock(), agent_report_ids=debate_ids,
                    verdict=debate_outcome,
                    side_by_id=debate_side, round_by_id=debate_round,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "decision_flow.record_phase_failed",
                    phase="researcher_debate", run_id=decision_run_id,
                    error=str(exc),
                )

        # ---------------- Trader ----------------
        trader_started_at = clock()
        trader = self._trader(tier.value)
        trader_report = await trader.run(
            analyst_reports=analyst_dicts,
            debate_outcome=(debate_outcome.model_dump() if debate_outcome else {}),
            positions_snapshot=positions_summary,
            user_constraints=user_constraints,
            tier=tier.value,
            mode=consult_mode,
            ticker=ticker,
        )
        trader_ids = await self._persist_agent_reports(
            decision_run_id, [trader_report]
        )
        trader_proposal: TraderProposal = trader_report.output  # type: ignore[assignment]
        try:
            await record_negotiation_phase(
                user_id=self.user_id, decision_run_id=decision_run_id,
                kind="trader", started_at=trader_started_at,
                finished_at=clock(), agent_report_ids=trader_ids,
                verdict=trader_proposal,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "decision_flow.record_phase_failed",
                phase="trader", run_id=decision_run_id, error=str(exc),
            )

        if trader_proposal.action == "hold":
            await self._close_decision_run(
                decision_run_id, finished_at=clock(), status="hold", fm="hold"
            )
            return BlockedProposal(
                reason=f"Trader returned HOLD: {trader_proposal.rationale_summary}",
                blocked_by="trader_hold",
                debate_outcome=debate_outcome,
                decision_run_id=decision_run_id,
            )

        if trader_proposal.action == "insufficient_data":
            # Distinct early-exit from HOLD — the trader couldn't
            # complete the analysis (load-bearing data missing AFTER
            # remediation). Surfaces in the UI as a separate verdict
            # so the user knows the system didn't fail-soft into HOLD.
            # See SDD §3.3 + [[feedback_agents_talk_to_each_other]].
            await self._close_decision_run(
                decision_run_id,
                finished_at=clock(),
                status="insufficient_data",
                fm="insufficient_data",
            )
            return BlockedProposal(
                reason=(
                    f"Trader returned INSUFFICIENT_DATA: "
                    f"{trader_proposal.rationale_summary}"
                ),
                blocked_by="trader_insufficient_data",
                debate_outcome=debate_outcome,
                decision_run_id=decision_run_id,
            )

        # ---------------- Risk team ----------------
        risk_outcome: RiskOutcome | None = None
        if tier == Tier.T0:
            # T0 has no LLM risk team; rule-based preflight is the gate
            # (called by the caller after this returns; we record an
            # implicit "APPROVE" so the fund manager has something to
            # read).
            risk_outcome = None
        else:
            risk_started_at = clock()
            risk_ids: list[int] = []
            risk_perspective: dict[int, str] = {}
            risk_round: dict[int, int] = {}
            verdicts: list[dict] = []
            n_rounds = self._rounds_for(tier)
            perspectives_for_tier: list[Perspective] = (
                ["neutral"] if tier == Tier.T1 else ["aggressive", "neutral", "conservative"]
            )

            for r_idx in range(1, n_rounds + 1):
                round_verdicts: list[dict] = []
                for perspective in perspectives_for_tier:
                    officer = self._risk_officer(perspective)
                    rep = await officer.run(
                        proposal=trader_proposal.model_dump(),
                        analyst_reports=analyst_dicts,
                        user_constraints=user_constraints,
                        risk_caps=risk_caps,
                        prior_rounds=verdicts,
                        round_index=r_idx,
                        n_max=n_rounds,
                    )
                    rep_ids = await self._persist_agent_reports(
                        decision_run_id, [rep]
                    )
                    risk_ids.extend(rep_ids)
                    for rid in rep_ids:
                        risk_perspective[rid] = perspective
                        risk_round[rid] = r_idx
                    round_verdicts.append(rep.output.model_dump())
                verdicts.extend(round_verdicts)

            facilitator = self._risk_fac()
            risk_fac_report = await facilitator.run(
                verdicts=verdicts, rounds_run=n_rounds
            )
            risk_fac_ids = await self._persist_agent_reports(
                decision_run_id, [risk_fac_report]
            )
            risk_ids.extend(risk_fac_ids)
            risk_outcome = risk_fac_report.output  # type: ignore[assignment]
            try:
                await record_negotiation_phase(
                    user_id=self.user_id, decision_run_id=decision_run_id,
                    kind="risk_team", started_at=risk_started_at,
                    finished_at=clock(), agent_report_ids=risk_ids,
                    verdict=risk_outcome,
                    perspective_by_id=risk_perspective, round_by_id=risk_round,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "decision_flow.record_phase_failed",
                    phase="risk_team", run_id=decision_run_id, error=str(exc),
                )

            if risk_outcome.consensus_verdict == "REJECT":
                await self._close_decision_run(
                    decision_run_id, finished_at=clock(), status="blocked", fm="block"
                )
                return BlockedProposal(
                    reason=(
                        "Risk team consensus REJECT; "
                        f"dissent: {risk_outcome.dissent_summary}"
                    ),
                    blocked_by="risk_team",
                    risk_outcome=risk_outcome,
                    debate_outcome=debate_outcome,
                    decision_run_id=decision_run_id,
                )

        # ---------------- T3 plan-critique RED gate ----------------
        if tier == Tier.T3 and plan_critique is not None:
            findings = plan_critique.get("findings", []) or []
            red_touching = [
                f
                for f in findings
                if f.get("severity") == "RED"
                and ticker.upper() in (f.get("plan_item_ref", "") + " " + f.get("topic", "")).upper()
            ]
            if red_touching:
                await self._close_decision_run(
                    decision_run_id, finished_at=clock(), status="blocked", fm="block"
                )
                return BlockedProposal(
                    reason=(
                        "T3 plan-critique RED finding touches this proposal; "
                        f"first finding: {red_touching[0].get('summary', '')}"
                    ),
                    blocked_by="plan_critique_red",
                    risk_outcome=risk_outcome,
                    debate_outcome=debate_outcome,
                    decision_run_id=decision_run_id,
                )

        # ---------------- Fund manager (T2/T3 only per SDD §4.1) ----------------
        # T0/T1 path uses the trader proposal + rule-based risk preflight only;
        # the LLM fund-manager integrity check is reserved for material/strategic
        # tiers where the cost (~Opus) is justified.
        fm_decision: FundManagerDecision | None = None
        if tier in (Tier.T2, Tier.T3):
            fm_started_at = clock()
            fm_agent = self._fund_manager()
            fm_report = await fm_agent.run(
                proposal=trader_proposal.model_dump(),
                risk_outcome=(risk_outcome.model_dump() if risk_outcome else None),
                plan_critique=plan_critique,
                user_constraints=user_constraints,
                tier=tier.value,
            )
            fm_ids = await self._persist_agent_reports(
                decision_run_id, [fm_report]
            )
            fm_decision = fm_report.output  # type: ignore[assignment]
            try:
                await record_negotiation_phase(
                    user_id=self.user_id, decision_run_id=decision_run_id,
                    kind="fund_manager", started_at=fm_started_at,
                    finished_at=clock(), agent_report_ids=fm_ids,
                    verdict=fm_decision,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "decision_flow.record_phase_failed",
                    phase="fund_manager", run_id=decision_run_id, error=str(exc),
                )

            if fm_decision.decision == "block":
                await self._close_decision_run(
                    decision_run_id, finished_at=clock(), status="blocked", fm="block"
                )
                return BlockedProposal(
                    reason=fm_decision.reason,
                    blocked_by="fund_manager",
                    fund_manager=fm_decision,
                    risk_outcome=risk_outcome,
                    debate_outcome=debate_outcome,
                    decision_run_id=decision_run_id,
                )

        # ---------------- Build the proposal ----------------
        # Phase 5 routing matrix (SDD §10.1): T0/T1 in the limited account
        # auto-promote past `awaiting_human` straight to `approved` so the
        # execution router (called by the caller) can run them. T2/T3
        # always go through human review even in the limited account, and
        # T3 still enters cooling first.
        cooling_until = None
        initial_status = ProposalStatus.AWAITING_HUMAN
        is_limited_t0t1 = (
            account_class == "limited" and tier in (Tier.T0, Tier.T1)
        )
        # Honor `queue_only` global mode and per-account override.
        settings = self._settings()
        global_mode = settings.execution.default_mode
        limited_mode = settings.limited_account.execution_mode
        queue_only = global_mode == "queue_only" or limited_mode == "queue_only"
        if is_limited_t0t1 and not queue_only:
            initial_status = ProposalStatus.APPROVED
        if tier == Tier.T3:
            cooling_until = clock() + timedelta(
                hours=self.config.resolve_cooling_off_hours(self.user_id)
            )
            initial_status = ProposalStatus.COOLING

        proposal = Proposal(
            user_id=self.user_id,
            ticker=trader_proposal.ticker,
            action=trader_proposal.action,
            size_shares_or_currency=trader_proposal.size_shares_or_currency,
            size_units=trader_proposal.size_units,
            instrument=trader_proposal.instrument,
            order_type=trader_proposal.order_type,
            limit_price=trader_proposal.limit_price,
            stop_price=trader_proposal.stop_price,
            time_in_force=trader_proposal.time_in_force,
            tier=tier.value,  # type: ignore[arg-type]
            account_class=account_class,
            status=initial_status,
            rationale_summary=trader_proposal.rationale_summary,
            expected_impact=trader_proposal.expected_impact,
            confidence=trader_proposal.confidence.value,
            cooling_off_until=cooling_until,
            decision_run_id=decision_run_id,
        )

        # T0/T1 paths skip the fund-manager LLM call entirely (SDD §4.1).
        # Record the proposal under "trader" with fm_decision=None so the
        # audit trail honestly reflects which agents ran.
        transitioned_by = "fund_manager" if fm_decision is not None else "trader"
        if proposal.status == ProposalStatus.APPROVED:
            transitioned_by = "auto_execute:limited_t0t1"
        proposal_id = await self._persist_proposal(
            proposal, fm_decision=fm_decision, transitioned_by=transitioned_by
        )
        proposal.id = proposal_id

        # Phase 5 audit_log: when auto-promoted, write the dedicated
        # `auto_promoted: True` event so downstream tooling (audit page,
        # CLI) can surface the path the proposal took.
        if proposal.status == ProposalStatus.APPROVED and is_limited_t0t1:
            from argosy.execution.audit import record_audit_event as _audit

            try:
                await _audit(
                    user_id=self.user_id,
                    event_type="proposal.auto_promoted",
                    entity_type="proposal",
                    entity_id=str(proposal_id),
                    payload={
                        "tier": tier.value,
                        "account_class": account_class,
                        "auto_promoted": True,
                    },
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("flow.auto_promote_audit_failed")

        await self._close_decision_run(
            decision_run_id,
            finished_at=clock(),
            status="approved",
            fm="green_light" if fm_decision is not None else None,
            proposal_id=proposal_id,
        )

        try:
            await publish_event(
                "proposal.created",
                {
                    "proposal_id": proposal_id,
                    "user_id": self.user_id,
                    "ticker": proposal.ticker,
                    "tier": tier.value,
                    "status": proposal.status.value,
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("proposal.publish_failed")

        return ApprovedProposal(
            proposal=proposal,
            fund_manager=fm_decision,
            risk_outcome=risk_outcome,
            debate_outcome=debate_outcome,
            decision_run_id=decision_run_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rounds_for(self, tier: Tier) -> int:
        return {
            Tier.T1: self.config.debate_rounds_t1,
            Tier.T2: self.config.debate_rounds_t2,
            Tier.T3: self.config.debate_rounds_t3,
        }[tier]

    async def _open_decision_run(
        self, *, ticker: str, tier: Tier, started_at: datetime
    ) -> int:
        if self.config.skip_persistence:
            return 0
        async with db_mod.get_session() as session:
            row = DecisionRun(
                user_id=self.user_id,
                ticker=ticker,
                tier=tier.value,
                started_at=started_at,
                status="running",
            )
            session.add(row)
            await session.commit()
            return row.id

    async def _close_decision_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: str,
        fm: str | None = None,
        proposal_id: int | None = None,
    ) -> None:
        if self.config.skip_persistence or run_id == 0:
            return
        async with db_mod.get_session() as session:
            row = await session.get(DecisionRun, run_id)
            if row is None:
                return
            row.finished_at = finished_at
            row.status = status
            if fm is not None:
                row.fund_manager_decision = fm
            if proposal_id is not None:
                row.proposal_id = proposal_id
            await session.commit()

    async def _persist_agent_reports(
        self, decision_run_id: int, reports: list[AgentReport]
    ) -> list[int]:
        """Persist agent reports under this decision_run_id.

        Returns the list of inserted ``agent_reports.id`` values in the
        same order as ``reports`` so callers can hand them to the
        provenance recorder (Wave C).
        """
        if self.config.skip_persistence or decision_run_id == 0:
            return []
        ids: list[int] = []
        async with db_mod.get_session() as session:
            for r in reports:
                row = AgentReportRow(
                    user_id=r.user_id,
                    agent_role=r.agent_role,
                    decision_id=str(decision_run_id),
                    prompt_hash=r.prompt_hash,
                    response_text=r.response_text,
                    tokens_in=r.tokens_in,
                    tokens_out=r.tokens_out,
                    cost_usd=float(r.cost_usd),
                    model=r.model,
                    confidence=r.confidence.value if r.confidence else None,
                    # Wave A — Anthropic Messages API telemetry (migration 0026).
                    cache_input_tokens=r.cache_input_tokens,
                    cache_creation_tokens=r.cache_creation_tokens,
                    thinking_tokens=r.thinking_tokens,
                    citations_json=r.citations_json,
                    # Wave B-UI Task 9 — sources serialised from build_prompt (migration 0027).
                    sources_json=r.sources_json,
                    # Wave B-UI follow-up Item 2 — correlation id for O(1) WS↔DB
                    # linking in useDecisionStream (migration 0028).
                    run_correlation_id=r.run_correlation_id,
                    # Wave B-UI follow-up Item B — full prompts for the Prompt
                    # tab (migration 0029).
                    system_prompt=r.system_prompt,
                    user_prompt=r.user_prompt,
                )
                session.add(row)
                await session.flush()
                ids.append(row.id)
            await session.commit()
        return ids

    async def _persist_proposal(
        self,
        proposal: Proposal,
        *,
        fm_decision: FundManagerDecision,
        transitioned_by: str,
    ) -> int:
        if self.config.skip_persistence:
            return 0
        async with db_mod.get_session() as session:
            # T4.4: best-effort plan lineage (audit) — the canonical plan
            # version this proposal traces to. Never blocks a proposal.
            plan_version_id = None
            try:
                from sqlalchemy import desc as _desc, select as _select

                from argosy.state.models import PlanVersion as _PV

                plan_version_id = (await session.execute(
                    _select(_PV.id)
                    .where(_PV.user_id == proposal.user_id, _PV.role == "current")
                    .order_by(_desc(_PV.id))
                    .limit(1)
                )).scalar_one_or_none()
            except Exception:  # noqa: BLE001 — lineage is best-effort
                plan_version_id = None
            row = ProposalRow(
                user_id=proposal.user_id,
                ticker=proposal.ticker,
                action=proposal.action,
                size_shares_or_currency=proposal.size_shares_or_currency,
                size_units=proposal.size_units,
                instrument=proposal.instrument,
                order_type=proposal.order_type,
                limit_price=proposal.limit_price,
                stop_price=proposal.stop_price,
                time_in_force=proposal.time_in_force,
                tier=proposal.tier,
                account_class=proposal.account_class,
                status=proposal.status.value,
                rationale_summary=proposal.rationale_summary,
                expected_impact_json=proposal.expected_impact.model_dump_json(),
                confidence=proposal.confidence,
                cooling_off_until=proposal.cooling_off_until,
                decision_run_id=proposal.decision_run_id,
                plan_version_id=plan_version_id,
            )
            session.add(row)
            await session.flush()
            history = ProposalHistory(
                proposal_id=row.id,
                status=row.status,
                transitioned_by=transitioned_by,
                note=(
                    fm_decision.reason
                    if fm_decision is not None
                    else "trader proposal approved at tier T0/T1 (no fund-manager LLM call per SDD §4.1)"
                ),
            )
            session.add(history)
            await session.commit()
            return row.id


# ----------------------------------------------------------------------
# Helpers (module-level)
# ----------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _interleave(bull: list[dict], bear: list[dict]) -> list[dict]:
    """Build a chronological prior-round list. Bull goes first per round."""
    out: list[dict] = []
    for i in range(max(len(bull), len(bear))):
        if i < len(bull):
            out.append({**bull[i], "side": "bull"})
        if i < len(bear):
            out.append({**bear[i], "side": "bear"})
    return out


__all__ = [
    "ApprovedProposal",
    "BlockedProposal",
    "DecisionFlow",
    "FlowConfig",
]
