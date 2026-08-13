"""Tests for the real-burn aggregation in plan_synthesis/inputs.py.

Two focused tests:
  1. When >=3 complete months of ExpenseTransaction data exist, the burn
     figure comes from real aggregation (source = "expense_transactions").
  2. When there is insufficient transaction data (< 3 complete months),
     the payload falls back to identity_yaml and labels it explicitly
     as "identity_yaml_fallback".
"""
from __future__ import annotations

import yaml
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.orchestrator.flows.plan_synthesis.inputs import (
    _assemble_household_budget_payload,
    _compute_real_burn_nis,
)
from argosy.services.expense_ingest.taxonomy_seed import (
    seed_system_defaults,
    seed_user_categories,
)
from argosy.state.models import (
    Base,
    ExpenseCategory,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    User,
    UserContext,
    UserFile,
)


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _make_engine(tmp_path):
    """Create an in-memory (well, tmp-path) SQLite DB with all tables."""
    db_path = tmp_path / "burn_test.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_base(session, user_id: str = "tester") -> tuple:
    """Seed user, categories, source.  Returns (source, spend_cat)."""
    session.add(User(id=user_id, plan="free"))
    session.flush()
    seed_system_defaults(session)
    session.flush()
    seed_user_categories(session, user_id)
    session.flush()

    uf = UserFile(
        user_id=user_id, sha256="a" * 64,
        original_name="seed", sanitized_name="seed",
        mime_type="application/octet-stream", kind="other",
        size_bytes=1, storage_path="/tmp/seed", source="chat_attachment",
    )
    session.add(uf)
    session.flush()

    src = ExpenseSource(
        user_id=user_id, kind="card", issuer="isracard",
        external_id="7777", display_name="test-card",
    )
    session.add(src)
    session.flush()

    spend_cat = session.query(ExpenseCategory).filter_by(
        user_id=user_id, slug="dining_out.restaurants",
    ).one()

    return src, spend_cat, uf


def _add_month_of_spend(
    session, *,
    user_id: str,
    src,
    spend_cat,
    file_id: int,
    month: date,
    amount_per_tx: Decimal = Decimal("1000"),
    tx_count: int = 50,  # comfortably above the 30-tx partial-month threshold
):
    """Insert one statement + tx_count debit rows for the given month."""
    stmt = ExpenseStatement(
        user_id=user_id, source_id=src.id, file_id=file_id,
        period_start=month,
        period_end=date(month.year, month.month, 28),
        parsed_total_nis=amount_per_tx * tx_count,
        declared_total_nis=amount_per_tx * tx_count,
        parser_name="isracard", parser_version="0.1.0",
        status="parsed",
    )
    session.add(stmt)
    session.flush()

    for i in range(tx_count):
        day = min(i + 1, 28)
        session.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(month.year, month.month, day),
            merchant_raw=f"merchant_{i}", merchant_normalized=f"m{i}",
            amount_nis=amount_per_tx,
            direction="debit", tx_type="regular",
            category_id=spend_cat.id, category_source="user",
            category_confidence=Decimal("1.0"),
            raw_row_json="{}",
        ))


# ---------------------------------------------------------------------------
# Test 1 — real aggregation is used when sufficient data exists
# ---------------------------------------------------------------------------

