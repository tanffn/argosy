"""Stage 3 — deep decision (reuse decisions/flow.py).

For each Stage-2 survivor, run the SAME full multi-agent fleet the /consult path
uses (analysts -> bull/bear -> trader -> risk team -> fund manager), producing a
fresh Buy/Sell/Hold proposal. This is PROPOSE-AND-ASK only: the funnel never
auto-executes a discretionary trade. We run at tier T2 so the full fleet + the
fund-manager integrity check always run and the proposal always lands in the
human-review queue (never auto-promoted), regardless of account class.

This is the single most expensive stage, so the orchestrator only calls it when
``decision_funnel_stage3`` is enabled AND a candidate survived triage. The
result is recorded as an immutable decision snapshot by the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from argosy.decisions.flow import ApprovedProposal, BlockedProposal, DecisionFlow
from argosy.decisions.per_ticker_analysts import (
    InsufficientAnalystQuorum,
    close_decision_run_blocked,
    open_decision_run_for_consult,
    run_per_ticker_analysts,
)
from argosy.decisions.tiers import Tier
from argosy.logging import get_logger
from argosy.services.decision_funnel.estate_kb import estate_constraints_block
from argosy.services.decision_funnel.position_context import position_context_block
from argosy.services.decision_funnel.sleeve_mandate import x10_sleeve_mandate_block

_log = get_logger("argosy.services.decision_funnel.deep_decision")


@dataclass(frozen=True)
class DeepDecisionOutcome:
    ticker: str
    status: Literal["approved", "blocked", "quorum_failed", "error"]
    decision_run_id: int | None = None
    proposal_id: int | None = None
    action: str | None = None
    blocked_reason: str | None = None
    blocked_by: str | None = None


async def run_deep_decision(
    *,
    user_id: str,
    ticker: str,
    positions_summary: str = "",
    user_constraints: str = "",
    account_class: str = "main",
    tier: Tier = Tier.T2,
    consult_mode: Literal["tactical_trade", "long_hold"] = "long_hold",
    funnel_meta: dict | None = None,
    subject_type: str = "holding",
    force: bool = False,
) -> DeepDecisionOutcome:
    """Run the full deep-decision fleet for one ticker. Never raises — returns
    a structured outcome the orchestrator records (incl. quorum / error).

    ``funnel_meta`` (source/shadow/expires_at/funnel_run_id) is threaded into
    the flow so the proposal is born with its funnel lifecycle fields set
    ATOMICALLY — a shadow proposal is never briefly client-visible.

    ``subject_type`` is the Stage-1 candidate kind: ``"discovery"`` (new-name
    pick from the high-potential funnel) additionally injects the plan-owned
    x10 SLEEVE MANDATE + live funding gap into ``user_constraints`` so the
    fleet adjudicates the name against the bounded moonshot sleeve, never as
    a core initiation (time-machine backtest lesson, 2026-07).
    """
    # Item B pushback gate — BEFORE analyst fan-out / agent spawn.
    try:
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker

        from argosy.services.verdict_registry import check_pushback_gate
        from argosy.state import db as _db_mod

        _url = str(_db_mod.get_engine().url).replace("+aiosqlite", "")
        _sf = sessionmaker(
            bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
            expire_on_commit=False,
        )
        _sess = _sf()
        try:
            _cited = None
            if funnel_meta and isinstance(funnel_meta.get("cited_new_facts"), list):
                _cited = funnel_meta["cited_new_facts"]
            _gate = check_pushback_gate(
                _sess,
                user_id=user_id,
                subject=ticker,
                cited_new_facts=_cited,
            )
        finally:
            _sess.close()
        if not force and _gate.defended and _gate.standing is not None:
            _log.info(
                "decision_funnel.verdict_defended",
                ticker=ticker,
                standing=_gate.standing.verdict,
                run_id=_gate.standing.source_decision_run_id,
            )
            return DeepDecisionOutcome(
                ticker=ticker,
                status="blocked",
                decision_run_id=_gate.standing.source_decision_run_id or 0,
                blocked_reason=_gate.reason,
                blocked_by="verdict_defended",
            )
    except Exception:  # noqa: BLE001 — gate must not crash stage 3
        _log.exception("decision_funnel.pushback_gate_failed", ticker=ticker)

    # INPUTS fix (SOFI proposal 1, 2026-07-09): the stage-3 fleet must know the
    # client's CURRENT POSITION in the ticker (shares/value/% book/account —
    # a BUY on a held name is a TOP-UP, never an initiation) and every ACTIVE
    # monitor flag on it (e.g. thesis_monitor_weakened) with its reason, so a
    # buy-more-vs-flag conflict is adjudicated explicitly. Previously the
    # orchestrator never passed positions_summary and the fleet ran position-
    # blind ("no prior holding in positions snapshot" on a ~$35.5k holding).
    # Deterministic input plumbing; best-effort — a load failure never kills
    # the funnel. Goes into BOTH positions_summary (trader) and
    # user_constraints (risk team + fund manager read that channel).
    try:
        _pos_ctx = await position_context_block(user_id=user_id, ticker=ticker)
        if _pos_ctx:
            if not positions_summary.strip():
                positions_summary = _pos_ctx
            user_constraints = (
                f"{user_constraints}\n\n{_pos_ctx}" if user_constraints.strip()
                else _pos_ctx
            )
    except Exception:  # noqa: BLE001 — inputs enrichment must not crash stage 3
        _log.exception("decision_funnel.position_context_failed", ticker=ticker)
    # INPUTS fix (time-machine backtest, 2026-07): a DISCOVERY candidate is a
    # candidate for the bounded x10/high-potential SLEEVE — the fleet must be
    # handed the sleeve mandate (plan-owned: class rationale + instrument
    # meta) + the sleeve's live funding gap, or it judges the name like a
    # core position (safety-first, initiation-sized) and kills exactly the
    # asymmetric names the sleeve exists to hold. Deterministic inputs
    # (mirrors estate_kb / position_context); best-effort — a load failure
    # never kills the funnel.
    if subject_type == "discovery":
        try:
            _mandate = await x10_sleeve_mandate_block(user_id=user_id)
            if _mandate:
                user_constraints = (
                    f"{user_constraints}\n\n{_mandate}"
                    if user_constraints.strip() else _mandate
                )
        except Exception:  # noqa: BLE001 — inputs enrichment must not crash stage 3
            _log.exception("decision_funnel.sleeve_mandate_failed", ticker=ticker)
    # INPUTS fix (verify-run 2026-07-08, SOFI): the stage-3 fleet must see
    # the estate/us-situs domain_knowledge — the FM previously noted "no
    # domain_knowledge file authorizing a US-estate rule was supplied" and
    # routed a US-domiciled BUY forward. Best-effort: a load failure never
    # kills the funnel (the deterministic floor below still guards).
    try:
        user_constraints = estate_constraints_block(user_constraints)
    except Exception:  # noqa: BLE001 — inputs enrichment must not crash stage 3
        _log.exception("decision_funnel.estate_kb_block_failed", ticker=ticker)
    try:
        pre_opened = await open_decision_run_for_consult(
            user_id=user_id, ticker=ticker, tier_value=tier.value
        )
    except Exception as exc:  # noqa: BLE001 — pre-open must not crash the funnel
        _log.warning("decision_funnel.deep_open_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", blocked_reason=str(exc)[:200],
            blocked_by="open_error",
        )
    try:
        result = await run_per_ticker_analysts(
            user_id=user_id, ticker=ticker, decision_run_id=pre_opened,
            mode=consult_mode,
        )
    except InsufficientAnalystQuorum as exc:
        await close_decision_run_blocked(
            decision_run_id=pre_opened, reason=exc.reason
        )
        _log.info("decision_funnel.deep_quorum_failed", ticker=ticker, reason=exc.reason)
        return DeepDecisionOutcome(
            ticker=ticker, status="quorum_failed", decision_run_id=pre_opened,
            blocked_reason=exc.reason, blocked_by="analyst_quorum",
        )
    except Exception as exc:  # noqa: BLE001
        await close_decision_run_blocked(
            decision_run_id=pre_opened, reason="per_ticker_analysts failure"
        )
        _log.warning("decision_funnel.deep_analysts_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", decision_run_id=pre_opened,
            blocked_reason=str(exc)[:200], blocked_by="analysts_error",
        )

    flow = DecisionFlow(user_id=user_id)
    try:
        outcome = await flow.run(
            ticker=ticker,
            tier=tier,
            analyst_reports=result.reports,
            positions_summary=positions_summary,
            user_constraints=user_constraints,
            account_class=account_class,  # type: ignore[arg-type]
            decision_run_id=pre_opened,
            persist_input_analysts=False,
            consult_mode=consult_mode,
            funnel_meta=funnel_meta,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("decision_funnel.deep_flow_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", decision_run_id=pre_opened,
            blocked_reason=str(exc)[:200], blocked_by="flow_error",
        )

    if isinstance(outcome, ApprovedProposal):
        floor_outcome = await _apply_us_situs_floor(outcome, user_id=user_id)
        if floor_outcome is not None:
            return floor_outcome
        return DeepDecisionOutcome(
            ticker=ticker, status="approved",
            decision_run_id=outcome.decision_run_id,
            proposal_id=outcome.proposal.id,
            action=outcome.proposal.action,
        )
    assert isinstance(outcome, BlockedProposal)
    return DeepDecisionOutcome(
        ticker=ticker, status="blocked",
        decision_run_id=outcome.decision_run_id,
        blocked_reason=outcome.reason, blocked_by=outcome.blocked_by,
    )


async def _floor_scope(*, ticker: str, user_id: str) -> tuple[str, str]:
    """Attribute a funnel buy to the LONG-HORIZON CORE vs a BOUNDED SLEEVE.

    Ariel's horizon-scoped estate policy (2026-07-08): the estate-safety
    requirement (Irish UCITS preferred, no US-situs) binds the 30+ year
    buy-and-hold CORE ETF allocation; bounded tactical/discovery sleeve
    positions (high-potential / x10 moonshot — SOFI-class names) MAY be
    US-domiciled, with the estate exposure annotated, never hidden.

    Attribution source = the canonical plan doc (the same object the plan's
    own domicile validator scopes by): a ticker that is an instrument of a
    plan class whose ``sigma_class`` is domicile-exempt (the high-growth /
    moonshot basket) is SLEEVE; an instrument of any other plan class is
    CORE. A ticker in NEITHER (the typical funnel discovery name) is SLEEVE
    by construction — funnel stage-3 subjects are held single names or
    discovery picks, never the core ETF allocation (sleeve-level reviews are
    deferred to the plan refresh). Any lookup failure also degrades to
    SLEEVE (with the degradation stated in the detail) — the lenient path
    still annotates loudly, so nothing is hidden.

    Returns ``(scope, detail)`` where scope is ``"core"`` or ``"sleeve"``.
    """
    fallback_detail = (
        "sleeve/core attribution unavailable at floor time — treated as a "
        "bounded-sleeve buy (funnel stage-3 buys are sleeve-bounded "
        "single-name/discovery flows by construction, never core allocation)"
    )
    try:
        from sqlalchemy import select

        from argosy.services.target_allocation_doc import (
            _DOMICILE_EXEMPT_SIGMA_CLASSES as _SLEEVE_SIGMA_CLASSES,
        )
        from argosy.services.target_allocation_doc import (
            load_plan_target_allocation,
        )
        from argosy.state import db as db_mod
        from argosy.state.models import PlanVersion

        async with db_mod.get_session() as session:
            pv = (
                await session.execute(
                    select(PlanVersion).where(
                        PlanVersion.user_id == user_id,
                        PlanVersion.role == "current",
                    )
                )
            ).scalar_one_or_none()
        doc = load_plan_target_allocation(pv) if pv is not None else None
        if doc is None:
            return ("sleeve", fallback_detail)
        t = (ticker or "").upper()
        for c in doc.classes:
            if any((i.symbol or "").upper() == t for i in c.instruments):
                if c.sigma_class in _SLEEVE_SIGMA_CLASSES:
                    return (
                        "sleeve",
                        f"instrument of bounded plan sleeve class "
                        f"'{c.label}' (sigma_class={c.sigma_class})",
                    )
                return ("core", f"instrument of long-horizon plan core class '{c.label}'")
        return (
            "sleeve",
            "not an instrument of any long-horizon plan core class — a "
            "bounded single-name/discovery position",
        )
    except Exception:  # noqa: BLE001 — attribution must not crash the floor
        _log.exception("decision_funnel.us_situs_floor_scope_failed", ticker=ticker)
        return ("sleeve", fallback_detail)


async def _apply_us_situs_floor(
    outcome: ApprovedProposal, *, user_id: str = ""
) -> DeepDecisionOutcome | None:
    """The deterministic estate/us-situs FLOOR over funnel-originated buys —
    HORIZON-SCOPED per Ariel's 2026-07-08 policy refinement.

    Uses the SAME rule module that guards deploy/plan
    (``argosy.quality.plan_risk_kernel.evaluate_us_situs`` — NVDA is the one
    sanctioned US-situs name; unknown symbols fail CLOSED there), then scopes
    the violation by sleeve attribution (``_floor_scope``):

    * LONG-HORIZON CORE buy (instrument of a non-exempt plan class): the
      strict floor holds — block US-situs non-NVDA, fail closed on unknown
      domicile. The 30+ year buy-and-hold allocation must be estate-safe.
    * BOUNDED SLEEVE buy (high-potential / moonshot class, discovery names
      not in the core plan, or attribution unavailable): NOT blocked for US
      domicile. Instead the estate exposure is ANNOTATED on the proposal
      (rationale + ProposalHistory) so nothing is hidden; unknown domicile is
      additionally flagged for instrument_reference curation.

    This stays inviolable-arithmetic-floor territory (estate rule), NOT a
    judgment gate — it never evaluates whether the buy is a good idea.

    Returns ``None`` when the buy proceeds (sells/holds out of scope — we
    cannot unwind history, only gate new flows; sleeve buys proceed with the
    annotation persisted best-effort). On a CORE violation the persisted
    proposal is flipped to ``blocked`` (with a ProposalHistory row recording
    the reason) and a blocked outcome is returned so the funnel trace records
    ``blocked_by='us_situs_floor'``.
    """
    prop = outcome.proposal
    if (prop.action or "").lower() != "buy":
        return None

    from argosy.quality.plan_risk_kernel import evaluate_us_situs

    amount = float(prop.size_shares_or_currency or 0.0) or 1.0
    result = evaluate_us_situs({}, proposed_buys={prop.ticker: amount})
    if result.ok:
        return None

    reason = "; ".join(v.detail for v in result.violations)
    scope, scope_detail = await _floor_scope(ticker=prop.ticker, user_id=user_id)
    if scope != "core":
        return await _accept_sleeve_estate_exposure(
            outcome, user_id=user_id, scope_detail=scope_detail, kernel_reason=reason
        )

    reason = f"long-horizon core buy ({scope_detail}): {reason}"
    _log.warning(
        "decision_funnel.us_situs_floor_blocked",
        ticker=prop.ticker, proposal_id=prop.id, reason=reason[:300],
    )
    try:
        await _mark_proposal_blocked_by_floor(
            proposal_id=prop.id,
            decision_run_id=outcome.decision_run_id,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 — must not crash the funnel; log LOUD
        _log.exception(
            "decision_funnel.us_situs_floor_persist_failed",
            ticker=prop.ticker, proposal_id=prop.id,
        )
        return DeepDecisionOutcome(
            ticker=prop.ticker, status="error",
            decision_run_id=outcome.decision_run_id, proposal_id=prop.id,
            blocked_reason=(
                "us_situs floor violation could NOT be persisted to the "
                f"proposal row — manual review required: {reason}"
            ),
            blocked_by="us_situs_floor",
        )
    return DeepDecisionOutcome(
        ticker=prop.ticker, status="blocked",
        decision_run_id=outcome.decision_run_id, proposal_id=prop.id,
        action=prop.action, blocked_reason=reason, blocked_by="us_situs_floor",
    )


async def _accept_sleeve_estate_exposure(
    outcome: ApprovedProposal, *, user_id: str, scope_detail: str, kernel_reason: str
) -> DeepDecisionOutcome | None:
    """A bounded-sleeve buy with US-situs (or unknown-domicile) estate
    exposure PROCEEDS, with the exposure annotated on the proposal so nothing
    is hidden. Always returns ``None`` (the buy is never blocked here);
    annotation persistence is best-effort and a failure only logs LOUD —
    accepting the buy is the policy, the annotation is the transparency."""
    prop = outcome.proposal
    unknown_domicile = False
    try:
        from argosy.services.instrument_reference import lookup as _ref

        unknown_domicile = _ref(prop.ticker) is None
    except Exception:  # noqa: BLE001
        _log.exception("decision_funnel.us_situs_sleeve_ref_lookup_failed", ticker=prop.ticker)

    pct_of_book = await _buy_pct_of_book(prop, user_id=user_id)
    size_str = (
        f"~{pct_of_book:.2f}% of book" if pct_of_book is not None
        else f"size={float(prop.size_shares_or_currency or 0.0):g} "
             f"{getattr(prop, 'size_units', '') or 'units'}"
    )
    if unknown_domicile:
        annotation = (
            f"ESTATE NOTE (us_situs_floor): {prop.ticker} domicile UNKNOWN "
            f"(not in instrument_reference) — treated as US-situs "
            f"conservatively; estate exposure accepted for bounded sleeve "
            f"({scope_detail}), {size_str}. CURATION FLAG: confirm domicile "
            f"and add {prop.ticker} to instrument_reference. Long-horizon "
            f"core buys remain strictly gated."
        )
        _log.warning(
            "decision_funnel.us_situs_curation_needed",
            ticker=prop.ticker, proposal_id=prop.id,
        )
    else:
        annotation = (
            f"ESTATE NOTE (us_situs_floor): {prop.ticker} is US-situs — "
            f"estate exposure accepted for bounded sleeve ({scope_detail}), "
            f"{size_str}. Long-horizon core buys of US-domiciled instruments "
            f"remain blocked; NVDA is the one sanctioned US-situs core sleeve."
        )
    _log.info(
        "decision_funnel.us_situs_sleeve_accepted",
        ticker=prop.ticker, proposal_id=prop.id,
        unknown_domicile=unknown_domicile,
        scope_detail=scope_detail[:200], kernel_reason=kernel_reason[:300],
    )
    try:
        await _annotate_proposal_estate_exposure(
            proposal_id=prop.id, annotation=annotation
        )
    except Exception:  # noqa: BLE001 — annotation must not crash the funnel
        _log.exception(
            "decision_funnel.us_situs_sleeve_annotation_failed",
            ticker=prop.ticker, proposal_id=prop.id,
        )
    return None


async def _buy_pct_of_book(prop, *, user_id: str) -> float | None:
    """Best-effort buy size as % of the tradeable book (for the estate
    annotation). Only computable for currency-sized proposals with a loadable
    book; returns ``None`` otherwise — the annotation then carries the raw
    size instead."""
    units = (getattr(prop, "size_units", "") or "").lower()
    if units not in ("currency", "usd"):
        return None
    amount = float(prop.size_shares_or_currency or 0.0)
    if amount <= 0 or not user_id:
        return None
    try:
        from argosy.services.decision_funnel.book import load_book
        from argosy.state import db as db_mod

        async with db_mod.get_session() as session:
            book = await session.run_sync(
                lambda s: load_book(s, user_id=user_id)
            )
        total_usd = sum(h.usd_value_k for h in book) * 1000.0
        if total_usd <= 0:
            return None
        return 100.0 * amount / total_usd
    except Exception:  # noqa: BLE001 — cosmetic figure only
        _log.debug("decision_funnel.us_situs_pct_of_book_failed", ticker=prop.ticker)
        return None


async def _annotate_proposal_estate_exposure(
    *, proposal_id: int | None, annotation: str
) -> None:
    """Append the estate-exposure annotation to the persisted proposal's
    rationale (client-visible) + an audit ProposalHistory row (status
    unchanged). No-op when the flow ran with persistence skipped
    (``proposal_id`` falsy) — the structured log above still carries it."""
    if not proposal_id:
        return
    from argosy.state import db as db_mod
    from argosy.state.models import Proposal as ProposalRow, ProposalHistory

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is None:
            return
        base = (row.rationale_summary or "").rstrip()
        row.rationale_summary = f"{base}\n\n{annotation}" if base else annotation
        session.add(
            ProposalHistory(
                proposal_id=proposal_id,
                status=row.status,  # annotation, not a transition
                transitioned_by="us_situs_floor",
                note=annotation[:2000],
            )
        )
        await session.commit()


async def _mark_proposal_blocked_by_floor(
    *, proposal_id: int | None, decision_run_id: int | None, reason: str
) -> None:
    """Flip the already-persisted proposal (and its decision run) to
    ``blocked`` and append the audit-trail history row. No-op when the flow
    ran with persistence skipped (``proposal_id`` falsy)."""
    if not proposal_id:
        return
    from argosy.state import db as db_mod
    from argosy.state.models import (
        DecisionRun,
        Proposal as ProposalRow,
        ProposalHistory,
    )

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is not None:
            row.status = "blocked"
            session.add(
                ProposalHistory(
                    proposal_id=proposal_id,
                    status="blocked",
                    transitioned_by="us_situs_floor",
                    note=reason[:2000],
                )
            )
        if decision_run_id:
            run = await session.get(DecisionRun, decision_run_id)
            if run is not None:
                run.status = "blocked"
        await session.commit()


__all__ = ["DeepDecisionOutcome", "run_deep_decision"]
