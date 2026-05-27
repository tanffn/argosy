"""EX2 anomaly-detection runner — unit tests with stubbed agent.

Covers:
  - Env-gate: pytest detection makes ``is_enabled_for_runtime`` False
    even when ``ARGOSY_ANOMALY_DETECTION_ENABLED=1``.
  - Env-gate: env-var off → skipped.
  - Stubbed normal state → row persisted with empty severity counts
    + no anomalies.
  - Stubbed RED anomaly → row persisted + ``anomaly.detected`` WS
    event emitted.
  - Watchlist seed YAML loads correctly (Card 2923 fee-waiver present).

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_anomaly_runner.py -v
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.anomaly_detection import (
    Anomaly,
    AnomalyDetectionReport,
    WatchlistEntryStatus,
)
from argosy.services import anomaly_runner as runner_mod
from argosy.services.anomaly_runner import (
    is_enabled_for_runtime,
    load_watchlist_seed,
    run_anomaly_check,
)
from argosy.state.models import (
    AnomalyReport,
    Base,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    User,
    UserFile,
)


USER = "ariel"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB.

    Mirrors the pattern in test_fleet_self_review.py: file-backed
    SQLite per-test so engines + threads can share state without the
    in-memory connection-binding gotcha.
    """
    db_path = tmp_path / "anomaly.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


# ----------------------------------------------------------------------
# Stub agent — bypasses the LLM call entirely.
# ----------------------------------------------------------------------


class _StubAgent:
    """Minimal stand-in for ``AnomalyDetectionAgent``.

    The runner only invokes ``agent.run_sync(...)`` and inspects the
    returned object's ``.output`` attribute. We mimic that shape with
    SimpleNamespace so we don't need to instantiate the real agent
    (which would build a Citations-API prompt + try to talk to
    Anthropic).
    """

    def __init__(self, canned: AnomalyDetectionReport) -> None:
        self._canned = canned

    def run_sync(self, **kwargs: Any) -> Any:
        # Mirror the BaseAgent.AgentReport shape only insofar as the
        # runner inspects it: .output is the pydantic model.
        return SimpleNamespace(output=self._canned)


def _seed_card_2923_statement(db, period_end: date = date(2026, 5, 28)) -> int:
    """Create a Discount Bank Card 2923 source + one statement + a
    couple of transactions. Returns the statement id.

    Used by the RED-anomaly test so the runner has something concrete
    to anchor the report row's ``source_statement_id`` against.
    """
    f = UserFile(
        user_id=USER, sha256="a" * 64,
        original_name="discount.html", sanitized_name="discount.html",
        mime_type="text/html", kind="other",
        size_bytes=1, storage_path="/tmp/discount", source="expense_statement",
    )
    db.add(f)
    db.flush()
    src = ExpenseSource(
        user_id=USER, kind="card", issuer="discount",
        external_id="2923", display_name="Discount Card 2923",
    )
    db.add(src)
    db.flush()
    stmt = ExpenseStatement(
        user_id=USER, source_id=src.id, file_id=f.id,
        period_start=date(2026, 5, 1), period_end=period_end,
        parsed_total_nis=Decimal("100"),
        declared_total_nis=Decimal("100"),
        parser_name="discount", parser_version="0.1.0",
        status="parsed",
    )
    db.add(stmt)
    db.flush()
    db.add(ExpenseTransaction(
        user_id=USER, source_id=src.id, statement_id=stmt.id,
        occurred_on=date(2026, 5, 15),
        merchant_raw="עמלת כרטיס", merchant_normalized="card fee",
        amount_nis=Decimal("12.50"),
        direction="debit", tx_type="regular",
        raw_row_json="{}",
    ))
    db.commit()
    return stmt.id


# ----------------------------------------------------------------------
# Test 1 — env gate respects pytest.
# ----------------------------------------------------------------------


def test_anomaly_skipped_under_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """``is_enabled_for_runtime`` must be False whenever
    ``PYTEST_CURRENT_TEST`` is set, regardless of the env-var opt-in.

    This is the test-isolation guard: even if a CI environment exports
    ``ARGOSY_ANOMALY_DETECTION_ENABLED=1`` globally, the runner must NOT
    spawn a real background thread under pytest.
    """
    monkeypatch.setenv("ARGOSY_ANOMALY_DETECTION_ENABLED", "1")
    # PYTEST_CURRENT_TEST is set by pytest itself for every test.
    assert is_enabled_for_runtime() is False


# ----------------------------------------------------------------------
# Test 2 — env gate respects the explicit-off env var.
# ----------------------------------------------------------------------


def test_anomaly_skipped_when_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside pytest, ``ARGOSY_ANOMALY_DETECTION_ENABLED!=1`` disables."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_ANOMALY_DETECTION_ENABLED", "0")
    assert is_enabled_for_runtime() is False
    monkeypatch.setenv("ARGOSY_ANOMALY_DETECTION_ENABLED", "false")
    assert is_enabled_for_runtime() is False
    monkeypatch.setenv("ARGOSY_ANOMALY_DETECTION_ENABLED", "1")
    assert is_enabled_for_runtime() is True


# ----------------------------------------------------------------------
# Test 3 — stubbed agent reporting normal state → row persisted, no
# anomalies, no WS event fired (severity counts all zero).
# ----------------------------------------------------------------------


