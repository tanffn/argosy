import sqlite3

from scripts import deploy_discord_when_idle as deploy


def test_deploy_checks_real_job_rows_without_modifying_them(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    (tmp_path / "db").mkdir()
    database = tmp_path / "db" / "argosy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE job_runs(id INTEGER,job_name TEXT,started_at TEXT,status TEXT)")
        connection.executemany("INSERT INTO job_runs VALUES(?,?,?,?)", [
            (1, "discovery_funnel", "2026-09-19 13:36:20", "running"),
            (2, "completed", "2026-09-19 12:00:00", "ok"),
        ])
    assert deploy.active_jobs() == [{"id": 1, "name": "discovery_funnel", "started_at": "2026-09-19 13:36:20"}]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM job_runs WHERE id=1").fetchone()[0] == "running"


def test_deploy_defers_without_invoking_reload_when_work_is_active(monkeypatch):
    clock = iter([0, 0, 61])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    monkeypatch.setattr(deploy, "active_jobs", lambda: [{"id": 1, "name": "research"}])
    states = []
    monkeypatch.setattr(deploy, "record", lambda state, **kwargs: states.append(state))
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not reload")))
    assert deploy.main(1) == 2
    assert states == ["waiting_for_jobs", "deferred"]
