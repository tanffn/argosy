"""Tests for the trend-radar scoring + filter core (pure, no network)."""
from __future__ import annotations

from argosy.services.trend_radar import (
    LiquidityFilter,
    RawSignal,
    score_and_filter,
    score_signal,
)


def _sig(ticker, **kw) -> RawSignal:
    fams = set(kw.pop("families", []))
    reasons = list(kw.pop("reasons", []))
    return RawSignal(ticker=ticker, families=fams, reasons=reasons, **kw)


# --- scoring ---------------------------------------------------------------


def test_family_weights_sum():
    s = _sig("X", families={"MOMENTUM", "ATTENTION", "GROWTH"})
    assert score_signal(s) == 90.0  # 35 + 30 + 25


def test_change_bonus_capped():
    s = _sig("X", families={"MOMENTUM"}, pct_change=60.0)  # 60/3=20 -> capped 10
    assert score_signal(s) == 45.0  # 35 + 10
    s2 = _sig("Y", families={"MOMENTUM"}, pct_change=9.0)  # 9/3 = 3
    assert score_signal(s2) == 38.0


# --- pump guard (>= 2 families) -------------------------------------------


def _liquid_kw():
    return dict(price=20.0, market_cap=2e9, avg_volume=2_000_000)


def test_single_family_held_back():
    uni = {"AAA": _sig("AAA", families={"MOMENTUM"}, **_liquid_kw())}
    res = score_and_filter(uni)
    assert res.shortlist == ()  # one family only — not surfaced
    # not quarantined as attention-only/liquidity either
    assert all(t != "AAA" for t, _ in res.quarantine)


def test_two_families_make_shortlist():
    uni = {"BBB": _sig("BBB", families={"MOMENTUM", "GROWTH"}, **_liquid_kw())}
    res = score_and_filter(uni)
    assert [c.ticker for c in res.shortlist] == ["BBB"]
    assert res.shortlist[0].score == 60.0  # 35 + 25


def test_attention_only_quarantined():
    uni = {"CCC": _sig("CCC", families={"ATTENTION"}, **_liquid_kw())}
    res = score_and_filter(uni)
    assert ("CCC", "attention-only") in res.quarantine
    assert res.shortlist == ()


# --- liquidity filter ------------------------------------------------------


def test_penny_stock_failed_liquidity():
    uni = {"PNY": _sig("PNY", families={"MOMENTUM", "ATTENTION"},
                       price=2.0, market_cap=1e9, avg_volume=5_000_000)}
    res = score_and_filter(uni)
    assert ("PNY", "failed-liquidity") in res.quarantine
    assert res.shortlist == ()


def test_megacap_excluded_by_cap_band():
    # 2-family but a $200B cap is above the satellite band -> filtered out.
    uni = {"MEGA": _sig("MEGA", families={"MOMENTUM", "GROWTH"},
                        price=300.0, market_cap=200e9, avg_volume=10_000_000)}
    res = score_and_filter(uni)
    assert ("MEGA", "failed-liquidity") in res.quarantine


def test_thin_dollar_volume_failed():
    # price*avg_volume = 6 * 1000 = $6k/day, below the $10M floor.
    uni = {"THIN": _sig("THIN", families={"MOMENTUM", "ATTENTION"},
                        price=6.0, market_cap=1e9, avg_volume=1_000)}
    res = score_and_filter(uni)
    assert ("THIN", "failed-liquidity") in res.quarantine


def test_unknown_fields_tolerated():
    # No market cap / volume known: liquidity passes (only KNOWN-bad rejects),
    # pump guard still requires 2 families.
    uni = {"UNK": _sig("UNK", families={"MOMENTUM", "GROWTH"}, price=12.0)}
    res = score_and_filter(uni)
    assert [c.ticker for c in res.shortlist] == ["UNK"]


