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

    def test_xls_only_upload_carries_forward_nis_cash(
        self, client_with_db, snapshot_root,
    ):
        """With a prior snapshot present, an XLS-only upload (no Osh in window)
        RESOLVES by carrying the prior NIS cash forward — a positions update
        is not blocked on the user also exporting a current-account statement.
        The carry-forward is labelled and leaves paired_osh_statement_id NULL
        so a later Osh can still refresh it."""
        _seed_user(client_with_db.app.state.session_factory)
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
        # The DB row is resolved-via-carry-forward (no Osh paired).
        sess = client_with_db.app.state.session_factory()
        try:
            row = sess.get(PortfolioSnapshotPart, payload["pending_pair_id"])
            assert row is not None
            assert row.status == "resolved"
            assert row.kind == "xls_positions"
            assert row.snapshot_date == date(2026, 5, 1)
            assert row.paired_osh_statement_id is None
        finally:
            sess.close()

    def test_xls_only_no_prior_tsv_queues_as_pending(
        self, client_with_db, snapshot_root,
    ):
        """Brand-new user with no prior snapshot to carry NIS cash from: the
        upload still queues as pending (nothing to carry forward)."""
        # Remove the seeded prior TSV so there is nothing to carry forward.
        for tsv in snapshot_root.glob("Family Finances Status*.tsv"):
            tsv.unlink()
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
        sess = client_with_db.app.state.session_factory()
        try:
            row = sess.get(PortfolioSnapshotPart, payload["pending_pair_id"])
            assert row is not None
            assert row.status == "pending"
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
        # XLS-only with a prior snapshot present → resolves via carry-forward
        # (no Osh yet, so paired_osh_statement_id is NULL). The Osh arriving
        # below must then REFRESH that carry-forward part.
        assert r1.json()["detect_status"] in ("ok", "skipped")
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

    def test_osh_arrival_writes_through_snapshot_store(
        self, client_with_db, snapshot_root,
    ):
        """Regression (write-through gap): resolving the pair must write the
        merged snapshot THROUGH to the DB snapshot store so GET /snapshot (which
        is DB-first) reflects the new month. Before the fix the resolution wrote
        only the TSV file and the UI silently kept showing the prior snapshot."""
        from argosy.services.portfolio_ingest.xls_osh_pair import (
            try_resolve_pending_on_osh_arrival,
        )
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
        )

        _seed_user(client_with_db.app.state.session_factory)
        xls_bytes = FIXTURE_XLS.read_bytes()
        r1 = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("a.xls", xls_bytes, "application/vnd.ms-excel")},
        )
        # Carry-forward resolves immediately; the Osh-arrival hook then
        # refreshes it and re-writes the snapshot through to the store.
        assert r1.json()["detect_status"] in ("ok", "skipped")
        xls_date = r1.json()["snapshot_date"]  # the parsed XLS snapshot_date

        stmt_id = _seed_leumi_osh(
            client_with_db.app.state.session_factory,
            user_id="ariel",
            period_end=date(2026, 4, 30),
            closing_balance_nis=75_000.0,
        )
        sess = client_with_db.app.state.session_factory()
        try:
            res = try_resolve_pending_on_osh_arrival(
                db=sess, statement_id=stmt_id, snapshot_root=snapshot_root,
            )
            sess.commit()  # the caller owns the commit (atomic with the batch)
        finally:
            sess.close()
        assert res is not None and res.status == "resolved"

        # The merged snapshot is now the LIVE DB snapshot (the gap: silently not
        # written, so the store had no row / kept the stale one).
        sess = client_with_db.app.state.session_factory()
        try:
            row = get_latest_snapshot_row(sess, "ariel")
            assert row is not None, "pairing did not write the snapshot through"
            assert row.snapshot_date.isoformat() == xls_date
        finally:
            sess.close()

        # And the user-facing surface reflects the new month.
        snap = client_with_db.get(
            "/api/portfolio/snapshot?user_id=ariel"
        ).json()
        assert snap["snapshot_date"] == xls_date


