"""Tests for the windfall detector + plan-aware allocator."""
from pathlib import Path
import tempfile
import textwrap

import pytest

from argosy.services.retirement.windfall_allocator import (
    AllocationProposal,
    propose_allocations,
)
from argosy.services.retirement.windfall_detector import (
    DEFAULT_THRESHOLD_NIS,
    DEFAULT_THRESHOLD_USD,
    AllocationLine,
    WindfallEvent,
    _classify_source,
    detect_windfall,
)


def _write_tsv(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip("\n"), encoding="utf-8")


def _minimal_tsv(
    *,
    leumi_usd_cash: float,
    leumi_nis_cash: float,
    fx: float = 2.94,
    nvda_shares: float = 11471,
    nvda_price: float = 200.14,
) -> str:
    return (
        f"\t24-Mar-26\t\n"
        f"\tUSD to NIS:\t{fx}\n"
        f"\tUSD to EUR:\t0.85\n"
        f"\n"
        f"Bank account / funds allocation\n"
        f"Review Status\tLocation\tCurrency\tType\tDetails\tSymbol\t# Shares\tCurrent price\tAvg Price\tCurrent Value\t(K) USD Value\t% Change\t% Yearly\n"
        f"\tschwab\tUSD\tNVIDIA\tRSU\tNVDA\t{int(nvda_shares)}\t{nvda_price}\t{nvda_price}\t{nvda_shares*nvda_price:,.0f}\t{int(nvda_shares*nvda_price/1000)}\t0%\t\n"
        f"\tLeumi\tNIS\tCash\tCash\t\t{int(leumi_nis_cash)}\t1\t1\t{leumi_nis_cash:,.0f}\t{int(leumi_nis_cash/fx/1000)}\t0%\t\n"
        f"\tLeumi\tUSD\tCash\tCash\t\t{int(leumi_usd_cash)}\t1\t1\t{leumi_usd_cash:,.0f}\t{int(leumi_usd_cash/1000)}\t0%\t\n"
        f"v\tLeumi\tUSD\tCore Equity\tETF\tVOO\t20\t665\t572\t13,300\t13\t16%\t\n"
        f"\n"
        f"Current allocation:\n"
        f"\tType\tSUM of (K) USD Value\tSUM of (K) USD Value\tTargetPct\tTargetK\tDelta (K) USD\t\n"
        f"\tCash\t13%\t188\t5%\t72.7\t-115.4\t\n"
        f"\tCore Equity\t26%\t381\t20%\t290.6\t-90.8\t\n"
        f"\tDefensive\t11%\t161\t10%\t145.3\t-15.3\t\n"
        f"\tGrand Total\t100%\t1453\t100%\t1453.2\t0.0\t\n"
    )


# ─── Detector tests ──────────────────────────────────────────────────────


class TestThresholdGate:
    def test_no_event_below_threshold(self, tmp_path: Path) -> None:
        prev = tmp_path / "prev.tsv"
        cur = tmp_path / "cur.tsv"
        _write_tsv(prev, _minimal_tsv(leumi_usd_cash=55_000, leumi_nis_cash=80_000))
        _write_tsv(cur, _minimal_tsv(leumi_usd_cash=60_000, leumi_nis_cash=80_000))
        # $5K USD delta, ₪0 NIS delta → both below threshold
        assert detect_windfall(cur, prev) is None

    def test_fires_on_usd_threshold(self, tmp_path: Path) -> None:
        prev = tmp_path / "prev.tsv"
        cur = tmp_path / "cur.tsv"
        _write_tsv(prev, _minimal_tsv(leumi_usd_cash=55_000, leumi_nis_cash=80_000))
        _write_tsv(cur, _minimal_tsv(leumi_usd_cash=155_000, leumi_nis_cash=80_000))
        # $100K USD delta → threshold crossed
        event = detect_windfall(cur, prev)
        assert event is not None
        assert event.cash_delta_usd == 100_000.0
        assert event.cash_delta_nis == 0.0

    def test_fires_on_nis_threshold(self, tmp_path: Path) -> None:
        prev = tmp_path / "prev.tsv"
        cur = tmp_path / "cur.tsv"
        _write_tsv(prev, _minimal_tsv(leumi_usd_cash=55_000, leumi_nis_cash=80_000))
        _write_tsv(cur, _minimal_tsv(leumi_usd_cash=55_000, leumi_nis_cash=200_000))
        # ₪120K NIS delta → threshold crossed
        event = detect_windfall(cur, prev)
        assert event is not None
        assert event.cash_delta_nis == 120_000.0

    def test_no_event_when_prev_missing(self, tmp_path: Path) -> None:
        cur = tmp_path / "cur.tsv"
        _write_tsv(cur, _minimal_tsv(leumi_usd_cash=200_000, leumi_nis_cash=80_000))
        assert detect_windfall(cur, None) is None


