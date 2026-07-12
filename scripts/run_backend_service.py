"""Backend service wrapper with auto-restart (Item I reliability).

Wraps uvicorn so a crash (sqlite locked, unexpected exit) becomes a short
blip instead of a multi-hour silent outage. Designed to be launched via
``Start-Process`` (detached) — the wrapper itself is the long-lived process;
child uvicorn is supervised and restarted.

Logs under ``tmp/uvicorn_service.*.log`` (and ``*.err.log``). Scratch-DB
testable: set ``ARGOSY_BACKEND_CMD`` to a fake command.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RESTART_DELAY_S = 5.0
DEFAULT_MAX_RAPID_CRASHES = 5
DEFAULT_RAPID_WINDOW_S = 120.0


@dataclass
class SupervisorConfig:
    cmd: list[str]
    cwd: Path
    log_dir: Path
    log_stem: str = "uvicorn_service"
    restart_delay_s: float = DEFAULT_RESTART_DELAY_S
    max_rapid_crashes: int = DEFAULT_MAX_RAPID_CRASHES
    rapid_window_s: float = DEFAULT_RAPID_WINDOW_S
    env: dict[str, str] = field(default_factory=dict)
    popen_factory: Callable[..., Any] | None = None


@dataclass
class SupervisorResult:
    exits: list[int]
    restarts: int
    stopped: bool
    reason: str


class BackendServiceSupervisor:
    """Supervise a backend child; restart on non-clean exit until stop."""

    def __init__(self, cfg: SupervisorConfig) -> None:
        self.cfg = cfg
        self._stop = False
        self._child: Any | None = None
        self._crash_times: list[float] = []

    def request_stop(self, *_args: object) -> None:
        self._stop = True
        if self._child is not None and getattr(self._child, "poll", lambda: 0)() is None:
            try:
                self._child.terminate()
            except OSError:
                pass

    def _log_paths(self) -> tuple[Path, Path]:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        out = self.cfg.log_dir / f"{self.cfg.log_stem}.log"
        err = self.cfg.log_dir / f"{self.cfg.log_stem}.err.log"
        return out, err

    def _append_banner(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(text)

    def _spawn(self) -> Any:
        out_path, err_path = self._log_paths()
        stamp = datetime.now(timezone.utc).isoformat()
        banner = f"\n===== supervisor spawn {stamp} cmd={self.cfg.cmd!r} =====\n"
        self._append_banner(out_path, banner)
        self._append_banner(err_path, banner)
        env = {**os.environ, **self.cfg.env}
        factory = self.cfg.popen_factory or subprocess.Popen
        if self.cfg.popen_factory is not None:
            return factory(self.cfg.cmd, cwd=str(self.cfg.cwd), env=env)
        out_f = out_path.open("a", encoding="utf-8")
        err_f = err_path.open("a", encoding="utf-8")
        try:
            return factory(
                self.cfg.cmd,
                cwd=str(self.cfg.cwd),
                env=env,
                stdout=out_f,
                stderr=err_f,
            )
        except Exception:
            out_f.close()
            err_f.close()
            raise

    def _too_many_rapid_crashes(self) -> bool:
        now = time.monotonic()
        window = self.cfg.rapid_window_s
        self._crash_times = [t for t in self._crash_times if now - t <= window]
        return len(self._crash_times) >= self.cfg.max_rapid_crashes

    def run(self, *, max_iterations: int | None = None) -> SupervisorResult:
        exits: list[int] = []
        restarts = 0
        iterations = 0
        reason = "running"

        while not self._stop:
            if max_iterations is not None and iterations >= max_iterations:
                reason = "max_iterations"
                break
            iterations += 1
            child = self._spawn()
            self._child = child
            code = child.wait()
            exits.append(int(code if code is not None else -1))
            self._child = None

            if self._stop:
                reason = "stop_requested"
                break
            if code == 0:
                reason = "clean_exit"
                break

            self._crash_times.append(time.monotonic())
            if self._too_many_rapid_crashes():
                reason = "rapid_crash_limit"
                out_path, err_path = self._log_paths()
                msg = (
                    f"\n===== supervisor giving up after "
                    f"{self.cfg.max_rapid_crashes} rapid crashes =====\n"
                )
                self._append_banner(out_path, msg)
                self._append_banner(err_path, msg)
                break

            restarts += 1
            out_path, _ = self._log_paths()
            self._append_banner(
                out_path,
                f"\n===== supervisor restart #{restarts} "
                f"after exit={code} sleep={self.cfg.restart_delay_s}s =====\n",
            )
            deadline = time.monotonic() + self.cfg.restart_delay_s
            while time.monotonic() < deadline and not self._stop:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

        return SupervisorResult(
            exits=exits, restarts=restarts, stopped=self._stop, reason=reason,
        )


def default_uvicorn_cmd(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    python: str | None = None,
) -> list[str]:
    py = python or sys.executable
    return [
        py, "-m", "uvicorn", "argosy.api.main:create_app",
        "--factory", "--host", host, "--port", str(port),
    ]


def build_config_from_env(argv: Sequence[str] | None = None) -> SupervisorConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ARGOSY_BACKEND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ARGOSY_BACKEND_PORT", "8000")))
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=float(os.environ.get("ARGOSY_BACKEND_RESTART_DELAY_S", str(DEFAULT_RESTART_DELAY_S))),
    )
    parser.add_argument(
        "--log-stem",
        default=os.environ.get("ARGOSY_BACKEND_LOG_STEM", "uvicorn_service"),
    )
    parser.add_argument("--max-iterations", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(os.environ.get("ARGOSY_HOME", Path(__file__).resolve().parents[1]))
    override = os.environ.get("ARGOSY_BACKEND_CMD", "").strip()
    cmd = [p for p in override.split("||") if p] if override else default_uvicorn_cmd(
        host=args.host, port=args.port,
    )
    cfg = SupervisorConfig(
        cmd=cmd, cwd=root, log_dir=root / "tmp",
        log_stem=args.log_stem, restart_delay_s=args.restart_delay,
    )
    os.environ["_ARGOSY_BACKEND_MAX_ITER"] = (
        str(args.max_iterations) if args.max_iterations is not None else ""
    )
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    cfg = build_config_from_env(argv)
    supervisor = BackendServiceSupervisor(cfg)

    def _handle(signum: int, _frame: object) -> None:
        supervisor.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass

    max_iter_raw = os.environ.get("_ARGOSY_BACKEND_MAX_ITER", "")
    max_iterations = int(max_iter_raw) if max_iter_raw.strip() else None
    result = supervisor.run(max_iterations=max_iterations)
    print(
        f"backend_service_supervisor done reason={result.reason} "
        f"restarts={result.restarts} exits={result.exits}",
        flush=True,
    )
    return 0 if result.reason in ("clean_exit", "stop_requested", "max_iterations") else 1


if __name__ == "__main__":
    raise SystemExit(main())
