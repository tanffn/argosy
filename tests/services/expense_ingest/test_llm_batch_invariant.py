"""Diagnostic test: LLM batch invariant check.

When ``agent.categorize_batch`` returns a result list whose tx_ids don't
match the input chunk's tx_ids, the resolver should:
  1. Emit a WARNING via ``argosy.logging.get_logger(__name__)`` naming the
     missing/extra tx_ids.
  2. NOT raise — graceful degradation. The resolver loop already tolerates
     missing entries by treating them as 'uncategorized' (skipped).
  3. Log an INFO line per missing row at the call site, naming
     ``tx_id`` + ``merchant_normalized`` so operators can audit.

Capture strategy: instead of fighting structlog's
``cache_logger_on_first_use`` cross-test interactions (the BoundLoggerLazyProxy
is resolved once and bound to a stdlib logger that pytest's caplog can't
re-tap mid-session), we install a recorder by reaching into the resolver
module and replacing its ``log`` reference with a tiny shim object that
appends every call to a list. This is brittle by design — the test fails
loudly if the resolver ever changes how it acquires the logger.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session


def _seed_uncategorized_rows(s: Session, n: int = 3) -> list[int]:
    """Seed n uncategorized debit rows; return their tx ids."""
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults, seed_user_categories,
    )
    from argosy.state.models import (
        ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
    )

    s.add(User(id="u1", plan="free")); s.flush()
    seed_system_defaults(s); s.flush()
    seed_user_categories(s, "u1"); s.flush()

    f = UserFile(
        user_id="u1", sha256="z" * 64, original_name="x",
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

    txs: list[ExpenseTransaction] = []
    for i in range(n):
        tx = ExpenseTransaction(
            user_id="u1", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, i + 1),
            merchant_raw=f"M{i}", merchant_normalized=f"merchant_{i}",
            amount_nis=Decimal("10"),
            direction="debit", tx_type="regular",
            raw_row_json="{}",
        )
        s.add(tx)
        txs.append(tx)
    s.commit()
    return [tx.id for tx in txs]


class _LogRecorder:
    """Tiny shim that records every method call. Mimics enough of structlog's
    ``BoundLogger`` API for the resolver's two callsites (``warning``,
    ``info``) — anything else routes silently to a no-op.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def warning(self, event: str, **kw) -> None:
        self.calls.append(("warning", event, kw))

    def info(self, event: str, **kw) -> None:
        self.calls.append(("info", event, kw))

    def __getattr__(self, name):  # any other level: no-op
        return lambda *a, **kw: None


def test_llm_batch_invariant_warns_when_results_missing_tx(
    alembic_engine_at_head, monkeypatch,
):
    """If the agent returns fewer results than the input batch, the resolver
    logs a WARNING naming the missing tx_ids and continues (no crash).
    """
    from argosy.agents.household_categorizer_types import CategorizeResult
    from argosy.services.expense_ingest import category_resolver
    from argosy.services.expense_ingest.category_resolver import (
        resolve_categories_for_user,
    )

    rec = _LogRecorder()
    monkeypatch.setattr(category_resolver, "log", rec)

    with Session(alembic_engine_at_head) as s:
        ids = _seed_uncategorized_rows(s, n=3)

        # Stub: drop the LAST tx so input != results.
        def _stub_categorize_batch(self, rows, taxonomy):
            return [
                CategorizeResult(
                    tx_id=r.tx_id, category_slug="dining",
                    confidence=0.9, rationale="stub",
                )
                for r in rows[:-1]
            ]

        with patch(
            "argosy.agents.household_categorizer.HouseholdCategorizerAgent.categorize_batch",
            new=_stub_categorize_batch,
        ):
            # Should NOT raise — graceful degradation.
            resolved = resolve_categories_for_user(s, "u1")

    # 2/3 confidently resolved (the dropped tx stays uncategorized).
    assert resolved == 2, (
        f"expected 2 confidently resolved (one tx dropped); got {resolved}"
    )

    # Exactly one WARNING with event='llm_batch_mismatch'
    warnings_with_event = [
        kw for level, event, kw in rec.calls
        if level == "warning" and event == "llm_batch_mismatch"
    ]
    assert len(warnings_with_event) == 1, (
        f"expected 1 'llm_batch_mismatch' warning, got {len(warnings_with_event)}: "
        f"all calls = {rec.calls}"
    )
    w = warnings_with_event[0]
    assert w["input_size"] == 3
    assert w["result_size"] == 2
    assert ids[-1] in w["missing_tx_ids"], (
        f"expected dropped tx_id {ids[-1]} in missing_tx_ids; got {w}"
    )
    assert w["extra_tx_ids"] == []

    # And exactly one INFO line for the dropped row, naming merchant_normalized.
    skipped = [
        kw for level, event, kw in rec.calls
        if level == "info" and event == "llm_skipped_tx"
    ]
    assert len(skipped) == 1, (
        f"expected 1 'llm_skipped_tx' info, got {len(skipped)}: all={rec.calls}"
    )
    assert skipped[0]["tx_id"] == ids[-1]
    assert skipped[0]["merchant_normalized"] == "merchant_2"


def test_llm_batch_invariant_silent_when_results_match(
    alembic_engine_at_head, monkeypatch,
):
    """No warning when the result set matches the input set exactly."""
    from argosy.agents.household_categorizer_types import CategorizeResult
    from argosy.services.expense_ingest import category_resolver
    from argosy.services.expense_ingest.category_resolver import (
        resolve_categories_for_user,
    )

    rec = _LogRecorder()
    monkeypatch.setattr(category_resolver, "log", rec)

    with Session(alembic_engine_at_head) as s:
        _seed_uncategorized_rows(s, n=3)

        def _stub_categorize_batch(self, rows, taxonomy):
            return [
                CategorizeResult(
                    tx_id=r.tx_id, category_slug="dining",
                    confidence=0.9, rationale="stub",
                )
                for r in rows
            ]

        with patch(
            "argosy.agents.household_categorizer.HouseholdCategorizerAgent.categorize_batch",
            new=_stub_categorize_batch,
        ):
            resolved = resolve_categories_for_user(s, "u1")

    assert resolved == 3
    bad = [
        (level, event) for level, event, _kw in rec.calls
        if event in ("llm_batch_mismatch", "llm_skipped_tx")
    ]
    assert not bad, f"expected no diagnostic logs, got {bad}"
