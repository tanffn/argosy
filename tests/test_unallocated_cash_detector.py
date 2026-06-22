"""Tests for the self-tuning unallocated-cash detector.

Closes the user-facing "I have $X unallocated cash, what should I do
with it?" flow ([[feedback_unallocated_cash_reframe]]). Threshold is
relative to plan-target cash (default 1.5x), not a hard-coded dollar
amount.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from argosy.ingest.tsv import AllocationRow, PortfolioSnapshot
from argosy.services.unallocated_cash_detector import (
    DEFAULT_OVERAGE_RATIO,
    _detect_from_snapshot,
    _find_cash_row,
)


def _canonical_doc():
    """A minimal canonical TargetAllocationDoc whose single Growth instrument is
    CNDX — used to drive the long-term proposals (instruments come from the doc,
    NOT a hardcoded class map)."""
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, GlideWaypoint, TargetAllocationDoc,
    )
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[AllocationClassDoc(
            label="Growth", snapshot_category="Growth", sigma_class="us_equity",
            target_pct=100.0,
            instruments=[AllocationInstrument(
                symbol="CNDX", role="primary", weight_within_class_pct=100.0,
                domicile="IE")])],
        glide=[GlideWaypoint(quarter=0, date=date(2026, 1, 1),
               composition_pct_by_class={"Growth": 100.0})],
    )


def _detect(snapshot, *, overage_ratio=DEFAULT_OVERAGE_RATIO, doc=None, holdings=None):
    """Test helper: call _detect_from_snapshot with a canonical doc + as_of so the
    long-term proposals are plan-bound (the production contract)."""
    return _detect_from_snapshot(
        snapshot, doc=doc if doc is not None else _canonical_doc(),
        holdings=holdings or {}, as_of=date(2026, 6, 1),
        overage_ratio=overage_ratio)


def _snap(
    *,
    cash_current_k: float,
    cash_target_k: float,
    growth_current_k: float = 100,
    growth_target_k: float = 200,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source_path="test://",
        snapshot_date=date(2026, 5, 29),
        fx_usd_nis=3.7,
        allocations=[
            AllocationRow(
                category="Cash",
                pct=cash_current_k / (cash_current_k + growth_current_k) * 100,
                usd_value_k=cash_current_k,
                target_pct=cash_target_k / (cash_target_k + growth_target_k) * 100,
                target_k=cash_target_k,
                delta_k=cash_target_k - cash_current_k,
            ),
            AllocationRow(
                category="Growth",
                pct=growth_current_k / (cash_current_k + growth_current_k) * 100,
                usd_value_k=growth_current_k,
                target_pct=growth_target_k / (cash_target_k + growth_target_k) * 100,
                target_k=growth_target_k,
                delta_k=growth_target_k - growth_current_k,
            ),
        ],
    )


class TestThresholdGating:
    def test_below_threshold_returns_none(self):
        """Current cash within 1.5x of target -> no event."""
        snap = _snap(cash_current_k=10, cash_target_k=10)
        assert _detect(snap) is None

    def test_just_below_threshold_returns_none(self):
        snap = _snap(cash_current_k=14, cash_target_k=10)  # 1.4x
        assert _detect(snap) is None

    def test_above_threshold_fires(self):
        """Current cash > target * 1.5 -> event fires."""
        snap = _snap(cash_current_k=20, cash_target_k=10)  # 2.0x
        event = _detect(snap)
        assert event is not None
        assert event.current_cash_k_usd == 20.0
        assert event.target_cash_k_usd == 10.0
        assert event.overage_ratio == 2.0
        assert event.excess_usd == 10_000.0  # ($20K - $10K) = $10K excess

    def test_custom_overage_ratio(self):
        """Tighter ratio = fires sooner."""
        snap = _snap(cash_current_k=11, cash_target_k=10)  # 1.1x
        assert _detect(snap, overage_ratio=1.5) is None
        e = _detect(snap, overage_ratio=1.05)
        assert e is not None
        assert e.excess_usd == pytest.approx(1000.0)


class TestProposalsShape:
    def test_proposals_target_under_target_classes(self):
        """The excess flows to growth (under-target) when growth has room."""
        snap = _snap(
            cash_current_k=50, cash_target_k=10,
            growth_current_k=100, growth_target_k=200,
        )
        event = _detect(snap)
        assert event is not None
        assert event.excess_usd == 40_000
        # At least one proposal in Growth (under target by $100K).
        growth_proposals = [p for p in event.proposals if p.asset_class == "Growth"]
        assert len(growth_proposals) > 0
        total_proposed = sum(p.amount_usd for p in event.proposals)
        # 100% of excess goes to long-term (no medium/short split for
        # the unallocated-cash flow).
        assert total_proposed == pytest.approx(40_000, rel=1e-3)

    def test_headline_describes_ratio(self):
        snap = _snap(cash_current_k=30, cash_target_k=10)
        event = _detect(snap)
        assert event is not None
        assert "3.0x" in event.headline


class TestMissingData:
    def test_no_allocations_returns_none(self):
        snap = PortfolioSnapshot(source_path="x", allocations=[])
        assert _detect(snap) is None

    def test_no_cash_row_returns_none(self):
        snap = PortfolioSnapshot(
            source_path="x",
            allocations=[
                AllocationRow(category="Growth", target_k=100, usd_value_k=50),
            ],
        )
        assert _detect(snap) is None

    def test_zero_target_cash_returns_none(self):
        """Defensive: zero or null target cash isn't actionable."""
        snap = PortfolioSnapshot(
            source_path="x",
            allocations=[
                AllocationRow(category="Cash", target_k=0, usd_value_k=50),
                AllocationRow(category="Growth", target_k=100, usd_value_k=50, target_pct=50.0),
            ],
        )
        assert _detect(snap) is None


