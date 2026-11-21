import asyncio
import json
import os
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from argosy.agents.base import ModelCall
from argosy.orchestrator.flows.plan_synthesis import codex_second_opinion as math
from argosy.orchestrator.flows.plan_synthesis import review_dispatch
from argosy.orchestrator.flows.plan_synthesis import whole_artifact_reader as reader
from argosy.quality.review_provenance import (
    codex_contributed, phase_three_snapshot, record_codex_plan_author,
)
from argosy.quality.verification import GateStatus


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    from argosy.config import reload_settings

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.setenv("ARGOSY_PROVIDER_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    reload_settings()
    monkeypatch.setattr(math, "_load_existing_codex_opinion", AsyncMock(return_value=None))


def invoke(which, *, includes_codex_sleeve=False):
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    common = dict(decision_run_id=8765, user_id="test")
    if which == "math":
        return asyncio.run(math.run_codex_second_opinion(
            **common, synth_draft_json="{}", analyst_reports_text="test", debate_outcomes_text="",
            risk_verdict_text="", user_directive="",
        ))
    return asyncio.run(reader.run_whole_artifact_review(
        **common, assembled_artifact="A synthetic document.", external_context="", prior_plan_text="",
        includes_codex_sleeve=includes_codex_sleeve,
    ))


@pytest.mark.parametrize("which", ["math", "reader"])
def test_provider_outage_preserves_advisory_and_recovery(enabled, monkeypatch, which):
    answer = '{"overall_assessment":"APPROVE","findings":[]}'
    rejected = SimpleNamespace(exit_code=1, stderr="ERROR: unexpected status 401 Unauthorized",
                               verdict_text=answer)
    primary = AsyncMock(side_effect=[
        rejected, SimpleNamespace(exit_code=0, model="gpt-test", verdict_text=answer),
    ])
    alternate = AsyncMock(return_value=ModelCall(
        text=answer, model="claude-test", tokens_in=12, tokens_out=4,
        raw={"cost_usd_from_sdk": 0.12},
    ))
    monkeypatch.setattr(review_dispatch, "_run_tandem", primary)
    monkeypatch.setattr(review_dispatch, "_review_with_claude", alternate)
    verdict, row, *gates = invoke(which)
    assert verdict is None
    assert row.model == "claude-test" and row.tokens_in == 12 and row.cost_usd == 0.12
    receipt = json.loads(row.response_text)
    assert receipt["advisory_review"]["overall_assessment"] == "APPROVE"
    assert "overall_assessment" not in receipt and receipt["reviewer"] == "claude"
    # The operator sees who reviewed the draft and whether the gate ran.
    from argosy.services.agent_tree_builder import _build_codex_node, _build_reader_node
    row.id = 1
    node = (_build_codex_node if which == "math" else _build_reader_node)(row)
    assert node.status == "degraded" and node.model == "claude-test"
    assert node.failure_reason == receipt["advisory_reason"]
    if gates:
        assert gates[0].status is GateStatus.DID_NOT_RUN
        from argosy.agents.fund_manager import FundManagerAgent
        system, user = FundManagerAgent(user_id="test").build_prompt(
            decision_kind="plan_revision", draft_plan="test", risk_verdict="test",
            codex_second_opinion=row.output,
        )
        assert "independent verification gate is still outstanding" in system
        assert "ADVISORY PLAN REVIEW" in user and "claude-test" in user
    recovered, row, *gates = invoke(which)
    assert recovered.overall_assessment == "APPROVE" and row.model == "gpt-test"
    assert alternate.await_count == 1


@pytest.mark.parametrize("which", ["math", "reader"])
def test_same_provider_author_and_reader_never_approve(enabled, monkeypatch, which):
    candidate = math.CodexSecondOpinion(overall_assessment="APPROVE")
    record_codex_plan_author("plan-synth-8765", "plan_synthesizer", candidate)
    assert not codex_contributed(8765)
    phase_three_snapshot(candidate, [], 8765)
    primary = AsyncMock(return_value=SimpleNamespace(
        exit_code=0, verdict_text='{"overall_assessment":"APPROVE","findings":[]}',
        model="gpt-test",
    ))
    monkeypatch.setattr(review_dispatch, "_run_tandem", primary)
    verdict, row, *gates = invoke(which)
    assert verdict is None and row.output.reviewer == "codex"
    assert "contributed" in row.output.advisory_reason
    assert "overall_assessment" not in json.loads(row.response_text)
    if gates:
        assert gates[0].status is GateStatus.DID_NOT_RUN
    # A replacement generation on the same run gets its own provenance.
    fresh = math.CodexSecondOpinion(overall_assessment="APPROVE")
    phase_three_snapshot(fresh, [], 8765)
    assert not codex_contributed(8765)
    verified, *_ = invoke(which)
    assert verified.overall_assessment == "APPROVE"


@pytest.mark.parametrize("which", ["math", "reader"])
def test_both_unavailable_produces_attributed_failure_receipt(enabled, monkeypatch, which):
    monkeypatch.setattr(review_dispatch, "_run_tandem", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(review_dispatch, "_review_with_claude", AsyncMock(side_effect=ConnectionError()))
    verdict, row, *_ = invoke(which)
    assert verdict is None
    assert row.output.advisory_reason == "Both model providers failed: codex_cli=timeout; claude_code=connection"


def test_bad_primary_output_stays_blocked_without_fallback(enabled, monkeypatch):
    alternate = AsyncMock()
    monkeypatch.setattr(review_dispatch, "_review_with_claude", alternate)
    monkeypatch.setattr(review_dispatch, "_run_tandem", AsyncMock(return_value=SimpleNamespace(
        exit_code=0, verdict_text="not JSON",
    )))
    _, _, gate = invoke("math")
    assert gate.status is GateStatus.BLOCK
    alternate.assert_not_called()


def test_codex_sleeve_makes_final_read_advisory_only(enabled, monkeypatch):
    monkeypatch.setattr(review_dispatch, "_run_tandem", AsyncMock(return_value=SimpleNamespace(
        exit_code=0, verdict_text='{"overall_assessment":"APPROVE","findings":[]}',
    )))
    _, _, numeric = invoke("math")
    assert numeric.status is GateStatus.PASS
    verdict, row = invoke("reader", includes_codex_sleeve=True)
    assert verdict is None and row.output.reviewer == "codex"
    assert "alternatives sleeve" in row.output.advisory_reason


def test_resume_reuses_only_independent_review_of_identical_evidence(monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from argosy.state import db

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as connection:
            await connection.execute(text(
                "CREATE TABLE agent_reports (id INTEGER PRIMARY KEY, user_id TEXT, "
                "agent_role TEXT, decision_id TEXT, user_prompt TEXT, response_text TEXT)"
            ))
            await connection.execute(text(
                "INSERT INTO agent_reports VALUES (1, 'test', 'codex_second_opinion', 'plan-synth-1', "
                "'prior inputs', '{\"overall_assessment\": \"APPROVE\"}'), "
                "(2, 'test', 'codex_second_opinion', 'plan-synth-1', 'new inputs', "
                "'{\"advisory_review\": {\"overall_assessment\": \"APPROVE\"}}')"
            ))
        monkeypatch.setattr(db, "get_session", async_sessionmaker(engine))
        lookup = dict(decision_audit_token="plan-synth-1", user_id="test")
        try:
            assert (await math._load_existing_codex_opinion(**lookup, prompt="prior inputs")).overall_assessment == "APPROVE"
            assert await math._load_existing_codex_opinion(**lookup, prompt="changed inputs") is None
            assert await math._load_existing_codex_opinion(**lookup, prompt="new inputs") is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("elapsed,expected", [(480, 60), (541, None)])
def test_reverse_review_uses_only_remaining_phase_time(enabled, monkeypatch, elapsed, expected):
    now = [1000.0]
    monkeypatch.setattr(review_dispatch, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def primary(**kwargs):
        now[0] += elapsed
        raise TimeoutError()

    alternate = AsyncMock(return_value=ModelCall(
        text='{"overall_assessment":"APPROVE"}', model="claude-test",
    ))
    monkeypatch.setattr(review_dispatch, "_run_tandem", primary)
    monkeypatch.setattr(review_dispatch, "_review_with_claude", alternate)
    verdict, _, gate = invoke("math")
    assert verdict is None and gate.status is GateStatus.DID_NOT_RUN
    if expected is None:
        alternate.assert_not_awaited()
    else:
        assert alternate.await_args.kwargs["timeout_s"] == expected


def test_worker_kills_descendants_before_returning_timeout(tmp_path):
    import psutil

    # This fake kit launches a real child, reports its PID and waits. Passing
    # the check requires the worker AND its child to die on the deadline.
    (tmp_path / "engine_codex.py").write_text(textwrap.dedent("""
        import subprocess, sys, time
        def run_codex(*, node_dir, **kwargs):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
            (node_dir / 'child.pid').write_text(str(child.pid))
            time.sleep(120)
    """), encoding="utf-8")
    with pytest.raises(TimeoutError):
        asyncio.run(review_dispatch._run_tandem(
            scripts_dir=tmp_path, node_dir=tmp_path, role="test", prompt="synthetic",
            decision_run_id=1, timeout_s=8, ceiling_s=10,
        ))
    pid = int((tmp_path / "child.pid").read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
