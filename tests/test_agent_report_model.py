"""AgentReport ORM exposes the new API telemetry columns introduced in 0026."""
from __future__ import annotations

from argosy.state.models import AgentReport


def test_agent_report_has_api_telemetry_attrs():
    fields = {c.key for c in AgentReport.__table__.columns}
    assert "cache_input_tokens" in fields
    assert "cache_creation_tokens" in fields
    assert "thinking_tokens" in fields
    assert "citations_json" in fields


def test_agent_report_column_defaults_match_migration():
    """Column defaults match migration 0026: 0 for token counts, NULL for citations_json.

    SQLAlchemy column defaults apply at INSERT time, not on unflushed instances,
    so we inspect the column metadata directly instead of constructing a row.
    """
    cols = AgentReport.__table__.columns

    # Token-count columns: NOT NULL with default=0 (python-side) and
    # server_default="0" (matches migration 0026's batch_op.add_column).
    for name in ("cache_input_tokens", "cache_creation_tokens", "thinking_tokens"):
        col = cols[name]
        assert col.nullable is False, f"{name} must be NOT NULL"
        assert col.default is not None and col.default.arg == 0, (
            f"{name} must have Python-side default=0"
        )
        assert col.server_default is not None and col.server_default.arg == "0", (
            f"{name} must have server_default='0'"
        )

    # citations_json: nullable Text, default None.
    citations = cols["citations_json"]
    assert citations.nullable is True
    assert citations.default is None or citations.default.arg is None
    assert citations.server_default is None
