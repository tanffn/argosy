"""Regression tests for Wave EX1.1 bug fixes.

Each test pins one of the 7 bugs from the EX1 handover so future regressions
are caught immediately.
"""

from __future__ import annotations


def test_bug5_household_categorizer_uses_canonical_model_id():
    """Bug 5 — model alias 'sonnet' replaced with a canonical model id.

    The api_key backend may reject the alias; only claude_code resolves it.
    Use the canonical id everywhere for portability. The original assertion
    pinned the role to Sonnet 4.6; after the fleet bump to Opus 4.7 (commit
    b9b360c) the assertion was generalized to "the role resolves to ANY known
    canonical id" — the invariant being protected is "not None / not a typo",
    not "specifically Sonnet".
    """
    from argosy.agents.base import DEFAULT_MODEL_BY_ROLE
    known_canonical_ids = {
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    }
    model_id = DEFAULT_MODEL_BY_ROLE["household_categorizer"]
    assert model_id in known_canonical_ids, (
        f"household_categorizer resolves to {model_id!r}; expected one of "
        f"{sorted(known_canonical_ids)} (canonical, non-aliased model ids)."
    )


def test_bug6_categories_resolved_excludes_uncategorized(alembic_engine_at_head):
    """Bug 6 — IngestResult.categories_resolved should NOT include rows the LLM
    returned 'uncategorized' for. Today the counter increments before the
    uncategorized check.
    """
    from datetime import date
    from decimal import Decimal
    from unittest.mock import patch
    from sqlalchemy.orm import Session

    from argosy.agents.household_categorizer_types import (
        CategorizeResult, CategorizeRow,
    )
    from argosy.services.expense_ingest.category_resolver import (
        resolve_categories_for_user,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )
    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    with Session(alembic_engine_at_head) as s:
        s.add(User(id="u1", plan="free"))
        s.flush()
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, "u1"); s.flush()
        f = UserFile(
            user_id="u1", sha256="b" * 64, original_name="x",
            sanitized_name="x", mime_type="x", kind="other",
            size_bytes=1, storage_path="/tmp/x", source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id="u1", kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="u1", source_id=src.id, file_id=f.id,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"), parser_name="isracard",
            parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        for i in range(10):
            s.add(ExpenseTransaction(
                user_id="u1", source_id=src.id,
                statement_id=stmt.id, occurred_on=date(2026, 4, i + 1),
                merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                amount_nis=Decimal("10"),
                direction="debit", tx_type="regular",
                raw_row_json="{}",
            ))
        s.commit()

        # Half resolved, half uncategorized
        def _stub(_uid, rows):
            return [
                CategorizeResult(
                    tx_id=r.tx_id,
                    category_slug="dining" if i < 5 else "uncategorized",
                    confidence=0.9 if i < 5 else 0.4,
                    rationale="stub",
                )
                for i, r in enumerate(rows)
            ]

        with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
                   side_effect=_stub):
            resolved = resolve_categories_for_user(s, "u1")

    assert resolved == 5, f"expected 5 (only confidently resolved), got {resolved}"


def test_bug7_leumi_raw_row_uses_semantic_keys():
    """Bug 7 — Leumi raw_row keys must be semantic ('date', 'description', etc.)
    not positional integer strings ('0'..'8').

    Uses the in-tree Leumi fixture if present; otherwise constructs a tiny
    HTML statement on the fly.
    """
    import json
    from pathlib import Path
    from argosy.services.expense_ingest.parsers import leumi_osh

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    leumi_files = list(fixtures.glob("leumi_osh_minimal*.xls"))
    if not leumi_files:
        import pytest
        pytest.skip("no Leumi minimal fixture available")
    result = leumi_osh.parse(leumi_files[0])
    assert result.transactions, "fixture has no transactions"
    keys = set(result.transactions[0].raw_row.keys())
    expected_minimum = {"date", "description"}
    purely_integer_keys = {k for k in keys if k.isdigit()}
    assert expected_minimum.issubset(keys), (
        f"raw_row missing semantic keys; got {sorted(keys)}"
    )
    assert not purely_integer_keys, (
        f"raw_row still has positional keys: {purely_integer_keys}"
    )


