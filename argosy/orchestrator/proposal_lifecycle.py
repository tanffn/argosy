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

from typing import Any

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
    account_class: str = "argonaut",
    rationale_summary: str = "",
    exit_trigger: str = "",
    execution_mode: str = "paper",
    paper: bool = True,
    **_extra: Any,
) -> ProposalRow:
    """Persist one speculation-origin proposal and return the ORM row.

    Notes:
      - ``size_usd`` lands in ``size_shares_or_currency`` with units
        ``currency`` so downstream cost-basis / risk math reads it as a
        dollar number, not a share count.
      - ``account_class`` accepts the wider Argonaut value (the column is
        a free-form ``String(16)``; only the pydantic transport object
        narrows to ``Literal["main", "limited"]``).
      - The ``paper`` flag is not persisted on ``proposals`` — it lives
        on ``Fill`` rows once the order is filled. We thread it back in
        the return tuple via ``RouteResult`` so the caller can stamp
        downstream PaperFill/audit records correctly.
      - ``exit_trigger`` and ``execution_mode`` are stashed inside
        ``rationale_summary`` (prefix-tagged with ``[exit]`` / ``[mode]``)
        so the existing schema carries them without a migration. A
        future migration may move them to dedicated columns.

    Requires a ``session`` keyword argument bound to an open
    SQLAlchemy session.

    .. note::
       This helper **commits the session itself** (see ``session.commit()``
       below). Callers must therefore not have uncommitted state that
       should remain pending — any pending writes will be flushed and
       committed together with the proposal + history rows. If the caller
       wants transactional isolation, it must use a separate session.
    """
    if session is None:
        raise ValueError(
            "create_speculative_proposal requires session="
        )

    # TODO(plan-distillate-wave4): promote ``exit_trigger`` and
    # ``execution_mode`` to dedicated columns on the ``proposals`` table
    # so we no longer have to stash them as ``[exit]`` / ``[mode]``
    # prefix-tagged lines inside ``rationale_summary``. This requires an
    # alembic migration + a backfill step that parses the prefixes out of
    # existing speculation-origin rows.
    summary = rationale_summary or ""
    if exit_trigger:
        summary = f"{summary}\n[exit] {exit_trigger}".strip()
    if execution_mode and execution_mode != "paper":
        summary = f"{summary}\n[mode] {execution_mode}".strip()

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
        rationale_summary=summary,
        expected_impact_json="",
        confidence="MEDIUM",
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
    session.commit()
    return row


__all__ = ["create_speculative_proposal"]
