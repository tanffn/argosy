"""Tests for the payslip-ingest service + §102 withholding closed loop.

Covers:
  * ingest_payslips against the REAL Ariel payslips under
    $ARGOSY_EXPENSE_SAMPLES_ROOT (skip-guarded when the dir is absent) — a row
    per period, idempotent re-run, catalog rows written.
  * latest_withholding_verdict / withholding_action_status (helper +
    satisfied=true on a reconciled verdict).
  * the GET /api/tax/withholding-check route returns the verdict.
  * the payslip_ingest job registers with source_kind='ingest'.
  * discovery + serialization round-trips with synthetic fakes (no samples dep).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.payslip_ingest import (
    deserialize_verdict,
    discover_payslip_pdfs,
    ingest_payslips,
    latest_withholding_verdict,
    withholding_action_status,
)
from argosy.state import db as db_module
from argosy.state.models import Base, PayslipFactRow, User, UserFile

# Real payslips live under $ARGOSY_EXPENSE_SAMPLES_ROOT/<year>/Payslip/Ariel/.
_SAMPLES_ROOT = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
_HAS_SAMPLES = bool(_SAMPLES_ROOT) and Path(_SAMPLES_ROOT).exists()
requires_samples = pytest.mark.skipif(
    not _HAS_SAMPLES,
    reason="ARGOSY_EXPENSE_SAMPLES_ROOT not set / payslip samples absent",
)


@pytest.fixture
def home_db(tmp_path, monkeypatch):
    """ARGOSY_HOME + sync(file) & async(same file) engines + seeded user.

    Mirrors conftest.argosy_home_db but yields the sync sessionmaker so the
    ingest call can be handed an explicit session_factory while catalog_upload
    (which opens its own async session via db_module) shares the same DB file.
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    sync_url = f"sqlite:///{settings.db_file}"
    async_url = f"sqlite+aiosqlite:///{settings.db_file}"

    sync_engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(sync_engine)
    SessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)

    db_module.init_engine(async_url)

    s = SessionLocal()
    try:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()
    finally:
        s.close()

    yield SessionLocal

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(db_module.dispose_engine())
    finally:
        loop.close()
    sync_engine.dispose()
    reload_settings()


# ---------------------------------------------------------------------------
# Discovery + serialization — no samples / no DB needed.
# ---------------------------------------------------------------------------


def test_discover_payslip_pdfs(tmp_path):
    """Finds <year>/Payslip/Ariel/YYYY_MM.pdf, sorted, ignoring junk names."""
    base = tmp_path / "2026" / "Payslip" / "Ariel"
    base.mkdir(parents=True)
    (base / "2026_03.pdf").write_bytes(b"%PDF-3")
    (base / "2026_01.pdf").write_bytes(b"%PDF-1")
    (base / "2026_02.pdf").write_bytes(b"%PDF-2")
    (base / "notes.pdf").write_bytes(b"%PDF-x")  # non-period name → skipped
    # Noga's folder must not be picked up by the default name="Ariel".
    noga = tmp_path / "2026" / "Payslip" / "Noga"
    noga.mkdir(parents=True)
    (noga / "2026_01.pdf").write_bytes(b"%PDF-n")

    found = discover_payslip_pdfs(tmp_path, name="Ariel")
    assert [p.name for p in found] == ["2026_01.pdf", "2026_02.pdf", "2026_03.pdf"]


def test_serialize_verdict_roundtrip():
    from argosy.services.payslip_parser import PayslipFacts
    from argosy.services.rsu_reconciliation.withholding_check import (
        check_withholding,
    )
    from argosy.services.payslip_ingest import _serialize_verdict

    facts = PayslipFacts(period_year=2026)  # no equity fields → no_equity_yet
    verdict = check_withholding(facts)
    restored = deserialize_verdict(_serialize_verdict(verdict))
    assert restored.status == verdict.status
    assert restored.summary == verdict.summary


def test_ingest_skips_when_samples_root_unconfigured(home_db, monkeypatch):
    monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)
    summary = ingest_payslips("ariel", session_factory=home_db)
    assert summary["skipped_reason"] == "samples_root_unconfigured"
    assert summary["ingested"] == 0


# ---------------------------------------------------------------------------
# Real-samples integration (skip-guarded).
# ---------------------------------------------------------------------------