class TestClassification:
    def test_classify_rsu_sale_when_nvda_matches(self) -> None:
        from argosy.services.retirement.windfall_detector import Sale
        # $100K cash arrived; NVDA -500 shares × $200 ≈ $100K
        sales = [Sale(symbol="NVDA", shares_sold=500, current_price=200, value_usd=100_000)]
        classified, needs_user = _classify_source(100_000, sales)
        assert classified == "rsu_sale"
        assert needs_user is False

    def test_classify_stock_sale_when_non_nvda_matches(self) -> None:
        from argosy.services.retirement.windfall_detector import Sale
        sales = [Sale(symbol="VOO", shares_sold=100, current_price=665, value_usd=66_500)]
        classified, needs_user = _classify_source(66_500, sales)
        assert classified == "stock_sale"
        assert needs_user is False

    def test_unclear_when_no_matching_sale(self) -> None:
        # $100K cash but no sales → unclear (bonus? deposit?)
        classified, needs_user = _classify_source(100_000, [])
        assert classified == "unclear"
        assert needs_user is True

    def test_unclear_when_amount_mismatches(self) -> None:
        from argosy.services.retirement.windfall_detector import Sale
        # $100K cash but only $20K of sales → most must be from elsewhere
        sales = [Sale(symbol="VOO", shares_sold=30, current_price=665, value_usd=19_950)]
        classified, needs_user = _classify_source(100_000, sales)
        assert classified == "unclear"
        assert needs_user is True


class TestEndToEnd:
    def test_full_event_with_rsu_sale(self, tmp_path: Path, monkeypatch) -> None:
        # The legacy TSV-diff sale attribution is now opt-in (it fabricated
        # phantom sales on symbol/column shifts in hand-maintained TSVs); this
        # test exercises that opt-in path on a CLEAN fixture where it's reliable.
        monkeypatch.setenv("ARGOSY_WINDFALL_TSV_SALE_DIFF", "1")
        prev = tmp_path / "prev.tsv"
        cur = tmp_path / "cur.tsv"
        # NVDA stays on Schwab in both files; only the share count drops.
        # Detector should diff (schwab, NVDA) shares directly.
        _write_tsv(prev, _minimal_tsv(
            leumi_usd_cash=55_000, leumi_nis_cash=80_000,
            nvda_shares=11971,
        ))
        _write_tsv(cur, _minimal_tsv(
            leumi_usd_cash=155_000, leumi_nis_cash=80_000,
            nvda_shares=11471,  # sold 500 @ ~$200 ≈ $100K
        ))
        event = detect_windfall(cur, prev)
        assert event is not None
        assert event.cash_delta_usd == 100_000.0
        assert len(event.matching_sales) == 1
        assert event.matching_sales[0].symbol == "NVDA"
        assert event.matching_sales[0].shares_sold == 500
        assert event.classified_source == "rsu_sale"
        assert event.requires_user_classification is False

    def test_default_does_not_fabricate_tsv_diff_sales(self, tmp_path: Path) -> None:
        # DEFAULT (flag off): even when a holding's share count drops between two
        # TSVs, the detector must NOT assert a sale source — TSV diffs are an
        # unreliable output, not a transaction. It surfaces the cash delta only.
        prev = tmp_path / "prev.tsv"
        cur = tmp_path / "cur.tsv"
        _write_tsv(prev, _minimal_tsv(
            leumi_usd_cash=55_000, leumi_nis_cash=80_000, nvda_shares=11971,
        ))
        _write_tsv(cur, _minimal_tsv(
            leumi_usd_cash=155_000, leumi_nis_cash=80_000, nvda_shares=11471,
        ))
        event = detect_windfall(cur, prev)
        assert event is not None
        assert event.cash_delta_usd == 100_000.0  # the cash signal is still real
        assert event.matching_sales == []  # but NO fabricated sale source
        assert event.classified_source == "unclear"
        assert event.requires_user_classification is True

    def test_allocation_table_parsed(self, tmp_path: Path) -> None:
        prev = tmp_path / "prev.tsv"
        cur = tmp_path / "cur.tsv"
        _write_tsv(prev, _minimal_tsv(leumi_usd_cash=55_000, leumi_nis_cash=80_000))
        _write_tsv(cur, _minimal_tsv(leumi_usd_cash=155_000, leumi_nis_cash=80_000))
        event = detect_windfall(cur, prev)
        assert event is not None
        assert len(event.allocation_delta_table) >= 3
        cash_line = next(
            (l for l in event.allocation_delta_table if l.asset_class == "Cash"),
            None,
        )
        assert cash_line is not None
        assert cash_line.delta_k_usd == pytest.approx(-115.4, abs=0.5)


