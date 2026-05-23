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


# ---------------------------------------------------------------------------
# Wave B-UI Task 9 — sources_json column (migration 0027)
# ---------------------------------------------------------------------------


def test_agent_report_has_sources_json_attr():
    """Migration 0027 adds sources_json column to agent_reports."""
    fields = {c.key for c in AgentReport.__table__.columns}
    assert "sources_json" in fields


def test_agent_report_sources_json_column_defaults():
    """sources_json is nullable Text with Python-side default=None and no server_default."""
    col = AgentReport.__table__.columns["sources_json"]
    assert col.nullable is True
    assert col.default is None or col.default.arg is None
    assert col.server_default is None


# ---------------------------------------------------------------------------
# Wave B-UI follow-up Item 2 — run_correlation_id column (migration 0028)
# ---------------------------------------------------------------------------


def test_agent_report_has_run_correlation_id_attr():
    """Migration 0028 adds run_correlation_id column to agent_reports."""
    fields = {c.key for c in AgentReport.__table__.columns}
    assert "run_correlation_id" in fields


def test_agent_report_run_correlation_id_column_defaults():
    """run_correlation_id is nullable String(36) with Python-side default=None
    and no server_default (mirrors the sources_json pattern from migration 0027).
    """
    col = AgentReport.__table__.columns["run_correlation_id"]
    assert col.nullable is True
    # String(36) — tight fit for a uuid4 without hyphens (32) or with (36).
    assert col.type.length == 36
    assert col.default is None or col.default.arg is None
    assert col.server_default is None
