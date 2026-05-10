"""End-to-end orchestrator tests using synthetic fixture files."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from argosy.agents.household_categorizer_types import CategorizeResult
from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"

_SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")


def _file(s: Session, *, path: Path, mime: str) -> int:
    import hashlib
    # Use a stable per-path sha256 so multiple calls within a test don't collide
    # on the (user_id, sha256) partial unique index.
    fake_sha = hashlib.sha256(str(path).encode()).hexdigest()
    if s.get(User, "ariel") is None:
        s.add(User(id="ariel", plan="free"))
    s.flush()
    f = UserFile(
        user_id="ariel", sha256=fake_sha, original_name=path.name,
        sanitized_name=path.name, mime_type=mime, kind="other",
        size_bytes=path.stat().st_size, storage_path=str(path),
        source="chat_attachment",
    )
    s.add(f); s.flush()
    return f.id


def test_orchestrator_ingests_max_fixture(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        mock_llm.return_value = []   # nothing routes to LLM (all unambiguous ענף)
        file_id = _file(s, path=FIXTURES / "max_minimal.xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s.commit()
        result = ingest_user_file(s, "ariel", file_id)
        s.commit()
        assert result.statement_id is not None
        assert s.query(ExpenseTransaction).count() == 5
        assert s.query(ExpenseSource).filter_by(issuer="max").count() == 1


def test_orchestrator_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        mock_llm.return_value = []
        file_id = _file(s, path=FIXTURES / "max_minimal.xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s.commit()
        r1 = ingest_user_file(s, "ariel", file_id); s.commit()
        r2 = ingest_user_file(s, "ariel", file_id); s.commit()
        # Same statement_id, no new tx rows the second time
        assert r1.statement_id == r2.statement_id
        assert r2.transactions_inserted == 0
        assert s.query(ExpenseTransaction).count() == 5


def test_orchestrator_correlates_after_both_ingested(alembic_engine_at_head):
    """Ingest a Leumi statement first, then an Isracard — correlation should
    fire and mark the bank's card-payment row as is_card_payment."""
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        # The mock returns a category for everything to keep the resolver happy
        def fake(_user_id, rows):
            return [
                CategorizeResult(tx_id=r.tx_id,
                                 category_slug="discretionary.shopping_other",
                                 confidence=0.90, rationale="x")
                for r in rows
            ]
        mock_llm.side_effect = fake

        leumi_file_id = _file(s, path=FIXTURES / "leumi_osh_minimal.xls",
                               mime="application/vnd.ms-excel")
        s.commit()
        ingest_user_file(s, "ariel", leumi_file_id); s.commit()

        # Add an Isracard statement matching the Leumi card-payment row of 3319.44
        isracard_file_id = _file(s, path=FIXTURES / "isracard_minimal.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        s.commit()
        ingest_user_file(s, "ariel", isracard_file_id); s.commit()

        # Force the Isracard statement's declared_total + charge_date to align
        # with the Leumi 3319.44 / 15.04 row, then run correlation:
        from argosy.services.expense_ingest.correlator import correlate_for_user
        from datetime import date
        from decimal import Decimal
        ica_stmt = (s.query(ExpenseStatement)
                     .filter_by(parser_name="isracard").one())
        ica_stmt.declared_total_nis = Decimal("3319.44")
        ica_stmt.charge_date = date(2026, 4, 15)
        s.commit()
        correlate_for_user(s, "ariel"); s.commit()

        bank_tx = s.query(ExpenseTransaction).filter(
            ExpenseTransaction.merchant_raw.contains("מאסטרקרד")
        ).one()
        assert bank_tx.is_card_payment is True
        assert bank_tx.matched_statement_id == ica_stmt.id


@pytest.mark.skipif(not _SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset")
def test_orchestrator_registers_two_leumi_sources(alembic_engine_at_head):
    """Ingesting one Leumi NIS (Osh) file AND one Leumi USD (פמ"ח) file
    must register two distinct ExpenseSource rows — same issuer 'leumi',
    different external_ids (44745280 vs 44745200). Multi-account Leumi
    guard regression test.
    """
    samples_root = Path(_SAMPLES)
    nis_candidates = [
        p for p in samples_root.glob("**/Leumi/leumi_*.xls")
        if p.is_file() and p.name != "usd.xls"
    ]
    usd_candidates = sorted(samples_root.glob("**/Leumi/usd.xls"))
    if not nis_candidates or not usd_candidates:
        pytest.skip("need both Leumi NIS and USD live samples")

    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm:
        def _stub(_user_id, rows):
            return [
                CategorizeResult(tx_id=r.tx_id,
                                 category_slug="discretionary.shopping_other",
                                 confidence=0.90, rationale="x")
                for r in rows
            ]
        mock_llm.side_effect = _stub

        nis_file_id = _file(
            s, path=nis_candidates[0], mime="application/vnd.ms-excel",
        )
        s.commit()
        ingest_user_file(s, "ariel", nis_file_id)
        s.commit()

        usd_file_id = _file(
            s, path=usd_candidates[0], mime="application/vnd.ms-excel",
        )
        s.commit()
        ingest_user_file(s, "ariel", usd_file_id)
        s.commit()

        leumi_sources = s.query(ExpenseSource).filter_by(issuer="leumi").all()
        assert len(leumi_sources) == 2, (
            f"expected 2 leumi sources, got {[(x.issuer, x.external_id) for x in leumi_sources]}"
        )
        ext_ids = {src.external_id for src in leumi_sources}
        assert ext_ids == {"44745280", "44745200"}
