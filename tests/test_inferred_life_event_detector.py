"""Tests for the inferred-life-event detector (Spec E commit #5).

Coverage:

  * **Heuristic — tuition_stopped happy path** — 12 months of tuition
    payments + 6 months absence -> high-confidence finding.
  * **Heuristic — tuition_stopped FALSE-POSITIVE** — autopay-merchant
    switch (old payments stop, new merchant under same label appears
    within 90 days) gets caught by the continuity check: confidence
    downgraded to ``low`` (forces LLM disambiguation).
  * **Heuristic — recurring_large_auto** — 3 car-scale purchases at
    ~5y cadence -> recurring proposal (high confidence: stdev < 1y).
  * **Heuristic — wedding_scale_transfer** — single NIS 150k transfer
    with WEDDING_VENDOR counterparty -> medium-confidence finding.
  * **Heuristic — recurring_renovation** — cluster of 4 construction
    transactions within 60 days totalling NIS 60k -> finding.
  * **Heuristic — kid_started_college** — 8 months absence + 4 months
    presence -> high-confidence finding.
  * **Conflict resolver — aliased pair suppression** — tuition_stopped
    + kid_started_college on the SAME counterparty within overlapping
    windows: both suppressed (``conflict_resolution=
    'aliased_pair_suppressed'``, ``dismissed=True``).
  * **Conflict resolver — disambiguator required** — same pair on
    DIFFERENT counterparties but overlapping windows: both marked
    ``aliased_pair_disambiguator_required`` + ``needs_llm_
    disambiguation=True``.
  * **UNIQUE constraint** — re-running the detector on the same
    (user, pattern, window) is a no-op (IntegrityError caught + 0
    new rows).
  * **Shadow mode — new user** — user created < 30 days ago: ALL
    findings persisted with ``dismissed=True``, ``status='shadow'``
    semantics, NO action_proposals row written, NO proposer call.
  * **Shadow mode — old user** — user created > 30 days ago: findings
    fire the proposer.
  * **LLM disambiguator stub** — the proposer-runner injection seam
    works (proposer_runner kwarg gets invoked and can return a
    proposal id back into the finding row).
  * **Partner-change v1.1 stub** — returns empty list.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_inferred_life_event_detector.py -v
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.inferred_life_event_detector import (
    CONFLICT_PAIR_OVERLAP_DAYS,
    SHADOW_MODE_NEW_ACCOUNT_DAYS,
    HeuristicFinding,
    _counterparty_continuity_check,
    _detect_kid_started_college,
    _detect_partner_change,
    _detect_recurring_large_auto,
    _detect_recurring_renovation,
    _detect_tuition_stopped,
    _detect_wedding_scale_transfer,
    _resolve_shadow_mode,
    _run_pre_proposal_conflict_resolver,
    run_detector,
)
from argosy.state.models import (
    Base,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    InferredLifeEventFinding,
    User,
    UserFile,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path DB.

    ``Base.metadata.create_all`` installs the ORM-declared schema —
    including the natural-key UniqueConstraint on
    ``inferred_life_event_findings`` declared in models.py.
    """
    db_path = tmp_path / "inferred_detector.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    # Seed user (old user — > 30 days, so not in shadow mode by default).
    db.add(
        User(
            id=USER,
            plan="free",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    db.flush()
    # Seed minimal source + statement for FK satisfaction.  Matches
    # the fixture shape used by tests/test_merchant_rolling_stats.py.
    f = UserFile(
        user_id=USER,
        sha256="b" * 64,
        original_name="test.csv",
        sanitized_name="test.csv",
        mime_type="text/csv",
        kind="other",
        size_bytes=1,
        storage_path="/tmp/test",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER,
        kind="card",
        issuer="visa",
        external_id="1234",
        display_name="Visa 1234",
    )
    db.add(src)
    db.flush()
    stmt = ExpenseStatement(
        user_id=USER,
        source_id=src.id,
        file_id=f.id,
        period_start=date(2020, 1, 1),
        period_end=date(2027, 12, 31),
        parsed_total_nis=Decimal("1000"),
        declared_total_nis=Decimal("1000"),
        parser_name="visa",
        parser_version="0.1.0",
        status="parsed",
    )
    db.add(stmt)
    db.commit()
    try:
        yield db, src.id, stmt.id
    finally:
        db.close()
        engine.dispose()


def _now() -> datetime:
    return datetime(2026, 6, 1, 3, 0, 0, tzinfo=timezone.utc)


def _make_tx(
    *,
    source_id: int,
    statement_id: int,
    occurred_on: date,
    merchant: str,
    amount_nis: Decimal,
    direction: str = "debit",
    tx_type: str = "regular",
) -> ExpenseTransaction:
    return ExpenseTransaction(
        user_id=USER,
        statement_id=statement_id,
        source_id=source_id,
        occurred_on=occurred_on,
        merchant_raw=merchant,
        merchant_normalized=merchant.lower(),
        amount_nis=amount_nis,
        amount_orig=amount_nis,
        currency_orig="NIS",
        direction=direction,
        tx_type=tx_type,
        is_card_payment=False,
        raw_row_json="{}",
    )


# ---------------------------------------------------------------------------
# Heuristic — tuition_stopped
# ---------------------------------------------------------------------------


def test_tuition_stopped_happy_path(sync_session):
    """12 months of tuition payments + 6 months gap -> high finding."""
    session, src_id, stmt_id = sync_session
    # Seed 14 monthly tuition payments ending in November 2025.
    txs = []
    for i in range(14):
        month_offset = i
        d = date(2024, 9, 1) + timedelta(days=30 * month_offset)
        txs.append(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="ARIEL UNIVERSITY",
                amount_nis=Decimal("4500"),
            )
        )
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_tuition_stopped(
        loaded, window_start=date(2024, 9, 1), window_end=date(2026, 6, 1)
    )
    assert len(findings) == 1, findings
    f = findings[0]
    assert f.pattern == "tuition_stopped"
    assert f.heuristic_confidence == "high"
    assert f.counterparty_key == "ariel university"
    assert len(f.evidence_transaction_ids) == 14


def test_tuition_stopped_no_finding_below_threshold(sync_session):
    """Only 6 months of payments -> no finding."""
    session, src_id, stmt_id = sync_session
    txs = [
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2024, 9, 1) + timedelta(days=30 * i),
            merchant="ARIEL UNIVERSITY",
            amount_nis=Decimal("4500"),
        )
        for i in range(6)
    ]
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()
    findings = _detect_tuition_stopped(
        loaded, window_start=date(2024, 9, 1), window_end=date(2026, 6, 1)
    )
    assert findings == []


