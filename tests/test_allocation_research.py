from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from argosy.agents.deployment_reviewer import DeploymentReviewOutput
from argosy.services.allocation_research import pending_tasks, refresh_due_research, sync_sheet_research
from argosy.services.deploy_decision_team import build_review_resolution, run_deploy_decision_team
from argosy.services.order_sheet import PendingResearch, pending_research_errors
from argosy.state.models import ScanState


def pending(**changes):
    data = dict(tickers=["AAA", "BBB"], disagreement="Different valuation assumptions",
                missing_evidence="Sourced terminal revenue", research_question="What supports terminal revenue?",
                next_review_date=(datetime.now(UTC) + timedelta(days=1)).date(),
                reserved_usd=10000, independence_reason="Core actions retain the entire research funding.")
    data.update(changes)
    return PendingResearch(**data)


def test_automatic_failed_retries_are_cooled_down_bounded_and_durable(alembic_engine_at_head):
    now = datetime(2026, 9, 14, 8, tzinfo=UTC)
    with Session(alembic_engine_at_head) as db:
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", pending_research=[
            pending(next_review_date=now.date())], candidate_comparisons=[]), "retry")
        db.commit()
        calls = []
        def fail(*args):
            calls.append(1)
            raise RuntimeError("temporary provider outage")
        for minutes in [0, 10, 31, 40, 62, 120]:
            out = refresh_due_research(db, "ariel", now=now+timedelta(minutes=minutes), research_fn=fail)
        assert len(calls) == 3
        assert out["recovery"][0]["automatic_retry"] is False
        assert out["recovery"][0]["attempts_today"] == 3
    with Session(alembic_engine_at_head) as db:
        refresh_due_research(db, "ariel", now=now+timedelta(hours=4), research_fn=fail)
        assert len(calls) == 3
        refresh_due_research(db, "ariel", now=now+timedelta(days=1), research_fn=fail)
        assert len(calls) == 4


def test_regrouping_preserves_retry_cooldown_and_exhaustion(alembic_engine_at_head):
    from argosy.services.allocation_research import research_retry_state
    now = datetime(2026, 9, 14, 8, tzinfo=UTC)
    with Session(alembic_engine_at_head) as db:
        sheet = SimpleNamespace(user_id="ariel", pending_research=[pending(next_review_date=now.date())], candidate_comparisons=[])
        sync_sheet_research(db, sheet, "initial")
        db.commit()
        calls = []
        def fail(*args):
            calls.append(1)
            raise RuntimeError("temporary outage")
        refresh_due_research(db, "ariel", now=now, research_fn=fail)
        sheet.pending_research = [pending(tickers=["AAA", "BBB", "CCC"], next_review_date=now.date())]
        sync_sheet_research(db, sheet, "merged")
        db.commit()
        refresh_due_research(db, "ariel", now=now+timedelta(minutes=1), research_fn=fail)
        assert len(calls) == 1
        for minutes in [31, 62]:
            refresh_due_research(db, "ariel", now=now+timedelta(minutes=minutes), research_fn=fail)
        assert len(calls) == 3
        sheet.pending_research = [pending(tickers=["AAA"], next_review_date=now.date()),
                                 pending(tickers=["BBB", "CCC"], next_review_date=now.date())]
        sync_sheet_research(db, sheet, "split")
        db.commit()
        out = refresh_due_research(db, "ariel", now=now+timedelta(hours=3), research_fn=fail)
        assert len(calls) == 3 and len(out["failures"]) == 2
        assert all(research_retry_state(row, now)["attempts_today"] == 3 for row in pending_tasks(db, "ariel"))


def test_regrouping_after_success_does_not_repeat_research_that_day(alembic_engine_at_head):
    now = datetime(2026, 9, 14, 8, tzinfo=UTC)
    with Session(alembic_engine_at_head) as db:
        for ticker in ["AAA", "BBB"]:
            db.add(ScanState(user_id="ariel", ticker=ticker, status="active"))
        sheet = SimpleNamespace(user_id="ariel", pending_research=[pending(next_review_date=now.date())], candidate_comparisons=[])
        sync_sheet_research(db, sheet, "initial")
        db.commit()
        calls = []
        def research(item, user):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return {t: {"verdict": "WATCH", "conviction": "LOW"} for t in item.tickers}
        for minutes in [0, 31, 62]:
            out = refresh_due_research(db, "ariel", now=now+timedelta(minutes=minutes), research_fn=research)
        assert out["refreshed"]
        sheet.pending_research = [pending(tickers=[t], next_review_date=now.date()) for t in ["AAA", "BBB"]]
        sync_sheet_research(db, sheet, "split-success")
        db.commit()
        refresh_due_research(db, "ariel", now=now+timedelta(hours=2), research_fn=research)
        assert len(calls) == 3
        refresh_due_research(db, "ariel", now=now+timedelta(days=1), research_fn=research)
        assert len(calls) == 5