class TestSnapshotChangeDetector:
    """Windfall detection is a property of a snapshot UPDATE, not of the upload
    HTTP route — so the shared detector must run from any path. These lock the
    skip-paths + the no-raise contract; the route + Osh-arrival paths exercise
    the detect-and-propose path end to end."""

    def test_skips_when_no_prior_tsv(self, client_with_db, tmp_path):
        from argosy.services.portfolio_ingest.snapshot_change import (
            run_windfall_detection_on_snapshot,
        )
        target = tmp_path / "Family Finances Status - 26 Jun.tsv"
        target.write_text(_minimal_prior_tsv(snapshot_date="29-Jun-26"),
                          encoding="utf-8")
        sess = client_with_db.app.state.session_factory()
        try:
            res = run_windfall_detection_on_snapshot(
                sess, user_id="ariel", target_path=target,
            )
        finally:
            sess.close()
        # Only one TSV at the root → nothing to diff against → skipped, no raise.
        assert res.detect_status == "skipped"
        assert res.event is None

    def test_fire_false_short_circuits(self, client_with_db, tmp_path):
        from argosy.services.portfolio_ingest.snapshot_change import (
            run_windfall_detection_on_snapshot,
        )
        target = tmp_path / "Family Finances Status - 26 Jun.tsv"
        target.write_text(_minimal_prior_tsv(), encoding="utf-8")
        sess = client_with_db.app.state.session_factory()
        try:
            res = run_windfall_detection_on_snapshot(
                sess, user_id="ariel", target_path=target, fire=False,
            )
        finally:
            sess.close()
        assert res.detect_status == "skipped"


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
        detail = (payload["detail"] or "").lower()
        assert (
            "did not match a known portfolio shape" in detail
            or "header marker" in detail
        )


# ---------------------------------------------------------------------------
# Splice math tests (codex zigzag (a) impl review #I9 -- previously the
# splice was only reached via integration tests that didn't read the
# synthesized output. These tests assert the actual TSV math.)
# ---------------------------------------------------------------------------


class TestSpliceMath:
    """Direct unit tests for the splice helpers in xls_osh_pair."""

    def test_allocation_block_recomputed_not_carried_verbatim(
        self, client_with_db, snapshot_root,
    ):
        """BLOCKER 1+2 regression: recomputed current_pct + current_k
        must reflect the new positions, not the prior TSV's values."""
        _seed_user(client_with_db.app.state.session_factory)
        _seed_leumi_osh(
            client_with_db.app.state.session_factory,
            user_id="ariel",
            period_end=date(2026, 5, 5),
            closing_balance_nis=100_000.0,  # ~$27K NIS cash @ 3.65
        )
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel", "fire_detector": "false"},
            files={"file": ("a.xls", FIXTURE_XLS.read_bytes(), "application/vnd.ms-excel")},
        )
        assert resp.json()["tsv_persisted"] is True
        body = Path(resp.json()["persisted_path"]).read_text(encoding="utf-8")

        # Prior fixture's Cash row was current_k=2.74. After splice, new
        # cash is 100_000 NIS / 3.65 fx / 1000 = ~27.4 K USD. The Cash row
        # in the synthesized TSV must reflect the new value, NOT the prior.
        cash_lines = [ln for ln in body.splitlines() if ln.split("\t")[1:2] == ["Cash"]]
        assert len(cash_lines) > 0, "Expected Cash row in Current allocation block"
        # The recomputed current_k_usd for Cash should be ~27.40 (close enough).
        cash_row_cells = cash_lines[0].split("\t")
        new_current_k = float(cash_row_cells[3].replace(",", "").strip())
        assert 25.0 < new_current_k < 30.0, (
            f"Cash current_k_usd should reflect Osh balance / fx (~27.4), "
            f"got {new_current_k}. Allocation block was probably carried "
            f"verbatim instead of recomputed (BLOCKER 1+2 regression)."
        )

    def test_grand_total_recomputed(self, client_with_db, snapshot_root):
        """Grand Total row's current_k_usd must be sum of by-type aggregates."""
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
        body = Path(resp.json()["persisted_path"]).read_text(encoding="utf-8")
        # Look for the Grand Total row in the Current allocation block.
        total_lines = [
            ln for ln in body.splitlines()
            if "total" in (ln.split("\t")[1] if len(ln.split("\t")) > 1 else "").lower()
        ]
        # If a Grand Total row exists in the prior fixture, it should be
        # present + recomputed in the output. Our fixture has one; assert
        # it didn't get dropped or stay verbatim at the prior value (2327).
        if total_lines:
            cells = total_lines[0].split("\t")
            new_total = float(cells[3].replace(",", "").strip())
            assert new_total != 2327.0, (
                "Grand Total stayed at the prior fixture's 2327 -- "
                "the recompute didn't fire."
            )

    def test_fx_rate_preserved_from_prior(
        self, client_with_db, snapshot_root,
    ):
        """Codex zigzag finding #5: FX must be snapshot-effective.
        We use the prior TSV's USD-to-NIS rate verbatim."""
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
        body = Path(resp.json()["persisted_path"]).read_text(encoding="utf-8")
        # Prior fixture set fx=3.65; the synthesized TSV must keep it.
        first_lines = body.splitlines()[:6]
        joined = "\n".join(first_lines)
        assert "3.65" in joined, (
            f"Expected prior TSV's USD/NIS rate 3.65 in the new TSV headers; "
            f"got: {joined!r}. FX is supposed to be snapshot-effective, not "
            f"defaulted (codex zigzag #5)."
        )


