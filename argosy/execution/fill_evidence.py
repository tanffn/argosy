"""Shared read-side accounting for real, identity-matching execution receipts."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


def canonical_currency(value: str | None) -> str:
    unit = (value or "").strip().upper()
    return "NIS" if unit == "ILS" else unit


def custody_broker(account_id: str) -> str | None:
    value = (account_id or "").strip().lower()
    for prefix, broker in (("schwab", "schwab_csv"), ("leumi", "leumi_tsv"), ("ibkr", "ibkr")):
        if value.startswith(prefix):
            return broker
    return None


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except (ValueError, TypeError, InvalidOperation):
        pass
    raise ValueError("non-finite or invalid number")


def ledger_amount(value: Any, *, positive: bool = True) -> Decimal:
    """Representable NUMERIC(18,4), tolerating only binary floating noise."""
    try:
        amount = number(value)
        rounded = amount.quantize(Decimal("0.0001"))
        if (abs(rounded) < Decimal("100000000000000")
                and (not positive or rounded > 0)
                and abs(amount - rounded) <= Decimal("0.00000001")):
            return rounded
    except (ValueError, InvalidOperation):
        pass
    raise ValueError("amount exceeds positive ledger precision or range")


@dataclass
class FillEvidence:
    live_rows: list[Any] = field(default_factory=list)
    paper_count: int = 0
    errors: list[str] = field(default_factory=list)
    quantity: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)
    commission_confirmed: bool = True
    complete: bool = False

    @property
    def vwap(self) -> float | None:
        return float(self.notional / self.quantity) if self.quantity > 0 and self.price_currency else None

    def _common_currency(self, field_name: str) -> str | None:
        units = {canonical_currency(getattr(row, field_name, None)) for row in self.live_rows}
        return next(iter(units)) if len(units) == 1 and "" not in units else None

    @property
    def price_currency(self) -> str | None:
        return self._common_currency("price_currency")

    @property
    def commission_currency(self) -> str | None:
        return self._common_currency("commission_currency")


def reconcile_fill_evidence(proposal: Any, rows: list[Any], *, target_quantity=None) -> FillEvidence:
    """Paper/mismatched receipts cannot prove real execution or affect VWAP.

    Rejected evidence remains in storage and is named in the audit. No current
    approval/expiry requirement: a historical execution remains a fact.
    """
    result = FillEvidence()
    identities = set()
    broker = custody_broker(proposal.account_id)
    for row in rows:
        if row.paper:
            result.paper_count += 1
            continue
        errors = []
        for key, expected in (("user_id", proposal.user_id), ("proposal_id", proposal.id),
                              ("ticker", proposal.ticker), ("action", proposal.action),
                              ("account_id", proposal.account_id), ("broker", broker)):
            if expected is None or getattr(row, key) != expected:
                errors.append(f"{key} mismatch or missing custody evidence")
        identity = (row.broker, row.external_fill_id)
        if not (row.broker_order_id or "").strip() or not (row.external_fill_id or "").strip():
            errors.append("missing broker order/execution identity")
        elif row.external_fill_id.startswith("derived:"):
            errors.append("derived identity is not a broker execution receipt")
        elif identity in identities:
            errors.append("duplicate execution identity")
        identities.add(identity)
        try:
            quantity, price = ledger_amount(row.quantity), ledger_amount(row.price)
            commission = ledger_amount(row.commission, positive=False)
            if quantity <= 0 or price <= 0:
                errors.append("quantity and price must be positive")
        except ValueError:
            errors.append("invalid receipt amounts")
        if errors:
            result.errors.append(f"Fill #{row.id}: " + "; ".join(errors))
            continue
        result.live_rows.append(row)
        result.commission_confirmed = result.commission_confirmed and getattr(row, "commission_confirmed", True)
        result.quantity += quantity
        result.notional += quantity * price
        result.commission += commission
    try:
        target = ledger_amount(proposal.size_shares_or_currency if target_quantity is None else target_quantity)
        if target <= 0 or proposal.size_units != "shares":
            raise ValueError("invalid share target")
        if result.quantity > target:
            result.errors.append(f"Live fill quantity {result.quantity} exceeds authored quantity {target}.")
        result.complete = bool(result.live_rows) and not result.errors and result.quantity == target
    except ValueError:
        result.errors.append("Proposal has no valid share target.")
    return result
