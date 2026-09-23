from datetime import UTC, datetime, timedelta
import json

import pytest

from argosy.services.knowledge_status import collect_knowledge_status, input_fingerprint
from argosy.services.jobs.knowledge_recheck import select_rechecks, KnowledgeRecheckJob
from argosy.state import db as db_mod
from argosy.state.models import AgentReport, AgentReportBlob, User


def item(content="The current rule"):
    return {"path": "domain_knowledge/rule.md", "frontmatter": "last_verified: 2020-01-01\ntopic: test",
            "content": content}


async def save(session, *, user="ariel", fingerprint=None, verified=True, due="2027-01-01", findings=()):
    await session.merge(User(id=user))
    row = AgentReport(user_id=user, agent_role="domain_refresh", created_at=datetime(2026, 9, 13, tzinfo=UTC))
    session.add(row)
    await session.flush()
    session.add(AgentReportBlob(report_id=row.id, key="output_json", value=json.dumps({"per_file": [{
        "path": "domain_knowledge/rule.md", "status": "no_change", "verification": "verified" if verified else "partial",
        "next_refresh_due": due, "note": "review", "findings": list(findings),
        "evidence": [{"url": "https://example.org/source", "retrieved_at": "2026-09-13"}],
    }]})))
    if fingerprint:
        session.add(AgentReportBlob(report_id=row.id, key="knowledge_input", value=json.dumps({
            "path": "rule.md", "fingerprint": fingerprint, "coverage_valid": True})))
    await session.commit()
    return row.id


def test_fingerprint_ignores_only_verification_bookkeeping():
    original = item()
    changed = {**original, "frontmatter": "last_verified: 2026-09-13\nnext_refresh_due: 2027-01-01\ntopic: test"}
    assert input_fingerprint(original) == input_fingerprint(changed)
    assert input_fingerprint(original) != input_fingerprint(item("A different rule"))


def test_production_registry_accepts_knowledge_job_metadata():
    from argosy.services.jobs import JobRegistry, RegisteredScheduler
    from argosy.services.jobs.knowledge_recheck import knowledge_recheck_metadata
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry)
    registry.bind_scheduler(scheduler)
    job = KnowledgeRecheckJob()
    scheduler.register_loop(job)
    registry.register(job=job, metadata=knowledge_recheck_metadata())


@pytest.mark.asyncio
async def test_current_projection_preserves_legacy_and_checks_content_user_and_expiry(engine):
    now = datetime(2026, 9, 13, tzinfo=UTC)
    async with db_mod.get_session() as session:
        legacy = await save(session)
        state = await collect_knowledge_status(session, user_id="ariel", files=[item()], now=now)
        assert state["reported_verified"] == 1 and state["verified"] == 0
        assert state["documents"][0]["report_id"] == legacy
        current = await save(session, fingerprint=input_fingerprint(item()))
        await save(session, user="someone_else", verified=False)
        state = await collect_knowledge_status(session, user_id="ariel", files=[item()], now=now)
        assert state["verified"] == 1 and state["documents"][0]["report_id"] == current
        state = await collect_knowledge_status(session, user_id="ariel", files=[item("Changed")], now=now)
        assert state["verified"] == 0
        state = await collect_knowledge_status(session, user_id="ariel", files=[item()], now=now + timedelta(days=200))
        assert state["verified"] == 0


@pytest.mark.asyncio
async def test_new_partial_supersedes_verified_without_deleting_history(engine):
    async with db_mod.get_session() as session:
        old = await save(session, fingerprint=input_fingerprint(item()))
        newer = await save(session, fingerprint=input_fingerprint(item()), verified=False)
        state = await collect_knowledge_status(session, user_id="ariel", files=[item()], now=datetime(2026,9,13,tzinfo=UTC))
        assert state["verified"] == 0 and state["documents"][0]["report_id"] == newer
        assert await session.get(AgentReport, old) is not None