@pytest.mark.parametrize("status", ["dropped", "quarantined"])
def test_pending_research_preserves_radar_lifecycle(alembic_engine_at_head, status):
    now = datetime(2026, 9, 22, 13, tzinfo=UTC)
    old = now - timedelta(days=1)
    with Session(alembic_engine_at_head) as db:
        db.add(ScanState(user_id="ariel", ticker="AAA", status=status,
                         rank=17, last_radar_at=old, quarantine_reason="existing reason"))
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", pending_research=[
            pending(tickers=["AAA"], next_review_date=now.date())], candidate_comparisons=[]), "lifecycle")
        db.commit()
        out = refresh_due_research(db, "ariel", now=now,
            research_fn=lambda item, user: {"AAA": {"verdict": "WATCH", "conviction": "LOW"}})
        scan = db.get(ScanState, ("ariel", "AAA"))
        assert scan.status == status and scan.rank == 17
        assert scan.last_radar_at.replace(tzinfo=UTC) == old
        assert scan.quarantine_reason == "existing reason"
        if status == "dropped":
            assert out["refreshed"] and not out["failures"]
            assert json.loads(scan.fleet_json)["verdict"] == "WATCH"
            assert pending_tasks(db, "ariel")[0].status == "pending"
        else:
            assert out["failures"] and not out["refreshed"]
            assert not scan.fleet_json


def test_author_sees_pending_dropped_research_not_unrelated_or_quarantined(alembic_engine_at_head):
    from argosy.services.allocation_author.packet_assembly import assemble_author_packet
    from argosy.services.target_allocation_doc import TargetAllocationDoc
    now = datetime.now(UTC)
    with Session(alembic_engine_at_head) as db:
        for ticker, status in [("AAA", "dropped"), ("BBB", "dropped"),
                               ("CCC", "quarantined"), ("DDD", "active")]:
            db.add(ScanState(user_id="ariel", ticker=ticker, status=status,
                            rank=17, last_fleet_at=now, last_radar_at=now-timedelta(days=1),
                            fleet_json=json.dumps({"verdict": "WATCH", "conviction": "LOW"})))
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", pending_research=[
            pending(tickers=["AAA", "CCC"], next_review_date=now.date())], candidate_comparisons=[]), "packet")
        db.commit()
        doc = TargetAllocationDoc(anchor_sigma=.18, blended_sigma=.16, nvda_cap_pct=30,
                                  fi_pct=10, provenance="test", classes=[], glide=[])
        packet = assemble_author_packet(db, user_id="ariel", doc=doc,
                                       holdings_usd={}, cash_usd=0, deployable_usd=0)
        candidates = {r["ticker"]: r for r in packet["discovery_candidates"]}
        assert set(candidates) == {"AAA", "DDD"}
        assert candidates["AAA"]["search_source"] == "allocation_research_followup"
        assert candidates["AAA"]["radar_status"] == "dropped"
        assert candidates["AAA"]["radar_as_of"] != candidates["AAA"]["fresh_as_of"]
        assert db.get(ScanState, ("ariel", "AAA")).status == "dropped"


def test_interrupted_research_resumes_checkpoint_without_promoting_partial_result(alembic_engine_at_head, monkeypatch):
    from argosy.services import allocation_research as module
    now = datetime(2026, 9, 14, 8, tzinfo=UTC)
    class ProcessExit(BaseException):
        pass
    seen = []
    def research(item, user, *, completed, checkpoint):
        seen.append(dict(completed))
        if not completed:
            checkpoint({"AAA": {"verdict": "WATCH", "conviction": "LOW"}})
            raise ProcessExit()
        return {**completed, "BBB": {"verdict": "WATCH", "conviction": "LOW"}}
    monkeypatch.setattr(module, "_research", research)
    with Session(alembic_engine_at_head) as db:
        for ticker in ["AAA", "BBB"]:
            db.add(ScanState(user_id="ariel", ticker=ticker, status="active"))
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", pending_research=[
            pending(next_review_date=now.date())], candidate_comparisons=[]), "checkpoint")
        db.commit()
        with pytest.raises(ProcessExit):
            refresh_due_research(db, "ariel", now=now)
        assert not db.get(ScanState, ("ariel", "AAA")).fleet_json
    with Session(alembic_engine_at_head) as db:
        out = refresh_due_research(db, "ariel", now=now+timedelta(minutes=31))
        assert out["refreshed"] and not out["failures"]
        assert list(seen[1]) == ["AAA"]
        assert pending_tasks(db, "ariel")[0].last_error is None
        assert json.loads(pending_tasks(db, "ariel")[0].result_json)["as_of"] == now.isoformat()
        refresh_due_research(db, "ariel", now=now+timedelta(hours=1))
        assert len(seen) == 2


