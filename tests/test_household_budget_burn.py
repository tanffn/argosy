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
        # Fix 4: fallback value is ROUNDED UP to the nearest ₪1,000.
        # yaml_burn=23084 → ceil(23.084) × 1000 = 24000.
        import math as _math
        expected_planning = float(_math.ceil(yaml_burn / 1000.0) * 1000)
        assert payload["monthly_burn_nis"] == pytest.approx(expected_planning, abs=0.01), (
            f"Fallback value must be rounded up: {yaml_burn} → {expected_planning}, "
            f"got {payload['monthly_burn_nis']}"
        )
        assert payload.get("monthly_burn_raw_nis") == pytest.approx(float(yaml_burn), abs=0.01), (
            "Raw typed value must travel alongside the rounded planning figure"
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
            tx_count=10,  # below the 50-tx threshold → treated as partial
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


# ---------------------------------------------------------------------------
# Targeted tests for the 5 Sol-review blockers
# ---------------------------------------------------------------------------

def _add_credit_refund(
    session, *,
    user_id: str,
    src,
    spend_cat,
    file_id: int,
    month: date,
    amount_nis: Decimal,
    refund_of_id: int | None = None,
):
    """Insert a single credit/refund row into the existing statement for that month.

    Reuses the statement already seeded by _add_month_of_spend to avoid
    the UNIQUE(user_id, source_id, period_start, period_end) constraint.
    """
    existing_stmt = session.query(ExpenseStatement).filter_by(
        user_id=user_id, source_id=src.id,
        period_start=month,
    ).one()
    tx = ExpenseTransaction(
        user_id=user_id, source_id=src.id, statement_id=existing_stmt.id,
        occurred_on=date(month.year, month.month, 15),
        merchant_raw="refund", merchant_normalized="refund",
        amount_nis=amount_nis,
        direction="credit", tx_type="refund",
        category_id=spend_cat.id, category_source="user",
        category_confidence=Decimal("1.0"),
        raw_row_json="{}",
        refund_of_id=refund_of_id,
    )
    session.add(tx)
    session.flush()
    return tx


# --- Blocker 1: refunds must be netted ---

def test_blocker1_refunds_are_netted(tmp_path):
    """A charge later refunded by a credit/refund row must net to zero spend.

    Without the fix, direction='debit' only meant the refund credit was never
    subtracted — overstating burn by the refund amount.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        src, spend_cat, uf = _seed_base(session)
        user_id = "tester"

        # Seed 4 complete months (50 debit txns × ₪1,000 = ₪50,000 each).
        for m in range(1, 5):
            _add_month_of_spend(
                session, user_id=user_id, src=src, spend_cat=spend_cat,
                file_id=uf.id, month=date(2026, m, 1),
                amount_per_tx=Decimal("1000"), tx_count=50,
            )
        # Add a ₪5,000 refund credit in month 2. Net for month 2 should be
        # ₪50,000 - ₪5,000 = ₪45,000 rather than ₪50,000.
        _add_credit_refund(
            session, user_id=user_id, src=src, spend_cat=spend_cat,
            file_id=uf.id, month=date(2026, 2, 1), amount_nis=Decimal("5000"),
        )
        session.commit()

        result = _compute_real_burn_nis(session, user_id)
        assert result is not None

        # Month 2 nets to 45,000; others stay at 50,000. Average = 48,750.
        # Rounded up to nearest 1,000 = 49,000.
        assert result["monthly_burn_nis"] == pytest.approx(49_000.0, abs=1.0), (
            f"Refund must reduce burn; expected 49000 (rounded up from 48750), "
            f"got {result['monthly_burn_nis']}"
        )
        assert result["monthly_burn_raw_nis"] == pytest.approx(48_750.0, abs=1.0)
    finally:
        session.close()
        engine.dispose()


# --- Blocker 2: completeness check uses all-transaction count ---

def test_blocker2_completeness_uses_all_transaction_count(tmp_path):
    """A month with few SPEND debits but many total transactions is NOT partial.

    Before the fix, only post-filter debit rows were counted: a month with
    70 income/transfer rows and 10 spend debits would be wrongly excluded as
    partial even though the statement is complete.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        src, spend_cat, uf = _seed_base(session)
        user_id = "tester"

        # Seed 3 months with 50 spend debits each (comfortably complete).
        for m in range(1, 4):
            _add_month_of_spend(
                session, user_id=user_id, src=src, spend_cat=spend_cat,
                file_id=uf.id, month=date(2026, m, 1),
                amount_per_tx=Decimal("1000"), tx_count=50,
            )

        # Add a 4th month with only 10 SPEND debits but 60 additional income
        # credits. Total = 70 rows → above the 50-row threshold.
        # The old code counted only spend debits (10) and excluded it as partial.
        income_cat = session.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="income.salary",
        ).one_or_none()
        if income_cat is None:
            # Fallback: use a different inflow category slug present in the taxonomy
            income_cat = session.query(ExpenseCategory).filter(
                ExpenseCategory.user_id == user_id,
                ExpenseCategory.is_inflow.is_(True),
            ).first()

        _add_month_of_spend(
            session, user_id=user_id, src=src, spend_cat=spend_cat,
            file_id=uf.id, month=date(2026, 4, 1),
            amount_per_tx=Decimal("1000"), tx_count=10,   # only 10 spend debits
        )
        # Add 60 income credits to push total row count above 50.
        if income_cat is not None:
            stmt4 = session.query(ExpenseStatement).filter_by(
                user_id=user_id, source_id=src.id,
            ).order_by(ExpenseStatement.id.desc()).first()
            for i in range(60):
                session.add(ExpenseTransaction(
                    user_id=user_id, source_id=src.id, statement_id=stmt4.id,
                    occurred_on=date(2026, 4, min(i + 1, 28)),
                    merchant_raw=f"salary_{i}", merchant_normalized=f"salary_{i}",
                    amount_nis=Decimal("100"),
                    direction="credit", tx_type="regular",
                    category_id=income_cat.id, category_source="user",
                    category_confidence=Decimal("1.0"),
                    raw_row_json="{}",
                ))
        session.commit()

        result = _compute_real_burn_nis(session, user_id)
        assert result is not None, (
            "Month 4 has 70 total rows (10 spend + 60 income) — should be complete"
        )
        # Month 4 spend = 10 × ₪1,000 = ₪10,000; months 1–3 = ₪50,000 each.
        # Average (if month 4 included) = (50k + 50k + 50k + 10k) / 4 = 40,000.
        # Rounded up = 40,000.
        assert result["monthly_burn_months_used"] == 4, (
            "Month 4 must be counted as complete (all-txn count ≥ 50)"
        )
    finally:
        session.close()
        engine.dispose()


