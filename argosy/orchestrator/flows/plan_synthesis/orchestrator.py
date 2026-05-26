"""plan_synthesis orchestrator — five-phase entry point and phase implementations.

Calling convention for monkeypatch compatibility
-------------------------------------------------
All helpers that tests may monkeypatch are called via the package namespace
(``_pkg.<name>``) rather than as bare module-level names.  This means when a
test does::

    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", ...)

the attribute on the *package* ``__init__`` is replaced, and the call site
here resolves through that same namespace — so the patch takes effect.

Import: ``from argosy.orchestrator.flows import plan_synthesis as _pkg``
is performed lazily inside each function to avoid circular-import risk
(the package's ``__init__`` imports from this module).
"""

from __future__ import annotations

import inspect
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import NamedTuple, cast

from sqlalchemy.orm import Session

from argosy.agents.base import AgentReport
from argosy.agents.concentration_analyst import ConcentrationAnalystAgent
from argosy.agents.household_budget_analyst import HouseholdBudgetAnalystAgent
from argosy.agents.fundamentals_analyst import FundamentalsAnalystAgent
# The FX analyst class is `FXAnalystAgent` in source; the synthesis flow
# (and its tests) refer to it as `FxAnalystAgent`. Aliased on import.
from argosy.agents.fx_analyst import FXAnalystAgent as FxAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.agents.plan_synthesizer import PlanSynthesizerAgent
from argosy.agents.plan_synthesizer_types import (
    PlanSynthesisOutput,
    SynthesisInputs,
)
from argosy.orchestrator.flows.plan_synthesis._types import (
    NoBaselineError,
    SynthesisResult,
    Trigger,
)
from argosy.agents.sentiment_analyst import SentimentAnalystAgent
from argosy.agents.tax_analyst import TaxAnalystAgent
from argosy.agents.technical_analyst import TechnicalAnalystAgent
from argosy.logging import get_logger
from argosy.state.models import DecisionRun, PlanVersion
from argosy.state.queries import get_active_baseline, get_current_plan, get_pending_draft

log = get_logger(__name__)


