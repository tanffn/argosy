"""Sprint #2 commit #12 — RSU pre-vest planner service tests.

Covers:
  * Empty user → empty outlook (no historical vests → nothing to project).
  * One historical vest → MAX_PROJECTED_VESTS_PER_GRANT projected vests.
  * Three-scenario tax math (nominal / effective / conservative).
  * Allocation preview included when an allocation table is available.
  * Allocation preview is empty when no portfolio snapshot exists.
  * horizon_days caps the projection count.
  * Multi-grant projection: distinct grants project independently.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.rsu_prevest_planner import (
    DEFAULT_CONSERVATIVE_FLOOR,
    DEFAULT_EFFECTIVE_RATE,
    DEFAULT_NOMINAL_RATE,
    MAX_PROJECTED_VESTS_PER_GRANT,
    compute_upcoming_vest_outlook,
)
from argosy.state.models import (
    Base,
    PlanVersion,
    PortfolioSnapshotRow,
    RsuVestEvent,
    User,
)


def _canonical_doc():
    """Canonical doc: Growth→CNDX. The allocation preview is sized off THIS doc
    (via cash_only_deploy), not the snapshot's TSV allocation table."""
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


def _seed_plan(session, *, user_id: str = "ariel") -> None:
    """Seed a current canonical plan so the allocation preview is plan-bound."""
    session.add(PlanVersion(
        user_id=user_id, role="current", version_label="t",
        target_allocation_json=_canonical_doc().model_dump_json(),
    ))
    session.commit()


@pytest.fixture
def db_session(tmp_path):
    """Self-contained SQLite + seeded user 'ariel'."""
    db_path = tmp_path / "rsu_prevest.db"
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


def _seed_vest(
    session,
    *,
    user_id: str = "ariel",
    grant_id: str = "G1",
    vest_date: date,
    shares: float = 100.0,
    fmv: float = 150.0,
) -> None:
    """Add an RsuVestEvent row."""
    session.add(RsuVestEvent(
        user_id=user_id,
        symbol="NVDA",
        grant_id=grant_id,
        vest_date=vest_date,
        shares_vested=Decimal(str(shares)),
        shares_withheld=Decimal("0"),
        shares_net=Decimal(str(shares)),
        fmv_per_share_usd=Decimal(str(fmv)),
        award_date=None,
        source_file="test_seed",
    ))
    session.commit()


def _seed_snapshot(
    session,
    *,
    user_id: str = "ariel",
    nvda_price: float | None = 145.0,
    with_allocations: bool = True,
) -> None:
    """Add a portfolio snapshot with a single NVDA position + optional
    allocation block.
    """
    positions = []
    if nvda_price is not None:
        positions.append({
            "symbol": "NVDA",
            "current_price": nvda_price,
        })
    allocations = []
    if with_allocations:
        allocations = [
            {
                "category": "Cash",
                "pct": 10.0,
                "usd_value_k": 50.0,
                "target_pct": 5.0,
                "target_k": 25.0,
                "delta_k": -25.0,
            },
            {
                "category": "Growth",
                "pct": 30.0,
                "usd_value_k": 150.0,
                "target_pct": 50.0,
                "target_k": 250.0,
                "delta_k": 100.0,
            },
            {
                "category": "Core Equity",
                "pct": 30.0,
                "usd_value_k": 150.0,
                "target_pct": 45.0,
                "target_k": 225.0,
                "delta_k": 75.0,
            },
        ]
    session.add(PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=date(2026, 5, 28),
        imported_at=datetime.now(timezone.utc),
        source_path="test://snap.tsv",
        fx_usd_nis=3.7,
        fx_usd_eur=None,
        positions_json=json.dumps(positions),
        real_estate_json="[]",
        allocations_json=json.dumps(allocations),
        nvda_sales_json="[]",
        pensions_json="[]",
        parse_warnings_json="[]",
        totals_json="{}",
    ))
    session.commit()