@requires_samples
def test_ingest_real_payslips_row_per_period_and_reconciled_latest(home_db):
    SessionLocal = home_db
    root = Path(_SAMPLES_ROOT)

    summary = ingest_payslips("ariel", session_factory=SessionLocal, samples_root=root)
    assert summary["skipped_reason"] is None
    assert summary["ingested"] >= 1, summary
    assert not summary["errors"], summary["errors"]

    s = SessionLocal()
    try:
        rows = s.query(PayslipFactRow).filter_by(user_id="ariel").all()
        assert len(rows) == summary["ingested"]
        # Each period unique; catalog wrote a UserFile per distinct sha.
        periods = {(r.period_year, r.period_month) for r in rows}
        assert len(periods) == len(rows)
        files = s.query(UserFile).filter_by(
            user_id="ariel", source="payslip_ingest"
        ).all()
        assert len(files) >= 1
        for f in files:
            assert f.kind == "pdf"

        # The latest verdict: the April 2026 payslip reconciles (see
        # withholding_check module docstring — accounted matches the §102 model).
        latest = latest_withholding_verdict("ariel", s)
        assert latest["has_verdict"] is True
        assert latest["status"] == "reconciled", latest

        # Closed-loop helper: reconciled → satisfied.
        st = withholding_action_status("ariel", s)
        assert st["satisfied"] is True
        assert st["status"] == "reconciled"
        assert "§102" in st["summary"] or "equity tax" in st["summary"]
    finally:
        s.close()


@requires_samples
def test_ingest_is_idempotent(home_db):
    SessionLocal = home_db
    root = Path(_SAMPLES_ROOT)

    first = ingest_payslips("ariel", session_factory=SessionLocal, samples_root=root)
    assert first["ingested"] >= 1
    second = ingest_payslips("ariel", session_factory=SessionLocal, samples_root=root)
    # Second run: every period already present with matching sha → all skipped.
    assert second["ingested"] == 0
    assert second["updated"] == 0
    assert second["skipped"] == first["ingested"]

    s = SessionLocal()
    try:
        rows = s.query(PayslipFactRow).filter_by(user_id="ariel").all()
        assert len(rows) == first["ingested"]  # no duplicate rows
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Helper honesty — no-data + discrepancy must NOT satisfy.
# ---------------------------------------------------------------------------


def test_withholding_action_status_no_data(home_db):
    s = home_db()
    try:
        st = withholding_action_status("ariel", s)
        assert st["has_verdict"] is False
        assert st["satisfied"] is False
        assert st["status"] == "no_data"
    finally:
        s.close()


def test_withholding_action_status_discrepancy_not_satisfied(home_db):
    """A persisted discrepancy verdict must never read as satisfied."""
    s = home_db()
    try:
        s.add(
            PayslipFactRow(
                user_id="ariel",
                period_year=2026,
                period_month=4,
                source_file_id=None,
                source_sha256="x" * 64,
                parsed_json="{}",
                verdict_json=json.dumps(
                    {"status": "discrepancy", "summary": "model mismatch"}
                ),
            )
        )
        s.commit()
        st = withholding_action_status("ariel", s)
        assert st["status"] == "discrepancy"
        assert st["satisfied"] is False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Route + job registration.
# ---------------------------------------------------------------------------


def test_withholding_check_route(client_with_db):
    # Seed a reconciled verdict directly via the app's sync session factory.
    SessionLocal = client_with_db.app.state.session_factory
    s = SessionLocal()
    try:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()
        s.add(
            PayslipFactRow(
                user_id="ariel",
                period_year=2026,
                period_month=4,
                source_file_id=None,
                source_sha256="a" * 64,
                parsed_json="{}",
                verdict_json=json.dumps(
                    {
                        "status": "reconciled",
                        "period": 2026,
                        "equity_ordinary_base": 60679.0,
                        "equity_capital_base": 549467.0,
                        "actual_tax_withheld": 167707.0,
                        "expected_at_wire_rate": 167706.25,
                        "reconc_residual": 0.75,
                        "conservative_liability": 175082.0,
                        "potential_filing_topup": 7375.0,
                        "effective_rate_pct": 27.5,
                        "summary": "Your payslip reconciles ₪167,707 of §102 equity tax.",
                        "confidence": "high",
                        "caveats": ["scope caveat"],
                    }
                ),
            )
        )
        s.commit()
    finally:
        s.close()

    r = client_with_db.get("/api/tax/withholding-check?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_verdict"] is True
    assert body["status"] == "reconciled"
    assert body["period_year"] == 2026
    assert body["verdict"]["actual_tax_withheld"] == 167707.0
    assert body["verdict"]["caveats"] == ["scope caveat"]


def test_withholding_check_route_no_data(client_with_db):
    SessionLocal = client_with_db.app.state.session_factory
    s = SessionLocal()
    try:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()
    finally:
        s.close()
    r = client_with_db.get("/api/tax/withholding-check?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["has_verdict"] is False
    assert body["status"] == "no_data"
    assert body["verdict"] is None


def test_payslip_ingest_job_registers():
    from argosy.orchestrator.loops.payslip_ingest import (
        PayslipIngestLoop,
        payslip_ingest_metadata,
    )

    md = payslip_ingest_metadata()
    assert md.name == "payslip_ingest"
    assert md.source_kind == "ingest"
    loop = PayslipIngestLoop(enabled=True, user_id="ariel")
    assert loop.name == "payslip_ingest"
    assert loop.schedule.cron == "30 6 * * *"