def test_anomaly_runner_stubbed_normal_state(
    sync_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NORMAL report still persists a row so the timeline is honest
    about every fire, but emits no WS event (nothing changed)."""
    canned = AnomalyDetectionReport(
        anomalies=[],
        watchlist_status=[
            WatchlistEntryStatus(
                name="discount_bank_card_2923_fee_waiver",
                state="NORMAL",
                last_evidence="עמלת כרטיס ₪12.50 + הנחת עמלה ₪-12.50",
            )
        ],
    )
    stub = _StubAgent(canned)

    # Spy on the WS publisher so we can assert it wasn't called.
    published: list[tuple[str, dict]] = []

    def _spy_publish(name: str, payload: dict) -> None:
        published.append((name, payload))

    monkeypatch.setattr(
        "argosy.api.events.publish_event_threadsafe", _spy_publish,
    )

    row = run_anomaly_check(
        USER, sync_session,
        triggered_by="manual", source_statement_id=None, agent=stub,
    )

    assert isinstance(row, AnomalyReport)
    assert row.user_id == USER
    assert row.triggered_by == "manual"
    assert row.source_statement_id is None
    sev = json.loads(row.severity_summary_json)
    assert sev == {"RED": 0, "AMBER": 0, "YELLOW": 0}
    payload = json.loads(row.report_json)
    assert payload["anomalies"] == []
    assert len(payload["watchlist_status"]) == 1
    # No WS event when nothing actionable happened.
    assert published == []


# ----------------------------------------------------------------------
# Test 4 — stubbed RED anomaly → row persisted + WS event emitted.
# ----------------------------------------------------------------------


def test_anomaly_runner_stubbed_red_anomaly(
    sync_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RED anomaly must persist + fire the ``anomaly.detected`` event."""
    stmt_id = _seed_card_2923_statement(sync_session)

    canned = AnomalyDetectionReport(
        anomalies=[
            Anomaly(
                severity="RED",
                watchlist_entry_name="discount_bank_card_2923_fee_waiver",
                observation=(
                    "Card 2923 charged ₪12.50 fee but no matching "
                    "discount line found in May 2026."
                ),
                last_seen="2026-04-30",
                suggested_action=(
                    "Call Discount Bank and ask about the fee-waiver "
                    "promotion expiry date."
                ),
            )
        ],
        watchlist_status=[
            WatchlistEntryStatus(
                name="discount_bank_card_2923_fee_waiver",
                state="ALERT",
                last_evidence="עמלת כרטיס ₪12.50 (no matching discount)",
            )
        ],
    )
    stub = _StubAgent(canned)

    # Spy on the WS publisher.
    published: list[tuple[str, dict]] = []

    def _spy_publish(name: str, payload: dict) -> None:
        published.append((name, payload))

    monkeypatch.setattr(
        "argosy.api.events.publish_event_threadsafe", _spy_publish,
    )

    row = run_anomaly_check(
        USER, sync_session,
        triggered_by="event", source_statement_id=stmt_id, agent=stub,
    )

    assert row.triggered_by == "event"
    assert row.source_statement_id == stmt_id
    sev = json.loads(row.severity_summary_json)
    assert sev["RED"] == 1
    assert sev["AMBER"] == 0
    assert sev["YELLOW"] == 0
    payload = json.loads(row.report_json)
    assert len(payload["anomalies"]) == 1
    assert payload["anomalies"][0]["severity"] == "RED"

    # WS event fired with the right shape.
    assert len(published) == 1, published
    name, ev_payload = published[0]
    assert name == "anomaly.detected"
    assert ev_payload["user_id"] == USER
    assert ev_payload["report_id"] == row.id
    assert ev_payload["triggered_by"] == "event"
    assert ev_payload["source_statement_id"] == stmt_id
    assert ev_payload["severity_summary"] == sev
    assert ev_payload["anomalies_count"] == 1


# ----------------------------------------------------------------------
# Test 5 — watchlist seed loads correctly.
# ----------------------------------------------------------------------


def test_anomaly_runner_loads_watchlist_seed() -> None:
    """``load_watchlist_seed()`` returns the Card 2923 entry from the
    canonical YAML on disk. Sanity-check that the schema fields the
    runner uses are populated (issuer_match/account_match/severity)."""
    entries = load_watchlist_seed()
    assert isinstance(entries, list)
    assert len(entries) >= 1, entries

    names = {e["name"] for e in entries}
    assert "discount_bank_card_2923_fee_waiver" in names, names

    card_2923 = next(
        e for e in entries
        if e["name"] == "discount_bank_card_2923_fee_waiver"
    )
    assert card_2923["issuer_match"] == "discount"
    assert card_2923["account_match"] == "2923"
    assert card_2923["severity"] == "RED"
    assert card_2923["expected_pattern"], card_2923
    assert card_2923["alert_when"], card_2923


# ----------------------------------------------------------------------
# Test 6 — load from explicit alt path (sanity for the path-override
# branch + the "missing file" graceful-degradation).
# ----------------------------------------------------------------------


def test_anomaly_runner_seed_missing_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    assert load_watchlist_seed(missing) == []


# ----------------------------------------------------------------------
# Test 7 — schedule_anomaly_check no-op under pytest.
# ----------------------------------------------------------------------


def test_schedule_anomaly_check_noop_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fire-and-forget entry must NOT spawn a thread under pytest,
    even with ``ARGOSY_ANOMALY_DETECTION_ENABLED=1``.

    Asserted by spying on threading.Thread — the runner never
    constructs one in this code path.
    """
    monkeypatch.setenv("ARGOSY_ANOMALY_DETECTION_ENABLED", "1")

    constructed: list[Any] = []
    original_thread = runner_mod.threading.Thread

    def _spy_thread(*args: Any, **kwargs: Any) -> Any:
        constructed.append((args, kwargs))
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(runner_mod.threading, "Thread", _spy_thread)

    runner_mod.schedule_anomaly_check(
        user_id=USER, triggered_by="event", source_statement_id=42,
    )
    runner_mod.schedule_anomaly_check(user_id=USER, triggered_by="daily")

    assert constructed == [], (
        "schedule_anomaly_check must not spawn a thread under pytest"
    )
