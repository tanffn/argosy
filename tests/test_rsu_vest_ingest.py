"""Tests for the Schwab Equity Awards parser extension + rsu_vest_events ingest.

Fixture: the real Schwab CSV export at
`tests/fixtures/portfolio_ingest_schwab/EquityAwardsCenter_Transactions_20260529.csv`
(74 vest events spanning 2022-06-15 → 2026-03-18).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.rsu_reconciliation.schwab_csv import parse_csv
from argosy.services.rsu_vest_ingest import ingest_schwab_vest_events
from argosy.state.models import Base, RsuVestEvent, User


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "portfolio_ingest_schwab"
    / "EquityAwardsCenter_Transactions_20260529.csv"
)


@pytest.fixture
def db_session(tmp_path):
    """Self-contained SQLite + seeded user 'ariel'."""
    db_path = tmp_path / "rsu_vest_ingest.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


class TestParserExtension:
    """Parser-level tests against the real CSV fixture."""

    def test_fixture_exists(self):
        assert FIXTURE_PATH.exists(), (
            f"Real Schwab CSV fixture missing at {FIXTURE_PATH}. "
            "Drop a fresh export from Schwab Equity Awards Center > "
            "Transaction History."
        )

    def test_parses_expected_action_mix(self):
        """The real CSV's exact counts (codex NIT: tighten from >=50 to
        the known fixture totals so regressions surface immediately)."""
        r = parse_csv(FIXTURE_PATH)
        # Observed in fixture 2026-05-29:
        assert len(r.sales) == 36
        assert len(r.disbursements) == 27
        assert len(r.vest_events) == 74
        # 74 RSU Deposits + 8 ESPP Deposits = 82.
        assert r.unparsed_actions.get("Deposit") == 82
        # No truncated Lapses in a clean export.
        assert r.unparsed_actions.get("Lapse_no_continuation", 0) == 0

    def test_vest_event_field_extraction(self):
        """Spot-check that a specific known vest event lands with the
        correct fields. The fixture contains a 2026-03-18 batch from 5
        different grants, all at FMV $181.93. Picking grant 213000 qty 280."""
        r = parse_csv(FIXTURE_PATH)
        matches = [
            v for v in r.vest_events
            if v.grant_id == "213000"
            and v.date.isoformat() == "2026-03-18"
        ]
        assert len(matches) == 1, (
            f"expected exactly one 2026-03-18 vest for grant 213000; "
            f"got {len(matches)}"
        )
        v = matches[0]
        assert v.symbol == "NVDA"
        assert v.shares_vested == 280
        assert v.fmv_per_share_usd == pytest.approx(181.93)
        assert v.award_date.isoformat() == "2022-06-08"

    def test_lapse_without_continuation_does_not_crash(self, tmp_path):
        """Defensive: a truncated CSV ending mid-event shouldn't raise."""
        csv = tmp_path / "truncated.csv"
        csv.write_text(
            '"Date","Action","Symbol","Description","Quantity",'
            '"FeesAndCommissions","DisbursementElection","Amount","Type",'
            '"Shares","SalePrice","SubscriptionDate","SubscriptionFair'
            'MarketValue","PurchaseDate","PurchasePrice","PurchaseFair'
            'MarketValue","DispositionType","GrantId","VestDate","Vest'
            'FairMarketValue","GrossProceeds","TotalCostBasis","Realized'
            'GainLoss","HoldingPeriod","AwardDate","AwardId","FairMarket'
            'ValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited",'
            '"Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold",'
            '"CashRefund","CarryForward"\n'
            '"03/18/2026","Lapse","NVDA","Restricted Stock Lapse","90",'
            + ('"",' * 30) + '""\n'
            # Truncated — no continuation row.
        )
        r = parse_csv(csv)
        assert len(r.vest_events) == 0
        # Lapse should NOT count as unparsed since we accepted the action.