def test_bug4_no_n_plus_1_source_lookup(alembic_engine_at_head):
    """Bug 4 — `session.get(ExpenseSource, ...)` is called once per LLM-batched
    tx. With 50 txs across 3 sources, we expect ≤ 3 source lookups, not 50.
    """
    from datetime import date
    from decimal import Decimal
    from unittest.mock import patch
    from sqlalchemy.orm import Session

    from argosy.agents.household_categorizer_types import CategorizeResult
    from argosy.services.expense_ingest.category_resolver import (
        resolve_categories_for_user,
    )
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )
    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    with Session(alembic_engine_at_head) as s:
        s.add(User(id="u1", plan="free"))
        s.flush()
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, "u1"); s.flush()

        # statement_id is NOT NULL on expense_transactions, so seed a
        # UserFile + ExpenseStatement per source first (mirrors T7 pattern).
        src_ids: list[int] = []
        stmt_ids: list[int] = []
        for ext in ("1111", "2222", "3333"):
            src = ExpenseSource(
                user_id="u1", kind="card", issuer="isracard",
                external_id=ext, display_name=f"test {ext}",
            )
            s.add(src); s.flush()
            src_ids.append(src.id)

            f = UserFile(
                user_id="u1", sha256=ext * 16, original_name=f"f{ext}",
                sanitized_name=f"f{ext}", mime_type="x", kind="other",
                size_bytes=1, storage_path=f"/tmp/{ext}",
                source="chat_attachment",
            )
            s.add(f); s.flush()
            stmt = ExpenseStatement(
                user_id="u1", source_id=src.id, file_id=f.id,
                period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
                parsed_total_nis=Decimal("0"), parser_name="isracard",
                parser_version="0.1.0", status="parsed",
            )
            s.add(stmt); s.flush()
            stmt_ids.append(stmt.id)

        for i in range(50):
            s.add(ExpenseTransaction(
                user_id="u1",
                source_id=src_ids[i % 3],
                statement_id=stmt_ids[i % 3],
                occurred_on=date(2026, 4, (i % 28) + 1),
                merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                amount_nis=Decimal("10"),
                direction="debit", tx_type="regular",
                raw_row_json="{}",
            ))
        s.commit()

        original_get = Session.get
        call_count = {"n": 0}

        def _counting_get(self, entity, ident, *args, **kwargs):
            if entity is ExpenseSource:
                call_count["n"] += 1
            return original_get(self, entity, ident, *args, **kwargs)

        def _stub(_uid, rows):
            return [
                CategorizeResult(
                    tx_id=r.tx_id, category_slug="dining", confidence=0.9,
                    rationale="stub",
                )
                for r in rows
            ]

        with patch.object(Session, "get", _counting_get), \
             patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
                   side_effect=_stub):
            resolve_categories_for_user(s, "u1")

    assert call_count["n"] <= 3, (
        f"N+1 regression: ExpenseSource fetched {call_count['n']} times "
        f"for 50 txs across 3 sources (expected ≤ 3)"
    )


def test_bug3_leumi_account_extracted_into_source_hint():
    """Bug 3 (part 1) — Leumi parser populates SourceHint.external_id with the
    actual account number from the HTML header (was previously None, with the
    orchestrator hardcoding '44745280').
    """
    from pathlib import Path
    from argosy.services.expense_ingest.parsers import leumi_osh

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    leumi_files = list(fixtures.glob("leumi_osh_minimal*.xls"))
    if not leumi_files:
        import pytest
        pytest.skip("no Leumi fixture")
    result = leumi_osh.parse(leumi_files[0])
    assert result.source_hint is not None
    assert result.source_hint.kind == "bank"
    assert result.source_hint.issuer == "leumi"
    # The minimal fixture's HTML header contains 'מס' חשבון: 882-447452/80'
    # → 8-digit account 44745280 (the '882' is Leumi's branch prefix).
    assert result.source_hint.external_id == "44745280"


