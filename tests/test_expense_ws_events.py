"""Smoke test that orchestrator emits expense.statement.parsed."""

from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.orchestrator import ingest_user_file
from argosy.state.models import User, UserFile

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_orchestrator_emits_statement_parsed_event(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm") as mock_llm, \
         patch("argosy.api.events.publish_event_threadsafe") as mock_pub:
        mock_llm.return_value = []
        s.add(User(id="ariel", plan="free")); s.flush()
        path = FIXTURES / "max_minimal.xlsx"
        f = UserFile(user_id="ariel", sha256="x"*64, original_name="m.xlsx",
                     sanitized_name="m.xlsx", mime_type="x", kind="other",
                     size_bytes=1, storage_path=str(path),
                     source="chat_attachment")
        s.add(f); s.commit()
        ingest_user_file(s, "ariel", f.id); s.commit()
        # The orchestrator should fire 'expense.statement.parsed' once
        names = [c.args[0] for c in mock_pub.call_args_list]
        assert "expense.statement.parsed" in names
