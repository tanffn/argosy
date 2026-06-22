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


def _canonical_doc():
    """Canonical 2-class doc: Growth→CNDX (70%), Defensive→IB01 (30%). The
    long-term instruments are sourced from THIS doc, not a hardcoded class map."""
    from datetime import date as _date

    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, GlideWaypoint, TargetAllocationDoc,
    )
    def _cls(label, sym, pct):
        return AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="us_equity",
            target_pct=pct,
            instruments=[AllocationInstrument(
                symbol=sym, role="primary", weight_within_class_pct=100.0,
                domicile="IE")])
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[_cls("Growth", "CNDX", 70.0), _cls("Defensive", "IB01", 30.0)],
        glide=[GlideWaypoint(quarter=0, date=_date(2026, 1, 1),
               composition_pct_by_class={"Growth": 70.0, "Defensive": 30.0})],
    )


def _propose(event, *, holdings=None):
    from datetime import date as _date
    return propose_allocations(
        event, doc=_canonical_doc(), holdings=holdings or {},
        as_of=_date(2026, 6, 1))


class TestAllocator:
    def test_long_term_instruments_come_from_canonical_doc(self) -> None:
        """Long-term picks are the canonical doc's instruments (CNDX/IB01),
        NOT a hardcoded class→ticker map."""
        plan = _propose(_stub_event(windfall_usd=100_000))
        instruments = {p.instrument for p in plan.long_term}
        assert instruments and instruments <= {"CNDX", "IB01"}

    def test_long_term_requires_canonical_doc(self) -> None:
        """No doc → fail loud, never a silent hardcoded fallback."""
        from datetime import date as _date
        with pytest.raises(ValueError):
            propose_allocations(_stub_event(windfall_usd=100_000), doc=None,
                                holdings={}, as_of=_date(2026, 6, 1))

    def test_budget_split_60_25_15(self) -> None:
        plan = _propose(_stub_event(windfall_usd=100_000))
        long_sum = sum(p.amount_usd for p in plan.long_term)
        med_sum = sum(p.amount_usd for p in plan.medium_term)
        short_sum = sum(p.amount_usd for p in plan.short_term)
        # Long ≤ 60% (the empty-book deploy of 60k fully places against targets).
        assert long_sum <= 60_000 + 1
        assert long_sum == pytest.approx(60_000, abs=1)
        assert med_sum == pytest.approx(25_000, abs=1)
        assert short_sum == pytest.approx(15_000, abs=1)

    def test_long_term_buys_split_by_canonical_weights(self) -> None:
        """60k long budget on an empty book splits 70/30 across CNDX/IB01 —
        the canonical glide weights, deterministically."""
        plan = _propose(_stub_event(windfall_usd=100_000))
        by_sym = {p.instrument: p.amount_usd for p in plan.long_term}
        assert by_sym.get("CNDX") == pytest.approx(42_000, abs=1)  # 70% of 60k
        assert by_sym.get("IB01") == pytest.approx(18_000, abs=1)  # 30% of 60k

    def test_medium_short_have_placeholder_rationale(self) -> None:
        plan = _propose(_stub_event(windfall_usd=100_000))
        for p in plan.medium_term:
            assert "agent fleet" in p.rationale.lower() or "synthesis" in p.rationale.lower()
        for p in plan.short_term:
            assert "watchlist" in p.rationale.lower() or "opportun" in p.rationale.lower()


# ─── Transaction-based source attribution ────────────────────────────────


def _live_shaped_schwab_csv() -> str:
    return textwrap.dedent('''\
"Date","Action","Symbol","Description","Quantity","FeesAndCommissions","DisbursementElection","Amount","Type","Shares","SalePrice","SubscriptionDate","SubscriptionFairMarketValue","PurchaseDate","PurchasePrice","PurchaseFairMarketValue","DispositionType","GrantId","VestDate","VestFairMarketValue","GrossProceeds","TotalCostBasis","RealizedGainLoss","HoldingPeriod","AwardDate","AwardId","FairMarketValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited","Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold","CashRefund","CarryForward"
"05/08/2026","Sale","NVDA","Share Sale","560","$2.60","","$121,005.00","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","RS","560","$216.085","","","","","","","182406","09/18/2024","$115.59","","$64,730.40","$56,277.20","LONG TERM","","","","","","","","","","",""
''')