def test_burn_from_real_transactions(tmp_path):
    """When >=3 complete months of expense data exist the payload reports
    source='expense_transactions' and a computed burn figure, not the
    identity_yaml hand-typed value.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        src, spend_cat, uf = _seed_base(session)
        user_id = "tester"

        # Seed an identity_yaml with a DIFFERENT burn figure so we can
        # verify which value the payload returns.
        yaml_burn = 99999  # obviously wrong sentinel
        session.add(UserContext(
            user_id=user_id,
            identity_yaml=yaml.dump({
                "monthly_expenses_total_nis": yaml_burn,
                "monthly_expenses_window": "2025-01 to 2025-12",
                "emergency_fund_months": 6,
            }),
        ))
        session.flush()

        # Add 6 months of spend: ₪1,000/tx × 50 tx = ₪50,000/month.
        for m in range(1, 7):
            _add_month_of_spend(
                session, user_id=user_id, src=src, spend_cat=spend_cat,
                file_id=uf.id,
                month=date(2026, m, 1),
                amount_per_tx=Decimal("1000"),
                tx_count=50,
            )
        session.commit()

        # --- _compute_real_burn_nis ---
        result = _compute_real_burn_nis(session, user_id)
        assert result is not None, "Expected real-data result, got None"
        assert result["monthly_burn_source"] == "expense_transactions"
        # Each month: 50 txns × ₪1,000 = ₪50,000.
        assert result["monthly_burn_nis"] == pytest.approx(50_000.0, abs=1.0), (
            f"Expected ~50000, got {result['monthly_burn_nis']}"
        )
        assert result["monthly_burn_txn_count"] > 0
        assert result["monthly_burn_months_used"] >= 3

        # --- _assemble_household_budget_payload ---
        payload = _assemble_household_budget_payload(session, user_id)
        assert payload["monthly_burn_source"] == "expense_transactions", (
            "Payload must use real transactions, not the YAML fallback"
        )
        assert payload["monthly_burn_nis"] != yaml_burn, (
            "Payload must NOT use the identity_yaml sentinel value when real data exists"
        )
        assert payload["monthly_burn_nis"] == pytest.approx(50_000.0, abs=1.0)
        assert payload["monthly_burn_txn_count"] > 0
        assert payload["monthly_burn_months_used"] >= 3
        assert "to" in payload["monthly_burn_window"]  # e.g. "2026-01 to 2026-05"
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 2 — fallback is labelled when there is insufficient transaction data
# ---------------------------------------------------------------------------

def test_burn_fallback_labelled_when_insufficient_data(tmp_path):
    """When fewer than min_complete_months of data exist the payload falls
    back to identity_yaml AND labels the source as 'identity_yaml_fallback',
    so the analyst can flag it as an estimate in key_concerns.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        src, spend_cat, uf = _seed_base(session)
        user_id = "tester"

        yaml_burn = 23084
        yaml_window = "2025-06 to 2026-05"
        session.add(UserContext(
            user_id=user_id,
            identity_yaml=yaml.dump({
                "monthly_expenses_total_nis": yaml_burn,
                "monthly_expenses_window": yaml_window,
                "emergency_fund_months": 6,
            }),
        ))
        session.flush()

        # Only 2 complete months — below the 3-month threshold.
        for m in (1, 2):
            _add_month_of_spend(
                session, user_id=user_id, src=src, spend_cat=spend_cat,
                file_id=uf.id,
                month=date(2026, m, 1),
                amount_per_tx=Decimal("500"),
                tx_count=50,
            )
        session.commit()

        # --- _compute_real_burn_nis must return None ---
        result = _compute_real_burn_nis(session, user_id, min_complete_months=3)
        assert result is None, (
            "Expected None (insufficient data) when only 2 complete months exist"
        )

        # --- _assemble_household_budget_payload must fall back + label it ---
        payload = _assemble_household_budget_payload(session, user_id)
        assert payload["monthly_burn_source"] == "identity_yaml_fallback", (
            "Source must be 'identity_yaml_fallback' when data is insufficient"
        )
        assert payload["monthly_burn_nis"] == yaml_burn, (
            "Fallback value must match the identity_yaml figure"
        )
        assert payload["monthly_burn_txn_count"] == 0, (
            "txn_count must be 0 to signal no real data was used"
        )
        assert payload["monthly_burn_months_used"] == 0
        # The YAML window should be preserved so the agent can quote it.
        assert yaml_window in payload["monthly_burn_window"]
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 3 — partial-month (thin) final month is excluded from the average
# ---------------------------------------------------------------------------

def test_partial_month_excluded_from_average(tmp_path):
    """The most-recent calendar month with < 30 transactions is treated as a
    partial ingest and excluded from the per-month average, but prior complete
    months still produce a valid (non-None) result when >=3 are present.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        src, spend_cat, uf = _seed_base(session)
        user_id = "tester"

        # Seed 3 complete months (50 txns each) at ₪1,000/txn.
        for m in range(1, 4):
            _add_month_of_spend(
                session, user_id=user_id, src=src, spend_cat=spend_cat,
                file_id=uf.id,
                month=date(2026, m, 1),
                amount_per_tx=Decimal("1000"),
                tx_count=50,
            )

        # Add a 4th, thin month (only 10 txns at ₪5,000 each) — simulates a
        # partial statement mid-month.  Its inflated per-tx amount would skew
        # the average upward if incorrectly included.
        _add_month_of_spend(
            session, user_id=user_id, src=src, spend_cat=spend_cat,
            file_id=uf.id,
            month=date(2026, 4, 1),
            amount_per_tx=Decimal("5000"),
            tx_count=10,  # below the 30-tx threshold → treated as partial
        )
        session.commit()

        result = _compute_real_burn_nis(session, user_id)
        assert result is not None
        # The 3 complete months (₪50,000 each) should drive the average;
        # the thin 4th month (₪50,000 total but only 10 txns) should be excluded.
        assert result["monthly_burn_nis"] == pytest.approx(50_000.0, abs=1.0), (
            f"Partial month must not skew the average; got {result['monthly_burn_nis']}"
        )
        # Only the 3 complete months should be counted.
        assert result["monthly_burn_months_used"] == 3
    finally:
        session.close()
        engine.dispose()


def test_planning_burn_is_derived_then_rounded_up_never_typed(monkeypatch, tmp_path):
    """The planning figure is a BUFFER on a measurement, not a typed number.

    Ariel's ruling (2026-08-13) was "round up to 25k". The danger in honouring
    that literally would be re-introducing exactly the defect this whole change
    removes: a hand-entered burn. So the derived value must survive alongside
    the padded one, and the padding may only ever round UP (understating burn
    is what retires you too early).
    """
    import math

    for raw, expected in [(24032.0, 25000.0), (25000.0, 25000.0), (22410.0, 23000.0)]:
        assert math.ceil(raw / 1000.0) * 1000 == expected


def test_buffer_never_reduces_burn() -> None:
    """Rounding is one-directional: the planning figure is never below measured."""
    import math

    for raw in (1.0, 999.0, 1000.0, 24032.4, 24999.9, 250000.0):
        planning = math.ceil(raw / 1000.0) * 1000
        assert planning >= raw
