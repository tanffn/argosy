"""Windowless, idempotent local server launcher. No analysis/trade APIs called.

Run with pythonw.exe from the desktop/logon task, or python.exe for JSON output.
Application-level 'degraded' status never triggers a process restart.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import webbrowser

import psutil

ROOT = Path(__file__).resolve().parents[1]


def probe(url: str, role: str, timeout: float = 8) -> dict:
    try:
        try:
            response = urlopen(url, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            body = response.read(2_000_000).decode("utf-8", errors="replace")
            status = response.code
        if role == "backend":
            data = json.loads(body)
            identified = isinstance(data, dict) and all(k in data for k in ("db", "git_sha", "started_at"))
            # A responding DB-error is a logical failure, not a hung server.
            alive = identified and status == 200
            return {"alive": alive, "identified": identified, "http": status,
                    "application_status": data.get("status") if isinstance(data, dict) else None}
        identified = "/_next/" in body and "argosy" in body.lower()
        return {"alive": identified and status == 200, "identified": identified, "http": status}
    except (OSError, URLError, ValueError) as exc:
        return {"alive": False, "identified": False, "error": str(exc)[:240]}


def is_owned_app(process: psutil.Process, role: str, root: Path, port: int) -> bool:
    try:
        cmd = process.cmdline()
        expected = root if role == "backend" else root / "ui"
        if Path(process.cwd()).resolve() != expected.resolve():
            return False
        text = " ".join(cmd).replace("\\", "/").lower()
        if role == "backend":
            identity = "argosy.api.main:create_app" in text
        else:
            identity = "node_modules/next/dist/bin/next" in text and "dev" in cmd
        return identity and any(
            cmd[i] in ("--port", "-p") and cmd[i + 1] == str(port)
            for i in range(len(cmd) - 1)
        )
    except (psutil.Error, OSError):
        return False


def owned_roots(role: str, root: Path, port: int) -> list[psutil.Process]:
    matches = [p for p in psutil.process_iter() if is_owned_app(p, role, root, port)]
    ids = {p.pid for p in matches}
    return [p for p in matches if not any(a.pid in ids for a in p.parents())]


def listener_is_owned(pid: int, role: str, root: Path, port: int) -> bool:
    try:
        process = psutil.Process(pid)
        return any(is_owned_app(p, role, root, port) for p in [process, *process.parents()])
    except psutil.Error:
        return False


def active_jobs(root: Path) -> list:
    with sqlite3.connect((root / "db/argosy.db").as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        return conn.execute("SELECT id,job_name FROM job_runs WHERE status='running'").fetchall()


def stop_owned(process: psutil.Process, role: str, root: Path, port: int) -> None:
    # Revalidate immediately before mutation; never terminate by port alone.
    if not is_owned_app(process, role, root, port):
        raise RuntimeError("Process identity changed; refusing to stop it")
    descendants = process.children(recursive=True)
    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    process.terminate()
    _, remaining = psutil.wait_procs([process, *descendants], timeout=5)
    if remaining:
        raise RuntimeError("Processes did not stop; refusing to stack replacements")


def start_role(role: str, root: Path, port: int) -> None:
    logs = root / "tmp"
    logs.mkdir(exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = {**os.environ, "ARGOSY_HOME": str(root), "PYTHONIOENCODING": "utf-8"}
    with (logs / "server_launcher.log").open("a", encoding="utf-8") as out:
        if role == "backend":
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(root / "scripts/start_backend_detached.ps1"), "-Port", str(port)],
                cwd=root, env=env, stdout=out, stderr=out, creationflags=flags, check=True, timeout=30,
            )
        else:
            import shutil
            node = shutil.which("node.exe") or shutil.which("node")
            if not node:
                raise RuntimeError("Node.js not found in PATH")
            subprocess.Popen(
                [node, str(root / "ui/node_modules/next/dist/bin/next"), "dev", "--hostname", "127.0.0.1", "-p", str(port)],
                cwd=root / "ui", env=env, stdout=out, stderr=out,
                creationflags=flags, close_fds=True,
            )


def ensure(role: str, root: Path, port: int, history: dict, *, check_only: bool = False) -> dict:
    url = f"http://127.0.0.1:{port}" + ("/api/health" if role == "backend" else "/")
    check = probe(url, role)
    if check["alive"]:
        return {"action": "skipped_healthy", "health": check}
    if check_only:
        return {"action": "not_healthy", "health": check}
    processes = owned_roots(role, root, port)
    # Development compilation/startup can be slow; don't kill a fresh launch.
    if any(time.time() - p.create_time() < 180 for p in processes):
        return {"action": "warming_up", "health": check}
    for _ in range(2):
        time.sleep(2)
        check = probe(url, role)
        if check["alive"]:
            return {"action": "skipped_healthy", "health": check}
    listeners = [c for c in psutil.net_connections(kind="tcp")
                 if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
    if any(not c.pid or not listener_is_owned(c.pid, role, root, port) for c in listeners):
        return {"action": "blocked_foreign_listener", "health": check}
    if role == "backend" and processes:
        jobs = active_jobs(root)
        if jobs:
            return {"action": "deferred_active_jobs", "jobs": jobs, "health": check}
    recent = [t for t in history.get(role, []) if time.time() - t < 1800]
    if len(recent) >= 3:
        return {"action": "restart_limit", "health": check}
    # Attempt history persists even if start fails, preventing crash/retry storms.
    history[role] = [*recent, time.time()]
    (root / "tmp/server_launcher_attempts.json").write_text(json.dumps(history), encoding="utf-8")
    for process in processes:
        if process.is_running():
            stop_owned(process, role, root, port)
    start_role(role, root, port)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        check = probe(url, role, timeout=3)
        if check["alive"]:
            return {"action": "restarted" if processes else "started", "health": check}
        time.sleep(1)
    return {"action": "starting_unverified", "health": check}


@contextmanager
def launcher_lock(path: Path):
    import msvcrt
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    (ROOT / "tmp").mkdir(exist_ok=True)
    with launcher_lock(ROOT / "tmp/server_launcher.lock") as acquired:
        if not acquired:
            if args.open_browser:
                webbrowser.open("http://localhost:1337")
            return 0
        state = ROOT / "tmp/server_launcher_attempts.json"
        history = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        result = {"checked_at": datetime.now(UTC).isoformat(), "services": {}}
        for role, port in (("backend", 8000), ("ui", 1337)):
            try:
                result["services"][role] = ensure(role, ROOT, port, history, check_only=args.check_only)
            except Exception as exc:
                result["services"][role] = {"action": "error", "error": str(exc)}
        text = json.dumps(result, indent=2)
        (ROOT / "tmp/server_launcher_status.json").write_text(text, encoding="utf-8")
        with (ROOT / "tmp/server_launcher_history.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(result) + "\n")
        if sys.stdout:
            print(text, flush=True)
        if args.open_browser and result["services"]["ui"].get("health", {}).get("alive"):
            webbrowser.open("http://localhost:1337")
        return 0 if all(s.get("health", {}).get("alive") for s in result["services"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
