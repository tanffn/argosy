"""Tests for assemble_deployment_market_context — Task 5 (live + cached fallback).

All tests are deterministic:
- Live paths: monkeypatched ``market_snapshot`` + ``verify_nvda``.
- Cached paths: use the ``alembic_engine_at_head`` fixture to seed real
  ``AgentReport`` rows, then call the assembler with a real sync session, OR
  monkeypatch ``_query_latest_agent_report`` for the no-DB cases.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.deployment_market_context import (
    DataFreshness,
    DeploymentMarketContext,
    NvdaVerification,
    assemble_deployment_market_context,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

def _make_snapshot_dict(
    sp500: float = 5400.0,
    vix: float = 18.5,
    oil_wti: float = 72.0,
    usd_nis: float = 3.65,
    boi_rate: float = 4.5,
    cpi_yoy: float = 3.2,
) -> dict[str, tuple[float, DataFreshness]]:
    """Return a market_snapshot-style dict (value, DataFreshness) per key."""
    now_iso = datetime.now(timezone.utc).isoformat()
    keys_values = {
        "sp500": sp500,
        "vix": vix,
        "oil_wti": oil_wti,
        "usd_nis": usd_nis,
        "boi_rate": boi_rate,
        "cpi_yoy": cpi_yoy,
    }
    return {
        k: (
            v,
            DataFreshness(
                field=k,
                fetched_at=now_iso,
                age_seconds=0.0,
                source="fred:test",
                is_stale=False,
            ),
        )
        for k, v in keys_values.items()
    }


def _make_nvda(consistent: bool | None = True) -> NvdaVerification:
    return NvdaVerification(
        price=130.0,
        shares=24_400_000_000.0,
        market_cap=130.0 * 24_400_000_000.0,
        consistent=consistent,
        note="test nvda",
    )


# ---------------------------------------------------------------------------
# Task 5a: live path — both live sources succeed
# ---------------------------------------------------------------------------


class TestLivePath:
    """When allow_live=True and live sources succeed, context is fresh."""

    @pytest.fixture(autouse=True)
    def _patch_live(self, monkeypatch):
        self._snap = _make_snapshot_dict()
        self._nvda = _make_nvda(consistent=True)

        monkeypatch.setattr(
            "argosy.services.market_snapshot.market_snapshot",
            lambda session: self._snap,
        )
        monkeypatch.setattr(
            "argosy.services.deployment_market_context.verify_nvda",
            lambda session: self._nvda,
        )

    def test_returns_deployment_market_context(self):
        ctx = assemble_deployment_market_context(session=None)
        assert isinstance(ctx, DeploymentMarketContext)

    def test_overall_age_label_is_live(self):
        ctx = assemble_deployment_market_context(session=None)
        assert ctx.overall_age_label == "live"

    def test_snapshot_keys_all_present(self):
        ctx = assemble_deployment_market_context(session=None)
        for k in ("sp500", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"):
            assert k in ctx.snapshot

    def test_snapshot_values_from_live_data(self):
        ctx = assemble_deployment_market_context(session=None)
        assert ctx.snapshot["sp500"] == pytest.approx(5400.0)
        assert ctx.snapshot["vix"] == pytest.approx(18.5)
        assert ctx.snapshot["usd_nis"] == pytest.approx(3.65)

    def test_nvda_set_from_live(self):
        ctx = assemble_deployment_market_context(session=None)
        assert ctx.nvda is not None
        assert ctx.nvda.price == pytest.approx(130.0)
        assert ctx.nvda.consistent is True

    def test_freshness_tuple_non_empty(self):
        ctx = assemble_deployment_market_context(session=None)
        assert len(ctx.freshness) > 0

    def test_freshness_age_near_zero(self):
        ctx = assemble_deployment_market_context(session=None)
        for df in ctx.freshness:
            assert df.age_seconds == pytest.approx(0.0)

    def test_is_any_stale_false_when_all_fresh_and_nvda_consistent(self):
        ctx = assemble_deployment_market_context(session=None)
        assert ctx.is_any_stale is False

    def test_is_any_stale_true_when_nvda_inconsistent(self, monkeypatch):
        monkeypatch.setattr(
            "argosy.services.deployment_market_context.verify_nvda",
            lambda session: _make_nvda(consistent=False),
        )
        ctx = assemble_deployment_market_context(session=None)
        assert ctx.is_any_stale is True

    def test_is_any_stale_true_when_one_freshness_stale(self, monkeypatch):
        # Inject one stale freshness entry into the snapshot.
        snap = _make_snapshot_dict()
        now_iso = datetime.now(timezone.utc).isoformat()
        stale_df = DataFreshness(
            field="vix",
            fetched_at=now_iso,
            age_seconds=100_000.0,
            source="fred:VIXCLS",
            is_stale=True,
        )
        snap["vix"] = (snap["vix"][0], stale_df)
        monkeypatch.setattr(
            "argosy.services.market_snapshot.market_snapshot",
            lambda session: snap,
        )
        ctx = assemble_deployment_market_context(session=None)
        assert ctx.is_any_stale is True


# ---------------------------------------------------------------------------
# Task 5b: live path fails → cached fallback with seeded AgentReport rows
# ---------------------------------------------------------------------------


class TestCachedFallbackViaRealDB:
    """When market_snapshot raises, fall back to AgentReport rows; age surfaced."""

    @pytest.fixture(autouse=True)
    def _patch_live_failing(self, monkeypatch):
        """Make market_snapshot raise unconditionally."""
        monkeypatch.setattr(
            "argosy.services.market_snapshot.market_snapshot",
            lambda session: (_ for _ in ()).throw(RuntimeError("live feed down")),
        )
        # verify_nvda should not be called (market_snapshot fails first), but
        # patch it defensively to a simple return so tests don't accidentally
        # hit the real adapter.
        monkeypatch.setattr(
            "argosy.services.deployment_market_context.verify_nvda",
            lambda session: _make_nvda(),
        )

    @pytest.fixture
    def seeded_db(self, alembic_engine_at_head):
        """Seed one 'macro' and one 'fx' AgentReport row aged 3h."""
        from argosy.state.models import AgentReport, User

        SF = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
        with SF() as s:
            # Seed user if not present.
            if s.get(User, "ariel") is None:
                s.add(User(id="ariel", plan="free"))
                s.flush()

            three_hours_ago = datetime.now(timezone.utc) - timedelta(hours=3)

            macro_payload = json.dumps({
                "sp500": 5300.0,
                "vix": 20.0,
                "oil_wti": 71.0,
                "boi_rate": 4.5,
                "cpi_yoy": 3.1,
            })
            fx_payload = json.dumps({"usd_nis": 3.62})

            s.add(AgentReport(
                user_id="ariel",
                agent_role="macro",
                response_text=macro_payload,
                prompt_hash="testhash1",
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.001,
                created_at=three_hours_ago,
            ))
            s.add(AgentReport(
                user_id="ariel",
                agent_role="fx",
                response_text=fx_payload,
                prompt_hash="testhash2",
                tokens_in=50,
                tokens_out=100,
                cost_usd=0.0005,
                created_at=three_hours_ago,
            ))
            s.commit()

        SF2 = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
        sess = SF2()
        yield sess
        sess.close()

    def test_returns_context_not_exception(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        assert isinstance(ctx, DeploymentMarketContext)

    def test_overall_age_label_mentions_cached(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        assert "cached" in ctx.overall_age_label.lower()

    def test_overall_age_label_contains_nonzero_age(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        # Should mention hours since we seeded 3h ago.
        label = ctx.overall_age_label
        # Must not be "live"
        assert label != "live"
        # Should contain some age indicator
        assert any(c.isdigit() for c in label), f"Expected digits in age label: {label!r}"

    def test_all_snapshot_keys_present(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        for k in ("sp500", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"):
            assert k in ctx.snapshot

    def test_snapshot_values_parsed_from_cache(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        assert ctx.snapshot["sp500"] == pytest.approx(5300.0)
        assert ctx.snapshot["vix"] == pytest.approx(20.0)
        assert ctx.snapshot["usd_nis"] == pytest.approx(3.62)

    def test_nvda_none_on_cached_path(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        assert ctx.nvda is None

    def test_freshness_age_non_zero(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        # All entries should have age > 0 since we seeded 3h ago.
        for df in ctx.freshness:
            assert df.age_seconds > 0, f"Expected non-zero age for {df.field}"

    def test_freshness_age_approximately_3h(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        for df in ctx.freshness:
            # 3h = 10800s; allow generous tolerance for test timing.
            assert 9_000 < df.age_seconds < 14_400, (
                f"{df.field} age={df.age_seconds:.0f}s, expected ~10800s"
            )

    def test_freshness_source_mentions_agent_reports(self, seeded_db):
        ctx = assemble_deployment_market_context(session=seeded_db, allow_live=True)
        for df in ctx.freshness:
            assert "agent_reports" in df.source


# ---------------------------------------------------------------------------
# Task 5c: allow_live=False → goes straight to cached path (no live calls)
# ---------------------------------------------------------------------------


class TestAllowLiveFalse:
    """allow_live=False bypasses live fetch entirely."""

    @pytest.fixture(autouse=True)
    def _assert_no_live_calls(self, monkeypatch):
        """Patch live sources to raise so that any call to them would fail the test."""
        def _boom_snap(session):
            raise AssertionError("market_snapshot must NOT be called when allow_live=False")

        def _boom_nvda(session):
            raise AssertionError("verify_nvda must NOT be called when allow_live=False")

        monkeypatch.setattr(
            "argosy.services.market_snapshot.market_snapshot",
            _boom_snap,
        )
        monkeypatch.setattr(
            "argosy.services.deployment_market_context.verify_nvda",
            _boom_nvda,
        )

    @pytest.fixture
    def seeded_db_for_allow_live_false(self, alembic_engine_at_head):
        """Seed a macro AgentReport row aged 1h."""
        from argosy.state.models import AgentReport, User

        SF = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
        with SF() as s:
            if s.get(User, "ariel") is None:
                s.add(User(id="ariel", plan="free"))
                s.flush()

            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            s.add(AgentReport(
                user_id="ariel",
                agent_role="macro",
                response_text=json.dumps({"vix": 17.0, "sp500": 5450.0}),
                prompt_hash="hash_allow_live_false",
                tokens_in=50,
                tokens_out=100,
                cost_usd=0.0,
                created_at=one_hour_ago,
            ))
            s.commit()

        SF2 = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
        sess = SF2()
        yield sess
        sess.close()

    def test_returns_context_not_exception(self, seeded_db_for_allow_live_false):
        ctx = assemble_deployment_market_context(
            session=seeded_db_for_allow_live_false,
            allow_live=False,
        )
        assert isinstance(ctx, DeploymentMarketContext)

    def test_overall_age_label_not_live(self, seeded_db_for_allow_live_false):
        ctx = assemble_deployment_market_context(
            session=seeded_db_for_allow_live_false,
            allow_live=False,
        )
        assert ctx.overall_age_label != "live"

    def test_nvda_none(self, seeded_db_for_allow_live_false):
        ctx = assemble_deployment_market_context(
            session=seeded_db_for_allow_live_false,
            allow_live=False,
        )
        assert ctx.nvda is None

    def test_snapshot_keys_all_present(self, seeded_db_for_allow_live_false):
        ctx = assemble_deployment_market_context(
            session=seeded_db_for_allow_live_false,
            allow_live=False,
        )
        for k in ("sp500", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"):
            assert k in ctx.snapshot

    def test_cached_values_parsed(self, seeded_db_for_allow_live_false):
        ctx = assemble_deployment_market_context(
            session=seeded_db_for_allow_live_false,
            allow_live=False,
        )
        assert ctx.snapshot["vix"] == pytest.approx(17.0)
        assert ctx.snapshot["sp500"] == pytest.approx(5450.0)


# ---------------------------------------------------------------------------
# Task 5d: cached path with no AgentReport rows — never blank, always surfaced
# ---------------------------------------------------------------------------


class TestCachedFallbackNoRows:
    """When no AgentReport rows exist, returns 0.0 values but never blank/silent."""

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch):
        # No live calls — force cached path.
        monkeypatch.setattr(
            "argosy.services.market_snapshot.market_snapshot",
            lambda session: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        monkeypatch.setattr(
            "argosy.services.deployment_market_context.verify_nvda",
            lambda session: _make_nvda(),
        )
        # Patch _query_latest_agent_report to return None (no rows).
        monkeypatch.setattr(
            "argosy.services.deployment_market_context._query_latest_agent_report",
            lambda session, user_id, role: None,
        )

    def test_returns_context_not_exception(self):
        ctx = assemble_deployment_market_context(session=None, allow_live=True)
        assert isinstance(ctx, DeploymentMarketContext)

    def test_all_snapshot_keys_present(self):
        ctx = assemble_deployment_market_context(session=None, allow_live=True)
        for k in ("sp500", "vix", "oil_wti", "usd_nis", "boi_rate", "cpi_yoy"):
            assert k in ctx.snapshot

    def test_all_values_zero_when_no_cache(self):
        ctx = assemble_deployment_market_context(session=None, allow_live=True)
        for k, v in ctx.snapshot.items():
            assert v == 0.0, f"Expected 0.0 for {k}, got {v}"

    def test_all_freshness_stale_when_no_cache(self):
        ctx = assemble_deployment_market_context(session=None, allow_live=True)
        assert all(df.is_stale for df in ctx.freshness)

    def test_is_any_stale_true_when_no_cache(self):
        ctx = assemble_deployment_market_context(session=None, allow_live=True)
        assert ctx.is_any_stale is True

    def test_overall_age_label_not_live_and_not_empty(self):
        ctx = assemble_deployment_market_context(session=None, allow_live=True)
        assert ctx.overall_age_label
        assert ctx.overall_age_label != "live"