@pytest.mark.parametrize("change", ["question", "next_day"])
def test_checkpoint_reuse_requires_same_request_and_day(alembic_engine_at_head, monkeypatch, change):
    from argosy.services import allocation_research as module
    now = datetime(2026, 9, 14, 8, tzinfo=UTC)
    item = pending(next_review_date=now.date())
    with Session(alembic_engine_at_head) as db:
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", pending_research=[item], candidate_comparisons=[]), "checkpoint")
        db.commit()
        row = pending_tasks(db, "ariel")[0]
        import hashlib
        row.result_json = json.dumps({"checkpoint": {"input_hash": hashlib.sha256(item.model_dump_json().encode()).hexdigest(),
            "as_of": now.isoformat(), "tickers": {"AAA": {"verdict": "WATCH"}}}})
        row.last_error = "interrupted"
        row.last_attempted_at = now
        if change == "question":
            payload = json.loads(row.payload_json)
            payload["research_question"] = "A different question"
            row.payload_json = json.dumps(payload)
        db.commit()
        def research(item, user, *, completed, checkpoint):
            assert not completed
            raise RuntimeError("sentinel: no old checkpoint reused")
        monkeypatch.setattr(module, "_research", research)
        later = now + (timedelta(days=1) if change == "next_day" else timedelta(minutes=31))
        out = refresh_due_research(db, "ariel", now=later)
        assert "sentinel" in out["failures"][0]


def test_contract_conserves_shared_alternative_reserve_and_excludes_orders():
    item = pending()
    args = dict(reserve_usd=10000, action_symbols={"CORE"}, as_of=datetime.now(UTC).date())
    assert not pending_research_errors([item], **args)
    assert pending_research_errors([item], **{**args, "reserve_usd": 9999})
    assert pending_research_errors([item], **{**args, "action_symbols": {"AAA"}})
    assert pending_research_errors([item, item], **args)
    assert pending_research_errors([pending(next_review_date=args["as_of"])], **args)
    with pytest.raises(ValueError):
        pending(reserved_usd=float("nan"))


def test_explicit_run_allows_zero_new_cash_but_never_negative():
    from argosy.api.routes.e2e_proof import RunRequest
    assert RunRequest(cash_usd=0).cash_usd == 0
    with pytest.raises(ValueError):
        RunRequest(cash_usd=-1)


def test_explicit_failed_retry_preserves_history_and_success_stays_daily(alembic_engine_at_head):
    now = datetime.now(UTC)
    item = pending(tickers=["AAA"], next_review_date=now.date())
    sheet = SimpleNamespace(user_id="ariel", pending_research=[item], candidate_comparisons=[])
    with Session(alembic_engine_at_head) as db:
        db.add(ScanState(user_id="ariel", ticker="AAA", status="active"))
        sync_sheet_research(db, sheet, "retry")
        db.commit()
        calls = []
        def fail(*args):
            calls.append("failed")
            raise ValueError("original schema error")
        def succeed(*args):
            calls.append("ok")
            return {"AAA": {"ticker": "AAA", "verdict": "WATCH", "conviction": "LOW"}}
        assert refresh_due_research(db, "ariel", now=now, research_fn=fail)["failures"]
        assert refresh_due_research(db, "ariel", now=now, research_fn=succeed)["failures"]
        assert calls == ["failed"]
        recovered = refresh_due_research(db, "ariel", now=now, research_fn=succeed, retry_failed=True)
        assert recovered["refreshed"] and not recovered["failures"]
        row = pending_tasks(db, "ariel")[0]
        assert "original schema error" in json.loads(row.payload_json)["lifecycle_history"][-1]["last_error"]
        assert row.last_error is None
        refresh_due_research(db, "ariel", now=now, research_fn=succeed, retry_failed=True)
        assert calls == ["failed", "ok"]


