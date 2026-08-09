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


@pytest.fixture(autouse=True)
def _no_real_schwab_root(monkeypatch):
    """The Schwab-CSV branch outranks every other source — keep the dev
    machine's real ``ARGOSY_EXPENSE_SAMPLES_ROOT`` (Google Drive) from
    leaking real sales into these fixtures."""
    monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)


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


def test_book_fallback_when_fills_empty(
    session_with_user, monkeypatch, tmp_path,
):
    """Fills empty → the guarded DB book's ``nvda_sales`` block is the source.

    This guards the live-DB case: ``fills`` table is empty in prod today,
    so without the fallback ConcentrationAnalyst would still get 0. The
    fallback now reads the merged, ingest-guarded book via
    ``load_current_book_snapshot`` instead of re-walking a raw TSV.
    """
    from argosy.services import nvda_sales_history
    from argosy.services import portfolio_snapshot_store as pss

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

    monkeypatch.setattr(
        pss, "load_current_book_snapshot", lambda *a, **k: _FakeSnapshot()
    )

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    # 560 (Jan) + 520 (Feb) + 520 (Apr) — Apr duplicate dropped.
    assert n == 1600


def test_sum_monthly_sales_dedups_identical_tuple_only():
    """Dedup key is the IDENTICAL (month, shares, price) tuple: the
    verbatim 'Apr 520 @ 199.56' repeat collapses, but two GENUINE
    same-size sales in one month at different prices both count."""
    from argosy.services.nvda_sales_history import _sum_monthly_sales

    as_of = date(2026, 7, 7)
    dup = [
        {"month": "Jan", "shares": 560, "price": 191.0},
        {"month": "Feb", "shares": 520, "price": 177.0},
        {"month": "Apr", "shares": 520, "price": 199.56},
        {"month": "Apr", "shares": 520, "price": 199.56},  # verbatim repeat
    ]
    assert _sum_monthly_sales(dup, anchor_year=2026, as_of=as_of) == 1600

    distinct = [
        {"month": "Apr", "shares": 520, "price": 199.56},
        {"month": "Apr", "shares": 520, "price": 188.0},  # different sale
    ]
    assert _sum_monthly_sales(distinct, anchor_year=2026, as_of=as_of) == 1040


def test_book_fallback_excludes_months_past_as_of(
    session_with_user, monkeypatch, tmp_path,
):
    """A sale logged for December must NOT count when as_of is in May."""
    from argosy.services import nvda_sales_history
    from argosy.services import portfolio_snapshot_store as pss

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

    monkeypatch.setattr(
        pss, "load_current_book_snapshot", lambda *a, **k: _FakeSnapshot()
    )

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 100


# ----------------------------------------------------------------------
# Schwab Equity Awards CSV — the binding real-sale source (exact dates)
# ----------------------------------------------------------------------


_SCHWAB_HEADER = (
    "Date,Action,Symbol,Description,Quantity,Type,Shares,SalePrice,"
    "FeesAndCommissions,Amount\n"
)


def _write_schwab_csv(root, year: str, name: str, rows: str) -> None:
    d = root / year / "Schwab"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(_SCHWAB_HEADER + rows, encoding="utf-8")


def test_schwab_csv_wins_over_fills_with_exact_dates(
    session_with_user, monkeypatch, tmp_path,
):
    """The on-disk Schwab CSV outranks fills AND windows on EXACT sale
    dates — a Jul 2 sale is excluded from a May-26 as_of even though a
    month-granular source would have no way to know."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )
    root = tmp_path / "resources"
    _write_schwab_csv(
        root, "2026", "EquityAwardsCenter_Transactions.csv",
        '04/14/2026,Sale,NVDA,Share sale,520,,,,$0.10,"$103,771.20"\n'
        '07/02/2026,Sale,NVDA,Share sale,300,,,,$0.15,"$57,000.00"\n',
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))

    # A decoy fill that must NOT be used once the CSV resolves.
    session_with_user.add(
        Fill(
            user_id="ariel", broker="schwab", broker_order_id="d1",
            ticker="NVDA", action="SELL", quantity=Decimal("999"),
            price=Decimal("190"), commission=Decimal("0"),
            filled_at=datetime(2026, 3, 1, 10, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 520, f"CSV (exact-dated) must win over fills; got {n}"

    # Same year through Jul 7 → the Jul 2 sale now counts.
    n2 = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 7, 7),
    )
    assert n2 == 820


def test_schwab_csv_dedups_across_overlapping_exports(
    session_with_user, monkeypatch, tmp_path,
):
    """Two exports carrying the same sale (overlapping export windows)
    count it once; the ``since`` filter is exact-dated too."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )
    root = tmp_path / "resources"
    sale_rows = (
        '04/14/2026,Sale,NVDA,Share sale,520,,,,$0.10,"$103,771.20"\n'
    )
    _write_schwab_csv(root, "2026", "export_a.csv", sale_rows)
    _write_schwab_csv(
        root, "2026", "export_b.csv",
        sale_rows + '07/10/2026,Sale,NVDA,Share sale,250,,,,$0.10,"$47,500.00"\n',
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 7, 12),
    )
    assert n == 770, f"duplicate sale must collapse; got {n}"

    # Windowed from Jul 6 (plan start): only the Jul 10 sale counts.
    n_since = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 7, 12),
        since=date(2026, 7, 6),
    )
    assert n_since == 250


