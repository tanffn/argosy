from argosy.agents.opportunity_pilot import (
    OpportunityChallengerAgent,
    OpportunityEvidenceAgent,
    OpportunityResolutionAgent,
    OpportunitySelectorAgent,
)


def test_pilot_agents_use_toolless_structured_contracts():
    for cls in (OpportunitySelectorAgent, OpportunityChallengerAgent, OpportunityResolutionAgent):
        assert cls.use_structured_output
        assert cls.claude_code_force_toolless
        assert cls.schema_retry_attempts == 2


def test_blind_challenge_and_resolution_are_distinct_roles():
    a = OpportunityChallengerAgent(user_id="test")
    b = OpportunityResolutionAgent(user_id="test")
    blind, _ = a.build_prompt(payload={"raw_analyst_reports": []})
    final, _ = b.build_prompt(payload={"raw_analyst_reports": []})
    assert "NOT been given" in blind
    assert "NOT been given" not in final
    assert "Do not vote" in final
    assert a.agent_role != b.agent_role


def test_evidence_followup_has_bounded_public_tools_and_no_trade_authority():
    agent = OpportunityEvidenceAgent(user_id="test")
    system, user = agent.build_prompt(payload={"ticker": "TEST", "unresolved_questions": ["Read Q2 filing"]})
    assert agent.use_structured_output
    assert agent.claude_code_public_documents
    assert agent.claude_code_max_turns == 12
    assert set(agent.claude_code_allowed_tools) == {"WebSearch", "WebFetch"}
    assert "at most THREE" in system
    assert "Do not supply a verdict" in system
    assert "Read Q2 filing" in user


def test_parent_reports_are_hydrated_and_cross_user_run_or_content_is_rejected(monkeypatch):
    import copy
    import json
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import pytest

    from scripts import run_opportunity_pilot as pilot

    def report(ident, role, run_id=None):
        return SimpleNamespace(id=ident, user_id="owner", agent_role=role, decision_id=run_id,
            response_text="raw", prompt_hash="hash", system_prompt="system", user_prompt="user",
            model="test", created_at=datetime.now(UTC), sources_json='[{"receipt":"actual"}]')
    stored = {1: report(1, "opportunity_pilot_selector"),
              2: report(2, "opportunity_pilot_resolution", "10"),
              3: report(3, "discovery_grader", "10"), 4: report(4, "fundamentals", "10")}
    stored[2].response_text = json.dumps({"verdict": "WATCH", "conviction": "LOW", "rationale": "research",
        "benchmark_opportunity_cost": "unknown", "falsifiers": [], "next_review_date": "2026-11-01",
        "unresolved_questions": ["Actual stored question"], "scenarios": []})
    class FakeSession:
        def __init__(self, engine): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connection(self): return self
        def exec_driver_sql(self, *args): pass
        def get(self, model, key): return stored.get(key)
    monkeypatch.setattr(pilot, "Session", FakeSession)
    monkeypatch.setattr(pilot, "read_engine", lambda: SimpleNamespace(dispose=lambda: None))
    parent = {"agenda": pilot._evidence(stored[1]), "results": [{"decision_run_id": "10",
              "final_evidence": pilot._evidence(stored[2]), "grader": pilot._evidence(stored[3]),
              "raw_reports": [pilot._evidence(stored[4])]}]}
    original = copy.deepcopy(parent)
    hydrated = pilot.verified_parent(copy.deepcopy(parent), "owner")
    assert hydrated["user_id"] == "owner"
    assert hydrated["results"][0]["raw_reports"][0]["sources_json"] == stored[4].sources_json
    assert parent == original
    corrupted_view = copy.deepcopy(parent)
    corrupted_view["results"][0]["final_view"] = {"unresolved_questions": []}
    rebuilt = pilot.verified_parent(corrupted_view, "owner")
    assert rebuilt["results"][0]["final_view"]["unresolved_questions"] == ["Actual stored question"]
    with pytest.raises(ValueError):
        pilot.verified_parent(copy.deepcopy(parent), "other")
    corrupt = copy.deepcopy(parent)
    corrupt["results"][0]["decision_run_id"] = "99"
    with pytest.raises(ValueError):
        pilot.verified_parent(corrupt, "owner")
    corrupt = copy.deepcopy(parent)
    corrupt["results"][0]["decision_run_id"] = None
    with pytest.raises(ValueError, match="run ID is required"):
        pilot.verified_parent(corrupt, "owner")
    corrupt = copy.deepcopy(parent)
    corrupt["results"][0]["raw_reports"][0]["response_text"] = "invented"
    with pytest.raises(ValueError):
        pilot.verified_parent(corrupt, "owner")


def test_followup_rejects_changed_or_recursive_parent_without_mutation(tmp_path, monkeypatch):
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace

    import pytest

    from scripts import run_opportunity_pilot as pilot

    monkeypatch.setattr(pilot, "analysis_database_path", lambda: tmp_path / "test.db")
    parent_path = tmp_path / "parent.json"
    parent_path.write_text(json.dumps({"kind": "analysis_only_opportunity_followup", "status": "completed"}))
    original = parent_path.read_bytes()
    seal = parent_path.with_suffix(".seal.json")
    seal.write_text(json.dumps({"sha256": "wrong"}))
    args = SimpleNamespace(follow_up=parent_path, user_id="owner", out=tmp_path / "new.json", reuse_evidence=None)
    with pytest.raises(ValueError, match="seal mismatch"):
        asyncio.run(pilot.followup(args))
    seal.write_text(json.dumps({"sha256": hashlib.sha256(original).hexdigest()}))
    with pytest.raises(ValueError, match="not another follow-up"):
        asyncio.run(pilot.followup(args))
    assert parent_path.read_bytes() == original
    assert not args.out.exists()
