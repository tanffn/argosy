"""Migration 0026 adds cache/thinking/citations telemetry columns to agent_reports."""
from __future__ import annotations

from sqlalchemy import inspect


def test_agent_reports_has_api_telemetry_columns(alembic_engine_at_head):
    insp = inspect(alembic_engine_at_head)
    columns = {c["name"] for c in insp.get_columns("agent_reports")}
    expected_new = {
        "cache_input_tokens",
        "cache_creation_tokens",
        "thinking_tokens",
        "citations_json",
    }
    missing = expected_new - columns
    assert not missing, f"agent_reports missing columns: {missing}"


def test_agent_reports_api_telemetry_defaults(alembic_engine_at_head):
    """New columns default to 0 / NULL so existing rows remain valid.

    SQLite reports server defaults as the SQL literal text (e.g. ``"'0'"``);
    we strip surrounding quotes so the assertion compares semantic values.
    """
    insp = inspect(alembic_engine_at_head)
    cols_by_name = {c["name"]: c for c in insp.get_columns("agent_reports")}

    def _default(name: str) -> str:
        return (cols_by_name[name]["default"] or "").strip("'")

    assert _default("cache_input_tokens") == "0"
    assert _default("cache_creation_tokens") == "0"
    assert _default("thinking_tokens") == "0"
    assert cols_by_name["citations_json"]["nullable"] is True
    assert cols_by_name["citations_json"]["default"] is None