def test_tuition_stopped_continuity_check_downgrades(sync_session):
    """Autopay-merchant switch -> continuity check downgrades to low.

    Spec §5.4 guardrail (counterparty continuity).  The disappearing
    "ARIEL UNIVERSITY" stream re-appears under "ARIEL UNI POST"
    within 90 days -> downgrade to ``low`` (forces LLM
    disambiguation).
    """
    session, src_id, stmt_id = sync_session
    # 14 months of original counterparty.
    txs = []
    for i in range(14):
        d = date(2024, 9, 1) + timedelta(days=30 * i)
        txs.append(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="ARIEL UNIVERSITY",
                amount_nis=Decimal("4500"),
            )
        )
    # 3 months of NEW counterparty under same label family
    # ("university" still in the merchant string so the heuristic's
    # label matcher still considers it tuition-shaped — that's the
    # whole point of the continuity check, the OLD bank-merchant
    # field changed but the underlying label family is still tuition).
    last_old = txs[-1].occurred_on
    for j in range(3):
        d = last_old + timedelta(days=30 * (j + 1))
        txs.append(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="ARIEL UNIVERSITY (NEW BANK)",
                amount_nis=Decimal("4500"),
            )
        )
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_tuition_stopped(
        loaded, window_start=date(2024, 9, 1), window_end=date(2026, 6, 1)
    )
    # The original stream still produces a finding...
    matching = [f for f in findings if f.counterparty_key == "ariel university"]
    assert matching, "tuition_stopped should still fire on the old stream"
    f = matching[0]
    assert f.heuristic_confidence == "high"  # before continuity check
    # ...but the continuity check downgrades it.
    downgraded = _counterparty_continuity_check(f, loaded)
    assert downgraded is True
    assert f.heuristic_confidence == "low"
    assert f.needs_llm_disambiguation is True


