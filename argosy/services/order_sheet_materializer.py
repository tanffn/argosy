"""Persist a validated order sheet into Argosy's executable proposal spine."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.decisions.proposals import ProposalStatus
from argosy.services.order_sheet import OrderAction, OrderSheet, require_valid_order_sheet
from argosy.state.models import (
    DecisionRun,
    Lot,
    PlanVersion,
    Proposal,
    ProposalHistory,
)


def order_sheet_fingerprint(sheet: OrderSheet) -> str:
    payload = sheet.model_dump(mode="json")
    # Backward-compatible schema extension: pre-comparison artifacts were
    # fingerprinted without this key.  Empty/default comparisons therefore do
    # not invalidate an otherwise unchanged stored sheet.
    if not payload.get("candidate_comparisons"):
        payload.pop("candidate_comparisons", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _recognized_account(account_id: str) -> bool:
    value = account_id.strip().lower()
    return value.startswith(("leumi", "schwab", "ibkr"))


def resolve_order_sheet_accounts(
    session: Session,
    sheet: OrderSheet,
    *,
    funding_account_id: str | None = None,
    sell_accounts_by_symbol: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Resolve exact custody without guessing.

    With zero new cash, one unambiguous sell account is also the exact source
    of the buy funding, so buys remain in that account. New external cash still
    requires an explicit/configured funding account.
    """
    supplied_sells = {
        str(symbol).upper(): str(account).strip()
        for symbol, account in (sell_accounts_by_symbol or {}).items()
    }
    sell_accounts: dict[str, str] = {}
    for line in sheet.lines:
        if line.action not in (OrderAction.SELL, OrderAction.TRIM):
            continue
        account = supplied_sells.get(line.symbol, "")
        if not account:
            accounts = set(
                session.execute(
                    select(Lot.account_id).where(
                        Lot.user_id == sheet.user_id,
                        Lot.ticker == line.symbol,
                        Lot.quantity > 0,
                    )
                ).scalars().all()
            )
            accounts = {value.strip() for value in accounts if value.strip()}
            if len(accounts) != 1:
                raise ValueError(
                    f"{line.symbol}: sell custody must resolve to exactly one "
                    f"account; found {sorted(accounts)}"
                )
            account = accounts.pop()
        if not _recognized_account(account):
            raise ValueError(f"{line.symbol}: unrecognized sell custody account {account!r}")
        sell_accounts[line.symbol] = account

    funding = (funding_account_id or "").strip()
    has_buys = any(
        line.action in (OrderAction.BUY, OrderAction.ADD) for line in sheet.lines
    )
    if has_buys and not funding:
        unique_sell_accounts = set(sell_accounts.values())
        if sheet.funding.new_cash_usd <= 0.01 and len(unique_sell_accounts) == 1:
            funding = unique_sell_accounts.pop()
        else:
            raise ValueError(
                "exact buy funding account is required; it can be inferred only "
                "for a zero-new-cash sheet funded from one sell account"
            )
    if has_buys and not _recognized_account(funding):
        raise ValueError(f"unrecognized buy funding account {funding!r}")
    return funding, sell_accounts