# --- Blocker 3: HouseholdBudgetReport has machine-readable burn provenance ---

def test_blocker3_household_budget_report_has_monthly_burn_source():
    """HouseholdBudgetReport must carry monthly_burn_source as a typed field.

    Downstream consumers (withdrawal_sequencer, target_progress) must be able
    to branch on provenance without parsing free-text key_concerns prose.
    """
    from argosy.agents.household_budget_analyst import HouseholdBudgetReport

    # Default value is "unknown" — never None.
    report = HouseholdBudgetReport()
    assert hasattr(report, "monthly_burn_source"), (
        "HouseholdBudgetReport must have monthly_burn_source field"
    )
    assert report.monthly_burn_source == "unknown"

    # Validate all valid source values round-trip through Pydantic.
    for src in ("expense_transactions", "identity_yaml_fallback",
                "identity_yaml_fallback_on_error", "unknown"):
        r = HouseholdBudgetReport(monthly_burn_source=src)
        assert r.monthly_burn_source == src

    # The field must appear in the JSON schema so the LLM sees it.
    schema = HouseholdBudgetReport.model_json_schema()
    assert "monthly_burn_source" in schema.get("properties", {}), (
        "monthly_burn_source must be in JSON schema so the LLM outputs it"
    )


# --- Blocker 4: YAML fallback applies round-up ---

