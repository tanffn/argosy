"""Unit tests for the economic-NVDA look-through pure computation."""

from argosy.services.nvda_lookthrough import (
    InstrumentWeight,
    compute_economic_nvda,
    _plan_instruments,
    _nvda_pct_from_holdings,
)
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)


def _fake_doc() -> TargetAllocationDoc:
    def _cls(label, sigma, pct, sym):
        return AllocationClassDoc(
            label=label, snapshot_category="X", sigma_class=sigma, target_pct=pct,
            instruments=[AllocationInstrument(symbol=sym, role="primary",
                                              weight_within_class_pct=100.0, domicile="IE")],
        )
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.178, nvda_cap_pct=13.0, fi_pct=8.0,
        provenance="test",
        classes=[
            _cls("US core", "us_equity", 30.60, "CSPX"),
            _cls("Growth", "us_growth_equity", 12.83, "R1GR"),
            _cls("NVDA", "concentrated_equity", 12.00, "NVDA"),
            _cls("Gold", "alternatives", 3.00, "SGLN"),
            _cls("Cash", "cash", 5.60, "IB01"),
            _cls("Bonds", "bonds", 2.40, "IBTA"),
        ],
        glide=[],
    )


def _live_sleeves() -> list[InstrumentWeight]:
    """The plan v37 equity sleeves + direct NVDA (full-book weights)."""
    return [
        InstrumentWeight("NVDA", "Strategic single-stock (NVDA)", 12.00),
        InstrumentWeight("CSPX", "US broad-market core", 30.60),
        InstrumentWeight("R1GR", "US growth tilt (ex-NVDA)", 12.83),
        InstrumentWeight("FUSA", "Dividend-quality income", 10.86),
        InstrumentWeight("EXUS", "International developed (ex-US)", 10.86),
        InstrumentWeight("EIMI", "Emerging-markets equity", 3.95),
    ]


def _live_holdings() -> dict[str, tuple[float | None, str]]:
    # NVDA weight INSIDE each fund (percent points), from yfinance top_holdings.
    return {
        "CSPX": (7.88, "yfinance"),
        "R1GR": (13.93, "yfinance"),
        "FUSA": (7.86, "yfinance"),
        "EXUS": (0.0, "yfinance"),   # ex-US index → no NVDA
        "EIMI": (0.0, "yfinance"),   # EM index → no NVDA
    }


def test_economic_nvda_exceeds_direct_ceiling():
    r = compute_economic_nvda(_live_sleeves(), _live_holdings(), as_of="2026-06-15")
    assert r.direct_pct == 12.00
    # indirect = 30.60*.0788 + 12.83*.1393 + 10.86*.0786 + 0 + 0
    expected_indirect = 30.60 * 7.88 / 100 + 12.83 * 13.93 / 100 + 10.86 * 7.86 / 100
    assert abs(r.indirect_pct - round(expected_indirect, 4)) < 1e-6
    assert abs(r.economic_pct - round(12.0 + expected_indirect, 4)) < 1e-6
    # The whole point: economic > the 12% direct ceiling.
    assert r.economic_pct > 12.0
    assert 16.5 < r.economic_pct < 17.5  # ~17%
    assert r.fully_resolved is True
    assert r.unresolved_symbols == []
    assert r.as_of == "2026-06-15"


def test_resolved_zero_nvda_is_not_unresolved():
    """A fund that genuinely holds no NVDA (ex-US/EM index) contributes 0 and is
    RESOLVED — it must not pollute the unresolved set."""
    r = compute_economic_nvda(_live_sleeves(), _live_holdings())
    by_sym = {c.symbol: c for c in r.contributions}
    assert by_sym["EXUS"].nvda_weight_in_fund_pct == 0.0
    assert by_sym["EXUS"].nvda_contribution_pct == 0.0
    assert "EXUS" not in r.unresolved_symbols


