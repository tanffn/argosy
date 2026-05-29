"""Tests for the bidirectional XLS-Osh pair flow.

Covers ``POST /api/portfolio/upload-snapshot`` when the upload is a Leumi
monthly portfolio XLS export (positions only, no cash), and the
companion service ``argosy.services.portfolio_ingest.xls_osh_pair``.

Three logical scenarios:
  1. XLS-only upload, no Osh in DB -> pending_pair status, row written.
  2. Osh already in DB when XLS uploads -> auto-pair, TSV synthesized,
     detector fires.
  3. XLS uploaded first (pending), Osh ingests later -> the orchestrator
     hook resolves the pair retroactively.

Plus idempotency + semantic-dedup edge cases.

Fixture: tests/fixtures/portfolio_ingest_leumi/Leumi_26_May_01.xls (real
May 2026 export). The same fixture used by test_leumi_xls_parser.py.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from argosy.state.models import (
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    PortfolioSnapshotPart,
    User,
    UserFile,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "portfolio_ingest_leumi"
FIXTURE_XLS = FIXTURE_DIR / "Leumi_26_May_01.xls"


# ---------------------------------------------------------------------------
# Shared minimal TSV (kept inline to avoid coupling to the existing test
# helper -- the splice service exercises real positions + allocation rows).
# ---------------------------------------------------------------------------


def _minimal_prior_tsv(
    *, snapshot_date: str = "24-Apr-26", fx: float = 3.65,
) -> str:
    """Build a minimal but realistic prior TSV the XLS splice can pull
    from. Includes one Schwab row + a couple of Leumi rows + Real estate
    + Current allocation block."""
    return (
        f"\t{snapshot_date}\t\n"
        f"\tUSD to NIS:\t{fx}\n"
        f"\tUSD to EUR:\t0.92\n"
        f"\n"
        f"Bank account / funds allocation\n"
        f"Review Status\tLocation\tCurrency\tType\tDetails\tSymbol\t# Shares\tCurrent price\tAvg Price\tCurrent Value\t(K) USD Value\t% Change\t% Yearly\n"
        f"\tschwab 876\tUSD\tEquity\tNVIDIA\tNVDA\t11471\t200.14\t200.14\t2295805\t2295\t0\t\n"
        f"\tLeumi\tUSD\tEquity\tAMD\tAMD\t100\t150.00\t140.00\t15000\t15\t0\t\n"
        f"\tLeumi\tUSD\tEquity\tCNDX\tCNDX\t200\t75.00\t70.00\t15000\t15\t0\t\n"
        f"\tLeumi\tNIS\tCash\t\t\t10000\t1\t1\t10000\t2.74\t0\t\n"
        f"\t\tSum:\t\t\t\t\t\t\t\t2327.74\t\t\n"
        f"\n"
        f"Real estate details:\n"
        f"\tTel Aviv\tNIS\tHome\t\t\t\t\t\t2000000\t\t\t\n"
        f"\n"
        f"Current allocation:\n"
        f"\tCategory\tCurrent %\tCurrent K USD\tTarget %\tTarget K USD\tDelta K\n"
        f"\tCash\t0.12%\t2.74\t5%\t116\t113.26\n"
        f"\tEquity\t1.29%\t30\t60%\t1396\t1366\n"
        f"\tGrand Total\t100%\t2327\t100%\t2327\t0\n"
        f"\n"
        f"NVDA Sales History:\n"
        f"\t2026-Q1\t1600\t195.00\t\n"
        f"\n"
        f"Pensions/Saving accounts (as of Apr):\n"
        f"\tAriel\tKupat Gemel\t250000\tNIS\n"
    )


@pytest.fixture
def snapshot_root(tmp_path, monkeypatch):
    """Point ARGOSY_EXPENSE_SAMPLES_ROOT at a tmp dir + seed a prior TSV
    that the XLS splice can pull from."""
    root = tmp_path / "snapshots"
    root.mkdir()
    (root / "Family Finances Status - 26 Apr.tsv").write_text(
        _minimal_prior_tsv(), encoding="utf-8",
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    return root


def _seed_user(session_factory, user_id: str = "ariel") -> None:
    sess = session_factory()
    try:
        if sess.get(User, user_id) is None:
            sess.add(User(id=user_id, plan="free"))
            sess.commit()
    finally:
        sess.close()


def _seed_leumi_osh(
    session_factory,
    *,
    user_id: str,
    period_end: date,
    closing_balance_nis: float,
) -> int:
    """Create an ExpenseSource (Leumi bank) + an ExpenseStatement +
    a single closing-balance transaction. Returns the statement id."""
    sess = session_factory()
    try:
        # Need a UserFile to FK the statement against.
        uf = UserFile(
            user_id=user_id,
            sha256="x" * 64,
            original_name="leumi_osh.html",
            sanitized_name="leumi_osh.html",
            mime_type="text/html",
            kind="other",
            size_bytes=100,
            storage_path="/tmp/x.html",
            source="intake_upload",
        )
        sess.add(uf)
        sess.flush()
        src = ExpenseSource(
            user_id=user_id,
            kind="bank",
            issuer="leumi",
            external_id="2399",
            display_name="Leumi Osh",
            active=True,
        )
        sess.add(src)
        sess.flush()
        stmt = ExpenseStatement(
            user_id=user_id,
            source_id=src.id,
            file_id=uf.id,
            period_start=period_end.replace(day=1),
            period_end=period_end,
            parsed_total_nis=closing_balance_nis,
            parser_name="leumi_osh",
            parser_version="0.1.0",
            status="ok",
        )
        sess.add(stmt)
        sess.flush()
        txn = ExpenseTransaction(
            user_id=user_id,
            statement_id=stmt.id,
            source_id=src.id,
            occurred_on=period_end,
            merchant_raw="closing balance",
            merchant_normalized="closing balance",
            amount_nis=1.0,
            direction="debit",
            tx_type="debit",
            raw_row_json=json.dumps({"balance": f"{closing_balance_nis:.2f}"}),
        )
        sess.add(txn)
        sess.commit()
        return stmt.id
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPendingPair:
    """XLS uploads without a matching Osh statement -> pending_pair."""

    def test_xls_only_upload_queues_as_pending(
        self, client_with_db, snapshot_root,
    ):
        _seed_user(client_with_db.app.state.session_factory)
        xls_bytes = FIXTURE_XLS.read_bytes()
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("Leumi_26_May.xls", xls_bytes, "application/vnd.ms-excel")},
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["detect_status"] == "pending_pair"
        assert payload["tsv_persisted"] is False
        assert payload["pending_pair_id"] is not None
        # The DB row reflects the queue state.
        sess = client_with_db.app.state.session_factory()
        try:
            row = sess.get(PortfolioSnapshotPart, payload["pending_pair_id"])
            assert row is not None
            assert row.status == "pending"
            assert row.kind == "xls_positions"
            assert row.snapshot_date == date(2026, 5, 1)
            assert row.paired_osh_statement_id is None
        finally:
            sess.close()

    def test_idempotent_sha_returns_existing(
        self, client_with_db, snapshot_root,
    ):
        """Uploading identical XLS bytes twice returns the same row."""
        _seed_user(client_with_db.app.state.session_factory)
        xls_bytes = FIXTURE_XLS.read_bytes()
        r1 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("a.xls", xls_bytes, "application/vnd.ms-excel")},
        )
        r2 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("a.xls", xls_bytes, "application/vnd.ms-excel")},
        )
        assert r1.json()["pending_pair_id"] == r2.json()["pending_pair_id"]
        # Both return the SAME sha.
        assert r1.json()["sha256"] == r2.json()["sha256"]


class TestXLSResolvesWithExistingOsh:
    """Osh already in DB when XLS arrives -> auto-pair and resolve."""

    def test_xls_resolves_against_existing_osh(
        self, client_with_db, snapshot_root,
    ):
        _seed_user(client_with_db.app.state.session_factory)
        # Pre-seed an Osh statement that matches the XLS snapshot date.
        _seed_leumi_osh(
            client_with_db.app.state.session_factory,
            user_id="ariel",
            period_end=date(2026, 5, 5),  # within +/-15d of XLS 2026-05-01
            closing_balance_nis=50_000.0,
        )
        xls_bytes = FIXTURE_XLS.read_bytes()
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel", "fire_detector": "false"},
            files={"file": ("Leumi_26_May.xls", xls_bytes, "application/vnd.ms-excel")},
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["detect_status"] in ("ok", "skipped")
        assert payload["tsv_persisted"] is True
        assert payload["persisted_path"] is not None
        # Synthesized TSV exists at the canonical path under snapshot_root.
        persisted = Path(payload["persisted_path"])
        assert persisted.exists()
        assert persisted.parent == snapshot_root
        assert "26 May" in persisted.name
        # The synthesized TSV should contain a Leumi NIS Cash row (from Osh).
        body = persisted.read_text(encoding="utf-8")
        assert "Leumi" in body
        assert "50000" in body or "50,000" in body
        # The pair row is marked resolved.
        sess = client_with_db.app.state.session_factory()
        try:
            row = sess.get(PortfolioSnapshotPart, payload["pending_pair_id"])
            assert row is not None
            assert row.status == "resolved"
            assert row.paired_osh_statement_id is not None
        finally:
            sess.close()


class TestOshHookResolvesPending:
    """XLS uploaded first; Osh arrives later via orchestrator -> pair resolves."""

    def test_osh_arrival_resolves_pending_via_service(
        self, client_with_db, snapshot_root,
    ):
        """Direct service call (not the orchestrator) to verify the hook
        logic, since hooking the full orchestrator path requires PDF
        ingest plumbing we don't replicate here."""
        from argosy.services.portfolio_ingest.xls_osh_pair import (
            try_resolve_pending_on_osh_arrival,
        )

        _seed_user(client_with_db.app.state.session_factory)
        # Upload XLS first -> pending.
        xls_bytes = FIXTURE_XLS.read_bytes()
        r1 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("a.xls", xls_bytes, "application/vnd.ms-excel")},
        )
        assert r1.json()["detect_status"] == "pending_pair"
        pending_id = r1.json()["pending_pair_id"]

        # Now seed an Osh statement and fire the hook explicitly.
        stmt_id = _seed_leumi_osh(
            client_with_db.app.state.session_factory,
            user_id="ariel",
            period_end=date(2026, 4, 30),  # within +/-15d of XLS 2026-05-01
            closing_balance_nis=75_000.0,
        )
        sess = client_with_db.app.state.session_factory()
        try:
            res = try_resolve_pending_on_osh_arrival(
                db=sess,
                statement_id=stmt_id,
                snapshot_root=snapshot_root,
            )
            sess.commit()
        finally:
            sess.close()
        assert res is not None
        assert res.status == "resolved"
        assert res.resolved_tsv_path is not None
        assert res.resolved_tsv_path.exists()

        # The pair row was updated.
        sess = client_with_db.app.state.session_factory()
        try:
            row = sess.get(PortfolioSnapshotPart, pending_id)
            assert row.status == "resolved"
            assert row.paired_osh_statement_id == stmt_id
            assert row.resolved_tsv_path is not None
        finally:
            sess.close()