def _emit_event(event_type: str, payload: dict) -> None:
    """Best-effort fire-and-forget publish from sync code (M2 fix).

    Delegates to ``publish_event_threadsafe`` which centralises the
    sync→async bridge and uses a threading.Lock so it is safe when called
    from asyncio.to_thread worker threads (monthly_cycle path).
    Any failure is swallowed — synthesis must never break because of a
    flaky event subscriber.
    """
    from argosy.api.events import publish_event_threadsafe

    publish_event_threadsafe(event_type, payload)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def run_synthesis(
    session: Session,
    *,
    user_id: str,
    trigger: Trigger,
    guidance: str = "",
    existing_decision_run_id: int | None = None,
    resume_from_phase: int = 1,
):
    """Execute the 5-phase synthesis. Writes a role='draft' row.

    Args:
        guidance: optional free-text from the user's check-in to weight
            the synthesis (e.g. "weight tax analyst more heavily").
        existing_decision_run_id: when set, reuse this DecisionRun row
            for audit lineage instead of opening a fresh one. Used by
            the plan_amendment_chat large worker so a single decision_run
            spans amendment dispatch + synthesis (no smeared lineage
            across two unrelated rows). When None, behaves as before:
            opens a fresh `decision_kind='plan_revision'` row.
        resume_from_phase: T2.3. When > 1, load earlier phases' outputs
            from ``decision_phases`` (kind='synthesis.phase_N',
            phase_output_json) instead of re-running them. Requires
            ``existing_decision_run_id`` so we know which prior run's
            phases to load. Default 1 = run all phases from scratch.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    baseline = get_active_baseline(session, user_id)
    if baseline is None:
        raise NoBaselineError(f"user {user_id!r} has no active baseline plan")

    prior_current = get_current_plan(session, user_id)

    if existing_decision_run_id is not None:
        # Reuse the caller's row (e.g. plan_amendment_chat large worker)
        # so the lineage chat-turn → DecisionRun → draft is a single
        # path, not two rows tied together by convention. The caller is
        # responsible for stamping the row's started_at + decision_kind.
        decision_run = session.get(DecisionRun, existing_decision_run_id)
        if decision_run is None:
            raise RuntimeError(
                f"existing_decision_run_id={existing_decision_run_id} not found",
            )
        decision_run_id = decision_run.id
    else:
        # Open a real DecisionRun row so PlanVersion.decision_run_id (Integer FK)
        # is valid and the SDD §6.11 audit lineage is real, not fictitious.
        # ticker="(plan)" and tier="T3" are sentinels for plan-revision runs
        # (distinct from per-trade runs which carry the actual ticker + tier).
        decision_run = DecisionRun(
            user_id=user_id,
            ticker="(plan)",
            tier="T3",
            decision_kind="plan_revision",
            started_at=datetime.now(timezone.utc),
            status="running",
        )
        session.add(decision_run)
        session.commit()
        session.refresh(decision_run)
        decision_run_id = decision_run.id  # integer PK — used for PlanVersion FK

    # String-form audit token for agent_reports.decision_id (String column).
    # Kept separate so the agent_reports audit trail has a human-readable ref.
    decision_audit_token: str = f"plan-synth-{decision_run_id}"

    log.info(
        "plan_synthesis.start",
        user_id=user_id,
        trigger=trigger,
        decision_run_id=decision_run_id,
    )
    _emit_event("plan.draft.started", {"user_id": user_id, "trigger": trigger})

    # Idempotency: demote any existing draft.
    existing = get_pending_draft(session, user_id)
    if existing is not None:
        existing.role = "superseded"
        existing.superseded_at = datetime.now(timezone.utc)
        session.commit()
        log.info(
            "plan_synthesis.demoted_existing_draft",
            superseded_id=existing.id,
            user_id=user_id,
        )

    # T2.1 — soft cost cap per synthesis run. Read from env so it can be
    # bumped without code edits ($10 default). When the cumulative cost
    # of persisted agent_reports exceeds the cap, we abort with an
    # explicit RuntimeError after the current phase finishes (we don't
    # interrupt mid-phase to avoid orphaning in-flight Opus calls that
    # were already going to charge).
    import os as _os

    cost_cap_usd = float(_os.environ.get("ARGOSY_SYNTHESIS_COST_CAP_USD", "10.0"))

    # T2.3 — resume support. When `resume_from_phase` > 1, look up any
    # decision_phases rows already persisted for this decision_run (from
    # a prior crashed/orphaned run) and surface their phase_output_json
    # as a dict the per-phase code below can short-circuit against.
    # Default-empty when nothing is loaded; safe to read freely.
    resumed_outputs: dict[int, str] = {}
    if resume_from_phase > 1:
        resumed_outputs = _pkg._load_completed_phase_outputs(
            session, decision_run_id=decision_run_id
        )
        log.info(
            "plan_synthesis.resume_loaded",
            user_id=user_id,
            decision_run_id=decision_run_id,
            resume_from_phase=resume_from_phase,
            loaded_phases=sorted(resumed_outputs.keys()),
        )

    # Phase 1: analyst reports.
    # Phases 1-5 receive the string audit token (used for log annotations and
    # agent_reports.decision_id which is a String column). The integer FK is
    # only written to PlanVersion and SynthesisInputs below.
    if 1 in resumed_outputs:
        analyst_reports_text = resumed_outputs[1]
        log.info(
            "plan_synthesis.phase_skipped_resumed",
            user_id=user_id, decision_run_id=decision_run_id, phase=1,
            chars=len(analyst_reports_text),
        )
    else:
        _phase_1_started_at = datetime.now(timezone.utc)
        analyst_reports_text = _pkg._run_phase_1_analysts(
            session=session, user_id=user_id, baseline=baseline,
            prior_current=prior_current, decision_run_id=decision_audit_token,
            guidance=guidance,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=1, started_at=_phase_1_started_at,
            phase_output=analyst_reports_text,
        )
    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_1",
        user_id=user_id,
    )

    # Assemble inputs for Phases 2+.
    portfolio_summary = _pkg._assemble_portfolio_summary(session=session, user_id=user_id)
    fills_summary = _pkg._assemble_fills_summary(session=session, user_id=user_id)

    # Phase 2: per-horizon debates.
    if 2 in resumed_outputs:
        debate_outcomes_text = resumed_outputs[2]
        log.info(
            "plan_synthesis.phase_skipped_resumed",
            user_id=user_id, decision_run_id=decision_run_id, phase=2,
            chars=len(debate_outcomes_text),
        )
    else:
        _phase_2_started_at = datetime.now(timezone.utc)
        debate_outcomes_text = _pkg._run_phase_2_debates(
            session=session, user_id=user_id,
            analyst_reports_text=analyst_reports_text,
            baseline=baseline, prior_current=prior_current,
            decision_run_id=decision_audit_token, trigger=trigger,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=2, started_at=_phase_2_started_at,
            phase_output=debate_outcomes_text,
        )
    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_2",
        user_id=user_id,
    )

    # Wave 3 / Task 3.2: load the per-user speculation cap so we can both
    # (a) tell the synthesizer prompt about it and (b) post-validate the
    # candidates the model emits.  Failure to load a cap is non-fatal —
    # we fall back to ``SpeculationCap()`` defaults (0.1% NW, 3 positions),
    # which the validator still applies.  Speculation caps must NEVER
    # silently disable themselves on a config blip.
    from argosy.config import SpeculationCap, get_user_agent_settings, load_speculation_cap
    try:
        cap = load_speculation_cap(
            user_id=user_id,
            agent_settings=get_user_agent_settings(user_id),
        )
    except Exception as exc:  # noqa: BLE001
        # Logs and events are complementary: the warning lives in stderr /
        # log aggregation for ops, and the structured event lets a UI
        # subscriber raise a user-visible alert ("we couldn't load your
        # speculation cap; reverting to defaults — please review your
        # agent_settings.yaml").  Wave 2 fix I3 introduced
        # ``publish_event_threadsafe`` precisely so synthesis (which can
        # run on a worker thread via monthly_cycle) can emit cleanly.
        log.warning(
            "plan_synthesis.speculation_cap_load_failed_using_default",
            user_id=user_id, error=str(exc),
        )
        _emit_event(
            "plan.synthesis.cap_load_failed",
            {"user_id": user_id, "error": str(exc)},
        )
        cap = SpeculationCap()  # conservative default — never disable the cap.

    # Phase 3: synthesize.
    if 3 in resumed_outputs:
        # Synthesizer output is the structured PlanSynthesisOutput. Round-
        # trip via JSON; pydantic re-validates on parse.
        import json as _json
        output = PlanSynthesisOutput.model_validate(_json.loads(resumed_outputs[3]))
        log.info(
            "plan_synthesis.phase_skipped_resumed",
            user_id=user_id, decision_run_id=decision_run_id, phase=3,
        )
    else:
        _phase_3_started_at = datetime.now(timezone.utc)
        output = _pkg._run_phase_3_synthesizer(
            session=session, user_id=user_id,
            baseline=baseline, prior_current=prior_current,
            analyst_reports_text=analyst_reports_text,
            debate_outcomes_text=debate_outcomes_text,
            portfolio_summary=portfolio_summary,
            fills_summary=fills_summary,
            decision_run_id=decision_audit_token,
            speculation_cap_pct=cap.max_pct_of_net_worth,
            speculation_cap_concurrent=cap.max_concurrent_positions,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=3, started_at=_phase_3_started_at,
            phase_output=output.model_dump_json(),
        )

    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_3",
        user_id=user_id,
    )

    # Defense-in-depth: post-filter speculative candidates that breach
    # the cap or lack ``risk_ceiling_check``.  Resolved via the package
    # namespace so tests that monkeypatch ``flow._enforce_speculation_cap``
    # are honoured.
    output = _pkg._enforce_speculation_cap(
        output,
        max_pct_of_net_worth=cap.max_pct_of_net_worth,
        max_concurrent_positions=cap.max_concurrent_positions,
    )

    # Phase 4: risk team plan-level review.
    if 4 in resumed_outputs:
        risk_verdict = resumed_outputs[4]
        log.info(
            "plan_synthesis.phase_skipped_resumed",
            user_id=user_id, decision_run_id=decision_run_id, phase=4,
        )
    else:
        _phase_4_started_at = datetime.now(timezone.utc)
        risk_verdict = _pkg._run_phase_4_risk(
            session=session, user_id=user_id, draft_output=output,
            analyst_reports_text=analyst_reports_text,
            decision_run_id=decision_audit_token,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=4, started_at=_phase_4_started_at,
            phase_output=risk_verdict,
        )
    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_4",
        user_id=user_id,
    )

    # Phase 5: fund manager integrity check.
    if 5 in resumed_outputs:
        approved = resumed_outputs[5].strip().lower() == "approved"
        log.info(
            "plan_synthesis.phase_skipped_resumed",
            user_id=user_id, decision_run_id=decision_run_id, phase=5,
            approved=approved,
        )
    else:
        _phase_5_started_at = datetime.now(timezone.utc)
        approved = _pkg._run_phase_5_fund_manager(
            session=session, user_id=user_id, draft_output=output,
            risk_verdict=risk_verdict, decision_run_id=decision_audit_token,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=5, started_at=_phase_5_started_at,
            phase_output="approved" if approved else "rejected",
        )
    # W3b.H: when FM rejects, persist the draft anyway (still as 'draft')
    # rather than raising. Without this, every FM rejection forfeits 15-20
    # minutes of analyst+debate+risk reasoning that already lives in the
    # JSONL trail. The persisted PlanVersion gives the UI (GET
    # /api/plan/draft) something to surface; the user reads FM's detailed
    # concerns inline (agent_reports.response_text for the fund_manager
    # row of the same decision_id) and decides whether to accept, reject,
    # or amend. Live runs #5, #6, #10, #13, #16 all hit FM rejection with
    # SUBSTANTIVE reasoning (Section 102 tax sequencing, escalate-not-
    # resolved, ConcentrationAnalyst's bogus null positions, FX low
    # confidence). The user is the final gate, not the FM agent.
    #
    # NOTE: writes role='draft' (not 'draft_rejected') so the existing
    # GET /api/plan/draft endpoint surfaces it. The fm_approved boolean is
    # captured on the audit trail (agent_reports + decision_runs.status).
    if not approved:
        log.warning(
            "plan_synthesis.fm_rejected_persisting_anyway",
            user_id=user_id, decision_run_id=decision_run_id,
        )

    # Persist as role='draft' — UI surfaces it regardless of FM verdict.
    # decision_run_id here is the integer PK — aligns with the Integer FK on
    # plan_versions.decision_run_id and satisfies Postgres type checking.
    inputs = output.inputs.model_copy(update={
        "baseline_id": baseline.id,
        "prior_current_id": prior_current.id if prior_current else None,
        "decision_run_id": decision_run_id,  # int
    })

    draft = PlanVersion(
        user_id=user_id,
        role="draft",
        version_label=(
            f"synth-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}"
            f"{'-fm-rejected' if not approved else ''}"
        ),
        source_path="",
        raw_markdown="",
        decision_run_id=decision_run_id,  # int FK
        derived_from_id=baseline.id,
        horizon_long_json=output.long.model_dump_json(),
        horizon_medium_json=output.medium.model_dump_json(),
        horizon_short_json=output.short.model_dump_json(),
        horizon_long_md=_pkg._horizon_md(output.long),
        horizon_medium_md=_pkg._horizon_md(output.medium),
        horizon_short_md=_pkg._horizon_md(output.short),
        synthesis_inputs_json=inputs.model_dump_json(),
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)

    # Stamp the DecisionRun row as finished — provides the audit lineage
    # SDD §6.11 promises: you can reconstruct the full synthesis by joining
    # plan_versions.decision_run_id → decision_runs.id.
    #
    # Skipped when the caller passed `existing_decision_run_id` (e.g. the
    # plan_amendment_chat large worker): the caller owns the row and may
    # need to re-check cancellation between synthesis-end and the
    # completed stamp. Stamping here would race that check.
    if existing_decision_run_id is None:
        decision_run.finished_at = datetime.now(timezone.utc)
        decision_run.status = "completed"
        decision_run.fund_manager_decision = "approved" if approved else "rejected"
        session.commit()

    # W1.C-v4: ingest the agent_reports forensic trail now that the
    # orchestrator's session has finished its own writes and the writer
    # lock is clean (the session just committed PlanVersion + DecisionRun
    # successfully, so it can write more rows in the same connection).
    # Best-effort — the JSONL stays behind on disk for manual replay via
    # ``argosy synthesis ingest-trail <decision_run_id>`` if this fails.
    try:
        _pkg._ingest_synthesis_trail(session, decision_audit_token)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_synthesis.trail_ingest_exception",
            decision_run_id=decision_run_id, error=str(exc),
        )

    # Invalidate the home-brief cache so the "ready to review" draft bullet
    # surfaces immediately (within the same request cycle) rather than waiting
    # for the 30-minute TTL to expire.  Failure is swallowed — synthesis must
    # never abort because of a flaky cache layer.
    from argosy.adapters.data.cache import invalidate_home_brief
    invalidate_home_brief(user_id)

    log.info("plan_synthesis.draft_persisted",
             user_id=user_id, draft_id=draft.id, decision_run_id=decision_run_id)
    _emit_event("plan.draft.completed", {"user_id": user_id, "draft_id": draft.id})

    # Provenance Wave C — final FM-decision row with the parsed verdict
    # DTO. The 5 per-phase rows (kinds 'synthesis.phase_1'..'phase_5')
    # were already persisted by _record_phase_completion during the
    # flow; this row carries the FundManagerPlanRevisionDecision DTO so
    # the replay UI's VerdictCard renders the approval call.
    try:
        import asyncio
        from argosy.agents.fund_manager import FundManagerPlanRevisionDecision
        from argosy.services.negotiation_recorder import (
            record_negotiation_phase,
        )

        verdict = FundManagerPlanRevisionDecision(
            approved=approved,
            reasons=[
                f"synthesis completed; draft_id={draft.id}",
                f"fund_manager verdict: {'approved' if approved else 'rejected'}",
                f"phase_4 risk verdict text length: {len(risk_verdict)}",
            ],
            cited_sources=["docs/design/SDD.md#§6.11"],
        )
        asyncio.run(record_negotiation_phase(
            user_id=user_id,
            decision_run_id=decision_run_id,
            kind="plan_synthesis.verdict",
            started_at=decision_run.started_at,
            agent_report_ids=[],
            verdict=verdict,
        ))
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "plan_synthesis.record_phase_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )

    return SynthesisResult(decision_run_id=decision_run_id, draft_id=draft.id)


# ----------------------------------------------------------------------
# Phase implementations (default — call existing fleet agents)
# ----------------------------------------------------------------------


# Resolved at call time via the package's sys.modules entry so
# monkeypatch.setattr on the package-level names (used by tests) takes effect.
# Capturing the class refs in a tuple at import time would freeze them and
# bypass the patch.
# Names are alphabetical for deterministic log output.
_PHASE_1_AGENT_NAMES = (
    "ConcentrationAnalystAgent",
    "FxAnalystAgent",
    "FundamentalsAnalystAgent",
    "HouseholdBudgetAnalystAgent",
    "MacroAnalystAgent",
    "NewsAnalystAgent",
    "PlanCritiqueAgent",
    "SentimentAnalystAgent",
    "TaxAnalystAgent",
    "TechnicalAnalystAgent",
)


_CONTROL_PLANE_KWARGS = frozenset({"decision_id", "turn_id", "intake_session_id"})


class _AgentRunResult(NamedTuple):
    """Return shape of ``_safe_run_agent``.

    ``text`` is the JSON-serialised structured output (the existing return
    shape callers concatenate into per-phase report blobs). ``report`` is
    the ``AgentReport`` dataclass produced by ``BaseAgent.run`` — kept
    alongside the text so the phase helper can collect it for a single
    bulk persist at phase boundary (W1.C-v2). ``report`` is ``None`` when
    the agent's ``run_sync`` returned an object that doesn't expose a
    dataclass (test stubs / monkeypatched ``run_sync``) — in that case the
    bulk persist call simply skips it.
    """

    text: str
    report: AgentReport | None


def _persist_agent_reports(
    session: Session, reports: list[AgentReport],
) -> None:
    """Append a phase's AgentReport dataclasses to a JSONL forensic trail.

    W1.C-v4 (lock-avoidance via file IO): rather than fighting the
    SQLite writer lock that the orchestrator's main Session holds for
    the entire synthesis (12-15+ min), we write each phase's reports
    to a per-synthesis JSONL file under
    ``${ARGOSY_HOME}/logs/synthesis/<decision_audit_token>.jsonl``.
    File IO has no SQLite contention; ingest into the DB happens once
    at the END of ``run_synthesis`` via ``_ingest_synthesis_trail``,
    when the orchestrator's session has finished its own writes and
    the writer lock is clean.

    Background — failed approaches:
      * W1.C-v1: per-agent inline async writes inside ``BaseAgent.run``
        → "database is locked" under the 9-way ThreadPool.
      * W1.C-v2: SQLAlchemy sub-session committed at phase boundary
        → still locked because the orchestrator's main Session holds
        the writer.
      * W1.C-v3: raw ``sqlite3`` with 5-minute ``busy_timeout``
        → the lock-holder outlasts ANY reasonable timeout; verified
        live: killing uvicorn instantly releases the lock.

    Idempotent within a synthesis run: each call appends new rows to
    the end of the file (one JSON object per ``AgentReport``). The
    ingest helper reads the full file and writes to the DB in one
    batch via the orchestrator's session.

    Crash-safety: if synthesis dies mid-flight at phase 3, the JSONL
    file still contains phases 1-2 on disk. ``argosy synthesis
    ingest-trail <decision_run_id>`` reads the file and writes the
    rows to the DB after the fact.

    ``session`` parameter is kept (unused) so callers don't change
    shape.
    """
    if not reports:
        return
    _ = session

    from pathlib import Path  # noqa: F401 — kept for type-hint clarity
    from argosy.config import get_settings

    settings = get_settings()
    # All reports in a phase share decision_id (synthesis flow guarantees
    # this — common_kwargs["decision_id"] is the audit token for every
    # phase-1 agent; phase 2 / 4 callers thread it the same way).
    decision_audit_token = getattr(reports[0], "decision_id", None) or "unknown"
    trail_dir = settings.home / "logs" / "synthesis"
    try:
        trail_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "plan_synthesis.trail_dir_mkdir_failed",
            error=str(exc),
        )
        return
    trail_path = trail_dir / f"{decision_audit_token}.jsonl"

    try:
        with trail_path.open("a", encoding="utf-8") as f:
            for r in reports:
                row = {
                    "user_id": r.user_id,
                    "agent_role": r.agent_role,
                    "decision_id": getattr(r, "decision_id", None),
                    "intake_session_id": None,
                    "prompt_hash": r.prompt_hash,
                    "response_text": r.response_text,
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                    "cost_usd": r.cost_usd,
                    "cache_input_tokens": r.cache_input_tokens,
                    "cache_creation_tokens": r.cache_creation_tokens,
                    "thinking_tokens": r.thinking_tokens,
                    "citations_json": r.citations_json,
                    "sources_json": r.sources_json,
                    "run_correlation_id": r.run_correlation_id,
                    "system_prompt": r.system_prompt,
                    "user_prompt": r.user_prompt,
                    "model": r.model,
                    "confidence": r.confidence.value if r.confidence else None,
                }
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        log.info(
            "plan_synthesis.trail_appended",
            count=len(reports),
            trail=str(trail_path.name),
        )
    except OSError as exc:
        log.warning(
            "plan_synthesis.trail_write_failed",
            count=len(reports), error=str(exc),
        )


def _ingest_synthesis_trail(
    session: Session, decision_audit_token: str,
) -> int:
    """Ingest the JSONL forensic trail into ``agent_reports``.

    Called at the end of ``run_synthesis`` after the PlanVersion +
    DecisionRun writes complete (when the orchestrator's session is
    clean for new writes). Uses the orchestrator's session directly —
    which we KNOW can write because it's the connection that's been
    holding the writer lock throughout synthesis.

    Returns the count of rows ingested. Best-effort: any failure logs
    and returns 0. Leaves the JSONL file in place for forensic / replay
    purposes (don't delete it; lets the operator re-ingest manually via
    ``argosy synthesis ingest-trail <decision_run_id>`` if the auto-
    ingest path missed for any reason).

    Returns 0 if the JSONL file doesn't exist (e.g. synthesis with no
    successful agents, or trail-write itself failed earlier).
    """
    from pathlib import Path  # noqa: F401 — kept for type-hint clarity
    from argosy.config import get_settings
    from argosy.state.models import AgentReport as AgentReportRow

    settings = get_settings()
    trail_path = (
        settings.home / "logs" / "synthesis" / f"{decision_audit_token}.jsonl"
    )
    if not trail_path.exists():
        return 0

    rows: list[dict] = []
    try:
        with trail_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    log.warning(
                        "plan_synthesis.trail_line_skipped_bad_json",
                        trail=trail_path.name, error=str(exc),
                    )
    except OSError as exc:
        log.warning(
            "plan_synthesis.trail_read_failed",
            trail=str(trail_path), error=str(exc),
        )
        return 0

    if not rows:
        return 0

    try:
        for row_dict in rows:
            ar = AgentReportRow(**row_dict)
            session.add(ar)
        session.commit()
        log.info(
            "plan_synthesis.trail_ingested",
            count=len(rows), trail=trail_path.name,
        )
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_synthesis.trail_ingest_failed",
            count=len(rows), error=str(exc),
        )
        try:
            session.rollback()
        except Exception:  # pragma: no cover
            pass
        return 0


def _pkg_build_prior_items_index(
    session, *, user_id: str, prior_current,
) -> list[dict]:
    """Flatten {targets, themes, actions, deltas} from prior plans into a
    structured index keyed by item_id.

    Reads:
      - the user's prior_current PlanVersion (if exists)
      - the most-recently-superseded draft for the same user

    Both contribute item_ids the synthesizer should preserve when revising.
    Returns a flat list of dicts: {item_id, item_kind, horizon, label,
    value, unit, from_plan}. Defensive — every plan_version row's JSON is
    try/excepted so a single corrupt row doesn't break synthesis.
    """
    from argosy.state.models import PlanVersion
    from sqlalchemy import desc, select

    seen_ids: set[str] = set()
    out: list[dict] = []

    def _harvest(pv: PlanVersion, source_label: str) -> None:
        for horizon, json_str in (
            ("long", pv.horizon_long_json),
            ("medium", pv.horizon_medium_json),
            ("short", pv.horizon_short_json),
        ):
            if not json_str:
                continue
            try:
                payload = json.loads(json_str)
            except (json.JSONDecodeError, TypeError):
                continue
            for kind_key in ("targets", "themes", "actions"):
                for entry in payload.get(kind_key) or []:
                    if not isinstance(entry, dict):
                        continue
                    # Synthesizer-emitted targets/themes/actions don't
                    # carry an item_id at the top level (only Delta does);
                    # we derive a synthetic id from horizon + kind + label
                    # so the index can surface "before/after" pairs even
                    # when no delta exists between iterations.
                    label = entry.get("label", "") or ""
                    if not label:
                        continue
                    slug = (
                        "".join(
                            c if c.isalnum() else "_"
                            for c in label.lower()
                        ).strip("_")[:40]
                    )
                    if not slug:
                        continue
                    iid = f"{horizon}.{kind_key}.{slug}"
                    if iid in seen_ids:
                        continue
                    seen_ids.add(iid)
                    out.append({
                        "item_id": iid,
                        "item_kind": kind_key.rstrip("s"),  # 'target', 'theme', 'action'
                        "horizon": horizon,
                        "label": label,
                        "value": entry.get("value", ""),
                        "unit": entry.get("unit", ""),
                        "from_plan": source_label,
                    })
            # Deltas DO carry their own item_id — surface those verbatim.
            for delta in payload.get("deltas_from_prior") or []:
                if not isinstance(delta, dict):
                    continue
                iid = delta.get("item_id")
                if not iid or iid in seen_ids:
                    continue
                seen_ids.add(iid)
                proposed = delta.get("proposed") or {}
                if not isinstance(proposed, dict):
                    proposed = {}
                out.append({
                    "item_id": iid,
                    "item_kind": delta.get("item_kind") or "?",
                    "horizon": delta.get("horizon") or horizon,
                    "label": proposed.get("label", delta.get("summary", "")),
                    "value": proposed.get("value", ""),
                    "unit": proposed.get("unit", ""),
                    "from_plan": source_label,
                })

    if prior_current is not None:
        try:
            _harvest(prior_current, f"#{prior_current.id} (current)")
        except Exception as exc:  # noqa: BLE001 — defensive
            log.warning(
                "plan_synthesis.prior_items_harvest_failed",
                source="current", error=str(exc),
            )

    # Also harvest the most-recent superseded draft for this user (rejected
    # drafts contain the synthesizer's most-recent thinking).
    try:
        latest_super = session.execute(
            select(PlanVersion)
            .where(PlanVersion.user_id == user_id)
            .where(PlanVersion.role == "superseded")
            .order_by(desc(PlanVersion.imported_at))
            .limit(1)
        ).scalar_one_or_none()
        if latest_super is not None:
            _harvest(latest_super, f"#{latest_super.id} (last draft)")
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_synthesis.prior_items_harvest_failed",
            source="superseded", error=str(exc),
        )

    return out


def _record_phase_completion(
    *,
    user_id: str,
    decision_run_id: int,
    phase_n: int,
    started_at: datetime,
    phase_output: str,
) -> None:
    """Persist a per-phase output row to ``decision_phases`` (T2.3).

    Synchronous wrapper over the async recorder. Best-effort — failure
    here logs + continues so synthesis isn't broken by a forensic gap.

    The persisted row uses ``kind='synthesis.phase_<N>'`` so the resume
    helper can look it up. ``phase_output`` is opaque text (the phase's
    rendered output): str for analyst/debate/risk/fm phases, JSON dump
    for the synthesizer's structured ``PlanSynthesisOutput``.
    """
    try:
        import asyncio
        from argosy.services.negotiation_recorder import (
            record_negotiation_phase,
        )

        asyncio.run(record_negotiation_phase(
            user_id=user_id,
            decision_run_id=decision_run_id,
            kind=f"synthesis.phase_{phase_n}",
            started_at=started_at,
            agent_report_ids=[],
            verdict=None,
            phase_output=phase_output,
        ))
        log.info(
            "plan_synthesis.phase_recorded",
            user_id=user_id,
            decision_run_id=decision_run_id,
            phase=phase_n,
            output_chars=len(phase_output) if phase_output else 0,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "plan_synthesis.record_phase_failed",
            user_id=user_id, decision_run_id=decision_run_id,
            phase=phase_n, error=str(exc),
        )


def _load_completed_phase_outputs(
    session: Session, *, decision_run_id: int
) -> dict[int, str]:
    """Read previously-persisted phase outputs for a synthesis run (T2.3).

    Returns ``{phase_n: phase_output_json}`` for any
    ``decision_phases`` rows whose ``kind`` matches the
    ``synthesis.phase_<N>`` pattern. Used by ``run_synthesis`` when
    ``resume_from_phase > 1`` to skip already-completed phases instead
    of re-running them.

    Defensive: a partial / corrupt phase row is skipped (not raised).
    Multiple rows for the same phase number return the latest (largest
    seq).
    """
    from argosy.state.models import DecisionPhase
    from sqlalchemy import select

    rows = session.execute(
        select(DecisionPhase)
        .where(DecisionPhase.decision_run_id == decision_run_id)
        .order_by(DecisionPhase.seq.asc())
    ).scalars().all()

    out: dict[int, str] = {}
    for r in rows:
        if not r.kind or not r.kind.startswith("synthesis.phase_"):
            continue
        try:
            phase_n = int(r.kind.split("synthesis.phase_", 1)[1])
        except ValueError:
            continue
        if r.phase_output_json is None:
            continue
        # Later rows (higher seq) for the same phase override earlier ones.
        out[phase_n] = r.phase_output_json
    return out


def _read_synthesis_trail_costs(decision_audit_token: str) -> float:
    """Sum cost_usd across rows in the per-synthesis JSONL forensic trail.

    Returns 0.0 when the trail file doesn't exist yet (first phase still
    in flight, or synthesis was skipped). Best-effort parse: malformed
    lines are skipped rather than failing the whole calc.
    """
    from argosy.config import get_settings as _get_settings

    settings = _get_settings()
    trail = settings.home / "logs" / "synthesis" / f"{decision_audit_token}.jsonl"
    if not trail.exists():
        return 0.0
    total = 0.0
    try:
        with trail.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cost = row.get("cost_usd")
                if isinstance(cost, (int, float)):
                    total += float(cost)
    except OSError:
        return total
    return round(total, 4)


def _check_cost_cap(
    *,
    decision_audit_token: str,
    cost_cap_usd: float,
    phase: str,
    user_id: str,
) -> None:
    """Raise RuntimeError if cumulative agent cost exceeds the soft cap.

    Reads from the JSONL forensic trail rather than the DB so the check
    works mid-synthesis (the DB ingest is deferred to end-of-run per
    W1.C-v4). Fires AFTER each phase completes — by design we don't
    interrupt mid-phase to avoid orphaning Opus calls that were already
    going to charge anyway. Emits a WS event ``plan_synthesis.cost_update``
    on every check so a UI can render the running spend.
    """
    spent = _read_synthesis_trail_costs(decision_audit_token)
    _emit_event(
        "plan_synthesis.cost_update",
        {
            "user_id": user_id,
            "decision_audit_token": decision_audit_token,
            "phase": phase,
            "cost_usd_so_far": spent,
            "cost_cap_usd": cost_cap_usd,
        },
    )
    log.info(
        "plan_synthesis.cost_check",
        user_id=user_id,
        decision_audit_token=decision_audit_token,
        phase=phase,
        cost_usd_so_far=spent,
        cost_cap_usd=cost_cap_usd,
    )
    if spent > cost_cap_usd:
        log.error(
            "plan_synthesis.cost_cap_exceeded",
            user_id=user_id,
            decision_audit_token=decision_audit_token,
            phase=phase,
            cost_usd_so_far=spent,
            cost_cap_usd=cost_cap_usd,
        )
        _emit_event(
            "plan_synthesis.cost_cap_exceeded",
            {
                "user_id": user_id,
                "decision_audit_token": decision_audit_token,
                "phase": phase,
                "cost_usd_so_far": spent,
                "cost_cap_usd": cost_cap_usd,
            },
        )
        raise RuntimeError(
            f"cost_cap_exceeded: spent ${spent:.2f} > cap ${cost_cap_usd:.2f} "
            f"after {phase}. Bump ARGOSY_SYNTHESIS_COST_CAP_USD or "
            f"investigate runaway agent."
        )


def _run_phase_1_analysts(*, session, user_id, baseline, prior_current,
                           decision_run_id, guidance) -> str:
    """Run all 9 analysts in parallel. Concatenate their reports as text.

    W1.B: kwargs are sourced from ``assemble_phase1_inputs`` (W1.A), which
    populates every per-analyst payload up front. ``_safe_run_agent``
    narrows the kwargs per-agent via ``inspect.signature(build_prompt)``
    so each analyst receives only the fields it declares; control-plane
    keys (``decision_id`` etc.) are always passed through.
    """
    log.info("plan_synthesis.phase_1.start",
             user_id=user_id, decision_run_id=decision_run_id)

    # Resolve agent classes via the *package* module (argosy.orchestrator.flows.plan_synthesis)
    # so tests that monkeypatch ``argosy.orchestrator.flows.plan_synthesis.<Name>`` are
    # honoured.  We cannot use sys.modules[__name__] here because __name__ is
    # the submodule (…plan_synthesis.orchestrator), not the package.
    _pkg_mod = sys.modules["argosy.orchestrator.flows.plan_synthesis"]
    phase_1_agents = tuple(getattr(_pkg_mod, name) for name in _PHASE_1_AGENT_NAMES)

    # W1.A produces every per-analyst payload up front (positions_summary,
    # plan_targets, fx_payload, tickers, fundamentals/news/social/indicators
    # payloads, macro_snapshot, lots/dividends/RSU summaries, plan_label
    # /markdown, snapshot_label/summary, user_context_yaml, domain_kb_files,
    # recent_events). The orchestrator hands the full bag to _safe_run_agent
    # which narrows it per-agent via inspect.signature so each analyst
    # receives only the kwargs its build_prompt declares.
    #
    # decision_run_id here is the *string audit token* (e.g. "plan-synth-42"),
    # threaded in at orchestrator.py:136 as ``decision_audit_token``. We pass
    # it positionally to assemble_phase1_inputs so the inputs helper stamps
    # snapshot_label with the same token.
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        assemble_phase1_inputs,
    )

    inputs = assemble_phase1_inputs(
        session,
        user_id=user_id,
        baseline=baseline,
        prior_current=prior_current,
        decision_audit_token=decision_run_id,
    )

    # Build the kwargs bag from the dataclass plus the control-plane key.
    # ``decision_id`` is consumed by BaseAgent.run (pops it before
    # build_prompt) — it must survive _safe_run_agent's narrowing, hence
    # _CONTROL_PLANE_KWARGS below.
    from dataclasses import asdict as _asdict

    common_kwargs: dict = _asdict(inputs)
    common_kwargs["decision_id"] = decision_run_id

    reports: list[str] = []
    collected: list[AgentReport] = []
    with ThreadPoolExecutor(max_workers=len(phase_1_agents)) as ex:
        futures = {
            ex.submit(_safe_run_agent, AgentCls, user_id, common_kwargs, decision_run_id): AgentCls
            for AgentCls in phase_1_agents
        }
        for fut in as_completed(futures):
            cls = futures[fut]
            try:
                result = fut.result()
                reports.append(f"=== {cls.__name__} ===\n{result.text}")
                if result.report is not None:
                    collected.append(result.report)
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_1.agent_failed",
                          agent=cls.__name__, error=str(exc),
                          decision_run_id=decision_run_id)
                # Failure of one analyst is recoverable — continue with
                # the others. Note in the concatenated text so the
                # synthesizer knows.
                reports.append(f"=== {cls.__name__} (FAILED) ===\n{exc}")

    # W1.C-v2 — single-writer batch persist at phase boundary. Resolved
    # via the package namespace so tests that monkeypatch
    # ``flow._persist_agent_reports`` are honoured.
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    _pkg._persist_agent_reports(session, collected)

    log.info("plan_synthesis.phase_1.done",
             user_id=user_id, decision_run_id=decision_run_id,
             reports_count=len(reports),
             persisted_count=len(collected))
    return "\n\n".join(reports)


def _safe_run_agent(AgentCls, user_id: str, kwargs: dict,
                    decision_run_id: str) -> _AgentRunResult:
    """Instantiate an analyst, run it, return ``(text, report)``.

    Return shape (W1.C-v2): ``_AgentRunResult(text, report)`` where
    ``text`` is the JSON of the structured output (existing contract for
    callers that concatenate into per-phase blobs) and ``report`` is the
    ``AgentReport`` dataclass produced by ``BaseAgent.run``. The caller's
    phase helper collects the ``report`` and hands the list to
    ``_persist_agent_reports`` once at phase end. ``report`` is ``None``
    when the agent's ``run_sync`` returned an object that isn't an
    ``AgentReport`` (test stubs that build ``SimpleNamespace`` payloads).

    ADAPTATION (vs spec): BaseAgent.__init__ takes a mandatory ``user_id``
    keyword — the spec wrote ``AgentCls()`` which would raise on any real
    agent. We try ``AgentCls(user_id=user_id)`` first and fall back to
    ``AgentCls()`` for stubs/tests whose constructors don't accept it.

    W1.B: narrow kwargs *upfront* via ``inspect.signature(agent.build_prompt)``
    so the first run_sync call already passes only what the agent declares.
    Control-plane keys (``decision_id``, ``turn_id``, ``intake_session_id``)
    are exempt from narrowing — BaseAgent.run pops them before calling
    build_prompt. Falls back to the legacy "pass-all-then-narrow-on-TypeError"
    path for stub agents whose build_prompt signature inspection misbehaves.
    """
    try:
        agent = AgentCls(user_id=user_id)
    except TypeError:
        agent = AgentCls()

    # Narrow upfront: keep only keys the agent's build_prompt declares,
    # plus the control-plane keys BaseAgent.run consumes. If the agent's
    # build_prompt has VAR_KEYWORD (**kwargs), pass everything — it can
    # accept the full bag without TypeError. Stub agents in tests may
    # not define build_prompt at all (they override run_sync directly);
    # in that case we pass the full bag and let the stub's run_sync
    # ignore what it doesn't need.
    try:
        bp = getattr(agent, "build_prompt", None)
        if bp is None:
            narrowed = dict(kwargs)
        else:
            sig = inspect.signature(bp)
            params = sig.parameters
            has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if has_var_keyword:
                narrowed = dict(kwargs)
            else:
                accepted = set(params.keys()) | _CONTROL_PLANE_KWARGS
                narrowed = {k: v for k, v in kwargs.items() if k in accepted}
    except (TypeError, ValueError):
        # Signature introspection failed (rare; e.g. a C-implemented stub).
        # Fall back to passing the full bag and rely on the TypeError-retry
        # below.
        narrowed = dict(kwargs)

    try:
        result = agent.run_sync(**narrowed)
    except TypeError:
        # Defensive fallback: re-narrow against the live signature in case
        # the agent's build_prompt was monkeypatched between introspection
        # and the call. Wrap the re-inspect itself in try/except — if the
        # agent has no build_prompt at all, or introspection fails again,
        # fall back to passing only the control-plane keys.
        try:
            sig = inspect.signature(agent.build_prompt)
            accepted = set(sig.parameters.keys()) | _CONTROL_PLANE_KWARGS
        except (AttributeError, TypeError, ValueError):
            accepted = set(_CONTROL_PLANE_KWARGS)
        narrowed = {k: v for k, v in kwargs.items() if k in accepted}
        result = agent.run_sync(**narrowed)

    out = getattr(result, "output", None)
    if out is not None and hasattr(out, "model_dump_json"):
        text = out.model_dump_json()
    else:
        text = str(out) if out is not None else ""
    # Real BaseAgent.run returns the ``AgentReport`` dataclass directly;
    # test stubs that build a ``SimpleNamespace`` won't pass isinstance.
    report = result if isinstance(result, AgentReport) else None
    return _AgentRunResult(text=text, report=report)


def _run_phase_2_debates(*, session, user_id, analyst_reports_text,
                         baseline, prior_current, decision_run_id, trigger) -> str:
    """Run bull/bear/facilitator across all three horizons in parallel.

    Each horizon argues theses, not trades. Per-horizon facilitator
    extracts a structured DebateOutcome record.

    W1.C-v2: per-horizon helper now returns ``(text, reports)``; this
    function collects all reports across the three horizons and
    bulk-persists once at phase end (single sync writer, no aiosqlite
    contention).
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    log.info("plan_synthesis.phase_2.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    collected: list[AgentReport] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(
                _pkg._run_one_horizon_debate,
                horizon=h,
                user_id=user_id,
                analyst_reports_text=analyst_reports_text,
                baseline=baseline,
                prior_current=prior_current,
                decision_run_id=decision_run_id,
                trigger=trigger,
            ): h for h in ("long", "medium", "short")
        }
        for fut in as_completed(futures):
            horizon = futures[fut]
            try:
                result = fut.result()
                # _run_one_horizon_debate may return either the legacy
                # str shape (test stubs via monkeypatch) or the new
                # (text, reports) tuple. Detect via isinstance.
                if isinstance(result, tuple) and len(result) == 2:
                    outcome_text, horizon_reports = result
                    collected.extend(
                        r for r in horizon_reports if r is not None
                    )
                else:
                    outcome_text = result
                parts.append(f"=== Debate outcome — {horizon} ===\n{outcome_text}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_2.debate_failed",
                          horizon=horizon, decision_run_id=decision_run_id,
                          error=str(exc))
                parts.append(f"=== Debate outcome — {horizon} (FAILED) ===\n{exc}")

    # W1.C-v2 batch persist for phase 2 (9 agents max: 3 horizons ×
    # bull/bear/facilitator). Routed through the package namespace so a
    # test patching ``flow._persist_agent_reports`` is honoured.
    _pkg._persist_agent_reports(session, collected)

    return "\n\n".join(parts)


