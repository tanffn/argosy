import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import argosy.services.discovery_followthrough as f
from argosy.services.research_catalog import pending_candidates, record_review_result, record_triage
from argosy.state.models import AgentReport, Base, DecisionRun, User
from argosy.state.research_models import ResearchItem, ResearchReviewRequest, ResearchSource

NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'followthrough.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id="a"), User(id="b")])
        db.add(AgentReport(user_id="a", agent_role="quick_estimator", decision_id="discovery:XYZ",
            created_at=NOW, system_prompt="NO fixed 5x or 10x", user_prompt="ticker=XYZ no primary evidence",
            response_text=json.dumps(dict(ticker="XYZ", go=False, conviction="LOW", sentiment=0,
                                         one_line="No catalyst exists")),
            prompt_hash="abc", tokens_in=1, tokens_out=1, cost_usd=0.01, model="test"))
        db.commit()
    yield url, engine
    engine.dispose()


def audited(store, justified="no", priority=70):
    url, _ = store
    claim = f.claim_audit("a", database_url=url, now=NOW)
    assessment = f.AuditAssessment(justified=justified, conclusion="Evidence gap, not a no-buy judgment",
        research_question="Retrieve the latest issuer earnings and dated catalysts" if justified != "yes" else "",
        priority=priority)
    f.complete_audit("a", claim, assessment, database_url=url, now=NOW)
    return claim


def test_audit_durable_idempotent_and_research_lane_isolated(store):
    url, engine = store
    audited(store)
    assert f.claim_audit("a", database_url=url, now=NOW) is None
    assert f.claim_audit("b", database_url=url, now=NOW) is None
    with Session(engine) as db:
        assert len(f.queue_summary(db, "a")) == 1
        assert f.queue_summary(db, "b") == []
        assert pending_candidates(db, user_id="a", held_tickers=set(), now=NOW) == []
        assert db.scalar(select(ResearchSource)).enabled is False


def test_live_lease_prevents_duplicate_model_work(store):
    url, _ = store
    assert f.claim_audit("a", database_url=url, now=NOW)
    assert f.claim_audit("a", database_url=url, now=NOW) is None


def test_supported_reject_stays_recorded_without_forcing_research(store):
    url, engine = store
    audited(store, "yes")
    with Session(engine) as db:
        assert db.scalar(select(ResearchItem)).status == "analyzed"
        assert f.queue_summary(db, "a") == []
    assert f.claim_review("a", database_url=url, now=NOW) is None


def test_audit_error_backs_off_without_calling_it_completed(store):
    url, engine = store
    claim = f.claim_audit("a", database_url=url, now=NOW)
    f.complete_audit("a", claim, None, error="authentication failed", database_url=url, now=NOW)
    assert f.claim_audit("a", database_url=url, now=NOW) is None
    with Session(engine) as db:
        assert db.get(ResearchItem, claim["item_id"]).status == "failed"


def test_review_error_is_pending_and_not_a_no_buy(store):
    url, engine = store
    audited(store)
    claim = f.claim_review("a", database_url=url, now=NOW)
    assert f.claim_review("a", database_url=url, now=NOW) is None
    f.finish_review("a", claim, error="No quorum", database_url=url, now=NOW)
    with Session(engine) as db:
        row = db.get(ResearchReviewRequest, claim["id"])
        assert row.state == "pending"
        assert json.loads(row.result_json)["error"] == "No quorum"
    assert f.claim_review("a", database_url=url, now=NOW) is None
    assert f.claim_review("a", database_url=url, now=NOW + timedelta(days=2))


def test_cross_tenant_completion_rejected(store):
    url, _ = store
    audited(store)
    claim = f.claim_review("a", database_url=url, now=NOW)
    with pytest.raises(ValueError, match="ownership"):
        f.finish_review("b", claim, error="test", database_url=url, now=NOW)


def test_daily_budget_survives_restart_and_reserves_exploration(store):
    url, _ = store
    for _ in range(3):
        assert f.DailyResearchBudget("a", database_url=url, day="2026-10-23").consume("repair")
    assert not f.DailyResearchBudget("a", database_url=url, day="2026-10-23").consume("repair")
    for _ in range(2):
        assert f.DailyResearchBudget("a", database_url=url, day="2026-10-23").consume()
    assert not f.DailyResearchBudget("a", database_url=url, day="2026-10-23").consume()
    assert f.DailyResearchBudget("a", database_url=url, day="2026-10-24").consume()