@pytest.fixture()
def _windfall_db():
    from datetime import date

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import (
        Base,
        ExpenseSource,
        ExpenseStatement,
        ExpenseTransaction,
        User,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(User(id="ariel", email="a@example.com"))
    s.add(ExpenseSource(
        id=6, user_id="ariel", kind="bank", issuer="leumi",
        external_id="44745200", display_name="Leumi USD account",
    ))
    s.add(ExpenseStatement(
        id=1, user_id="ariel", source_id=6, file_id=1,
        period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        parsed_total_nis=0, parser_name="leumi_usd", parser_version="1",
        status="ok",
    ))
    s.add(ExpenseTransaction(
        id=2220, user_id="ariel", statement_id=1, source_id=6,
        occurred_on=date(2026, 6, 8), merchant_raw="העברת כספים",
        merchant_normalized="העברת כספים", amount_orig=112229.99,
        currency_orig="USD", direction="credit", tx_type="transfer",
        raw_row_json="{}",
    ))
    s.commit()
    yield s
    s.close()


def test_attribute_cash_source_upgrades_unclear(tmp_path: Path, _windfall_db) -> None:
    """The proven flow: a windfall starts 'unclear' (TSV-diff neutralized),
    then transaction-based attribution links the 560-NVDA sale → $112,230
    Leumi transfer and upgrades it to a confident rsu_sale with a traceable
    source line."""
    from argosy.services.retirement.windfall_detector import (
        WindfallEvent,
        attribute_cash_source,
    )

    csv_path = tmp_path / "EquityAwardsCenter_Transactions.csv"
    csv_path.write_text(_live_shaped_schwab_csv(), encoding="utf-8")

    event = WindfallEvent(
        detected_at=__import__("datetime").datetime.now(),
        cash_delta_usd=112_229.99,
        cash_delta_nis=0.0,
        cash_delta_total_usd_equiv=112_229.99,
        fx_usd_nis=3.0,
        classified_source="unclear",
        requires_user_classification=True,
        source_tsv="cur.tsv",
    )
    event = attribute_cash_source(event, csv_path, _windfall_db, "ariel")

    assert event.classified_source == "rsu_sale"
    assert event.requires_user_classification is False
    assert len(event.reconciled_source_lines) == 1
    line = event.reconciled_source_lines[0]
    assert "560 sh" in line
    assert "112,230" in line
    assert event.reconciled_matched_usd == pytest.approx(112229.99)
    assert event.reconciled_unexplained_usd == pytest.approx(0.0)


def test_attribute_cash_source_degrades_when_no_csv(tmp_path: Path, _windfall_db) -> None:
    """Missing Schwab CSV → event stays 'unclear', no source lines, no raise."""
    from argosy.services.retirement.windfall_detector import (
        WindfallEvent,
        attribute_cash_source,
    )

    missing = tmp_path / "does_not_exist.csv"
    event = WindfallEvent(
        detected_at=__import__("datetime").datetime.now(),
        cash_delta_usd=112_229.99,
        cash_delta_nis=0.0,
        cash_delta_total_usd_equiv=112_229.99,
        fx_usd_nis=3.0,
        classified_source="unclear",
        requires_user_classification=True,
    )
    event = attribute_cash_source(event, missing, _windfall_db, "ariel")
    assert event.classified_source == "unclear"
    assert event.reconciled_source_lines == []
    # No CSV → nothing to reconcile against; no attribution, no residual.
    assert event.reconciled_matched_usd == pytest.approx(0.0)
    assert event.reconciled_unexplained_usd == pytest.approx(0.0)


_SIM_PATH = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Schwab/"
    "Nvidia simulation Report.xlsx"
)


@pytest.mark.skipif(not _SIM_PATH.exists(), reason="simulation report not mounted")
def test_windfall_surface_shows_grant_derived_102_tax(tmp_path: Path) -> None:
    """When the sim report sits beside the Schwab CSV, the windfall source line
    shows the grant-derived §102 capital/ordinary split — NOT a flat estimate."""
    import shutil
    from datetime import date

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.services.retirement.windfall_detector import (
        WindfallEvent,
        attribute_cash_source,
    )
    from argosy.state.models import (
        Base, ExpenseSource, ExpenseStatement, ExpenseTransaction, User,
    )

    # Dedicated DB: the single 2026-05-18 transfer the 560-sh @ $216.09 sale
    # produced (~$88K net under the §102 wire-calibrated model).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id="ariel", email="a@example.com"))
    db.add(ExpenseSource(
        id=6, user_id="ariel", kind="bank", issuer="leumi",
        external_id="44745200", display_name="Leumi USD account",
    ))
    db.add(ExpenseStatement(
        id=1, user_id="ariel", source_id=6, file_id=1,
        period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        parsed_total_nis=0, parser_name="leumi_usd", parser_version="1",
        status="ok",
    ))
    db.add(ExpenseTransaction(
        id=2223, user_id="ariel", statement_id=1, source_id=6,
        occurred_on=date(2026, 5, 18), merchant_raw="העברת כספים",
        merchant_normalized="העברת כספים", amount_orig=88253.43,
        currency_orig="USD", direction="credit", tx_type="transfer",
        raw_row_json="{}",
    ))
    db.commit()

    csv_path = tmp_path / "EquityAwardsCenter_Transactions.csv"
    csv_path.write_text(_live_shaped_schwab_csv(), encoding="utf-8")
    # Place the real sim report next to the CSV so it auto-resolves.
    shutil.copy(_SIM_PATH, tmp_path / "Nvidia simulation Report.xlsx")

    event = WindfallEvent(
        detected_at=__import__("datetime").datetime.now(),
        cash_delta_usd=88_253.43, cash_delta_nis=0.0,
        cash_delta_total_usd_equiv=88_253.43, fx_usd_nis=3.0,
        classified_source="unclear", requires_user_classification=True,
        source_tsv="cur.tsv",
    )
    event = attribute_cash_source(event, csv_path, db, "ariel")
    db.close()

    assert event.classified_source == "rsu_sale"
    assert len(event.reconciled_source_lines) == 1
    line = event.reconciled_source_lines[0]
    assert "§102" in line
    assert "capital @25%" in line
    assert "ordinary @50%" in line   # wire-calibrated effective ordinary rate
    assert "flat estimate" not in line
    # The ValueWithRationale rationale documents the §102 model.
    vwr = event.to_value_with_rationale_dict()["reconciled_source"]
    assert "§102" in vwr.rationale