# ---------------------------------------------------------------------------
# Heuristic — recurring_large_auto
# ---------------------------------------------------------------------------


def test_recurring_large_auto_three_priors_high_confidence(sync_session):
    """3 car-scale purchases at ~5y cadence -> high-confidence finding."""
    session, src_id, stmt_id = sync_session
    # Three purchases ~5 years apart, stdev < 1y.
    txs = [
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2016, 6, 1),
            merchant="TOYOTA DEALER TEL AVIV",
            amount_nis=Decimal("80000"),
        ),
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2021, 5, 15),
            merchant="HONDA AUTO RAANANA",
            amount_nis=Decimal("95000"),
        ),
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 4, 20),
            merchant="HYUNDAI DEALER NETANYA",
            amount_nis=Decimal("110000"),
        ),
    ]
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_recurring_large_auto(
        loaded, window_start=date(2016, 1, 1), window_end=date(2026, 6, 1)
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.pattern == "recurring_car_purchase"
    assert f.heuristic_confidence == "high"


# ---------------------------------------------------------------------------
# Heuristic — wedding_scale_transfer
# ---------------------------------------------------------------------------


def test_wedding_scale_transfer_medium_confidence(sync_session):
    """Single NIS 150k transfer with WEDDING counterparty -> medium."""
    session, src_id, stmt_id = sync_session
    txs = [
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 3, 15),
            merchant="WEDDING VENDOR LTD",
            amount_nis=Decimal("150000"),
            tx_type="transfer",
        ),
    ]
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_wedding_scale_transfer(
        loaded, window_start=date(2025, 1, 1), window_end=date(2026, 6, 1)
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.pattern == "wedding_scale_transfer"
    assert f.heuristic_confidence == "medium"
    assert f.needs_llm_disambiguation is True


def test_wedding_scale_transfer_below_100k_no_finding(sync_session):
    """Below the NIS 100k floor -> no finding fired.

    Codex BLOCKER #1 from the spec-E-5 review: any lower threshold
    widens the false-positive surface without adding signal.  The
    heuristic must enforce a strict NIS 100k floor per spec §5.3
    row 3.
    """
    session, src_id, stmt_id = sync_session
    txs = [
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 3, 15),
            merchant="WEDDING VENDOR",
            amount_nis=Decimal("85000"),
            tx_type="transfer",
        ),
    ]
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_wedding_scale_transfer(
        loaded, window_start=date(2025, 1, 1), window_end=date(2026, 6, 1)
    )
    assert findings == [], (
        "wedding_scale_transfer at NIS 85k must NOT fire — strict "
        "NIS 100k floor per spec §5.3 (codex BLOCKER #1 from "
        "spec-E-5 review)"
    )


# ---------------------------------------------------------------------------
# Heuristic — recurring_renovation
# ---------------------------------------------------------------------------


