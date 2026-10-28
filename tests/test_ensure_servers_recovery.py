"""Automatic recovery distinguishes idle listeners, real work and app errors."""
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import ensure_servers as launcher


@pytest.fixture
def root(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "tmp").mkdir()
    with sqlite3.connect(tmp_path / "db/argosy.db") as db:
        db.executescript("""
            CREATE TABLE job_runs(id INTEGER PRIMARY KEY, job_name TEXT, status TEXT);
            CREATE TABLE chat_analysis_requests(status TEXT);
            CREATE TABLE chat_turns(status TEXT);
            INSERT INTO job_runs VALUES(1,'discord_advisor','running');
        """)
    return tmp_path


def test_idle_discord_does_not_block_recovery(root):
    assert launcher.active_jobs(root) == []


@pytest.mark.parametrize("table,status", [("chat_analysis_requests", "running"),
    ("chat_analysis_requests", "queued"), ("chat_analysis_requests", "cancelling"),
    ("chat_turns", "queued"), ("chat_turns", "delivery_pending")])
def test_active_conversation_is_protected(root, table, status):
    with sqlite3.connect(root / "db/argosy.db") as db:
        db.execute(f"INSERT INTO {table} VALUES(?)", (status,))
    assert launcher.active_jobs(root)


def test_research_job_is_protected_even_with_idle_listener(root):
    with sqlite3.connect(root / "db/argosy.db") as db:
        db.execute("INSERT INTO job_runs VALUES(2,'discovery_funnel','running')")
    assert launcher.active_jobs(root) == [(2, "discovery_funnel")]


def test_missing_conversation_schema_cannot_authorize_a_restart(root):
    with sqlite3.connect(root / "db/argosy.db") as db:
        db.execute("DROP TABLE chat_turns")
    assert launcher.active_jobs(root)


def test_responding_application_error_is_not_a_socket_failure(root, monkeypatch):
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: {
        "alive": False, "identified": True, "http": 503, "application_status": "db_error"})
    def no_process_mutation(*args):
        raise AssertionError("A responding application issue must not restart a process")
    monkeypatch.setattr(launcher, "owned_roots", no_process_mutation)
    assert launcher.ensure("backend", root, 8000, {})["action"] == "skipped_application_issue"


def test_failed_listener_with_idle_discord_reaches_owned_restart(root, monkeypatch):
    dead = {"alive": False, "identified": False, "error": "connection refused"}
    healthy = {"alive": True, "identified": True, "http": 200}
    responses = iter([dead, dead, dead, healthy])
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: next(responses))
    process = SimpleNamespace(create_time=lambda: 0, is_running=lambda: True)
    monkeypatch.setattr(launcher, "owned_roots", lambda *a: [process])
    monkeypatch.setattr(launcher.psutil, "net_connections", lambda **k: [])
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    actions = []
    monkeypatch.setattr(launcher, "stop_owned", lambda *a: actions.append("stop_owned"))
    monkeypatch.setattr(launcher, "start_role", lambda *a: actions.append("start"))
    result = launcher.ensure("backend", root, 8000, {})
    assert result["action"] == "restarted"
    assert actions == ["stop_owned", "start"]
