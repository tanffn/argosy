import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from argosy.agents import provider_fallback as fallback
from argosy.agents.base import BaseAgent, ModelCall, _llm_backend_available
from argosy.agents.errors import AgentRunError


class Output(BaseModel):
    message: str


class Agent(BaseAgent):
    agent_role = "fallback_test"
    output_model = Output
    require_citations = False
    use_structured_output = True


AUTH = "test: Claude CLI result success; turns=1; Failed to authenticate: OAuth session expired"


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(fallback, "_recent_outages", {})
    monkeypatch.setenv("ARGOSY_PROVIDER_FALLBACK_ENABLED", "1")
    instance = Agent(user_id="fallback-test")
    instance._backend_override = "claude_code"
    return instance


def codex_response():
    return ModelCall(text='{"message":"complete"}', model="gpt-test", raw={"backend": "codex_cli"})


async def test_healthy_primary_does_not_contact_codex(agent, monkeypatch):
    original = ModelCall(text='{"message":"original"}', model=agent.model)
    primary = AsyncMock(return_value=original)
    alternate = AsyncMock()
    monkeypatch.setattr(agent, "_call_via_claude_code", primary)
    monkeypatch.setattr(fallback, "call_codex", alternate)
    assert await agent._call_model(system="role", user="question") is original
    alternate.assert_not_awaited()


async def test_expired_session_routes_once_preserves_sources_and_provenance(agent, monkeypatch):
    primary = AsyncMock(side_effect=AgentRunError(AUTH))
    alternate = AsyncMock(side_effect=lambda **_: codex_response())
    monkeypatch.setattr(agent, "_call_via_claude_code", primary)
    monkeypatch.setattr(fallback, "call_codex", alternate)
    for _ in range(2):
        call = await agent._call_model(system="role", user="question", sources=[("s1", "source body")])
        assert agent._parse_output(call.text).message == "complete"
        assert call.model == "gpt-test"
        assert call.raw["fallback_reason"] == "authentication"
        assert call.raw["fallback_from"] == "claude_code"
    assert primary.await_count == 1
    assert alternate.await_count == 2
    args = alternate.await_args.kwargs
    assert "source body" in args["user"] and "s1" in args["user"]
    assert args["schema"] == Output.model_json_schema()
    # No sliding cooldown: healthy primary is tested again at its deadline.
    key = (agent.user_id, "claude_code", agent.model)
    end, _ = fallback._recent_outages[key]
    monkeypatch.setattr(fallback.time, "monotonic", lambda: end + 1)
    primary.side_effect = None
    primary.return_value = ModelCall(text="healthy", model=agent.model)
    assert (await agent._call_model(system="s", user="u")).text == "healthy"
    assert alternate.await_count == 2


async def test_incompatible_request_can_still_use_healthy_primary_during_cooldown(agent, monkeypatch):
    agent._backend_override = "api_key"
    agent.citations_enabled = True
    fallback.note_primary_outage((agent.user_id, "api_key", agent.model), "authentication")
    primary = AsyncMock(return_value=ModelCall(text="with spans"))
    monkeypatch.setattr(agent, "_call_via_api_key", primary)
    alternate = AsyncMock()
    monkeypatch.setattr(fallback, "call_codex", alternate)
    call = await agent._call_model(system="s", user="u", sources=[("doc", "source")])
    assert call.text == "with spans"
    alternate.assert_not_awaited()


@pytest.mark.parametrize("error", [
    "policy refused this request", "invalid JSON: rate limit exceeded", "wrong attachment format",
    "test: claude-agent-sdk error: model does not support structured output",
    "test: claude-agent-sdk error: command failed with exit code 1\n[claude.exe stderr]\npermission denied",
    "test: Anthropic API error: invalid tool schema: 'rate limit exceeded'",
])
async def test_permanent_failure_is_never_rerouted(agent, monkeypatch, error):
    exc = AgentRunError(error)
    primary = AsyncMock(side_effect=exc)
    alternate = AsyncMock()
    monkeypatch.setattr(agent, "_call_via_claude_code", primary)
    monkeypatch.setattr(fallback, "call_codex", alternate)
    with pytest.raises(AgentRunError) as caught:
        await agent._call_model(system="s", user="u")
    assert caught.value is exc
    alternate.assert_not_awaited()


