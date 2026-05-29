"""Tests for ``argosy/services/state_snapshot.py`` (Spec B commit #2).

Coverage:
  - ``collect_state_snapshot`` returns a dict with the six top-level
    sections always present.
  - Populated ``portfolio_snapshots`` row → portfolio.total_value_usd
    + concentration / allocations populated.
  - Empty ``portfolio_snapshots`` → portfolio is ``{}`` (the KEY is
    still present, but its value is an empty dict).
  - BoI adapter raising → ``macro.fx_usd_nis_spot`` is ``None`` and
    ``source_versions['historical_replay_gaps']`` contains an entry
    for ``macro.fx_usd_nis_spot``.
  - ``persist_state_snapshot`` + ``get_latest_state_snapshot`` round-trip.
  - ``get_state_snapshot_by_date`` returns a match / None.
  - Inserting two snapshots for the same (user, date) raises
    ``IntegrityError`` (UNIQUE constraint).
  - ``state_snapshot_to_dict`` parses both JSON columns.
  - Historical replay raises ``StateReplayError`` when no
    plan_version exists on/before ``as_of``.

Test cmd:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_state_snapshot.py -v
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from argosy.services.state_snapshot import (
    StateReplayError,
    collect_state_snapshot,
    get_latest_state_snapshot,
    get_state_snapshot_by_date,
    persist_state_snapshot,
    state_snapshot_to_dict,
)
from argosy.state.models import (
    Base,
    NewsSignal,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB.

    File-backed (not :memory:) so engines + threads can share state
    without the in-memory connection-binding gotcha. Pattern lifted
    from test_anomaly_runner.py.
    """
    db_path = tmp_path / "state_snapshot.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.add(UserContext(
        user_id=USER,
        identity_yaml=(
            "allocation_target:\n"
            "  Growth: 0.40\n"
            "  Income: 0.30\n"
            "  Cash: 0.10\n"
            "fx_rate:\n"
            "  usd_nis: 3.6\n"
        ),
        goals_yaml="",
        constraints_yaml="",
    ))
    # One plan_version with role='current' so the plan_inputs section
    # has something to chew on.
    db.add(PlanVersion(
        user_id=USER,
        version_label="seed-plan-v1",
        source_path="",
        raw_markdown="# Seed plan",
        imported_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        role="current",
        synthesis_inputs_json=json.dumps({
            "mu_nominal_annual": 0.08,
            "sigma_annual": 0.18,
            "inflation_annual": 0.025,
            "marginal_tax_rate": 0.25,
            "retirement_age": 67.0,
            "withdrawal_policy": "constant_real",
        }),
    ))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_portfolio_row(
    session,
    *,
    snapshot_date: date = date(2026, 5, 1),
    total_usd_value_k: float = 1234.5,
    cash_balances_usd_k: float = 50.0,
    fx_usd_nis: float = 3.6,
) -> int:
    """Insert one PortfolioSnapshotRow with realistic positions."""
    positions = [
        {
            "review_status": "",
            "location": "Schwab",
            "currency": "USD",
            "asset_type": "stock",
            "details": "NVIDIA",
            "symbol": "NVDA",
            "shares": 1000.0,
            "current_price": 900.0,
            "current_value_local": 900_000.0,
            "usd_value_k": 900.0,
            "raw_line": 0,
        },
        {
            "review_status": "",
            "location": "Schwab",
            "currency": "USD",
            "asset_type": "etf",
            "details": "SCHD",
            "symbol": "SCHD",
            "shares": 500.0,
            "current_price": 80.0,
            "current_value_local": 40_000.0,
            "usd_value_k": 284.5,
            "raw_line": 0,
        },
        {
            "review_status": "",
            "location": "Schwab",
            "currency": "USD",
            "asset_type": "Cash",
            "details": "cash",
            "symbol": "",
            "shares": None,
            "current_price": None,
            "current_value_local": 50_000.0,
            "usd_value_k": 50.0,
            "raw_line": 0,
        },
    ]
    allocations = [
        {"category": "Growth", "pct": 0.55,
         "usd_value_k": 700.0, "target_pct": 0.40, "target_k": 500.0},
        {"category": "Income", "pct": 0.25,
         "usd_value_k": 300.0, "target_pct": 0.30, "target_k": 370.0},
        {"category": "Cash", "pct": 0.04,
         "usd_value_k": 50.0, "target_pct": 0.10, "target_k": 120.0},
    ]
    totals = {
        "total_usd_value_k": total_usd_value_k,
        "cash_balances_usd_k": cash_balances_usd_k,
    }
    row = PortfolioSnapshotRow(
        user_id=USER,
        snapshot_date=snapshot_date,
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/family.tsv",
        positions_json=json.dumps(positions),
        allocations_json=json.dumps(allocations),
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps(totals),
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=None,
        parse_warnings_json="[]",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


# Adapters. Sync (no asyncio in tests) -- the collector's
# _maybe_await tolerates both sync return values and coroutines.


class FakeBoiAdapter:
    def __init__(self, rate: float = 2.81, as_of: str = "2026-05-29"):
        self._rate = rate
        self._as_of = as_of

    def get_usd_nis(self, *, on_or_before=None, ttl_seconds=None):
        return {"rate": self._rate, "source": "fake", "as_of": self._as_of}


class FailingBoiAdapter:
    def get_usd_nis(self, *, on_or_before=None, ttl_seconds=None):
        raise RuntimeError("BoI unreachable (test stub)")


class FakeFredAdapter:
    """Returns canned series with a single value at the tail."""

    def __init__(self, values: dict[str, float] | None = None):
        # Defaults exercise the happy-path numeric extraction.
        self._values = values or {
            "DFF": 5.25,
            "DGS10": 4.30,
            "SP500": 5300.0,
            "NASDAQCOM": 17000.0,
            "VIXCLS": 18.2,
        }

    def get_series(self, series_id: str, **kwargs):
        v = self._values.get(series_id)
        if v is None:
            return []
        return [{"date": "2026-05-29", "value": v}]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_collect_state_snapshot_six_sections_always_present(sync_session):
    """Empty DB modulo seeded user + plan_version + user_context --
    every top-level section key is present even when sub-sources
    are missing."""
    out = collect_state_snapshot(sync_session, USER)
    state = out["state"]

    # All six sections present.
    for key in (
        "plan_inputs", "portfolio", "macro", "cashflow_recent",
        "tax_assumptions", "metadata",
    ):
        assert key in state, f"missing section {key!r}"

    # plan_inputs got seeded values from the synthesis_inputs_json.
    assert state["plan_inputs"]["assumed_mu_nominal_annual"] == 0.08
    assert state["plan_inputs"]["assumed_sigma_annual"] == 0.18
    assert state["plan_inputs"]["assumed_target_allocation"] == {
        "Growth": 0.40, "Income": 0.30, "Cash": 0.10,
    }
    assert state["plan_inputs"]["plan_version_role"] == "current"

    # portfolio is {} (no portfolio_snapshots row seeded) -- but key
    # is present, which is the contract for the diff service.
    assert state["portfolio"] == {}

    # cashflow_recent always has last_3_months (3 months back).
    assert len(state["cashflow_recent"]["last_3_months"]) == 3

    # tax_assumptions has the four canonical keys.
    for k in (
        "current_marginal_bracket_pct",
        "effective_rate_prior_year_pct",
        "assumed_marginal_rate_pct",
        "withholding_supplemental_cap_pct",
    ):
        assert k in state["tax_assumptions"]

    # metadata always has snapshot_date + user_id.
    assert state["metadata"]["user_id"] == USER
    assert state["metadata"]["snapshot_date"] is not None

    # source_versions always present with the historical_replay_gaps key.
    assert "historical_replay_gaps" in out["source_versions"]


def test_collect_with_portfolio_snapshot_populates_portfolio(sync_session):
    """When a portfolio_snapshots row exists, portfolio fields
    populate -- total_value_usd, concentration, allocations."""
    _seed_portfolio_row(sync_session)

    out = collect_state_snapshot(sync_session, USER)
    p = out["state"]["portfolio"]

    assert p != {}, "portfolio should be populated when a row exists"
    # total_value_usd = total_usd_value_k * 1000
    assert p["total_value_usd"] == pytest.approx(1234.5 * 1000.0)
    # cash_balances_usd = cash_balances_usd_k * 1000
    assert p["cash_balances_usd"] == pytest.approx(50.0 * 1000.0)
    # Concentration: NVDA ($900K) / total
    assert p["top_concentration_pct"] == pytest.approx(900_000 / (1234.5 * 1000.0))
    # Three positions -- NVDA, SCHD, cash row.
    assert len(p["positions"]) == 3
    tickers = [pos["ticker"] for pos in p["positions"]]
    assert "NVDA" in tickers
    # Allocations -- one row per category.
    assert {a["category"] for a in p["allocations"]} == {"Growth", "Income", "Cash"}
    # snapshot_date round-tripped to ISO string.
    assert p["snapshot_date"] == "2026-05-01"


def test_collect_with_empty_portfolio_table_returns_empty_dict(sync_session):
    """Empty portfolio_snapshots → portfolio is ``{}`` (key present,
    value empty)."""
    out = collect_state_snapshot(sync_session, USER)
    assert "portfolio" in out["state"]
    assert out["state"]["portfolio"] == {}


def test_boi_adapter_failure_recorded_as_gap(sync_session):
    """Adapter raising → fx_usd_nis_spot is None, gap entry present."""
    out = collect_state_snapshot(
        sync_session, USER,
        boi_adapter=FailingBoiAdapter(),
        fred_adapter=None,
    )
    assert out["state"]["macro"]["fx_usd_nis_spot"] is None
    gaps = out["source_versions"]["historical_replay_gaps"]
    # Look for the field path in any gap entry.
    assert any("macro.fx_usd_nis_spot" in g for g in gaps), (
        f"expected fx_usd_nis_spot gap, got gaps={gaps!r}"
    )


def test_boi_adapter_success_populates_fx(sync_session):
    """Working BoI adapter → fx_usd_nis_spot populated."""
    out = collect_state_snapshot(
        sync_session, USER,
        boi_adapter=FakeBoiAdapter(rate=2.81),
        fred_adapter=FakeFredAdapter(),
    )
    assert out["state"]["macro"]["fx_usd_nis_spot"] == pytest.approx(2.81)
    assert out["state"]["macro"]["fx_as_of"] == "2026-05-29"
    # FRED values populated too.
    assert out["state"]["macro"]["vix"] == pytest.approx(18.2)
    assert out["state"]["macro"]["fed_funds_rate_pct"] == pytest.approx(5.25)


def test_news_signals_filtered_to_high_materiality(sync_session):
    """Only materiality='high' rows within the lookback window appear."""
    now = datetime.now(timezone.utc)
    sync_session.add(NewsSignal(
        source="rss", source_ref="ref-1",
        received_at=now - timedelta(days=2),
        parsed_tickers=json.dumps(["NVDA"]),
        event_keywords=json.dumps(["earnings"]),
        sentiment="positive", source_trust="high",
        evidence_excerpt="...", raw_text="...",
        materiality="high",
    ))
    sync_session.add(NewsSignal(
        source="rss", source_ref="ref-2",
        received_at=now - timedelta(days=1),
        parsed_tickers=json.dumps([]),
        event_keywords=json.dumps(["FOMC"]),
        sentiment="neutral", source_trust="medium",
        evidence_excerpt="...", raw_text="...",
        materiality="low",  # should be excluded
    ))
    sync_session.commit()

    out = collect_state_snapshot(sync_session, USER)
    news = out["state"]["macro"]["recent_high_materiality_news"]
    assert len(news) == 1
    assert news[0]["parsed_tickers"] == ["NVDA"]
    assert out["state"]["macro"]["recent_news_summary"]["keyword_counts"] == {
        "earnings": 1,
    }


def test_persist_and_get_latest_roundtrip(sync_session):
    """persist_state_snapshot + get_latest_state_snapshot round-trip."""
    out = collect_state_snapshot(sync_session, USER)
    row = persist_state_snapshot(
        sync_session,
        user_id=USER,
        snapshot_date=date(2026, 5, 29),
        state=out["state"],
        source_versions=out["source_versions"],
    )
    assert row.id is not None
    assert row.snapshot_date == date(2026, 5, 29)

    latest = get_latest_state_snapshot(sync_session, USER)
    assert latest is not None
    assert latest.id == row.id


def test_get_latest_picks_most_recent(sync_session):
    """When multiple snapshots exist, latest snapshot_date wins."""
    out = collect_state_snapshot(sync_session, USER)
    persist_state_snapshot(
        sync_session, user_id=USER,
        snapshot_date=date(2026, 5, 1),
        state=out["state"], source_versions=out["source_versions"],
    )
    persist_state_snapshot(
        sync_session, user_id=USER,
        snapshot_date=date(2026, 5, 29),
        state=out["state"], source_versions=out["source_versions"],
    )
    latest = get_latest_state_snapshot(sync_session, USER)
    assert latest.snapshot_date == date(2026, 5, 29)


def test_get_state_snapshot_by_date_match_and_miss(sync_session):
    """Exact-match returns the row; missing date returns None."""
    out = collect_state_snapshot(sync_session, USER)
    persist_state_snapshot(
        sync_session, user_id=USER,
        snapshot_date=date(2026, 5, 15),
        state=out["state"], source_versions=out["source_versions"],
    )
    hit = get_state_snapshot_by_date(sync_session, USER, date(2026, 5, 15))
    assert hit is not None
    miss = get_state_snapshot_by_date(sync_session, USER, date(2026, 5, 16))
    assert miss is None


def test_duplicate_user_date_raises_integrity_error(sync_session):
    """UNIQUE(user_id, snapshot_date) → second insert raises."""
    out = collect_state_snapshot(sync_session, USER)
    persist_state_snapshot(
        sync_session, user_id=USER,
        snapshot_date=date(2026, 5, 29),
        state=out["state"], source_versions=out["source_versions"],
    )
    with pytest.raises(IntegrityError):
        persist_state_snapshot(
            sync_session, user_id=USER,
            snapshot_date=date(2026, 5, 29),
            state=out["state"], source_versions=out["source_versions"],
        )


def test_state_snapshot_to_dict_parses_json_columns(sync_session):
    """to_dict re-parses both JSON columns and stamps metadata.snapshot_id."""
    out = collect_state_snapshot(sync_session, USER)
    row = persist_state_snapshot(
        sync_session, user_id=USER,
        snapshot_date=date(2026, 5, 29),
        state=out["state"], source_versions=out["source_versions"],
    )
    parsed = state_snapshot_to_dict(row)
    assert parsed["id"] == row.id
    assert parsed["user_id"] == USER
    assert parsed["snapshot_date"] == "2026-05-29"
    # Six top-level sections survive the round-trip.
    for key in (
        "plan_inputs", "portfolio", "macro", "cashflow_recent",
        "tax_assumptions", "metadata",
    ):
        assert key in parsed["state"]
    # metadata.snapshot_id is now stamped.
    assert parsed["state"]["metadata"]["snapshot_id"] == row.id


def test_state_snapshot_to_dict_tolerates_corrupt_json(sync_session):
    """Corrupt JSON in either column → empty dict, no exception."""
    out = collect_state_snapshot(sync_session, USER)
    row = persist_state_snapshot(
        sync_session, user_id=USER,
        snapshot_date=date(2026, 5, 29),
        state=out["state"], source_versions=out["source_versions"],
    )
    row.state_json = "this is not json{{{"
    row.source_versions_json = "}}}{{{"
    sync_session.commit()
    parsed = state_snapshot_to_dict(row)
    assert parsed["state"] == {}
    assert parsed["source_versions"] == {}


def test_historical_replay_without_plan_raises(sync_session):
    """as_of pre-dating every plan_version → StateReplayError."""
    # Seeded plan has imported_at = 2026-05-01.
    with pytest.raises(StateReplayError):
        collect_state_snapshot(
            sync_session, USER,
            as_of=date(2025, 1, 1),
        )


def test_historical_replay_records_macro_gaps(sync_session):
    """as_of with a working adapter still records that macro fields
    weren't time-traveled (v1 collector falls back to live values).
    Uses a date AFTER the seeded plan's imported_at so plan_inputs
    can reconstruct -- the test isolates macro-replay behaviour."""
    # Seeded plan has imported_at = 2026-05-01; pick 2026-05-15.
    out = collect_state_snapshot(
        sync_session, USER,
        as_of=date(2026, 5, 15),
        boi_adapter=FakeBoiAdapter(),
        fred_adapter=FakeFredAdapter(),
    )
    gaps = out["source_versions"]["historical_replay_gaps"]
    assert any("historical replay used live adapter" in g for g in gaps), (
        f"expected historical-replay gaps for macro fields, got {gaps!r}"
    )


def test_collect_no_user_at_all_returns_empty_sections(tmp_path):
    """Brand-new DB with no User row: plan_inputs is {} (no
    plan_version) and the snapshot still assembles."""
    db_path = tmp_path / "empty.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        # Don't seed any user / plan / context.
        out = collect_state_snapshot(sess, "no-such-user")
        assert out["state"]["plan_inputs"] == {}
        assert out["state"]["portfolio"] == {}
        # cashflow_recent + tax_assumptions still have their canonical keys.
        assert "last_3_months" in out["state"]["cashflow_recent"]
        assert "assumed_marginal_rate_pct" in out["state"]["tax_assumptions"]
    finally:
        sess.close()
        engine.dispose()


def test_json_serialisation_handles_decimal_and_datetime(sync_session):
    """The state dict is round-trip-safe through json.dumps even if
    upstream produces Decimals / datetimes."""
    out = collect_state_snapshot(sync_session, USER)
    # Should not raise.
    blob = json.dumps(out)
    assert isinstance(blob, str) and len(blob) > 0
    # Round-trip preserves structure.
    parsed = json.loads(blob)
    assert "state" in parsed and "source_versions" in parsed


def test_persist_rejects_non_jsonable_value(sync_session):
    """Codex IMPORTANT integration: persist_state_snapshot must
    fail-fast (no silent str() coercion) when a non-JSON-safe value
    survives into the state dict. _json_safe raises TypeError."""

    class Opaque:  # not in the _json_safe whitelist
        pass

    bad_state = {"plan_inputs": {"weird": Opaque()}}
    with pytest.raises(TypeError, match="refusing to serialise"):
        persist_state_snapshot(
            sync_session, user_id=USER,
            snapshot_date=date(2026, 5, 29),
            state=bad_state, source_versions={},
        )


def test_historical_pension_state_records_gap(sync_session):
    """Codex BLOCKER #1 integration: when as_of is set, the fact
    that extract_pension_state can't be time-traveled is recorded
    in historical_replay_gaps explicitly."""
    out = collect_state_snapshot(
        sync_session, USER,
        as_of=date(2026, 5, 15),  # after seeded plan's imported_at
    )
    gaps = out["source_versions"]["historical_replay_gaps"]
    assert any("plan_inputs.pension_state" in g for g in gaps), (
        f"expected pension_state historical-replay gap; got {gaps!r}"
    )


def test_historical_cashflow_records_gap(sync_session):
    """Codex IMPORTANT #2 integration: as_of forces a gap entry for
    cashflow_recent (helpers don't accept an as_of cutoff)."""
    out = collect_state_snapshot(
        sync_session, USER,
        as_of=date(2026, 5, 15),
    )
    gaps = out["source_versions"]["historical_replay_gaps"]
    assert any(g.startswith("cashflow_recent") for g in gaps), (
        f"expected cashflow_recent historical-replay gap; got {gaps!r}"
    )


def test_historical_tax_query_filters_by_created_at(sync_session):
    """Codex BLOCKER #2 integration: tax_analyst reports inserted
    AFTER as_of are excluded from the tax_assumptions section."""
    from argosy.state.models import AgentReport

    # Future tax_analyst report (created_at after the as_of we'll
    # query for).
    future_report = AgentReport(
        user_id=USER,
        agent_role="tax_analyst",
        prompt_hash="",
        response_text=json.dumps({"effective_rate_pct": 0.42}),
        tokens_in=0, tokens_out=0, cost_usd=0,
        model="test",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),  # AFTER as_of
    )
    sync_session.add(future_report)
    sync_session.commit()

    # As-of BEFORE the future report → effective_rate stays None.
    out = collect_state_snapshot(
        sync_session, USER,
        as_of=date(2026, 5, 15),
    )
    assert out["state"]["tax_assumptions"]["effective_rate_prior_year_pct"] is None

    # Live mode → the latest report (the 0.42 one) gets picked up.
    live = collect_state_snapshot(sync_session, USER)
    assert live["state"]["tax_assumptions"][
        "effective_rate_prior_year_pct"
    ] == pytest.approx(0.42)