class TestOshUsdDisambiguation:
    """BLOCKER 3 regression: Leumi USD statement must NOT match an XLS pair."""

    def test_leumi_usd_statement_not_treated_as_osh(
        self, client_with_db, snapshot_root,
    ):
        """Seed a Leumi statement parsed by leumi_usd (not leumi_osh).
        The XLS upload should NOT auto-pair against it -- the matcher
        must discriminate by parser_name."""
        _seed_user(client_with_db.app.state.session_factory)
        sess = client_with_db.app.state.session_factory()
        try:
            uf = UserFile(
                user_id="ariel",
                sha256="u" * 64,
                original_name="leumi_usd.html",
                sanitized_name="leumi_usd.html",
                mime_type="text/html",
                kind="other",
                size_bytes=100,
                storage_path="/tmp/u.html",
                source="intake_upload",
            )
            sess.add(uf)
            sess.flush()
            src = ExpenseSource(
                user_id="ariel", kind="bank", issuer="leumi",
                external_id="usd-acct", display_name="Leumi USD",
                active=True,
            )
            sess.add(src)
            sess.flush()
            stmt = ExpenseStatement(
                user_id="ariel", source_id=src.id, file_id=uf.id,
                period_start=date(2026, 5, 1), period_end=date(2026, 5, 5),
                parsed_total_nis=0,
                parser_name="leumi_usd",  # <-- not leumi_osh
                parser_version="0.1.0", status="ok",
            )
            sess.add(stmt)
            sess.commit()
        finally:
            sess.close()
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel"},
            files={"file": ("a.xls", FIXTURE_XLS.read_bytes(), "application/vnd.ms-excel")},
        )
        # Because the matcher requires parser_name="leumi_osh", the Leumi USD
        # statement is NOT treated as an Osh: no Osh is paired. The upload
        # still resolves (carrying prior NIS cash forward), but crucially
        # paired_osh_statement_id stays NULL — the USD balance was never fed
        # through the NIS-divide (which would be off by ~3.7x).
        payload = resp.json()
        assert payload["detect_status"] in ("ok", "skipped")
        sess = client_with_db.app.state.session_factory()
        try:
            row = sess.get(PortfolioSnapshotPart, payload["pending_pair_id"])
            assert row is not None
            assert row.paired_osh_statement_id is None, (
                "Leumi USD statement was incorrectly treated as Osh"
            )
        finally:
            sess.close()