class TestCashRowDetection:
    def test_literal_cash(self):
        rows = [AllocationRow(category="Cash", target_k=10)]
        assert _find_cash_row(rows) is not None

    def test_cash_substring(self):
        rows = [AllocationRow(category="Cash & ST bonds", target_k=10)]
        assert _find_cash_row(rows) is not None

    def test_no_match(self):
        rows = [AllocationRow(category="Growth", target_k=10)]
        assert _find_cash_row(rows) is None


# ---------------------------------------------------------------------------
# Staleness guard + API route tests (codex zigzag (b)#I1 + #I2)
# ---------------------------------------------------------------------------


class TestStalenessGuard:
    """detect_unallocated_cash_overage must not fire on stale snapshots."""

    def test_fresh_snapshot_fires(self, argosy_home_db):
        """A snapshot dated today produces an event when overage exists."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import json as _json
        from argosy.state.models import PortfolioSnapshotRow
        from argosy.services.unallocated_cash_detector import (
            detect_unallocated_cash_overage,
        )
        from argosy.config import get_settings

        engine = create_engine(f"sqlite:///{get_settings().db_file}")
        SessionLocal = sessionmaker(bind=engine)
        sess = SessionLocal()
        try:
            today = date(2026, 5, 29)
            row = PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=today,
                imported_at=datetime.now(timezone.utc),
                source_path="x",
                positions_json="[]",
                allocations_json=_json.dumps([
                    {"category": "Cash", "pct": 80.0, "usd_value_k": 100,
                     "target_pct": 30.0, "target_k": 50, "delta_k": -50},
                    {"category": "Growth", "pct": 20.0, "usd_value_k": 50,
                     "target_pct": 70.0, "target_k": 100, "delta_k": 50},
                ]),
                nvda_sales_json="[]", real_estate_json="[]",
                pensions_json="[]", totals_json="{}",
                parse_warnings_json="[]",
            )
            sess.add(row)
            sess.commit()
            event = detect_unallocated_cash_overage(
                sess, user_id="ariel", today=today,
            )
            assert event is not None
            assert event.excess_usd == 50_000.0
        finally:
            sess.close()

    def test_stale_snapshot_returns_none(self, argosy_home_db):
        """A snapshot older than DEFAULT_STALENESS_DAYS returns None."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import json as _json
        from argosy.state.models import PortfolioSnapshotRow
        from argosy.services.unallocated_cash_detector import (
            detect_unallocated_cash_overage,
        )
        from argosy.config import get_settings

        engine = create_engine(f"sqlite:///{get_settings().db_file}")
        SessionLocal = sessionmaker(bind=engine)
        sess = SessionLocal()
        try:
            today = date(2026, 5, 29)
            ancient = date(2026, 1, 15)  # 4+ months old
            row = PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=ancient,
                imported_at=datetime.now(timezone.utc),
                source_path="x",
                positions_json="[]",
                allocations_json=_json.dumps([
                    {"category": "Cash", "pct": 80.0, "usd_value_k": 100,
                     "target_pct": 30.0, "target_k": 50, "delta_k": -50},
                    {"category": "Growth", "pct": 20.0, "usd_value_k": 50,
                     "target_pct": 70.0, "target_k": 100, "delta_k": 50},
                ]),
                nvda_sales_json="[]", real_estate_json="[]",
                pensions_json="[]", totals_json="{}",
                parse_warnings_json="[]",
            )
            sess.add(row)
            sess.commit()
            event = detect_unallocated_cash_overage(
                sess, user_id="ariel", today=today,
            )
            assert event is None, (
                "Stale snapshot (>45d old) should not fire the detector"
            )
        finally:
            sess.close()


