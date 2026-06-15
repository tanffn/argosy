"""Canonical per-property payment ledger → computed remaining-to-pay.

The portfolio snapshot's per-property "Loan" row is a static, re-import-clobbered
figure. This module makes the PAYMENTS the source of truth: given a property's
contract price (the snapshot "Home") and its ledger of payments, the remaining
balance is COMPUTED as ``price − Σ(net payments)`` — so it survives TSV
re-imports and traces to the source invoices (auto-memory
``feedback_reconcile_against_raw_source`` + ``feedback_plan_ui_one_canonical_source``).

Two layers, kept apart so the math is unit-testable without a DB:

1. :func:`compute_property_ledger` — PURE. price + entries → paid / vat / remaining.
   Equity-building uses the NET (ex-VAT) amounts; VAT is summed separately as a
   sunk cost, never counted as equity.
2. :func:`load_property_ledgers` — reads ``real_estate_payments`` and pairs each
   property with its contract price to produce a ledger per property.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class LedgerEntry:
    payment_date: date | None
    invoice_no: str | None
    amount_net_local: float
    vat_local: float
    kind: str
    description: str


@dataclass(frozen=True)
class PropertyLedger:
    property_key: str
    currency: str
    total_price_local: float | None   # contract price (the snapshot "Home")
    paid_net_local: float             # Σ net payments (this is the equity built)
    vat_paid_local: float             # Σ VAT (sunk cost, not equity)
    remaining_local: float | None     # max(0, price − paid_net) (None if no price)
    entries: tuple[LedgerEntry, ...] = field(default=())
    # > 0 when net payments EXCEED the contract price beyond a rounding epsilon —
    # a reconciliation failure (duplicate/opening double-count, gross-vs-net
    # mix) the caller must surface rather than silently show remaining=0.
    overpaid_local: float = 0.0

    @property
    def has_entries(self) -> bool:
        return bool(self.entries)


def compute_property_ledger(
    *,
    property_key: str,
    currency: str,
    total_price_local: float | None,
    entries: list[LedgerEntry],
) -> PropertyLedger:
    """Pure: roll a property's payment entries into paid / vat / remaining.

    ``paid_net_local`` is the sum of the ex-VAT amounts (the equity built in the
    asset); ``vat_paid_local`` is the VAT (a sunk tax cost). ``remaining_local``
    is ``total_price_local − paid_net_local`` (clamped at 0 so an over-paid /
    rounding overshoot never shows a negative balance), or None when no contract
    price is known.
    """
    paid = round(sum(e.amount_net_local for e in entries), 2)
    vat = round(sum(e.vat_local for e in entries), 2)
    remaining: float | None = None
    overpaid = 0.0
    if total_price_local is not None:
        raw_remaining = round(total_price_local - paid, 2)
        remaining = max(0.0, raw_remaining)
        # Beyond a 1-unit rounding epsilon, an over-payment is a real ledger
        # error (double-count / basis mismatch), not a paid-off property — flag
        # it loudly instead of masking it as a clean zero balance.
        if raw_remaining < -1.0:
            overpaid = round(-raw_remaining, 2)
    # Newest first for display.
    ordered = tuple(sorted(
        entries,
        key=lambda e: (e.payment_date is None, e.payment_date or date.min),
        reverse=True,
    ))
    return PropertyLedger(
        property_key=property_key,
        currency=currency,
        total_price_local=total_price_local,
        paid_net_local=paid,
        vat_paid_local=vat,
        remaining_local=remaining,
        entries=ordered,
        overpaid_local=overpaid,
    )


def load_property_ledgers(
    session,
    *,
    user_id: str,
    total_price_by_property: dict[str, float],
    currency_by_property: dict[str, str] | None = None,
) -> dict[str, PropertyLedger]:
    """Load payments from ``real_estate_payments`` and build a ledger per
    property that HAS payments. Properties with no rows are absent from the
    result (the caller falls back to the snapshot Loan row).

    ``total_price_by_property`` maps property_key → contract price (the snapshot
    Home); a property with payments but no known price gets ``remaining=None``.
    """
    from sqlalchemy import select

    from argosy.state.models import RealEstatePayment

    currency_by_property = currency_by_property or {}
    rows = session.execute(
        select(RealEstatePayment).where(RealEstatePayment.user_id == user_id)
    ).scalars().all()

    by_key: dict[str, list[LedgerEntry]] = {}
    ccy_seen: dict[str, str] = {}
    for r in rows:
        by_key.setdefault(r.property_key, []).append(LedgerEntry(
            payment_date=r.payment_date,
            invoice_no=r.invoice_no,
            amount_net_local=float(r.amount_net_local or 0.0),
            vat_local=float(r.vat_local or 0.0),
            kind=r.kind,
            description=r.description or "",
        ))
        ccy_seen.setdefault(r.property_key, r.currency or "EUR")

    out: dict[str, PropertyLedger] = {}
    for key, entries in by_key.items():
        out[key] = compute_property_ledger(
            property_key=key,
            currency=currency_by_property.get(key) or ccy_seen.get(key, "EUR"),
            total_price_local=total_price_by_property.get(key),
            entries=entries,
        )
    return out


@dataclass(frozen=True)
class PropertyOverride:
    """A durable per-property correction the TSV snapshot can't express — an
    impairment / write-off (e.g. a developer-bankruptcy property worth $0 whose
    mortgage was never drawn) plus an optional contingent recovery. Stored in the
    profile's ``real_estate_overrides`` block (NOT the snapshot), so it survives
    TSV re-imports.

    ``current_value_local`` overrides the snapshot Home; ``loan_local`` overrides
    the Loan. ``recovery_expected_local`` is a CONTINGENT future inflow that is
    deliberately NOT added to net worth (low-confidence; booked only when
    realized) — it rides along as a note.
    """

    property_key: str
    status: str                              # active | bust | sold | impaired
    current_value_local: float | None
    loan_local: float | None
    recovery_expected_local: float | None
    recovery_confidence: str
    note: str


def load_real_estate_overrides(session, *, user_id: str) -> dict[str, PropertyOverride]:
    """Read the profile's ``real_estate_overrides`` block (identity_yaml) into a
    map keyed by property name. Empty when none are set."""
    from sqlalchemy import select

    from argosy.state.models import UserContext

    ctx = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    raw = getattr(ctx, "identity_yaml", None) if ctx else None
    if not raw:
        return {}
    try:
        import yaml as _yaml
        data = _yaml.safe_load(raw) or {}
    except Exception:  # noqa: BLE001
        return {}
    block = data.get("real_estate_overrides") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return {}

    def _f(v: object) -> float | None:
        try:
            return None if v is None or isinstance(v, bool) else float(v)
        except (TypeError, ValueError):
            return None

    out: dict[str, PropertyOverride] = {}
    for key, ovr in block.items():
        if not isinstance(ovr, dict):
            continue
        out[str(key)] = PropertyOverride(
            property_key=str(key),
            status=str(ovr.get("status") or "impaired"),
            current_value_local=_f(ovr.get("current_value_local")),
            loan_local=_f(ovr.get("loan_local")),
            recovery_expected_local=_f(ovr.get("recovery_expected_local")),
            recovery_confidence=str(ovr.get("recovery_confidence") or "LOW"),
            note=str(ovr.get("note") or ""),
        )
    return out


__all__ = [
    "LedgerEntry",
    "PropertyLedger",
    "PropertyOverride",
    "compute_property_ledger",
    "load_property_ledgers",
    "load_real_estate_overrides",
]
