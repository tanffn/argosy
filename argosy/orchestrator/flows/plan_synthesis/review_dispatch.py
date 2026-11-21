"""Bounded dispatch and provenance for the two independent plan reviews."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
import tomllib
from types import SimpleNamespace
import uuid

from pydantic import BaseModel

from argosy.agents.base import AgentReport, BaseAgent
from argosy.logging import get_logger
from argosy.quality.review_provenance import codex_contributed


log = get_logger(__name__)


class ReviewUnavailable(RuntimeError):
    def failed_report(self, *, role: str, user_id: str, decision_run_id: int) -> AgentReport | None:
        if not str(self).startswith(("Both model providers failed:", "Provider fallback not attempted:")):
            return None
        failure = AdvisoryPlanReview(
            reviewer="unavailable", model="", advisory_reason=str(self), advisory_review={},
        )
        return AgentReport(
            agent_role=role, user_id=user_id, model="unavailable", tokens_in=0, tokens_out=0,
            cost_usd=0, confidence=None, prompt_hash="", response_text=failure.model_dump_json(),
            output=failure, decision_id=f"plan-synth-{decision_run_id}",
            run_correlation_id=str(uuid.uuid4()),
        )


class AdvisoryPlanReview(BaseModel):
    reviewer: str
    model: str
    advisory_reason: str
    # Promotion readers require a top-level overall_assessment. Keeping the
    # actual findings nested also prevents advisory rows from being resumed
    # or interpreted by older code as independent approvals.
    advisory_review: dict


@dataclass
class ReviewResponse:
    provider: str
    model: str
    text: str
    tokens_in: int
    tokens_out: int
    cost: float
    advisory_reason: str = ""
    cache_input_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_tokens: int = 0

    def report(self, *, parsed: BaseModel, role: str, user_id: str,
               decision_run_id: int, prompt: str) -> AgentReport:
        output = parsed
        if self.advisory_reason:
            output = AdvisoryPlanReview(
                reviewer=self.provider, model=self.model,
                advisory_reason=self.advisory_reason, advisory_review=parsed.model_dump(),
            )
        return AgentReport(
            agent_role=role, user_id=user_id, model=self.model,
            response_text=output.model_dump_json(indent=2), tokens_in=self.tokens_in,
            tokens_out=self.tokens_out, cost_usd=self.cost, prompt_hash="",
            confidence=None, output=output, decision_id=f"plan-synth-{decision_run_id}",
            run_correlation_id=str(uuid.uuid4()), system_prompt="", user_prompt=prompt,
            cache_input_tokens=self.cache_input_tokens, cache_creation_tokens=self.cache_creation_tokens,
            thinking_tokens=self.thinking_tokens,
        )


class _ClaudePlanReviewer(BaseAgent):
    agent_role = "plan_review_fallback"
    claude_code_force_toolless = True
    claude_code_max_retries = 0


async def _review_with_claude(*, prompt: str, user_id: str, timeout_s: float):
    agent = _ClaudePlanReviewer(user_id=user_id)
    agent.sdk_timeout_seconds = timeout_s
    # Use the Claude-only transport. Calling the provider router again here
    # could send the fallback straight back to the failed Codex engine.
    call = await asyncio.wait_for(agent._call_via_claude_code(
        system="Review the supplied plan using only the evidence provided. Return the requested JSON.",
        user=prompt,
    ), timeout=timeout_s)
    if not (call.raw or {}).get("cost_usd_from_sdk"):
        call.raw = dict(call.raw or {}, cost_usd_from_sdk=agent._estimate_usd(
            tokens_in=call.tokens_in, tokens_out=call.tokens_out,
            cache_input_tokens=call.cache_input_tokens, cache_creation_tokens=call.cache_creation_tokens,
            thinking_tokens=call.thinking_tokens,
        ))
    return call


async def _run_tandem(*, scripts_dir, node_dir, role, prompt, decision_run_id, timeout_s, ceiling_s):
    from argosy.agents.provider_fallback import CodexUnavailable, isolated_codex_flags, run_subscription_process

    worker = Path(__file__).with_name("tandem_review_worker.py")
    # The parent owns the whole process tree. Expire its deadline before
    # the inner kit timeout can kill only its direct subprocess on Windows.
    deadline = min(ceiling_s, timeout_s)
    # Isolate the review from ambient tools/MCP while retaining the operator's
    # chosen model and reasoning effort. The tandem CLI reports its actual
    # model in the startup banner.
    flags = isolated_codex_flags()
    config_file = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    if config_file.is_file():
        with config_file.open("rb") as config_stream:
            config = tomllib.load(config_stream)
        profile = config.get("profiles", {}).get(config.get("profile"), {})
        model = profile.get("model") or config.get("model")
        effort = profile.get("model_reasoning_effort") or config.get("model_reasoning_effort")
        if model:
            flags += ["--model", model]
        if effort:
            flags += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    request = dict(scripts_dir=str(scripts_dir), node_dir=str(node_dir), role=role,
                   prompt=prompt, decision_run_id=decision_run_id, timeout_s=deadline + 60,
                   extra_args=flags)
    status, stdout, _ = await run_subscription_process(
        [sys.executable, str(worker)], cwd=worker.parent, stdin=json.dumps(request), timeout=deadline,
    )
    if status:
        raise ReviewUnavailable(f"Codex worker exited with status {status}")
    envelope = json.loads(stdout)
    if envelope.get("error") == "installation":
        raise CodexUnavailable("installation")
    if "result" not in envelope:
        raise ReviewUnavailable("Codex worker did not return a review")
    return SimpleNamespace(**envelope["result"])


def _exit_reason(code: int, stderr: str) -> str | None:
    from argosy.agents.provider_fallback import provider_unavailability

    if code == 124:
        return "timeout"
    for line in stderr.splitlines():
        # Non-JSON tandem stderr can also echo the financial prompt. Only
        # classify actual CLI diagnostic lines and never include them in alerts.
        if not re.search(r"(?:^|\s)error:?\s", line, re.I):
            continue
        status = re.search(r"(?:status(?: code)?\s*[:=]?\s*|HTTP\/\S+\s+)(401|408|429|500|502|503|504|529)\b", line, re.I)
        error = RuntimeError("Codex CLI error: " + line)
        if status:
            error.status_code = int(status[1])
        if reason := provider_unavailability(error):
            return reason
    return None


def _codex_response(result) -> ReviewResponse:
    total = int(getattr(result, "tokens", 0) or 0)
    cost = getattr(result, "cost", None)
    if cost is None:
        try:
            from engine_stats import estimate_cost_usd
            cost = estimate_cost_usd(model="codex-gpt-5-5", tokens=total)
        except Exception as exc:
            log.warning("plan_review.cost_estimate_failed", error_type=type(exc).__name__)
            cost = 0
    try:
        cost = min(10.0, max(0.0, float(cost)))
    except (TypeError, ValueError):
        cost = 0.0
    # The tandem kit invokes the CLI's configured model. Only trust the
    # runtime banner header, since stderr also contains model-generated text.
    banner = (getattr(result, "stderr", "") or "").split("--------", 2)
    match = re.search(r"(?m)^model:\s*([\w.-]+)\s*$", banner[1]) if len(banner) >= 3 else None
    model = getattr(result, "model", None) or (match[1] if match else "codex-cli-default")
    explicit_out = getattr(result, "tokens_out", None)
    return ReviewResponse(
        "codex", model, getattr(result, "verdict_text", "") or "",
        int(getattr(result, "tokens_in", 0) or 0),
        total if explicit_out is None else int(explicit_out or 0), cost,
    )


async def dispatch_plan_review(*, scripts_dir: Path, node_dir: Path, role: str,
                               prompt: str, decision_run_id: int, user_id: str,
                               timeout_s: float, ceiling_s: float) -> ReviewResponse:
    from argosy.agents.provider_fallback import provider_fallback_enabled, provider_unavailability
    from argosy.services.fleet_reliability import remaining_provider_seconds

    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    fallback_enabled = provider_fallback_enabled()
    try:
        budget = remaining_provider_seconds(ceiling_s)
    except TimeoutError as exc:
        raise ReviewUnavailable("Provider fallback not attempted: review phase deadline already expired") from exc
    phase_deadline = time.monotonic() + budget
    # Both normal primary timeouts already leave 60s grace. A shorter
    # enclosing fleet budget still needs a bounded opportunity to fail over.
    reserve = min(60.0, budget * 0.15) if fallback_enabled else 0.0
    try:
        result = await _run_tandem(
            scripts_dir=scripts_dir, node_dir=node_dir, role=role, prompt=prompt,
            decision_run_id=decision_run_id, timeout_s=min(timeout_s, budget - reserve),
            ceiling_s=max(0, phase_deadline - time.monotonic()),
        )
    except (TimeoutError, ImportError, FileNotFoundError) as exc:
        cause = "timeout" if isinstance(exc, TimeoutError) else "installation"
    except Exception as exc:
        cause = provider_unavailability(exc)
        if not cause and "codex binary not resolvable" in str(exc).lower():
            cause = "CLI unavailable"
        if not cause:
            raise ReviewUnavailable(f"Codex dispatch raised {type(exc).__name__}") from exc
    else:
        code = getattr(result, "exit_code", 0)
        if code == 0:
            response = _codex_response(result)
            if codex_contributed(decision_run_id):
                response.advisory_reason = "Codex contributed to the draft and cannot independently verify it."
            return response
        cause = _exit_reason(code, getattr(result, "stderr", "") or "")
        if not cause:
            raise ReviewUnavailable(f"Codex exited with status {code} and did not complete its review")

    if not fallback_enabled:
        raise ReviewUnavailable(f"Codex unavailable ({cause}), provider fallback disabled")
    log.warning("plan_review.provider_fallback", role=role, decision_run_id=decision_run_id,
                primary="codex", fallback="claude", reason=cause)
    remaining = min(timeout_s, phase_deadline - time.monotonic())
    if remaining <= 0:
        raise ReviewUnavailable(f"Provider fallback not attempted: codex_cli={cause}; phase deadline expired")
    try:
        call = await _review_with_claude(prompt=prompt, user_id=user_id, timeout_s=remaining)
        if not call.text.strip():
            raise RuntimeError("Claude review completed without an answer")
    except Exception as exc:
        failure = provider_unavailability(exc) or "response_or_transport_failure"
        raise ReviewUnavailable(
            f"Both model providers failed: codex_cli={cause}; claude_code={failure}"
        ) from exc
    raw = call.raw if isinstance(call.raw, dict) else {}
    return ReviewResponse(
        "claude", call.model, call.text, call.tokens_in, call.tokens_out,
        float(raw.get("cost_usd_from_sdk") or 0),
        f"Codex unavailable ({cause}). Claude supplied an advisory review. "
        "The independent Codex check remains outstanding.",
        cache_input_tokens=call.cache_input_tokens, cache_creation_tokens=call.cache_creation_tokens,
        thinking_tokens=call.thinking_tokens,
    )
