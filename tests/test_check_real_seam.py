from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_real_seam.py"
_SPEC = importlib.util.spec_from_file_location("check_real_seam", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
check_real_seam = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_real_seam)


def test_git_scope_uses_repo_safe_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="argosy/x.py\n")

    monkeypatch.setattr(check_real_seam.subprocess, "run", fake_run)

    assert check_real_seam._git_output(["diff", "--name-only", "HEAD"]) == (
        "argosy/x.py\n"
    )
    command = seen["command"]
    assert command[:3] == [
        "git",
        "-c",
        f"safe.directory={check_real_seam.REPO_ROOT.as_posix()}",
    ]
    assert seen["kwargs"]["check"] is True


def test_git_scope_failure_is_not_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr="unsafe repository")

    monkeypatch.setattr(check_real_seam.subprocess, "run", fail)

    with pytest.raises(RuntimeError, match="unsafe repository"):
        check_real_seam._changed_python_files()


def test_main_returns_two_when_changed_scope_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_real_seam.sys, "argv", [str(_SCRIPT)])
    monkeypatch.setattr(
        check_real_seam,
        "_changed_python_files",
        lambda: (_ for _ in ()).throw(RuntimeError("scope unavailable")),
    )

    assert check_real_seam.main() == 2