def test_budget_deferral_is_not_a_research_failure_and_runs_next_daily_slot(store):
    url, engine = store
    audited(store)
    claim = f.claim_review("a", database_url=url, now=NOW)
    f.finish_review("a", claim, error="Daily research budget used", deferred=True, database_url=url, now=NOW)
    with Session(engine) as db:
        data = json.loads(db.get(ResearchReviewRequest, claim["id"]).result_json)
        assert "error" not in data
        assert data["attempts"] == 0
        assert datetime.fromisoformat(data["due_at"]) == datetime(2026, 10, 24, tzinfo=UTC)
    assert f.claim_review("a", database_url=url, now=NOW + timedelta(hours=19))


def test_future_reports_are_not_contemporaneous_evidence(store):
    url, _ = store
    assert f.claim_audit("a", database_url=url, now=NOW - timedelta(seconds=1)) is None


def test_general_queue_preserves_metadata_and_does_not_complete_failed_reviews(store):
    url, engine = store
    audited(store)
    with Session(engine) as db:
        row = db.scalar(select(ResearchReviewRequest))
        candidate = SimpleNamespace(extra={"research_request_ids": [row.id]})
        record_triage(db, candidate, SimpleNamespace(warrants_decision=True, rationale="review"),
                      user_id="a", now=NOW)
        assert json.loads(row.result_json)["priority"] == 70
        result = SimpleNamespace(status="quorum_failed", action=None, proposal_id=None,
                                 decision_run_id=None, blocked_reason="missing evidence")
        record_review_result(db, candidate, result, user_id="a", now=NOW)
        assert row.state == "ready"
        result.status = "blocked"
        result.blocked_by = "verdict_defended"
        result.news_assessment = {"disposition": "reuse", "rationale": "No changed thesis after review"}
        record_review_result(db, candidate, result, user_id="a", now=NOW)
        assert row.state == "reviewed"
        assert json.loads(row.result_json)["news_assessment"]["disposition"] == "reuse"


@pytest.mark.real_seam
def test_success_creates_three_post_decision_clocks_in_same_transaction(store):
    from argosy.services.contracts import FleetPick
    from argosy.state.models import Prediction, ScanState
    url, engine = store
    audited(store)
    claim = f.claim_review("a", database_url=url, now=NOW)
    with Session(engine) as db:
        db.add(ScanState(user_id="a", ticker="XYZ", estimator_json="{}"))
        run = DecisionRun(user_id="a", status="completed", ticker="XYZ", started_at=NOW, finished_at=NOW)
        db.add(run)
        db.commit()
        run_id = run.id
        report = AgentReport(user_id="a", agent_role="discovery_grader", decision_id=str(run_id),
            created_at=NOW, response_text=json.dumps({"ticker": "XYZ", "conviction": "LOW", "verdict": "WATCH",
                "thesis_md": "Full independent thesis", "cites": []}),
            prompt_hash="test", tokens_in=1, tokens_out=1, cost_usd=0.01, model="test")
        db.add(report)
        db.commit()
        report_id = report.id
    pick = FleetPick("XYZ", "LOW", "Full independent thesis", "WATCH", decision_run_id=run_id,
                     report_id=report_id, completed_at=NOW.isoformat())
    f.bind_run("a", claim, run_id, database_url=url)
    f.finish_review("a", claim, pick, database_url=url, now=NOW)
    # Replaying finalization must be exactly-once for its clocks.
    f.finish_review("a", claim, pick, database_url=url, now=NOW)
    with Session(engine) as db:
        clocks = list(db.scalars(select(Prediction)))
        assert len(clocks) == 3
        assert all(p.entry_price is None for p in clocks)
        assert db.get(ResearchReviewRequest, claim["id"]).state == "reviewed"
        from argosy.services.predictions.writers import ensure_discovery_evaluation_predictions
        assert ensure_discovery_evaluation_predictions(db, user_id="a")["predictions_created"] == 0
        assert len(list(db.scalars(select(Prediction)))) == 3
        assert json.loads(db.get(ScanState, ("a", "XYZ")).fleet_json)["quality_request_id"] == claim["id"]


def test_new_questions_for_same_ticker_are_coalesced_without_losing_origins(store):
    url, engine = store
    audited(store)
    with Session(engine) as db:
        original = db.scalar(select(ResearchReviewRequest))
        second = ResearchReviewRequest(id="second", user_id="a", item_id=original.item_id,
            ticker=original.ticker, reason="Verify dilution independently", state="pending",
            created_at=NOW, updated_at=NOW, result_json=original.result_json)
        db.add(second)
        db.commit()
    claim = f.claim_review("a", database_url=url, now=NOW)
    assert len(claim["request_ids"]) == 2
    assert "Verify dilution independently" in claim["question"]
    assert f.claim_review("a", database_url=url, now=NOW) is None
    f.finish_review("a", claim, error="interrupted", database_url=url, now=NOW)
    with Session(engine) as db:
        assert all(row.state == "pending" for row in db.scalars(select(ResearchReviewRequest)))
        assert all("_auto_owner" not in json.loads(row.result_json)
                   for row in db.scalars(select(ResearchReviewRequest)))


