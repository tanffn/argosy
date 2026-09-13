"""Activate research intake after this checkout's running jobs finish.

Designed for a hidden background process. Status: tmp/research-activation.json.
Never terminates an active job or a backend belonging to another checkout.
"""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

import psutil

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "tmp/research-activation.json"
PYTHON = ROOT / ".venv/Scripts/python.exe"


def status(state, **details):
    STATUS.parent.mkdir(exist_ok=True)
    STATUS.write_text(json.dumps({"state": state, "at": time.time(), **details}, indent=2), encoding="utf-8")


def active_jobs():
    with sqlite3.connect(f"file:{ROOT.as_posix()}/db/argosy.db?mode=ro", uri=True) as db:
        return [r[0] for r in db.execute("select job_name from job_runs where status='running'")]


def backend():
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status == psutil.CONN_LISTEN and connection.laddr.port == 8000 and connection.pid:
            process = psutil.Process(connection.pid)
            if not any("argosy.api.main:create_app" in arg for arg in process.cmdline()):
                raise RuntimeError("Port 8000 belongs to another application")
            expected = str(ROOT / "scripts/run_backend_service.py").lower()
            if not any(expected in " ".join(p.cmdline()).lower() for p in process.parents()[:4]):
                raise RuntimeError("Backend is not supervised by this checkout")
            return process
    raise RuntimeError("No supervised backend listening on port 8000")


def main():
    os.chdir(ROOT)
    deadline = time.monotonic() + 4 * 3600
    while jobs := active_jobs():
        status("waiting_for_idle", active_jobs=jobs)
        if time.monotonic() >= deadline:
            raise RuntimeError("Jobs remained active for four hours; backend was left running")
        time.sleep(15)
    process = backend()
    if active_jobs():
        return main()
    status("restarting", previous_pid=process.pid)
    process.terminate()
    for _ in range(80):
        time.sleep(0.5)
        try:
            with urlopen("http://127.0.0.1:8000/api/input-sources/research/sources", timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            pass
    else:
        raise RuntimeError("Supervisor did not restore the research API")
    results = {}
    for command in ["backfill", "seed", "poll"]:
        result = subprocess.run([str(PYTHON), "scripts/research_sources.py", command],
                                capture_output=True, text=True, timeout=300,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        results[command] = {"exit_code": result.returncode, "output": result.stdout[-10000:], "error": result.stderr[-2000:]}
    status("complete", source_setup=results)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        status("failed", error=str(exc))
        raise