def test_ambiguous_short_symbol_dropped():
    uni = {"A": _sig("A", families={"ATTENTION"})}  # no price, 1 char
    res = score_and_filter(uni)
    assert ("A", "ambiguous-short-symbol") in res.quarantine


def test_shortlist_sorted_by_score_and_limited():
    uni = {
        "HI": _sig("HI", families={"MOMENTUM", "ATTENTION", "GROWTH"}, **_liquid_kw()),
        "LO": _sig("LO", families={"MOMENTUM", "GROWTH"}, **_liquid_kw()),
    }
    res = score_and_filter(uni, limit=1)
    assert [c.ticker for c in res.shortlist] == ["HI"]  # 90 > 60, limit 1


def test_custom_filter_band():
    f = LiquidityFilter(cap_max=500e9)  # allow megacaps
    uni = {"MEGA": _sig("MEGA", families={"MOMENTUM", "GROWTH"},
                        price=300.0, market_cap=200e9, avg_volume=10_000_000)}
    res = score_and_filter(uni, filters=f)
    assert [c.ticker for c in res.shortlist] == ["MEGA"]


# --- MOMENTUM_SUSTAINED family + thematic lane ------------------------------


def test_momentum_sustained_family_weight():
    s = _sig("X", families={"MOMENTUM_SUSTAINED"})
    assert score_signal(s) == 40.0


def test_momentum_sustained_participates_in_pump_guard():
    # single family (even the highest-weighted one) is still held back
    uni = {"AAA": _sig("AAA", families={"MOMENTUM_SUSTAINED"}, **_liquid_kw())}
    res = score_and_filter(uni)
    assert res.shortlist == ()
    uni2 = {"BBB": _sig("BBB", families={"MOMENTUM_SUSTAINED", "GROWTH"},
                        **_liquid_kw())}
    res2 = score_and_filter(uni2)
    assert [c.ticker for c in res2.shortlist] == ["BBB"]
    assert res2.shortlist[0].score == 65.0  # 40 + 25


def test_thematic_lane_off_by_default_megacap_dropped():
    # 2-family, liquid, but $130bn cap -- above the satellite ceiling and the
    # thematic lane is not enabled -> quarantined, not silently admitted.
    uni = {"MU": _sig("MU", families={"MOMENTUM_SUSTAINED", "GROWTH"},
                      price=120.0, market_cap=130e9, avg_volume=20_000_000)}
    res = score_and_filter(uni)
    assert res.shortlist == ()
    assert ("MU", "failed-liquidity") in res.quarantine


def test_thematic_lane_enabled_admits_large_cap_tagged_thematic():
    uni = {"MU": _sig("MU", families={"MOMENTUM_SUSTAINED", "GROWTH"},
                      price=120.0, market_cap=130e9, avg_volume=20_000_000)}
    res = score_and_filter(uni, thematic_lane_enabled=True)
    assert [c.ticker for c in res.shortlist] == ["MU"]
    assert res.shortlist[0].lane == "thematic"


def test_thematic_lane_still_respects_its_own_band():
    # $600bn is above even the thematic ceiling ($500bn) -> still rejected.
    uni = {"HUGE": _sig("HUGE", families={"MOMENTUM_SUSTAINED", "GROWTH"},
                        price=300.0, market_cap=600e9, avg_volume=50_000_000)}
    res = score_and_filter(uni, thematic_lane_enabled=True)
    assert res.shortlist == ()
    assert ("HUGE", "failed-liquidity") in res.quarantine


def test_satellite_lane_default_tag():
    uni = {"SAT": _sig("SAT", families={"MOMENTUM", "GROWTH"}, **_liquid_kw())}
    res = score_and_filter(uni, thematic_lane_enabled=True)
    assert res.shortlist[0].lane == "satellite"


def test_insufficient_history_flag_propagates_to_candidate():
    uni = {"NEW": _sig("NEW", families={"MOMENTUM_SUSTAINED", "GROWTH"},
                       insufficient_history=True, **_liquid_kw())}
    res = score_and_filter(uni)
    assert res.shortlist[0].insufficient_history is True


