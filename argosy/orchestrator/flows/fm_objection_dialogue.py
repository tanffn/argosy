"""FM-objection ZigZag — slim FM ↔ analyst dialogue for ONE objection.

The Fund Manager's verdict on a plan-synthesis draft is a one-shot
judgment: it reads the draft and emits ``approved=false`` with a list
of objections. Until this flow shipped, the user's only options were:

  1. Accept the rejection wholesale (DEFER everything, lose the draft).
  2. Trigger a full re-synthesis (~$3-4 + ~30-70 min) for what may be
     a single localized disagreement.

The ZigZag is a slim middle path: per FM objection, fire ONE 3-turn
dialogue between the FM and the specific analyst(s) the FM is concerned
about. Total per-dialogue cost target: $0.20-0.50.

Three turns:
  1. (No LLM) — reformat the FM's objection text as a question.
  2. ``AnalystResponderAgent`` (Sonnet) — analyst responds with one of
     CONCEDE / REBUT / CLARIFY, structured as ``AnalystResponseToFM``.
  3. ``FundManagerDialogueVerdictAgent`` (Opus) — FM reads (1) + (2)
     and produces ``FMObjectionDialogueVerdict``: one of
     FM_ACCEPTS_ANALYST / FM_MAINTAINS_OBJECTION / FM_REVISES_OBJECTION
     / ESCALATE_TO_USER.

Persistence: one ``decision_runs`` row with
``decision_kind="fm_objection_dialogue"`` and ``notes_json`` carrying
``{objection_index, analyst_role, resolution, ...}``. The two LLM
agent_reports (analyst + FM) are persisted via the standard
JSONL-trail + negotiation-recorder paths so they appear in the
/decisions UI.

Idempotency: a dialogue keyed on
``(user_id, plan_version_id, objection_index, analyst_role)`` that is
in-flight (or completed within 5 min) returns the existing
``decision_run_id`` instead of starting a second run. The window
encompasses BOTH the in-flight window AND the post-completion grace
(per spec) — process-local, single-user system, single-instance.

Cost-cap: before dispatching, ``ARGOSY_SYNTHESIS_COST_CAP_USD``
(default $10) is enforced against the user's last-24h spend. If
headroom < ``ESTIMATED_RUN_COST_USD`` we refuse cleanly with
``CostCapExceededError``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.agents.base import AgentReport
from argosy.logging import get_logger
from argosy.state.models import (
    AgentReport as AgentReportORM,
    DecisionRun,
    PlanVersion,
)

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Public types
# ----------------------------------------------------------------------


Resolution = Literal[
    "FM_ACCEPTS_ANALYST",
    "FM_MAINTAINS_OBJECTION",
    "FM_REVISES_OBJECTION",
    "ESCALATE_TO_USER",
]


@dataclass
class DialogueOutcome:
    """Structured outcome of one FM↔analyst dialogue."""

    resolution: Resolution
    analyst_stance: Literal["CONCEDE", "REBUT", "CLARIFY"]
    analyst_reasoning_md: str = ""
    analyst_suggested_fix: str = ""
    fm_reasoning_md: str = ""
    updated_objection_text: str | None = None
    suggested_plan_amendment: str | None = None
    cited_sources: list[str] = field(default_factory=list)


class FMObjectionDialogueError(Exception):
    """Base error for the FM-objection ZigZag flow."""


class ObjectionNotFoundError(FMObjectionDialogueError):
    """The objection_index doesn't exist on the current draft's FM verdict."""


class CostCapExceededError(FMObjectionDialogueError):
    """Cumulative cost would cross ``$ARGOSY_SYNTHESIS_COST_CAP_USD``."""


class InvalidAnalystRoleError(FMObjectionDialogueError):
    """analyst_role not in the canonical map (or not referenced by this objection)."""


# ----------------------------------------------------------------------
# Idempotency: in-flight registry
# ----------------------------------------------------------------------
#
# Keyed by ``(user_id, plan_version_id, objection_index, analyst_role)``.
# Within ``IDEMPOTENCY_WINDOW_SECONDS`` (5 min per spec, longer than the
# per_delta_pushback registry because dialogues take ~30-60 s end-to-end
# whereas pushback is ~10-20 s) a second call returns the existing run.


IDEMPOTENCY_WINDOW_SECONDS = 300.0  # 5 minutes — spec

# Conservative per-dialogue cost budget. Real spend lands ~$0.10-0.30
# typically; we round up so a near-cap user gets a clean refusal
# rather than a $9.85 → $10.20 surprise.
ESTIMATED_RUN_COST_USD = 0.50

_in_flight_lock = threading.Lock()
_in_flight: dict[tuple[str, int, int, str], tuple[int, float]] = {}


def _idempotency_key(
    *, user_id: str, plan_version_id: int, objection_index: int, analyst_role: str,
) -> tuple[str, int, int, str]:
    return (user_id, plan_version_id, objection_index, analyst_role)


def _claim_inflight_or_get(
    *,
    user_id: str,
    plan_version_id: int,
    objection_index: int,
    analyst_role: str,
    decision_run_id: int,
) -> int | None:
    """Try to claim the in-flight slot. Returns existing id if window not elapsed."""
    key = _idempotency_key(
        user_id=user_id, plan_version_id=plan_version_id,
        objection_index=objection_index, analyst_role=analyst_role,
    )
    now = time.monotonic()
    with _in_flight_lock:
        existing = _in_flight.get(key)
        if existing is not None:
            run_id, started = existing
            if (now - started) <= IDEMPOTENCY_WINDOW_SECONDS:
                return run_id
        _in_flight[key] = (decision_run_id, now)
        return None


def _release_inflight(
    *, user_id: str, plan_version_id: int, objection_index: int, analyst_role: str,
) -> None:
    """Drop the registry entry when the flow finishes (success or fail)."""
    key = _idempotency_key(
        user_id=user_id, plan_version_id=plan_version_id,
        objection_index=objection_index, analyst_role=analyst_role,
    )
    with _in_flight_lock:
        _in_flight.pop(key, None)


def _peek_inflight(
    *, user_id: str, plan_version_id: int, objection_index: int, analyst_role: str,
) -> int | None:
    key = _idempotency_key(
        user_id=user_id, plan_version_id=plan_version_id,
        objection_index=objection_index, analyst_role=analyst_role,
    )
    now = time.monotonic()
    with _in_flight_lock:
        existing = _in_flight.get(key)
        if existing is None:
            return None
        run_id, started = existing
        if (now - started) <= IDEMPOTENCY_WINDOW_SECONDS:
            return run_id
        return None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def parse_agent_refs_from_objection(detail: str) -> list[str]:
    """Pull every ``agent_report:XAgent`` reference out of an FM objection.

    The FM cites prior analyst reports via the source-id convention
    ``agent_report:<AgentClassName>``. Parse those out so the API route
    knows which analyst dropdown options to render. Deduplicated in
    encounter order.

    The reference shape is intentionally narrow — only references to
    agent classes in the canonical map count. References to non-analyst
    agents (researcher, trader, risk officers, etc.) are filtered out
    since they don't have a ZigZag responder.
    """
    import re

    from argosy.agents.analyst_responder import ANALYST_AGENT_NAME_TO_ROLE

    if not detail:
        return []
    # Match "agent_report:<CapitalizedName>Agent" — capture the agent
    # class name. Tolerate optional trailing punctuation; the regex
    # consumes only the class name itself.
    pattern = re.compile(r"agent_report:([A-Z][A-Za-z]+Agent)")
    seen: dict[str, None] = {}
    for m in pattern.finditer(detail):
        name = m.group(1)
        if name in ANALYST_AGENT_NAME_TO_ROLE and name not in seen:
            seen[name] = None
    return list(seen.keys())


def _resolve_prior_agent_report(
    session: Session, *, user_id: str, decision_audit_token: str, agent_role: str,
) -> AgentReportORM | None:
    """Find the most recent agent_report for (user, decision_audit_token, role).

    Used to seed the analyst-responder prompt with the analyst's prior
    reasoning so it doesn't respond from cold. Returns None if no row
    exists — the agent can still respond from first principles in its
    domain; we tell it so in the prompt.
    """
    row = session.execute(
        select(AgentReportORM).where(
            AgentReportORM.user_id == user_id,
            AgentReportORM.decision_id == decision_audit_token,
            AgentReportORM.agent_role == agent_role,
        ).order_by(desc(AgentReportORM.created_at)).limit(1)
    ).scalar_one_or_none()
    return row


def _total_recent_cost_usd(session: Session, *, user_id: str) -> float:
    """Sum cost_usd across last-24h agent_reports for the user.

    Mirrors per_delta_pushback._total_recent_cost_usd for parity. Bounded
    24h look-back so a one-time spike doesn't permanently lock the cap.
    """
    try:
        from sqlalchemy import func

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        spent = session.execute(
            select(func.coalesce(func.sum(AgentReportORM.cost_usd), 0)).where(
                AgentReportORM.user_id == user_id,
                AgentReportORM.created_at >= cutoff,
            )
        ).scalar_one()
        return float(spent or 0.0)
    except Exception as exc:  # noqa: BLE001 — best effort
        log.warning(
            "fm_objection_dialogue.cost_lookup_failed",
            user_id=user_id, error=str(exc),
        )
        return 0.0


def _persist_agent_reports_jsonl(
    *, decision_audit_token: str, reports: list[AgentReport]
) -> None:
    """Append each AgentReport to the per-run JSONL trail.

    Mirrors plan_synthesis._persist_agent_reports for parity with the
    existing trail-ingest tooling.
    """
    if not reports:
        return
    from argosy.config import get_settings
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _agent_report_to_row_dict,
    )

    settings = get_settings()
    trail_dir = settings.home / "logs" / "synthesis"
    try:
        trail_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "fm_objection_dialogue.trail_dir_mkdir_failed", error=str(exc),
        )
        return
    trail_path = trail_dir / f"{decision_audit_token}.jsonl"
    try:
        with trail_path.open("a", encoding="utf-8") as f:
            for r in reports:
                row = _agent_report_to_row_dict(r)
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.warning(
            "fm_objection_dialogue.trail_write_failed",
            count=len(reports), error=str(exc),
        )


# ----------------------------------------------------------------------
# Slim flow
# ----------------------------------------------------------------------


# Cap on prior-agent-report excerpt size injected into the responder
# prompt. Some analyst reports (plan_critique especially) emit 20-30k+
# chars; truncating to 6k keeps the dialogue prompt well under the
# Sonnet context budget while preserving the lead 5-10 paragraphs which
# carry the load-bearing reasoning + cited sources.
_MAX_PRIOR_REPORT_EXCERPT_CHARS = 6000


def _truncate_prior_report(text: str) -> str:
    """Trim the prior report excerpt to a sane prompt size."""
    if not text:
        return ""
    if len(text) <= _MAX_PRIOR_REPORT_EXCERPT_CHARS:
        return text
    head = text[:_MAX_PRIOR_REPORT_EXCERPT_CHARS]
    return head + "\n\n... [excerpt truncated for prompt budget] ..."


def _run_dialogue(
    *,
    user_id: str,
    objection_topic: str,
    objection_detail: str,
    objection_severity: str,
    analyst_role: str,
    prior_decision_audit_token: str,
    prior_agent_report_excerpt: str,
    prior_agent_report_id: int | None,
    decision_audit_token: str,
    user_guidance: str = "",
) -> tuple[DialogueOutcome, list[AgentReport]]:
    """Run the analyst → FM 2-LLM-call dialogue.

    Returns (outcome, agent_reports). The caller persists the reports
    via the JSONL trail and the negotiation recorder.
    """
    from argosy.agents.analyst_responder import AnalystResponderAgent
    from argosy.agents.fund_manager_dialogue_verdict import (
        FundManagerDialogueVerdictAgent,
    )

    collected: list[AgentReport] = []

    # Turn 2 — analyst response.
    analyst = AnalystResponderAgent(user_id=user_id)
    analyst_report = analyst.run_sync(
        analyst_role=analyst_role,
        objection_topic=objection_topic,
        objection_detail=objection_detail,
        objection_severity=objection_severity,
        prior_agent_report_excerpt=_truncate_prior_report(
            prior_agent_report_excerpt,
        ),
        prior_decision_audit_token=prior_decision_audit_token,
        prior_agent_report_id=prior_agent_report_id,
        user_guidance=user_guidance or "",
        decision_id=decision_audit_token,
    )
    if isinstance(analyst_report, AgentReport):
        collected.append(analyst_report)
    analyst_out = getattr(analyst_report, "output", analyst_report)
    analyst_stance = getattr(analyst_out, "stance", "REBUT")
    analyst_reasoning_md = getattr(analyst_out, "reasoning_md", "") or ""
    analyst_suggested_fix = getattr(analyst_out, "suggested_fix", "") or ""
    analyst_cited_sources = list(getattr(analyst_out, "cited_sources", []) or [])

    # Turn 3 — FM verdict.
    fm = FundManagerDialogueVerdictAgent(user_id=user_id)
    fm_report = fm.run_sync(
        objection_topic=objection_topic,
        objection_detail=objection_detail,
        objection_severity=objection_severity,
        analyst_role=analyst_role,
        analyst_stance=analyst_stance,
        analyst_reasoning_md=analyst_reasoning_md,
        analyst_suggested_fix=analyst_suggested_fix,
        analyst_cited_sources=analyst_cited_sources,
        user_guidance=user_guidance or "",
        decision_id=decision_audit_token,
    )
    if isinstance(fm_report, AgentReport):
        collected.append(fm_report)
    fm_out = getattr(fm_report, "output", fm_report)
    resolution: Resolution = getattr(fm_out, "resolution", "FM_MAINTAINS_OBJECTION")
    fm_reasoning_md = getattr(fm_out, "reasoning_md", "") or ""
    updated_objection_text = getattr(fm_out, "updated_objection_text", None)
    suggested_plan_amendment = getattr(fm_out, "suggested_plan_amendment", None)
    fm_cited = list(getattr(fm_out, "cited_sources", []) or [])

    # Cited sources roll-up — combine analyst + FM citations.
    merged_cited: list[str] = []
    seen: set[str] = set()
    for s in (*analyst_cited_sources, *fm_cited):
        if s and s not in seen:
            merged_cited.append(s)
            seen.add(s)

    outcome = DialogueOutcome(
        resolution=resolution,
        analyst_stance=analyst_stance,
        analyst_reasoning_md=analyst_reasoning_md,
        analyst_suggested_fix=analyst_suggested_fix,
        fm_reasoning_md=fm_reasoning_md,
        updated_objection_text=updated_objection_text,
        suggested_plan_amendment=suggested_plan_amendment,
        cited_sources=merged_cited,
    )
    return outcome, collected


# ----------------------------------------------------------------------
# Public dispatcher
# ----------------------------------------------------------------------


@dataclass
class StartResult:
    """Return shape of ``start_fm_objection_dialogue``."""

    decision_run_id: int
    inflight: bool


def start_fm_objection_dialogue(
    session: Session,
    *,
    user_id: str,
    plan_version_id: int,
    objection_index: int,
    analyst_role: str,
    objection_topic: str,
    objection_detail: str,
    objection_severity: str,
    prior_decision_audit_token: str,
    user_guidance: str = "",
    run_inline: bool = False,
) -> StartResult:
    """Kick off the slim FM↔analyst dialogue for one objection.

    Steps:
      1. Idempotency peek — if a dialogue for the same 4-tuple is
         already in-flight, return its run_id with ``inflight=True``.
      2. Cost-cap check — refuse with CostCapExceededError if 24h
         spend + ESTIMATED_RUN_COST_USD would breach the cap.
      3. Validate analyst_role is in the canonical map.
      4. Open a ``decision_runs`` row with
         ``decision_kind="fm_objection_dialogue"`` and stamp notes_json
         with {objection_index, analyst_role, ...}.
      5. Dispatch the slim flow on a background thread (or inline for
         tests).
      6. Return (decision_run_id, inflight=False).
    """
    from argosy.agents.analyst_responder import ANALYST_AGENT_NAME_TO_ROLE

    analyst_role = (analyst_role or "").strip().lower()
    if analyst_role not in set(ANALYST_AGENT_NAME_TO_ROLE.values()):
        raise InvalidAnalystRoleError(
            f"analyst_role {analyst_role!r} is not in the canonical map. "
            f"Allowed: {sorted(set(ANALYST_AGENT_NAME_TO_ROLE.values()))}"
        )

    # 1. Idempotency peek BEFORE opening anything new.
    existing = _peek_inflight(
        user_id=user_id, plan_version_id=plan_version_id,
        objection_index=objection_index, analyst_role=analyst_role,
    )
    if existing is not None:
        log.info(
            "fm_objection_dialogue.idempotent_short_circuit",
            user_id=user_id, plan_version_id=plan_version_id,
            objection_index=objection_index, analyst_role=analyst_role,
            existing_run_id=existing,
        )
        return StartResult(decision_run_id=existing, inflight=True)

    # 2. Cost-cap check.
    cost_cap_usd = float(os.environ.get("ARGOSY_SYNTHESIS_COST_CAP_USD", "10.0"))
    spent_so_far = _total_recent_cost_usd(session, user_id=user_id)
    headroom = cost_cap_usd - spent_so_far
    if headroom < ESTIMATED_RUN_COST_USD:
        log.warning(
            "fm_objection_dialogue.cost_cap_refused",
            user_id=user_id, spent_24h=spent_so_far, cap=cost_cap_usd,
            estimated_cost=ESTIMATED_RUN_COST_USD,
        )
        raise CostCapExceededError(
            f"spent ${spent_so_far:.2f} in last 24h vs cap ${cost_cap_usd:.2f}; "
            f"estimated dialogue cost ${ESTIMATED_RUN_COST_USD:.2f} would breach. "
            "Bump ARGOSY_SYNTHESIS_COST_CAP_USD or wait for the 24h window to roll."
        )

    # 3. Pre-fetch the prior analyst agent_report excerpt so we can
    # thread it into the prompt. Best-effort — the responder can still
    # answer from first principles if the row is missing.
    prior_row = _resolve_prior_agent_report(
        session,
        user_id=user_id,
        decision_audit_token=prior_decision_audit_token,
        agent_role=analyst_role,
    )
    prior_excerpt = ""
    prior_id: int | None = None
    if prior_row is not None:
        prior_excerpt = prior_row.response_text or ""
        prior_id = prior_row.id

    # 4. Open the DecisionRun row.
    notes = {
        "objection_index": objection_index,
        "analyst_role": analyst_role,
        "objection_topic": objection_topic,
        "objection_severity": objection_severity,
        "prior_decision_audit_token": prior_decision_audit_token,
        "plan_version_id": plan_version_id,
        # Persist user_guidance verbatim for audit / replay. Already
        # length-capped at the route layer (max 2000 chars).
        "user_guidance": (user_guidance or "")[:2000],
    }
    run = DecisionRun(
        user_id=user_id,
        ticker="(plan)",
        tier=None,
        decision_kind="fm_objection_dialogue",
        started_at=datetime.now(timezone.utc),
        status="running",
        notes_json=json.dumps(notes, default=str),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    decision_run_id = run.id

    # 4b. Claim the in-flight slot. Race-safe: if a concurrent caller
    # beat us, use their id and roll our orphan row.
    claimed_existing = _claim_inflight_or_get(
        user_id=user_id, plan_version_id=plan_version_id,
        objection_index=objection_index, analyst_role=analyst_role,
        decision_run_id=decision_run_id,
    )
    if claimed_existing is not None and claimed_existing != decision_run_id:
        log.info(
            "fm_objection_dialogue.race_lost_using_existing",
            our_run_id=decision_run_id, existing_run_id=claimed_existing,
        )
        run.status = "superseded"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        return StartResult(decision_run_id=claimed_existing, inflight=True)

    # 5. Dispatch.
    kwargs = {
        "user_id": user_id,
        "plan_version_id": plan_version_id,
        "objection_index": objection_index,
        "analyst_role": analyst_role,
        "objection_topic": objection_topic,
        "objection_detail": objection_detail,
        "objection_severity": objection_severity,
        "prior_decision_audit_token": prior_decision_audit_token,
        "prior_agent_report_excerpt": prior_excerpt,
        "prior_agent_report_id": prior_id,
        "decision_run_id": decision_run_id,
        "user_guidance": user_guidance or "",
    }
    if run_inline:
        try:
            _execute_and_finalize(**kwargs)
        finally:
            _release_inflight(
                user_id=user_id, plan_version_id=plan_version_id,
                objection_index=objection_index, analyst_role=analyst_role,
            )
    else:
        t = threading.Thread(
            target=_thread_entry,
            kwargs=kwargs,
            name=f"fm-objection-dialogue-{decision_run_id}",
            daemon=True,
        )
        t.start()

    return StartResult(decision_run_id=decision_run_id, inflight=False)


def _thread_entry(**kwargs: Any) -> None:
    """Background-thread wrapper — always releases the in-flight slot."""
    user_id = kwargs["user_id"]
    plan_version_id = kwargs["plan_version_id"]
    objection_index = kwargs["objection_index"]
    analyst_role = kwargs["analyst_role"]
    try:
        _execute_and_finalize(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "fm_objection_dialogue.background_failed",
            user_id=user_id, plan_version_id=plan_version_id,
            objection_index=objection_index, analyst_role=analyst_role,
            error=str(exc),
        )
    finally:
        _release_inflight(
            user_id=user_id, plan_version_id=plan_version_id,
            objection_index=objection_index, analyst_role=analyst_role,
        )


def _execute_and_finalize(
    *,
    user_id: str,
    plan_version_id: int,
    objection_index: int,
    analyst_role: str,
    objection_topic: str,
    objection_detail: str,
    objection_severity: str,
    prior_decision_audit_token: str,
    prior_agent_report_excerpt: str,
    prior_agent_report_id: int | None,
    decision_run_id: int,
    user_guidance: str = "",
) -> None:
    """End-to-end execution of one dialogue. Persists outcome + finalizes row."""
    from argosy.api.events import publish_event_threadsafe
    from argosy.state import db as db_mod

    decision_audit_token = f"fm-obj-dialogue-{decision_run_id}"
    started_at = datetime.now(timezone.utc)

    publish_event_threadsafe(
        "plan.fm_objection.dialogue.started",
        {
            "user_id": user_id,
            "plan_version_id": plan_version_id,
            "objection_index": objection_index,
            "analyst_role": analyst_role,
            "decision_run_id": decision_run_id,
        },
    )

    outcome: DialogueOutcome | None = None
    collected: list[AgentReport] = []
    error_text: str | None = None

    try:
        outcome, collected = _run_dialogue(
            user_id=user_id,
            objection_topic=objection_topic,
            objection_detail=objection_detail,
            objection_severity=objection_severity,
            analyst_role=analyst_role,
            prior_decision_audit_token=prior_decision_audit_token,
            prior_agent_report_excerpt=prior_agent_report_excerpt,
            prior_agent_report_id=prior_agent_report_id,
            decision_audit_token=decision_audit_token,
            user_guidance=user_guidance,
        )
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
        log.exception(
            "fm_objection_dialogue.flow_failed",
            user_id=user_id, plan_version_id=plan_version_id,
            objection_index=objection_index, analyst_role=analyst_role,
            error=error_text,
        )

    # Persist the JSONL trail.
    _persist_agent_reports_jsonl(
        decision_audit_token=decision_audit_token, reports=collected,
    )

    # Record the phase via the negotiation recorder.
    try:
        import asyncio

        from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
            _persist_phase_agent_reports_async,
        )
        from argosy.services.negotiation_recorder import (
            record_negotiation_phase,
        )

        async def _do_recorder() -> None:
            ids: list[int] = []
            if collected:
                try:
                    ids = await _persist_phase_agent_reports_async(collected)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "fm_objection_dialogue.persist_agent_reports_failed",
                        error=str(exc),
                    )
            phase_kind = "fm_objection_dialogue.verdict"
            phase_output: str | dict = (
                {
                    "resolution": outcome.resolution,
                    "analyst_stance": outcome.analyst_stance,
                    "analyst_reasoning_md": outcome.analyst_reasoning_md,
                    "analyst_suggested_fix": outcome.analyst_suggested_fix,
                    "fm_reasoning_md": outcome.fm_reasoning_md,
                    "updated_objection_text": outcome.updated_objection_text,
                    "suggested_plan_amendment": outcome.suggested_plan_amendment,
                    "cited_sources": outcome.cited_sources,
                }
                if outcome is not None
                else (error_text or "flow_failed")
            )
            await record_negotiation_phase(
                user_id=user_id,
                decision_run_id=decision_run_id,
                kind=phase_kind,
                started_at=started_at,
                agent_report_ids=ids,
                verdict=None,
                phase_output=phase_output,
            )

        asyncio.run(_do_recorder())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "fm_objection_dialogue.recorder_failed",
            user_id=user_id, error=str(exc),
        )

    # Finalize the DecisionRun row.
    try:
        import asyncio
        from sqlalchemy import update as sa_update

        async def _finalize_async() -> None:
            async with db_mod.get_session() as s:
                row = await s.get(DecisionRun, decision_run_id)
                if row is None:
                    return
                try:
                    notes = json.loads(row.notes_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    notes = {}
                if outcome is not None:
                    notes["resolution"] = outcome.resolution
                    notes["analyst_stance"] = outcome.analyst_stance
                    notes["analyst_reasoning_md"] = outcome.analyst_reasoning_md
                    notes["analyst_suggested_fix"] = outcome.analyst_suggested_fix
                    notes["fm_reasoning_md"] = outcome.fm_reasoning_md
                    notes["updated_objection_text"] = outcome.updated_objection_text
                    notes["suggested_plan_amendment"] = outcome.suggested_plan_amendment
                    notes["cited_sources"] = outcome.cited_sources
                    status_value = "completed"
                else:
                    notes["error"] = error_text or "flow_failed"
                    status_value = "failed"
                await s.execute(
                    sa_update(DecisionRun)
                    .where(DecisionRun.id == decision_run_id)
                    .values(
                        notes_json=json.dumps(notes, default=str),
                        status=status_value,
                        finished_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()

        asyncio.run(_finalize_async())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "fm_objection_dialogue.finalize_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )

    publish_event_threadsafe(
        "plan.fm_objection.dialogue.completed",
        {
            "user_id": user_id,
            "plan_version_id": plan_version_id,
            "objection_index": objection_index,
            "analyst_role": analyst_role,
            "decision_run_id": decision_run_id,
            "resolution": outcome.resolution if outcome is not None else None,
            "error": error_text,
        },
    )


# ----------------------------------------------------------------------
# Re-render helper for the GET /dialogues endpoint
# ----------------------------------------------------------------------


def list_dialogues_for_plan_version(
    session: Session, *, user_id: str, plan_version_id: int,
) -> list[dict]:
    """Return the user's prior dialogues for this plan_version, newest first.

    Used by GET /api/plan/draft/objections/{idx}/dialogues so the UI can
    re-render the dialogue state on page reload without re-fetching the
    LLM output. Each row is a DICT, not a pydantic model, so the API
    route can map fields to its response schema independently.
    """
    rows = session.execute(
        select(DecisionRun).where(
            DecisionRun.user_id == user_id,
            DecisionRun.decision_kind == "fm_objection_dialogue",
        ).order_by(desc(DecisionRun.started_at))
    ).scalars().all()
    out: list[dict] = []
    for r in rows:
        try:
            notes = json.loads(r.notes_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if notes.get("plan_version_id") != plan_version_id:
            continue
        out.append({
            "decision_run_id": r.id,
            "status": r.status,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "notes": notes,
        })
    return out


__all__ = [
    "CostCapExceededError",
    "DialogueOutcome",
    "ESTIMATED_RUN_COST_USD",
    "FMObjectionDialogueError",
    "IDEMPOTENCY_WINDOW_SECONDS",
    "InvalidAnalystRoleError",
    "ObjectionNotFoundError",
    "Resolution",
    "StartResult",
    "_claim_inflight_or_get",
    "_in_flight",
    "_in_flight_lock",
    "_peek_inflight",
    "_release_inflight",
    "_run_dialogue",
    "list_dialogues_for_plan_version",
    "parse_agent_refs_from_objection",
    "start_fm_objection_dialogue",
]