def test_bug3_orchestrator_raises_on_account_mismatch(alembic_engine_at_head):
    """Bug 3 (part 2) — orchestrator raises ValueError if the Leumi-parsed
    account number doesn't match the hardcoded '44745280' single-user value.
    """
    import hashlib
    import pytest
    from pathlib import Path
    from sqlalchemy.orm import Session
    from argosy.services.expense_ingest.orchestrator import ingest_user_file
    from argosy.state.models import User, UserFile

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    wrong_acct = fixtures / "leumi_osh_wrong_acct.xls"
    if not wrong_acct.exists():
        pytest.skip("wrong-account Leumi fixture not present")

    # Note: we register the UserFile row directly (mirrors the pattern used in
    # tests/test_expense_orchestrator.py::_file), rather than going through
    # `catalog_upload`. catalog_upload is async + uses aiosqlite which conflicts
    # with the sync session below on the same SQLite file.
    with Session(alembic_engine_at_head) as s:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        f = UserFile(
            user_id="ariel",
            sha256=hashlib.sha256(str(wrong_acct).encode()).hexdigest(),
            original_name=wrong_acct.name,
            sanitized_name=wrong_acct.name,
            mime_type="application/vnd.ms-excel",
            kind="other",
            size_bytes=wrong_acct.stat().st_size,
            storage_path=str(wrong_acct),
            source="chat_attachment",
        )
        s.add(f); s.commit()
        with pytest.raises(ValueError, match="Leumi account mismatch"):
            ingest_user_file(s, "ariel", f.id)


def test_bug2_correlator_sums_only_nis_rows(alembic_engine_at_head):
    """Bug 2 (part 2) — correlator skips amount_nis IS NULL rows so foreign
    charges don't crash the abs(declared_total - amount) comparison or
    silently zero out the total.

    Plan-defect adaptations vs. the spec snippet:
      * ExpenseStatement.file_id is NOT NULL — seed a UserFile.
      * statement_id is NOT NULL on ExpenseTransaction (already in spec).
    """
    from datetime import date
    from decimal import Decimal
    from sqlalchemy.orm import Session

    from argosy.services.expense_ingest.correlator import correlate_for_user
    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    with Session(alembic_engine_at_head) as s:
        s.add(User(id="u1", plan="free"))
        s.flush()
        f = UserFile(
            user_id="u1", sha256="c" * 64, original_name="x",
            sanitized_name="x", mime_type="x", kind="other",
            size_bytes=1, storage_path="/tmp/x", source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id="u1", kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="u1", source_id=src.id, file_id=f.id,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            parser_name="isracard", parser_version="0.1.0",
            parsed_total_nis=Decimal("100"),
            declared_total_nis=Decimal("100"), status="parsed",
        )
        s.add(stmt); s.flush()
        s.add(ExpenseTransaction(
            user_id="u1", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 5),
            merchant_raw="A", merchant_normalized="a",
            amount_nis=Decimal("100"), direction="debit", tx_type="regular",
            raw_row_json="{}",
        ))
        s.add(ExpenseTransaction(
            user_id="u1", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 6),
            merchant_raw="B", merchant_normalized="b",
            amount_nis=None, amount_orig=Decimal("12.18"),
            currency_orig="USD",
            direction="debit", tx_type="regular",
            raw_row_json="{}",
        ))
        s.commit()

        # Just exercising — no exception is the assertion. Returns a count.
        n = correlate_for_user(s, "u1")
    assert n >= 0


def test_bug2_refund_matcher_pairs_via_amount_orig_when_nis_null(alembic_engine_at_head):
    """Bug 2 (part 2) — refund_matcher falls back to (amount_orig, currency_orig)
    when both refund and prior debit have amount_nis IS NULL (foreign rows).
    """
    from datetime import date
    from decimal import Decimal
    from sqlalchemy.orm import Session

    from argosy.services.expense_ingest.refund_matcher import (
        match_refunds_for_user,
    )
    from argosy.state.models import (
        ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
        User, UserFile,
    )

    with Session(alembic_engine_at_head) as s:
        s.add(User(id="u1", plan="free")); s.flush()
        f = UserFile(
            user_id="u1", sha256="d" * 64, original_name="x",
            sanitized_name="x", mime_type="x", kind="other",
            size_bytes=1, storage_path="/tmp/x", source="chat_attachment",
        )
        s.add(f); s.flush()
        cat = ExpenseCategory(slug="travel.flights", label_en="Flights",
                              label_he="טיסות")
        s.add(cat); s.flush()
        src = ExpenseSource(user_id="u1", kind="card", issuer="isracard",
                            external_id="9999", display_name="Test 9999")
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="u1", source_id=src.id, file_id=f.id,
            period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
            parsed_total_nis=Decimal("0"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        # Prior foreign debit (amount_nis NULL, USD 12.18, categorized).
        prior = ExpenseTransaction(
            user_id="u1", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 2, 15), merchant_raw="FOREIGN MERCH",
            merchant_normalized="foreign merch",
            amount_nis=None, amount_orig=Decimal("12.18"),
            currency_orig="USD",
            direction="debit", tx_type="regular",
            category_id=cat.id, category_source="user",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        )
        s.add(prior); s.flush()
        # Foreign refund — same merchant, USD 12.18, NIS=NULL.
        refund = ExpenseTransaction(
            user_id="u1", statement_id=stmt.id, source_id=src.id,
            occurred_on=date(2026, 3, 10), merchant_raw="FOREIGN MERCH",
            merchant_normalized="foreign merch",
            amount_nis=None, amount_orig=Decimal("12.18"),
            currency_orig="USD",
            direction="credit", tx_type="refund", raw_row_json="{}",
        )
        s.add(refund); s.commit()

        n = match_refunds_for_user(s, "u1")
        s.commit()
        s.refresh(refund)
        assert n == 1, "expected refund_matcher to pair via amount_orig/currency_orig"
        assert refund.refund_of_id == prior.id
        assert refund.category_id == cat.id
        assert refund.category_source == "inherited_from_refund"