@pytest.mark.parametrize("fund_verdict", ["HOLD", "TRIM"])
def test_mixed_fund_and_stock_research_needs_no_fake_fund_scan(alembic_engine_at_head, fund_verdict):
    from argosy.services.allocation_research import research_context, fund_research_evidence
    now = datetime.now(UTC)
    item = pending(tickers=["AAA", "IWQU"], next_review_date=now.date())
    sheet = SimpleNamespace(user_id="ariel", pending_research=[item], candidate_comparisons=[], freshness_days=3,
        review_resolution=SimpleNamespace(one_voice=True, reviewers_ran=5, reviewers_expected=5))
    with Session(alembic_engine_at_head) as db:
        db.add(ScanState(user_id="ariel", ticker="AAA", status="active"))
        sync_sheet_research(db, sheet, "mixed")
        db.commit()
        result = refresh_due_research(db, "ariel", now=now, research_fn=lambda *a: {
            "AAA": {"ticker": "AAA", "verdict": "WATCH", "conviction": "LOW"},
            "IWQU": {"kind": "fund_vehicle", "ticker": "IWQU", "verdict": fund_verdict, "conviction": "MED", "report_id": 123},
        })
        assert result["refreshed"] and not result["failures"]
        assert db.get(ScanState, ("ariel", "IWQU")) is None
        evidence = fund_research_evidence(research_context(db, "ariel"))
        assert evidence["IWQU"]["verdict"] == fund_verdict
        sheet.pending_research = [pending(tickers=["AAA"])]
        comparison = SimpleNamespace(ticker="IWQU", evidence_fresh_as_of=now, research_verdict=fund_verdict, research_conviction="MED")
        sheet.candidate_comparisons = [comparison]
        comparison.research_verdict = "BUY"
        with pytest.raises(ValueError, match="current sourced comparisons"):
            sync_sheet_research(db, sheet, "wrong-grade")
        db.rollback()
        comparison.research_verdict = fund_verdict
        comparison.evidence_fresh_as_of = now - timedelta(days=10)
        with pytest.raises(ValueError, match="current sourced comparisons"):
            sync_sheet_research(db, sheet, "stale-grade")
        db.rollback()
        comparison.evidence_fresh_as_of = now
        sync_sheet_research(db, sheet, "fund-resolved")
        db.commit()
        assert len(pending_tasks(db, "ariel")) == 1
        assert '"IWQU"' not in pending_tasks(db, "ariel")[0].payload_json.split('"prior_tasks"')[0]


def test_fund_research_dispatch_and_question_rendering(monkeypatch):
    from argosy.services import allocation_research as service
    from argosy.agents.fund_vehicle_analyst import FundVehicleAnalystAgent
    calls = []
    async def fund(ticker, issue, user):
        calls.append((ticker, issue.research_question, user))
        return {"kind": "fund_vehicle", "report_id": 1}
    monkeypatch.setattr(service, "_research_fund", fund)
    issue = pending(tickers=["IWQU"])
    assert service._research(issue, "ariel")["IWQU"]["kind"] == "fund_vehicle"
    assert calls == [("IWQU", issue.research_question, "ariel")]
    agent = FundVehicleAnalystAgent.__new__(FundVehicleAnalystAgent)
    system, user, sources = agent.build_prompt(ticker="IWQU", fund_context={"research_question": "Does this funded size clear the allocation criterion?",
        "research_reserve_usd": 20000, "live_execution_evidence": {"price_as_of": "2026-09-11"}})
    supplied = system + user + "\n".join(body for _, body in sources)
    assert "Does this funded size clear" in supplied and "20000" in supplied and "2026-09-11" in supplied


@pytest.mark.parametrize("safe,clear", [(False, False), (True, True)])
def test_every_blind_reviewer_must_approve_separation(safe, clear):
    proposal = SimpleNamespace(buys=[], sells=[], pending_research=[pending()])
    def reviewer(lens, packet, buys, **kwargs):
        assert packet["proposed_pending_research"][0]["reserved_usd"] == 10000
        assert "independence_reason" not in packet["proposed_pending_research"][0]
        return DeploymentReviewOutput(lens=lens, separation_safe=(safe or lens == "prudence"))
    decision = run_deploy_decision_team({}, proposal, lenses=("prudence", "sizing"), review_fn=reviewer)
    assert decision.all_clear == clear
    assert build_review_resolution([decision], proposal.pending_research).separation_reviewed == clear