def test_unresolved_sleeve_excluded_and_flagged():
    """A fund whose holdings could not be fetched is flagged unresolved and
    EXCLUDED from the indirect sum — never silently assumed 0 (fail-loud)."""
    sleeves = _live_sleeves()
    holdings = _live_holdings()
    del holdings["R1GR"]  # simulate a fetch failure for the biggest contributor
    r = compute_economic_nvda(sleeves, holdings)
    assert "R1GR" in r.unresolved_symbols
    assert r.fully_resolved is False
    # R1GR's ~1.79pp must NOT be in the economic figure (honest lower bound).
    full = compute_economic_nvda(sleeves, _live_holdings())
    assert r.economic_pct < full.economic_pct
    r1gr = next(c for c in r.contributions if c.symbol == "R1GR")
    assert r1gr.nvda_contribution_pct is None


def test_explicit_none_weight_is_unresolved():
    holdings = {"CSPX": (None, "stale")}
    r = compute_economic_nvda(
        [InstrumentWeight("NVDA", "NVDA", 12.0), InstrumentWeight("CSPX", "core", 30.6)],
        holdings,
    )
    assert r.unresolved_symbols == ["CSPX"]
    assert r.indirect_pct == 0.0
    assert r.economic_pct == 12.0


def test_no_direct_nvda_instrument():
    r = compute_economic_nvda(
        [InstrumentWeight("CSPX", "core", 30.6)],
        {"CSPX": (7.88, "yf")},
    )
    assert r.direct_pct == 0.0
    assert abs(r.economic_pct - round(30.6 * 7.88 / 100, 4)) < 1e-6


def test_plan_instruments_excludes_non_equity_sleeves():
    """Cash / bonds / gold hold no NVDA by definition — they must be excluded
    from the look-through entirely (neither fetched nor flagged unresolved), so
    a bond/gold sleeve never pollutes the breakdown. The direct NVDA position is
    kept; equity sleeves are fetched."""
    instruments, fetch_symbols = _plan_instruments(_fake_doc())
    syms = {i.symbol for i in instruments}
    assert syms == {"CSPX", "R1GR", "NVDA"}  # gold/cash/bonds dropped
    assert set(fetch_symbols) == {"CSPX", "R1GR"}  # NVDA itself not fetched
    # full-book weights = class target × within-class (here 100%).
    by_sym = {i.symbol: i.portfolio_weight_pct for i in instruments}
    assert by_sym["CSPX"] == 30.60
    assert by_sym["NVDA"] == 12.00


def test_nvda_pct_from_holdings_validation():
    """Corrupt/empty holdings → None (refetch/unresolved), never silent 0
    (codex #3); out-of-range fractions rejected (codex #5); a non-empty table
    lacking NVDA is a genuine resolved-0."""
    # empty / wrong-type → None (unusable, not resolved-0)
    assert _nvda_pct_from_holdings({}) is None
    assert _nvda_pct_from_holdings(None) is None
    assert _nvda_pct_from_holdings([("NVDA", 0.1)]) is None
    # non-empty without NVDA → genuine resolved-0
    assert _nvda_pct_from_holdings({"AAPL": 0.12, "MSFT": 0.09}) == 0.0
    # normal fraction → percent points
    assert _nvda_pct_from_holdings({"NVDA": 0.0788, "AAPL": 0.07}) == 7.88
    # out-of-range / non-numeric → None (corrupt, never overstate)
    assert _nvda_pct_from_holdings({"NVDA": 1.5}) is None
    assert _nvda_pct_from_holdings({"NVDA": -0.1}) is None
    assert _nvda_pct_from_holdings({"NVDA": "x"}) is None


def test_unresolved_symbols_deduped():
    """A fund symbol appearing in two classes must not produce duplicate
    unresolved entries (codex #2)."""
    sleeves = [
        InstrumentWeight("NVDA", "nvda", 12.0),
        InstrumentWeight("CSPX", "core", 20.0),
        InstrumentWeight("CSPX", "core2", 10.0),
    ]
    r = compute_economic_nvda(sleeves, {})  # CSPX unresolved, twice
    assert r.unresolved_symbols == ["CSPX"]


def test_contributions_sorted_desc_unresolved_last():
    sleeves = _live_sleeves()
    holdings = _live_holdings()
    del holdings["FUSA"]  # one unresolved
    r = compute_economic_nvda(sleeves, holdings)
    resolved = [c for c in r.contributions if c.nvda_contribution_pct is not None]
    contribs = [c.nvda_contribution_pct for c in resolved]
    assert contribs == sorted(contribs, reverse=True)
    # the unresolved row sorts to the end
    assert r.contributions[-1].symbol == "FUSA"