class TestIngestService:
    """Ingest-level tests — service writes rows to rsu_vest_events table."""

    def test_first_ingest_persists_all_events(self, db_session):
        result = ingest_schwab_vest_events(
            session=db_session,
            user_id="ariel",
            csv_path=FIXTURE_PATH,
        )
        # Exact known count per codex NIT.
        assert result.parsed_event_count == 74
        assert result.inserted_count == 74
        assert result.duplicate_count == 0

        rows = db_session.query(RsuVestEvent).all()
        assert len(rows) == 74

    def test_second_ingest_is_idempotent(self, db_session):
        """Re-running the ingest against the same CSV must NOT duplicate
        rows. (user_id, grant_id, vest_date) is UNIQUE."""
        first = ingest_schwab_vest_events(
            session=db_session, user_id="ariel", csv_path=FIXTURE_PATH,
        )
        second = ingest_schwab_vest_events(
            session=db_session, user_id="ariel", csv_path=FIXTURE_PATH,
        )
        assert first.inserted_count > 0
        assert second.inserted_count == 0
        assert second.duplicate_count == first.parsed_event_count

        row_count = db_session.query(RsuVestEvent).count()
        assert row_count == first.parsed_event_count

    def test_per_grant_vest_count(self, db_session):
        """Sanity: at least 5 distinct grants in the fixture (213000,
        246477, 289172, 289173, 331375 from the 2026-03-18 batch)."""
        ingest_schwab_vest_events(
            session=db_session, user_id="ariel", csv_path=FIXTURE_PATH,
        )
        grants = {
            r.grant_id
            for r in db_session.query(RsuVestEvent.grant_id).all()
        }
        assert len(grants) >= 5
        # The observed 2026-03-18 batch:
        for expected in {"213000", "246477", "289172", "289173", "331375"}:
            assert expected in grants, (
                f"expected grant {expected} in fixture; got {grants}"
            )

    def test_data_quality_constraints_fire(self, db_session):
        """The CHECK constraints on shares + fmv must reject bad data."""
        from datetime import date

        # Negative shares_vested → should fail.
        with pytest.raises(sa.exc.IntegrityError):
            db_session.add(RsuVestEvent(
                user_id="ariel",
                symbol="NVDA",
                grant_id="TEST1",
                vest_date=date(2026, 1, 1),
                shares_vested=Decimal("-1"),
                shares_withheld=Decimal("0"),
                shares_net=Decimal("0"),
                fmv_per_share_usd=Decimal("100"),
                award_date=None,
                source_file="bogus",
            ))
            db_session.flush()
        db_session.rollback()

        # FMV zero → should fail (must be > 0).
        with pytest.raises(sa.exc.IntegrityError):
            db_session.add(RsuVestEvent(
                user_id="ariel",
                symbol="NVDA",
                grant_id="TEST2",
                vest_date=date(2026, 1, 1),
                shares_vested=Decimal("100"),
                shares_withheld=Decimal("0"),
                shares_net=Decimal("100"),
                fmv_per_share_usd=Decimal("0"),
                award_date=None,
                source_file="bogus",
            ))
            db_session.flush()
        db_session.rollback()

        # withheld > vested → should fail (codex NICE invariant).
        with pytest.raises(sa.exc.IntegrityError):
            db_session.add(RsuVestEvent(
                user_id="ariel",
                symbol="NVDA",
                grant_id="TEST3",
                vest_date=date(2026, 1, 1),
                shares_vested=Decimal("10"),
                shares_withheld=Decimal("20"),  # > vested
                shares_net=Decimal("0"),
                fmv_per_share_usd=Decimal("100"),
                award_date=None,
                source_file="bogus",
            ))
            db_session.flush()
        db_session.rollback()


class TestLapseObservability:
    """Observability counter for unmatched Lapse rows (codex IMPORTANT)."""

    def test_truncated_lapse_increments_diagnostic_counter(self, tmp_path):
        """When a Lapse row's continuation is missing (or interrupted by
        another action before the continuation arrives), the parser must
        surface this in unparsed_actions as 'Lapse_no_continuation' so a
        stuck-pipeline state is visible rather than silent."""
        csv = tmp_path / "lapse_then_sale.csv"
        csv.write_text(
            '"Date","Action","Symbol","Description","Quantity",'
            '"FeesAndCommissions","DisbursementElection","Amount","Type",'
            '"Shares","SalePrice","SubscriptionDate","SubscriptionFair'
            'MarketValue","PurchaseDate","PurchasePrice","PurchaseFair'
            'MarketValue","DispositionType","GrantId","VestDate","Vest'
            'FairMarketValue","GrossProceeds","TotalCostBasis","Realized'
            'GainLoss","HoldingPeriod","AwardDate","AwardId","FairMarket'
            'ValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited",'
            '"Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold",'
            '"CashRefund","CarryForward"\n'
            # Lapse without continuation — interrupted by a Sale below.
            '"03/18/2026","Lapse","NVDA","Restricted Stock Lapse","90",'
            + ('"",' * 30) + '""\n'
            # Sale row arrives before any continuation — should bump counter.
            '"05/08/2026","Sale","NVDA","Share Sale","100","$1.00","","$10000",'
            + ('"",' * 27) + '""\n'
        )
        r = parse_csv(csv)
        assert len(r.vest_events) == 0
        assert r.unparsed_actions.get("Lapse_no_continuation") == 1
