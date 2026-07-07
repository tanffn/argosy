"""Tests for argosy.services.nvda_sales_history.

The helper feeds ``Phase1Inputs.nvda_shares_sold_ytd`` /
``nvda_target_shares_ytd``, which in turn feed
``ConcentrationAnalystAgent``'s NVDA pace block. Before this wiring,
those fields were declared on the dataclass but never populated —
synthesis emitted ``shares_sold_ytd=0`` in every report.

Required cases (per the bug brief):

  (a) no fills, no TSV   → 0
  (b) fills before Jan 1 → not counted
  (c) sell fills         → counted (negative qty AND SELL action)
  (d) buy fills          → not counted
  (e) idempotent         → two calls return the same number
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, Fill, PlanVersion, User


def _make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


@pytest.fixture
def session_with_user():
    s = _make_session()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


# ----------------------------------------------------------------------
# compute_nvda_shares_sold_ytd
# ----------------------------------------------------------------------


def test_no_fills_returns_zero(session_with_user, monkeypatch, tmp_path):
    """(a) Empty fills + no TSV available → 0."""
    from argosy.services import nvda_sales_history

    # Stub the TSV fallback to "no TSV reachable" so we exercise the
    # fills-only path. ARGOSY_HOME points at a clean tmpdir for belt-and-braces.
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 0


def test_fills_before_jan1_not_counted(session_with_user, monkeypatch, tmp_path):
    """(b) A NVDA sell stamped 2025-12-31 must NOT count against 2026 YTD."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    # Last-minute 2025 sale — shouldn't appear in 2026 YTD.
    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="x1",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("100"),
            price=Decimal("180"),
            commission=Decimal("0"),
            filled_at=datetime(2025, 12, 31, 18, 30, tzinfo=timezone.utc),
            paper=False,
        )
    )
    # Same-ticker sell INSIDE the window — must be counted, proves the
    # cutoff is correct rather than the function returning 0 outright.
    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="x2",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("250"),
            price=Decimal("199"),
            commission=Decimal("0"),
            filled_at=datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 250, f"expected only the 2026 fill to count, got {n}"


def test_sell_fills_negative_quantity_counted(
    session_with_user, monkeypatch, tmp_path,
):
    """(c) Sell fills count under BOTH the SELL-action and negative-qty conventions."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    # SELL action with positive quantity (Schwab CSV convention).
    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="a",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("520"),
            price=Decimal("199"),
            commission=Decimal("0"),
            filled_at=datetime(2026, 4, 14, 10, tzinfo=timezone.utc),
            paper=False,
        )
    )
    # Negative quantity, ambiguous action (some IBKR exports do this).
    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="ibkr",
            broker_order_id="b",
            ticker="NVDA",
            action="",
            quantity=Decimal("-560"),
            price=Decimal("191"),
            commission=Decimal("0"),
            filled_at=datetime(2026, 1, 21, 10, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 520 + 560


def test_buy_fills_not_counted(session_with_user, monkeypatch, tmp_path):
    """(d) BUY action + positive qty NEVER counts as a sale."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="b1",
            ticker="NVDA",
            action="BUY",
            quantity=Decimal("400"),
            price=Decimal("180"),
            commission=Decimal("0"),
            filled_at=datetime(2026, 2, 5, 10, tzinfo=timezone.utc),
            paper=False,
        )
    )
    # And one sell so we know the function isn't bailing on "any data".
    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="s1",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("250"),
            price=Decimal("199"),
            commission=Decimal("0"),
            filled_at=datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 250, f"buy must not count toward sales; got {n}"