class TestAPIRoute:
    """Route-level integration test for /api/portfolio/unallocated-cash-proposal.

    Codex zigzag (b)#I2: the route does `UnallocatedCashProposalDTO(**payload)`
    which would raise ValidationError → 500 if event.to_dict() drifts.
    """

    def test_route_returns_null_when_no_snapshot(self, client_with_db):
        resp = client_with_db.get(
            "/api/portfolio/unallocated-cash-proposal?user_id=ariel",
        )
        assert resp.status_code == 200
        assert resp.json() is None

    def test_route_returns_proposal_when_overage(self, client_with_db):
        import json as _json
        from argosy.state.models import PlanVersion, PortfolioSnapshotRow, User
        sess = client_with_db.app.state.session_factory()
        try:
            if sess.get(User, "ariel") is None:
                sess.add(User(id="ariel", plan="free"))
            # Seed a current canonical plan so the long-term buy list is
            # plan-bound (CNDX from the doc — not a hardcoded class map).
            sess.add(PlanVersion(
                user_id="ariel", role="current", version_label="t",
                target_allocation_json=_canonical_doc().model_dump_json(),
            ))
            today = datetime.now(timezone.utc).date()
            row = PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=today,
                imported_at=datetime.now(timezone.utc),
                source_path="x",
                positions_json="[]",
                allocations_json=_json.dumps([
                    {"category": "Cash", "pct": 80.0, "usd_value_k": 100,
                     "target_pct": 30.0, "target_k": 50, "delta_k": -50},
                    {"category": "Growth", "pct": 20.0, "usd_value_k": 50,
                     "target_pct": 70.0, "target_k": 100, "delta_k": 50},
                ]),
                nvda_sales_json="[]", real_estate_json="[]",
                pensions_json="[]", totals_json="{}",
                parse_warnings_json="[]",
            )
            sess.add(row)
            sess.commit()
        finally:
            sess.close()
        resp = client_with_db.get(
            "/api/portfolio/unallocated-cash-proposal?user_id=ariel",
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload is not None
        # DTO shape contract
        assert payload["excess_usd"] == 50_000.0
        assert payload["overage_ratio"] == 2.0
        assert payload["snapshot_date"] == today.isoformat()
        assert len(payload["proposals"]) > 0
        # Plan-bound: the buy list comes from the canonical doc's instruments.
        assert {p["instrument"] for p in payload["proposals"]} == {"CNDX"}
        assert "allocation_delta_table" in payload
