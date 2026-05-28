"""Tests for portfolio-weighted sigma calibration (Wave 3 · HIGH #7)."""
import json
from datetime import date, datetime, timezone

import pytest

from argosy.services.retirement.sigma_calibration import (
    SigmaCalibration,
    calibrate_sigma_from_holdings,
)
from argosy.state.models import PortfolioSnapshotRow, User


def _seed_user(session, user_id: str = "ariel") -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()


def _seed_snapshot(session, *, positions: list[dict], user_id: str = "ariel") -> None:
    session.add(
        PortfolioSnapshotRow(
            user_id=user_id,
            snapshot_date=date(2026, 5, 1),
            imported_at=datetime.now(timezone.utc),
            source_path="/tmp/seed.tsv",
            positions_json=json.dumps(positions),
            allocations_json="[]",
            nvda_sales_json="[]",
            real_estate_json="[]",
            totals_json="{}",
        )
    )
    session.commit()


class TestSigmaCalibration:
    def test_nvda_heavy_lifts_sigma(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snapshot(s, positions=[
                {"symbol": "NVDA", "asset_type": "NVIDIA", "usd_value_k": 2400.0},
                {"symbol": "VOO", "asset_type": "etf", "usd_value_k": 200.0},
                {"symbol": "SGOV", "asset_type": "etf", "details": "Treasury", "usd_value_k": 200.0},
                {"symbol": "-", "asset_type": "Cash", "usd_value_k": 200.0},
            ])
            cal = calibrate_sigma_from_holdings(user_id="ariel", session=s)
        assert isinstance(cal, SigmaCalibration)
        # 2.4/3.0 = 80% NVDA × 0.45 + 0.067 × 0.18 + 0.067 × 0.06 + 0.067 × 0.02
        # ≈ 0.36 + 0.012 + 0.004 + 0.0013 ≈ 0.377
        assert cal.sigma_annual.value > 0.30
        assert cal.sigma_annual.value < 0.40
        # Concentrated equity has the largest contribution
        assert cal.breakdown[0]["asset_class"] == "concentrated_equity"

    def test_diversified_portfolio_keeps_sigma_low(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snapshot(s, positions=[
                {"symbol": "VOO", "asset_type": "etf", "usd_value_k": 600.0},
                {"symbol": "VEA", "asset_type": "etf", "details": "International developed", "usd_value_k": 200.0},
                {"symbol": "BND", "asset_type": "etf", "details": "Bond index", "usd_value_k": 200.0},
            ])
            cal = calibrate_sigma_from_holdings(user_id="ariel", session=s)
        # 60% × 0.18 + 20% × 0.20 + 20% × 0.06 = 0.108 + 0.04 + 0.012 = 0.16
        assert cal.sigma_annual.value == pytest.approx(0.16, abs=0.02)

    def test_all_cash_returns_low_sigma(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snapshot(s, positions=[
                {"symbol": "-", "asset_type": "Cash", "usd_value_k": 1000.0},
            ])
            cal = calibrate_sigma_from_holdings(user_id="ariel", session=s)
        assert cal.sigma_annual.value == pytest.approx(0.02)

    def test_no_snapshot_returns_diversified_default(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            cal = calibrate_sigma_from_holdings(user_id="ariel", session=s)
        assert cal.sigma_annual.value == pytest.approx(0.18)
        assert cal.sigma_annual.confidence == "low"

    def test_breakdown_sorted_by_value_descending(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snapshot(s, positions=[
                {"symbol": "-", "asset_type": "Cash", "usd_value_k": 100.0},
                {"symbol": "NVDA", "asset_type": "NVIDIA", "usd_value_k": 1000.0},
                {"symbol": "VOO", "asset_type": "etf", "usd_value_k": 500.0},
            ])
            cal = calibrate_sigma_from_holdings(user_id="ariel", session=s)
        usd_values = [row["usd_value"] for row in cal.breakdown]
        assert usd_values == sorted(usd_values, reverse=True)

    def test_alternatives_considered_present(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_snapshot(s, positions=[
                {"symbol": "NVDA", "asset_type": "NVIDIA", "usd_value_k": 1000.0},
            ])
            cal = calibrate_sigma_from_holdings(user_id="ariel", session=s)
        assert len(cal.sigma_annual.alternatives_considered) >= 2