def test_thematic_lane_enabled_flag_reads_env(monkeypatch):
    from argosy.services.trend_radar import thematic_lane_enabled

    monkeypatch.delenv("ARGOSY_THEMATIC_LANE_ENABLED", raising=False)
    assert thematic_lane_enabled() is False
    monkeypatch.setenv("ARGOSY_THEMATIC_LANE_ENABLED", "true")
    assert thematic_lane_enabled() is True
    monkeypatch.setenv("ARGOSY_THEMATIC_LANE_ENABLED", "0")
    assert thematic_lane_enabled() is False


def test_to_sleeve_candidates_excludes_thematic_lane():
    from argosy.services.trend_radar import TrendCandidate, to_sleeve_candidates

    cands = [
        TrendCandidate("SAT", "Satellite pick", 80.0, ("MOMENTUM", "GROWTH"),
                       ("r",), 10.0, 1e9, 5e7, 5.0, lane="satellite"),
        TrendCandidate("THM", "Thematic pick", 90.0,
                       ("MOMENTUM_SUSTAINED", "GROWTH"), ("r",), 100.0, 130e9,
                       5e7, 2.0, lane="thematic"),
    ]
    out = to_sleeve_candidates(cands)
    assert [s.ticker for s in out] == ["SAT"]


# --- bridge to the sleeve --------------------------------------------------


def test_to_sleeve_candidates_maps_conviction_and_marks_us_situs():
    from argosy.services.trend_radar import TrendCandidate, to_sleeve_candidates

    cands = [
        TrendCandidate("HIQ", "Hi Conviction", 80.0, ("MOMENTUM", "GROWTH"),
                       ("r",), 10.0, 1e9, 5e7, 5.0),
        TrendCandidate("MED", "Mid", 60.0, ("MOMENTUM", "ATTENTION"),
                       ("r",), 10.0, 1e9, 5e7, 5.0),
        TrendCandidate("LOW", "Low", 50.0, ("MOMENTUM", "ATTENTION"),
                       ("r",), 10.0, 1e9, 5e7, 5.0),
    ]
    out = to_sleeve_candidates(cands, held_tickers=frozenset({"MED"}))
    assert [s.ticker for s in out] == ["HIQ", "MED", "LOW"]
    assert [s.conviction for s in out] == ["HIGH", "MEDIUM", "LOW"]
    assert all(s.vehicle == "single_name" and s.us_situs for s in out)
    assert all(s.source == "trend_radar" for s in out)
    assert out[1].held_today is True
    assert "monitor" in out[0].thesis.lower()


def test_to_sleeve_candidates_respects_max_names():
    from argosy.services.trend_radar import TrendCandidate, to_sleeve_candidates

    cands = [TrendCandidate(f"T{i}", "", 60.0, ("MOMENTUM", "GROWTH"), (),
                            10.0, 1e9, 5e7, 1.0) for i in range(6)]
    assert len(to_sleeve_candidates(cands, max_names=3)) == 3


def test_sleeve_sizes_trend_candidates():
    from argosy.services.high_potential_sleeve import build_high_potential_sleeve
    from argosy.services.trend_radar import TrendCandidate, to_sleeve_candidates

    cands = [
        TrendCandidate("AAA", "A", 80.0, ("MOMENTUM", "GROWTH"), (), 10, 1e9, 5e7, 1),
        TrendCandidate("BBB", "B", 50.0, ("MOMENTUM", "ATTENTION"), (), 10, 1e9, 5e7, 1),
    ]
    sleeve = build_high_potential_sleeve(10_000.0, to_sleeve_candidates(cands))
    # HIGH(3) vs LOW(1) -> 7500 / 2500
    by = {a.candidate.ticker: a.amount_usd for a in sleeve}
    assert by["AAA"] == 7500.0
    assert by["BBB"] == 2500.0