def test_due_queue_survives_session_and_refresh_is_not_resolution(alembic_engine_at_head):
    now = datetime.now(UTC)
    item = pending(next_review_date=now.date() - timedelta(days=2))
    sheet = SimpleNamespace(user_id="ariel", pending_research=[item], candidate_comparisons=[])
    with Session(alembic_engine_at_head) as db:
        sync_sheet_research(db, sheet, "first")
        for ticker in item.tickers:
            db.add(ScanState(user_id="ariel", ticker=ticker, status="active"))
        db.commit()
    with Session(alembic_engine_at_head) as db:
        def research(issue, user):
            assert user == "ariel" and issue.research_question
            return {s: {"ticker": s, "verdict": "WATCH", "conviction": "LOW", "thesis_md": "Still uncertain"} for s in issue.tickers}
        result = refresh_due_research(db, "ariel", now=now, research_fn=research)
        assert len(result["refreshed"]) == 1
        assert len(pending_tasks(db, "ariel")) == 1  # research is never approval
        assert not pending_tasks(db, "someone_else")
        assert not refresh_due_research(db, "ariel", now=now, research_fn=lambda *_: pytest.fail())["refreshed"]
        # Re-authoring cannot keep pushing an overdue date forward.
        sheet.pending_research = [pending()]
        sync_sheet_research(db, sheet, "second")
        db.commit()
        assert pending_tasks(db, "ariel")[0].next_review_date == item.next_review_date
        sheet.pending_research = []
        sync_sheet_research(db, sheet, "third")
        assert pending_tasks(db, "ariel")  # omission is not resolution
        sheet.candidate_comparisons = [SimpleNamespace(ticker=s) for s in item.tickers]
        with pytest.raises(ValueError, match="current sourced comparisons"):
            sync_sheet_research(db, sheet, "presence-only")
        sheet.review_resolution = SimpleNamespace(one_voice=True, reviewers_ran=5, reviewers_expected=5)
        sheet.freshness_days = 3
        sheet.candidate_comparisons = [SimpleNamespace(
            ticker=s, evidence_fresh_as_of=now, research_verdict="WATCH", research_conviction="LOW",
        ) for s in item.tickers]
        sheet.candidate_comparisons[0].evidence_fresh_as_of = now - timedelta(days=6)
        with pytest.raises(ValueError, match="current sourced comparisons"):
            sync_sheet_research(db, sheet, "stale-comparison")
        sheet.candidate_comparisons[0].evidence_fresh_as_of = now
        sheet.candidate_comparisons[0].research_verdict = "BUY"
        with pytest.raises(ValueError, match="current sourced comparisons"):
            sync_sheet_research(db, sheet, "wrong-grade")
        sheet.candidate_comparisons[0].research_verdict = "WATCH"
        sync_sheet_research(db, sheet, "fourth")
        db.commit()
        assert not pending_tasks(db, "ariel")


def test_failed_research_is_durable_and_retries_next_day(alembic_engine_at_head):
    now = datetime.now(UTC)
    item = pending(next_review_date=now.date())
    with Session(alembic_engine_at_head) as db:
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", pending_research=[item], candidate_comparisons=[]), "sheet")
        db.commit()
        attempts = []
        def fail(*_):
            attempts.append(1)
            raise RuntimeError("Claude authentication expired")
        out = refresh_due_research(db, "ariel", now=now, research_fn=fail)
        assert "authentication" in out["failures"][0]
        assert pending_tasks(db, "ariel")[0].last_error
        refresh_due_research(db, "ariel", now=now, research_fn=fail)
        assert len(attempts) == 1
        refresh_due_research(db, "ariel", now=now + timedelta(days=1), research_fn=fail)
        assert len(attempts) == 2


def test_additive_migration_round_trip(alembic_engine_at_head):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    cfg = Config("alembic.ini")
    assert inspect(alembic_engine_at_head).has_table("allocation_research_tasks")
    command.downgrade(cfg, "0116_youtube_influencer_intelligence")
    assert not inspect(alembic_engine_at_head).has_table("allocation_research_tasks")
    command.upgrade(cfg, "head")
    assert inspect(alembic_engine_at_head).has_table("allocation_research_tasks")