def test_recurring_renovation_cluster_detected(sync_session):
    """4 construction transactions within 60 days >= NIS 50k -> finding."""
    session, src_id, stmt_id = sync_session
    txs = [
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 1, 5),
            merchant="CONSTRUCTION CO",
            amount_nis=Decimal("18000"),
        ),
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 1, 25),
            merchant="PLUMBER PRO",
            amount_nis=Decimal("12000"),
        ),
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 2, 10),
            merchant="ELECTRICIAN ELI",
            amount_nis=Decimal("15000"),
        ),
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 3, 1),
            merchant="TILES STORE",
            amount_nis=Decimal("22000"),
        ),
    ]
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_recurring_renovation(
        loaded, window_start=date(2025, 1, 1), window_end=date(2026, 6, 1)
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.pattern == "recurring_renovation"
    assert f.heuristic_confidence == "medium"
    assert len(f.evidence_transaction_ids) == 4


# ---------------------------------------------------------------------------
# Heuristic — kid_started_college
# ---------------------------------------------------------------------------


def test_kid_started_college_appearance_after_absence(sync_session):
    """6 months absence + 4 months presence -> high-confidence finding."""
    session, src_id, stmt_id = sync_session
    # NO tuition payments before April 2026 in the 12-month window.
    # Then 4 monthly payments to UNIVERSITY ending May 2026.
    txs = []
    for i in range(4):
        d = date(2026, 2, 1) + timedelta(days=30 * i)
        txs.append(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="TEL AVIV UNIVERSITY",
                amount_nis=Decimal("6000"),
            )
        )
    session.add_all(txs)
    session.commit()
    loaded = session.query(ExpenseTransaction).all()

    findings = _detect_kid_started_college(
        loaded, window_start=date(2025, 6, 1), window_end=date(2026, 6, 1)
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.pattern == "kid_started_college"
    assert f.heuristic_confidence == "high"


# ---------------------------------------------------------------------------
# Conflict resolver (codex BLOCKER #3)
# ---------------------------------------------------------------------------


def test_conflict_resolver_aliased_pair_same_counterparty():
    """Same counterparty + overlap -> both suppressed."""
    f1 = HeuristicFinding(
        pattern="tuition_stopped",
        heuristic_confidence="high",
        evidence_window_start=date(2024, 9, 1),
        evidence_window_end=date(2025, 12, 1),
        evidence_transaction_ids=[1, 2, 3],
        evidence_summary="...",
        counterparty_key="bank of israel education",
    )
    f2 = HeuristicFinding(
        pattern="kid_started_college",
        heuristic_confidence="high",
        evidence_window_start=date(2026, 1, 1),
        evidence_window_end=date(2026, 5, 1),
        evidence_transaction_ids=[4, 5, 6],
        evidence_summary="...",
        counterparty_key="bank of israel education",  # SAME
    )

    resolved = _run_pre_proposal_conflict_resolver([f1, f2])
    assert resolved == 2
    assert f1.conflict_resolution == "aliased_pair_suppressed"
    assert f2.conflict_resolution == "aliased_pair_suppressed"


def test_conflict_resolver_aliased_pair_different_counterparty():
    """Overlap without shared counterparty -> LLM disambiguator required."""
    f1 = HeuristicFinding(
        pattern="tuition_stopped",
        heuristic_confidence="high",
        evidence_window_start=date(2024, 9, 1),
        evidence_window_end=date(2025, 12, 1),
        evidence_transaction_ids=[1, 2, 3],
        evidence_summary="...",
        counterparty_key="hebrew university",
    )
    f2 = HeuristicFinding(
        pattern="kid_started_college",
        heuristic_confidence="high",
        evidence_window_start=date(2026, 1, 1),
        evidence_window_end=date(2026, 5, 1),
        evidence_transaction_ids=[4, 5, 6],
        evidence_summary="...",
        counterparty_key="tel aviv university",  # DIFFERENT
    )

    resolved = _run_pre_proposal_conflict_resolver([f1, f2])
    assert resolved == 2
    assert f1.conflict_resolution == "aliased_pair_disambiguator_required"
    assert f2.conflict_resolution == "aliased_pair_disambiguator_required"
    assert f1.needs_llm_disambiguation is True
    assert f2.needs_llm_disambiguation is True


def test_conflict_resolver_no_overlap_no_action():
    """Pair without window overlap -> no resolution."""
    f1 = HeuristicFinding(
        pattern="tuition_stopped",
        heuristic_confidence="high",
        evidence_window_start=date(2020, 1, 1),
        evidence_window_end=date(2021, 1, 1),
        evidence_transaction_ids=[1],
        evidence_summary="...",
        counterparty_key="cp1",
    )
    f2 = HeuristicFinding(
        pattern="kid_started_college",
        heuristic_confidence="high",
        evidence_window_start=date(2026, 1, 1),
        evidence_window_end=date(2026, 5, 1),
        evidence_transaction_ids=[2],
        evidence_summary="...",
        counterparty_key="cp1",
    )
    resolved = _run_pre_proposal_conflict_resolver([f1, f2])
    assert resolved == 0
    assert f1.conflict_resolution is None
    assert f2.conflict_resolution is None


# ---------------------------------------------------------------------------
# Orchestrator — shadow-mode + UNIQUE-constraint + proposer fan-out
# ---------------------------------------------------------------------------


def _seed_high_confidence_tuition_stopped(session, src_id, stmt_id):
    """Helper — seed the txs that fire a high-confidence
    tuition_stopped finding."""
    txs = []
    for i in range(14):
        d = date(2024, 9, 1) + timedelta(days=30 * i)
        txs.append(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="ARIEL UNIVERSITY",
                amount_nis=Decimal("4500"),
            )
        )
    session.add_all(txs)
    session.commit()