def test_compute_is_idempotent(session_with_user, monkeypatch, tmp_path):
    """(e) Two calls produce identical answers (no state mutation)."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    session_with_user.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="i1",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("500"),
            price=Decimal("199"),
            commission=Decimal("0"),
            filled_at=datetime(2026, 2, 14, 14, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    n1 = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    n2 = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n1 == n2 == 500


def test_tsv_fallback_when_fills_empty(
    session_with_user, monkeypatch, tmp_path,
):
    """Fills empty → TSV ``nvda_sales`` block becomes the source.

    This guards the live-DB case: ``fills`` table is empty in prod today,
    so without the fallback ConcentrationAnalyst would still get 0. The
    parser already exposes ``nvda_sales`` rows from the Family Finances
    Status TSV — we just need to follow the same code path that the
    /api/plan/draft/nvda-trajectory endpoint uses.
    """
    from argosy.services import nvda_sales_history

    class _FakeSale:
        def __init__(self, month: str, shares: int) -> None:
            self.month = month
            self.shares = shares
            self.price = None

    class _FakeSnapshot:
        snapshot_date = date(2026, 5, 26)
        nvda_sales = [
            _FakeSale("Jan", 560),
            _FakeSale("Feb", 520),
            _FakeSale("Apr", 520),
            _FakeSale("Apr", 520),  # duplicate — must dedup
        ]
        positions: list = []

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    fake_tsv = tmp_path / "fake.tsv"
    fake_tsv.write_text("placeholder")
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: fake_tsv,
    )
    import argosy.ingest.tsv as tsv_mod

    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _FakeSnapshot()
    )

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    # 560 (Jan) + 520 (Feb) + 520 (Apr) — Apr duplicate dropped.
    assert n == 1600


def test_tsv_fallback_excludes_months_past_as_of(
    session_with_user, monkeypatch, tmp_path,
):
    """A sale logged for December must NOT count when as_of is in May."""
    from argosy.services import nvda_sales_history

    class _FakeSale:
        def __init__(self, month: str, shares: int) -> None:
            self.month = month
            self.shares = shares
            self.price = None

    class _FakeSnapshot:
        snapshot_date = date(2026, 5, 26)
        nvda_sales = [
            _FakeSale("Jan", 100),
            _FakeSale("Dec", 9999),  # future month — must not count yet
        ]
        positions: list = []

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    fake_tsv = tmp_path / "fake.tsv"
    fake_tsv.write_text("placeholder")
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: fake_tsv,
    )
    import argosy.ingest.tsv as tsv_mod

    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _FakeSnapshot()
    )

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 100


# ----------------------------------------------------------------------
# compute_nvda_target_shares_ytd
# ----------------------------------------------------------------------


def test_target_zero_when_no_plan(session_with_user, monkeypatch, tmp_path):
    """No draft + no current plan → 0 (UI renders neutral badge)."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    n = nvda_sales_history.compute_nvda_target_shares_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 0


# ----------------------------------------------------------------------
# compute_nvda_sale_pace — the ONE canonical glide-derived derivation
# ----------------------------------------------------------------------


def _glide_doc_json(*, w_start: float = 60.0, w_end: float = 8.0) -> str:
    """Minimal TargetAllocationDoc with an NVDA class + 4-quarter glide.

    Includes a DECOY class whose label also mentions NVDA
    ("...ex-NVDA-dense") — the pace must identify the NVDA class by its
    INSTRUMENTS, never by label substring.
    """
    import json

    steps = 4
    glide = []
    dates = ["2026-07-06", "2026-10-06", "2027-01-06", "2027-04-06", "2027-07-06"]
    for q, d in enumerate(dates):
        w = w_start + (w_end - w_start) * (q / steps)
        glide.append({
            "quarter": q,
            "date": d,
            "composition_pct_by_class": {
                "Strategic single-stock (NVDA)": round(w, 4),
                "Global quality growth (ex-NVDA-dense)": round(11.0 - w / 60.0, 4),
            },
        })
    return json.dumps({
        "schema_version": 1,
        "basis": "full tradeable book",
        "anchor_sigma": 0.2,
        "blended_sigma": 0.2,
        "nvda_cap_pct": 13.0,
        "fi_pct": 8.0,
        "provenance": "test",
        "classes": [
            {
                "label": "Strategic single-stock (NVDA)",
                "snapshot_category": "Individual Stocks",
                "sigma_class": "nvda_single",
                "target_pct": 8.0,
                "instruments": [
                    {"symbol": "NVDA", "role": "hold",
                     "weight_within_class_pct": 100.0},
                ],
            },
            {
                "label": "Global quality growth (ex-NVDA-dense)",
                "snapshot_category": "Growth",
                "sigma_class": "growth",
                "target_pct": 11.0,
                "instruments": [
                    {"symbol": "IWQU", "role": "primary",
                     "weight_within_class_pct": 100.0},
                ],
            },
        ],
        "glide": glide,
    })