def _run_one_horizon_debate(*, horizon: str, user_id: str,
                             analyst_reports_text: str,
                             baseline, prior_current, decision_run_id: str,
                             trigger: str) -> tuple[str, list[AgentReport]]:
    """Run bull/bear/facilitator for one horizon.

    Reuses the existing argosy.agents.researcher and researcher_facilitator
    modules. The horizon shapes the prompt's question:
      - long: "do principles + targets still hold?"
      - medium: "tactical posture for next 1-2 years?"
      - short: "specific calls for next 30 days?"

    ADAPTATIONS vs spec:
      * Spec used a single `ResearcherAgent(stance=...)` class; the actual
        codebase exposes `BullResearcherAgent` and `BearResearcherAgent`
        as concrete subclasses (no `stance` kwarg).
      * `BaseAgent.__init__` requires a `user_id` keyword — added to this
        function's signature and threaded through from
        `_run_phase_2_debates`.
      * The researcher `build_prompt` signature is
        `(analyst_reports: list[dict], prior_rounds, round_index, n_max,
        ticker)` — NOT `(question, analyst_reports, round_n, round_max,
        opposing)`. We embed the per-horizon question into the analyst
        reports payload as a `_horizon_prompt` entry so it reaches the
        model, and wrap the concatenated text in a single dict (the
        agents' build_prompt iterates `analyst_reports`). The cross-side
        rebuttal is conveyed via `prior_rounds` (the bear sees the bull's
        round 1 turn).
      * The facilitator `build_prompt` signature is
        `(bull_turns, bear_turns, rounds_run, ticker)` — not
        `(question, bull, bear)`. We pass single-element lists.
    """
    from argosy.agents.researcher import (
        BearResearcherAgent,
        BullResearcherAgent,
    )
    from argosy.agents.researcher_facilitator import ResearcherFacilitatorAgent

    horizon_question = {
        "long": "Do the durable principles and 5+ year targets still hold?",
        "medium": (
            "Given the analyst reports and current state, what tactical "
            "posture should drive the next 1-2 years? Specific targets and "
            "themed actions; this is the strategic centerpiece."
        ),
        "short": (
            "What specific calls for the next 30 days? Defer or pull "
            "anything forward? Speculative candidates worth surfacing?"
        ),
    }[horizon]

    # The researcher build_prompt iterates analyst_reports as a list[dict];
    # we package the concatenated upstream text plus the horizon question
    # into a single synthetic report dict.
    analyst_reports_payload = [{
        "agent_role": "phase_1_aggregated",
        "horizon": horizon,
        "horizon_question": horizon_question,
        "report_text": analyst_reports_text,
    }]
    ticker = f"plan-{horizon}"

    bull = BullResearcherAgent(user_id=user_id)
    bear = BearResearcherAgent(user_id=user_id)
    fac = ResearcherFacilitatorAgent(user_id=user_id)

    bull_report = bull.run_sync(
        analyst_reports=analyst_reports_payload,
        prior_rounds=[],
        round_index=1,
        n_max=2,
        ticker=ticker,
        decision_id=decision_run_id,
    )
    bull_turn = bull_report.output if hasattr(bull_report, "output") else None
    bull_turn_dict = bull_turn.model_dump() if bull_turn is not None else {}

    bear_report = bear.run_sync(
        analyst_reports=analyst_reports_payload,
        prior_rounds=[bull_turn_dict] if bull_turn_dict else [],
        round_index=1,
        n_max=2,
        ticker=ticker,
        decision_id=decision_run_id,
    )
    bear_turn = bear_report.output if hasattr(bear_report, "output") else None
    bear_turn_dict = bear_turn.model_dump() if bear_turn is not None else {}

    fac_report = fac.run_sync(
        bull_turns=[bull_turn_dict] if bull_turn_dict else [],
        bear_turns=[bear_turn_dict] if bear_turn_dict else [],
        rounds_run=1,
        ticker=ticker,
        decision_id=decision_run_id,
    )
    out = fac_report.output if hasattr(fac_report, "output") else fac_report
    text = out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
    # W1.C-v2: hand the AgentReport dataclasses back to the phase helper
    # for a single bulk persist at phase boundary. Test stubs return
    # SimpleNamespace which isn't an AgentReport — those are filtered out
    # here (and again defensively inside _persist_agent_reports).
    collected: list[AgentReport] = [
        r for r in (bull_report, bear_report, fac_report)
        if isinstance(r, AgentReport)
    ]
    return text, collected


