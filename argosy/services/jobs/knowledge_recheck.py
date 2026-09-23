"""Bounded daily knowledge repair, separate from the historical annual run."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from argosy.agents.domain_refresh import DomainRefreshAgent, DomainRefreshReport, write_back_refresh_results
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.annual import (
    _default_files_provider, _persist_refresh_report, _research_documents, _surface_refresh_discrepancies,
)
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.knowledge_status import collect_knowledge_status, document_key
from argosy.state import db as db_mod
from argosy.state.models import JobRun


def select_rechecks(documents, *, now, attempts, requested=(), failed=(), limit=2):
    """Only cadence/coverage selection. The fleet decides materiality and ownership."""
    selected = []
    for doc in documents:
        key = doc["path"]
        if requested:
            if key in requested:
                selected.append(doc)
            continue
        if doc["state"] == "verified" and key not in failed:
            continue
        last = attempts.get(key)
        if last and now - last < timedelta(days=1):
            continue
        findings = doc["findings"] if doc["input_matches"] and key not in failed else []
        if findings and not any(f["owner"] == "argosy" and f.get("retry_after")
                                and f["retry_after"] <= now.date().isoformat() for f in findings):
            continue
        selected.append(doc)
    return sorted(selected, key=lambda d: (attempts.get(d["path"], datetime.min.replace(tzinfo=UTC)),
                                           d["checked_at"] or "", d["path"]))[:limit]


def recheck_history(history):
    """Carry per-document failures across empty/other-document runs indefinitely."""
    attempts, unresolved = {}, {}
    for started, value in reversed(history):
        try:
            summary = json.loads(value or "{}")
            if isinstance(summary.get("unresolved_errors"), dict):
                unresolved = dict(summary["unresolved_errors"])
            for result in summary.get("documents", []):
                attempts[result["path"]] = started.replace(tzinfo=UTC)
                if result.get("error"):
                    unresolved[result["path"]] = result["error"]
                elif result.get("verification"):
                    unresolved.pop(result["path"], None)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return attempts, unresolved


class KnowledgeRecheckJob(CadenceLoop):
    name = "knowledge_recheck"

    def __init__(self, *, user_id="ariel", enabled=True, documents=()):
        super().__init__(schedule=LoopSchedule(cron="15 6 * * *"), enabled=enabled)
        self.user_id = user_id
        self.documents = tuple(document_key(p) for p in documents)
        if len(self.documents) > 2:
            raise ValueError("At most two documents per bounded recheck")
        self.last_output_summary = None

    async def tick(self, *, now=None):
        self.last_output_summary = None
        async with db_mod.get_session() as session:
            history = (await session.execute(select(JobRun.started_at, JobRun.output_summary)
                .where(JobRun.job_name == self.name).order_by(JobRun.id.desc()).limit(100))).all()
        attempts, unresolved = recheck_history(history)
        self.last_output_summary = {"documents": [], "unresolved_errors": unresolved}
        def deferred(status, reason):
            self.last_output_summary.update(status="error" if unresolved else status,
                reason=reason, error_count=len(unresolved))
            return self.last_output_summary
        if os.environ.get("ARGOSY_KILL") == "1":
            return deferred("skipped", "kill_switch")
        if await get_cost_guard(user_id=self.user_id).should_pause_non_routine(loop_name=self.name):
            return deferred("paused", "cost_guard")
        moment = (now or (lambda: datetime.now(UTC)))()
        files = _default_files_provider()
        async with db_mod.get_session() as session:
            # Annual and targeted repair share KB writeback: never run together.
            active = (await session.execute(select(JobRun.id).where(
                JobRun.job_name == "annual", JobRun.status == "running"))).scalars().all()
            if active:
                return deferred("skipped", "annual_in_progress")
            state = await collect_knowledge_status(session, user_id=self.user_id, files=files, now=moment)
        known = {d["path"] for d in state["documents"]}
        if set(self.documents) - known:
            raise ValueError("Unknown knowledge document requested")
        selected = select_rechecks(state["documents"], now=moment, attempts=attempts,
                                   requested=self.documents, failed=unresolved)
        keys = {d["path"] for d in selected}
        items = [f for f in files if document_key(f["path"]) in keys]
        results = []
        self.last_output_summary.update(documents=results, selected=sorted(keys))
        # Checkpoint pending work BEFORE entering the fleet. A killed process or
        # cancellation after report persistence must not leave an old green
        # review hiding an unfinished attempt.
        for key in keys:
            unresolved[key] = "Verification attempt did not finish."
        if keys:
            await self._checkpoint()
        async for item, outcome in _research_documents(items, lambda: DomainRefreshAgent(user_id=self.user_id)):
            result = {"path": document_key(item["path"])}
            try:
                if isinstance(outcome, Exception):
                    raise outcome
                report_id = await _persist_refresh_report(outcome, item=item)
                result["report_id"] = report_id
                outputs = outcome.output.per_file
                if len(outputs) != 1 or outputs[0].path.replace("\\", "/") != item["path"].replace("\\", "/"):
                    raise ValueError("Refresh report must cover exactly the requested document")
                review = outputs[0]
                if review.verification == "verified" and review.findings:
                    raise ValueError("Verified result contains unresolved material findings")
                if item.get("dependencies_stable") is False:
                    raise ValueError("Cited local evidence changed during recheck")
                from argosy.config import get_settings
                writeback = write_back_refresh_results(DomainRefreshReport(per_file=[review]),
                    root=get_settings().domain_knowledge_dir,
                    source_hashes={item["path"].replace("\\", "/"): item["content_sha256"]})
                result.update(verification=review.verification, writeback=writeback)
                if writeback["missing"] or writeback["changed_since_review"]:
                    raise ValueError("Knowledge document missing or changed during recheck")
                await _surface_refresh_discrepancies(user_id=self.user_id,
                    output=DomainRefreshReport(per_file=[review]), now=moment, document_scope=result["path"])
                unresolved.pop(result["path"], None)
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                unresolved[result["path"]] = result["error"]
            results.append(result)
            await self._checkpoint()
        errors = len(unresolved)
        if not errors:
            from argosy.services.knowledge_notice_closure import reconcile_knowledge_notices
            self.last_output_summary['notice_closures'] = await reconcile_knowledge_notices(user_id=self.user_id)
        self.last_output_summary.update(status="error" if errors else "ok", error_count=errors,
                                       unresolved_errors=unresolved, reviewed=len(results),
                                       note="Evidence gaps remain in current knowledge status; this is execution status only.")
        return self.last_output_summary

    async def _checkpoint(self):
        async with db_mod.get_session() as session:
            rows = (await session.execute(select(JobRun).where(
                JobRun.job_name == self.name, JobRun.status == "running"))).scalars().all()
            if len(rows) != 1:
                raise RuntimeError("Knowledge repair checkpoint requires exactly one registered running receipt")
            rows[0].output_summary = json.dumps(self.last_output_summary)
            await session.commit()


def knowledge_recheck_metadata():
    from argosy.services.jobs import JobMetadata
    return JobMetadata(name="knowledge_recheck", schedule_cron="15 6 * * *",
        schedule_human="Daily 06:15 Jerusalem; up to two due documents",
        source_kind="monitor", description="Targeted knowledge verification; no trades or automatic claim edits",
        long_running=False)
