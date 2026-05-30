"""Wiring test for the anomaly-detector orchestrator hook.

Confirms that ``argosy.services.expense_ingest.orchestrator._run_anomaly_detectors``
is invoked after a successful ingest pass AND that it actually fires the
Bucket A merchant-spike detector against a transaction + baseline that
exceeds the spike threshold.

The 5 wired detectors (one row in this assertion set is enough — the
test only needs to prove the orchestrator hooks ARE on the path):
  * Bucket A — ``detect_bucket_a`` (amount outliers).
  * Bucket B-recurring — ``detect_missing_recurring``.
  * Bucket C-novel — ``detect_novel_merchants``.
  * Bucket C-drift — ``detect_category_drift``.
  * Bucket D — ``detect_cross_card_duplicates``.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_anomaly_buckets_wired.py -v
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.expense_ingest.orchestrator import _run_anomaly_detectors
from argosy.state.models import (
    Base,
    ExpenseReviewQueue,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    MerchantRollingStats,
    User,
    UserFile,
)


USER = "ariel"
AS_OF = date.today()


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "anomaly_wired.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id=USER, plan="free"))
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_source_statement(db) -> tuple[int, int]:
    f = UserFile(
        user_id=USER, sha256="w" * 64,
        original_name="x.csv", sanitized_name="x.csv",
        mime_type="text/csv", kind="other",
        size_bytes=1, storage_path="/tmp/x",
        source="expense_statement",
    )
    db.add(f); db.flush()
    src = ExpenseSource(
        user_id=USER, kind="card", issuer="visa",
        external_id="4242", display_name="Visa 4242",
    )
    db.add(src); db.flush()
    stmt = ExpenseStatement(
        user_id=USER, source_id=src.id, file_id=f.id,
        period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        parsed_total_nis=Decimal("1000"),
        declared_total_nis=Decimal("1000"),
        parser_name="visa", parser_version="0.1.0",
        status="parsed",
    )
    db.add(stmt); db.flush()
    return src.id, stmt.id


# ---------------------------------------------------------------------------
# Test 1 — direct call: a $5,000 (₪18,000-equiv) charge at a merchant with
# a ₪200-mean baseline trips Bucket A2 (merchant spike).
# ---------------------------------------------------------------------------


def test_run_anomaly_detectors_fires_bucket_a(db):
    """Pre-seed a merchant baseline (mean=200, n=10) + a 20x spike txn at
    that merchant. ``_run_anomaly_detectors`` should write at least one
    ExpenseReviewQueue row (the A2 fire).

    A2 gates: ``amount >= 3 * mean_M``, ``mean_M >= 50``, baseline count
    >= 3. Our setup clears all three.
    """
    src_id, stmt_id = _seed_source_statement(db)

    # Seed a rolling-stats baseline for "grocery_world" at category 1.
    baseline = MerchantRollingStats(
        user_id=USER,
        merchant_normalized="grocery_world",
        category_id=1,
        window_start=AS_OF - timedelta(days=179),
        window_end=AS_OF,
        txn_count=10,
        median_nis=Decimal("200"),
        mad_nis=Decimal("20"),
        mean_nis=Decimal("200"),
        stdev_nis=None,
        min_nis=Decimal("150"),
        max_nis=Decimal("250"),
        first_seen_at=AS_OF - timedelta(days=120),
        last_seen_at=AS_OF - timedelta(days=2),
    )
    db.add(baseline); db.flush()

    # The spike: ₪5000 at the same merchant — 25x the mean, well over the
    # 3x A2 threshold AND over the 5x critical threshold.
    tx = ExpenseTransaction(
        user_id=USER, source_id=src_id, statement_id=stmt_id,
        occurred_on=AS_OF - timedelta(days=1),
        merchant_raw="GROCERY WORLD", merchant_normalized="grocery_world",
        amount_nis=Decimal("5000"),
        direction="debit", tx_type="regular",
        category_id=1,
        raw_row_json="{}",
    )
    db.add(tx); db.commit()

    # Act — invoke the wired hook directly.
    _run_anomaly_detectors(db, USER)
    db.commit()

    rows = list(db.execute(sa.select(ExpenseReviewQueue)).scalars().all())
    assert len(rows) >= 1, (
        f"expected at least one bucket to fire on the ₪5000 spike; got 0 rows"
    )
    # At least one should be a Bucket A row (amount-outlier).
    buckets = {r.bucket for r in rows}
    assert "amount" in buckets, (
        f"expected an 'amount' bucket fire (A2); got buckets={buckets}"
    )


# ---------------------------------------------------------------------------
# Test 2 — wiring: a real ``ingest_user_file`` call invokes
# ``_run_anomaly_detectors`` exactly once with (session, user_id).
# ---------------------------------------------------------------------------


def test_orchestrator_invokes_anomaly_detectors(db, tmp_path, monkeypatch):
    """Stub the parser dispatch so we can drive the orchestrator without a
    real bank file, then assert ``_run_anomaly_detectors`` was called.

    This test guards the wiring itself — without it a future refactor
    could quietly delete the ``_run_anomaly_detectors(...)`` call site
    and the direct test above would still pass."""
    from argosy.services.expense_ingest import orchestrator as orch_mod
    from argosy.services.expense_ingest.types import (
        NormalizedTransaction, ParseResult, ParserName,
        SourceHint, StatementMeta,
    )

    # Create a UserFile to ingest.
    storage = tmp_path / "stub.csv"
    storage.write_text("stub", encoding="utf-8")
    f = UserFile(
        user_id=USER, sha256="e" * 64,
        original_name="stub.csv", sanitized_name="stub.csv",
        mime_type="text/csv", kind="other",
        size_bytes=4, storage_path=str(storage),
        source="expense_statement",
    )
    db.add(f); db.commit()

    fake_result = ParseResult(
        statement=StatementMeta(
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            declared_total_nis=100.0,
            parsed_total_nis=100.0,
        ),
        transactions=[
            NormalizedTransaction(
                occurred_on=date(2026, 5, 15),
                merchant_raw="X", merchant_normalized="x",
                amount_nis=100.0,
                direction="debit", tx_type="regular",
                raw_row={},
            ),
        ],
        source_hint=SourceHint(
            kind="card", issuer="visa", external_id="0000",
            display_name="Visa 0000",
        ),
    )

    def _fake_parse(_path):
        return fake_result

    # Patch the parser dispatch + format detector to route to our fake.
    monkeypatch.setattr(
        orch_mod, "PARSER_DISPATCH",
        {ParserName.AMEX: _fake_parse},
    )
    monkeypatch.setattr(
        orch_mod, "detect_format", lambda _p: ParserName.AMEX,
    )
    # Avoid calling the real LLM-backed category resolver / refund
    # matcher / correlator — they're orthogonal to what this test
    # guards (the call site of _run_anomaly_detectors).
    monkeypatch.setattr(
        orch_mod, "resolve_categories_for_user", lambda *a, **kw: 0,
    )
    monkeypatch.setattr(
        orch_mod, "match_refunds_for_user", lambda *a, **kw: 0,
    )
    monkeypatch.setattr(
        orch_mod, "correlate_for_user", lambda *a, **kw: 0,
    )

    # Spy on _run_anomaly_detectors. Patch at the orchestrator-module
    # binding so the live call resolves to our spy.
    called: list[tuple] = []

    def _spy(session, user_id):
        called.append((session, user_id))

    monkeypatch.setattr(orch_mod, "_run_anomaly_detectors", _spy)

    result = orch_mod.ingest_user_file(db, USER, f.id)
    assert result.transactions_inserted == 1
    assert len(called) == 1, called
    assert called[0][1] == USER


# ---------------------------------------------------------------------------
# Test 3 — failure isolation: one detector raising does not abort the
# others nor break the ingest commit.
# ---------------------------------------------------------------------------


def test_one_detector_failure_does_not_abort_others(db):
    """Patch detect_bucket_a to raise; assert the remaining detectors
    still ran (no exception escapes) and that subsequent session work
    can still commit."""
    src_id, stmt_id = _seed_source_statement(db)
    tx = ExpenseTransaction(
        user_id=USER, source_id=src_id, statement_id=stmt_id,
        occurred_on=AS_OF - timedelta(days=1),
        merchant_raw="X", merchant_normalized="x",
        amount_nis=Decimal("100"),
        direction="debit", tx_type="regular",
        raw_row_json="{}",
    )
    db.add(tx); db.commit()

    def _boom(*_a, **_kw):
        raise RuntimeError("bucket a synthetic failure")

    # Spy on each of the other 4 detectors so we can verify they
    # were INVOKED even after bucket_a failed. Codex review of the
    # anomaly wiring (2026-05-30) explicitly asked for this assertion.
    invoked: dict[str, bool] = {
        "missing_recurring": False,
        "novel_merchants": False,
        "category_drift": False,
        "cross_card_duplicates": False,
    }

    def _spy(name: str):
        def _wrapped(*_a, **_kw):
            invoked[name] = True
        return _wrapped

    with patch(
        "argosy.services.anomaly.bucket_a.detect_bucket_a",
        side_effect=_boom,
    ), patch(
        "argosy.services.anomaly.bucket_b_recurring.detect_missing_recurring",
        side_effect=_spy("missing_recurring"),
    ), patch(
        "argosy.services.anomaly.bucket_c.detect_novel_merchants",
        side_effect=_spy("novel_merchants"),
    ), patch(
        "argosy.services.anomaly.bucket_c.detect_category_drift",
        side_effect=_spy("category_drift"),
    ), patch(
        "argosy.services.anomaly.bucket_d.detect_cross_card_duplicates",
        side_effect=_spy("cross_card_duplicates"),
    ):
        # Must NOT raise.
        _run_anomaly_detectors(db, USER)

    # Session must still be usable — commit a no-op to prove it.
    db.commit()

    # Codex BLOCKER fix: assert the 4 non-A detectors were each invoked
    # despite bucket_a's failure. Cross-detector isolation is the
    # load-bearing guarantee of the SAVEPOINT wrapper.
    assert all(invoked.values()), f"non-A detectors not all invoked: {invoked}"

    # Sanity: queue table is reachable.
    rows = list(db.execute(sa.select(ExpenseReviewQueue)).scalars().all())
    assert isinstance(rows, list)