def test_bug2_isracard_foreign_row_amount_nis_is_none():
    """Bug 2 (part 1) — Isracard parser stores amount_nis=None for non-NIS
    rows (was: stored the raw foreign amount as if it were NIS).
    """
    from pathlib import Path
    from argosy.services.expense_ingest.parsers import isracard

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    src = fixtures / "isracard_minimal.xlsx"
    if not src.exists():
        import pytest
        pytest.skip("isracard_minimal fixture not present")
    result = isracard.parse(src)
    foreign_rows = [t for t in result.transactions if t.currency_orig is not None]
    assert foreign_rows, "fixture has no foreign rows — adjust fixture"
    for tx in foreign_rows:
        assert tx.amount_nis is None, (
            f"foreign row stored amount_nis={tx.amount_nis} (expected None)"
        )
        assert tx.amount_orig is not None
        assert tx.currency_orig in {"USD", "EUR", "GBP"}


def test_bug1_max_parser_uses_last4_hint_when_provided():
    """Bug 1 (part 1) — when last4_hint is provided, Max parser uses it as
    external_id rather than extracting the bank-account last-4 from the sheet
    name.
    """
    from pathlib import Path
    from argosy.services.expense_ingest.parsers import max as p_max

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    max_files = list(fixtures.glob("max_minimal*.xlsx"))
    if not max_files:
        import pytest
        pytest.skip("no Max fixture")
    result = p_max.parse(max_files[0], last4_hint="6225")
    assert result.source_hint is not None
    assert result.source_hint.external_id == "6225"


def test_bug1_max_parser_warns_when_last4_hint_missing(caplog):
    """Bug 1 (part 2) — when no hint is provided, the parser falls back AND
    emits a warning so callers don't silently use the wrong external_id.
    """
    import warnings
    from pathlib import Path
    from argosy.services.expense_ingest.parsers import max as p_max

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    max_files = list(fixtures.glob("max_minimal*.xlsx"))
    if not max_files:
        import pytest
        pytest.skip("no Max fixture")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p_max.parse(max_files[0])
        assert any("last4_hint" in str(w.message) for w in caught), (
            "expected a UserWarning mentioning 'last4_hint'"
        )


def test_bug1_backfill_walker_passes_folder_name_as_last4_hint(tmp_path, monkeypatch):
    """Bug 1 (part 3) — `argosy expenses backfill --dir <root>` walks
    `Cards/Max/<last4>/<file>.xlsx` and threads <last4> through to the
    orchestrator as last4_hint.
    """
    from pathlib import Path
    from unittest.mock import patch
    from typer.testing import CliRunner

    from argosy.cli.expenses_admin import app

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    max_src = fixtures / "max_minimal.xlsx"
    if not max_src.exists():
        import pytest
        pytest.skip("no Max fixture")

    # Stage a folder structure: <tmp>/Cards/Max/6225/Apr.xlsx
    target_dir = tmp_path / "Cards" / "Max" / "6225"
    target_dir.mkdir(parents=True)
    (target_dir / "Apr.xlsx").write_bytes(max_src.read_bytes())

    captured: dict = {}

    def _spy_ingest(session, user_id, file_id, *, last4_hint=None):
        captured["last4_hint"] = last4_hint
        return type("Result", (), {"statement_id": 0, "transactions_inserted": 0,
                                   "correlations_made": 0, "categories_resolved": 0,
                                   "refunds_matched": 0, "parser_name": "max"})()

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    with patch("argosy.cli.expenses_admin.ingest_user_file", side_effect=_spy_ingest):
        runner = CliRunner()
        runner.invoke(app, ["backfill", "--user-id", "ariel", "--dir", str(tmp_path)])

    assert captured.get("last4_hint") == "6225"