def test_run_detector_old_user_fires_proposer(sync_session):
    """Old user (> 30 days) -> proposer called for non-dismissed
    findings."""
    session, src_id, stmt_id = sync_session
    _seed_high_confidence_tuition_stopped(session, src_id, stmt_id)

    proposer_calls: list[dict[str, Any]] = []

    async def stub_proposer(
        sess, *, inferred_event, user_id, **_extras
    ):
        proposer_calls.append(
            {
                "finding_id": inferred_event.id,
                "pattern": inferred_event.pattern,
                "user_id": user_id,
            }
        )
        # Return a fake proposal id (the orchestrator binds it back).
        return 999

    summary = run_detector(
        session,
        USER,
        lookback_days=720,
        now=_now(),
        proposer_runner=stub_proposer,
    )

    assert summary.shadow_mode is False, summary
    assert summary.findings_total >= 1, summary
    assert summary.findings_proposed >= 1, summary
    assert proposer_calls, "proposer should have been called"
    assert proposer_calls[0]["pattern"] == "tuition_stopped"

    # The finding row should have the proposal id wired back.
    row = (
        session.query(InferredLifeEventFinding)
        .filter_by(user_id=USER, pattern="tuition_stopped")
        .one()
    )
    assert row.proposed_action_id == 999


def test_run_detector_shadow_mode_for_new_user(sync_session):
    """New user (< 30 days) -> all findings dismissed + no proposer call."""
    session, src_id, stmt_id = sync_session
    _seed_high_confidence_tuition_stopped(session, src_id, stmt_id)

    # Move the user's created_at to "5 days ago" relative to _now().
    user = session.query(User).filter_by(id=USER).one()
    user.created_at = _now() - timedelta(days=5)
    session.commit()

    proposer_calls: list[dict[str, Any]] = []

    async def stub_proposer(sess, *, inferred_event, user_id, **_extras):
        proposer_calls.append({"id": inferred_event.id})
        return 123

    summary = run_detector(
        session,
        USER,
        lookback_days=720,
        now=_now(),
        proposer_runner=stub_proposer,
    )

    assert summary.shadow_mode is True
    assert summary.findings_total >= 1
    assert summary.findings_shadow >= 1
    assert summary.findings_proposed == 0
    assert proposer_calls == [], (
        "proposer must NOT fire in shadow mode (per Ariel's locked "
        "decision in spec §5.4)"
    )

    # Verify ALL persisted findings are dismissed.
    rows = session.query(InferredLifeEventFinding).all()
    assert rows
    assert all(r.dismissed for r in rows)
    assert all(r.proposed_action_id is None for r in rows)


def test_run_detector_unique_constraint_idempotent_redetect(sync_session):
    """Re-running detector on same window -> no duplicate rows."""
    session, src_id, stmt_id = sync_session
    _seed_high_confidence_tuition_stopped(session, src_id, stmt_id)

    async def stub_proposer(sess, *, inferred_event, user_id, **_extras):
        return None

    summary1 = run_detector(
        session,
        USER,
        lookback_days=720,
        now=_now(),
        shadow_mode=True,  # short-circuit proposer for stability
        proposer_runner=stub_proposer,
    )
    first_count = session.query(InferredLifeEventFinding).count()
    assert first_count >= 1

    summary2 = run_detector(
        session,
        USER,
        lookback_days=720,
        now=_now(),
        shadow_mode=True,
        proposer_runner=stub_proposer,
    )
    second_count = session.query(InferredLifeEventFinding).count()
    assert second_count == first_count, (
        "UNIQUE(user_id, pattern, window) should make re-detection a "
        "no-op; rows changed from "
        f"{first_count} to {second_count}"
    )


