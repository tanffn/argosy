"""Single-attempt subscription transport and provider-specific outage diagnosis."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from argosy.agents.errors import AgentRunError, MissingAPIKeyError

if TYPE_CHECKING:
    from argosy.agents.base import ModelCall


_recent_outages: dict[tuple[str, str, str], tuple[float, str]] = {}
_outage_lock = threading.Lock()


def note_primary_outage(key: tuple[str, str, str], reason: str) -> None:
    with _outage_lock:
        now = time.monotonic()
        for expired in [k for k, (end, _) in _recent_outages.items() if end <= now]:
            del _recent_outages[expired]
        _recent_outages[key] = (now + 45, reason)


def recent_primary_outage(key: tuple[str, str, str]) -> str | None:
    with _outage_lock:
        end, reason = _recent_outages.get(key, (0, ""))
        if end > time.monotonic():
            return reason
        _recent_outages.pop(key, None)
        return None


def clear_primary_outage(key: tuple[str, str, str]) -> None:
    with _outage_lock:
        _recent_outages.pop(key, None)


def provider_fallback_enabled() -> bool:
    return os.getenv("ARGOSY_PROVIDER_FALLBACK_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


class CodexUnavailable(AgentRunError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Codex CLI unavailable ({reason})")


class AlternateDeadlineExpired(TimeoutError):
    """The fleet worker's budget expired before an alternate model call could start."""


def _remaining_call_time(seconds: float) -> float:
    from argosy.services.fleet_reliability import remaining_provider_seconds

    if seconds <= 0:
        raise AlternateDeadlineExpired("Call deadline expired before the alternate model could start")
    try:
        return remaining_provider_seconds(seconds)
    except TimeoutError as exc:
        raise AlternateDeadlineExpired("Fleet attempt expired before the alternate model call") from exc


def _diagnosis(message: str) -> str | None:
    message = message.lower()
    if any(s in message for s in ("permission denied", "access denied", "forbidden",
                                  "policy violation", "policy refusal", "content_policy_violation")):
        return None
    for reason, signals in (
        ("authentication", ("failed to authenticate", "authentication_error", "not logged in",
                            "please run /login", "please log in", "please login", "invalid api key",
                            "invalid x-api-key", "expired oauth", "oauth session expired",
                            "refresh token has already been used", "token_invalidated", "token_expired",
                            "401 unauthorized", "unexpected status 401")),
        ("rate_limit", ("rate_limit_error", "rate limit reached", "usage limit", "hit your limit",
                        "hit your session limit", "too many requests", "insufficient_quota",
                        "out of extra usage", "you've hit your limit", "rate limit exceeded")),
        ("capacity", ("overloaded_error", "server is overloaded", "at capacity", "service unavailable",
                      "temporarily unavailable", "upstream connect error", "internal_server_error")),
        ("connection", ("connection refused", "connection reset", "connection error",
                        "error sending request for url", "stream disconnected before completion")),
        ("timeout", ("request timed out", "connection timed out", "request timeout")),
        ("installation", ("claude-agent-sdk is not installed", "claude code not found",
                          "claude code cli not found", "anthropic sdk is not installed")),
    ):
        if any(s in message for s in signals):
            return reason
    return None


