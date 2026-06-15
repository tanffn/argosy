"""Tests for the canonical real-estate payment ledger + its equity integration."""

from datetime import date

from argosy.services.real_estate_ledger import (
    LedgerEntry,
    compute_property_ledger,
)
from argosy.services.real_estate_equity import compute_real_estate_equity


def _pipera_entries() -> list[LedgerEntry]:
    return [
        LedgerEntry(date(2025, 7, 16), None, 14173.00, 0.0, "opening", "prior advances"),
        LedgerEntry(date(2026, 4, 7), "HSFV 4297", 596.88, 125.34, "advance", "Final Advance I"),
        LedgerEntry(date(2026, 4, 7), "HSFV 4298", 24311.31, 5105.37, "advance", "Partial Advance II"),
    ]


def test_pipera_ledger_paid_and_remaining():
    """The Pipera scenario: contract 113,219; opening 14,173 + two advances →
    paid 39,081.19, remaining 74,137.81, VAT 5,230.71 (sunk, not equity)."""
    lg = compute_property_ledger(
        property_key="Pipera", currency="EUR",
        total_price_local=113219.0, entries=_pipera_entries(),
    )
    assert lg.paid_net_local == 39081.19
    assert lg.vat_paid_local == 5230.71
    assert lg.remaining_local == 74137.81
    # equity built == paid net == price − remaining
    assert round(lg.total_price_local - lg.remaining_local, 2) == lg.paid_net_local


def test_vat_excluded_from_equity():
    """VAT is summed separately and never reduces the remaining (it's a sunk
    tax, not equity in the asset)."""
    lg = compute_property_ledger(
        property_key="X", currency="EUR", total_price_local=1000.0,
        entries=[LedgerEntry(None, "i1", 100.0, 21.0, "advance", "")],
    )
    assert lg.paid_net_local == 100.0
    assert lg.vat_paid_local == 21.0
    assert lg.remaining_local == 900.0  # 1000 - 100, VAT ignored


def test_remaining_none_without_price():
    lg = compute_property_ledger(
        property_key="X", currency="EUR", total_price_local=None,
        entries=[LedgerEntry(None, None, 50.0, 0.0, "advance", "")],
    )
    assert lg.remaining_local is None
    assert lg.paid_net_local == 50.0


def test_overpay_clamps_remaining_but_flags_overpaid():
    """A material over-payment clamps remaining at 0 BUT surfaces overpaid_local
    so the caller can warn (codex #2: don't mask a reconciliation error as a
    clean zero balance)."""
    lg = compute_property_ledger(
        property_key="X", currency="EUR", total_price_local=100.0,
        entries=[LedgerEntry(None, None, 120.0, 0.0, "advance", "")],
    )
    assert lg.remaining_local == 0.0
    assert lg.overpaid_local == 20.0


def test_rounding_epsilon_is_not_flagged_as_overpaid():
    """A sub-unit rounding overshoot is tolerated (not flagged as an error)."""
    lg = compute_property_ledger(
        property_key="X", currency="EUR", total_price_local=100.0,
        entries=[LedgerEntry(None, None, 100.5, 0.0, "advance", "")],
    )
    assert lg.remaining_local == 0.0
    assert lg.overpaid_local == 0.0


def test_entries_sorted_newest_first():
    lg = compute_property_ledger(
        property_key="X", currency="EUR", total_price_local=None,
        entries=_pipera_entries(),
    )
    dates = [e.payment_date for e in lg.entries]
    assert dates[0] == date(2026, 4, 7)
    assert dates[-1] == date(2025, 7, 16)


# --- integration with compute_real_estate_equity --------------------------


class _Row:
    def __init__(self, location, currency, role, value_local):
        self.location, self.currency, self.role, self.value_local = (
            location, currency, role, value_local)


def test_loan_override_supersedes_snapshot_loan():
    """When a property has a ledger, the computed remaining (loan_override)
    replaces the stale snapshot Loan row, and the net equity reflects it."""
    rows = [
        _Row("Pipera", "EUR", "Home", 113219.0),
        _Row("Pipera", "EUR", "Loan", 99046.0),   # stale snapshot value
    ]
    eq = compute_real_estate_equity(
        rows, fx_usd_nis=3.0, fx_usd_eur=0.85,
        loan_override={"Pipera": 74137.81},
    )
    p = eq.properties[0]
    assert p.loan_local == 74137.81            # ledger value, NOT 99046
    assert p.net_local == round(113219.0 - 74137.81, 2)  # 39081.19
    assert any("payment ledger" in w for w in p.warnings)


def test_no_override_uses_snapshot_loan():
    """Properties without a ledger keep the snapshot Loan (unchanged behavior)."""
    rows = [
        _Row("Obor", "EUR", "Home", 118020.0),
        _Row("Obor", "EUR", "Loan", 73314.0),
    ]
    eq = compute_real_estate_equity(rows, fx_usd_nis=3.0, fx_usd_eur=0.85)
    p = eq.properties[0]
    assert p.loan_local == 73314.0
    assert not any("payment ledger" in w for w in p.warnings)