def test_regrouping_cannot_postpone_or_erase_research(alembic_engine_at_head):
    import json
    overdue = datetime.now(UTC).date() - timedelta(days=3)
    with Session(alembic_engine_at_head) as db:
        sheet = SimpleNamespace(user_id="ariel", candidate_comparisons=[], pending_research=[pending(next_review_date=overdue)])
        sync_sheet_research(db, sheet, "initial")
        db.commit()
        old = pending_tasks(db, "ariel")[0]
        old.last_error = "source unavailable"
        old.result_json = json.dumps({"earlier_finding": "uncertain revenue"})
        db.commit()
        sheet.pending_research = [pending(tickers=["AAA", "BBB", "CCC"])]
        sync_sheet_research(db, sheet, "merged")
        db.commit()
        merged = pending_tasks(db, "ariel")[0]
        assert merged.next_review_date == overdue
        assert merged.last_error == "source unavailable"
        assert "uncertain revenue" in merged.result_json
        sheet.pending_research = [pending(tickers=["AAA"]), pending(tickers=["BBB", "CCC"])]
        sync_sheet_research(db, sheet, "split")
        db.commit()
        rows = pending_tasks(db, "ariel")
        assert len(rows) == 2
        assert all(row.next_review_date == overdue and row.last_error == "source unavailable" for row in rows)
        assert all("uncertain revenue" in row.result_json for row in rows)


def test_mixed_resolve_carry_and_reopened_lifecycle_preserve_history(alembic_engine_at_head):
    import json
    now = datetime.now(UTC)
    with Session(alembic_engine_at_head) as db:
        sheet = SimpleNamespace(user_id="ariel", candidate_comparisons=[], pending_research=[pending()],
            freshness_days=3, review_resolution=SimpleNamespace(one_voice=True, reviewers_ran=5, reviewers_expected=5))
        sync_sheet_research(db, sheet, "original")
        db.commit()
        old = pending_tasks(db, "ariel")[0]
        old.last_error = "original error"
        old.result_json = '{"original finding": true}'
        db.add(ScanState(user_id="ariel", ticker="BBB", status="active", last_fleet_at=now,
                         fleet_json='{"verdict":"PASS","conviction":"LOW"}'))
        db.commit()
        sheet.pending_research = [pending(tickers=["AAA"])]
        sheet.candidate_comparisons = [SimpleNamespace(ticker="BBB", evidence_fresh_as_of=now,
                                                      research_verdict="PASS", research_conviction="LOW")]
        sync_sheet_research(db, sheet, "mixed")
        db.commit()
        assert [json.loads(row.payload_json)["tickers"] for row in pending_tasks(db, "ariel")] == [["AAA"]]
        sheet.pending_research = [pending()]
        sheet.candidate_comparisons = []
        sync_sheet_research(db, sheet, "reopened")
        db.commit()
        active = pending_tasks(db, "ariel")[0]
        history = json.loads(active.payload_json)["lifecycle_history"]
        assert any(row["source_fingerprint"] == "original" and row["last_error"] == "original error" for row in history)
        assert any("original finding" in (row["result_json"] or "") for row in history)


def test_zero_cash_pending_research_reaches_real_route_author(alembic_engine_at_head, monkeypatch):
    from argosy.api.routes import portfolio
    from argosy.config import get_settings
    from argosy.services.allocation_author import packet_assembly, reliable
    from argosy.services.allocation_author.flow import AuthorOutcome
    from tests.test_deployment_canonical import _doc_with
    doc = _doc_with({"Core": [("CSPX", "IE")]})
    monkeypatch.setattr(portfolio, "_load_current_doc_and_holdings", lambda *a: (doc, {"CSPX": 100000}, 0))
    monkeypatch.setattr(get_settings(), "deployment_author_enabled", True)
    monkeypatch.setattr(get_settings(), "deployment_funnel_enabled", False)
    monkeypatch.setattr(packet_assembly, "assemble_author_packet", lambda *a, **k: {"deployable_usd": k["deployable_usd"]})
    calls = []
    def author(packet, **kwargs):
        calls.append(packet["deployable_usd"])
        return AuthorOutcome(status="unavailable")
    monkeypatch.setattr(reliable, "authored_allocation", author)
    with Session(alembic_engine_at_head) as db:
        sync_sheet_research(db, SimpleNamespace(user_id="ariel", candidate_comparisons=[], pending_research=[pending()]), "sheet")
        db.commit()
        portfolio.get_deploy_cash(cash_usd=0, user_id="ariel", live=False, sleeve_pct=5,
            use_high_potential=False, fleet_review=False, include_order_sheet=True,
            allow_sells=False, horizon_years_min=1, horizon_years_max=5, db=db)
        assert calls == [0.0]