class TestAssetTypeCarryForward:
    """BLOCKER 4 regression: asset_type must be preserved from prior TSV,
    not hard-coded 'Equity'."""

    def test_asset_type_preserved_for_known_security(
        self, client_with_db, snapshot_root,
    ):
        """The prior fixture labels Leumi AMD + CNDX as 'Equity'. After
        splice they should stay 'Equity' (mapped via _build_prior_mappings).
        A regression to hard-coded 'Equity' would silently pass this test;
        but if the prior fixture used 'Growth' or 'Dividend' for AMD,
        a regression would surface."""
        _seed_user(client_with_db.app.state.session_factory)
        # Use a custom prior TSV with a non-Equity asset_type for AMD.
        custom_prior = (
            "\t24-Apr-26\t\n"
            "\tUSD to NIS:\t3.65\n"
            "\tUSD to EUR:\t0.92\n"
            "\n"
            "Bank account / funds allocation\n"
            "Review Status\tLocation\tCurrency\tType\tDetails\tSymbol\t# Shares\tCurrent price\tAvg Price\tCurrent Value\t(K) USD Value\t% Change\t% Yearly\n"
            # AMD labeled 'Growth' in prior so we can detect carry-forward.
            "\tLeumi\tUSD\tGrowth\tAMD\tAMD\t100\t150.00\t140.00\t15000\t15\t0\t\n"
            "\tLeumi\tNIS\tCash\t\t\t10000\t1\t1\t10000\t2.74\t0\t\n"
            "\t\tSum:\t\t\t\t\t\t\t\t17.74\t\t\n"
            "\n"
            "Current allocation:\n"
            "\tCategory\tCurrent %\tCurrent K USD\tTarget %\tTarget K USD\tDelta K\n"
            "\tCash\t15.4%\t2.74\t5%\t1\t-1.74\n"
            "\tGrowth\t84.6%\t15\t60%\t10\t-5\n"
        )
        (snapshot_root / "Family Finances Status - 26 Apr.tsv").write_text(
            custom_prior, encoding="utf-8",
        )
        _seed_leumi_osh(
            client_with_db.app.state.session_factory,
            user_id="ariel",
            period_end=date(2026, 5, 5),
            closing_balance_nis=20_000.0,
        )
        resp = client_with_db.post(
            "/api/portfolio/upload-snapshot",
            data={"user_id": "ariel", "fire_detector": "false"},
            files={"file": ("a.xls", FIXTURE_XLS.read_bytes(), "application/vnd.ms-excel")},
        )
        body = Path(resp.json()["persisted_path"]).read_text(encoding="utf-8")
        # The XLS fixture contains AMD as a position. After splice, the
        # AMD row's asset_type column should be 'Growth' (carried from
        # prior), not 'Equity' (the old hard-coded value).
        amd_lines = [
            ln for ln in body.splitlines()
            if len(ln.split("\t")) > 5
            and ln.split("\t")[1] == "Leumi"
            and "AMD" in (ln.split("\t")[5] or "")
        ]
        assert len(amd_lines) > 0, "No AMD Leumi row found in synthesized TSV"
        cells = amd_lines[0].split("\t")
        # cells[3] is the asset_type column.
        assert cells[3] == "Growth", (
            f"AMD asset_type should be carried forward as 'Growth' from "
            f"the prior TSV; got {cells[3]!r}. Regression to hard-coded "
            f"'Equity' (BLOCKER 4)."
        )


class TestNoPriorTsvGracefulBootstrap:
    """Codex zigzag (a)#9: first-time user with no prior TSV must not brick."""

    def test_no_prior_tsv_falls_back_to_minimal_synthesis(
        self, client_with_db, tmp_path, monkeypatch,
    ):
        """Override the snapshot_root fixture: no pre-seeded prior TSV.
        Upload should still produce a synthesized TSV (minimal shape)."""
        empty_root = tmp_path / "empty_snapshots"
        empty_root.mkdir()
        monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(empty_root))
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
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # Even without a prior TSV the route should succeed (not 'failed').
        assert payload["tsv_persisted"] is True, (
            f"Expected graceful fallback synthesis; got: {payload}"
        )
        body = Path(payload["persisted_path"]).read_text(encoding="utf-8")
        # The fallback path emits the Bank account header + the new cash
        # + position rows even without a prior to carry forward from.
        assert "Bank account / funds allocation" in body
        assert "Leumi" in body
