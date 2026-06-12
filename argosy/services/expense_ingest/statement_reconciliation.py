"""Statement-merge reconciliation: dedup overlapping dumps + validate gaps.

Bank statements arrive as date-range dumps that can OVERLAP or leave a GAP:

* **Overlap** — two dumps covering 1-8 and 5-16 both contain the days 5-8 rows.
  The per-transaction content hash (``persistence._content_hash``) includes
  ``statement_id``, so it only dedups WITHIN a statement / re-ingests of the same
  period — it does NOT catch the same transaction arriving in two overlapping
  statements. Those double-count. This module removes the duplicates inside the
  overlap window (keeping the pre-existing statement's copy).

* **Gap** — two dumps covering 1-8 and 14-16 leave days 9-13 absent. A gap only
  loses money if a transaction happened in it, which is provable from the running
  **balance**: if the earlier dump CLOSED at the same balance the later dump
  OPENED at, no transaction was missed; otherwise money moved → a loud warning
  with the Δ. Gaps are checked on BOTH sides (the earlier-than-new and the
  later-than-new neighbour), so an out-of-order upload still catches its gap.

Currency-aware: a row carries either a NIS running balance (``balance`` +
``amount_nis``) or a USD one (``balance_usd`` + ``amount_orig``); the dedup key
and the balance math use the right pair so distinct USD rows aren't collapsed and
USD continuity isn't computed against a null NIS amount.

Run after transactions are persisted, before the ingest batch commits. The caller
owns the commit (the deletes + flush stay inside its transaction / a savepoint).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy import select

from argosy.state.models import ExpenseStatement, ExpenseTransaction

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Running-balance comparison slack. Balances print to the agora/cent, so a true
# movement is >= 0.01; only sub-cent float/parse noise should be absorbed.
_BALANCE_TOLERANCE = Decimal("0.01")


@dataclass
class ContinuityResult:
    earlier_statement_id: int
    later_statement_id: int
    has_gap: bool
    gap_days: int
    earlier_closing: Decimal | None
    later_opening: Decimal | None
    # True/False when both balances are known AND same-currency; None when they
    # couldn't be read or differ in currency (then continuity is unverifiable).
    balance_continuous: bool | None
    delta: Decimal | None
    currency: str | None
    warning: str | None


@dataclass
class ReconciliationReceipt:
    statement_id: int
    overlap_duplicates_removed: int
    overlapping_statement_ids: list[int]
    continuities: list[ContinuityResult]
    warnings: list[str] = field(default_factory=list)


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


def _balance_and_currency(txn: ExpenseTransaction) -> tuple[Decimal | None, str | None]:
    """The row's running balance + its currency, read from raw_row_json.
    NIS rows store ``balance``; USD rows (leumi_usd) store ``balance_usd``."""
    try:
        row = json.loads(txn.raw_row_json or "{}")
    except (ValueError, TypeError):
        return None, None
    nis = _to_decimal(row.get("balance"))
    if nis is not None:
        return nis, "NIS"
    usd = _to_decimal(row.get("balance_usd"))
    if usd is not None:
        return usd, "USD"
    return None, None


def _signed_amount(txn: ExpenseTransaction, currency: str | None) -> Decimal:
    """Signed effect of a txn on its running balance (credit +, debit -), taking
    the amount from the field that matches the balance currency: NIS→amount_nis,
    USD→amount_orig (amount_nis is None on USD rows)."""
    amt = txn.amount_nis if currency == "NIS" else txn.amount_orig
    if amt is None:  # fall back to whichever is populated
        amt = txn.amount_orig if txn.amount_nis is None else txn.amount_nis
    if amt is None:
        return Decimal(0)
    return amt if txn.direction == "credit" else -amt


def _ordered_txns(session: "Session", statement_id: int) -> list[ExpenseTransaction]:
    # ASC(occurred_on, id): [-1] is the chronologically last txn and [0] the
    # first, matching the codex-verified convention in
    # xls_osh_pair._get_osh_closing_balance_nis (DESC,DESC first == this [-1]).
    # Kept identical on purpose so the two never diverge.
    return list(
        session.execute(
            select(ExpenseTransaction)
            .where(ExpenseTransaction.statement_id == statement_id)
            .order_by(ExpenseTransaction.occurred_on, ExpenseTransaction.id)
        ).scalars()
    )


def _closing_balance(txns: list[ExpenseTransaction]) -> tuple[Decimal | None, str | None]:
    if not txns:
        return None, None
    return _balance_and_currency(txns[-1])


def _opening_balance(txns: list[ExpenseTransaction]) -> tuple[Decimal | None, str | None]:
    if not txns:
        return None, None
    first = txns[0]
    bal, cur = _balance_and_currency(first)
    if bal is None:
        return None, cur
    # Stored balance is AFTER the first txn; opening = before = bal - effect.
    return bal - _signed_amount(first, cur), cur


def _dedup_key(t: ExpenseTransaction) -> tuple:
    """Identity of a transaction for cross-statement overlap matching. Includes
    ``reference`` + ``amount_orig``/``currency_orig`` so distinct USD rows (same
    date/merchant/direction, ``amount_nis`` None) are NOT collapsed."""
    return (
        t.occurred_on, t.merchant_raw, t.direction, t.reference,
        t.amount_nis, t.amount_orig, t.currency_orig,
    )


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start <= b_end and b_start <= a_end


def _dedup_overlap(
    session: "Session", new_stmt: ExpenseStatement, others: list[ExpenseStatement],
) -> tuple[int, list[int]]:
    """Remove ``new_stmt`` transactions that duplicate a transaction already present
    in an overlapping statement, within the overlap window.

    Builds ONE combined key-multiset from all overlapping statements' in-window
    rows, then makes a SINGLE pass over the new transactions — so a new row that
    overlaps several existing statements is matched (and deleted) at most once
    (codex r2 B2: the prior per-other loop deleted the same new row twice)."""
    overlapping_ids: list[int] = []
    windows: list[tuple] = []  # per-overlap (window_start, window_end)
    pool: dict[tuple, int] = {}  # combined multiset of existing in-window keys
    for other in others:
        if not _overlaps(
            new_stmt.period_start, new_stmt.period_end,
            other.period_start, other.period_end,
        ):
            continue
        overlapping_ids.append(other.id)
        w_start = max(new_stmt.period_start, other.period_start)
        w_end = min(new_stmt.period_end, other.period_end)
        windows.append((w_start, w_end))
        for t in _ordered_txns(session, other.id):
            if w_start <= t.occurred_on <= w_end:
                k = _dedup_key(t)
                pool[k] = pool.get(k, 0) + 1

    removed = 0
    if pool:
        for t in _ordered_txns(session, new_stmt.id):  # single pass over NEW rows
            if not any(ws <= t.occurred_on <= we for ws, we in windows):
                continue  # outside every overlap window
            k = _dedup_key(t)
            if pool.get(k, 0) > 0:
                session.delete(t)
                pool[k] -= 1
                removed += 1
    if removed:
        session.flush()
    return removed, overlapping_ids


def _continuity_between(
    session: "Session", earlier: ExpenseStatement, later: ExpenseStatement,
) -> ContinuityResult:
    """Gap + balance continuity from ``earlier`` (closes) to ``later`` (opens)."""
    gap_days = (later.period_start - earlier.period_end).days
    has_gap = gap_days > 1  # D → D+1 is contiguous, not a gap
    # Uncovered span = the calendar days strictly between the two statements
    # (gap_days is boundary distance; missing-day count is one less).
    uncovered_days = max(0, gap_days - 1)
    first_uncovered = (earlier.period_end + timedelta(days=1)).isoformat()
    last_uncovered = (later.period_start - timedelta(days=1)).isoformat()
    uncovered = f"{uncovered_days} day(s) uncovered, {first_uncovered}..{last_uncovered}"

    earlier_closing, cur1 = _closing_balance(_ordered_txns(session, earlier.id))
    later_opening, cur2 = _opening_balance(_ordered_txns(session, later.id))

    currency = cur1 if cur1 == cur2 else None
    balance_continuous: bool | None = None
    delta: Decimal | None = None
    warning: str | None = None

    if earlier_closing is not None and later_opening is not None and currency is not None:
        delta = later_opening - earlier_closing
        balance_continuous = abs(delta) <= _BALANCE_TOLERANCE
        if has_gap and not balance_continuous:
            warning = (
                f"possible missing transactions ({uncovered}): statement closed at "
                f"{earlier_closing} {currency}, next opened at {later_opening} "
                f"{currency} (Δ {delta})."
            )
    elif has_gap:
        why = (
            "balances are in different currencies"
            if (cur1 and cur2 and cur1 != cur2)
            else "no running balance on the rows"
        )
        warning = (
            f"date gap ({uncovered}); balance continuity could not be verified ({why})."
        )

    return ContinuityResult(
        earlier_statement_id=earlier.id, later_statement_id=later.id,
        has_gap=has_gap, gap_days=gap_days,
        earlier_closing=earlier_closing, later_opening=later_opening,
        balance_continuous=balance_continuous, delta=delta,
        currency=currency, warning=warning,
    )


def _check_neighbours(
    session: "Session", new_stmt: ExpenseStatement, others: list[ExpenseStatement],
) -> list[ContinuityResult]:
    """Check the gap on BOTH sides of ``new_stmt`` — the closest statement that
    ENDS at/before it (earlier) and the closest that STARTS at/after it (later) —
    so an out-of-order upload still catches its gap."""
    results: list[ContinuityResult] = []
    earlier_candidates = [s for s in others if s.period_end <= new_stmt.period_start]
    if earlier_candidates:
        prior = max(earlier_candidates, key=lambda s: s.period_end)
        results.append(_continuity_between(session, prior, new_stmt))
    later_candidates = [s for s in others if s.period_start >= new_stmt.period_end]
    if later_candidates:
        nxt = min(later_candidates, key=lambda s: s.period_start)
        results.append(_continuity_between(session, new_stmt, nxt))
    return results


def reconcile_statement(
    session: "Session", *, user_id: str, source_id: int, statement_id: int,
) -> ReconciliationReceipt:
    """Reconcile a freshly-ingested statement against the others for the same
    ``(user_id, source_id)``: dedup overlapping duplicates + validate gaps on both
    sides. Never raises for data reasons; the caller wraps it best-effort."""
    new_stmt = session.get(ExpenseStatement, statement_id)
    if new_stmt is None:
        return ReconciliationReceipt(statement_id, 0, [], [], [])

    others = list(
        session.execute(
            select(ExpenseStatement).where(
                ExpenseStatement.user_id == user_id,
                ExpenseStatement.source_id == source_id,
                ExpenseStatement.id != statement_id,
            )
        ).scalars()
    )

    warnings: list[str] = []
    removed, overlapping_ids = _dedup_overlap(session, new_stmt, others)
    if removed:
        warnings.append(
            f"overlap dedup: removed {removed} duplicate transaction(s) already "
            f"present in statement(s) {overlapping_ids}."
        )

    continuities = _check_neighbours(session, new_stmt, others)
    warnings.extend(c.warning for c in continuities if c.warning)

    return ReconciliationReceipt(
        statement_id=statement_id,
        overlap_duplicates_removed=removed,
        overlapping_statement_ids=overlapping_ids,
        continuities=continuities,
        warnings=warnings,
    )


__all__ = [
    "ContinuityResult",
    "ReconciliationReceipt",
    "reconcile_statement",
]