class TestEmptyUser:
    def test_no_vests_returns_empty_outlook(self, db_session):
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365,
            as_of=date(2026, 5, 29),
        )
        assert out.upcoming == []
        assert out.user_id == "ariel"
        assert out.horizon_days == 365
        # Rates still computed at the outlook level so the UI footnote
        # has something to render even on an empty outlook.
        assert out.rate_nominal == DEFAULT_NOMINAL_RATE
        assert out.rate_effective == DEFAULT_EFFECTIVE_RATE
        assert out.rate_conservative == max(
            DEFAULT_CONSERVATIVE_FLOOR, DEFAULT_NOMINAL_RATE + 0.05,
        )


class TestProjection:
    def test_one_grant_projects_up_to_max(self, db_session):
        """One historical vest → MAX_PROJECTED_VESTS_PER_GRANT projected
        (within a long enough horizon).
        """
        _seed_vest(
            db_session,
            grant_id="G1",
            vest_date=date(2026, 3, 1),
            shares=100.0,
            fmv=150.0,
        )
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365 * 2,
            as_of=date(2026, 5, 29),
        )
        assert len(out.upcoming) == MAX_PROJECTED_VESTS_PER_GRANT
        # Ascending order.
        dates = [u.expected_vest_date for u in out.upcoming]
        assert dates == sorted(dates)
        # First projected vest is strictly after as_of; latest hist was
        # 2026-03-01, +90d=2026-05-30, which is after as_of=2026-05-29.
        assert out.upcoming[0].expected_vest_date == date(2026, 5, 30)
        # Subsequent at +90d each.
        assert out.upcoming[1].expected_vest_date == date(2026, 8, 28)

    def test_horizon_caps_projection(self, db_session):
        """30-day horizon should yield at most 1 projected vest."""
        _seed_vest(
            db_session,
            grant_id="G1",
            vest_date=date(2026, 5, 15),
            shares=100.0,
            fmv=150.0,
        )
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=30,
            as_of=date(2026, 5, 29),
        )
        # Latest=2026-05-15 +90d=2026-08-13 — well past as_of+30d=2026-06-28.
        # So no projection inside the horizon.
        assert out.upcoming == []

    def test_horizon_just_inside_first_projection(self, db_session):
        """Horizon picks up exactly one projection when set just past it."""
        _seed_vest(
            db_session,
            grant_id="G1",
            vest_date=date(2026, 3, 1),
            shares=100.0,
            fmv=150.0,
        )
        # +90d from 2026-03-01 = 2026-05-30. as_of = 2026-05-29, horizon=2
        # → window ends 2026-05-31. Exactly one projected vest.
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=2,
            as_of=date(2026, 5, 29),
        )
        assert len(out.upcoming) == 1
        assert out.upcoming[0].expected_vest_date == date(2026, 5, 30)

    def test_multi_grant_projects_independently(self, db_session):
        """Two grants → each projects forward up to MAX."""
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 1), shares=100, fmv=150,
        )
        _seed_vest(
            db_session, grant_id="G2",
            vest_date=date(2026, 4, 1), shares=50, fmv=150,
        )
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365 * 2,
            as_of=date(2026, 5, 29),
        )
        # 4 per grant × 2 grants = 8 projections.
        assert len(out.upcoming) == MAX_PROJECTED_VESTS_PER_GRANT * 2
        grants = {u.grant_id for u in out.upcoming}
        assert grants == {"G1", "G2"}
        # G2's per-tranche shares should reflect G2's latest vest.
        g2 = [u for u in out.upcoming if u.grant_id == "G2"]
        assert all(u.shares_projected == 50.0 for u in g2)


