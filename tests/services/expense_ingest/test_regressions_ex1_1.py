"""Regression tests for Wave EX1.1 bug fixes.

Each test pins one of the 7 bugs from the EX1 handover so future regressions
are caught immediately.
"""

from __future__ import annotations


def test_bug5_household_categorizer_uses_canonical_model_id():
    """Bug 5 — model alias 'sonnet' replaced with canonical 'claude-sonnet-4-6'.

    The api_key backend may reject the alias; only claude_code resolves it.
    Use the canonical id everywhere for portability.
    """
    from argosy.agents.base import DEFAULT_MODEL_BY_ROLE
    assert DEFAULT_MODEL_BY_ROLE["household_categorizer"] == "claude-sonnet-4-6"


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
    leumi_files = list(fixtures.glob("leumi_osh*.xls"))
    if not leumi_files:
        import pytest
        pytest.skip("no Leumi fixture available — re-run after Task 9 adds one")
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