async def test_dual_outage_reports_both_without_recursing_or_echoing_content(agent, monkeypatch):
    primary = AsyncMock(side_effect=AgentRunError(AUTH))
    alternate = AsyncMock(side_effect=fallback.CodexUnavailable("rate_limit"))
    monkeypatch.setattr(agent, "_call_via_claude_code", primary)
    monkeypatch.setattr(fallback, "call_codex", alternate)
    with pytest.raises(AgentRunError, match="Both model providers failed: claude_code=authentication; codex_cli=rate_limit") as caught:
        await agent._call_model(system="s", user="private")
    assert fallback.provider_unavailability(caught.value) is None
    primary.assert_awaited_once()
    alternate.assert_awaited_once()


async def test_recovered_primary_can_serve_when_cached_alternate_goes_down(agent, monkeypatch):
    key = (agent.user_id, "claude_code", agent.model)
    fallback.note_primary_outage(key, "timeout")
    alternate = AsyncMock(side_effect=fallback.CodexUnavailable("connection"))
    primary = AsyncMock(return_value=ModelCall(text="recovered"))
    monkeypatch.setattr(agent, "_call_via_claude_code", primary)
    monkeypatch.setattr(fallback, "call_codex", alternate)
    assert (await agent._call_model(system="s", user="u")).text == "recovered"
    assert fallback.recent_primary_outage(key) is None
    alternate.assert_awaited_once()
    primary.assert_awaited_once()


def test_managed_settings_outage_scoped_to_provider_and_does_not_override_denial():
    detail = "Your organization requires remote managed settings to load"
    assert fallback.provider_unavailability(RuntimeError(detail)) is None
    diagnostic = AgentRunError("role: claude-agent-sdk error: Command failed with exit code 1\n"
                               "[claude.exe stderr]\n" + detail)
    assert fallback.provider_unavailability(diagnostic) == "managed_settings_unavailable"
    assert fallback.provider_unavailability(AgentRunError(str(diagnostic) + ": permission denied")) is None


async def test_switch_off_preserves_original_error(agent, monkeypatch):
    monkeypatch.setenv("ARGOSY_PROVIDER_FALLBACK_ENABLED", "0")
    primary = AsyncMock(side_effect=AgentRunError(AUTH))
    alternate = AsyncMock()
    monkeypatch.setattr(agent, "_call_via_claude_code", primary)
    monkeypatch.setattr(fallback, "call_codex", alternate)
    with pytest.raises(AgentRunError, match="OAuth session expired"):
        await agent._call_model(system="s", user="u")
    alternate.assert_not_awaited()


async def test_codex_invocation_isolated_subscription_and_usage(monkeypatch):
    invocations = []
    async def fake_cli(args, *, cwd, stdin, timeout):
        invocations.append(args)
        if args[-2:] == ["login", "status"]:
            return 0, "", "Logged in using ChatGPT"
        assert args[-1] == "-" and stdin == "sensitive content"
        assert "sensitive" not in " ".join(args)
        assert json.loads(next(x for x in args if x.startswith("model_instructions_file=")).split("=", 1)[1])
        assert json.dumps({"type": "object"}) in (cwd / "role.txt").read_text(encoding="utf8")
        assert "--ignore-user-config" in args and "read-only" in args and "--strict-config" in args
        assert 'forced_login_method="chatgpt"' in args and 'web_search="disabled"' in args
        assert "shell_tool" in args and "code_mode_host" in args
        usage = {"input_tokens": 13, "cached_input_tokens": 3, "output_tokens": 7}
        return 0, "\n".join(map(json.dumps, [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
            {"type": "turn.completed", "usage": usage},
        ])), ""

    monkeypatch.setattr(fallback, "codex_executable", lambda: "codex")
    monkeypatch.setattr(fallback, "run_subscription_process", fake_cli)
    result = await fallback.call_codex(system="secret role", user="sensitive content", schema={"type": "object"}, model="gpt-test")
    assert result.model == "gpt-test" and (result.tokens_in, result.cache_input_tokens, result.tokens_out) == (10, 3, 7)
    assert len(invocations) == 2