def _run_phase_3_synthesizer(*, session, user_id, baseline, prior_current,
                             analyst_reports_text, debate_outcomes_text,
                             portfolio_summary, fills_summary,
                             decision_run_id,
                             speculation_cap_pct: float | None = None,
                             speculation_cap_concurrent: int | None = None,
                             ) -> PlanSynthesisOutput:
    """Default Phase 3: call PlanSynthesizerAgent.

    ``speculation_cap_pct`` / ``speculation_cap_concurrent`` (Wave 3, Task
    3.2): when set, the synthesizer prompt includes a HARD CONSTRAINT
    block telling the model to keep speculative_candidates within those
    bounds.  Defense-in-depth: ``_enforce_speculation_cap`` re-validates
    after the model returns, so a model that fluffs the constraint cannot
    harm the user.  Both kwargs default to None for backwards compat with
    tests / call sites that don't load the cap.
    """
    # ADAPTATION: spec wrote PlanSynthesizerAgent() but BaseAgent.__init__
    # requires user_id as a mandatory keyword argument.
    agent = PlanSynthesizerAgent(user_id=user_id)
    baseline_md = baseline.distillate_rendered or "(no distillate available)"
    prior_md = ""
    if prior_current:
        prior_md = "\n\n".join(filter(None, [
            prior_current.horizon_long_md,
            prior_current.horizon_medium_md,
            prior_current.horizon_short_md,
        ]))
    # T4.8a — build the prior-items index from both prior_current AND
    # the most-recent superseded draft (if any). The synthesizer uses
    # this to preserve item_id when revising the same logical item.
    prior_items_index = _pkg_build_prior_items_index(
        session, user_id=user_id, prior_current=prior_current,
    )
    result = agent.run_sync(
        baseline_distillate_md=baseline_md,
        prior_current_md=prior_md,
        analyst_reports_text=analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
        speculation_cap_pct=speculation_cap_pct,
        speculation_cap_concurrent=speculation_cap_concurrent,
        prior_items_index=prior_items_index,
        decision_id=decision_run_id,
    )
    # W1.C-v2: single-agent phase still uses the uniform bulk-persist
    # pattern (one-element list) so every synthesis phase writes to
    # ``agent_reports`` via the same code path. Routed through the
    # package namespace so a test patching ``flow._persist_agent_reports``
    # is honoured. Stub agents return SimpleNamespace; the isinstance
    # guard in _persist_agent_reports filters those out.
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    if isinstance(result, AgentReport):
        _pkg._persist_agent_reports(session, [result])
    return result.output  # type: ignore[attr-defined]


