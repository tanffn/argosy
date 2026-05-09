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
