"""Apply broker-confirmed receipts atomically, never infer missing settlement."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from argosy.execution.fill_evidence import canonical_currency, number, reconcile_fill_evidence
from argosy.execution.settlement import FillSettlement
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    lock_book_writer,
    row_to_snapshot,
)
from argosy.services.snapshot_refresh import Fill as BookFill
from argosy.services.snapshot_refresh import apply_fills_to_snapshot
from argosy.state.models import (
    AuditLog,
    Fill,
    FillBookApplication,
    PendingOrder,
    PortfolioSnapshotRow,
    Proposal,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _receipt_hash(fill: Fill) -> str:
    values = {name: getattr(fill, name) for name in (
        "user_id", "proposal_id", "broker", "broker_order_id", "external_fill_id",
        "account_id", "ticker", "action", "paper", "execution_time_confirmed", "commission_confirmed", "native_account_id",
        "price_currency", "commission_currency")}
    values.update({name: str(number(getattr(fill, name)).normalize())
                   for name in ("quantity", "price", "commission")})
    values["filled_at"] = _utc(fill.filled_at).isoformat()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def apply_received_fill(
    session: Session, *, fill_id: int, user_id: str,
    settlement: FillSettlement | None = None,
    defer_application: bool = False,
) -> FillBookApplication:
    """Same transaction as receipt capture; no commit, network or broker action.

    Applied means shares/cash updated, NOT tax lots, statement verification or
    proof-quality performance. Historical/same-statement-day receipts remain
    explicit reconciliation work: they may already be included in the book.
    """
    lock_book_writer(session, user_id)
    fill = session.get(Fill, fill_id)
    if fill is None or fill.user_id != user_id:
        raise ValueError("fill ownership is unproven")
    digest = _receipt_hash(fill)
    application = session.get(FillBookApplication, fill_id)
    supplied = settlement.model_dump_json() if settlement is not None else None
    if application is not None:
        if application.user_id != user_id or application.receipt_hash != digest:
            raise ValueError("recorded receipt changed after book application assessment")
        if (supplied and application.settlement_json
                and FillSettlement.model_validate_json(application.settlement_json) != settlement):
            saved = FillSettlement.model_validate_json(application.settlement_json)
            if (application.applied_snapshot_id is not None or saved.listing_symbol is not None
                    or settlement.listing_symbol is None
                    or saved.model_dump(exclude={"listing_symbol"}) != settlement.model_dump(exclude={"listing_symbol"})):
                raise ValueError("settlement facts conflict with the saved broker receipt")
            session.add(AuditLog(user_id=user_id, event_type="fill.listing_confirmed", entity_type="fill",
                entity_id=str(fill.id), created_at=datetime.now(UTC), payload_json=json.dumps({
                    "before": saved.model_dump(mode="json"), "after": settlement.model_dump(mode="json")})))
        if application.applied_snapshot_id is not None:
            return application
    else:
        application = FillBookApplication(fill_id=fill.id, user_id=user_id, receipt_hash=digest,
                                           status="needs_settlement", reason="", updated_at=datetime.now(UTC))
        session.add(application)
    if supplied:
        application.settlement_json = supplied
    application.updated_at = datetime.now(UTC)
    if not application.settlement_json:
        application.status = "needs_settlement"
        application.reason = "Broker currency, withholding, net cash and settlement reference are required"
        session.flush()
        return application
    if defer_application:
        application.status, application.reason = "pending", "Awaiting complete broker response before application"
        session.flush()
        return application

    facts = FillSettlement.model_validate_json(application.settlement_json)
    try:
        # Roll back all snapshot/unmanaged/audit side effects if application
        # fails, but retain the real receipt and its explicit unresolved state.
        with session.begin_nested():
            proposal = session.get(Proposal, fill.proposal_id) if fill.proposal_id else None
            if proposal is None:
                raise ValueError("receipt has no verified proposal identity")
            receipts = list(session.scalars(select(Fill).where(Fill.proposal_id == proposal.id)))
            evidence = reconcile_fill_evidence(proposal, receipts)
            if fill.paper or evidence.errors or fill not in evidence.live_rows:
                raise ValueError("execution evidence needs reconciliation: " + "; ".join(evidence.errors))
            now = datetime.now(UTC)
            if not fill.execution_time_confirmed:
                raise ValueError("broker execution time is unknown; confirm it before book application")
            if not fill.commission_confirmed:
                raise ValueError("broker commission is unknown; confirm it before book application")
            currencies = [fill.price_currency, fill.commission_currency, facts.currency]
            if any(value is None or not value.strip() for value in currencies):
                raise ValueError("execution price and commission currencies require explicit confirmation")
            units = {canonical_currency(value) for value in currencies}
            if len(units) != 1:
                raise ValueError("execution price, commission and settlement currencies differ; conversion evidence required")
            if _utc(fill.filled_at) > now:
                raise ValueError("execution timestamp is in the future")
            account = fill.account_id.strip().lower()
            if not account:
                raise ValueError("receipt has no exact custody account")
            staging_error = session.scalar(select(PendingOrder.id).outerjoin(
                Proposal, (Proposal.id == PendingOrder.proposal_id) & (Proposal.user_id == user_id),
            ).where(
                PendingOrder.user_id == user_id,
                or_(func.lower(func.trim(PendingOrder.account_id)) == account,
                    func.lower(func.trim(Proposal.account_id)) == account),
                PendingOrder.receipt_sync_error.is_not(None),
            ).limit(1))
            if staging_error:
                raise ValueError("broker receipt staging for this account is incomplete; retry reconciliation first")
            real_rows = session.scalars(select(PortfolioSnapshotRow).where(
                PortfolioSnapshotRow.user_id == user_id,
            ).order_by(PortfolioSnapshotRow.id.desc()))
            baseline = None
            for row in real_rows:
                if (row.source_path or "").startswith(("fills-applied:", "self-refresh:")):
                    continue
                covered = json.loads(row.totals_json or "{}").get("accounts_covered", [])
                if account in {str(value).strip().lower() for value in covered}:
                    baseline = row
                    break
            if baseline is None or baseline.snapshot_date is None:
                raise ValueError("no dated broker baseline covering the exact custody account")
            # Date-only statements cannot establish intra-day event ordering.
            latest_possible_cutoff = datetime.combine(baseline.snapshot_date + timedelta(days=1), time(12), UTC)
            if _utc(fill.filled_at) <= latest_possible_cutoff:
                raise ValueError("receipt may already be included in the account statement; reconcile before applying")
            later = session.scalar(select(FillBookApplication.fill_id).join(
                Fill, Fill.id == FillBookApplication.fill_id).where(
                FillBookApplication.user_id == user_id,
                FillBookApplication.applied_snapshot_id.is_not(None),
                func.lower(func.trim(Fill.account_id)) == account,
                or_(Fill.filled_at > fill.filled_at, (Fill.filled_at == fill.filled_at) & (Fill.id > fill.id)),
            ).limit(1))
            if later:
                raise ValueError("receipt precedes already applied activity; ordered reconciliation is required")
            earlier = session.scalar(select(Fill.id).outerjoin(
                FillBookApplication, FillBookApplication.fill_id == Fill.id).where(
                Fill.user_id == user_id, func.lower(func.trim(Fill.account_id)) == account, Fill.paper.is_(False),
                Fill.id != fill.id,
                or_(Fill.execution_time_confirmed.is_(False), Fill.filled_at < fill.filled_at,
                    (Fill.filled_at == fill.filled_at) & (Fill.id < fill.id)),
                FillBookApplication.applied_snapshot_id.is_(None),
            ).limit(1))
            if earlier:
                raise ValueError("earlier receipt in this account remains unapplied; reconcile in execution order")
            prior = get_latest_snapshot_row(session, user_id)
            if prior is None:
                raise ValueError("no current portfolio snapshot")
            from argosy.adapters.data.symbols import to_yahoo_symbol
            from argosy.services.snapshot_refresh import (
                _EXCHANGE_HINT_SUFFIX,
                matching_fill_positions,
                resolved_quote_symbols,
            )

            held = matching_fill_positions(row_to_snapshot(prior).positions, symbol=fill.ticker,
                                           location=fill.account_id, currency=facts.currency)
            details = ""
            if held:
                for position in held:
                    listings = resolved_quote_symbols(position.symbol, currency=position.currency, details=position.details)
                    if not listings:
                        raise ValueError("existing holding listing is unresolved or contradictory")
                    if facts.listing_symbol and facts.listing_symbol.strip().upper() not in {s.upper() for s in listings}:
                        raise ValueError("receipt listing conflicts with the held instrument")
            if not held:
                if not facts.listing_symbol:
                    raise ValueError("new holding requires a verified listing symbol from the broker evidence")
                listing = facts.listing_symbol.strip().upper()
                bare = to_yahoo_symbol(fill.ticker)
                if listing == bare and facts.currency == "USD":
                    details = f"Verified US listing: {bare}"
                else:
                    venues = [venue for venue, suffix in _EXCHANGE_HINT_SUFFIX.items() if listing == bare + suffix]
                    if not venues:
                        raise ValueError("verified listing cannot be mapped to the existing quote resolver")
                    details = f"Verified listing: {fill.ticker} {venues[0]}"
            result = apply_fills_to_snapshot(
                session, user_id=user_id, fills=[BookFill(
                    symbol=fill.ticker, action=fill.action, shares=float(fill.quantity),
                    price=float(fill.price), commission=float(fill.commission),
                    tax_withheld=float(facts.tax_withheld), net_cash_delta=float(facts.net_cash_delta),
                    currency=facts.currency, location=fill.account_id,
                    details=details, observed_as_of=_utc(fill.filled_at).date(),
                )], cash_location=fill.account_id, cash_currency=facts.currency,
                source_tag=f"fills-applied:receipt:{fill.id}",
                extra_warnings=[f"broker-receipt:{fill.id}:{fill.external_fill_id}"],
                today=max(prior.snapshot_date or _utc(fill.filled_at).date(), _utc(fill.filled_at).date()),
                commit=False,
            )
            application.base_snapshot_id = prior.id
            application.applied_snapshot_id = result.row.id
            application.status = "applied"
            application.reason = "Shares/cash updated; tax lots and broker-statement reconciliation remain unverified"
            session.add(AuditLog(user_id=user_id, event_type="fill.book_applied",
                                 entity_type="fill", entity_id=str(fill.id), payload_json=json.dumps({
                                     "base_snapshot_id": prior.id, "applied_snapshot_id": result.row.id,
                                     "settlement": facts.model_dump(mode="json"), "receipt_hash": digest,
                                 }), created_at=now))
            session.flush()
    except ValueError as exc:
        application.status = "needs_reconciliation"
        application.reason = str(exc)
    except Exception as exc:
        if not session.is_active or (isinstance(exc, DBAPIError) and exc.connection_invalidated):
            raise
        application.status = "application_error"
        application.reason = f"{type(exc).__name__}: {exc}"
        session.add(AuditLog(user_id=user_id, event_type="fill.book_application_failed", entity_type="fill",
                             entity_id=str(fill.id), created_at=datetime.now(UTC),
                             payload_json=json.dumps({"error": application.reason})))
    session.flush()
    return application


def confirm_execution_time(session: Session, fill: Fill, executed_at: datetime) -> None:
    """Enrich an explicitly unknown timestamp, with an immutable before/after audit."""
    if executed_at.tzinfo is None or executed_at.utcoffset() is None:
        raise ValueError("confirmed execution timestamp requires an explicit timezone")
    executed_at = executed_at.astimezone(UTC)
    lock_book_writer(session, fill.user_id)
    application = session.get(FillBookApplication, fill.id)
    if fill.execution_time_confirmed:
        if _utc(fill.filled_at) != _utc(executed_at):
            raise ValueError("confirmed execution time conflicts with saved receipt")
        return
    if application and (application.applied_snapshot_id is not None or application.receipt_hash != _receipt_hash(fill)):
        raise ValueError("receipt cannot be re-timed after application or unexplained mutation")
    before = fill.filled_at
    fill.filled_at, fill.execution_time_confirmed = executed_at, True
    if application:
        application.receipt_hash = _receipt_hash(fill)
    session.add(AuditLog(user_id=fill.user_id, event_type="fill.execution_time_confirmed",
                         entity_type="fill", entity_id=str(fill.id), created_at=datetime.now(UTC),
                         payload_json=json.dumps({"previous_placeholder": str(before), "executed_at": str(executed_at)})))
    session.flush()


def confirm_broker_metadata(session: Session, fill: Fill, *, commission=None, native_account_id=None,
                           price_currency=None, commission_currency=None) -> None:
    """Audited one-way enrichment, never overwrite a known fee or custody fact."""
    changes = {}
    if commission is not None:
        if fill.commission_confirmed and number(fill.commission) != number(commission):
            raise ValueError("Broker execution identity already exists with different receipt facts (commission)")
        if not fill.commission_confirmed:
            changes.update(commission=commission, commission_confirmed=True)
    if native_account_id is not None:
        if fill.native_account_id is not None and fill.native_account_id != native_account_id:
            raise ValueError("Broker execution identity already exists with different receipt facts (native custody)")
        if fill.native_account_id is None:
            changes["native_account_id"] = native_account_id
    for key, supplied in (("price_currency", price_currency), ("commission_currency", commission_currency)):
        if supplied is not None:
            supplied = canonical_currency(supplied)
            if not supplied:
                raise ValueError("empty receipt currency")
            prior = getattr(fill, key)
            if canonical_currency(prior) and canonical_currency(prior) != supplied:
                raise ValueError("Broker execution identity already exists with different receipt currency")
            if not canonical_currency(prior):
                changes[key] = supplied
    if not changes:
        return
    lock_book_writer(session, fill.user_id)
    application = session.get(FillBookApplication, fill.id)
    if application and (application.applied_snapshot_id is not None or application.receipt_hash != _receipt_hash(fill)):
        raise ValueError("cannot enrich metadata after application or unexplained receipt mutation")
    before = {key: getattr(fill, key) for key in changes}
    for key, value in changes.items():
        setattr(fill, key, value)
    if application:
        application.receipt_hash = _receipt_hash(fill)
    session.add(AuditLog(user_id=fill.user_id, event_type="fill.broker_metadata_confirmed", entity_type="fill",
        entity_id=str(fill.id), created_at=datetime.now(UTC),
        payload_json=json.dumps({"before": before, "after": changes}, default=str)))
    session.flush()


def recover_fill_applications(session: Session, *, user_id: str, limit: int = 200, commit: bool = True) -> dict:
    """Bounded scheduled catch-up using saved facts, never manufacturing settlement."""
    candidates = list(session.scalars(select(Fill).outerjoin(
        FillBookApplication, FillBookApplication.fill_id == Fill.id).where(
        Fill.user_id == user_id, Fill.paper.is_(False),
        FillBookApplication.applied_snapshot_id.is_(None),
        or_(FillBookApplication.fill_id.is_(None), FillBookApplication.settlement_json.is_not(None)),
    ).order_by(FillBookApplication.updated_at.asc().nullsfirst(), Fill.filled_at, Fill.id).limit(limit)))
    candidates.sort(key=lambda receipt: (_utc(receipt.filled_at), receipt.id))
    counts = {"examined": len(candidates), "applied": 0, "needs_settlement": 0,
              "needs_reconciliation": 0, "application_error": 0, "errors": 0}
    for fill in candidates:
        fill_id = fill.id
        try:
            with session.begin_nested():
                result = apply_received_fill(session, fill_id=fill_id, user_id=user_id)
            counts[result.status] += 1
            if result.status == "application_error":
                counts["errors"] += 1
        except Exception as exc:
            # One damaged receipt cannot roll back another account's successful
            # application or prevent repricing; retain a durable error/attempt.
            failure = session.get(FillBookApplication, fill_id)
            if failure is None:
                failure = FillBookApplication(fill_id=fill_id, user_id=user_id, receipt_hash="unverified")
                session.add(failure)
            failure.status, failure.reason = "needs_reconciliation", f"{type(exc).__name__}: {exc}"
            failure.updated_at = datetime.now(UTC)
            session.add(AuditLog(user_id=user_id, event_type="fill.book_application_failed", entity_type="fill",
                                 entity_id=str(fill_id), payload_json=json.dumps({"error": failure.reason}),
                                 created_at=failure.updated_at))
            session.flush()
            counts["errors"] += 1
    if commit:
        session.commit()
    return counts
