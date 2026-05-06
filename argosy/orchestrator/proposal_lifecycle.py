"""Thin synchronous helpers around the `proposals` table.

Wave 3 (plan-distillate). The decision-flow path
(``argosy.decisions.flow.DecisionFlow``) creates proposal rows from the
analyst -> trader -> fund-manager pipeline using its own async
``_persist_proposal`` helper. That path is the source-of-truth for full
T1/T2/T3 trade decisions.

The speculation router (``argosy.orchestrator.speculation_router``)
needs a much narrower entry point: it has already chosen the ticker,
size, and tier (T0) from a synthesizer-emitted speculative candidate
that the user accepted. It just needs to write a single ``proposals``
row and return its ``.id`` so downstream UI / paper-fill plumbing can
take it from there.

ADAPTATION (vs. the plan): the plan referenced
``argosy.orchestrator.proposal_lifecycle.create_speculative_proposal``
as if it already existed. It did not. Per the plan's own guidance
("expose a thin helper in whatever module currently writes ``proposals``
rows"), this module is that thin helper. It does NOT duplicate the full
decision-flow logic — it just persists a row with the same column
shape that ``DecisionFlow._persist_proposal`` produces, plus a
ProposalHistory entry recording the speculation-router origin.

The router calls this through an indirection seam (``_create_proposal``)
so unit tests can monkeypatch it without ever touching the DB.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from argosy.decisions.proposals import ProposalStatus
from argosy.state.models import Proposal as ProposalRow, ProposalHistory


def create_speculative_proposal(
    *,
    session: Session | None = None,
    user_id: str,
    ticker: str,
    action: str = "buy",
    size_usd: float,
    order_type: str = "limit",
    tier: str = "T0",
    account_class: str = "limited",
    rationale_summary: str = "",
    exit_trigger: str = "",
    execution_mode: str = "paper",
    paper: bool = True,
    decision_run_id: int | None = None,
) -> ProposalRow:
    """Persist one speculation-origin proposal and return the ORM row.

    Notes:
      - ``size_usd`` lands in ``size_shares_or_currency`` with units
        ``currency`` so downstream cost-basis / risk math reads it as a
        dollar number, not a share count.
      - ``account_class`` defaults to ``"limited"`` (the DB/code value
        the broker router in ``argosy/execution/router.py`` checks; the
        "Argonaut" feature is the user-facing name for that class).
      - The ``paper`` flag is not persisted on ``proposals`` — it lives
        on ``Fill`` rows once the order is filled. We thread it back in
        the return tuple via ``RouteResult`` so the caller can stamp
        downstream PaperFill/audit records correctly.
      - ``exit_trigger`` and ``execution_mode`` are persisted in the
        existing JSON column ``expected_impact_json`` rather than smuggled
        as prefix-tagged lines inside ``rationale_summary`` — that column
        already exists on the ``proposals`` table and was previously
        unused for speculation rows.
      - ``decision_run_id`` (I1) carries the synthesis-run audit lineage
        from the originating PlanVersion forward onto the proposals row
        — so the SDD §6.11 promise ("you can reconstruct the full
        synthesis by joining plan_versions.decision_run_id →
        decision_runs.id") extends to speculation-origin proposals too.
        ProposalHistory has no ``decision_run_id`` column of its own;
        joining ProposalHistory.proposal_id → proposals.decision_run_id
        recovers the lineage when reading history rows.

    Requires a ``session`` keyword argument bound to an open
    SQLAlchemy session.

    .. note::
       This helper **flushes** the session to populate ``row.id`` but
       does NOT commit. The caller (``route_accepted_candidate``) owns
       the transaction and is responsible for committing — this avoids
       the prior split-ownership where both helper and caller could
       commit, mixing concerns.
    """
    if session is None:
        raise ValueError(
            "create_speculative_proposal requires the `session` keyword argument"
        )

    # TODO(plan-distillate-wave4): if the product surfaces filtering /
    # search by exit_trigger or execution_mode, promote them from this
    # JSON column to dedicated columns via an alembic migration + a
    # one-shot backfill. For now the JSON column is sufficient — the
    # data is read back in the proposal-detail UI, never aggregated.
    expected_impact_json = json.dumps({
        "exit_trigger": exit_trigger,
        "execution_mode": execution_mode,
    })

    row = ProposalRow(
        user_id=user_id,
        ticker=ticker,
        action=action,
        size_shares_or_currency=float(size_usd),
        size_units="currency",
        instrument="stock",
        order_type=order_type,
        tier=tier,
        account_class=account_class,
        status=ProposalStatus.DRAFT.value,
        rationale_summary=rationale_summary or "",
        expected_impact_json=expected_impact_json,
        confidence="MEDIUM",
        decision_run_id=decision_run_id,
    )
    session.add(row)
    session.flush()  # populate row.id

    history = ProposalHistory(
        proposal_id=row.id,
        status=row.status,
        transitioned_by="speculation_router",
        note=f"speculation-origin candidate; paper={paper}; mode={execution_mode}",
    )
    session.add(history)
    session.flush()
    return row


__all__ = ["create_speculative_proposal"]
