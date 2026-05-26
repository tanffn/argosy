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


def test_target_zero_when_no_plan(session_with_user):
    """No draft + no current plan → 0 (UI renders neutral badge)."""
    from argosy.services import nvda_sales_history

    n = nvda_sales_history.compute_nvda_target_shares_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 0


def test_target_prorates_annual_from_horizon_medium(session_with_user):
    """Annual NVDA-sale target from horizon_medium_json prorates by days."""
    import json

    from argosy.services import nvda_sales_history

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


def test_target_unit_must_be_shares(session_with_user):
    """A NVDA target with unit='pct_of_portfolio' must NOT be treated as
    a share-count target (avoids reading the 45% cap as 45 shares)."""
    import json

    from argosy.services import nvda_sales_history

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
