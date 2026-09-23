"""Persist a validated order sheet into Argosy's executable proposal spine."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from argosy.decisions.proposals import ProposalStatus
from argosy.execution.fill_evidence import ledger_amount
from argosy.services.order_sheet import (
    FundingSummary,
    OrderAction,
    OrderLine,
    OrderSheet,
    require_valid_order_sheet,
)
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
    if not payload.get("pending_research"):
        payload.pop("pending_research", None)
    if payload.get("review_resolution") and not payload["review_resolution"].get("separation_reviewed"):
        payload["review_resolution"].pop("separation_reviewed", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _recognized_account(account_id: str) -> bool:
    value = account_id.strip().lower()
    return value.startswith(("leumi", "schwab", "ibkr"))


def _object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def linked_order_sheet_proposals(session: Session, sheet: OrderSheet) -> list[Proposal]:
    """Match structured identity, not incidental fingerprint text in prose.

    The originating run also anchors a row whose mutable payload was damaged.
    Keep such rows for validation, including an accidentally changed source.
    """
    fingerprint = order_sheet_fingerprint(sheet)
    candidates = session.execute(select(Proposal, DecisionRun).outerjoin(
        DecisionRun, DecisionRun.id == Proposal.decision_run_id,
    ).where(or_(Proposal.user_id == sheet.user_id, DecisionRun.user_id == sheet.user_id), or_(
        Proposal.expected_impact_json.like(f"%{fingerprint}%"),
        DecisionRun.notes_json.like(f"%{fingerprint}%"),
    )).order_by(Proposal.id)).all()
    return [row for row, run in candidates if (
        _object(row.expected_impact_json).get("order_sheet_fingerprint") == fingerprint
        or (run is not None and run.user_id == sheet.user_id
            and _object(run.notes_json).get("order_sheet_fingerprint") == fingerprint)
    )]


def linked_order_sheet_runs(session: Session, sheet: OrderSheet) -> list[DecisionRun]:
    """The original run survives even if all of its order rows go missing."""
    fingerprint = order_sheet_fingerprint(sheet)
    candidates = session.scalars(select(DecisionRun).where(
        DecisionRun.user_id == sheet.user_id,
        DecisionRun.notes_json.like(f"%{fingerprint}%"),
    )).all()
    return [run for run in candidates
            if _object(run.notes_json).get("order_sheet_fingerprint") == fingerprint]


def _same_number(left, right) -> bool:
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and abs(a - b) <= Decimal("0.00000001")
    except (ValueError, TypeError, InvalidOperation):
        return False


def _ledger_number(value) -> Decimal:
    """Positive NUMERIC(18,4), allowing only harmless binary float noise."""
    return ledger_amount(value)


def materialized_order_sheet_errors(
    session: Session, sheet: OrderSheet, rows: list[Proposal],
    *, expected_accounts: dict[str, str] | None = None,
) -> list[str]:
    """Check exact persisted execution fields against authored and custody evidence.

    This is identity/arithmetic integrity, not another investment reviewer.
    Legacy custody remains verifiable from the original materialization receipt;
    current lots are deliberately not used (fills may have changed inventory).
    """
    errors: list[str] = []
    fingerprint = order_sheet_fingerprint(sheet)
    lines = {line.symbol: line for line in sheet.lines}
    if len(rows) != len(lines):
        errors.append(f"Expected {len(lines)} order rows; found {len(rows)}.")
    seen = set()
    run_ids = {row.decision_run_id for row in rows}
    if rows and (None in run_ids or len(run_ids) != 1):
        errors.append("Order rows do not share one originating decision run.")
    if len({row.account_id for row in rows if row.ticker in lines
            and lines[row.ticker].action in (OrderAction.BUY, OrderAction.ADD)}) > 1:
        errors.append("Buy rows do not share the sheet's single funding account.")
    for row in rows:
        prefix = f"Proposal #{row.id} ({row.ticker})"
        line = lines.get(row.ticker)
        if line is None or row.ticker in seen:
            errors.append(f"{prefix}: unexpected or duplicate ticker.")
            continue
        seen.add(row.ticker)
        expected = _object(row.expected_impact_json)
        if expected.get("order_sheet_fingerprint") != fingerprint:
            errors.append(f"{prefix}: artifact fingerprint mismatch.")
        try:
            if OrderLine.model_validate(expected.get("order_line")) != line:
                errors.append(f"{prefix}: stored authored line differs from the sheet.")
            if FundingSummary.model_validate(expected.get("funding")) != sheet.funding:
                errors.append(f"{prefix}: stored funding differs from the sheet.")
        except ValueError:
            errors.append(f"{prefix}: missing or invalid authored execution payload.")
        fields = {
            "user_id": sheet.user_id, "source": "order_sheet", "size_units": "shares",
            "action": "buy" if line.action in (OrderAction.BUY, OrderAction.ADD) else "sell",
            "instrument": line.instrument_type, "order_type": "limit",
            "time_in_force": "DAY", "account_class": "main", "tier": "T2", "shadow": 0,
            "stop_price": None,
        }
        for field, value in fields.items():
            if getattr(row, field) != value:
                errors.append(f"{prefix}: {field} differs from the authored execution contract.")
        for field, value in (("size_shares_or_currency", line.shares), ("limit_price", line.evidence.price_usd)):
            try:
                _ledger_number(value)
                _ledger_number(getattr(row, field))
            except ValueError:
                errors.append(f"{prefix}: {field} exceeds positive ledger precision or range.")
            if not _same_number(getattr(row, field), value):
                errors.append(f"{prefix}: {field} differs from the authored amount.")
        expiry = row.expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        generated = sheet.generated_at
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=UTC)
        if expiry != generated + timedelta(days=sheet.freshness_days):
            errors.append(f"{prefix}: expiry differs from the authored deadline.")

        run = session.get(DecisionRun, row.decision_run_id) if row.decision_run_id else None
        notes = _object(run.notes_json) if run else {}
        if (run is None or run.user_id != sheet.user_id or run.decision_kind != "portfolio_order_sheet"
                or notes.get("order_sheet_fingerprint") != fingerprint):
            errors.append(f"{prefix}: originating run identity is missing or inconsistent.")
        receipts = session.scalars(select(ProposalHistory).where(
            ProposalHistory.proposal_id == row.id,
            ProposalHistory.transitioned_by.in_(("order_sheet_materializer", "unified_order_sheet_acceptance")),
        )).all()
        pattern = rf"validated sheet {re.escape(fingerprint)}; account=(.+); venue={re.escape(line.venue)}; unified_approval=(?:True|False)"
        accounts = {m.group(1) for receipt in receipts if (m := re.fullmatch(pattern, receipt.note or ""))}
        if len(accounts) != 1 or row.account_id not in accounts:
            errors.append(f"{prefix}: custody differs from the original materialization receipt or is unproven.")
        manifest = notes.get("execution_accounts")
        if manifest is not None and (not isinstance(manifest, dict) or manifest.get(line.symbol) != row.account_id):
            errors.append(f"{prefix}: custody differs from the run's execution-account manifest.")
        if expected_accounts is not None and expected_accounts.get(line.symbol) != row.account_id:
            errors.append(f"{prefix}: retry requests a different custody account.")
    if missing := set(lines) - seen:
        errors.append(f"Missing authored tickers: {', '.join(sorted(missing))}.")
    return errors


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
    existing = linked_order_sheet_proposals(session, sheet)
    existing_runs = linked_order_sheet_runs(session, sheet)
    if existing_runs and not existing:
        raise ValueError("Order-sheet materialization mismatch: originating run exists but order rows are missing")
    if not sheet.lines:
        if existing:
            raise ValueError("No-action sheet unexpectedly has materialized orders")
        return []  # A no-action review has no execution run to materialize.
    for line in sheet.lines:
        for value in (line.shares, line.evidence.price_usd):
            try:
                _ledger_number(value)
            except ValueError as exc:
                raise ValueError(f"{line.symbol}: {exc}") from exc
    fingerprint = order_sheet_fingerprint(sheet)
    if existing:
        # A replay cannot depend on inventory that a completed sale exhausted.
        # Unspecified routing stays bound to its original receipt/manifest;
        # explicit new routing is compared, never silently substituted.
        execution_accounts = {row.ticker: row.account_id for row in existing}
        supplied_sells = {str(k).upper(): str(v).strip() for k, v in (sell_accounts_by_symbol or {}).items()}
        for line in sheet.lines:
            if line.action in (OrderAction.BUY, OrderAction.ADD):
                if funding_account_id and funding_account_id.strip():
                    execution_accounts[line.symbol] = funding_account_id.strip()
            elif supplied_sells.get(line.symbol):
                execution_accounts[line.symbol] = supplied_sells[line.symbol]
        errors = materialized_order_sheet_errors(session, sheet, existing, expected_accounts=execution_accounts)
        if errors:
            raise ValueError("Order-sheet materialization mismatch: " + " ".join(errors))
        if approved_by_unified_acceptance:
            allowed = {"awaiting_human", "approved", "executed_live", "executed_paper"}
            if any(row.status not in allowed for row in existing):
                raise ValueError("Cannot re-approve cancelled, rejected or expired materialized orders")
            for row in existing:
                if row.status == ProposalStatus.AWAITING_HUMAN.value:
                    row.status = ProposalStatus.APPROVED.value
                    session.add(ProposalHistory(
                        proposal_id=row.id, status=row.status,
                        transitioned_by="unified_order_sheet_acceptance",
                        note=f"Unified approval of previously materialized sheet {fingerprint}",
                    ))
            session.flush()
        return existing

    funding_account, sell_accounts = resolve_order_sheet_accounts(
        session, sheet, funding_account_id=funding_account_id,
        sell_accounts_by_symbol=sell_accounts_by_symbol,
    )
    execution_accounts = {
        line.symbol: funding_account if line.action in (OrderAction.BUY, OrderAction.ADD) else sell_accounts[line.symbol]
        for line in sheet.lines
    }

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
                "execution_accounts": execution_accounts,
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
    "linked_order_sheet_proposals",
    "materialized_order_sheet_errors",
    "materialize_order_sheet",
    "order_sheet_fingerprint",
    "resolve_order_sheet_accounts",
]