def _enforce_speculation_cap(
    output: PlanSynthesisOutput,
    *,
    max_pct_of_net_worth: float,
    max_concurrent_positions: int,
) -> PlanSynthesisOutput:
    """Drop any speculative candidates that breach the cap.

    The synthesizer prompt is told the cap, but defense-in-depth: we
    enforce it here so a model that fluffs the constraint cannot harm
    the user.

    Drops candidates that:
      - exceed ``max_pct_of_net_worth``
      - lack ``risk_ceiling_check`` (i.e. the synthesizer didn't
        affirmatively confirm the position fits the user's bound)
    Also enforces the concurrent-position limit by truncating after
    ``max_concurrent_positions`` survivors (deterministic order — first
    candidates emitted by the model win).
    """
    if not output.short.speculative_candidates:
        return output

    kept = []
    for c in output.short.speculative_candidates:
        if c.suggested_position_pct_of_net_worth > max_pct_of_net_worth:
            log.warning(
                "plan_synthesis.speculative_dropped_over_cap",
                ticker=c.ticker,
                pct=c.suggested_position_pct_of_net_worth,
                cap=max_pct_of_net_worth,
            )
            continue
        if not c.risk_ceiling_check:
            log.warning(
                "plan_synthesis.speculative_dropped_no_ceiling_check",
                ticker=c.ticker,
            )
            continue
        kept.append(c)
        if len(kept) >= max_concurrent_positions:
            break

    if len(kept) == len(output.short.speculative_candidates):
        return output
    new_short = output.short.model_copy(update={"speculative_candidates": kept})
    return output.model_copy(update={"short": new_short})