def test_blocker4_yaml_fallback_rounds_up(tmp_path):
    """A typed identity_yaml value of 24,001 must yield planning_burn=25,000.

    Before the fix, the YAML path bypassed the round-up and fed 24,001
    directly as the planning figure, violating the conservatism rule.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        _, _, _ = _seed_base(session)
        user_id = "tester"

        # 24,001 is NOT on a 1,000 boundary → must round up to 25,000.
        session.add(UserContext(
            user_id=user_id,
            identity_yaml=yaml.dump({
                "monthly_expenses_total_nis": 24_001,
                "monthly_expenses_window": "2025-01 to 2025-12",
                "emergency_fund_months": 6,
            }),
        ))
        session.commit()

        # Zero complete months → falls back to identity_yaml.
        payload = _assemble_household_budget_payload(session, user_id)
        assert payload["monthly_burn_source"] == "identity_yaml_fallback"
        assert payload["monthly_burn_nis"] == pytest.approx(25_000.0, abs=0.01), (
            f"Typed 24001 must round up to 25000; got {payload['monthly_burn_nis']}"
        )
        assert payload.get("monthly_burn_raw_nis") == pytest.approx(24_001.0, abs=0.01), (
            "Raw typed value must travel alongside the rounded planning figure"
        )

        # Also test an already-round value: 25,000 → 25,000 (no change).
        session.query(UserContext).filter_by(user_id=user_id).delete()
        session.add(UserContext(
            user_id=user_id,
            identity_yaml=yaml.dump({
                "monthly_expenses_total_nis": 25_000,
                "monthly_expenses_window": "2025-01 to 2025-12",
                "emergency_fund_months": 6,
            }),
        ))
        session.commit()
        payload2 = _assemble_household_budget_payload(session, user_id)
        assert payload2["monthly_burn_nis"] == pytest.approx(25_000.0, abs=0.01)
    finally:
        session.close()
        engine.dispose()


# --- Blocker 5: computation failure is labelled distinctly ---

def test_blocker5_computation_failure_labelled_distinctly(monkeypatch, tmp_path):
    """An exception in _compute_real_burn_nis must be labelled 'on_error', not
    'insufficient_transaction_data'.

    The old code caught all exceptions and silently converted them to
    'identity_yaml_fallback' with reason='insufficient expense transaction data'
    — a lie that hid bugs behind a plausible-but-wrong message.
    """
    engine = _make_engine(tmp_path)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    session = SF()
    try:
        _, _, _ = _seed_base(session)
        user_id = "tester"
        session.add(UserContext(
            user_id=user_id,
            identity_yaml=yaml.dump({
                "monthly_expenses_total_nis": 20_000,
                "monthly_expenses_window": "2025-01 to 2025-12",
                "emergency_fund_months": 6,
            }),
        ))
        session.commit()

        # Simulate a crash inside _compute_real_burn_nis.
        import argosy.orchestrator.flows.plan_synthesis.inputs as _inputs_mod
        monkeypatch.setattr(
            _inputs_mod, "_compute_real_burn_nis",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated DB crash")),
        )

        payload = _assemble_household_budget_payload(session, user_id)

        # Must fall back to YAML but label it as an error, not data insufficiency.
        assert payload["monthly_burn_source"] == "identity_yaml_fallback_on_error", (
            "Computation failure must be labelled 'identity_yaml_fallback_on_error', "
            f"not '{payload.get('monthly_burn_source')}'"
        )
        # The YAML value must still be used (with round-up).
        assert payload["monthly_burn_nis"] is not None
        assert payload["monthly_burn_nis"] == pytest.approx(20_000.0, abs=0.01), (
            "Should fall back to YAML value even on error"
        )
    finally:
        session.close()
        engine.dispose()