@pytest.mark.asyncio
async def test_invalid_latest_report_does_not_fall_back_to_old_green(engine):
    from sqlalchemy import select
    async with db_mod.get_session() as session:
        await save(session, fingerprint=input_fingerprint(item()))
        newer = await save(session, fingerprint=input_fingerprint(item()))
        blob = (await session.execute(select(AgentReportBlob).where(
            AgentReportBlob.report_id == newer, AgentReportBlob.key == "output_json"))).scalar_one()
        blob.value = '{"per_file": []}'
        await session.commit()
        state = await collect_knowledge_status(session, user_id="ariel", files=[item()], now=datetime(2026,9,13,tzinfo=UTC))
        assert state["verified"] == 0 and state["documents"][0]["report_id"] == newer


@pytest.mark.asyncio
async def test_current_knowledge_remains_visible_after_successful_annual(engine, monkeypatch):
    from uuid import uuid4
    from argosy.services.decision_readiness import collect_decision_readiness
    from argosy.state.models import JobRun
    from argosy.services import knowledge_status
    async def incomplete(*args, **kwargs):
        return {"status": "incomplete", "documents": []}
    monkeypatch.setattr(knowledge_status, "collect_knowledge_status", incomplete)
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        session.add(JobRun(job_name="annual", status="ok", started_at=now, finished_at=now,
                           output_summary='{}', idempotency_key=uuid4().hex))
        await session.commit()
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
        annual = next(j for j in state["jobs"] if j["name"] == "annual")
        assert annual["status"] == "knowledge_incomplete" and not annual["attention_required"]
        assert annual["recorded_status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_error", [False, True])
async def test_evidence_only_annual_is_not_presented_as_crash_but_runtime_error_is(engine, monkeypatch, runtime_error):
    from uuid import uuid4
    from argosy.services.decision_readiness import collect_decision_readiness
    from argosy.state.models import JobRun
    from argosy.services import knowledge_status
    async def incomplete(*args, **kwargs):
        return {"status": "incomplete", "documents": []}
    monkeypatch.setattr(knowledge_status, "collect_knowledge_status", incomplete)
    now = datetime.now(UTC)
    files = [{"path": "rule.md", "verification": "partial"}]
    if runtime_error:
        files[0]["error"] = "Agent invocation failed"
    summary = {"domain_refresh_error": "verification_incomplete: test", "domain_refresh_files": files}
    async with db_mod.get_session() as session:
        row = JobRun(job_name="annual", status="error", started_at=now, finished_at=now,
                     output_summary=json.dumps(summary), idempotency_key=uuid4().hex)
        session.add(row)
        await session.commit()
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
        annual = next(j for j in state["jobs"] if j["name"] == "annual")
        assert annual["status"] == ("error" if runtime_error else "knowledge_incomplete")
        assert annual["attention_required"] is runtime_error
        assert row.status == "error"


def test_retry_waits_for_private_evidence_but_changed_input_is_rechecked():
    now = datetime(2026,9,13,tzinfo=UTC)
    doc = {"path": "rule.md", "state": "incomplete", "input_matches": True, "checked_at": None,
           "findings": [{"owner": "user", "retry_after": None}]}
    assert select_rechecks([doc], now=now, attempts={}) == []
    changed = {**doc, "input_matches": False}
    assert select_rechecks([changed], now=now, attempts={}) == [changed]
    assert select_rechecks([changed], now=now, attempts={"rule.md": now}) == []
    assert select_rechecks([doc], now=now, attempts={}, requested=["rule.md"]) == [doc]


def test_retry_respects_fleet_date_and_bounded_fairness():
    now = datetime(2026,9,13,tzinfo=UTC)
    doc = {"path": "rule.md", "state": "incomplete", "input_matches": True, "checked_at": None,
           "findings": [{"owner": "argosy", "retry_after": "2026-09-14"}]}
    assert not select_rechecks([doc], now=now, attempts={})
    assert select_rechecks([doc], now=now+timedelta(days=1), attempts={}) == [doc]
    docs = [{**doc, "path": p, "findings": []} for p in ("a", "b", "c")]
    selected = select_rechecks(docs, now=now, attempts={"a": now-timedelta(days=2)})
    assert [d["path"] for d in selected] == ["b", "c"]


