"""Per-account Type DTO: the canonical type_label, reference-driven estate
safety, and the fail-loud guard for un-curated holdings (codex code review)."""
from __future__ import annotations

from argosy.api.routes.portfolio import _snapshot_to_dto
from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot


def _dto(positions):
    snap = PortfolioSnapshot(source_path="t.tsv", positions=positions)
    return _snapshot_to_dto(snap)


def test_curated_cashlike_etf_keeps_estate_flag_even_with_cash_source_type():
    # SGOV is US-domiciled (estate-EXPOSED) but its source Type can read cash-ish.
    # Estate must come from the reference, not be suppressed by the display Type.
    dto = _dto([
        PortfolioPosition(location="schwab 876", currency="USD",
                          asset_type="Cash", details="Treasuries",
                          symbol="SGOV", usd_value_k=10.0),
    ])
    pos = dto.positions[0]
    assert pos.estate_safe is False           # exposed — NOT suppressed to None
    assert pos.classified is True
    assert pos.type_label == "ETF · T-Bill"
    assert pos.name == "iShares 0-3 Month Treasury Bond ETF"  # description line
    assert dto.classification_warnings == []


def test_physical_cash_row_has_no_estate_marker_and_is_not_flagged():
    dto = _dto([
        PortfolioPosition(location="Leumi", currency="NIS",
                          asset_type="Cash", details="", symbol="",
                          usd_value_k=50.0),
    ])
    pos = dto.positions[0]
    assert pos.estate_safe is None            # physical cash → no marker
    assert pos.classified is True             # no real ticker → not flagged
    assert pos.name == "Leumi ILS cash"       # fallback label, not blank
    assert dto.classification_warnings == []


def test_symbol_less_rows_get_human_fallback_labels():
    # The four opaque book rows (blank/"-" Symbol) surface a human name in the
    # DTO instead of a blank — display only; value/estate flags untouched.
    dto = _dto([
        PortfolioPosition(location="Aborad", currency="USD",
                          asset_type="Real estate", details="Real estate",
                          symbol="-", usd_value_k=69.0),
        PortfolioPosition(location="Leumi", currency="NIS",
                          asset_type="Cash", details="", symbol="",
                          usd_value_k=17.66),
        PortfolioPosition(location="Leumi", currency="USD",
                          asset_type="Cash", details="", symbol="",
                          usd_value_k=5.45),
        PortfolioPosition(location="schwab 876", currency="USD",
                          asset_type="Cash", details="Cash", symbol="-",
                          usd_value_k=5.89),
    ])
    names = [p.name for p in dto.positions]
    assert names == [
        "Aborad property",
        "Leumi ILS cash",
        "Leumi USD cash",
        "Schwab 876 USD cash",
    ]
    assert dto.classification_warnings == []


def test_real_unknown_ticker_is_flagged_fail_loud():
    # A real-but-uncurated US-domiciled ETF must be caught, not silently
    # bucketed estate-safe via a name-keyword ("emerging") match.
    dto = _dto([
        PortfolioPosition(location="schwab 876", currency="USD",
                          asset_type="Growth", details="Vanguard FTSE Emerging Markets ETF",
                          symbol="VWO", usd_value_k=5.0),
    ])
    pos = dto.positions[0]
    assert pos.classified is False
    assert "VWO" in dto.classification_warnings