# ─── Allocator tests ─────────────────────────────────────────────────────


def _stub_event(
    *,
    windfall_usd: float = 100_000,
    allocation_table: list[AllocationLine] | None = None,
) -> WindfallEvent:
    return WindfallEvent(
        detected_at=__import__("datetime").datetime.now(),
        cash_delta_usd=windfall_usd,
        cash_delta_nis=0.0,
        cash_delta_total_usd_equiv=windfall_usd,
        fx_usd_nis=3.0,
        matching_sales=[],
        classified_source="rsu_sale",
        requires_user_classification=False,
        allocation_delta_table=allocation_table or [
            AllocationLine(asset_class="Core Equity", current_pct=0.26,
                           current_k_usd=381, target_pct=0.20,
                           target_k_usd=290.6, delta_k_usd=-90.8),
            AllocationLine(asset_class="Defensive", current_pct=0.11,
                           current_k_usd=161, target_pct=0.10,
                           target_k_usd=145.3, delta_k_usd=-15.3),
            AllocationLine(asset_class="Growth", current_pct=0.11,
                           current_k_usd=158, target_pct=0.20,
                           target_k_usd=290.6, delta_k_usd=+132.2),
        ],
        source_tsv="cur.tsv",
        previous_tsv="prev.tsv",
    )


class TestAllocator:
    def test_long_term_fills_under_target_classes_first(self) -> None:
        event = _stub_event(windfall_usd=100_000)
        plan = propose_allocations(event)
        # Biggest under-target gap is Growth (+132K), then International (+67K)
        long_classes = {p.asset_class for p in plan.long_term}
        assert "Growth" in long_classes

    def test_long_term_does_not_add_to_over_target_classes(self) -> None:
        event = _stub_event(windfall_usd=100_000)
        plan = propose_allocations(event)
        # Core Equity has delta -90.8 (OVER target) — should NOT appear
        assert "Core Equity" not in {p.asset_class for p in plan.long_term}

    def test_budget_split_60_25_15(self) -> None:
        event = _stub_event(windfall_usd=100_000)
        plan = propose_allocations(event)
        long_sum = sum(p.amount_usd for p in plan.long_term)
        med_sum = sum(p.amount_usd for p in plan.medium_term)
        short_sum = sum(p.amount_usd for p in plan.short_term)
        # Long ≤ 60% (might be less if total under-target gap < 60%)
        assert long_sum <= 60_000 + 1
        assert med_sum == pytest.approx(25_000, abs=1)
        assert short_sum == pytest.approx(15_000, abs=1)

    def test_preferred_instrument_picked(self) -> None:
        event = _stub_event(windfall_usd=100_000)
        plan = propose_allocations(event)
        growth_picks = [p.instrument for p in plan.long_term
                        if p.asset_class == "Growth"]
        # Domicile-aware (S18): first preferred for Growth is the UCITS twin CNDX,
        # NOT US-domiciled QQQM (US-situs estate exposure for a non-US-person).
        assert "CNDX" in growth_picks
        assert "QQQM" not in growth_picks

    def test_medium_short_have_placeholder_rationale(self) -> None:
        event = _stub_event(windfall_usd=100_000)
        plan = propose_allocations(event)
        for p in plan.medium_term:
            assert "agent fleet" in p.rationale.lower() or "synthesis" in p.rationale.lower()
        for p in plan.short_term:
            assert "watchlist" in p.rationale.lower() or "opportun" in p.rationale.lower()