def _seed_pace_plan_and_snapshot(
    session, monkeypatch, tmp_path, *, nvda_shares_now: float,
    horizon_medium_json: str | None = None,
) -> None:
    """Draft plan with a glide doc + latest snapshot holding NVDA + a
    month-granular sales block (Apr sale — BEFORE the July plan start)."""
    from argosy.ingest.tsv import NVDASale, PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )
    session.add(
        PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="glide-draft",
            horizon_medium_json=horizon_medium_json,
            target_allocation_json=_glide_doc_json(),
        )
    )
    persist_snapshot(
        session,
        user_id="ariel",
        snapshot=PortfolioSnapshot(
            source_path="test",
            snapshot_date=date(2026, 7, 6),
            fx_usd_nis=3.0,
            fx_usd_eur=0.85,
            positions=[
                PortfolioPosition(
                    location="schwab", currency="USD", asset_type="NVIDIA",
                    details="RSU", symbol="NVDA", shares=nvda_shares_now,
                    current_price=193.0,
                    current_value_local=nvda_shares_now * 193.0,
                    usd_value_k=nvda_shares_now * 0.193,
                ),
            ],
            nvda_sales=[NVDASale(month="Apr", shares=520, price=199.56)],
        ),
    )
    session.commit()


def test_pace_glide_plan_relative_day_one(
    session_with_user, monkeypatch, tmp_path,
):
    """Day 1-2 of the plan year: the target is ~1 day of flow (tens of
    shares), sold-since-plan-start is 0, and the pace reads ON TRACK —
    never 'behind by thousands' (the calendar-YTD artifact this replaces).
    The Apr sale counts toward the calendar context figure only."""
    from argosy.services import nvda_sales_history

    _seed_pace_plan_and_snapshot(
        session_with_user, monkeypatch, tmp_path, nvda_shares_now=11_471.0,
    )
    pace = nvda_sales_history.compute_nvda_sale_pace(
        session_with_user, "ariel", as_of=date(2026, 7, 7),
    )
    assert pace.basis == "glide"
    assert pace.plan_start == date(2026, 7, 6)
    # Annual flow = held_start * (1 - 8/60) ≈ 9,941 — the glide's implied
    # plan-year sale, NOT the stale 12%-sleeve 9,270 row.
    assert pace.annual_flow == pytest.approx(11_471 * (1 - 8.0 / 60.0), abs=1)
    # Day-1 pro-rated target: about one day of pace, tiny vs the annual.
    assert 0 <= pace.target_shares <= 60
    assert pace.sold_shares == 0  # Apr sale is BEFORE the plan year
    assert pace.sold_calendar_ytd == 520  # calendar context preserved
    assert pace.status == "on"
    assert pace.on_track