def test_schwab_csv_absent_falls_back_to_fills(
    session_with_user, monkeypatch, tmp_path,
):
    """No root / no NVDA sale rows → the fills branch still works."""
    from argosy.services import nvda_sales_history

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )
    # Root exists but carries only a non-NVDA sale.
    root = tmp_path / "resources"
    _write_schwab_csv(
        root, "2026", "other.csv",
        '02/01/2026,Sale,AAPL,Share sale,10,,,,$0.10,"$2,000.00"\n',
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))

    session_with_user.add(
        Fill(
            user_id="ariel", broker="schwab", broker_order_id="f1",
            ticker="NVDA", action="SELL", quantity=Decimal("111"),
            price=Decimal("190"), commission=Decimal("0"),
            filled_at=datetime(2026, 3, 1, 10, tzinfo=timezone.utc),
            paper=False,
        )
    )
    session_with_user.commit()

    n = nvda_sales_history.compute_nvda_shares_sold_ytd(
        session_with_user, "ariel", as_of=date(2026, 5, 26),
    )
    assert n == 111


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
    role: str = "draft",
) -> None:
    """Plan (draft by default) with a glide doc + latest snapshot holding
    NVDA + a month-granular sales block (Apr sale — BEFORE the July plan
    start)."""
    from argosy.ingest.tsv import NVDASale, PortfolioPosition, PortfolioSnapshot
    from argosy.services.portfolio_snapshot_store import persist_snapshot

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "argosy.api.routes.portfolio._find_latest_tsv", lambda: None,
    )
    session.add(
        PlanVersion(
            user_id="ariel",
            role=role,
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


def test_pace_glide_tax_year_quota_day_one(
    session_with_user, monkeypatch, tmp_path,
):
    """Day 2 of a mid-year plan: the headline is the TAX-YEAR quota
    (Israeli CGT is assessed Jan–Dec), pre-plan sales count toward it,
    and a plan revision never resets the year. Never 'behind by
    thousands' on day 1 (the daily pro-rata artifact this replaces)."""
    from argosy.services import nvda_sales_history

    _seed_pace_plan_and_snapshot(
        session_with_user, monkeypatch, tmp_path, nvda_shares_now=11_471.0,
    )
    pace = nvda_sales_history.compute_nvda_sale_pace(
        session_with_user, "ariel", as_of=date(2026, 7, 7),
    )
    assert pace.basis == "glide"
    assert pace.plan_start == date(2026, 7, 6)
    assert pace.tax_year == 2026
    # Tax-year quota: actual Jan-1 book (11,471 held + 520 sold in Apr)
    # minus the glide's implied Dec-31 holdings. w(Dec 31) interpolates
    # Oct 6 (47%) → Jan 6 (34%): 47 - 13*86/92 ≈ 34.85.
    w_dec31 = 47.0 - 13.0 * 86 / 92
    expected_quota = (11_471 + 520) - 11_471 * w_dec31 / 60.0
    assert pace.annual_flow == pytest.approx(expected_quota, abs=2)
    # Sold counts the CALENDAR year — the pre-plan Apr sale is in.
    assert pace.sold_shares == 520
    assert pace.sold_calendar_ytd == 520
    assert pace.sold_since_plan_start == 0
    # Expected-by-now ≈ pre-plan actual + ~1 day of glide flow → the
    # delta is tiny and well inside the generous band.
    assert abs(pace.delta_shares) <= 60
    assert pace.status == "on"
    assert pace.on_track
    # Next dated glide checkpoint: Oct 6 at ≤47%, implying ~2,485 shares
    # to sell from current holdings (11,471 * 13/60).
    assert pace.next_waypoint_date == date(2026, 10, 6)
    assert pace.next_waypoint_weight_pct == pytest.approx(47.0)
    assert pace.shares_to_sell_by_waypoint == pytest.approx(
        11_471 * 13.0 / 60.0, abs=2,
    )


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
    assert pace.sold_since_plan_start == 2400
    # Q1 waypoint weight = 47.0 → expected sold by now
    # = 11,471 * (1 - 47/60) ≈ 2,485 (glide-implied schedule).
    assert pace.target_shares == pytest.approx(
        11_471 * (1 - 47.0 / 60.0), abs=2,
    )
    # 2,400 vs 2,485 is within the ±10%-of-quota band → on track.
    assert pace.status == "on"
    assert pace.on_track
    # The next checkpoint after Oct 6 is Jan 6 2027 at ≤34%: from the
    # current 9,071 held down to 11,471*34/60 ≈ 6,500 → ~2,571 to sell.
    assert pace.next_waypoint_date == date(2027, 1, 6)
    assert pace.next_waypoint_weight_pct == pytest.approx(34.0)
    assert pace.shares_to_sell_by_waypoint == pytest.approx(
        9_071 - 11_471 * 34.0 / 60.0, abs=2,
    )


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
    # Glide-derived expectation: pre-plan actual (520) + ~1 day of glide
    # flow (~27) ≈ 547 — nowhere near the 9,270-row's ≈4,750 pro-rata.
    assert 500 <= n <= 620, f"glide-derived tax-year expectation, got {n}"


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


# ----------------------------------------------------------------------
# Cross-surface consistency: the home Deconcentration card and the /plan
# NVDA share-trajectory chart must render the ONE canonical doc glide —
# same waypoints in two denominators (weight-% vs shares).
# ----------------------------------------------------------------------


def test_glide_share_path_matches_pace_arithmetic(
    session_with_user, monkeypatch, tmp_path,
):
    """compute_nvda_glide_share_path is the pace derivation in share units:
    shares(waypoint) = held_start * w / w_start, held_start = held_now +
    sold-since-plan-start."""
    from argosy.services import nvda_sales_history

    _seed_pace_plan_and_snapshot(
        session_with_user, monkeypatch, tmp_path, nvda_shares_now=11_471.0,
    )
    path = nvda_sales_history.compute_nvda_glide_share_path(
        session_with_user, "ariel", as_of=date(2026, 7, 7),
    )
    assert path is not None
    assert path.plan_start == date(2026, 7, 6)
    assert path.held_now == 11_471
    assert path.held_start == 11_471  # Apr sale is pre-plan-start
    # Waypoints: 60 → 47 → 34 → 21 → 8 (%), shares = 11,471 * w/60.
    assert [w.weight_pct for w in path.waypoints] == pytest.approx(
        [60.0, 47.0, 34.0, 21.0, 8.0],
    )
    for w in path.waypoints:
        assert w.shares == int(round(11_471 * w.weight_pct / 60.0))
    assert path.target_shares == int(round(11_471 * 8.0 / 60.0))

    # Ties to the pace: the next-waypoint sell amount is the same glide
    # arithmetic (held_now minus the waypoint's implied shares).
    pace = nvda_sales_history.compute_nvda_sale_pace(
        session_with_user, "ariel", as_of=date(2026, 7, 7),
    )
    wp = next(
        w for w in path.waypoints
        if w.waypoint_date == pace.next_waypoint_date
    )
    assert pace.next_waypoint_weight_pct == pytest.approx(wp.weight_pct)
    assert pace.shares_to_sell_by_waypoint == pytest.approx(
        path.held_now - wp.shares, abs=1,
    )


def test_deconcentration_and_trajectory_render_one_canonical_glide(
    session_with_user, monkeypatch, tmp_path,
):
    """The home Deconcentration card (allocation-glidepath NVDA waypoints)
    and the /plan NVDA trajectory (projected_path + ceiling) must agree on
    every waypoint — same dates, same weights, shares tied by held_start *
    w/w_start. Regression: the trajectory used to bind to
    compute_nvda_projection, which (a) returned None whenever the promoted
    plan had no decision_run_id — the live v67 case, chart lost its plan
    line while the home card kept rendering — and (b) drew a linear
    cap/current ramp instead of the doc glide when it did resolve."""
    from datetime import date as _date

    from argosy.api.routes.plan import _compute_nvda_trajectory
    from argosy.services import nvda_sales_history
    from argosy.services.allocation_glidepath import (
        compute_allocation_glidepath,
    )

    _seed_pace_plan_and_snapshot(
        session_with_user, monkeypatch, tmp_path,
        nvda_shares_now=11_471.0, role="current",
    )

    # Surface 1 — home Deconcentration card: the glidepath endpoint's NVDA
    # class waypoints (the card extracts them client-side by class name).
    gp = compute_allocation_glidepath(
        session_with_user, "ariel", _date(2026, 7, 7),
    )
    assert gp is not None
    decon_wps = [
        (p.point_date.isoformat(),
         p.composition_pct_by_class["Strategic single-stock (NVDA)"])
        for p in gp.points
    ]

    # Surface 2 — /plan trajectory chart: projected_path + ceiling.
    traj = _compute_nvda_trajectory(user_id="ariel", db=session_with_user)
    traj_wps = [
        (p.date, p.tradeable_weight_pct) for p in traj.projected_path
    ]

    # The canonical derivation both must equal.
    path = nvda_sales_history.compute_nvda_glide_share_path(
        session_with_user, "ariel", as_of=_date(2026, 7, 7),
    )
    assert path is not None
    canon = [
        (w.waypoint_date.isoformat(), round(w.weight_pct, 2))
        for w in path.waypoints
    ]

    assert [(d, round(w, 2)) for d, w in decon_wps] == canon
    assert [(d, round(w, 2)) for d, w in traj_wps] == canon
    # Share axis: every projected point carries the glide-implied count.
    assert [p.shares for p in traj.projected_path] == [
        w.shares for w in path.waypoints
    ]
    assert traj.ceiling_target_shares == float(path.target_shares)
    # today_shares comes from the same snapshot the pace/glide path read
    # (no TSV in this fixture).
    assert traj.today_shares == path.held_now


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
