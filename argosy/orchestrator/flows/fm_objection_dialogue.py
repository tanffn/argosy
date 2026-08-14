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


def _classify_objection_owner_llm(
    user_id: str,
    objection_topic: str,
    objection_detail: str,
    objection_severity: str,
) -> tuple[str | None, bool, str]:
    """Call ObjectionOwnerClassifierAgent to find the owner of an un-cited objection.

    Returns ``(owner_role | None, needs_user_input, user_question)``.
    ``owner_role`` is constrained to the canonical role list; a hallucinated
    role is treated as None. Fail-soft on any exception → (None, False, "").
    """
    # Sol final pass: these imports sat OUTSIDE the try. An ImportError here
    # would abort scheduling for an un-cited objection, so it reached neither
    # an analyst nor the inbox and was never counted as lost. Owner resolution
    # is best-effort by design — it must never be able to take the loop down.
    try:
        from argosy.agents.analyst_responder import ANALYST_AGENT_NAME_TO_ROLE
        from argosy.agents.objection_owner_classifier import (
            ObjectionOwnerClassifierAgent,
        )

        candidate_roles = sorted(set(ANALYST_AGENT_NAME_TO_ROLE.values()))
    except Exception as exc:  # noqa: BLE001
        log.error(
            "auto_dialogue.classifier_unavailable",
            user_id=user_id, error=str(exc),
            note="objection will fall through to the user-surfacing path",
        )
        return None, False, ""
    try:
        agent = ObjectionOwnerClassifierAgent(user_id=user_id)
        report = agent.run_sync(
            objection_topic=objection_topic,
            objection_detail=objection_detail,
            objection_severity=objection_severity,
            candidate_roles=candidate_roles,
        )
        out = getattr(report, "output", report)
        owner_role = getattr(out, "owner_role", None) or None
        needs_user_input = bool(getattr(out, "needs_user_input", False))
        user_question = (getattr(out, "user_question", "") or "").strip()

        # Constrain to known roles — never accept a hallucinated string.
        known_roles = set(ANALYST_AGENT_NAME_TO_ROLE.values())
        if owner_role and owner_role not in known_roles:
            log.warning(
                "objection_owner_classifier.unknown_role_returned",
                user_id=user_id, owner_role=owner_role,
                known_roles=sorted(known_roles),
            )
            owner_role = None

        return owner_role, needs_user_input, user_question
    except Exception as exc:  # noqa: BLE001 — fail-soft; caller logs + surfaces
        log.warning(
            "objection_owner_classifier.call_failed",
            user_id=user_id,
            objection_topic=objection_topic,
            error=str(exc),
        )
        return None, False, ""


def _surface_unroutable_as_proposal(**kwargs: object) -> bool:
    """Total wrapper: surfacing an objection must NEVER raise.

    Sol final pass: the implementation can raise before its own try block
    (``same_path_signature``, a local import), and callers only record a loss
    when they see ``False``. A raise there meant the objection reached neither
    an analyst nor the inbox AND was not counted — the exact silent-loss this
    flow exists to make impossible. This wrapper converts any escape into a
    counted ``False``.
    """
    try:
        return bool(_surface_unroutable_as_proposal_impl(**kwargs))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — surfacing must not raise
        log.error(
            "fm_objection_dialogue.surface_raised_objection_lost",
            error=str(exc),
            objection_index=kwargs.get("objection_index"),
            topic=str(kwargs.get("objection_topic"))[:200],
        )
        return False