def test_run_detector_aliased_pair_suppressed():
    """Pre-proposal conflict resolver suppresses aliased pairs.

    This is the codex BLOCKER #3 contract: tuition_stopped +
    kid_started_college on the SAME counterparty within overlapping
    evidence windows MUST be suppressed (re-categorisation masked as
    life event).  Verified at the resolver level here because the
    DB-level orchestrator path requires real expense_transactions
    matching BOTH heuristics simultaneously on the same counterparty
    — the tuition_stopped heuristic uses ``last = txs[-1].occurred_on``
    so a single counterparty's stream cannot fire BOTH stop+start at
    the orchestrator level (which is the correct design).  The
    resolver-level test pins the suppression invariant without
    relying on a fixture that fires both heuristics naturally.
    """
    f_stop = HeuristicFinding(
        pattern="tuition_stopped",
        heuristic_confidence="high",
        evidence_window_start=date(2024, 9, 1),
        evidence_window_end=date(2025, 9, 1),
        evidence_transaction_ids=[1, 2, 3],
        evidence_summary="...",
        counterparty_key="ariel university",
    )
    f_start = HeuristicFinding(
        pattern="kid_started_college",
        heuristic_confidence="high",
        evidence_window_start=date(2025, 11, 1),  # within 90d of stop
        evidence_window_end=date(2026, 6, 1),
        evidence_transaction_ids=[4, 5, 6],
        evidence_summary="...",
        counterparty_key="ariel university",  # SAME counterparty
    )

    resolved = _run_pre_proposal_conflict_resolver([f_stop, f_start])
    assert resolved == 2
    assert f_stop.conflict_resolution == "aliased_pair_suppressed"
    assert f_start.conflict_resolution == "aliased_pair_suppressed"
    # Once suppressed, the orchestrator MUST persist both as dismissed
    # (it reads ``conflict_resolution == 'aliased_pair_suppressed'`` to
    # flip the dismissed flag before insertion).  The non-DB
    # contract this test pins is: the resolver mutates BOTH findings,
    # not just one — preventing the "only one of the pair was
    # suppressed and the other still proposed" bug from spec §5.4.


# ---------------------------------------------------------------------------
# Shadow-mode resolver
# ---------------------------------------------------------------------------


def test_resolve_shadow_mode_new_user_returns_true(sync_session):
    session, _, _ = sync_session
    user = session.query(User).filter_by(id=USER).one()
    user.created_at = _now() - timedelta(days=5)
    session.commit()
    assert (
        _resolve_shadow_mode(
            session, USER, now=_now(), shadow_mode_override=None
        )
        is True
    )


def test_resolve_shadow_mode_old_user_returns_false(sync_session):
    session, _, _ = sync_session
    user = session.query(User).filter_by(id=USER).one()
    user.created_at = _now() - timedelta(
        days=SHADOW_MODE_NEW_ACCOUNT_DAYS + 5
    )
    session.commit()
    assert (
        _resolve_shadow_mode(
            session, USER, now=_now(), shadow_mode_override=None
        )
        is False
    )


def test_resolve_shadow_mode_override_wins(sync_session):
    session, _, _ = sync_session
    # User old (would normally be FALSE).
    user = session.query(User).filter_by(id=USER).one()
    user.created_at = _now() - timedelta(days=400)
    session.commit()
    # Override flips it.
    assert (
        _resolve_shadow_mode(
            session, USER, now=_now(), shadow_mode_override=True
        )
        is True
    )


# ---------------------------------------------------------------------------
# Partner-change v1.1 stub
# ---------------------------------------------------------------------------


def test_partner_change_returns_empty(sync_session):
    session, src_id, stmt_id = sync_session
    # Any transactions — the stub returns [] unconditionally.
    session.add(
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 3, 1),
            merchant="WEDDING VENDOR",
            amount_nis=Decimal("150000"),
        )
    )
    session.commit()
    loaded = session.query(ExpenseTransaction).all()
    assert (
        _detect_partner_change(
            loaded,
            window_start=date(2025, 1, 1),
            window_end=date(2026, 6, 1),
        )
        == []
    )


# ---------------------------------------------------------------------------
# LLM disambiguator seam — mocked proposer returns dismissal signal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# False-positive control over a synthetic year of "normal" transactions
# (codex BLOCKER #2 from the spec-E-5 review)
# ---------------------------------------------------------------------------