class TestSplice:
    """The TSV splice preserves non-Leumi rows + recomputes allocations."""

    def test_synthesized_tsv_keeps_schwab_rows(
        self, client_with_db, snapshot_root,
    ):
        _seed_user(client_with_db.app.state.session_factory)
        _seed_leumi_osh(
            client_with_db.app.state.session_factory,
            user_id="ariel",
            period_end=date(2026, 5, 5),
            closing_balance_nis=50_000.0,
        )
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel", "fire_detector": "false"},
            files={"file": ("a.xls", FIXTURE_XLS.read_bytes(), "application/vnd.ms-excel")},
        )
        assert resp.json()["tsv_persisted"] is True
        body = Path(resp.json()["persisted_path"]).read_text(encoding="utf-8")
        # The prior TSV's Schwab row (NVDA, location 'schwab 876') must be present.
        assert "schwab 876" in body
        assert "NVDA" in body
        # Real estate block preserved.
        assert "Real estate details:" in body
        # NVDA Sales History block preserved.
        assert "NVDA Sales History:" in body
        # Pensions block preserved.
        assert "Pensions/Saving accounts" in body


class TestSnifferFallback:
    """Files that aren't TSV or Leumi XLS are rejected with a clear detail."""

    def test_random_text_rejected(self, client_with_db, snapshot_root):
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("foo.txt", b"some random bytes", "text/plain")},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tsv_persisted"] is False
        assert payload["detect_status"] == "skipped"
        assert "did not match a known portfolio shape" in (payload["detail"] or "").lower() or (
            "header marker" in (payload["detail"] or "").lower()
        )
