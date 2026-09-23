"""Read-only operational readiness from durable execution receipts.

This is not an investment-quality verdict. No model calls on a health GET.
The registry is currently single-user, like its underlying job_runs table.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from argosy.services.jobs.summary_status import derive_run_status
from argosy.state.models import FunnelRun, FunnelStageRow, JobRun, NewsSignal, UserContext

# Only analysis, intake and maintenance jobs may be retried automatically. Never execution,
# transfers, proposal approval, or an entire plan-regeneration pipeline.
RECOVERABLE_JOBS = (
    "news_daily", "state_observer_daily", "holdings_review", "discovery_funnel",
    "watchlist", "thesis_monitor_daily", "decision_funnel",
    "earnings_calendar_daily", "sec_earnings_daily", "signal_streams_daily",
    "period_directive_daily",  # authors proposals only; never approves or executes
    "verdict_trigger_daily",  # idempotent note-only inbox unlocks; no fleet/trades
    "youtube_subscriptions",  # durable intake cursors and per-item retry/budget limits
    "job_runs_retention",  # idempotent maintenance, protects live/unfinished receipts
)
RETRY_DELAY = timedelta(minutes=30)
MAX_SLOT_ATTEMPTS = 3  # original attempt plus two recoveries, persisted across restarts


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def managed_settings_failure(message: str | None) -> bool:
    return "remote managed settings" in (message or "").lower()


def job_has_managed_settings_failure(row: JobRun) -> bool:
    # Summary-derived failures have a generic scheduler error_message; the
    # provider diagnosis remains inside output_summary.errors/stage_errors.
    return managed_settings_failure(row.error_message) or managed_settings_failure(row.output_summary)


def network_resolution_failure(message: str | None) -> bool:
    """Recognize recorded DNS failures, not a live connectivity assessment."""
    text = (message or "").lower()
    return any(marker in text for marker in (
        "could not resolve host", "could not resolve proxy", "getaddrinfo failed",
        "temporary failure in name resolution", "name or service not known",
        "no such host is known", "nodename nor servname provided",
    ))


def job_has_network_resolution_failure(row: JobRun) -> bool:
    return network_resolution_failure(row.error_message) or network_resolution_failure(row.output_summary)


def effective_status(row: JobRun, failed_funnel_ids: frozenset[int] = frozenset()) -> str:
    if row.status != "ok":
        return row.status
    try:
        summary = json.loads(row.output_summary or "{}")
    except (ValueError, TypeError):
        return "error"
    if row.job_name == "decision_funnel" and isinstance(summary, dict) and summary.get("run_id") in failed_funnel_ids:
        return "error"
    if isinstance(summary, dict) and summary.get("status") in ("paused", "skipped", "disabled"):
        return summary["status"]
    return derive_run_status(summary)[0]


async def legacy_failed_funnels(session, user_id: str | None) -> frozenset[int]:
    """Validate pre-fix success receipts against their actual stage errors."""
    failed_ids = select(FunnelStageRow.run_id).where(
        FunnelStageRow.decision.in_(("triage_error", "deep_decision_error", "error"))
    )
    stmt = select(FunnelRun).where(FunnelRun.id.in_(failed_ids))
    if user_id is not None:
        stmt = stmt.where(FunnelRun.user_id == user_id)
    rows = (await session.execute(stmt)).scalars()
    # New totals aggregate failures for the current attempt; older appended
    # stage errors must not invalidate a later successful retry of that run.
    return frozenset(r.id for r in rows if "error_count" not in json.loads(r.totals_json or "{}"))


async def latest_job_runs(session, names=RECOVERABLE_JOBS) -> list[JobRun]:
    latest = select(func.max(JobRun.id)).group_by(JobRun.job_name)
    if names is not None:
        latest = latest.where(JobRun.job_name.in_(names))
    return list((await session.execute(select(JobRun).where(JobRun.id.in_(latest)))).scalars())


def failure_guidance(row: JobRun) -> dict:
    """Public operational guidance; never publish raw stderr/prompts/credentials."""
    message = f"{row.error_message or ''} {row.output_summary or ''}".lower()
    if getattr(row, "job_name", None) == "period_directive_daily":
        try:
            summary = json.loads(row.output_summary or "{}")
            # Inspect failures, not investment prose mentioning a financing budget.
            message = f"{row.error_message or ''} {json.dumps(summary.get('research', {}).get('failures', []))} {json.dumps(summary.get('artifact_failures', []))}".lower()
        except (ValueError, TypeError, AttributeError):
            pass
        # Blocking provider causes outrank transient causes in mixed failures.
        if any(s in message for s in ("authentication_error", "oauth token", "not logged in", "please log in", "please run /login", "invalid api key")):
            return {"code": "authentication", "reason": "Model authentication failed.",
                    "action": "Sign in to Claude again if using CLI, or check the configured API credential; then retry the failed job."}
        if managed_settings_failure(message):
            return {"code": "claude_sign_in", "reason": "Claude could not load required managed settings.",
                    "action": "Sign in to Claude again, then retry the failed job from Job history."}
        if any(s in message for s in ("budget", "costguard", "spend cap", "insufficient credit")):
            return {"code": "budget", "reason": "A spending limit or insufficient credit stopped this job.",
                    "action": "Check the budget and provider balance in settings before retrying."}
    if "smtp_not_configured" in message:
        return {"code": "email_not_configured", "reason": "The digest was generated, but email delivery is not configured.",
                "action": "Configure SMTP if you want weekly email, or disable the email schedule and use the in-app reports. Retrying alone will not fix this."}
    if "error_max_turns" in message:
        return {"code": "model_turn_limit", "reason": "The research agent exhausted its tool-turn budget before finishing.",
                "action": "Check the per-document work size and configured turn budget. Retrying the same oversized request does not resolve this."}
    if "verification_incomplete" in message:
        return {"code": "knowledge_verification_incomplete", "reason": "The knowledge refresh finished with documents it could not fully verify.",
                "action": "Open the annual job details for per-document source gaps. Incomplete documents keep their previous verification dates; repair source access before retrying."}
    if network_resolution_failure(message):
        return {"code": "network_dns", "reason": "Network/DNS lookup failed during this run; the data source could not be reached.",
                "action": "Once connectivity returns, eligible analysis jobs retry automatically after a 30-minute cooldown, up to three attempts per scheduled slot. A successful rerun clears this failure; persistent failures need a network/DNS check."}
    if managed_settings_failure(message):
        return {"code": "claude_sign_in", "reason": "Claude could not load required managed settings.",
                "action": "Sign in to Claude again, then retry the failed job from Job history. If it still fails, check organization settings or network access."}
    if any(s in message for s in ("authentication_error", "oauth token", "not logged in", "please log in", "please run /login", "invalid api key")):
        return {"code": "authentication", "reason": "Model authentication failed.",
                "action": "Sign in to Claude again if using CLI, or check the configured API credential; then retry the failed job."}
    if any(s in message for s in ("budget", "costguard", "spend cap", "insufficient credit")):
        return {"code": "budget", "reason": "A spending limit or insufficient credit stopped this job.",
                "action": "Check the budget and provider balance in settings before retrying."}
    if any(s in message for s in ("rate_limit", "rate limit", "429")):
        return {"code": "rate_limit", "reason": "The provider rate limit was reached.",
                "action": "Wait for the provider limit to reset, then check Job history for recovery or retry."}
    if "database is locked" in message or "database table is locked" in message:
        return {"code": "database_busy", "reason": "A local database write was blocked by another transaction.",
                "action": "Eligible analysis jobs retry within their normal limits. Repeated failures require checking transaction ownership; do not clear the failure without a successful rerun."}
    if any(s in message for s in ("timeout", "timed out")):
        return {"code": "timeout", "reason": "The job timed out before completion.",
                "action": "Check Job history for recovery or retry; repeated timeouts need investigation."}
    if "domain_refresh" in message and "exit code 1" in message:
        return {"code": "knowledge_refresh_runtime", "reason": "The tax/rules knowledge-refresh agent exited unsuccessfully; the CLI did not provide a useful cause.",
                "action": "Investigate the knowledge-refresh runtime before retrying. This does not establish an authentication failure; the last successful verification remains dated."}
    if getattr(row, "job_name", None) == "period_directive_daily":
        try:
            summary = json.loads(row.output_summary or "{}")
            if summary.get("research", {}).get("failures"):
                return {"code": "allocation_research_failed",
                        "reason": "Allocation research did not finish; this is separate from trade-plan generation.",
                        "action": "Argosy retries eligible research with saved progress and bounded attempts. If retries are exhausted, inspect the research failure; no trade approval is required."}
        except (ValueError, TypeError, AttributeError):
            pass
    return {"code": "job_failed", "reason": "The job failed or reported incomplete work.",
            "action": "Open Job history for the recorded error and retry after the cause is resolved."}


async def collect_decision_readiness(session, *, user_id: str, now: datetime | None = None) -> dict:
    now = utc(now or datetime.now(UTC))
    from argosy.config import get_settings
    disabled_jobs = set()
    if not get_settings().discord_listener_enabled:
        disabled_jobs.add("discord_listener")
    from argosy.agent_settings import load_agent_settings
    if not load_agent_settings(user_id, create_if_missing=False).cadences.weekly_email_digest.enabled:
        disabled_jobs.add("weekly_email_digest")
    latest = {r.job_name: r for r in await latest_job_runs(session, names=None)}
    knowledge = None
    if "annual" in latest:
        from argosy.services.knowledge_status import collect_knowledge_status
        try:
            knowledge = await collect_knowledge_status(session, user_id=user_id, now=now)
        except Exception:
            # A failed status read is not evidence of recovery.
            knowledge = {"status": "unavailable", "documents": [],
                         "reason": "Current knowledge evidence could not be read."}
    failed_funnel_ids = await legacy_failed_funnels(session, user_id)
    urgent_findings = []
    if knowledge:
        import yaml
        context = await session.get(UserContext, user_id)
        try:
            preferences = yaml.safe_load(context.constraints_yaml or '') if context else {}
            policy = (preferences or {}).get('knowledge_followups', {})
            due = date.fromisoformat(policy['paperwork_due_date']).isoformat()
        except (ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
            due = None
        knowledge['paperwork_due_date'] = due
        urgent_findings = [{**finding, 'path': doc['path']}
            for doc in knowledge.get('documents', []) if doc.get('input_matches')
            for finding in doc.get('findings', []) if finding.get('urgency') == 'urgent']
    # Surface other scheduled-job failures too (e.g. self-evaluation), without
    # expanding the automatic-retry allowlist or daily freshness requirements.
    extra_failures = sorted(name for name, row in latest.items()
                            if name not in RECOVERABLE_JOBS and effective_status(row, failed_funnel_ids) in {"error", "running", "cancelled"})
    monitored_names = tuple(dict.fromkeys((*RECOVERABLE_JOBS, *extra_failures,
                                          *(["annual"] if "annual" in latest else []))))
    successes = {}
    history = (await session.execute(
        select(JobRun).where(JobRun.job_name.in_(monitored_names), JobRun.status == "ok")
        .order_by(JobRun.id.desc())
    )).scalars()
    for row in history:
        if row.job_name not in successes and effective_status(row, failed_funnel_ids) == "ok" and row.finished_at:
            successes[row.job_name] = row.finished_at
        if len(successes) == len(monitored_names):
            break
    jobs = []
    for name in monitored_names:
        row = latest.get(name)
        last_ok = successes.get(name)
        state = effective_status(row, failed_funnel_ids) if row else "never_run"
        recorded_status = state
        guidance = failure_guidance(row) if row and state == "error" else None
        if name == "period_directive_daily" and guidance and guidance["code"] in {
            "allocation_research_failed", "timeout", "network_dns", "rate_limit", "database_busy",
        }:
            from argosy.orchestrator.loops.base import LoopSchedule
            from argosy.services.jobs.period_directive_daily import _DEFAULT_CRON, _DEFAULT_TZ
            slot = LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ).prev_due_before(now)
            slot_attempts = (await session.execute(select(func.count()).select_from(JobRun).where(
                JobRun.job_name == name, JobRun.started_at >= slot,
            ))).scalar_one() if slot else MAX_SLOT_ATTEMPTS
            summary = json.loads(row.output_summary or "{}")
            retries = summary.get("research", {}).get("recovery", [])
            # Amber only while bounded recovery is actually eligible. Never hide
            # auth/budget failures, an invalid artifact, or a stalled recovery loop.
            if (slot and utc(row.started_at) >= slot and slot_attempts < MAX_SLOT_ATTEMPTS
                    and summary.get("order_sheet_fingerprint") and summary.get("proposal_id")
                    and summary.get("materialization_status") == "awaiting_unified_approval"
                    and summary.get("research", {}).get("failures")
                    and retries and all(r.get("automatic_retry") and r.get("next_retry_at")
                        and now <= utc(datetime.fromisoformat(r["next_retry_at"])) + timedelta(hours=2)
                        for r in retries)):
                state = "research_recovery_pending"
                guidance = {"code": "allocation_research_recovering",
                            "reason": "Trade plan generated; separate allocation research is awaiting automatic recovery. " + guidance["reason"],
                            "action": "Argosy will retry the unfinished research. No trade approval or paperwork is needed for recovery."}
        if name in disabled_jobs:
            state = "disabled"
        # Daily jobs allow a weekend/day-boundary buffer, not weeks of stale
        # evidence. Last success remains visible even during an in-flight run.
        if name in RECOVERABLE_JOBS and state == "ok" and (last_ok is None or now - utc(last_ok) > timedelta(hours=48)):
            state = "stale"
        if name == "annual" and state == "ok" and knowledge:
            state = ("knowledge_recovered" if knowledge["status"] == "verified" else
                     "knowledge_unavailable" if knowledge["status"] == "unavailable" else "knowledge_incomplete")
        if name == "annual" and row and state == "error":
            try:
                summary = json.loads(row.output_summary or "{}")
                evidence_only = (str(summary.get("domain_refresh_error", "")).startswith("verification_incomplete:")
                    and not summary.get("pension_refresh_error")
                    and bool(summary.get("domain_refresh_files"))
                    and not any(f.get("error") for f in summary["domain_refresh_files"]))
            except (ValueError, TypeError, AttributeError):
                evidence_only = False
            if evidence_only and knowledge and knowledge["status"] != "unavailable":
                state = "knowledge_recovered" if knowledge["status"] == "verified" else "knowledge_incomplete"
        jobs.append({
            "name": name, "status": state,
            "recorded_status": recorded_status,
            "attention_required": state not in {"ok", "disabled", "knowledge_recovered", "knowledge_incomplete"},
            "run_id": row.id if row else None,
            "last_attempt_at": utc(row.started_at).isoformat() if row else None,
            "last_success_at": utc(last_ok).isoformat() if last_ok else None,
            "failure": guidance if state in {"error", "research_recovery_pending"} else None,
        })
    pending, oldest = (await session.execute(
        select(func.count(), func.min(NewsSignal.received_at)).where(NewsSignal.analyzed_at.is_(None))
    )).one()
    blocked = any(j["failure"] and j["failure"]["code"] in ("claude_sign_in", "authentication") for j in jobs)
    issues = [j["name"] for j in jobs if j["attention_required"]]
    status = "blocked" if blocked or urgent_findings else "degraded" if issues or pending else "ready"
    message = (
        "The last recorded jobs hit a Claude/model sign-in or settings failure. If already signed in again, retry failed jobs to verify recovery. Daily analysis is incomplete; current recommendations are not verified."
        if blocked else
        "A critical finding needs attention now. See its specific consequence and next action."
        if urgent_findings else
        "Some analysis or knowledge checks are incomplete or stale. This is not a no-action recommendation."
        if issues or pending else
        "Daily analysis jobs are current. This does not certify a trade plan or investment performance."
    )
    return {
        "status": status, "checked_at": now.isoformat(), "message": message,
        "jobs": jobs, "pending_news": pending,
        "knowledge": knowledge,
        "urgent_findings": urgent_findings,
        "oldest_pending_news_at": utc(oldest).isoformat() if oldest else None,
        "recovery": {"delay_minutes": 30, "max_attempts_per_slot": MAX_SLOT_ATTEMPTS},
    }