def provider_unavailability(exc: BaseException) -> str | None:
    """Classify provider errors only. Content, policy and validation errors stay failures."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, AlternateDeadlineExpired) or str(exc).startswith("Both model providers failed:"):
            return None
        if isinstance(exc, CodexUnavailable):
            return exc.reason
        if isinstance(exc, MissingAPIKeyError):
            return "authentication"
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, ConnectionError) or type(exc).__name__ == "APIConnectionError":
            return "connection"
        status = getattr(exc, "status_code", None)
        if status == 401:
            return "authentication"
        if status == 429:
            return "rate_limit"
        if status in (408, 504) or type(exc).__name__ == "APITimeoutError":
            return "timeout"
        if status in (500, 502, 503, 529):
            return "capacity"
        if type(exc).__name__ == "CLINotFoundError":
            return "installation"
        # The SDK wraps CLI stderr AND content errors under AgentRunError.
        # Read only the diagnostic line so text in a schema, source, PDF
        # or appended stderr cannot turn a bad request into a provider outage.
        detail = str(exc)
        diagnostic = detail.split("\n[claude.exe stderr", 1)[0]
        if ("claude-agent-sdk error:" in diagnostic and "[claude.exe stderr]" in detail
                and any(marker in detail.lower() for marker in (
                    "your organization requires remote managed settings to load",
                    "remote managed settings could not be loaded"))
                and not any(marker in detail.lower() for marker in (
                    "permission denied", "policy violation", "access denied", "forbidden"))):
            return "managed_settings_unavailable"
        if ("claude-agent-sdk error:" in diagnostic and "[claude.exe stderr was empty]" in detail
                and re.search(r"command failed with exit code 1\b", diagnostic, re.I)
                and getattr(exc.__cause__, "exit_code", None) == 1):
            return "process_exit"
        if any(s in diagnostic.lower() for s in
               ("claude cli result", "claude-agent-sdk error", "claude-agent-sdk is not installed",
                "codex cli", "codex exec", "anthropic sdk is not installed")):
            if reason := _diagnosis(diagnostic):
                return reason
        exc = exc.__cause__
    return None


def codex_executable() -> str | None:
    """Resolve the native executable so timeouts own the model process, not an npm shim."""
    if os.name != "nt":
        return shutil.which("codex")
    if native := shutil.which("codex.exe"):
        return native
    shim = shutil.which("codex.cmd")
    if shim:
        packages = Path(shim).parent / "node_modules/@openai/codex/node_modules/@openai"
        for path in packages.glob("codex-win32-*/vendor/*/bin/codex.exe"):
            return str(path)
    return None


def _cli_environment() -> dict[str, str]:
    # Subprocesses authenticate using the existing ChatGPT login. Do not
    # pass broker keys, API keys, alternate endpoints or a user's proxy.
    names = {"PATH", "HOME", "USERPROFILE", "CODEX_HOME", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
             "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "PATHEXT", "COMSPEC",
             "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "XDG_RUNTIME_DIR",
             "DBUS_SESSION_BUS_ADDRESS", "HOMEDRIVE", "HOMEPATH"}
    return {k: v for k, v in os.environ.items() if k.upper() in names}


def _kill_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # This PID owns the only job handle. Native CLI, MCP helpers and any
        # inherited-pipe grandchildren are terminated together by the kernel.
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def run_subscription_process(args: list[str], *, cwd: Path, stdin: str, timeout: float) -> tuple[int, str, str]:
    # Windows' uvicorn SelectorEventLoop cannot create asyncio subprocesses.
    # Spawn synchronously, move only pipe IO to a thread, and keep the handle
    # until cancellation/timeout has reaped this call's process tree.
    if os.name == "nt":
        # Execute the installed file directly. Importing argosy.agents as a
        # module would initialize the entire fleet and emit logs into stdout.
        args = [sys.executable, "-I", str(Path(__file__).with_name("subscription_worker.py")), *args]
    process = subprocess.Popen(
        args, cwd=cwd, env=_cli_environment(), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        start_new_session=os.name != "nt",
    )
    io = asyncio.create_task(asyncio.to_thread(process.communicate, stdin.encode("utf-8")))
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(io), timeout)
    finally:
        # On POSIX the group can still own pipes after its leader exits.
        _kill_process_tree(process)
        await asyncio.wait_for(asyncio.shield(io), timeout=5)
    return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _parse_completion(status: int, stdout: str, stderr: str, model: str) -> ModelCall:
    from argosy.agents.base import ModelCall

    answer = ""
    usage = None
    error = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            answer = event["item"].get("text", "")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
        elif kind == "turn.failed":
            error = str(event.get("error", ""))
    if status or error is not None or usage is None or not answer.strip():
        # The CLI may echo prompts, documents and file paths in diagnostics.
        # Classify them privately; never forward raw CLI output to a job alert.
        if reason := _diagnosis((error or "") + "\n" + stderr):
            raise CodexUnavailable(reason)
        raise AgentRunError(f"Codex CLI did not return a completed answer (exit {status})")
    cached = int(usage.get("cached_input_tokens") or 0)
    return ModelCall(
        text=answer, model=model,
        tokens_in=max(0, int(usage.get("input_tokens") or 0) - cached),
        cache_input_tokens=cached, tokens_out=int(usage.get("output_tokens") or 0),
        raw={"backend": "codex_cli", "cost_estimate_basis": "configured_role_budget"},
    )


def isolated_codex_flags(*, web_search: bool = False, mcp: bool = False) -> list[str]:
    flags = ["--strict-config", "--ignore-user-config", "--ignore-rules", "--ephemeral",
             "-c", 'approval_policy="never"', "-c", 'forced_login_method="chatgpt"',
             "-c", "project_doc_max_bytes=0", "-c", f'web_search="{"live" if web_search else "disabled"}"']
    for feature in ("shell_tool", "unified_exec", "view_image", "apps", "plugins", "hooks",
                    "multi_agent", "multi_agent_v2", "image_generation", "goals", "skill_search"):
        flags += ["--disable", feature]
    flags += ["--enable", "skip_host_skill_discovery"]
    if not (mcp or web_search):
        flags += ["--disable", "code_mode_host"]
    return flags


async def call_codex(
    *, system: str, user: str, schema: dict[str, Any] | None = None,
    model: str | None = None, timeout_seconds: int = 600,
    images: Sequence[Path] = (), web_search: bool = False, mcp_config: Sequence[str] = (),
) -> ModelCall:
    """Run one subscription model turn in an isolated, read-only work directory."""
    deadline = time.monotonic() + timeout_seconds
    executable = codex_executable()
    if executable is None:
        raise CodexUnavailable("installation")
    model = model or os.getenv("ARGOSY_CODEX_FALLBACK_MODEL") or "gpt-6.2-sol"
    with tempfile.TemporaryDirectory(prefix="argosy-codex-") as temporary:
        cwd = Path(temporary)
        code, out, err = await run_subscription_process(
            [executable, "login", "status"], cwd=cwd, stdin="",
            timeout=_remaining_call_time(min(10, deadline - time.monotonic())),
        )
        if code or not any(line.strip().lower() == "logged in using chatgpt"
                           for line in (out + "\n" + err).splitlines()):
            raise CodexUnavailable("authentication")
        instructions = cwd / "role.txt"
        if schema:
            system += "\nReturn only JSON matching the following schema.\n" + json.dumps(schema, ensure_ascii=False)
        instructions.write_text(system, encoding="utf-8")
        args = [executable, "exec", *isolated_codex_flags(web_search=web_search, mcp=bool(mcp_config)),
                "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--model", model,
                "-c", "model_instructions_file=" + json.dumps(instructions.as_posix())]
        for override in mcp_config:
            args += ["-c", override]
        for path in images:
            args += ["-i", str(path)]
        args.append("-")
        model_timeout = _remaining_call_time(deadline - time.monotonic())
        try:
            status, stdout, stderr = await run_subscription_process(
                args, cwd=cwd, stdin=user, timeout=model_timeout,
            )
        except TimeoutError as exc:
            raise CodexUnavailable("timeout") from exc
        return _parse_completion(status, stdout, stderr, model)