def test_unresolved_runtime_failure_survives_empty_run_and_verified_document():
    from argosy.services.jobs.knowledge_recheck import recheck_history
    now = datetime(2026,9,13,tzinfo=UTC)
    attempts, errors = recheck_history([(now, json.dumps({"documents": [], "unresolved_errors": {"rule.md": "timeout"}}))])
    assert errors == {"rule.md": "timeout"}
    doc = {"path": "rule.md", "state": "verified", "input_matches": True, "checked_at": None, "findings": []}
    assert select_rechecks([doc], now=now, attempts=attempts, failed=errors) == [doc]
    _, errors = recheck_history([(now, json.dumps({"documents": [], "unresolved_errors": {}})),
        (now-timedelta(days=1), json.dumps({"documents": [{"path": "rule.md", "error": "old failure"}]}))])
    assert errors == {}


@pytest.mark.asyncio
async def test_recheck_accepts_registered_clock_and_honors_kill_switch(engine, monkeypatch):
    monkeypatch.setenv("ARGOSY_KILL", "1")
    result = await KnowledgeRecheckJob().tick(now=lambda: datetime(2026,9,13,tzinfo=UTC))
    assert result["status"] == "skipped"


def test_interrupted_newer_error_overrides_older_empty_failure_snapshot():
    from argosy.services.jobs.knowledge_recheck import recheck_history
    now = datetime.now(UTC)
    _, errors = recheck_history([
        (now, json.dumps({"documents": [{"path": "rule.md", "error": "new timeout"}]})),
        (now-timedelta(days=1), json.dumps({"documents": [], "unresolved_errors": {}})),
    ])
    assert errors == {"rule.md": "new timeout"}


@pytest.mark.asyncio
async def test_kill_switch_does_not_clear_unresolved_runtime_error(engine, monkeypatch):
    from argosy.state.models import JobRun
    from uuid import uuid4
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        session.add(JobRun(job_name="knowledge_recheck", status="error", started_at=now, finished_at=now,
            idempotency_key=uuid4().hex, output_summary=json.dumps({"unresolved_errors": {"rule.md": "timeout"}})))
        await session.commit()
    monkeypatch.setenv("ARGOSY_KILL", "1")
    result = await KnowledgeRecheckJob().tick()
    assert result["status"] == "error" and result["unresolved_errors"] == {"rule.md": "timeout"}


@pytest.mark.asyncio
async def test_local_dependency_change_invalidates_evidence(engine, tmp_path, monkeypatch):
    from argosy.services.knowledge_status import dependency_versions
    from sqlalchemy import select
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(tmp_path))
    source = tmp_path / "declaration.txt"
    source.write_text("Old declaration", encoding="utf-8")
    target = item()
    target["frontmatter"] += "\nsources:\n  - url: file://Resources/declaration.txt"
    async with db_mod.get_session() as session:
        rid = await save(session, fingerprint=input_fingerprint(target))
        proof = (await session.execute(select(AgentReportBlob).where(
            AgentReportBlob.report_id == rid, AgentReportBlob.key == "knowledge_input"))).scalar_one()
        value = json.loads(proof.value)
        value["dependencies"] = await dependency_versions(session, user_id="ariel", item=target)
        proof.value = json.dumps(value)
        await session.commit()
        before = await collect_knowledge_status(session, user_id="ariel", files=[target], now=datetime(2026,9,13,tzinfo=UTC))
        assert before["verified"] == 1
        source.write_text("New declaration", encoding="utf-8")
        after = await collect_knowledge_status(session, user_id="ariel", files=[target], now=datetime(2026,9,13,tzinfo=UTC))
        assert after["verified"] == 0 and not after["documents"][0]["input_matches"]