def materialize_order_sheet(
    session: Session,
    sheet: OrderSheet,
    *,
    funding_account_id: str | None = None,
    sell_accounts_by_symbol: dict[str, str] | None = None,
    approved_by_unified_acceptance: bool = False,
) -> list[Proposal]:
    """Create one executable proposal for every action line.

    Materialization is idempotent for the exact sheet.  When called from the
    unified ActionProposal acceptance boundary, that one human decision also
    approves every exact line; asking for a second per-line approval would make
    the supposedly unified sheet optional again.  This never executes trades:
    broker preflight, execution and reconciliation remain separate boundaries.
    """

    require_valid_order_sheet(sheet)
    funding_account, sell_accounts = resolve_order_sheet_accounts(
        session,
        sheet,
        funding_account_id=funding_account_id,
        sell_accounts_by_symbol=sell_accounts_by_symbol,
    )
    fingerprint = order_sheet_fingerprint(sheet)
    existing = list(
        session.execute(
            select(Proposal)
            .where(Proposal.user_id == sheet.user_id)
            .where(Proposal.source == "order_sheet")
            .where(Proposal.expected_impact_json.like(f"%{fingerprint}%"))
            .order_by(Proposal.id)
        )
        .scalars()
        .all()
    )
    if existing:
        if len(existing) != len(sheet.lines):
            raise RuntimeError("partial order-sheet materialization detected")
        return existing

    current_plan_id = session.execute(
        select(PlanVersion.id)
        .where(PlanVersion.user_id == sheet.user_id, PlanVersion.role == "current")
        .order_by(desc(PlanVersion.id))
        .limit(1)
    ).scalar_one_or_none()
    run = DecisionRun(
        user_id=sheet.user_id,
        ticker="MULTI",
        tier="T2",
        decision_kind="portfolio_order_sheet",
        finished_at=sheet.generated_at,
        status="completed",
        fund_manager_decision="VALIDATED_ORDER_SHEET",
        notes_json=json.dumps(
            {
                "order_sheet_fingerprint": fingerprint,
                "funding": sheet.funding.model_dump(mode="json"),
            },
            sort_keys=True,
        ),
    )
    session.add(run)
    session.flush()

    rows: list[Proposal] = []
    expires_at = sheet.generated_at + timedelta(days=sheet.freshness_days)
    for line in sheet.lines:
        is_buy = line.action in (OrderAction.BUY, OrderAction.ADD)
        account_id = (
            funding_account if is_buy else sell_accounts.get(line.symbol, "")
        )
        if not _recognized_account(account_id):
            raise ValueError(
                f"{line.symbol}: exact custody account is required before materialization"
            )
        expected: dict[str, Any] = {
            "order_sheet_fingerprint": fingerprint,
            "order_line": line.model_dump(mode="json"),
            "funding": sheet.funding.model_dump(mode="json"),
        }
        initial_status = (
            ProposalStatus.APPROVED.value
            if approved_by_unified_acceptance
            else ProposalStatus.AWAITING_HUMAN.value
        )
        row = Proposal(
            user_id=sheet.user_id,
            ticker=line.symbol,
            action="buy" if is_buy else "sell",
            size_shares_or_currency=line.shares,
            size_units="shares",
            instrument=line.instrument_type,
            order_type="limit",
            limit_price=line.evidence.price_usd,
            time_in_force="DAY",
            tier="T2",
            account_class="main",
            account_id=account_id,
            status=initial_status,
            rationale_summary=f"{line.thesis} Falsifier: {line.falsifier}",
            expected_impact_json=json.dumps(expected, sort_keys=True),
            confidence="MEDIUM",
            decision_run_id=run.id,
            plan_version_id=current_plan_id,
            source="order_sheet",
            shadow=0,
            expires_at=expires_at,
        )
        session.add(row)
        session.flush()
        session.add(
            ProposalHistory(
                proposal_id=row.id,
                status=row.status,
                transitioned_by=(
                    "unified_order_sheet_acceptance"
                    if approved_by_unified_acceptance
                    else "order_sheet_materializer"
                ),
                note=(
                    f"validated sheet {fingerprint}; account={account_id}; "
                    f"venue={line.venue}; unified_approval="
                    f"{approved_by_unified_acceptance}"
                ),
            )
        )
        from argosy.services.predictions.writers import write_order_sheet_predictions

        write_order_sheet_predictions(
            session,
            sheet.user_id,
            fingerprint=fingerprint,
            proposal_id=row.id,
            ticker=line.symbol,
            action=line.action.value,
            event_at=sheet.generated_at,
            due_at=datetime.combine(
                line.expectation.due_date,
                time(23, 59, 59),
                tzinfo=UTC,
            ),
            entry_price=line.evidence.price_usd,
            expectation=line.expectation.statement,
            success_measure=line.expectation.success_measure,
            stance_source=line.stance_source,
        )
        rows.append(row)
    session.flush()
    run.proposal_id = rows[0].id if rows else None
    return rows


__all__ = [
    "materialize_order_sheet",
    "order_sheet_fingerprint",
    "resolve_order_sheet_accounts",
]
