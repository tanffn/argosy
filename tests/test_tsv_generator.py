"""Tests for the Argosy-generates-the-TSV flow.

Covers POST /api/portfolio/generate-tsv + the underlying
generate_family_finances_tsv service. Per [[feedback_argosy_generates_tsv]]
this is now the primary path for composing the canonical Family
Finances Status TSV; the user does not run an external script.
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
    User,
    UserFile,
)


def _prior_tsv() -> str:
    """A realistic Family Finances Status TSV with:
      * Schwab + Leumi positions
      * Leumi NIS Cash + Leumi USD Cash rows
      * Real estate + Current allocation + NVDA Sales + Pensions sections
    """
    return (
        "\t24-Apr-26\t\n"
        "\tUSD to NIS:\t3.65\n"
        "\tUSD to EUR:\t0.92\n"
        "\n"
        "Bank account / funds allocation\n"
        "Review Status\tLocation\tCurrency\tType\tDetails\tSymbol\t# Shares\tCurrent price\tAvg Price\tCurrent Value\t(K) USD Value\t% Change\t% Yearly\n"
        "\tschwab 876\tUSD\tEquity\tNVIDIA\tNVDA\t11471\t200.14\t200.14\t2295805\t2295\t0\t\n"
        "\tLeumi\tUSD\tEquity\tAMD\tAMD\t100\t150.00\t140.00\t15000\t15\t0\t\n"
        "\tLeumi\tNIS\tCash\t\t\t10000\t1\t1\t10000\t2.74\t0\t\n"
        "\tLeumi\tUSD\tCash\t\t\t5000\t1\t1\t5000\t5\t0\t\n"
        "\t\tSum:\t\t\t\t\t\t\t\t2317.74\t\t\n"
        "\n"
        "Real estate details:\n"
        "\tTel Aviv\tNIS\tHome\t\t\t\t\t\t2000000\t\t\t\n"
        "\n"
        "Current allocation:\n"
        "\tCategory\tCurrent %\tCurrent K USD\tTarget %\tTarget K USD\tDelta K\n"
        "\tCash\t0.33%\t7.74\t5%\t116\t108.26\n"
        "\tEquity\t99.67%\t2310\t60%\t1396\t-914\n"
        "\tGrand Total\t100%\t2317.74\t100%\t2317.74\t0\n"
        "\n"
        "NVDA Sales History:\n"
        "\t2026-Q1\t1600\t195.00\t\n"
        "\n"
        "Pensions/Saving accounts (as of Apr):\n"
        "\tAriel\tKupat Gemel\t250000\tNIS\n"
    )


@pytest.fixture
def snapshot_root(tmp_path, monkeypatch):
    root = tmp_path / "snapshots"
    root.mkdir()
    (root / "Family Finances Status - 26 Apr.tsv").write_text(
        _prior_tsv(), encoding="utf-8",
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


def _seed_bank_statement(
    session_factory,
    *,
    user_id: str,
    parser_name: str,
    period_end: date,
    closing_balance: float,
    balance_key: str,
    issuer: str = "leumi",
) -> int:
    """Insert an ExpenseStatement + a closing-balance transaction. Returns id."""
    sess = session_factory()
    try:
        uf = UserFile(
            user_id=user_id,
            sha256=f"x{period_end.isoformat()}{parser_name}".ljust(64, "0")[:64],
            original_name=f"{parser_name}.html",
            sanitized_name=f"{parser_name}.html",
            mime_type="text/html",
            kind="other",
            size_bytes=100,
            storage_path=f"/tmp/{parser_name}.html",
            source="intake_upload",
        )
        sess.add(uf)
        sess.flush()
        src = ExpenseSource(
            user_id=user_id, kind="bank", issuer=issuer,
            external_id=f"{parser_name}-acct", display_name=parser_name,
            active=True,
        )
        sess.add(src)
        sess.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=uf.id,
            period_start=period_end.replace(day=1),
            period_end=period_end,
            parsed_total_nis=closing_balance,
            parser_name=parser_name, parser_version="0.1.0",
            status="ok",
        )
        sess.add(stmt)
        sess.flush()
        txn = ExpenseTransaction(
            user_id=user_id, statement_id=stmt.id, source_id=src.id,
            occurred_on=period_end,
            merchant_raw="closing", merchant_normalized="closing",
            amount_nis=1.0, direction="debit", tx_type="debit",
            raw_row_json=json.dumps({balance_key: f"{closing_balance:.2f}"}),
        )
        sess.add(txn)
        sess.commit()
        return stmt.id
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGeneratorHappyPath:
    def test_writes_tsv_with_overridden_cash_rows(
        self, client_with_db, snapshot_root,
    ):
        _seed_user(client_with_db.app.state.session_factory)
        # Fresh Leumi NIS closing balance: 75,000 NIS.
        _seed_bank_statement(
            client_with_db.app.state.session_factory,
            user_id="ariel", parser_name="leumi_osh",
            period_end=date(2026, 5, 28),
            closing_balance=75_000.0, balance_key="balance",
        )
        # Fresh Leumi USD closing balance: 8,500 USD.
        _seed_bank_statement(
            client_with_db.app.state.session_factory,
            user_id="ariel", parser_name="leumi_usd",
            period_end=date(2026, 5, 28),
            closing_balance=8_500.0, balance_key="balance_usd",
        )
        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["tsv_persisted"] is True
        assert payload["leumi_nis_cash"] == 75_000.0
        assert payload["leumi_usd_cash"] == 8_500.0
        path = Path(payload["persisted_path"])
        assert path.exists()
        body = path.read_text(encoding="utf-8")
        # Leumi NIS Cash row should reflect 75K (not the prior 10K).
        assert "75000.00" in body, (
            "Expected refreshed Leumi NIS cash 75000.00 in TSV"
        )
        # Leumi USD Cash row should reflect 8500 (not the prior 5000).
        assert "8500.00" in body, (
            "Expected refreshed Leumi USD cash 8500.00 in TSV"
        )
        # Non-Leumi rows (Schwab) preserved.
        assert "schwab 876" in body
        # Carry-forward sections preserved.
        assert "Real estate details:" in body
        assert "NVDA Sales History:" in body
        assert "Pensions/Saving accounts" in body

    def test_snapshot_date_bumped_to_today(
        self, client_with_db, snapshot_root,
    ):
        _seed_user(client_with_db.app.state.session_factory)
        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        assert resp.status_code == 200
        today_iso = datetime.now(timezone.utc).date().isoformat()
        assert resp.json()["snapshot_date"] == today_iso

    def test_allocation_block_recomputed(
        self, client_with_db, snapshot_root,
    ):
        _seed_user(client_with_db.app.state.session_factory)
        _seed_bank_statement(
            client_with_db.app.state.session_factory,
            user_id="ariel", parser_name="leumi_osh",
            period_end=date(2026, 5, 28),
            closing_balance=200_000.0, balance_key="balance",
        )
        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        body = Path(resp.json()["persisted_path"]).read_text(encoding="utf-8")
        # Find the Cash row in Current allocation block; should reflect
        # the new total (200K NIS / 3.65 fx = ~54.8K USD plus prior 5K USD = ~59.8K).
        cash_alloc = [
            ln for ln in body.splitlines()
            if ln.split("\t")[1:2] == ["Cash"]
        ]
        assert cash_alloc, "Cash row should be in allocation block"
        cells = cash_alloc[0].split("\t")
        new_current_k = float(cells[3].replace(",", "").strip())
        assert 50.0 < new_current_k < 70.0, (
            f"Cash current_k_usd should reflect refreshed totals; "
            f"got {new_current_k}"
        )


class TestGeneratorEdgeCases:
    def test_no_prior_tsv_returns_clear_detail(
        self, client_with_db, tmp_path, monkeypatch,
    ):
        """Empty scan root -> response not_persisted with a helpful detail.
        No exception, no 500."""
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(empty_root))
        _seed_user(client_with_db.app.state.session_factory)
        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tsv_persisted"] is False
        assert "no prior" in (payload["detail"] or "").lower()

    def test_no_bank_statements_carries_forward_with_warning(
        self, client_with_db, snapshot_root,
    ):
        """When neither Leumi NIS nor Leumi USD statements exist, cash
        rows carry forward verbatim from prior + warnings are surfaced."""
        _seed_user(client_with_db.app.state.session_factory)
        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["tsv_persisted"] is True
        assert payload["leumi_nis_cash"] is None
        assert payload["leumi_usd_cash"] is None
        warnings = payload["warnings"]
        # Both bank-source warnings should be present.
        assert any("Leumi NIS" in w for w in warnings)
        assert any("Leumi USD" in w for w in warnings)
        body = Path(payload["persisted_path"]).read_text(encoding="utf-8")
        # Cash rows preserve prior values (10000 NIS / 5000 USD).
        assert "10000" in body
        assert "5000" in body

    def test_fallback_to_older_statement_on_extraction_failure(
        self, client_with_db, snapshot_root,
    ):
        """Codex zigzag v2 IMPORTANT (2026-05-29): if the newest Leumi
        NIS statement has a malformed raw_row_json, the lookup should
        fall back to the next-older statement instead of returning None
        and silently staling the cash row."""
        _seed_user(client_with_db.app.state.session_factory)
        # Older statement -- valid.
        _seed_bank_statement(
            client_with_db.app.state.session_factory,
            user_id="ariel", parser_name="leumi_osh",
            period_end=date(2026, 4, 28),
            closing_balance=42_000.0, balance_key="balance",
        )
        # Newer statement -- inject a malformed raw_row_json.
        sess = client_with_db.app.state.session_factory()
        try:
            uf = UserFile(
                user_id="ariel", sha256="bad" + "0" * 61,
                original_name="bad.html", sanitized_name="bad.html",
                mime_type="text/html", kind="other",
                size_bytes=100, storage_path="/tmp/bad.html",
                source="intake_upload",
            )
            sess.add(uf)
            sess.flush()
            src = ExpenseSource(
                user_id="ariel", kind="bank", issuer="leumi",
                external_id="leumi_osh-bad", display_name="Leumi Osh (bad)",
                active=True,
            )
            sess.add(src)
            sess.flush()
            stmt = ExpenseStatement(
                user_id="ariel", source_id=src.id, file_id=uf.id,
                period_start=date(2026, 5, 1), period_end=date(2026, 5, 28),
                parsed_total_nis=0,
                parser_name="leumi_osh", parser_version="0.1.0", status="ok",
            )
            sess.add(stmt)
            sess.flush()
            # Transaction with raw_row_json missing the "balance" key.
            txn = ExpenseTransaction(
                user_id="ariel", statement_id=stmt.id, source_id=src.id,
                occurred_on=date(2026, 5, 28),
                merchant_raw="bad", merchant_normalized="bad",
                amount_nis=1.0, direction="debit", tx_type="debit",
                raw_row_json='{"some_other_field": "garbage"}',
            )
            sess.add(txn)
            sess.commit()
        finally:
            sess.close()

        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        # The fallback should have recovered the 42K NIS from April.
        assert payload["leumi_nis_cash"] == 42_000.0, (
            f"Fallback didn't recover: {payload}"
        )
        # Warnings should mention the fallback.
        assert any(
            "Recovered Leumi NIS cash via fallback" in w
            for w in payload["warnings"]
        ), payload["warnings"]

    def test_leumi_usd_picked_for_usd_cash_only(
        self, client_with_db, snapshot_root,
    ):
        """Codex-zigzag (a)#3 regression: a Leumi USD statement must NOT
        override the NIS cash row. Only the USD cash row sees its data."""
        _seed_user(client_with_db.app.state.session_factory)
        # Only USD statement seeded.
        _seed_bank_statement(
            client_with_db.app.state.session_factory,
            user_id="ariel", parser_name="leumi_usd",
            period_end=date(2026, 5, 28),
            closing_balance=12_000.0, balance_key="balance_usd",
        )
        resp = client_with_db.post(
            "/api/portfolio/generate-tsv",
            data={"user_id": "ariel"},
        )
        payload = resp.json()
        assert payload["leumi_usd_cash"] == 12_000.0
        assert payload["leumi_nis_cash"] is None
        body = Path(payload["persisted_path"]).read_text(encoding="utf-8")
        # USD cash refreshed.
        assert "12000.00" in body
        # NIS cash retained from prior (10000).
        assert "10000" in body


def test_refresh_inserts_missing_leumi_usd_cash_row():
    """generate-tsv must INSERT a Leumi USD cash row when the prior TSV lacked
    one but a USD balance exists — not just refresh existing rows (codex)."""
    from argosy.services.portfolio_ingest.tsv_generator import (
        _refresh_cash_rows_in_position_block,
    )
    prior = [
        "Bank account / funds allocation",
        "\tLeumi\tNIS\tCash\t\t\t\t\t\t58944.86\t20.04\t\t",
        "\tLeumi\tUSD\tEquity\tVOO\tVOO\t20\t678\t572\t13564\t13.56\t\t",
        "Current allocation:",
    ]
    out = _refresh_cash_rows_in_position_block(
        prior_lines=prior, leumi_nis_cash=58944.86,
        leumi_usd_cash=264997.33, fx_usd_nis=2.94161,
    )
    usd_cash = [
        ln for ln in out
        if ln.split("\t")[1:4] == ["Leumi", "USD", "Cash"]
    ]
    assert len(usd_cash) == 1, "a Leumi USD cash row was inserted"
    assert usd_cash[0].split("\t")[9] == "264997.33"
    # Inserted right after the NIS cash row (not at the very end).
    assert out[1].split("\t")[1:4] == ["Leumi", "NIS", "Cash"]
    assert out[2].split("\t")[1:4] == ["Leumi", "USD", "Cash"]


def test_refresh_inserts_usd_row_even_when_nis_extraction_failed():
    """If NIS balance couldn't be extracted (leumi_nis_cash=None) but USD did,
    the USD row must still insert right after the (carried) NIS row, inside the
    position block — not get appended after the whole TSV (codex r2)."""
    from argosy.services.portfolio_ingest.tsv_generator import (
        _refresh_cash_rows_in_position_block,
    )
    prior = [
        "Bank account / funds allocation",
        "\tLeumi\tNIS\tCash\t\t\t\t\t\t58944.86\t20.04\t\t",
        "\tLeumi\tUSD\tEquity\tVOO\tVOO\t20\t678\t572\t13564\t13.56\t\t",
        "Current allocation:",
    ]
    out = _refresh_cash_rows_in_position_block(
        prior_lines=prior, leumi_nis_cash=None,
        leumi_usd_cash=264997.33, fx_usd_nis=2.94161,
    )
    # USD cash row inserted directly after the NIS cash row, before the
    # "Current allocation:" terminator (i.e. inside the position block).
    assert out[1].split("\t")[1:4] == ["Leumi", "NIS", "Cash"]
    assert out[2].split("\t")[1:4] == ["Leumi", "USD", "Cash"]
    assert out[-1].lower().startswith("current allocation")
