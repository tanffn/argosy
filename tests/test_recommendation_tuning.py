from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.recommendation_tuning import build_tuning_snapshot, partition
from argosy.state.models import AgentReport, Base, User
from evals.fleet_calibration import tuning


@pytest.mark.parametrize("malformed_first", [False, True])
def test_half_open_partition_never_restarts_first_observation(malformed_first):
    bounds = ["2026-08-28", "2026-09-14", "2026-09-28"]
    assert partition("2026-09-13", bounds) == "development"
    assert partition("2026-09-14", bounds) == "reserved"
    assert partition("2026-09-28", bounds) == "outside"
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="owner"))
        db.add(User(id="other"))
        for ident, owner, ticker, at in [(1, "owner", "AAA", "2026-09-01"),
                                        (2, "owner", "AAA", "2026-09-15"),
                                        (3, "owner", "BBB", "2026-09-15"),
                                        (4, "other", "ZZZ", "2026-09-01")]:
            db.add(AgentReport(id=ident, user_id=owner, agent_role="quick_estimator", model="test",
                response_text="invalid" if malformed_first and ident == 1 else '{"ticker":"'+ticker+'","go":false,"conviction":"LOW","sentiment":0,"one_line":"unknown"}',
                user_prompt=f"ticker={ticker} name=archived", system_prompt="original policy", created_at=datetime.fromisoformat(at)))
        db.flush()
        snapshot = build_tuning_snapshot(db, user_id="owner", packet_date=date(2026, 10, 23), boundaries=bounds)
    assert snapshot["counts"]["all_screen_calls"] == 3
    assert {(r["ticker"], r["partition"]) for r in snapshot["first_screens"]} == {("AAA", "development"), ("BBB", "reserved")}
    assert "4" not in snapshot["raw_records"]
    if malformed_first:
        assert snapshot["first_screens"][0]["go"] is None
    engine.dispose()


def test_seals_detect_mutation_and_are_write_once(tmp_path):
    path = tmp_path / "snapshot.json"
    tuning.save_new(path, {"protocol": tuning.TUNING_PROTOCOL})
    tuning.seal(path, "frozen")
    assert tuning.load_sealed(path)[0]["protocol"] == tuning.TUNING_PROTOCOL
    with pytest.raises(FileExistsError):
        tuning.save_new(path, {})
    path.write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError, match="seal mismatch"):
        tuning.load_sealed(path)


def test_paired_result_requires_every_pair_and_does_not_judge_buy_labels():
    def arm(defects):
        return {"status": "completed", "review_verified": True, "review": {"output": {
            "assessable": True, "findings": [{"severity": "material"}] * defects}}}
    doc = {"pairs": [{"arms": {"excerpt": arm(1), "complete": arm(0)}} for _ in range(4)]}
    assert tuning.paired_summary(doc)["conclusion"] == "initial_fidelity_benefit"
    doc["pairs"][0]["arms"]["complete"]["review_verified"] = False
    assert tuning.paired_summary(doc)["conclusion"] == "inconclusive"
    doc["pairs"][0]["arms"]["complete"] = arm(2)
    assert tuning.paired_summary(doc)["worse"] == 1
    assert not tuning.paired_summary(doc)["investment_performance_claim"]


def test_claim_quotes_are_attribution_only_and_assessors_have_no_tools():
    assessment = tuning.FidelityAssessment(assessable=True, findings=[tuning.Finding(
        tag="unsupported_fact", severity="material", claim_quote="invented profit", reason="not supplied")],
        preserved_material_unknowns=True, extra_research_needed=[], conclusion="research")
    assert tuning.verified_findings(assessment, "The invented profit is unsupported")
    assert not tuning.verified_findings(assessment, "Different statement")
    assert tuning.FidelityAgent.claude_code_force_toolless
    assert tuning.FidelityAgent.claude_code_force_isolated


def test_one_shot_claim_survives_new_filename_and_freeze_timestamp(tmp_path):
    snapshot = {"protocol": tuning.TUNING_PROTOCOL, "frozen_at": "first", "selection": "same"}
    tuning.claim_trial(snapshot, "paired-development", tmp_path / "a.json", claim_root=tmp_path)
    snapshot["frozen_at"] = "second"
    with pytest.raises(FileExistsError):
        tuning.claim_trial(snapshot, "paired-development", tmp_path / "b.json", claim_root=tmp_path)


def test_exclusive_claim_is_synchronized_before_return(tmp_path, monkeypatch):
    events = []
    original = tuning.os.fsync
    def sync(descriptor):
        original(descriptor)
        events.append("fsync")
    monkeypatch.setattr(tuning.os, "fsync", sync)
    tuning.claim_trial({"protocol": "test"}, "audit", tmp_path / "out.json", claim_root=tmp_path)
    events.append("claim_returned")
    assert events == ["fsync", "claim_returned"]


def test_admission_is_durable_stops_before_assessor_and_records_cancellation(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace

    class FakeAgent:
        agent_role = "test"
        model = "test"
        thinking_effort = "high"
        thinking_budget = 0
        sdk_timeout_seconds = 60
        citations_enabled = False
        max_tokens = 100
        claude_code_max_retries = 0
        schema_retry_attempts = 0
        calls = 0
        async def run(self, **kwargs):
            self.calls += 1
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["admitted_sessions"][-1]["status"] == "running"
            if kwargs.get("cancel"):
                raise asyncio.CancelledError()
            return SimpleNamespace(cost_usd=31, prompt_hash="hash")
    agent = FakeAgent()
    path = tmp_path / "trial.json"
    def document():
        return {"maximum_sdk_sessions": 2, "observed_cost_usd": 0,
                "runtime_contract": {"agents": {"FakeAgent": tuning.effective_runtime(agent)}}}
    doc = document()
    asyncio.run(tuning.admitted_call(agent, {}, doc, path))
    with pytest.raises(ValueError, match="no new SDK session"):
        asyncio.run(tuning.admitted_call(agent, {}, doc, path))
    assert agent.calls == 1
    doc = document()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tuning.admitted_call(agent, {"cancel": True}, doc, path))
    assert doc["unknown_cost_failure"]
    assert doc["admitted_sessions"][0]["status"] == "interrupted"