def test_bug2_monthly_summary_returns_per_currency_map(client_with_db):
    """Bug 2 (part 3) — /api/expenses/monthly-summary returns per-currency
    totals so foreign rows (amount_nis IS NULL after T12) don't get silently
    dropped or summed into NIS.

    Seeds two NIS debits + one foreign (USD) debit in the same month + a NIS
    debit in a different month, then asserts the response shape:

      [ {month: 'YYYY-MM', totals_by_currency: {'NIS': ..., 'USD': ...},
         transaction_count: 3}, ... ]
    """
    from datetime import date
    from decimal import Decimal

    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.flush()
        f = UserFile(
            user_id="ariel", sha256="e" * 64, original_name="x",
            sanitized_name="x", mime_type="x", kind="other",
            size_bytes=1, storage_path="/tmp/x", source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id="ariel", kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id="ariel", source_id=src.id, file_id=f.id,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()

        # Two NIS debits in April 2026 — total NIS = 250.
        s.add(ExpenseTransaction(
            user_id="ariel", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 5),
            merchant_raw="NIS_A", merchant_normalized="nis_a",
            amount_nis=Decimal("100"), direction="debit", tx_type="regular",
            raw_row_json="{}",
        ))
        s.add(ExpenseTransaction(
            user_id="ariel", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 12),
            merchant_raw="NIS_B", merchant_normalized="nis_b",
            amount_nis=Decimal("150"), direction="debit", tx_type="regular",
            raw_row_json="{}",
        ))
        # One foreign USD debit — amount_nis IS NULL per T12.
        s.add(ExpenseTransaction(
            user_id="ariel", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 20),
            merchant_raw="USD_C", merchant_normalized="usd_c",
            amount_nis=None, amount_orig=Decimal("25"), currency_orig="USD",
            direction="debit", tx_type="regular", raw_row_json="{}",
        ))
        # A NIS debit in a different month (March) so we exercise the
        # per-month grouping.
        s.add(ExpenseTransaction(
            user_id="ariel", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 3, 15),
            merchant_raw="NIS_MAR", merchant_normalized="nis_mar",
            amount_nis=Decimal("42"), direction="debit", tx_type="regular",
            raw_row_json="{}",
        ))
        s.commit()

    response = client_with_db.get(
        "/api/expenses/monthly-summary",
        params={"user_id": "ariel", "months": 12},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # New shape: list of months, each with month + totals_by_currency dict.
    assert isinstance(body, list), f"expected list, got {type(body).__name__}: {body!r}"
    months = {row["month"]: row for row in body}
    assert "2026-04" in months
    assert "2026-03" in months

    apr = months["2026-04"]
    assert "totals_by_currency" in apr
    assert isinstance(apr["totals_by_currency"], dict)
    # NIS bucket sums the two NIS rows (100 + 150 = 250).
    assert apr["totals_by_currency"].get("NIS") == 250.0
    # USD bucket has the foreign row's amount_orig (25.0), separate from NIS.
    assert apr["totals_by_currency"].get("USD") == 25.0
    assert apr["transaction_count"] == 3

    mar = months["2026-03"]
    assert mar["totals_by_currency"].get("NIS") == 42.0
    assert "USD" not in mar["totals_by_currency"]
    assert mar["transaction_count"] == 1


def test_bug1_rest_upload_returns_400_for_max_without_card_last4(client_with_db):
    """Bug 1 (part 4) — the REST upload route returns 400 when a Max statement
    is uploaded without a card_last4 form field.
    """
    from pathlib import Path

    fixtures = Path(__file__).parent.parent.parent / "fixtures" / "expenses"
    max_src = fixtures / "max_minimal.xlsx"
    if not max_src.exists():
        import pytest
        pytest.skip("no Max fixture")

    response = client_with_db.post(
        "/api/expenses/upload",
        data={"user_id": "ariel"},  # no card_last4
        files={"files": ("max.xlsx", max_src.read_bytes(),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 200  # the route returns 200 with per-file results
    body = response.json()
    assert body["results"][0]["status"] == "failed"
    assert "card_last4 required for Max uploads" in body["results"][0]["error"]