def test_false_positive_control_normal_year(sync_session):
    """A synthetic year of "normal" transactions MUST NOT fire most
    heuristics.

    Codex BLOCKER #2 from the spec-E-5 review: without this gate the
    commit cannot demonstrate the spec §5.4 false-positive-control
    contract.  The fixture mimics a typical user's transaction stream:

    * Recurring grocery + utility payments (NOT life-event-shaped).
    * One mid-size NIS 30k transfer (below ALL scale thresholds).
    * Two NIS 20k car-repair transactions (above neither the car-scale
      NIS 60k threshold nor the renovation NIS 50k cluster threshold).
    * Recurring kindergarten payment ONLY 4 months long (below tuition
      12-month prior-count threshold + still active, so no gap).

    Expected: ZERO findings from all six heuristics.  If the detector
    fires anything on this fixture, the heuristics are too loose for
    the 12-month-backfill gate (spec §5.7).
    """
    session, src_id, stmt_id = sync_session
    # 12 months of grocery payments — NOT life-event-shaped.
    for i in range(52):
        d = date(2025, 6, 1) + timedelta(days=7 * i)
        session.add(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="SUPER YOHANANOF",
                amount_nis=Decimal("450"),
            )
        )
    # Monthly utility bills.
    for i in range(12):
        d = date(2025, 6, 15) + timedelta(days=30 * i)
        session.add(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="ELECTRIC COMPANY",
                amount_nis=Decimal("600"),
            )
        )
    # Single mid-size transfer (below wedding/car thresholds).
    session.add(
        _make_tx(
            source_id=src_id,
            statement_id=stmt_id,
            occurred_on=date(2026, 1, 15),
            merchant="FAMILY MEMBER",
            amount_nis=Decimal("30000"),
            tx_type="transfer",
        )
    )
    # Two car-repair transactions WAY below the NIS 60k car-purchase
    # threshold AND no renovation/wedding labels.
    for amt, day in [(Decimal("20000"), 60), (Decimal("18000"), 120)]:
        session.add(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=date(2025, 8, 1) + timedelta(days=day),
                merchant="AUTO REPAIR SHOP",
                amount_nis=amt,
            )
        )
    # Recurring kindergarten payment — only 4 months, below the
    # 12-month prior-count threshold for tuition_stopped.  Also it's
    # STILL ACTIVE through May 2026 so no gap exists.
    for i in range(4):
        d = date(2026, 2, 1) + timedelta(days=30 * i)
        session.add(
            _make_tx(
                source_id=src_id,
                statement_id=stmt_id,
                occurred_on=d,
                merchant="HAPPY KINDERGARTEN",
                amount_nis=Decimal("3500"),
            )
        )
    session.commit()

    proposer_calls: list[str] = []

    async def stub_proposer(sess, *, inferred_event, user_id, **_extras):
        proposer_calls.append(inferred_event.pattern)
        return None

    summary = run_detector(
        session,
        USER,
        lookback_days=720,
        now=_now(),
        proposer_runner=stub_proposer,
    )

    assert summary.findings_total == 0, (
        f"detector fired {summary.findings_total} findings on a "
        f"synthetic year of NORMAL transactions — heuristics are too "
        f"loose. Summary={summary.to_dict()}"
    )
    assert proposer_calls == []
    rows = session.query(InferredLifeEventFinding).all()
    assert rows == []


# ---------------------------------------------------------------------------
# LLM disambiguator seam — mocked proposer returns dismissal signal
# ---------------------------------------------------------------------------


def test_llm_disambiguator_seam_via_proposer_runner(sync_session):
    """The proposer-runner injection seam works — a stub can return
    None (e.g. "LLM dismissed this") and the orchestrator handles it
    gracefully without writing a proposal id back."""
    session, src_id, stmt_id = sync_session
    _seed_high_confidence_tuition_stopped(session, src_id, stmt_id)

    async def dismissing_proposer(sess, *, inferred_event, user_id, **_extras):
        # Stub: the runner inspected the finding + decided no proposal.
        return None

    summary = run_detector(
        session,
        USER,
        lookback_days=720,
        now=_now(),
        proposer_runner=dismissing_proposer,
    )
    assert summary.proposer_calls >= 1
    rows = (
        session.query(InferredLifeEventFinding)
        .filter_by(pattern="tuition_stopped")
        .all()
    )
    assert rows
    # The orchestrator should NOT have set proposed_action_id because
    # the stub returned None.
    assert all(r.proposed_action_id is None for r in rows)