@pytest.mark.asyncio
async def test_dependency_and_attachment_use_same_code_with_separate_data_home(engine, tmp_path, monkeypatch):
    from argosy.services import knowledge_status as service
    from argosy.orchestrator.loops.annual import _attach_local_sources
    from types import SimpleNamespace
    import argosy.config
    package = tmp_path / "package"
    (package / "argosy").mkdir(parents=True)
    code = package / "argosy" / "example.py"
    code.write_text("value = 1", encoding="utf-8")
    monkeypatch.setattr(service, "CODE_ROOT", package)
    monkeypatch.setattr(argosy.config, "get_settings", lambda: SimpleNamespace(home=tmp_path / "data"))
    target = {**item(), "frontmatter": "code_references:\n  - argosy/example.py"}
    prepared, _ = _attach_local_sources(target)
    async with db_mod.get_session() as session:
        versions = await service.dependency_versions(session, user_id="ariel", item=target)
        assert versions[0][1] == prepared["code_evidence"][0]["sha256"]
        code.write_text("value = 2", encoding="utf-8")
        assert versions != await service.dependency_versions(session, user_id="ariel", item=target)


@pytest.mark.asyncio
async def test_registered_pending_checkpoint_survives_interruption(engine):
    from argosy.state.models import JobRun
    from argosy.services.jobs.knowledge_recheck import recheck_history
    from uuid import uuid4
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        row = JobRun(job_name="knowledge_recheck", status="running", started_at=now,
                     idempotency_key=uuid4().hex)
        session.add(row)
        await session.commit()
        rid = row.id
    job = KnowledgeRecheckJob()
    job.last_output_summary = {"selected": ["rule.md"], "documents": [],
                              "unresolved_errors": {"rule.md": "Verification attempt did not finish."}}
    await job._checkpoint()
    async with db_mod.get_session() as session:
        saved = await session.get(JobRun, rid)
        _, errors = recheck_history([(saved.started_at, saved.output_summary)])
        assert errors == job.last_output_summary["unresolved_errors"]


def test_contradictory_verified_with_material_findings_cannot_stamp_file(tmp_path):
    from argosy.agents.domain_refresh import DomainRefreshReport, FileRefreshResult, write_back_refresh_results
    from datetime import date
    root = tmp_path / "domain_knowledge"
    root.mkdir()
    path = root / "rule.md"
    original = "---\nlast_verified: 2020-01-01\n---\nOriginal rule\n"
    path.write_text(original, encoding="utf-8")
    result = FileRefreshResult(path="domain_knowledge/rule.md", status="no_change", verification="verified",
        evidence=[{"url": "https://example.org/source", "retrieved_at": date.today().isoformat()}],
        findings=[{"scope": "public_rule", "claim": "Rule", "missing_evidence": "Primary source",
                   "owner": "argosy", "next_action": "Fetch primary", "affected_advice": "Tax estimate"}])
    output = write_back_refresh_results(DomainRefreshReport(per_file=[result]), root=root)
    assert output["unverified"] == ["domain_knowledge/rule.md"] and not output["updated"]
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_targeted_correction_proposals_do_not_overwrite_each_other(engine):
    from sqlalchemy import select
    from argosy.state.models import ActionProposal
    from argosy.orchestrator.loops.annual import _surface_refresh_discrepancies
    from argosy.agents.domain_refresh import DomainRefreshReport, FileRefreshResult
    async with db_mod.get_session() as session:
        await session.merge(User(id="ariel"))
        await session.commit()
    for path in ("a.md", "b.md"):
        output = DomainRefreshReport(per_file=[FileRefreshResult(path=path, status="change_proposed",
            verification="verified", diff="proposed correction", note=path)])
        await _surface_refresh_discrepancies(user_id="ariel", output=output,
                                            now=datetime.now(UTC), document_scope=path)
    async with db_mod.get_session() as session:
        rows = (await session.execute(select(ActionProposal).where(
            ActionProposal.dedup_key.like("domain_refresh_discrepancies:ariel:%")))).scalars().all()
        assert len(rows) == 2
        assert {json.loads(row.suggested_payload)["discrepancies"][0]["path"] for row in rows} == {"a.md", "b.md"}