def test_audit_rejects_mutated_evidence(store):
    url, engine = store
    claim = f.claim_audit("a", database_url=url, now=NOW)
    with Session(engine) as db:
        report = db.get(AgentReport, claim["case"]["report_id"])
        report.user_prompt = "Changed evidence"
        db.commit()
    with pytest.raises(ValueError, match="source evidence"):
        f.complete_audit("a", claim, f.AuditAssessment(justified="yes", conclusion="supported"),
                         database_url=url, now=NOW)


def test_recovery_uses_original_synthesis_time_not_restart_time(store):
    url, engine = store
    with Session(engine) as db:
        run = DecisionRun(user_id="a", ticker="XYZ", status="running", started_at=NOW)
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(AgentReport(user_id="a", agent_role="discovery_grader", decision_id=str(run_id),
            created_at=NOW, response_text=json.dumps({"ticker": "XYZ", "conviction": "LOW",
                "verdict": "PASS", "thesis_md": "Dated conclusion", "cites": []}),
            prompt_hash="test", tokens_in=1, tokens_out=1, cost_usd=0.01, model="test"))
        db.commit()
    result = f._recover_completed("a", {"decision_run_id": run_id, "ticker": "XYZ"}, database_url=url)
    assert datetime.fromisoformat(result.completed_at) == NOW
    with Session(engine) as db:
        assert db.get(DecisionRun, run_id).status == "completed"


@pytest.mark.real_seam
@pytest.mark.parametrize("prior_blob", [None, "[]", "null", "broken"])
def test_new_incomplete_run_cannot_hide_older_completed_paid_research(store, monkeypatch, prior_blob):
    from argosy.services import high_potential_funnel as funnel
    from argosy.services import research_catalog as catalog
    url, engine = store
    original_session = catalog.research_session
    def local_session(*args, **kwargs):
        return original_session(url, user_id=kwargs.get("user_id"))
    monkeypatch.setattr(catalog, "research_session", local_session)
    monkeypatch.setattr(f, "research_session", local_session)
    monkeypatch.setattr(funnel, "radar_fingerprint", lambda _: "fp")
    with Session(engine) as db:
        notes = json.dumps({"radar_fingerprint": "fp", "research_contract": funnel.FLEET_RESEARCH_CONTRACT})
        older = DecisionRun(user_id="a", ticker="XYZ", status="completed", started_at=NOW,
                            finished_at=NOW, notes_json=notes)
        newer = DecisionRun(user_id="a", ticker="XYZ", status="running", started_at=NOW, notes_json=notes)
        db.add_all([older, newer])
        db.flush()
        older_id, newer_id = older.id, newer.id
        db.add(AgentReport(user_id="a", agent_role="discovery_grader", decision_id=str(older_id),
            created_at=NOW, response_text=json.dumps({"ticker": "XYZ", "conviction": "LOW",
                "verdict": "WATCH", "thesis_md": "Original research", "cites": []}),
            prompt_hash="test", tokens_in=1, tokens_out=1, cost_usd=0.01, model="test"))
        db.commit()
    recovered = funnel._recover_grade("a", SimpleNamespace(ticker="XYZ"), {"fleet_json": prior_blob}, NOW)
    assert recovered.decision_run_id == older_id
    with Session(engine) as db:
        assert db.get(DecisionRun, newer_id).status == "running"


def test_scheduled_path_automatically_audits_reviews_then_explores(monkeypatch):
    import argosy.orchestrator.loops.discovery_funnel_loop as loop
    from argosy.services.high_potential_funnel import FunnelResult
    events = []
    async def audit(user_id):
        events.append("audit")
        return {"audited": 1}
    async def review(user_id, budget):
        events.append("repair")
        return {"attempted": 1}
    async def discover(user_id, **kwargs):
        events.append("discovery")
        assert kwargs["research_budget"]
        return FunnelResult([], [], [], "now")
    monkeypatch.setattr(f, "audit_recent_screens", audit)
    monkeypatch.setattr(f, "run_due_reviews", review)
    monkeypatch.setattr(loop, "run_funnel", discover)
    result, summary = asyncio.run(loop._run_with_retry("a"))
    assert events == ["audit", "repair", "discovery"]
    assert summary["reviews"]["attempted"] == 1
