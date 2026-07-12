"""Backend service supervisor — simulated crash → restart."""
from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.run_backend_service import (
    BackendServiceSupervisor,
    SupervisorConfig,
)


class _FakeProc:
    def __init__(self, codes: list[int]):
        self._codes = list(codes)
        self._idx = 0
        self._terminated = False

    def poll(self):
        return None if self._idx < len(self._codes) else self._codes[-1]

    def wait(self):
        if self._idx >= len(self._codes):
            return self._codes[-1]
        code = self._codes[self._idx]
        self._idx += 1
        return code

    def terminate(self):
        self._terminated = True


def test_supervisor_restarts_after_crash(tmp_path: Path):
    codes = [1, 1, 0]  # crash, crash, clean
    spawned: list[list[str]] = []

    def factory(cmd, **kwargs):
        spawned.append(list(cmd))
        # Each spawn consumes the next exit code via a fresh FakeProc slice.
        # Simpler: one shared counter.
        return _seq_proc(codes)

    # Shared sequential fake: each Popen returns next exit on wait().
    state = {"i": 0}

    class SeqProc:
        def poll(self):
            return None

        def wait(self):
            i = state["i"]
            state["i"] = i + 1
            return codes[i]

        def terminate(self):
            pass

    def factory2(cmd, **kwargs):
        spawned.append(list(cmd))
        return SeqProc()

    cfg = SupervisorConfig(
        cmd=["fake-uvicorn"],
        cwd=tmp_path,
        log_dir=tmp_path / "tmp",
        log_stem="test_svc",
        restart_delay_s=0.01,
        popen_factory=factory2,
    )
    result = BackendServiceSupervisor(cfg).run(max_iterations=10)
    assert result.restarts == 2
    assert result.exits == [1, 1, 0]
    assert result.reason == "clean_exit"
    assert len(spawned) == 3
    log = (tmp_path / "tmp" / "test_svc.log").read_text(encoding="utf-8")
    assert "supervisor restart #1" in log
    assert "supervisor restart #2" in log


def test_supervisor_stops_on_rapid_crash_limit(tmp_path: Path):
    state = {"i": 0}

    class AlwaysCrash:
        def poll(self):
            return None

        def wait(self):
            state["i"] += 1
            return 2

        def terminate(self):
            pass

    cfg = SupervisorConfig(
        cmd=["boom"],
        cwd=tmp_path,
        log_dir=tmp_path / "tmp",
        log_stem="rapid",
        restart_delay_s=0.0,
        max_rapid_crashes=3,
        rapid_window_s=60.0,
        popen_factory=lambda *a, **k: AlwaysCrash(),
    )
    result = BackendServiceSupervisor(cfg).run(max_iterations=20)
    assert result.reason == "rapid_crash_limit"
    assert result.restarts >= 2
    assert len(result.exits) == 3


def _seq_proc(codes: list[int]):
    return subprocess.CompletedProcess(args=[], returncode=codes[0])