def _run_phase_4_risk(*, session, user_id, draft_output: PlanSynthesisOutput,
                      analyst_reports_text: str, decision_run_id: str) -> str:
    """Plan-level risk verdict from three perspectives + facilitator merge.

    Runs the aggressive / neutral / conservative ``RiskOfficerAgent`` in
    parallel (one verdict per perspective) and then asks the
    ``RiskFacilitatorAgent`` to consolidate. Returns a single text blob
    that the synthesizer's downstream steps embed in the draft transcript.

    ADAPTATIONS vs spec:
      * Spec used ``RiskOfficerAgent(stance=...)``; the actual class is
        constructed as ``RiskOfficerAgent(user_id=..., perspective=...)``
        (kwarg renamed; ``user_id`` now mandatory). ``_make_risk_officer``
        normalises the kwarg.
      * Spec called the officer with ``run_sync(draft_plan=...,
        analyst_reports=...)``; the actual ``build_prompt`` signature is
        ``(proposal, analyst_reports: list[dict], user_constraints,
        risk_caps, prior_rounds, round_index, n_max)``. We adapt by
        passing the draft plan as the ``proposal`` dict and wrapping the
        upstream concatenated text in a single synthetic analyst report
        dict (mirroring the Phase-2 adaptation).
      * Spec called the facilitator with ``run_sync(draft_plan=...,
        risk_reviews=...)``; the actual ``build_prompt`` signature is
        ``(verdicts: list[dict], rounds_run: int)``. We parse each
        per-perspective JSON output back into a dict and pass it through;
        on parse failure we fall back to a sentinel dict so the
        facilitator at least sees the perspective.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    log.info("plan_synthesis.phase_4.start",
             user_id=user_id, decision_run_id=decision_run_id)

    parts: list[str] = []
    raw_outputs: dict[str, str] = {}
    collected: list[AgentReport] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(_run_one_risk_perspective,
                      stance=stance, user_id=user_id,
                      draft_output=draft_output,
                      analyst_reports_text=analyst_reports_text,
                      decision_run_id=decision_run_id): stance
            for stance in ("aggressive", "neutral", "conservative")
        }
        for fut in as_completed(futures):
            stance = futures[fut]
            try:
                result = fut.result()
                # W1.C-v2: _run_one_risk_perspective now returns
                # (text, report). Handle the legacy str shape too in case
                # a test stub returns a plain string.
                if isinstance(result, tuple) and len(result) == 2:
                    payload, report = result
                    if report is not None:
                        collected.append(report)
                else:
                    payload = result
                raw_outputs[stance] = payload
                parts.append(f"=== Risk {stance} ===\n{payload}")
            except Exception as exc:  # noqa: BLE001
                log.error("plan_synthesis.phase_4.risk_failed",
                          stance=stance, decision_run_id=decision_run_id,
                          error=str(exc))
                parts.append(f"=== Risk {stance} (FAILED) ===\n{exc}")

    # Facilitator merge.
    from argosy.agents.risk_facilitator import RiskFacilitatorAgent
    try:
        facilitator = RiskFacilitatorAgent(user_id=user_id)
    except TypeError:
        # Fallback for stubbed/legacy constructors that don't accept user_id.
        facilitator = RiskFacilitatorAgent()  # type: ignore[call-arg]

    # Build a list[dict] of verdicts for the real facilitator signature.
    # Best-effort JSON parse; on failure the facilitator still sees the
    # perspective so it can ESCALATE rather than silently drop the voice.
    verdicts: list[dict] = []
    for stance, raw in raw_outputs.items():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                parsed.setdefault("perspective", stance)
                parsed.setdefault("round_index", 1)
                verdicts.append(parsed)
            else:
                verdicts.append({"perspective": stance, "round_index": 1,
                                 "verdict": "ESCALATE", "raw": raw})
        except (ValueError, TypeError):
            verdicts.append({"perspective": stance, "round_index": 1,
                             "verdict": "ESCALATE", "raw": raw})

    try:
        merged = facilitator.run_sync(
            verdicts=verdicts,
            rounds_run=1,
            decision_id=decision_run_id,
        )
        if isinstance(merged, AgentReport):
            collected.append(merged)
        merged_out = getattr(merged, "output", merged)
        merged_text = (
            merged_out.model_dump_json()
            if hasattr(merged_out, "model_dump_json") else str(merged_out)
        )
        parts.append(f"=== Risk facilitator verdict ===\n{merged_text}")
    except Exception as exc:  # noqa: BLE001
        log.error("plan_synthesis.phase_4.facilitator_failed",
                  decision_run_id=decision_run_id, error=str(exc))
        parts.append(f"=== Risk facilitator (FAILED) ===\n{exc}")

    # W1.C-v2 batch persist for phase 4 (3 officers + 1 facilitator).
    # Routed through the package namespace so a test patching
    # ``flow._persist_agent_reports`` is honoured.
    _pkg._persist_agent_reports(session, collected)

    return "\n\n".join(parts)


def _make_risk_officer(stance: str, *, user_id: str | None = None):
    """Return a ``RiskOfficerAgent`` configured for the requested stance.

    ADAPTATION: the real ``RiskOfficerAgent.__init__`` signature is
    ``(user_id: str, perspective: Perspective)`` — the spec used a
    ``stance`` kwarg with no ``user_id``. We accept ``stance`` as a
    positional argument (to match the test stub
    ``_fake_officer(stance)``) and translate to ``perspective``;
    ``user_id`` is keyword-only because the test monkeypatches this
    helper and never passes one.
    """
    from argosy.agents.risk_officer import Perspective, RiskOfficerAgent
    return RiskOfficerAgent(user_id=user_id or "system", perspective=cast(Perspective, stance))


def _run_one_risk_perspective(*, stance: str, user_id: str,
                              draft_output: PlanSynthesisOutput,
                              analyst_reports_text: str,
                              decision_run_id: str
                              ) -> tuple[str, AgentReport | None]:
    """Run one risk-officer perspective and return ``(text, report)``.

    W1.C-v2: returns the AgentReport alongside the text so the phase
    helper can collect it for bulk persist at phase boundary.

    ADAPTATION: ``_make_risk_officer`` is a documented monkeypatch seam
    used by tests; the spec test's stub has signature ``(stance)`` only,
    so we attempt the keyword-arg path (``user_id``) first and fall
    back to the bare positional form when the patched stub doesn't
    accept it. Mirrors the ``_safe_run_agent`` retry pattern used in
    Phase 1.

    Resolves ``_make_risk_officer`` through the package namespace so that
    ``monkeypatch.setattr(flow, "_make_risk_officer", ...)`` intercepts
    the call.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    try:
        officer = _pkg._make_risk_officer(stance, user_id=user_id)
    except TypeError:
        officer = _pkg._make_risk_officer(stance)

    # Real RiskOfficerAgent.build_prompt expects:
    #   proposal: dict, analyst_reports: list[dict], user_constraints: str,
    #   risk_caps: dict, prior_rounds: list[dict], round_index: int, n_max: int
    # The test stub accepts **kw, so it ignores anything we pass.
    try:
        proposal = json.loads(draft_output.model_dump_json())
    except (ValueError, TypeError):
        proposal = {"raw": str(draft_output)}

    analyst_reports_payload = [{
        "agent_role": "phase_1_aggregated",
        "report_text": analyst_reports_text,
    }]

    result = officer.run_sync(
        proposal=proposal,
        analyst_reports=analyst_reports_payload,
        user_constraints="",
        risk_caps={},
        prior_rounds=[],
        round_index=1,
        n_max=1,
        decision_id=decision_run_id,
    )
    out = getattr(result, "output", result)
    text = out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
    report = result if isinstance(result, AgentReport) else None
    return text, report