class TestTaxScenarios:
    def test_three_scenario_math(self, db_session):
        """Math: gross = shares × price; post_tax = gross × (1 - rate).
        Conservative floor caps at max(0.47, nominal + 0.05).
        """
        _seed_vest(
            db_session,
            grant_id="G1",
            vest_date=date(2026, 3, 1),
            shares=100.0,
            fmv=200.0,
        )
        # No portfolio snapshot → FMV fallback.
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365,
            as_of=date(2026, 5, 29),
        )
        assert len(out.upcoming) > 0
        v = out.upcoming[0]
        # No snapshot → price falls back to FMV = 200.
        assert v.nvda_price_usd == 200.0
        assert v.expected_gross_usd == pytest.approx(100.0 * 200.0)
        # Nominal scenario.
        assert v.rate_nominal == DEFAULT_NOMINAL_RATE
        assert v.expected_post_tax_nominal_usd == pytest.approx(
            v.expected_gross_usd * (1.0 - DEFAULT_NOMINAL_RATE)
        )
        # Effective scenario.
        assert v.rate_effective == DEFAULT_EFFECTIVE_RATE
        assert v.expected_post_tax_effective_usd == pytest.approx(
            v.expected_gross_usd * (1.0 - DEFAULT_EFFECTIVE_RATE)
        )
        # Conservative = max(0.47, 0.42 + 0.05) = 0.47.
        expected_cons = max(
            DEFAULT_CONSERVATIVE_FLOOR, DEFAULT_NOMINAL_RATE + 0.05,
        )
        assert v.rate_conservative == expected_cons
        assert v.expected_post_tax_conservative_usd == pytest.approx(
            v.expected_gross_usd * (1.0 - expected_cons)
        )
        # Conservative must be <= the other two post-tax amounts (it's
        # the worst case).
        assert (
            v.expected_post_tax_conservative_usd
            <= v.expected_post_tax_nominal_usd
        )
        assert (
            v.expected_post_tax_conservative_usd
            <= v.expected_post_tax_effective_usd
        )


class TestAllocationPreview:
    def test_preview_present_when_plan_accepted(self, db_session):
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 1), shares=100, fmv=200,
        )
        _seed_snapshot(db_session, nvda_price=180.0)
        _seed_plan(db_session)
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365,
            as_of=date(2026, 5, 29),
        )
        v = out.upcoming[0]
        # Snapshot supplied a spot price → that wins over FMV fallback.
        assert v.nvda_price_usd == 180.0
        assert v.expected_gross_usd == pytest.approx(100 * 180)
        # Preview is plan-bound: instruments come from the canonical doc (CNDX),
        # NOT a hardcoded class→ticker map.
        assert len(v.allocation_preview) > 0
        assert {p.instrument for p in v.allocation_preview} == {"CNDX"}
        # Total allocation should equal the nominal post-tax amount (100% budget,
        # empty book → all of it deploys to the single canonical instrument).
        total = sum(p.amount_usd for p in v.allocation_preview)
        assert total == pytest.approx(v.expected_post_tax_nominal_usd, rel=1e-3)
        assert total > 0.0

    def test_preview_empty_when_no_plan_accepted(self, db_session):
        """No accepted plan → honest empty preview, never a hardcoded fallback."""
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 1), shares=100, fmv=200,
        )
        _seed_snapshot(db_session, nvda_price=180.0)
        # No plan seeded.
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365,
            as_of=date(2026, 5, 29),
        )
        v = out.upcoming[0]
        assert v.allocation_preview == []

    def test_preview_empty_when_no_snapshot(self, db_session):
        """No snapshot AND no plan → empty preview."""
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 1), shares=100, fmv=200,
        )
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365,
            as_of=date(2026, 5, 29),
        )
        v = out.upcoming[0]
        assert v.allocation_preview == []


class TestSerialization:
    def test_to_dict_round_trip(self, db_session):
        """``to_dict`` produces a JSON-serializable shape."""
        _seed_vest(
            db_session, grant_id="G1",
            vest_date=date(2026, 3, 1), shares=10, fmv=200,
        )
        out = compute_upcoming_vest_outlook(
            db_session, "ariel",
            horizon_days=365,
            as_of=date(2026, 5, 29),
        )
        d = out.to_dict()
        # Round-trips through json without exception.
        as_json = json.loads(json.dumps(d))
        assert as_json["user_id"] == "ariel"
        assert len(as_json["upcoming"]) > 0
        first = as_json["upcoming"][0]
        for key in (
            "grant_id",
            "expected_vest_date",
            "days_until",
            "shares_projected",
            "nvda_price_usd",
            "expected_gross_usd",
            "rate_nominal",
            "rate_effective",
            "rate_conservative",
            "expected_post_tax_nominal_usd",
            "expected_post_tax_effective_usd",
            "expected_post_tax_conservative_usd",
            "allocation_preview",
        ):
            assert key in first, f"missing key {key!r} in upcoming dict"