async def test_api_key_only_login_does_not_execute(monkeypatch):
    runner = AsyncMock(return_value=(0, "Logged in using an API key", ""))
    monkeypatch.setattr(fallback, "codex_executable", lambda: "codex")
    monkeypatch.setattr(fallback, "run_subscription_process", runner)
    with pytest.raises(fallback.CodexUnavailable, match="authentication"):
        await fallback.call_codex(system="s", user="u")
    runner.assert_awaited_once()


async def test_preexisting_worker_deadline_does_not_report_an_alternate_outage(agent, monkeypatch):
    from argosy.services import fleet_reliability

    def expired(_):
        raise TimeoutError("deadline")
    runner = AsyncMock()
    monkeypatch.setattr(fleet_reliability, "remaining_provider_seconds", expired)
    monkeypatch.setattr(agent, "_call_via_claude_code", AsyncMock(side_effect=AgentRunError(AUTH)))
    monkeypatch.setattr(fallback, "codex_executable", lambda: "codex")
    monkeypatch.setattr(fallback, "run_subscription_process", runner)
    with pytest.raises(fallback.AlternateDeadlineExpired, match="before starting the Codex model") as caught:
        await agent._call_model(system="s", user="u")
    assert fallback.provider_unavailability(caught.value) is None
    runner.assert_not_awaited()


def test_failed_or_incomplete_turn_cannot_return_partial_output():
    partial = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}})
    for status, events in ((0, partial), (1, partial), (0, partial + '\n{"type":"turn.failed"}')):
        with pytest.raises(AgentRunError):
            fallback._parse_completion(status, events, "", "gpt-test")


def test_preserve_configured_budget_estimate_and_real_codex_token_buckets(agent):
    call = ModelCall(text="done", tokens_in=90, cache_input_tokens=10, tokens_out=5,
                     model="gpt-test", raw={"backend": "codex_cli"})
    assert agent._estimate_call_usd(call) == agent._estimate_usd(tokens_in=100, cache_input_tokens=10, tokens_out=5)
    assert call.tokens_in == 90 and call.cache_input_tokens == 10


async def test_real_child_excludes_api_credentials_and_stops_on_cancellation(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-sentinel")
    cmd = [sys.executable, "-I", "-c", "import os; print(os.getenv('OPENAI_API_KEY', 'unset'))"]
    assert (await fallback.run_subscription_process(cmd, cwd=tmp_path, stdin="", timeout=5))[1].strip() == "unset"
    pidfile = tmp_path / "pid"
    script = "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(fallback.run_subscription_process(
        [sys.executable, "-I", "-c", script], cwd=tmp_path, stdin="", timeout=30))
    try:
        async with asyncio.timeout(10):
            while not pidfile.exists():
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    import psutil

    assert not psutil.pid_exists(int(pidfile.read_text()))


async def test_native_root_exits_while_child_holds_stdout(tmp_path):
    import psutil

    # The child inherits both captured pipes, then the native root exits.
    # Entering read-until-EOF before killing its subtree used to hang forever.
    program = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']); "
        "pathlib.Path('child.pid').write_text(str(p.pid))"
    )
    try:
        await fallback.run_subscription_process([sys.executable, "-c", program], cwd=tmp_path, stdin="", timeout=8)
    except TimeoutError:
        pass  # POSIX process group is killed by the caller's deadline.
    pid = int((tmp_path / "child.pid").read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.llm_eval
async def test_live_subscription_route_on_synthetic_packet(agent, request):
    """No portfolio, DB writes or synthetic injection of a primary failure."""
    if request.config.getoption("-m") != "llm_eval" or not _llm_backend_available():
        pytest.skip("Run explicitly with -m llm_eval and an installed subscription CLI")
    agent.claude_code_max_retries = 0
    agent.sdk_timeout_seconds = 60
    agent.claude_code_force_toolless = True
    call = await agent._call_model(system="Return a JSON object with a message field.",
                                   user="Set message to ARGOSY_OK.")
    assert agent._parse_output(call.text).message == "ARGOSY_OK"
    assert call.raw["backend"] in {"claude_code", "codex_cli"}
    print(f"backend={call.raw['backend']} model={call.model} "
          f"reason={call.raw.get('fallback_reason', 'primary')} schema=ARGOSY_OK")