def _make_fund_manager(user_id: str | None = None):
    """Factory seam for the fund manager agent.

    Accepts an optional ``user_id`` so the real audit trail names the
    requesting user; falls back to ``"system"`` when not provided (e.g.
    scheduled runs or test monkeypatches).

    Test stubs use ``lambda *args, **kw: _FakeFM(...)`` so the optional
    argument is safely ignored without breaking the call site.

    ADAPTATION vs spec: ``BaseAgent.__init__`` requires ``user_id`` as a
    mandatory keyword (see Tasks 2.5/2.7-2.9), so ``FundManagerAgent()``
    with no args from the spec would TypeError. We pass a sentinel when
    no real user is available.
    """
    from argosy.agents.fund_manager import FundManagerAgent
    return FundManagerAgent(user_id=user_id or "system")


def _run_phase_5_fund_manager(*, session, user_id,
                              draft_output: PlanSynthesisOutput,
                              risk_verdict: str, decision_run_id: str) -> bool:
    """Final integrity check.

    Validates:
      - distillate hard-constraints honored
      - three horizons cohere
      - every target has rationale + cited source
      - 'no_change' justified by evidence if claimed

    Returns True to green-light the draft, False to reject.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    log.info("plan_synthesis.phase_5.start",
             user_id=user_id, decision_run_id=decision_run_id)
    fm = _pkg._make_fund_manager(user_id=user_id)
    result = fm.run_sync(
        decision_kind="plan_revision",
        draft_plan=draft_output.model_dump_json(),
        risk_verdict=risk_verdict,
        decision_id=decision_run_id,
    )
    # W1.C-v2: uniform bulk-persist pattern. Phase 5 calls exactly one
    # agent; wrap its dataclass in a 1-element list and route through
    # the package namespace. Stub agents return SimpleNamespace; only
    # real AgentReport instances are persisted.
    if isinstance(result, AgentReport):
        _pkg._persist_agent_reports(session, [result])
    out = result.output

    # The plan-revision path validates against FundManagerPlanRevisionDecision
    # which has a typed `approved` bool — read it directly when available.
    # Fall back to JSON parsing for test stubs that return generic objects.
    if hasattr(out, "approved"):
        approved = bool(out.approved)
    else:
        payload_text = out.model_dump_json() if hasattr(out, "model_dump_json") else str(out)
        try:
            payload = json.loads(payload_text)
            approved = bool(payload.get("approved", False))
        except (ValueError, TypeError):
            log.error("plan_synthesis.phase_5.payload_unparseable",
                      decision_run_id=decision_run_id, payload=payload_text)
            return False

    log.info("plan_synthesis.phase_5.verdict",
             user_id=user_id, decision_run_id=decision_run_id,
             approved=approved)
    return approved