def test_portfolio_historical_prefers_snapshot_date(sync_session):
    """Codex IMPORTANT #1 integration: when ``snapshot_date`` is set
    on a row, the historical query filters by it (the row's business
    date), NOT by imported_at. A row imported AFTER as_of but with
    snapshot_date BEFORE as_of must be picked up."""
    # Row whose business date is well before as_of, but imported
    # AFTER as_of. Without the IMPORTANT #1 fix, this row would be
    # excluded by an imported_at-only filter.
    row = PortfolioSnapshotRow(
        user_id=USER,
        snapshot_date=date(2026, 3, 1),  # business date BEFORE as_of
        imported_at=datetime(2026, 6, 1, tzinfo=timezone.utc),  # imported AFTER
        source_path="/tmp/x.tsv",
        positions_json="[]",
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps({"total_usd_value_k": 999.0,
                                "cash_balances_usd_k": 0.0}),
        fx_usd_nis=3.5,
        fx_usd_eur=None,
        parse_warnings_json="[]",
    )
    sync_session.add(row)
    sync_session.commit()

    out = collect_state_snapshot(
        sync_session, USER,
        as_of=date(2026, 5, 15),  # after business date, before import
    )
    # Portfolio should be populated from the row we just seeded.
    assert out["state"]["portfolio"] != {}
    assert out["state"]["portfolio"]["total_value_usd"] == pytest.approx(
        999.0 * 1000.0
    )
