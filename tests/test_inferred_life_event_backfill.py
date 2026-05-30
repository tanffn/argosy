"""Empirical backfill verification for the inferred-life-event detector
(Spec E commit #9 — FINAL of Sprint E).

Per ``docs/superpowers/specs/2026-05-29-last-mile-delivery-design.md``
§9 commit #9 + §5.7 / §5.6 + the five-guardrail false-positive control
in §5.4: the merge gate for the entire detector subsystem is empirical
— "if detector fires garbage, don't merge". This test loads
fixture-driven 12-month-extended transaction streams, runs the live
``run_detector`` against them, and asserts:

  * **Normal year** (``normal_year.json``) — typical household stream
    of groceries / utilities / mortgage / 4-month-kindergarten-then-
    -stop / car-service / annual-insurance MUST fire ZERO findings.
    This is the spec §9 commit #9 merge-gate assertion.

  * **Tuition stop** (``tuition_stop_scenario.json``) — 14 months of
    college tuition then a 6-month-plus gap MUST surface at least one
    ``tuition_stopped`` finding.

  * **Recurring car** (``recurring_car_scenario.json``) — 3 car-scale
    purchases at ~5y cadence MUST surface at least one
    ``recurring_car_purchase`` finding.

  * **Wedding** (``wedding_scenario.json``) — single NIS 150k wedding-
    vendor transfer MUST surface at least one ``wedding_scale_transfer``
    finding.

  * **Kindergarten only** (``kindergarten_only.json``) — 9 months of
    active kindergarten payments MUST NOT fire ``kid_started_college``
    (the spec D #5 BLOCKER: kindergarten is in the tuition pattern set
    for ``tuition_stopped`` but NOT in the college-only pattern set
    for ``kid_started_college``).

  * **Aggregate** — across all 5 fixtures, every fixture's
    ``patterns_forbidden`` entry MUST NOT appear in that fixture's
    findings (the per-fixture "no false-positive" contract), AND the
    normal-year + kindergarten-only fixtures' ``findings_total`` MUST
    be exactly zero (the spec §9 commit #9 merge-gate).

Tests run under ``pytest -m "not llm_eval"`` (no LLM calls — proposer
is mocked via the ``proposer_runner`` injection seam already proven by
``tests/test_inferred_life_event_detector.py``).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.inferred_life_event_detector import (
    DetectorSummary,
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

#: Anchor clock matches every fixture's ``anchor_date`` (2026-06-01).
#: Pinned so the heuristic windows + the conflict resolver are
#: deterministic across machines.
ANCHOR_NOW: datetime = datetime(2026, 6, 1, 3, 0, 0, tzinfo=timezone.utc)

#: Directory containing the fixture JSON files.
FIXTURE_DIR: Path = (
    Path(__file__).parent / "fixtures" / "inferred_event_backfill"
)

#: All fixtures the parametrised tests iterate.
ALL_FIXTURE_NAMES: tuple[str, ...] = (
    "normal_year",
    "tuition_stop_scenario",
    "recurring_car_scenario",
    "wedding_scenario",
    "kindergarten_only",
)


# ---------------------------------------------------------------------------
# Fixture-loading helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict[str, Any]:
    """Load + minimally validate one fixture JSON file."""
    path = FIXTURE_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"inferred_event_backfill: fixture not found at {path}. "
            f"Ensure tests/fixtures/inferred_event_backfill/{name}.json "
            f"exists."
        )
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    for required in ("name", "anchor_date", "lookback_days",
                     "transactions", "expectations"):
        if required not in data:
            raise ValueError(
                f"fixture {name!r} missing top-level key {required!r}; "
                f"got keys={list(data.keys())}"
            )
    return data


@pytest.fixture
def detector_session(tmp_path):
    """Sync sqlite Session bound to an isolated tmp_path DB.

    Seeds the minimum FK chain (User + UserFile + ExpenseSource +
    ExpenseStatement) the ``ExpenseTransaction`` row needs.  The user
    is intentionally old (Jan 2025) so the detector runs in
    NON-shadow mode by default — the backfill gate tests the
    user-visible code path.
    """
    db_path = tmp_path / "inferred_backfill.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(
        User(
            id=USER,
            plan="free",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    )
    db.flush()
    f = UserFile(
        user_id=USER,
        sha256="b" * 64,
        original_name="backfill.csv",
        sanitized_name="backfill.csv",
        mime_type="text/csv",
        kind="other",
        size_bytes=1,
        storage_path="/tmp/backfill",
        source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER,
        kind="card",
        issuer="visa",
        external_id="0000",
        display_name="Visa Backfill",
    )
    db.add(src)
    db.flush()
    stmt = ExpenseStatement(
        user_id=USER,
        source_id=src.id,
        file_id=f.id,
        period_start=date(2010, 1, 1),
        period_end=date(2027, 12, 31),
        parsed_total_nis=Decimal("1"),
        declared_total_nis=Decimal("1"),
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


def _seed_fixture_transactions(
    session,
    *,
    source_id: int,
    statement_id: int,
    fixture: dict[str, Any],
) -> int:
    """Insert all transactions from a fixture into the DB.  Returns the
    number of rows inserted."""
    rows = fixture["transactions"]
    for r in rows:
        merchant = r["merchant"]
        session.add(
            ExpenseTransaction(
                user_id=USER,
                statement_id=statement_id,
                source_id=source_id,
                occurred_on=date.fromisoformat(r["occurred_on"]),
                merchant_raw=merchant,
                merchant_normalized=merchant.lower(),
                amount_nis=Decimal(str(r["amount_nis"])),
                amount_orig=Decimal(str(r["amount_nis"])),
                currency_orig="NIS",
                direction=r.get("direction", "debit"),
                tx_type=r.get("tx_type", "regular"),
                is_card_payment=False,
                raw_row_json="{}",
            )
        )
    session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Shared detector-run helper
# ---------------------------------------------------------------------------


def _run_detector_with_stub_proposer(
    session,
    *,
    lookback_days: int,
) -> tuple[DetectorSummary, list[str]]:
    """Run the detector with a noop stub proposer.

    Returns ``(summary, proposer_call_patterns)`` so tests can assert
    against both the in-memory ``DetectorSummary`` AND the patterns the
    runner would have fired the proposer for.
    """
    proposer_calls: list[str] = []

    async def stub_proposer(sess, *, inferred_event, user_id, **_extras):
        proposer_calls.append(inferred_event.pattern)
        return None

    summary = run_detector(
        session,
        USER,
        lookback_days=lookback_days,
        now=ANCHOR_NOW,
        proposer_runner=stub_proposer,
    )
    return summary, proposer_calls


def _findings_by_pattern(session) -> dict[str, list[InferredLifeEventFinding]]:
    """Group persisted findings by pattern for assertion clarity."""
    rows = session.query(InferredLifeEventFinding).all()
    grouped: dict[str, list[InferredLifeEventFinding]] = {}
    for r in rows:
        grouped.setdefault(r.pattern, []).append(r)
    return grouped


# ---------------------------------------------------------------------------
# Per-fixture tests
# ---------------------------------------------------------------------------


def test_normal_year_fires_zero_findings(detector_session):
    """SPEC §9 COMMIT #9 MERGE GATE — normal_year MUST fire 0 findings.

    Twelve months of typical household transactions (grocery, utility,
    mortgage, 4-month kindergarten that's still active, occasional
    car-service, annual insurance) must NOT trip ANY of the six
    heuristics.  If this assertion fails, the heuristic thresholds are
    too loose for the spec §5.4 false-positive control + the spec §9
    commit #9 empirical-proof-gate and the detector commits MUST be
    reverted before re-merge (spec §5.7 / §9).
    """
    session, src_id, stmt_id = detector_session
    fixture = _load_fixture("normal_year")
    _seed_fixture_transactions(
        session, source_id=src_id, statement_id=stmt_id, fixture=fixture
    )

    summary, proposer_calls = _run_detector_with_stub_proposer(
        session, lookback_days=fixture["lookback_days"]
    )

    assert summary.findings_total == 0, (
        f"NORMAL-YEAR MERGE GATE FAILED — detector fired "
        f"{summary.findings_total} findings on a synthetic year of "
        f"typical household transactions. Per spec §9 commit #9, this "
        f"means the detector is too noisy and the sprint must NOT "
        f"merge. Summary={summary.to_dict()}, "
        f"persisted_patterns={list(_findings_by_pattern(session).keys())}"
    )
    assert proposer_calls == [], (
        f"NORMAL-YEAR MERGE GATE FAILED — proposer would have fired "
        f"for patterns={proposer_calls}. Expected zero calls."
    )
    assert session.query(InferredLifeEventFinding).count() == 0


def test_tuition_stop_fires_tuition_stopped(detector_session):
    """Tuition_stop_scenario MUST fire >= 1 tuition_stopped finding."""
    session, src_id, stmt_id = detector_session
    fixture = _load_fixture("tuition_stop_scenario")
    _seed_fixture_transactions(
        session, source_id=src_id, statement_id=stmt_id, fixture=fixture
    )

    summary, proposer_calls = _run_detector_with_stub_proposer(
        session, lookback_days=fixture["lookback_days"]
    )

    grouped = _findings_by_pattern(session)
    assert summary.findings_total >= 1, (
        f"tuition_stop fixture fired 0 findings; expected >= 1 "
        f"tuition_stopped. Summary={summary.to_dict()}"
    )
    assert "tuition_stopped" in grouped, (
        f"Expected a tuition_stopped finding; got patterns="
        f"{list(grouped.keys())}"
    )
    # Forbidden patterns from the fixture MUST not appear.
    for forbidden in fixture["expectations"]["patterns_forbidden"]:
        assert forbidden not in grouped, (
            f"tuition_stop fixture leaked false-positive {forbidden!r} "
            f"finding(s); spec §5.4 false-positive control violation."
        )


def test_recurring_car_fires_recurring_car_purchase(detector_session):
    """Recurring_car_scenario MUST fire >= 1 recurring_car_purchase
    finding."""
    session, src_id, stmt_id = detector_session
    fixture = _load_fixture("recurring_car_scenario")
    _seed_fixture_transactions(
        session, source_id=src_id, statement_id=stmt_id, fixture=fixture
    )

    summary, proposer_calls = _run_detector_with_stub_proposer(
        session, lookback_days=fixture["lookback_days"]
    )

    grouped = _findings_by_pattern(session)
    assert "recurring_car_purchase" in grouped, (
        f"Expected a recurring_car_purchase finding; got patterns="
        f"{list(grouped.keys())}, summary={summary.to_dict()}"
    )
    for forbidden in fixture["expectations"]["patterns_forbidden"]:
        assert forbidden not in grouped, (
            f"recurring_car fixture leaked false-positive "
            f"{forbidden!r} finding(s); spec §5.4 violation."
        )


def test_wedding_fires_wedding_scale_transfer(detector_session):
    """Wedding_scenario MUST fire >= 1 wedding_scale_transfer."""
    session, src_id, stmt_id = detector_session
    fixture = _load_fixture("wedding_scenario")
    _seed_fixture_transactions(
        session, source_id=src_id, statement_id=stmt_id, fixture=fixture
    )

    summary, proposer_calls = _run_detector_with_stub_proposer(
        session, lookback_days=fixture["lookback_days"]
    )

    grouped = _findings_by_pattern(session)
    assert "wedding_scale_transfer" in grouped, (
        f"Expected a wedding_scale_transfer finding; got patterns="
        f"{list(grouped.keys())}, summary={summary.to_dict()}"
    )
    for forbidden in fixture["expectations"]["patterns_forbidden"]:
        assert forbidden not in grouped, (
            f"wedding fixture leaked false-positive {forbidden!r} "
            f"finding(s); spec §5.4 violation."
        )


def test_kindergarten_only_does_not_fire_college_or_tuition(detector_session):
    """Kindergarten_only MUST fire ZERO kid_started_college findings.

    This is the spec D #5 / codex BLOCKER #2 contract: kindergarten is
    in the broad tuition pattern set (so an existing kindergarten
    enrollment ending fires tuition_stopped — correct) but NOT in the
    strict college-only pattern set, so a NEW kindergarten enrollment
    must NEVER fire kid_started_college (a 3yo entering kindergarten
    is not the same life-event-phase change as an 18yo entering
    college).
    """
    session, src_id, stmt_id = detector_session
    fixture = _load_fixture("kindergarten_only")
    _seed_fixture_transactions(
        session, source_id=src_id, statement_id=stmt_id, fixture=fixture
    )

    summary, proposer_calls = _run_detector_with_stub_proposer(
        session, lookback_days=fixture["lookback_days"]
    )

    grouped = _findings_by_pattern(session)
    assert "kid_started_college" not in grouped, (
        f"BLOCKER: kindergarten_only fixture fired kid_started_college "
        f"— violates spec D #5 / codex BLOCKER #2 pattern-set split. "
        f"Findings={[(r.pattern, r.evidence_summary) for r in grouped.get('kid_started_college', [])]}"
    )
    assert "tuition_stopped" not in grouped, (
        f"kindergarten_only fixture fired tuition_stopped — the 9-month "
        f"stream is still ACTIVE through May 2026 (no gap) AND prior "
        f"count is below the 12-month threshold. "
        f"Findings={[(r.pattern, r.evidence_summary) for r in grouped.get('tuition_stopped', [])]}"
    )
    assert summary.findings_total == 0, (
        f"kindergarten_only fixture fired {summary.findings_total} "
        f"findings; expected 0. Patterns={list(grouped.keys())}, "
        f"summary={summary.to_dict()}"
    )


# ---------------------------------------------------------------------------
# Aggregate false-positive control across all 5 fixtures
# ---------------------------------------------------------------------------


def _run_one_fixture(
    detector_session_factory,
    name: str,
) -> tuple[DetectorSummary, dict[str, list[InferredLifeEventFinding]],
           dict[str, Any]]:
    """Helper for the aggregate test: run a single fixture against a
    NEW DB session + return the per-fixture artefacts."""
    fixture = _load_fixture(name)
    # Drive the fixture set-up the same way the per-fixture tests do,
    # but on a brand-new in-memory DB so cross-fixture state doesn't
    # leak.  We reuse the seeding helpers.
    session, src_id, stmt_id = detector_session_factory
    _seed_fixture_transactions(
        session, source_id=src_id, statement_id=stmt_id, fixture=fixture
    )
    summary, _ = _run_detector_with_stub_proposer(
        session, lookback_days=fixture["lookback_days"]
    )
    grouped = _findings_by_pattern(session)
    return summary, grouped, fixture


@pytest.mark.parametrize("fixture_name", ALL_FIXTURE_NAMES)
def test_aggregate_false_positive_control(detector_session, fixture_name):
    """For every fixture: no ``patterns_forbidden`` entry surfaces.

    The aggregate gate per spec §5.7 + §9: the wedding-NIS-20k anti-
    pattern (a too-low wedding floor would have fired on normal_year's
    NIS 30k family transfer) MUST NOT fire on normal_year — that's the
    strict-NIS-100k-floor contract from codex BLOCKER #1.  We pin the
    contract per-fixture rather than only on normal_year so any future
    fixture that gains a 'patterns_forbidden' entry inherits the same
    gate without test churn.
    """
    summary, grouped, fixture = _run_one_fixture(
        detector_session, fixture_name
    )

    forbidden = list(fixture["expectations"].get("patterns_forbidden", []))
    leaked: dict[str, int] = {}
    for f in forbidden:
        if f in grouped:
            leaked[f] = len(grouped[f])

    assert not leaked, (
        f"FALSE-POSITIVE CONTROL VIOLATION on fixture {fixture_name!r}: "
        f"forbidden patterns leaked: {leaked}. "
        f"Spec §5.4 + §9 commit #9 contract is that the detector MUST "
        f"NOT fire transparently-false-positive findings on these "
        f"synthetic streams.  Summary={summary.to_dict()}, "
        f"all_patterns={list(grouped.keys())}"
    )

    # Per-fixture findings_total band check (the wedding/tuition/car
    # fixtures cap at 3 to catch heuristic-storm regressions).
    fmin = fixture["expectations"].get("findings_total_min", 0)
    fmax = fixture["expectations"].get("findings_total_max", 9999)
    assert fmin <= summary.findings_total <= fmax, (
        f"findings_total for fixture {fixture_name!r} = "
        f"{summary.findings_total}; expected band [{fmin}, {fmax}]. "
        f"Patterns={list(grouped.keys())}"
    )