def _surface_unroutable_as_proposal_impl(
    *,
    user_id: str,
    plan_version_id: int,
    decision_run_id: int,
    objection_index: int,
    objection_topic: str,
    objection_detail: str,
    objection_severity: str,
    user_question: str,
) -> bool:
    """Create an open ActionProposal so an unroutable objection reaches the user's inbox.

    Follows the ``critique_reconcile.needs_user_input`` pattern exactly:
    kind="note_only", dedup_key scoped to (user, plan_version, objection_index).

    The ``user_question`` must be a concrete, specific question (not generic
    "synthesis failed" text). The caller is responsible for providing it.
    Applies the escalation-bar transport guard (same_path_signature) as a
    warning-only log — never blocks.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone

    from argosy.services.escalation_guard import same_path_signature
    from argosy.state import db as db_mod
    from argosy.state.models import ActionProposal

    question = user_question or (
        f"The Fund Manager raised an objection ({objection_severity}) that "
        f"could not be routed to any analyst: {objection_topic}. "
        f"Detail: {objection_detail[:400]}"
    )

    # Transport guard. Ariel's escalation bar (CLAUDE.md): only structurally
    # different PATHS reach him; two agents disagreeing on a derived VALUE is a
    # derivation question the fleet must zigzag out itself.
    #
    # Sol flagged that this only warned and escalated anyway. The fix is NOT to
    # drop it — of the two failure modes, spamming Ariel is recoverable and
    # silently binning a fund-manager BLOCKER is not, which is the whole reason
    # this function exists. Instead it is surfaced but LABELLED as an internal
    # resolution failure rather than a genuine decision, and logged at ERROR so
    # it reads as a fleet defect in ops. If these show up in the inbox, the fix
    # is to improve owner routing, not to start dropping them.
    _looks_like_value_dispute = bool(same_path_signature(question))
    if _looks_like_value_dispute:
        log.error(
            "escalation_guard.value_dispute_reached_user",
            user_id=user_id,
            source="fm_objection_dialogue.unroutable_proposal",
            objection_index=objection_index,
            topic=objection_topic,
            question=question[:300],
            note="fleet failed to resolve a derivation question internally",
        )
        question = (
            "[Argosy could not settle this internally — it is a derivation "
            "question the fleet should have resolved, not a decision for you. "
            "Answer only if you want to; otherwise it is a bug to fix.] "
        ) + question

    async def _do() -> None:
        import json as _json

        now = datetime.now(timezone.utc)
        # Sol review: keying on (user, plan_version, index) alone means a
        # DIFFERENT objection landing at the same index silently overwrites the
        # open proposal for the previous one — losing it. The content hash makes
        # the key identify the objection itself, so re-runs still dedup but a
        # genuinely different question gets its own row.
        import hashlib as _hashlib

        _content_sig = _hashlib.sha256(
            f"{objection_topic}\n{objection_detail}".encode("utf-8", "replace")
        ).hexdigest()[:12]
        dedup_key = (
            f"fm_objection_unroutable:{user_id}:{plan_version_id}:"
            f"{objection_index}:{_content_sig}"
        )
        payload = {
            "objection_index": objection_index,
            "objection_topic": objection_topic,
            "plan_version_id": plan_version_id,
            "decision_run_id": decision_run_id,
        }
        severity_label = (
            "warning" if objection_severity in ("RED", "BLOCKER", "CRITICAL") else "info"
        )
        async with db_mod.get_session() as s:
            from sqlalchemy import select as _select
            existing = (
                await s.execute(
                    _select(ActionProposal).where(
                        ActionProposal.dedup_key == dedup_key,
                        ActionProposal.status == "open",
                    )
                )
            ).scalars().first()
            if existing is not None:
                existing.summary = f"FM objection needs your input — {objection_topic}"
                existing.rationale_md = question
                existing.suggested_payload = _json.dumps(payload)
                existing.severity = severity_label
                existing.surfaced_at = now
                existing.expires_at = now + timedelta(days=30)
            else:
                s.add(
                    ActionProposal(
                        user_id=user_id,
                        summary=f"FM objection needs your input — {objection_topic}",
                        rationale_md=question,
                        suggested_payload=_json.dumps(payload),
                        severity=severity_label,
                        surfaced_at=now,
                        expires_at=now + timedelta(days=30),
                        status="open",
                        kind="note_only",
                        dedup_key=dedup_key,
                        execution_state="proposed",
                    )
                )
            await s.commit()

    try:
        asyncio.run(_do())
        return True
    except Exception as exc:  # noqa: BLE001
        # Sol review: this is the LAST RESORT for an objection nobody could
        # route. Swallowing a failure here means a blocking objection from the
        # fund manager exists only in a log line — the precise failure this
        # whole change removes. Log at ERROR and tell the caller, so the batch
        # summary can report objections that reached NOBODY.
        log.error(
            "fm_objection_dialogue.proposal_create_failed_objection_lost",
            user_id=user_id,
            objection_index=objection_index,
            topic=objection_topic,
            error=str(exc),
        )
        return False


def schedule_auto_dialogues_for_draft(
    session: Session,
    *,
    user_id: str,
    plan_version_id: int,
    decision_run_id: int,
) -> int:
    """Fire FM<->analyst dialogues for every objection on this draft.

    Called by plan_synthesis/orchestrator.py post-FM-verdict to
    pre-resolve concerns the fleet can settle internally, BEFORE the
    user sees /plan. Best-effort and idempotent (the underlying
    dispatcher short-circuits on duplicate in-flight runs). Background-
    threaded per objection, so this returns quickly.

    Owner resolution (two-stage):
      1. Regex — ``_parse_analyst_refs_any_form`` scans for explicit
         ``agent_report:XAgent`` citations. This handles the common case
         where the FM names the specialist it's questioning.
      2. LLM classifier — when the regex finds nothing, we call
         ``ObjectionOwnerClassifierAgent`` constrained to the canonical
         role list. It either returns an ``owner_role`` (derivation
         question → zigzag with that analyst) or ``needs_user_input=True``
         (structural fork → surface to the user's inbox as an
         ActionProposal with a concrete question).

    No silent drops:
      - A regex hit routes immediately.
      - A classifier hit (owner_role) routes after the LLM call.
      - A classifier hit (needs_user_input) creates an ActionProposal.
      - A classifier miss (None + False) logs at WARNING and creates a
        fallback ActionProposal so nothing vanishes.
      - Contrast with the old code: all three no-owner cases were a
        silent log.info + continue.

    Returns the count of dialogues actually dispatched.
    """
    from argosy.api.routes.plan import (
        _classify_severity,
        _parse_fm_response,
        _split_reason,
    )
    from argosy.state.models import AgentReport

    decision_audit_token = f"plan-synth-{decision_run_id}"
    fm_row = session.execute(
        select(AgentReport).where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_audit_token,
            AgentReport.agent_role == "fund_manager",
        ).order_by(desc(AgentReport.created_at)).limit(1)
    ).scalar_one_or_none()
    if fm_row is None or not fm_row.response_text:
        log.info(
            "auto_dialogue.no_fm_row",
            user_id=user_id, plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
        )
        return 0

    parsed = _parse_fm_response(fm_row.response_text)
    reasons = parsed.get("reasons") or []

    dispatched = 0
    # Objections that reached NEITHER an analyst NOR the user's inbox.
    # Non-empty here is a hard defect: a fund-manager BLOCKER went nowhere.
    lost_objections: list[int] = []
    for idx, raw in enumerate(reasons):
        if not isinstance(raw, str) or not raw.strip():
            continue
        topic, detail = _split_reason(raw)
        severity = _classify_severity(topic, detail)
        # Scan BOTH topic and detail — see UI-side parser comment;
        # the backend splitter occasionally puts citation text in
        # the topic half for long FM reasons.
        analyst_roles = _parse_analyst_refs_any_form(topic + " " + detail)

        if not analyst_roles:
            # Stage 2: no explicit citation → ask the LLM classifier.
            owner_role, needs_user_input, user_question = _classify_objection_owner_llm(
                user_id, topic, detail, severity,
            )
            if owner_role:
                analyst_roles = [owner_role]
                log.info(
                    "auto_dialogue.classifier_resolved_owner",
                    user_id=user_id, plan_version_id=plan_version_id,
                    decision_run_id=decision_run_id, objection_index=idx,
                    owner_role=owner_role,
                )
            elif needs_user_input:
                # Genuine structural fork — surface to the user's inbox.
                log.warning(
                    "auto_dialogue.unroutable_needs_user_input",
                    user_id=user_id, plan_version_id=plan_version_id,
                    decision_run_id=decision_run_id, objection_index=idx,
                    severity=severity, topic=topic,
                )
                if not _surface_unroutable_as_proposal(
                    user_id=user_id,
                    plan_version_id=plan_version_id,
                    decision_run_id=decision_run_id,
                    objection_index=idx,
                    objection_topic=topic,
                    objection_detail=detail,
                    objection_severity=severity,
                    user_question=user_question,
                ):
                    lost_objections.append(idx)
                continue
            else:
                # Classifier could not determine ownership at all.
                # Must not vanish — log at WARNING and create a fallback
                # proposal so the objection surfaces somewhere.
                log.warning(
                    "auto_dialogue.unroutable_no_owner",
                    user_id=user_id, plan_version_id=plan_version_id,
                    decision_run_id=decision_run_id, objection_index=idx,
                    severity=severity, topic=topic,
                )
                if not _surface_unroutable_as_proposal(
                    user_id=user_id,
                    plan_version_id=plan_version_id,
                    decision_run_id=decision_run_id,
                    objection_index=idx,
                    objection_topic=topic,
                    objection_detail=detail,
                    objection_severity=severity,
                    user_question=user_question,  # may be empty; helper fills fallback
                ):
                    lost_objections.append(idx)
                continue

        # Use the first analyst ref (most-cited / canonical owner).
        # Multi-analyst objections are rare; when the user wants a
        # different analyst they can still fire the manual dialogue.
        analyst_role = analyst_roles[0]
        try:
            result = start_fm_objection_dialogue(
                session,
                user_id=user_id,
                plan_version_id=plan_version_id,
                objection_index=idx,
                analyst_role=analyst_role,
                objection_topic=topic,
                objection_detail=detail,
                objection_severity=severity,
                prior_decision_audit_token=decision_audit_token,
                user_guidance="",  # No user input on auto-dispatch.
            )
            dispatched += 1
            log.info(
                "auto_dialogue.dispatched",
                user_id=user_id, plan_version_id=plan_version_id,
                objection_index=idx, analyst_role=analyst_role,
                dialogue_run_id=result.decision_run_id,
                inflight=result.inflight,
            )
        except CostCapExceededError as exc:
            # Sol final pass: breaking here dropped THIS objection and every
            # remaining one. Hitting a spend cap is a reason to stop paying for
            # LLM dialogues — it is not a reason to lose the fund manager's
            # blocking objections. Surfacing is a plain DB write with no model
            # call, so surface the rest and then stop dispatching.
            log.warning(
                "auto_dialogue.cost_cap_stopped_surfacing_remainder",
                user_id=user_id, plan_version_id=plan_version_id,
                objection_index=idx, dispatched_before_stop=dispatched,
                remaining=len(reasons) - idx, error=str(exc),
            )
            for _rem_idx in range(idx, len(reasons)):
                _rem_topic, _rem_detail = _split_reason(reasons[_rem_idx])
                if not _surface_unroutable_as_proposal(
                    user_id=user_id, plan_version_id=plan_version_id,
                    decision_run_id=decision_run_id, objection_index=_rem_idx,
                    objection_topic=_rem_topic, objection_detail=_rem_detail,
                    objection_severity=_classify_severity(_rem_topic, _rem_detail),
                    user_question=(
                        "Argosy stopped its internal review at the spend cap "
                        f"before resolving this objection: {_rem_topic}"
                    ),
                ):
                    lost_objections.append(_rem_idx)
            break
        except InvalidAnalystRoleError as exc:
            # Sol review: a routed objection whose dispatch fails was neither
            # zigzagged NOR surfaced -- it just vanished. Fall through to the
            # user-surfacing path so a blocking objection always lands
            # somewhere.
            log.warning(
                "auto_dialogue.invalid_role_surfacing",
                user_id=user_id, objection_index=idx,
                analyst_role=analyst_role, error=str(exc),
            )
            if not _surface_unroutable_as_proposal(
                user_id=user_id, plan_version_id=plan_version_id,
                decision_run_id=decision_run_id, objection_index=idx,
                objection_topic=topic, objection_detail=detail,
                objection_severity=severity,
                user_question=(
                    f"Argosy could not route this blocking objection to "
                    f"{analyst_role} (dispatch rejected: {exc}). It needs a "
                    f"human look: {topic}"
                ),
            ):
                lost_objections.append(idx)
            continue
        except Exception as exc:  # noqa: BLE001
            # Sol re-review: only InvalidAnalystRoleError got the fallback; a
            # generic dispatch failure still vanished. EVERY failure to reach an
            # owner must end up somewhere a human can see.
            log.warning(
                "auto_dialogue.dispatch_failed_surfacing",
                user_id=user_id, objection_index=idx,
                analyst_role=analyst_role, error=str(exc),
            )
            if not _surface_unroutable_as_proposal(
                user_id=user_id, plan_version_id=plan_version_id,
                decision_run_id=decision_run_id, objection_index=idx,
                objection_topic=topic, objection_detail=detail,
                objection_severity=severity,
                user_question=(
                    f"Argosy could not deliver this blocking objection to "
                    f"{analyst_role} (dispatch failed: {exc}). It needs a human "
                    f"look: {topic}"
                ),
            ):
                lost_objections.append(idx)
            continue
    log.info(
        "auto_dialogue.batch_complete",
        user_id=user_id, plan_version_id=plan_version_id,
        decision_run_id=decision_run_id, dispatched=dispatched,
        total_objections=len(reasons),
        lost_objections=len(lost_objections),
    )
    if lost_objections:
        # Sol re-review: the surfacing helper can fail to write its proposal.
        # If that happens the objection exists only in a log line -- exactly the
        # failure this whole flow removes -- so say so at ERROR with the indices.
        log.error(
            "auto_dialogue.objections_lost",
            user_id=user_id, plan_version_id=plan_version_id,
            decision_run_id=decision_run_id,
            lost_indices=lost_objections,
            note="blocking objections reached neither an analyst nor the inbox",
        )
    return dispatched


@dataclass
class FmConvergenceResult:
    """Outcome of running FM<->analyst dialogues INLINE to convergence."""

    dispatched: int
    resolutions: list[dict] = field(default_factory=list)   # per objection (+ terminal_state)
    all_agreed: bool = False                                # every objection CLEARED_NO_CHANGE_REQUIRED
    unresolved: list[str] = field(default_factory=list)     # why not (typed blocking states)


# Cost/scope bound on inline convergence (codex: keep small; overflow stays blocking).
_MAX_INLINE_OBJECTIONS = 10

# Typed terminal states (codex): "dialogue resolved" != "authority cleared". ONLY
# CLEARED_NO_CHANGE_REQUIRED clears the FM authority.
_CLEARED = "CLEARED_NO_CHANGE_REQUIRED"


def _terminal_state(resolution: str, analyst_stance: str) -> str:
    """Map (FM resolution, analyst stance) → a fail-closed terminal state.

    The critical guardrail (codex): an FM that ACCEPTS an analyst who CONCEDED is
    confirming a DEFECT — the plan must change + re-gate, so it stays blocking. Only an
    FM-accepted REBUT/CLARIFY (the objection was wrong / already satisfied, no artifact
    change) clears. A REVISE silently weakening the objection never auto-clears."""
    stance = (analyst_stance or "").strip().upper()
    if resolution == "FM_ACCEPTS_ANALYST":
        if stance == "CONCEDE":
            return "CHANGE_REQUIRED"          # defect confirmed → blocking
        return _CLEARED                       # rebut/clarify accepted → no change needed
    if resolution == "FM_REVISES_OBJECTION":
        return "REVISED_BLOCKING"             # revised, still open
    if resolution == "ESCALATE_TO_USER":
        return "ESCALATE_TO_USER"
    return "MAINTAINED_BLOCKING"


def converge_fm_objections(
    session: Session, *, user_id: str, plan_version_id: int, decision_run_id: int,
) -> FmConvergenceResult:
    """Run the FM<->analyst dialogue for every objection INLINE (synchronous) and report
    whether they ALL closed by agreement. The caller decides whether to clear the FM
    authority; this function only negotiates + reports (bounded by the cost cap)."""
    from argosy.api.routes.plan import (
        _classify_severity, _parse_fm_response, _split_reason,
    )
    from argosy.state.models import AgentReport

    decision_audit_token = f"plan-synth-{decision_run_id}"
    fm_row = session.execute(
        select(AgentReport).where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_audit_token,
            AgentReport.agent_role == "fund_manager",
        ).order_by(desc(AgentReport.created_at)).limit(1)
    ).scalar_one_or_none()
    if fm_row is None or not fm_row.response_text:
        return FmConvergenceResult(dispatched=0, all_agreed=False,
                                   unresolved=["no FM objection report"])

    reasons = _parse_fm_response(fm_row.response_text).get("reasons") or []
    resolutions: list[dict] = []
    unresolved: list[str] = []
    dispatched = 0
    for idx, raw in enumerate(reasons):
        if not isinstance(raw, str) or not raw.strip():
            continue
        if dispatched >= _MAX_INLINE_OBJECTIONS:
            unresolved.append(f"[{idx}] overflow > {_MAX_INLINE_OBJECTIONS} inline → blocking")
            continue
        topic, detail = _split_reason(raw)
        roles = _parse_analyst_refs_any_form(topic + " " + detail)
        if not roles:
            unresolved.append(f"[{idx}] {topic}: UNOWNED_BLOCKING")
            continue
        try:
            start = start_fm_objection_dialogue(
                session, user_id=user_id, plan_version_id=plan_version_id,
                objection_index=idx, analyst_role=roles[0], objection_topic=topic,
                objection_detail=detail,
                objection_severity=_classify_severity(topic, detail),
                prior_decision_audit_token=decision_audit_token, user_guidance="",
                run_inline=True,
            )
        except CostCapExceededError as exc:
            unresolved.append(f"[{idx}] {topic}: TIMEOUT_BLOCKING (cost cap: {exc})")
            break
        except Exception as exc:  # noqa: BLE001
            unresolved.append(f"[{idx}] {topic}: TIMEOUT_BLOCKING (dialogue failed: {exc})")
            continue
        dispatched += 1
        run = session.get(DecisionRun, start.decision_run_id)
        try:
            notes = json.loads(run.notes_json or "{}") if run else {}
        except (json.JSONDecodeError, TypeError):
            notes = {}
        resolution = notes.get("resolution", "FM_MAINTAINS_OBJECTION")
        stance = notes.get("analyst_stance", "")
        terminal = _terminal_state(resolution, stance)
        resolutions.append({
            "objection_index": idx, "analyst_role": roles[0], "topic": topic,
            "resolution": resolution, "analyst_stance": stance,
            "terminal_state": terminal,
            "fm_reasoning_md": notes.get("fm_reasoning_md", ""),
        })
        if terminal != _CLEARED:
            unresolved.append(f"[{idx}] {topic}: {terminal}")
    all_agreed = dispatched > 0 and not unresolved
    log.info(
        "fm_objection_dialogue.converge_complete", user_id=user_id,
        plan_version_id=plan_version_id, dispatched=dispatched,
        all_agreed=all_agreed, unresolved=len(unresolved),
    )
    return FmConvergenceResult(dispatched=dispatched, resolutions=resolutions,
                               all_agreed=all_agreed, unresolved=unresolved)


def _parse_analyst_refs_any_form(text: str) -> list[str]:
    """Find every distinct analyst reference in ``text``, tolerating
    both the CamelCase class form (``agent_report:PlanCritiqueAgent``)
    and the snake_case role form (``agent_report:plan_critique``).

    Returns role names in encounter order, deduplicated. Empty when no
    recognized analyst references are present. Mirrors the UI parser
    in ``ui/src/components/plan/fm-objections-card.tsx`` so backend
    auto-dispatch and frontend "Discuss with [analyst]" agree on which
    objections have an analyst owner.
    """
    import re
    from argosy.agents.analyst_responder import ANALYST_AGENT_NAME_TO_ROLE

    if not text:
        return []
    seen: dict[str, None] = {}
    pattern = re.compile(r"agent_report:([A-Za-z_]+)")
    known_roles = set(ANALYST_AGENT_NAME_TO_ROLE.values())
    for m in pattern.finditer(text):
        ref = m.group(1)
        role = ANALYST_AGENT_NAME_TO_ROLE.get(ref)
        if role is None and ref in known_roles:
            role = ref
        if role and role not in seen:
            seen[role] = None
    return list(seen.keys())


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
    "_classify_objection_owner_llm",
    "_in_flight",
    "_in_flight_lock",
    "_peek_inflight",
    "_release_inflight",
    "_run_dialogue",
    "_surface_unroutable_as_proposal",
    "list_dialogues_for_plan_version",
    "parse_agent_refs_from_objection",
    "schedule_auto_dialogues_for_draft",
    "start_fm_objection_dialogue",
]
