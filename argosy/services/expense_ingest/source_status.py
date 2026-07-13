"""Per-statement reconciliation Status for /expenses/sources.

Status is NOT only parsed−declared (that left Leumi / Cal rolling / Discount
as ``unknown`` forever). Kind-aware:

* **card** — charge-date bucket(s) ↔ bank card-payment debit; fall back to
  declared-total gap when buckets cannot be formed; else ``n/a``.
* **bank** — overlap identity-balance continuity or adjacent opening/closing;
  else ``n/a``.

Values: ``green | yellow | red | n/a`` (``unknown`` retired).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy import select

from argosy.state.models import ExpenseSource, ExpenseStatement, ExpenseTransaction

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

AMOUNT_MATCH_NIS = Decimal("0.50")
DATE_WINDOW_DAYS = 2
# Bank charge larger than card bucket by more than this → missing card lines.
MISSING_LINES_NIS = Decimal("5.00")
BALANCE_TOL = Decimal("0.01")

_CARD_PAYMENT_SMELL = (
    "ל.מאסטרקרד", "כרטיסי אשראי", "ויזה", "דיינרס",
    "אמריקן אקספרס", "ישראכרט", "מאסטרקרד", "מקס",
)


def declared_gap_status(gap: float | None) -> str:
    """Classic footer reconciliation (kept for Declared column + fallback)."""
    if gap is None:
        return "n/a"
    a = abs(gap)
    if a < 0.5:
        return "green"
    if a < 5.0:
        return "yellow"
    return "red"


def _to_decimal(raw) -> Decimal | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("₪", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _smells_like_card_payment(merchant: str | None) -> bool:
    if not merchant:
        return False
    return any(k in merchant for k in _CARD_PAYMENT_SMELL)


def _signed_nis(tx: ExpenseTransaction) -> Decimal:
    if tx.amount_nis is None:
        return Decimal("0")
    return tx.amount_nis if tx.direction == "debit" else -tx.amount_nis


def _charge_buckets(statement: ExpenseStatement, txs: list[ExpenseTransaction]) -> dict[date, Decimal]:
    """Sum signed NIS spend per charge date (מועד חיוב / posted_on / stmt.charge_date)."""
    buckets: dict[date, Decimal] = {}
    any_posted = False
    for tx in txs:
        if tx.posted_on is not None:
            any_posted = True
            buckets[tx.posted_on] = buckets.get(tx.posted_on, Decimal("0")) + _signed_nis(tx)
    if any_posted:
        return buckets
    if statement.charge_date is not None:
        total = sum((_signed_nis(tx) for tx in txs), Decimal("0"))
        return {statement.charge_date: total}
    return {}


def _bank_card_candidates(session: "Session", user_id: str) -> list[ExpenseTransaction]:
    rows = (
        session.execute(
            select(ExpenseTransaction)
            .join(ExpenseSource, ExpenseSource.id == ExpenseTransaction.source_id)
            .where(
                ExpenseTransaction.user_id == user_id,
                ExpenseSource.kind == "bank",
                ExpenseTransaction.direction == "debit",
                ExpenseTransaction.amount_nis.isnot(None),
            )
        )
        .scalars()
        .all()
    )
    out: list[ExpenseTransaction] = []
    for tx in rows:
        if tx.is_card_payment or _smells_like_card_payment(tx.merchant_raw):
            out.append(tx)
    return out


def _bucket_vs_bank(
    bucket_date: date,
    bucket_sum: Decimal,
    *,
    statement_id: int,
    external_id: str,
    bank_txs: list[ExpenseTransaction],
    today: date,
) -> str:
    """Return match | bank_higher | card_higher | no_bank | future."""
    if bucket_date > today:
        return "future"
    target = abs(bucket_sum)
    best: ExpenseTransaction | None = None
    best_delta: Decimal | None = None
    window: list[ExpenseTransaction] = []
    for b in bank_txs:
        # Already-linked correlator rows win even outside the date window.
        if b.matched_statement_id == statement_id:
            delta = abs((b.amount_nis or Decimal("0")) - target)
            if best_delta is None or delta < best_delta:
                best, best_delta = b, delta
            continue
        if abs((b.occurred_on - bucket_date).days) > DATE_WINDOW_DAYS:
            continue
        window.append(b)
        delta = abs((b.amount_nis or Decimal("0")) - target)
        # Isracard last-4 on the bank row → always pair (then classify
        # match / bank_higher / card_higher). Cal/Max use internal refs
        # (8547/…) so they pair on amount ±0.50; sole window candidate
        # still pairs so missing card lines can surface as red.
        if b.reference == external_id or delta <= AMOUNT_MATCH_NIS:
            if best_delta is None or delta < best_delta:
                best, best_delta = b, delta
    if best is None and len(window) == 1:
        best = window[0]
        best_delta = abs((best.amount_nis or Decimal("0")) - target)
    if best is None or best_delta is None:
        return "no_bank"
    bank_amt = best.amount_nis or Decimal("0")
    diff = bank_amt - target
    if abs(diff) <= AMOUNT_MATCH_NIS:
        return "match"
    if diff > MISSING_LINES_NIS:
        return "bank_higher"
    if diff < -MISSING_LINES_NIS:
        return "card_higher"
    return "match"


def card_statement_status(
    session: "Session",
    *,
    user_id: str,
    source: ExpenseSource,
    statement: ExpenseStatement,
    txs: list[ExpenseTransaction] | None = None,
    today: date | None = None,
) -> str:
    today = today or date.today()
    if txs is None:
        txs = list(
            session.execute(
                select(ExpenseTransaction).where(
                    ExpenseTransaction.statement_id == statement.id,
                )
            ).scalars()
        )

    # Correlator already linked a bank lump-sum to this statement.
    linked = session.execute(
        select(ExpenseTransaction.id).where(
            ExpenseTransaction.user_id == user_id,
            ExpenseTransaction.matched_statement_id == statement.id,
        ).limit(1)
    ).first()
    if linked is not None:
        return "green"

    buckets = _charge_buckets(statement, txs)
    if buckets:
        bank_txs = _bank_card_candidates(session, user_id)
        outcomes = [
            _bucket_vs_bank(
                d, s,
                statement_id=statement.id,
                external_id=source.external_id or "",
                bank_txs=bank_txs,
                today=today,
            )
            for d, s in buckets.items()
        ]
        actionable = [o for o in outcomes if o != "future"]
        if actionable:
            if any(o == "bank_higher" for o in actionable):
                return "red"
            if any(o == "card_higher" for o in actionable):
                return "yellow"
            if all(o == "match" for o in actionable):
                return "green"
            if any(o == "match" for o in actionable) and any(o == "no_bank" for o in actionable):
                return "yellow"
            # all no_bank → fall through to declared

    if statement.declared_total_nis is not None:
        gap = float(statement.parsed_total_nis or 0) - float(statement.declared_total_nis)
        return declared_gap_status(gap)
    return "n/a"


def _balance_from_tx(tx: ExpenseTransaction) -> tuple[Decimal | None, str | None]:
    try:
        row = json.loads(tx.raw_row_json or "{}")
    except (ValueError, TypeError):
        return None, None
    nis = _to_decimal(row.get("balance"))
    if nis is not None:
        return nis, "NIS"
    usd = _to_decimal(row.get("balance_usd"))
    if usd is not None:
        return usd, "USD"
    return None, None


def _identity_key(tx: ExpenseTransaction) -> tuple:
    return (
        tx.occurred_on,
        tx.merchant_raw,
        tx.direction,
        tx.reference,
        tx.amount_nis,
        tx.amount_orig,
        tx.currency_orig,
    )


def _ordered_txs(session: "Session", statement_id: int) -> list[ExpenseTransaction]:
    return list(
        session.execute(
            select(ExpenseTransaction)
            .where(ExpenseTransaction.statement_id == statement_id)
            .order_by(ExpenseTransaction.occurred_on, ExpenseTransaction.id)
        ).scalars()
    )


def _overlap_identity_status(
    earlier_txs: list[ExpenseTransaction],
    later_txs: list[ExpenseTransaction],
) -> str:
    """Compare running balances on rows present in both dumps."""
    by_key: dict[tuple, ExpenseTransaction] = {_identity_key(t): t for t in earlier_txs}
    compared = 0
    for t in later_txs:
        k = _identity_key(t)
        other = by_key.get(k)
        if other is None:
            continue
        b1, c1 = _balance_from_tx(other)
        b2, c2 = _balance_from_tx(t)
        if b1 is None or b2 is None or c1 != c2:
            continue
        compared += 1
        if abs(b1 - b2) > BALANCE_TOL:
            return "red"
    if compared == 0:
        return "n/a"
    return "green"


def _adjacent_continuity_status(
    earlier_txs: list[ExpenseTransaction],
    later_txs: list[ExpenseTransaction],
) -> str:
    from argosy.services.expense_ingest.statement_reconciliation import (
        _closing_balance,
        _opening_balance,
    )

    close, c1 = _closing_balance(earlier_txs)
    open_, c2 = _opening_balance(later_txs)
    if close is None or open_ is None or c1 != c2 or c1 is None:
        return "n/a"
    if abs(open_ - close) <= BALANCE_TOL:
        return "green"
    return "red"


def bank_statement_status(
    session: "Session",
    *,
    user_id: str,
    source: ExpenseSource,
    statement: ExpenseStatement,
) -> str:
    others = list(
        session.execute(
            select(ExpenseStatement).where(
                ExpenseStatement.user_id == user_id,
                ExpenseStatement.source_id == source.id,
                ExpenseStatement.id != statement.id,
            )
        ).scalars()
    )
    if not others:
        return "n/a"

    mine = _ordered_txs(session, statement.id)
    if not mine:
        return "n/a"

    # Prefer an overlapping neighbour (identity check); else nearest earlier.
    overlapping = [
        o for o in others
        if o.period_start <= statement.period_end and statement.period_start <= o.period_end
    ]
    if overlapping:
        # Pick the overlap with the most calendar overlap.
        def _overlap_days(o: ExpenseStatement) -> int:
            a = max(o.period_start, statement.period_start)
            b = min(o.period_end, statement.period_end)
            return max(0, (b - a).days)

        neighbour = max(overlapping, key=_overlap_days)
        return _overlap_identity_status(mine, _ordered_txs(session, neighbour.id))

    earlier = [o for o in others if o.period_end < statement.period_start]
    if not earlier:
        later = [o for o in others if o.period_start > statement.period_end]
        if not later:
            return "n/a"
        neighbour = min(later, key=lambda s: s.period_start)
        return _adjacent_continuity_status(mine, _ordered_txs(session, neighbour.id))

    neighbour = max(earlier, key=lambda s: s.period_end)
    return _adjacent_continuity_status(_ordered_txs(session, neighbour.id), mine)


def statement_status(
    session: "Session",
    *,
    user_id: str,
    source: ExpenseSource,
    statement: ExpenseStatement,
    txs: list[ExpenseTransaction] | None = None,
    today: date | None = None,
) -> str:
    if source.kind == "card":
        return card_statement_status(
            session, user_id=user_id, source=source, statement=statement,
            txs=txs, today=today,
        )
    if source.kind == "bank":
        return bank_statement_status(
            session, user_id=user_id, source=source, statement=statement,
        )
    # Fallback for any future kinds
    if statement.declared_total_nis is not None:
        gap = float(statement.parsed_total_nis or 0) - float(statement.declared_total_nis)
        return declared_gap_status(gap)
    return "n/a"


__all__ = [
    "declared_gap_status",
    "card_statement_status",
    "bank_statement_status",
    "statement_status",
]
