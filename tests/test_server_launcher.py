"""Launcher health contract and restart safety; no production server mutations."""
import importlib.util
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_spec = importlib.util.spec_from_file_location("server_launcher", Path(__file__).parents[1] / "scripts/ensure_servers.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


@pytest.fixture
def local_http():
    state = {"status": 200, "body": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(state["body"].encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join()
    server.server_close()


def test_application_error_is_not_a_dead_process(local_http):
    state, url = local_http
    state["body"] = json.dumps({"status": "error", "db": "error", "git_sha": "abc", "started_at": "today"})
    result = launcher.probe(url, "backend")
    assert result["alive"]
    assert result["application_status"] == "error"


def test_foreign_service_is_not_argosy(local_http):
    state, url = local_http
    state["body"] = '{"status":"ok"}'
    assert not launcher.probe(url, "backend")["identified"]
    state["body"] = '<html>Other application /_next/</html>'
    assert not launcher.probe(url, "ui")["identified"]


def test_ui_success_and_error(local_http):
    state, url = local_http
    state["body"] = '<html>Argosy<script src="/_next/app.js"></script></html>'
    assert launcher.probe(url, "ui")["alive"]
    state["status"] = 500
    assert not launcher.probe(url, "ui")["alive"]


def test_healthy_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: {"alive": True})
    monkeypatch.setattr(launcher, "start_role", lambda *a: pytest.fail("healthy server restarted"))
    assert launcher.ensure("backend", tmp_path, 8000, {})["action"] == "skipped_healthy"


def test_check_only_never_changes_state(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: {"alive": False})
    monkeypatch.setattr(launcher, "owned_roots", lambda *a: pytest.fail("check-only inspected processes"))
    assert launcher.ensure("ui", tmp_path, 1337, {}, check_only=True)["action"] == "not_healthy"


class Process:
    def __init__(self, cwd, cmd, age=500):
        self._cwd, self._cmd, self.age = cwd, cmd, age

    def cwd(self):
        return str(self._cwd)

    def cmdline(self):
        return self._cmd

    def create_time(self):
        return launcher.time.time() - self.age


def test_process_identity_requires_checkout_and_port(tmp_path):
    proc = Process(tmp_path, ["python", "-m", "uvicorn", "argosy.api.main:create_app", "--port", "8000"])
    assert launcher.is_owned_app(proc, "backend", tmp_path, 8000)
    assert not launcher.is_owned_app(proc, "backend", tmp_path / "other", 8000)
    assert not launcher.is_owned_app(proc, "backend", tmp_path, 8001)
    proc = Process(tmp_path / "ui", ["node", "node_modules/next/dist/bin/next", "dev", "-p", "1337"])
    assert launcher.is_owned_app(proc, "ui", tmp_path, 1337)


def setup_unhealthy(monkeypatch, processes):
    monkeypatch.setattr(launcher, "probe", lambda *a, **k: {"alive": False})
    monkeypatch.setattr(launcher, "owned_roots", lambda *a: processes)
    monkeypatch.setattr(launcher.time, "sleep", lambda *a: None)
    monkeypatch.setattr(launcher.psutil, "net_connections", lambda **k: [])
    monkeypatch.setattr(launcher, "start_role", lambda *a: pytest.fail("must not launch"))


def test_startup_grace(monkeypatch, tmp_path):
    setup_unhealthy(monkeypatch, [Process(tmp_path, [], age=20)])
    assert launcher.ensure("ui", tmp_path, 1337, {})["action"] == "warming_up"


def test_active_work_protected(monkeypatch, tmp_path):
    setup_unhealthy(monkeypatch, [Process(tmp_path, [])])
    monkeypatch.setattr(launcher, "active_jobs", lambda *a: [(123, "holdings_review")])
    assert launcher.ensure("backend", tmp_path, 8000, {})["action"] == "deferred_active_jobs"


def test_restart_storm_bounded(monkeypatch, tmp_path):
    setup_unhealthy(monkeypatch, [])
    state = {"ui": [launcher.time.time()] * 3}
    assert launcher.ensure("ui", tmp_path, 1337, state)["action"] == "restart_limit"


def test_lock_excludes_second_invocation(tmp_path):
    with launcher.launcher_lock(tmp_path / "launcher.lock") as first:
        with launcher.launcher_lock(tmp_path / "launcher.lock") as second:
            assert first
            assert not second