def test_pace_glide_midyear_proration_and_sold_window(
    session_with_user, monkeypatch, tmp_path,
):
    """At the Q1 waypoint the target equals the waypoint's implied share
    delta; sales inside the plan year count via the fills ledger."""
    from argosy.services import nvda_sales_history

    # 2,400 sold since plan start → held now 9,071; held_at_start = 11,471.
    _seed_pace_plan_and_snapshot(
        session_with_user, monkeypatch, tmp_path, nvda_shares_now=9_071.0,
    )
    session_with_user.add(
        Fill(
            user_id="ariel", broker="schwab", broker_order_id="g1",
            ticker="NVDA", action="SELL", quantity=Decimal("2400"),
            price=Decimal("190"), commission=Decimal("0"),
            filled_at=datetime(2026, 8, 15, 10, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    pace = nvda_sales_history.compute_nvda_sale_pace(
        session_with_user, "ariel", as_of=date(2026, 10, 6),
    )
    assert pace.basis == "glide"
    assert pace.sold_shares == 2400
    # Q1 waypoint weight = 47.0 → target = 11,471 * (1 - 47/60) ≈ 2,485.
    assert pace.target_shares == pytest.approx(
        11_471 * (1 - 47.0 / 60.0), abs=2,
    )
    # 2,400 vs 2,485 is within the ±5%-of-annual band → on track.
    assert pace.status == "on"
    assert pace.on_track


def test_pace_glide_wins_over_stale_horizon_row(
    session_with_user, monkeypatch, tmp_path,
):
    """The live shape: the medium horizon still carries the old 12%-sleeve
    9,270-share row, but the glide doc is canonical — the wrapper must NOT
    pro-rate 9,270 over the CALENDAR year (≈4,750 by July)."""
    import json

    from argosy.services import nvda_sales_history

    horizon = json.dumps({
        "targets": [
            {"label": "NVDA shares to sell to reach the 12% IPS sleeve",
             "value": 9270.0, "unit": "shares"},
        ],
    })
    _seed_pace_plan_and_snapshot(
        session_with_user, monkeypatch, tmp_path,
        nvda_shares_now=11_471.0, horizon_medium_json=horizon,
    )
    n = nvda_sales_history.compute_nvda_target_shares_ytd(
        session_with_user, "ariel", as_of=date(2026, 7, 7),
    )
    assert n <= 60, f"glide-derived plan-relative target expected, got {n}"


def test_pace_horizon_fallback_without_glide_doc(
    session_with_user, monkeypatch, tmp_path,
):
    """No target_allocation_json → the legacy medium-horizon calendar
    proration still works (basis='horizon')."""
    import json

    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )
    horizon = json.dumps({
        "targets": [
            {"label": "NVDA deconcentration shares to sell (next 12 months)",
             "value": 1440.0, "unit": "shares"},
        ],
    })
    session_with_user.add(
        PlanVersion(
            user_id="ariel", role="draft", version_label="no-glide",
            horizon_medium_json=horizon,
        )
    )
    session_with_user.commit()
    pace = nvda_sales_history.compute_nvda_sale_pace(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert pace.basis == "horizon"
    assert 560 <= pace.target_shares <= 600


def test_target_prorates_annual_from_horizon_medium(
    session_with_user, monkeypatch, tmp_path,
):
    """Annual NVDA-sale target from horizon_medium_json prorates by days."""
    import json

    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    # 1,440 shares/12 months — matches run #9's actual draft.
    horizon = json.dumps({
        "targets": [
            {
                "label": "NVDA deconcentration shares to sell (next 12 months)",
                "value": 1440.0,
                "unit": "shares",
            },
            {
                "label": "Other unrelated target",
                "value": 999.0,
                "unit": "pct_of_portfolio",
            },
        ],
    })
    session_with_user.add(
        PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t-draft",
            horizon_medium_json=horizon,
        )
    )
    session_with_user.commit()

    # May 26 (anchor used in the live DB): day_of_year ≈ 146 → 1440 * 146/365 ≈ 575.
    n = nvda_sales_history.compute_nvda_target_shares_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert 560 <= n <= 600, f"expected ~575, got {n}"


def test_target_unit_must_be_shares(session_with_user, monkeypatch, tmp_path):
    """A NVDA target with unit='pct_of_portfolio' must NOT be treated as
    a share-count target (avoids reading the 45% cap as 45 shares)."""
    import json

    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )

    horizon = json.dumps({
        "targets": [
            {
                "label": "NVDA share of portfolio (12-month target)",
                "value": 45.0,
                "unit": "pct_of_portfolio",
            },
        ],
    })
    session_with_user.add(
        PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t-draft",
            horizon_medium_json=horizon,
        )
    )
    session_with_user.commit()

    n = nvda_sales_history.compute_nvda_target_shares_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 0
