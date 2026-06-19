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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple, cast

from sqlalchemy.orm import Session

from argosy.agents.base import AgentReport
from argosy.agents.errors import AgentRunError
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
    IncompleteFleetError,
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


# Prime directive injected into the coherence arbitrator/panel when the
# deliberation reconcile path settles a goal/framing tension (mirrors the
# project prime directive in CLAUDE.md / auto-memory).
_COHERENCE_PRIME_DIRECTIVE = (
    "Maximize the family's financial position and secure the earliest safe "
    "retirement; conservatism that costs retirement years is the anti-goal."
)


# T2.6b — orchestrator-level retry budget for bear_researcher.
#
# Belt-and-suspenders on top of BaseAgent's internal N=3 retry envelope
# (see `argosy/agents/base.py::_call_via_claude_code_inner` retry loop).
# BaseAgent retries *within* a single SDK session; this retry restarts the
# whole call (fresh subprocess, fresh prompt build) so corruption that
# survives the SDK's recovery (rare but observed: synthesis #29 lost both
# the medium AND long horizons to back-to-back bear_researcher exit-1
# flakes that escaped the SDK layer as AgentRunError, killing 6 of 9
# phase-2 reports). Live evidence in `logs/app/application.log` around
# 2026-05-26T22:39 UTC.
#
# Scope: bear_researcher only. Bull failed visibly less in production
# (likely because its prompt is shorter / always runs first / doesn't have
# the "rebut the bull's turn" context inflation). If bull starts flaking
# at a comparable rate we'll widen this. Facilitator is short / synthesis-
# style and has not been observed to hit the exit-1 flake.
_BEAR_RESEARCHER_MAX_ATTEMPTS = 3
_BEAR_RESEARCHER_RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)  # per attempt index 0,1,2


def _is_bear_transient_flake(exc: BaseException) -> bool:
    """Return True iff ``exc`` is the AgentRunError fingerprint of a
    claude.exe exit-1 flake (empty stderr) for which a fresh retry is
    likely to succeed.

    Mirrors `BaseAgent._call_via_claude_code_inner`'s `_has_exit1_signature`
    so the orchestrator retry only fires on the SAME failure mode the
    SDK-level retry handles — not on deterministic failures (validation
    errors, citation-gate misses, etc.) that would just burn cost on
    repeat. The check is by error-string signature because the
    AgentRunError wraps the SDK exception and erases its concrete class.

    Word-boundary regex on "exit code 1" prevents false-positives like
    "exit code 137" (SIGKILL) and "exit code 127" (CLI not found) which
    have different root causes.
    """
    s = str(exc)
    has_exit1_sig = bool(
        re.search(r"\bexit code 1\b", s)
        or "(exit code: 1)" in s
    )
    has_empty_stderr = "[claude.exe stderr was empty]" in s or (
        # Some older error strings (pre f4b2dce) omitted the stderr
        # tail entirely when stderr_lines was empty. Treat the
        # SDK's placeholder "Check stderr output for details" as the
        # same fingerprint when no concrete stderr text follows.
        "Check stderr output for details" in s
        and "[claude.exe stderr]" not in s
    )
    return has_exit1_sig and has_empty_stderr


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

    # T0.3 — reset the per-synthesis adapter-outcome buffer up front so
    # every adapter call recorded during this run lands in a clean
    # contextvar list. The buffer is contextvars-scoped, so two concurrent
    # synthesis runs on different asyncio tasks don't stomp on each other.
    from argosy.services.adapter_outcomes import reset_outcomes
    reset_outcomes()

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

    # Idempotency: demote any existing draft — but defer the actual
    # role flip to commit time when the new draft is being written
    # (see below in this function). The previous implementation
    # demoted right here, before the 30-60 min synthesis ran; on any
    # phase failure the user ended up with no pending draft and no
    # successor (real incident: decision_run #43, 2026-05-30 — draft
    # #14 stranded as role='superseded' with no successor for ~24h).
    # See the same fix in plan_amendment/workers.py.

    # T2.1 — soft cost cap per synthesis run. Read from env so it can be
    # bumped without code edits ($10 default). When the cumulative cost
    # of persisted agent_reports exceeds the cap, we abort with an
    # explicit RuntimeError after the current phase finishes (we don't
    # interrupt mid-phase to avoid orphaning in-flight Opus calls that
    # were already going to charge).
    import os as _os

    cost_cap_usd = float(_os.environ.get("ARGOSY_SYNTHESIS_COST_CAP_USD", "20.0"))

    # DERIVATION-FIRST: prepend LOCKED DERIVED FACTS to the synthesizer guidance so the
    # synthesizer USES the team-derived numbers (NVDA deconcentration target/sell + FI
    # margin on the honest liquid basis) instead of INHERITING a cadence/target from the
    # baseline doc (the ``3,000 sh/yr`` class — past behavior laundered via a citation).
    # Best-effort + flag-gated (default ON); a failure no-ops (the fail-closed promote
    # gate is the separate backstop). Disabled with ARGOSY_DERIVED_FACTS=0.
    if _os.environ.get("ARGOSY_DERIVED_FACTS", "1") == "1":
        try:
            from argosy.services.derived_facts import (
                build_derived_facts, render_derived_facts_guidance,
            )
            _derived_block = render_derived_facts_guidance(
                build_derived_facts(
                    session, user_id=user_id, decision_run_id=decision_run_id,
                )
            )
            if _derived_block:
                guidance = (_derived_block + "\n\n" + (guidance or "")).strip()
                log.info(
                    "plan_synthesis.derived_facts_injected",
                    user_id=user_id, decision_run_id=decision_run_id,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never abort synthesis
            log.warning(
                "plan_synthesis.derived_facts_failed",
                user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
            )

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
        # Treat ``resume_from_phase`` as a true "re-run from here" boundary:
        # only REUSE phases strictly below it; everything at/after re-runs
        # with the (possibly new) guidance. Without this filter every
        # already-completed phase was reused regardless, so resuming a
        # FULLY-completed run (e.g. to fold the Fund Manager's objections
        # back in from phase 3 — reusing the expensive phase-1 analysts +
        # phase-2 debates) re-ran nothing. For crash-recovery the route
        # passes resume_from_phase = max(completed)+1, so every completed
        # phase is already below the boundary → this filter is a no-op
        # there. See post_check_in_resume.
        resumed_outputs = {
            k: v for k, v in resumed_outputs.items() if k < resume_from_phase
        }
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
        # T0.3 — phase 1's persisted payload is a JSON-encoded dict carrying
        # ``analyst_reports_text`` + ``adapter_outcomes``. Older synthesis
        # runs persisted the raw text instead, so fall back to treating
        # the column value as the text directly when JSON parsing fails.
        _raw_p1 = resumed_outputs[1]
        try:
            _parsed_p1 = json.loads(_raw_p1)
            if isinstance(_parsed_p1, dict) and "analyst_reports_text" in _parsed_p1:
                analyst_reports_text = _parsed_p1["analyst_reports_text"]
            else:
                analyst_reports_text = _raw_p1
        except (json.JSONDecodeError, TypeError):
            analyst_reports_text = _raw_p1
        log.info(
            "plan_synthesis.phase_skipped_resumed",
            user_id=user_id, decision_run_id=decision_run_id, phase=1,
            chars=len(analyst_reports_text),
        )
    else:
        _phase_1_started_at = datetime.now(timezone.utc)
        _phase_1_result = _pkg._run_phase_1_analysts(
            session=session, user_id=user_id, baseline=baseline,
            prior_current=prior_current, decision_run_id=decision_audit_token,
            guidance=guidance,
        )
        # T0.1 — phase 1 returns (text, reports, failed_roles). Detect the
        # tuple shape for backwards compat with test stubs that return a
        # 2-tuple (text, reports) or a bare string (``lambda **kw: "..."``).
        # ``failed_roles`` feeds the run-completeness gate below; legacy
        # shapes default it to [] (no failure info → gate doesn't fire).
        if isinstance(_phase_1_result, tuple) and len(_phase_1_result) == 3:
            analyst_reports_text, _phase_1_reports, _phase_1_failed_roles = _phase_1_result
        elif isinstance(_phase_1_result, tuple) and len(_phase_1_result) == 2:
            analyst_reports_text, _phase_1_reports = _phase_1_result
            _phase_1_failed_roles = []
        else:
            analyst_reports_text, _phase_1_reports, _phase_1_failed_roles = (
                _phase_1_result, [], [],
            )
        # T0.3 — collect every adapter outcome recorded during phase 1
        # (analyst agents fan out to data adapters; each adapter call
        # appends to the contextvar buffer reset at the start of this
        # synthesis run). Attached to the phase row's phase_output_json
        # as a structured dict so the UI / audit trail can show
        # "finnhub_news: 14 records" or "sec_13f: HTTP 404".
        import dataclasses as _dc

        from argosy.services.adapter_outcomes import collect_outcomes
        _phase_1_output = {
            "analyst_reports_text": analyst_reports_text,
            "adapter_outcomes": [_dc.asdict(o) for o in collect_outcomes()],
        }
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=1, started_at=_phase_1_started_at,
            phase_output=_phase_1_output,
            agent_report_rows=_phase_1_reports,
        )
    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_1",
        user_id=user_id,
    )

    # Run-completeness gate (codex-reviewed). Abort BEFORE the expensive
    # phases 2-5 if a CRITICAL analyst failed to produce a report — we never
    # build or promote a plan on missing critical data, and the synthesizer
    # never gets the chance to fabricate the missing headline number (the
    # made-up ₪21M FI target is exactly this failure mode). Only when phase 1
    # actually RAN this cycle: on resume the phase-1 data was validated in the
    # original run and ``_phase_1_reports`` isn't repopulated. On abort the
    # prior current plan is left untouched (no draft is written) and the
    # decision_run is stamped 'failed' so the check-in surface can report it.
    if 1 not in resumed_outputs:
        missing_critical = _failed_critical_agents(_phase_1_failed_roles)
        if missing_critical:
            decision_run.status = "failed"
            decision_run.finished_at = datetime.now(timezone.utc)
            session.commit()
            log.error(
                "plan_synthesis.run_completeness_gate_failed",
                user_id=user_id,
                decision_run_id=decision_run_id,
                missing_critical=missing_critical,
            )
            _emit_event(
                "plan.synthesis.incomplete",
                {
                    "user_id": user_id,
                    "decision_run_id": decision_run_id,
                    "missing_critical": missing_critical,
                },
            )
            raise IncompleteFleetError(missing_critical)

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
        _phase_2_result = _pkg._run_phase_2_debates(
            session=session, user_id=user_id,
            analyst_reports_text=analyst_reports_text,
            baseline=baseline, prior_current=prior_current,
            decision_run_id=decision_audit_token, trigger=trigger,
            guidance=guidance,
        )
        if isinstance(_phase_2_result, tuple) and len(_phase_2_result) == 2:
            debate_outcomes_text, _phase_2_reports = _phase_2_result
        else:
            debate_outcomes_text, _phase_2_reports = _phase_2_result, []
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=2, started_at=_phase_2_started_at,
            phase_output=debate_outcomes_text,
            agent_report_rows=_phase_2_reports,
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
        _phase_3_result = _pkg._run_phase_3_synthesizer(
            session=session, user_id=user_id,
            baseline=baseline, prior_current=prior_current,
            analyst_reports_text=analyst_reports_text,
            debate_outcomes_text=debate_outcomes_text,
            portfolio_summary=portfolio_summary,
            fills_summary=fills_summary,
            decision_run_id=decision_audit_token,
            speculation_cap_pct=cap.max_pct_of_net_worth,
            speculation_cap_concurrent=cap.max_concurrent_positions,
            guidance=guidance,
        )
        # T0.1 — new return shape is (PlanSynthesisOutput, list[AgentReport]);
        # legacy stubs (``lambda **kw: _stub_synthesis_output()``) return
        # the bare ``PlanSynthesisOutput`` so detect via isinstance of the
        # expected output type.
        if (
            isinstance(_phase_3_result, tuple)
            and len(_phase_3_result) == 2
            and not isinstance(_phase_3_result, PlanSynthesisOutput)
        ):
            output, _phase_3_reports = _phase_3_result
        else:
            output, _phase_3_reports = _phase_3_result, []
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=3, started_at=_phase_3_started_at,
            phase_output=output.model_dump_json(),
            agent_report_rows=_phase_3_reports,
        )

    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_3",
        user_id=user_id,
    )

    # Phase 2 of docs/plans/argosy-comprehensive-plan-integration.md:
    # rewrite jargon-heavy prose to household English BEFORE the
    # speculation-cap post-filter mutates structure. The invariant
    # validator enforces bit-equality on every structured field; any
    # drift raises and aborts the synthesis cycle. Resolved through
    # the package namespace so tests can monkeypatch the rewriter.
    output = _pkg._run_plan_language_rewriter(
        output=output,
        user_id=user_id,
        decision_run_id=decision_run_id,
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
        _phase_4_result = _pkg._run_phase_4_risk(
            session=session, user_id=user_id, draft_output=output,
            analyst_reports_text=analyst_reports_text,
            decision_run_id=decision_audit_token,
            guidance=guidance,
        )
        if isinstance(_phase_4_result, tuple) and len(_phase_4_result) == 2:
            risk_verdict, _phase_4_reports = _phase_4_result
        else:
            risk_verdict, _phase_4_reports = _phase_4_result, []
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=4, started_at=_phase_4_started_at,
            phase_output=risk_verdict,
            agent_report_rows=_phase_4_reports,
        )
    _pkg._check_cost_cap(
        decision_audit_token=decision_audit_token,
        cost_cap_usd=cost_cap_usd,
        phase="phase_4",
        user_id=user_id,
    )

    # Phase 4.5 — Argosy ZigZag — codex (gpt-5) as an INDEPENDENT
    # second-opinion reviewer. Sees the synth draft + analyst reports
    # + debate outcomes + risk verdict + user directive; does NOT see
    # the FM's prior-round objections (would mirror FM's framing —
    # defeats the independent purpose) or any codex output from this
    # run (this is the FIRST codex call).
    #
    # Fail-soft: any error returns (None, None) and Phase 5 runs as if
    # codex didn't exist. The kill switches (env var, pytest) and
    # idempotency check live inside ``run_codex_second_opinion``.
    import asyncio as _asyncio

    # Derived-numbers manifest codex audits the headline figures against.
    # Computed once from the already-persisted phase-1 reports + the
    # deterministic methodology; reused for the reconcile re-review.
    def _build_numbers_block() -> str:
        try:
            from argosy.services.plan_numeric_resolver import (
                render_numbers_for_synth,
                resolve_plan_numbers,
            )
            _drun = _decision_run_int(decision_run_id)
            if _drun is None:
                return ""
            return render_numbers_for_synth(
                resolve_plan_numbers(
                    session, user_id=user_id, decision_run_id=_drun,
                    include_canonical_ages=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_synthesis.codex_numbers_block_failed", error=str(exc))
            return ""

    _numbers_block = _build_numbers_block()

    # RAW holdings — the codex reviewer re-derives net worth / US-situs estate
    # / NVDA weight from THESE (its own logic, blind to how the pipeline
    # computed them) and flags any pipeline-claimed number it cannot reproduce.
    # This is the adversarial contract: independent re-derivation from raw
    # inputs, not consistency-checking the prose against a shared manifest.
    def _build_raw_holdings_block() -> str:
        try:
            from argosy.state.models import PortfolioSnapshotRow
            from sqlalchemy import select as _select

            snap = session.execute(
                _select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.user_id == user_id)
                .order_by(PortfolioSnapshotRow.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if snap is None:
                return ""
            positions = json.loads(snap.positions_json or "[]")
            fx = snap.fx_usd_nis
            lines = [
                f"Snapshot stored FX USD/NIS = {fx} (snapshot id={snap.id}; "
                f"usd_value_k is THOUSANDS of USD).",
                "FX CONVENTION: net worth, US-situs estate exposure, and EVERY "
                "other USD→NIS translation use the Bank of Israel CURRENT daily "
                "representative USD/NIS rate (stated as 'USD/NIS' in the "
                "PIPELINE-CLAIMED HEADLINE NUMBERS block), which may differ "
                "slightly from this snapshot's stored rate. Reproduce net worth "
                "AND the US-situs estate exposure at the BOI current rate — NOT "
                "this snapshot's stored rate — and only flag a USD→NIS figure "
                "(net_worth, us_situs_estate, …) as DIVERGES if it disagrees AT "
                "THE BOI CURRENT RATE. (The US-situs USD basis and instrument "
                "set are identical; a NIS gap that vanishes at the BOI rate is "
                "an FX-convention artifact, not a divergence.)",
                # The `details` cell is often Hebrew (mojibake on a cp1252 hop),
                # which strips the exchange/domicile signal a US-situs
                # classification needs. `instrument_name` is the OBJECTIVE
                # plain-English identity (e.g. "iShares Core S&P 500 (UCITS)",
                # "Schwab US Dividend Equity ETF") from the canonical reference —
                # raw reference data, NOT Argosy's US-situs conclusion — so the
                # reviewer classifies domicile correctly while still re-deriving
                # the US-situs total independently. (Run-114 codex under-counted
                # US-situs by ~$40K because it had only garbled tickers to go on.)
                "symbol | instrument_name | broker_location | currency | "
                "asset_type | usd_value_k | details",
            ]
            from argosy.services.instrument_reference import name_for

            for p in positions:
                if not isinstance(p, dict):
                    continue
                _sym = (p.get("symbol") or "").strip()
                _name = name_for(_sym, p.get("details") or "") or "-"
                lines.append(
                    f"{_sym or '-'} | {_name} | {p.get('location') or '-'} | "
                    f"{p.get('currency') or '-'} | {p.get('asset_type') or '-'} | "
                    f"{p.get('usd_value_k')} | {(p.get('details') or '')[:60]}"
                )
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 — reviewer degrades gracefully
            log.warning("plan_synthesis.codex_holdings_block_failed", error=str(exc))
            return ""

    _holdings_block = _build_raw_holdings_block()

    def _run_codex(draft):
        return _asyncio.run(
            _pkg.run_codex_second_opinion(
                synth_draft_json=draft.model_dump_json(),
                analyst_reports_text=analyst_reports_text,
                debate_outcomes_text=debate_outcomes_text,
                risk_verdict_text=risk_verdict,
                user_directive=guidance,
                decision_run_id=decision_run_id,
                user_id=user_id,
                derived_numbers_block=_numbers_block,
                raw_holdings_block=_holdings_block,
            )
        )

    codex_opinion = None
    codex_row = None
    try:
        codex_opinion, codex_row = _run_codex(output)
    except Exception as exc:  # noqa: BLE001 — fail-soft
        log.warning(
            "codex_second_opinion.run_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )
        codex_opinion, codex_row = None, None

    # ------------------------------------------------------------------
    # FORCING LOOP (codex-recommended, bounded to ONE reconcile round):
    # when codex BLOCKS on a numeric/methodology finding (a fabricated /
    # uncited / contradictory headline number, or an indefensible FI
    # methodology), re-run the synthesizer ONCE with the objection folded
    # into guidance, then re-review. Fail-closed: if codex still BLOCKS the
    # draft does NOT auto-promote (the FM gate + #20 already enforce that)
    # and the persisted draft carries the unresolved objection. Disabled
    # under ARGOSY_NUMERIC_RECONCILE=0; never fires under pytest (codex is
    # skipped there, so codex_opinion is None).
    import os as _os
    # Durable marker of the zigzag reconcile so /decisions/[id] can VISUALLY
    # show that codex pushed back, the synthesizer was re-run to correct it,
    # and whether the re-review still blocks. Stays None when no reconcile
    # fired; merged into the codex phase's phase_output_json below so the
    # agent-tree builder can surface it on the codex node. Fail-soft — never
    # breaks synthesis.
    _reconcile_marker: dict | None = None
    if _os.environ.get("ARGOSY_NUMERIC_RECONCILE", "1") == "1":
        _reconcile_guidance = _codex_numeric_reconcile_guidance(codex_opinion)
        if _reconcile_guidance:
            _reconcile_objection_topic = _codex_first_numeric_topic(codex_opinion)
            log.warning(
                "plan_synthesis.numeric_reconcile_triggered",
                user_id=user_id, decision_run_id=decision_run_id,
            )
            try:
                _augmented = (guidance + "\n\n" + _reconcile_guidance).strip()
                _recon_started = datetime.now(timezone.utc)
                _recon_result = _pkg._run_phase_3_synthesizer(
                    session=session, user_id=user_id,
                    baseline=baseline, prior_current=prior_current,
                    analyst_reports_text=analyst_reports_text,
                    debate_outcomes_text=debate_outcomes_text,
                    portfolio_summary=portfolio_summary,
                    fills_summary=fills_summary,
                    decision_run_id=decision_audit_token,
                    speculation_cap_pct=cap.max_pct_of_net_worth,
                    speculation_cap_concurrent=cap.max_concurrent_positions,
                    guidance=_augmented,
                )
                if (
                    isinstance(_recon_result, tuple) and len(_recon_result) == 2
                    and not isinstance(_recon_result, PlanSynthesisOutput)
                ):
                    output, _recon_reports = _recon_result
                else:
                    output, _recon_reports = _recon_result, []
                # Re-run the household-English rewriter + speculation cap on
                # the reconciled draft so it matches the normal pipeline.
                output = _pkg._run_plan_language_rewriter(
                    output=output, user_id=user_id, decision_run_id=decision_run_id,
                )
                output = _pkg._enforce_speculation_cap(
                    output,
                    max_pct_of_net_worth=cap.max_pct_of_net_worth,
                    max_concurrent_positions=cap.max_concurrent_positions,
                )
                # Re-record phase 3 with the reconciled output (higher seq
                # wins in _load_completed_phase_outputs → resume-safe) so the
                # audit trail + any crash-resume reflect the reconciled draft.
                _pkg._record_phase_completion(
                    user_id=user_id, decision_run_id=decision_run_id,
                    phase_n=3, started_at=_recon_started,
                    phase_output=output.model_dump_json(),
                    agent_report_rows=_recon_reports,
                )
                # Refresh the manifest (the reconciled synth report is now the
                # latest for its role) + re-review.
                _numbers_block = _build_numbers_block()
                codex_opinion, codex_row = _run_codex(output)
                _still_blocking = (
                    getattr(codex_opinion, "overall_assessment", None) == "BLOCK"
                )
                _reconcile_marker = {
                    "triggered": True,
                    "still_blocking": bool(_still_blocking),
                    "objection_topic": _reconcile_objection_topic,
                }
                log.warning(
                    "plan_synthesis.numeric_reconcile_done",
                    user_id=user_id, decision_run_id=decision_run_id,
                    still_blocking=_still_blocking,
                )
            except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
                # Record that a reconcile was ATTEMPTED even if the re-synth/
                # re-review raised — the pushback still happened and should be
                # visible. still_blocking stays unknown (conservatively True
                # so the UI doesn't imply a clean resolution we can't prove).
                _reconcile_marker = {
                    "triggered": True,
                    "still_blocking": True,
                    "objection_topic": _reconcile_objection_topic,
                }
                log.warning(
                    "plan_synthesis.numeric_reconcile_failed",
                    user_id=user_id, decision_run_id=decision_run_id,
                    error=str(exc),
                )

    # Persist the codex row through the same recorder pipeline as
    # every other phase so it appears in agent_reports + the phase
    # tree. When codex was skipped (None) we don't record an empty
    # phase row — that would muddy the audit trail.
    if codex_row is not None:
        # Build the phase output, folding in the zigzag reconcile marker
        # (when one fired) so the agent-tree builder can surface it on the
        # codex node. Fail-soft: a merge failure falls back to the bare
        # opinion JSON so the audit trail is never lost.
        _codex_phase_output: str = (
            codex_opinion.model_dump_json() if codex_opinion else ""
        )
        if _reconcile_marker is not None:
            try:
                import json as _json
                _payload = (
                    codex_opinion.model_dump() if codex_opinion else {}
                )
                _payload["codex_reconcile"] = _reconcile_marker
                _codex_phase_output = _json.dumps(_payload)
            except Exception:  # noqa: BLE001 — never lose the codex row
                pass
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=45,  # 4.5 — half-step between risk and FM
            started_at=datetime.now(timezone.utc),
            phase_output=_codex_phase_output,
            agent_report_rows=[codex_row],
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
        _phase_5_result = _pkg._run_phase_5_fund_manager(
            session=session, user_id=user_id, draft_output=output,
            risk_verdict=risk_verdict, decision_run_id=decision_audit_token,
            guidance=guidance,
            codex_second_opinion=codex_opinion,
        )
        if isinstance(_phase_5_result, tuple) and len(_phase_5_result) == 2:
            approved, _phase_5_reports = _phase_5_result
        else:
            approved, _phase_5_reports = _phase_5_result, []
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=5, started_at=_phase_5_started_at,
            phase_output="approved" if approved else "rejected",
            agent_report_rows=_phase_5_reports,
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

    # Demote any prior draft at the same commit as the new draft so a
    # synthesis failure earlier in this function never leaves the user
    # with no pending draft (real incident on decision_run #43,
    # 2026-05-30). The partial unique index uq_plan_versions_draft_per_user
    # is enforced statement-by-statement on both SQLite (dev) and
    # Postgres (prod); flushing the UPDATE before the INSERT keeps the
    # constraint satisfied at every point.
    existing_draft = get_pending_draft(session, user_id)
    if existing_draft is not None:
        existing_draft.role = "superseded"
        existing_draft.superseded_at = datetime.now(timezone.utc)
        session.flush()
        log.info(
            "plan_synthesis.demoted_existing_draft",
            superseded_id=existing_draft.id,
            user_id=user_id,
        )

    # The user-facing + audit horizon bodies (appendices on long, headline
    # scrub, jargon/history strip) and the canonical target-allocation are
    # assembled below via _assemble_draft_bodies — the SINGLE renderer shared
    # with the reader-reconcile re-persist so both paths produce an identical
    # draft shape.


    # Team-source the Alternatives sleeve (size + instruments are agent-derived,
    # deterministically verified, estate-gated, then debated by an ETP-aware
    # fleet + sized by the sleeve fund manager — 0% is a valid outcome). The
    # decision threads into the canonical allocation. Best-effort + NEVER fatal:
    # on any failure the plan is built with NO alternatives sleeve (0%), never a
    # stale or unverified one.
    _alternatives_sleeve = None
    try:
        from argosy.orchestrator.flows.plan_synthesis.alternatives_phase import (
            run_alternatives_phase,
        )

        _alternatives_sleeve = run_alternatives_phase(
            user_id=user_id,
            macro_context={
                "anchor_sigma": 0.18,
                "regime": (
                    "Israeli long-hold investor, heavily NVDA-concentrated (via RSUs) "
                    "and actively deconcentrating; elevated US equity valuations; "
                    "geopolitical risk elevated. Estate constraint: every instrument "
                    "must be non-US-domiciled."
                ),
            },
        )
        log.info(
            "plan_synthesis.alternatives_decision",
            user_id=user_id,
            decision_run_id=decision_run_id,
            decision=_alternatives_sleeve.decision,
            target_pct=_alternatives_sleeve.target_pct,
            instruments=len(_alternatives_sleeve.instruments),
        )
    except Exception as exc:  # noqa: BLE001 — alternatives phase never breaks synthesis
        log.warning(
            "plan_synthesis.alternatives_phase_failed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            error=str(exc),
        )
        _alternatives_sleeve = None

    # Assemble every user-facing + audit body field from the synth output via
    # the SINGLE shared renderer (also used by the reader-reconcile re-persist).
    _bodies = _assemble_draft_bodies(
        session, output=output, user_id=user_id,
        decision_run_id=decision_run_id, alternatives_sleeve=_alternatives_sleeve,
    )

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
        # v4 (block B1, 2026-06-02) — user-facing variants now carry the
        # Deltas block at the TOP (user-requested counter-decision to
        # Phase 1's strip). They still drop the status-header suffix and
        # the per-target ``(stated …; revisit …)`` parentheticals. Audit
        # variants retain everything for the /decisions/<id> dev pane.
        # ``horizon_long_md`` additionally carries the v4 appendix block
        # (assumption ledger + section-by-section evidence + fleet
        # receipts) assembled just above.
        horizon_long_md=_bodies["horizon_long_md"],
        horizon_medium_md=_bodies["horizon_medium_md"],
        horizon_short_md=_bodies["horizon_short_md"],
        horizon_long_md_audit=_bodies["horizon_long_md_audit"],
        horizon_medium_md_audit=_bodies["horizon_medium_md_audit"],
        horizon_short_md_audit=_bodies["horizon_short_md_audit"],
        synthesis_inputs_json=inputs.model_dump_json(),
        target_allocation_json=_bodies["target_allocation_json"],
        # Persist the structured sections so the plan-output gate can
        # evaluate section_coverage + evidence_per_section against the REAL
        # sections at promote-time (they were previously dropped on the floor).
        sections_json=_bodies["sections_json"],
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)

    # Stamp the DecisionRun row as finished — provides the audit lineage
    # SDD §6.11 promises: you can reconstruct the full synthesis by joining
    # plan_versions.decision_run_id → decision_runs.id.
    #
    # T2.8 — the prior implementation gated this on
    # `existing_decision_run_id is None` to avoid racing the plan_amendment
    # cancellation check. The downside: /api/advisor/check-in ALWAYS passes
    # the pre-created run_id, so every "normal" synthesis kept its
    # decision_run row as `status='running'` forever. Run #24 surfaced this
    # — the draft persisted + FM verdict fired but #24's row still showed
    # status='running' after completion.
    #
    # Fix: always stamp the completion fields when the orchestrator owns
    # the synthesis to its end. The amendment-cancel path uses a different
    # code path (the worker that runs the amendment owns its own
    # status-transition logic and checks cancellation BEFORE entering
    # run_synthesis); by the time we reach this line, the synthesis has
    # already produced the draft, so a late cancellation flip would be
    # incorrect anyway.
    decision_run.finished_at = datetime.now(timezone.utc)
    decision_run.status = "completed"
    decision_run.fund_manager_decision = "approved" if approved else "rejected"
    session.commit()

    # ------------------------------------------------------------------
    # FINAL STAGE — whole-artifact adversarial reader (Task 7).
    #
    # Runs AFTER the draft PlanVersion + DecisionRun are committed so
    # ``assemble_plan_artifact`` reads the JUST-PERSISTED draft's full_text
    # (it resolves get_pending_draft / get_current_plan off the DB). The
    # reader owns COHERENCE OF THE WHOLE (contradictions / fragile-headline
    # / staleness / regressions); the codex Phase-4.5 gate owns the math.
    #
    # Gating + fail-soft: ``run_whole_artifact_review`` short-circuits to
    # (None, None) under the codex kill switches AND under pytest (so every
    # existing synthesis test is unchanged unless it monkeypatches the
    # dispatcher). The dispatch is additionally wrapped here so any
    # assemble / dispatch error degrades to a no-op rather than aborting a
    # synthesis whose draft is already safely persisted.
    #
    # Promotion coupling: a reader BLOCK marks the draft NOT-auto-promotable
    # through the SAME field the fund_manager uses —
    # ``decision_run.fund_manager_decision = "rejected"`` — which
    # ``api/routes/plan.py::post_draft_accept`` consults to raise its 422
    # promotion gate. The user remains the final gate (promotion still
    # possible via ?override_fm_rejection=true, audit-logged). No parallel
    # promotion mechanism is introduced. A reader BLOCK never UN-rejects an
    # FM rejection (it can only tighten the gate, never loosen it).
    # Layer B (shift-left): run the deterministic gate suite on the persisted
    # draft BEFORE the expensive LLM reader, so a cheap deterministic defect is
    # surfaced in-stage. Best-effort + never aborts synthesis; recorded as
    # phase 5.3 so /decisions/[id] shows it ran before the reader.
    try:
        _instage_started = datetime.now(timezone.utc)
        _instage_verdict = _pkg.run_deterministic_gate_instage(
            session=session, user_id=user_id, draft=draft,
            decision_run_id=decision_run_id,
        )
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=53, started_at=_instage_started,
            phase_output=_instage_verdict.summary(),
            agent_report_rows=[],
        )
        if not _instage_verdict.passes:
            log.warning(
                "plan_synthesis.instage_gate_violations",
                user_id=user_id, decision_run_id=decision_run_id,
                summary=_instage_verdict.summary(),
            )
    except Exception as exc:  # noqa: BLE001 — surfacing only; never abort
        log.warning(
            "plan_synthesis.instage_gate_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )

    def _assemble_and_read(settled_rulings=None):
        """Assemble the just-persisted draft into its full artifact and run the
        whole-artifact reader against it. Returns ``(verdict, row)``; the inner
        assemble degrades to an empty artifact (→ reader BLOCKs) on failure.

        ``settled_rulings`` (the coherence-deliberation accumulating loop) are
        injected so the reader does not re-litigate already-arbitrated questions
        — it must still verify every surface against each ruling and may appeal."""
        _prior_plan_text = ""
        if prior_current is not None:
            _prior_plan_text = "\n\n".join(filter(None, [
                prior_current.horizon_long_md,
                prior_current.horizon_medium_md,
                prior_current.horizon_short_md,
            ]))
            # Strip the prior plan's baked internal-review metadata (fleet/analysis
            # receipts, coherence/FM-dialogue appendices) BEFORE the reader diffs
            # against it — exactly as the assembled artifact strips its own. A
            # stale "second opinion returned BLOCK" receipt baked into the prior
            # body is process metadata, not plan content; left in, the reader reads
            # the new draft's "pending review" status as a regression/downgrade
            # against it. The reader must diff PLAN-to-PLAN, not against receipts.
            from argosy.services.assembled_artifact import (
                _strip_internal_metadata_sections,
            )
            _prior_plan_text = _strip_internal_metadata_sections(_prior_plan_text)

        _assembled_text = ""
        try:
            from argosy.services.assembled_artifact import assemble_plan_artifact

            _assembled = assemble_plan_artifact(session, user_id=user_id)
            _assembled_text = _assembled.full_text or ""
        except Exception as exc:  # noqa: BLE001 — empty artifact → reader BLOCKs
            log.warning(
                "whole_artifact_reader.assemble_failed",
                user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
            )

        # Fresh external-context packet: at minimum today's ISO date so the
        # reader can flag stale "as of" content. No market context threads
        # through this caller today (empty is handled by the reader).
        _external_context = (
            f"Today's date (ISO): {datetime.now(timezone.utc).date().isoformat()}"
        )

        return _asyncio.run(
            _pkg.run_whole_artifact_review(
                assembled_artifact=_assembled_text,
                external_context=_external_context,
                prior_plan_text=_prior_plan_text,
                decision_run_id=decision_run_id,
                user_id=user_id,
                settled_rulings=settled_rulings,
            )
        )

    try:
        _reader_verdict, _reader_row = _assemble_and_read()
    except Exception as exc:  # noqa: BLE001 — fail-soft; draft already persisted
        log.warning(
            "whole_artifact_reader.run_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )
        _reader_verdict, _reader_row = None, None

    # COHERENCE RECONCILE LOOP — give the reader the SAME feedback the codex
    # numeric zigzag has (the helper at ``_codex_numeric_reconcile_guidance`` +
    # the forcing loop near phase 4.5). When the reader BLOCKS on a FIXABLE
    # coherence hole (contradiction / cross-surface / stale date / fragile
    # claim), fold the finding into synthesizer guidance, RE-RUN synthesis,
    # RE-PERSIST the draft via the shared ``_assemble_draft_bodies`` renderer,
    # and RE-READ — so the hole comes back RESOLVED without a human editing
    # prose. Bounded to ONE round (like codex) + fail-closed: if it still BLOCKS
    # after the bound the draft stays not-auto-promotable (the gate below fires).
    # Disabled under ARGOSY_READER_RECONCILE=0; never fires when the reader was
    # skipped (verdict None — pytest / kill switch).
    _reader_reconcile_marker: dict | None = None
    # Bound on reader-reconcile rounds. Default 1 (the expensive full-resynth
    # path). Overnight/surgical runs raise it via env so cheap surgical rounds
    # can iterate toward convergence.
    try:
        _READER_RECONCILE_MAX_ROUNDS = max(1, int(_os.environ.get("ARGOSY_READER_RECONCILE_MAX_ROUNDS", "1")))
    except (TypeError, ValueError):
        _READER_RECONCILE_MAX_ROUNDS = 1
    if (
        _os.environ.get("ARGOSY_READER_RECONCILE", "1") == "1"
        and _reader_verdict is not None
    ):
        _reader_objection_topic = _reader_first_objection_topic(_reader_verdict)
        _reader_round = 0
        while (
            _reader_round < _READER_RECONCILE_MAX_ROUNDS
            and getattr(_reader_verdict, "overall_assessment", "") == "BLOCK"
        ):
            _reader_guidance = _reader_coherence_reconcile_guidance(_reader_verdict)
            if not _reader_guidance:
                break  # infra-failure BLOCK / no fixable finding — re-synth can't help
            _reader_round += 1
            log.warning(
                "plan_synthesis.reader_reconcile_triggered",
                user_id=user_id, decision_run_id=decision_run_id,
                round=_reader_round,
            )
            # COHERENCE-DELIBERATION reconcile (default OFF — set
            # ARGOSY_COHERENCE_DELIBERATION=1 to enable). Replaces the surgical
            # closer + full re-synth for this round: cluster the reader's BLOCKER
            # findings into structured disputes, route each (deterministic
            # resolver for value mismatches; panel -> facilitator -> arbitrator
            # for goal/framing tensions; untypeable = BLOCK), conform ALL
            # surfaces atomically, deterministically verify, persist each ruling
            # to the coherence ledger, and re-read with the accumulated rulings
            # injected so a settled question is not re-litigated. Fail-closed: a
            # non-ok pass (untypeable / unresolved / conform or verify failure)
            # does NOT promote and does NOT fall back to the markdown closer (the
            # known-unsound path) — the existing reader gate below keeps it
            # BLOCKED. Every path here ends the iteration (continue/break) so the
            # surgical + full-re-synth code below never runs while this is on.
            if _os.environ.get("ARGOSY_COHERENCE_DELIBERATION", "0") == "1":
                try:
                    from argosy.quality.coherence import ledger as _coh_ledger
                    from argosy.agents.coherence_panelist import CoherencePanelistAgent
                    from argosy.agents.coherence_facilitator import CoherenceFacilitatorAgent
                    from argosy.agents.coherence_arbitrator import CoherenceArbitratorAgent
                    from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
                        resolver_context,
                    )
                    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

                    _delib_started = datetime.now(timezone.utc)
                    _drun_int = _decision_run_int(decision_run_id)
                    _delib_resolved = (
                        resolve_plan_numbers(session, user_id=user_id, decision_run_id=_drun_int)
                        if _drun_int is not None else None
                    )
                    _delib_canonical = (
                        resolver_context(_delib_resolved) if _delib_resolved is not None else ""
                    )

                    # Build deliberation findings from the reader's BLOCKERs. The
                    # reader classifies subject_type itself (the prompt taxonomy);
                    # cluster_findings forces framing subjects to arbitration.
                    _delib_findings = [
                        {
                            "subject_type": getattr(f, "subject_type", "") or "",
                            "kind": getattr(f, "kind", "") or "other",
                            "severity": "BLOCKER",
                            "surfaces_cited": list(f.surfaces_cited or []),
                            "field_path": getattr(f, "field_path", "") or "",
                            "normalized_claim": getattr(f, "normalized_claim", "") or "",
                            "detail": f.detail,
                        }
                        for f in _reader_verdict.findings
                        if f.severity == "BLOCKER"
                    ]
                    _delib_bodies = {
                        "long_md": draft.horizon_long_md or "",
                        "medium_md": draft.horizon_medium_md or "",
                        "short_md": draft.horizon_short_md or "",
                    }
                    _delib_json_surfaces: dict = {}
                    if draft.horizon_short_json:
                        try:
                            _delib_json_surfaces["short_actions_json"] = json.loads(
                                draft.horizon_short_json
                            )
                        except (TypeError, ValueError):
                            pass

                    _delib = _pkg.run_coherence_deliberation_pass(
                        bodies=_delib_bodies, json_surfaces=_delib_json_surfaces,
                        findings=_delib_findings, canonical_facts=_delib_canonical,
                        prime_directive=_COHERENCE_PRIME_DIRECTIVE,
                        make_panelist=lambda role: CoherencePanelistAgent(user_id=user_id),
                        facilitator=CoherenceFacilitatorAgent(user_id=user_id),
                        arbitrator=CoherenceArbitratorAgent(user_id=user_id),
                        resolver_value_fn=None,
                    )
                    _pkg._record_phase_completion(
                        user_id=user_id, decision_run_id=decision_run_id,
                        phase_n=56, started_at=_delib_started,
                        phase_output=(
                            f"coherence_deliberation: ok={_delib.ok} "
                            f"rulings={len(_delib.rulings)} errors={_delib.errors}"
                        ),
                        agent_report_rows=[],
                    )
                    if not _delib.ok:
                        # Fail-closed: do NOT promote, do NOT fall back to the
                        # closer. The reader gate below keeps the draft BLOCKED.
                        log.warning(
                            "plan_synthesis.coherence_deliberation_blocked",
                            user_id=user_id, decision_run_id=decision_run_id,
                            round=_reader_round, errors=_delib.errors,
                        )
                        break
                    # Conform succeeded — persist the conformed surfaces.
                    draft.horizon_long_md = _delib.bodies.get("long_md", draft.horizon_long_md)
                    draft.horizon_medium_md = _delib.bodies.get("medium_md", draft.horizon_medium_md)
                    draft.horizon_short_md = _delib.bodies.get("short_md", draft.horizon_short_md)
                    if "short_actions_json" in _delib.json_surfaces:
                        draft.horizon_short_json = json.dumps(
                            _delib.json_surfaces["short_actions_json"], ensure_ascii=False
                        )
                    session.commit()
                    for _r in _delib.rulings:
                        _coh_ledger.record_ruling(
                            session, user_id=user_id, decision_run_id=_drun_int,
                            dispute_key=_r["dispute_key"], subject_type=_r["subject_type"],
                            question=_r["question"], ruling=_r["ruling"],
                            rationale=_r["rationale"], basis=_r["basis"],
                            resolved_by=_r["resolved_by"], invariants=_r["invariants"],
                            conformed_surfaces=_r["conformed_surfaces"],
                        )
                    # Re-read with the accumulated rulings injected so a settled
                    # question is not re-litigated (the bounded accumulating loop).
                    _delib_rulings = [
                        {"subject_type": rr.subject_type, "ruling": rr.ruling}
                        for rr in _coh_ledger.load_active_rulings(session, user_id=user_id)
                    ]
                    _reader_verdict, _reader_row = _assemble_and_read(
                        settled_rulings=_delib_rulings
                    )
                    if getattr(_reader_verdict, "overall_assessment", "") != "BLOCK":
                        continue  # deliberation cleared the BLOCK — loop exits clean
                    # Still BLOCKing — the next round (if any) re-reads with this
                    # round's rulings already in the ledger. Bound is the while
                    # condition; never fall through to the markdown closer.
                    continue
                except Exception as exc:  # noqa: BLE001 — fail-closed, never fall back
                    log.warning(
                        "plan_synthesis.coherence_deliberation_failed",
                        user_id=user_id, decision_run_id=decision_run_id,
                        error=str(exc),
                    )
                    break
            # Surgical pre-pass (default OFF — set ARGOSY_SURGICAL_CORRECTION=1 to
            # enable). Fix RENDERABLE reader findings at their cited segment via a
            # cheap prose edit (seconds), persist in place, and re-read. If that
            # clears the BLOCK (or only structural/infra findings remain), SKIP
            # the ~45-min full re-synth this round. Only genuinely structural
            # findings fall through to full re-synth below — which is NOT demoted,
            # it remains the fallback + the whole-artifact reader stays the net.
            if _os.environ.get("ARGOSY_SURGICAL_CORRECTION", "0") == "1":
                try:
                    from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
                        surgically_correct_draft,
                    )
                    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

                    _surg_started = datetime.now(timezone.utc)
                    _drun_int = _decision_run_int(decision_run_id)
                    _surg_resolved = (
                        resolve_plan_numbers(session, user_id=user_id, decision_run_id=_drun_int)
                        if _drun_int is not None else None
                    )
                    _surg = surgically_correct_draft(
                        bodies={
                            "long": draft.horizon_long_md or "",
                            "medium": draft.horizon_medium_md or "",
                            "short": draft.horizon_short_md or "",
                        },
                        reader_verdict=_reader_verdict, resolved=_surg_resolved,
                    )
                    _pkg._record_phase_completion(
                        user_id=user_id, decision_run_id=decision_run_id,
                        phase_n=54, started_at=_surg_started,
                        phase_output=(
                            f"surgical: {len(_surg.edits)} edits, "
                            f"{len(_surg.addressed)} addressed, "
                            f"{len(_surg.unaddressed)} fallback"
                        ),
                        agent_report_rows=[],
                    )
                    if _surg.edits:
                        draft.horizon_long_md = _surg.corrected_bodies["long"]
                        draft.horizon_medium_md = _surg.corrected_bodies["medium"]
                        draft.horizon_short_md = _surg.corrected_bodies["short"]
                        session.commit()
                        _reader_verdict, _reader_row = _assemble_and_read()
                        if getattr(_reader_verdict, "overall_assessment", "") != "BLOCK":
                            continue  # surgical edits cleared the block — skip re-synth
                        if not _reader_coherence_reconcile_guidance(_reader_verdict):
                            break  # only structural/infra findings remain — re-synth won't help
                except Exception as exc:  # noqa: BLE001 — best-effort; never abort
                    log.warning(
                        "plan_synthesis.surgical_reconcile_failed",
                        user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
                    )
            try:
                _augmented = (guidance + "\n\n" + _reader_guidance).strip()
                _recon_started = datetime.now(timezone.utc)
                _recon_result = _pkg._run_phase_3_synthesizer(
                    session=session, user_id=user_id,
                    baseline=baseline, prior_current=prior_current,
                    analyst_reports_text=analyst_reports_text,
                    debate_outcomes_text=debate_outcomes_text,
                    portfolio_summary=portfolio_summary,
                    fills_summary=fills_summary,
                    decision_run_id=decision_audit_token,
                    speculation_cap_pct=cap.max_pct_of_net_worth,
                    speculation_cap_concurrent=cap.max_concurrent_positions,
                    guidance=_augmented,
                )
                if (
                    isinstance(_recon_result, tuple) and len(_recon_result) == 2
                    and not isinstance(_recon_result, PlanSynthesisOutput)
                ):
                    output, _recon_reports = _recon_result
                else:
                    output, _recon_reports = _recon_result, []
                output = _pkg._run_plan_language_rewriter(
                    output=output, user_id=user_id, decision_run_id=decision_run_id,
                )
                output = _pkg._enforce_speculation_cap(
                    output,
                    max_pct_of_net_worth=cap.max_pct_of_net_worth,
                    max_concurrent_positions=cap.max_concurrent_positions,
                )
                _pkg._record_phase_completion(
                    user_id=user_id, decision_run_id=decision_run_id,
                    phase_n=3, started_at=_recon_started,
                    phase_output=output.model_dump_json(),
                    agent_report_rows=_recon_reports,
                )
                # Re-render the draft body from the reconciled output and UPDATE
                # the persisted draft IN PLACE so ``assemble_plan_artifact``
                # re-reads the corrected plan. The alternatives decision is
                # REUSED (its agent fleet is not re-run for a prose reconcile).
                _recon_bodies = _assemble_draft_bodies(
                    session, output=output, user_id=user_id,
                    decision_run_id=decision_run_id,
                    alternatives_sleeve=_alternatives_sleeve,
                )
                draft.horizon_long_json = output.long.model_dump_json()
                draft.horizon_medium_json = output.medium.model_dump_json()
                draft.horizon_short_json = output.short.model_dump_json()
                draft.horizon_long_md = _recon_bodies["horizon_long_md"]
                draft.horizon_medium_md = _recon_bodies["horizon_medium_md"]
                draft.horizon_short_md = _recon_bodies["horizon_short_md"]
                draft.horizon_long_md_audit = _recon_bodies["horizon_long_md_audit"]
                draft.horizon_medium_md_audit = _recon_bodies["horizon_medium_md_audit"]
                draft.horizon_short_md_audit = _recon_bodies["horizon_short_md_audit"]
                draft.target_allocation_json = _recon_bodies["target_allocation_json"]
                draft.sections_json = _recon_bodies["sections_json"]
                session.commit()
                _reader_verdict, _reader_row = _assemble_and_read()
            except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
                log.warning(
                    "plan_synthesis.reader_reconcile_failed",
                    user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
                )
                break
        if _reader_round > 0:
            _reader_still_blocking = (
                getattr(_reader_verdict, "overall_assessment", "") == "BLOCK"
            )
            _reader_reconcile_marker = {
                "triggered": True,
                "still_blocking": bool(_reader_still_blocking),
                "objection_topic": _reader_objection_topic,
            }
            log.warning(
                "plan_synthesis.reader_reconcile_done",
                user_id=user_id, decision_run_id=decision_run_id,
                still_blocking=_reader_still_blocking,
            )

    # Persist the FINAL reader row (phase_n=55 — the holistic stage after
    # codex's 4.5 and the FM's phase 5), folding the reconcile marker into the
    # phase output JSON so /decisions/[id] can show the reader zigzag. Mirror
    # codex's guard: only record a real row.
    if _reader_row is not None:
        _reader_phase_output = (
            _reader_verdict.model_dump_json() if _reader_verdict else ""
        )
        if _reader_reconcile_marker is not None:
            try:
                import json as _json
                _payload = _reader_verdict.model_dump() if _reader_verdict else {}
                _payload["reader_reconcile"] = _reader_reconcile_marker
                _reader_phase_output = _json.dumps(_payload)
            except Exception:  # noqa: BLE001 — never lose the reader row
                pass
        _pkg._record_phase_completion(
            user_id=user_id, decision_run_id=decision_run_id,
            phase_n=55, started_at=datetime.now(timezone.utc),
            phase_output=_reader_phase_output,
            agent_report_rows=[_reader_row],
        )

    # A reader BLOCK (the FINAL verdict, after any reconcile) tightens the
    # promotion gate via the FM's own field.
    if (
        _reader_verdict is not None
        and _reader_verdict.overall_assessment == "BLOCK"
        and decision_run.fund_manager_decision != "rejected"
    ):
        decision_run.fund_manager_decision = "rejected"
        session.commit()
        log.warning(
            "whole_artifact_reader.block_marks_not_promotable",
            user_id=user_id, decision_run_id=decision_run_id,
            findings=len(_reader_verdict.findings),
        )

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

    # Fire-and-forget: precompute plain-English translations of any FM
    # objections so that the user's first /plan load doesn't pay the
    # 100+ second wall-clock for N parallel Sonnet translator calls. The
    # on-demand path on GET /api/plan/draft/objections still works as a
    # fallback when this best-effort warm-fill fails. Skipped entirely
    # when FM approved (no objections to translate). Errors here are
    # NON-FATAL — synthesis completed successfully; the cache warm is a
    # pure latency optimisation.
    if not approved:
        try:
            _pkg._schedule_fm_objection_translation_precompute(
                session=session,
                user_id=user_id,
                plan_version_id=draft.id,
                decision_run_id=decision_run_id,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning(
                "fm_objection_translation_precompute.schedule_failed",
                user_id=user_id,
                decision_run_id=decision_run_id,
                draft_id=draft.id,
                error=str(exc),
            )
    else:
        log.info(
            "fm_objection_translation_precompute.skipped_fm_approved",
            user_id=user_id,
            decision_run_id=decision_run_id,
            draft_id=draft.id,
        )

    # Fleet self-review — fire-and-forget post-synthesis sweep. Runs a
    # daemon thread against a fresh sessionmaker so the orchestrator
    # returns immediately even if the detectors are slow. Failures are
    # logged + swallowed inside ``schedule_post_synthesis_review`` so
    # synthesis never breaks because of an observability surface.
    #
    # The trigger lives HERE (rather than in an event subscriber) because
    # we want the review to see the draft + agent_reports that THIS run
    # produced — every write above this line has been committed.
    try:
        from argosy.services.fleet_self_review_runner import (
            schedule_post_synthesis_review,
        )
        schedule_post_synthesis_review(
            session=session,
            user_id=user_id,
            decision_run_id=decision_run_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "fleet_self_review.schedule_failed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            error=str(exc),
        )

    # Auto-dialogue dispatch — fire FM<->analyst dialogues for every
    # FM objection that has an analyst owner so the fleet pre-resolves
    # what it can BEFORE the user sees /plan. Surfacing decision lives
    # in the /api/plan/draft/objections route: objections that resolve
    # to FM_ACCEPTS_ANALYST hide; FM_MAINTAINS_OBJECTION /
    # ESCALATE_TO_USER / FM_REVISES_OBJECTION + no-analyst-owner cases
    # surface as Blocker / Decision rows.
    #
    # Background-threaded per objection. Best-effort: if cost cap is
    # breached partway, the remaining objections surface unresolved
    # (still a Blocker, just without an analyst push-back attempt).
    # Skipped entirely when FM approved — no objections to dispatch.
    if not approved:
        # CLOSE-THE-LOOP (default OFF — ARGOSY_FM_DIALOGUE_CONVERGE=1): run the FM<->analyst
        # dialogues INLINE to convergence and, if EVERY objection cleared by an FM-accepted
        # rebuttal/clarification (no artifact change required), clear the FM authority so
        # the draft is no longer FM-rejected. Fail-closed + authority-specific (codex
        # review): a confirmed defect / revise / escalate / owner-less objection stays
        # blocking, and the FM authority is cleared ONLY when the whole-artifact reader is
        # also not blocking (the reader is a separate authority — never laundered away).
        _fm_converged = False
        if _os.environ.get("ARGOSY_FM_DIALOGUE_CONVERGE", "0") == "1":
            try:
                from argosy.orchestrator.flows.fm_objection_dialogue import (
                    converge_fm_objections,
                )
                _conv = converge_fm_objections(
                    session, user_id=user_id, plan_version_id=draft.id,
                    decision_run_id=decision_run_id,
                )
                _reader_ok = (
                    _reader_verdict is None
                    or getattr(_reader_verdict, "overall_assessment", "") != "BLOCK"
                )
                if _conv.all_agreed and _reader_ok:
                    decision_run.fund_manager_decision = "approved"
                    session.commit()
                    _fm_converged = True
                    log.warning(
                        "fm_objection_dialogue.authority_cleared",
                        user_id=user_id, decision_run_id=decision_run_id,
                        dispatched=_conv.dispatched,
                    )
                else:
                    log.warning(
                        "fm_objection_dialogue.not_cleared",
                        user_id=user_id, decision_run_id=decision_run_id,
                        all_agreed=_conv.all_agreed, reader_ok=_reader_ok,
                        unresolved=_conv.unresolved[:6],
                    )
            except Exception as exc:  # noqa: BLE001 — fail-closed: stay rejected
                log.warning(
                    "fm_objection_dialogue.converge_failed",
                    user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
                )
        if not _fm_converged:
            try:
                from argosy.orchestrator.flows.fm_objection_dialogue import (
                    schedule_auto_dialogues_for_draft,
                )
                schedule_auto_dialogues_for_draft(
                    session,
                    user_id=user_id,
                    plan_version_id=draft.id,
                    decision_run_id=decision_run_id,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning(
                    "auto_dialogue.schedule_failed",
                    user_id=user_id,
                    decision_run_id=decision_run_id,
                    draft_id=draft.id,
                    error=str(exc),
                )

    # Provenance Wave C — final FM-decision row with the parsed verdict
    # DTO. The 5 per-phase rows (kinds 'synthesis.phase_1'..'phase_5')
    # were already persisted by _record_phase_completion during the
    # flow; this row carries the FundManagerPlanRevisionDecision DTO so
    # the replay UI's VerdictCard renders the approval call.
    #
    # T0.1 follow-up — thread the FM's agent_report id so this final
    # plan_synthesis.verdict phase row's participants_json points at the
    # FM run that produced the call. Prior to this fix the recorder
    # received agent_report_ids=[] (the FM's id wasn't surfaced from
    # phase 5 to here), leaving the verdict row's participants_json as
    # '[]' and the replay UI's sequence diagram empty for the final
    # row. The FM agent_report row was already persisted in DB by
    # phase 5's _record_phase_completion → _persist_phase_agent_reports_async
    # path before we reach this line, so a fresh query against
    # (user_id, decision_id=decision_audit_token, agent_role='fund_manager')
    # will find it. We pick the LATEST row (ORDER BY id DESC LIMIT 1)
    # — when phase 5's FM retry budget consumed more than one attempt,
    # the most-recently-written row is the one that produced the
    # verdict we're recording here.
    try:
        import asyncio
        from sqlalchemy import select as _select, update as _update
        from argosy.agents.fund_manager import FundManagerPlanRevisionDecision
        from argosy.services.negotiation_recorder import (
            record_negotiation_phase,
        )
        from argosy.state.models import AgentReport as _AgentReportRow

        fm_row = session.execute(
            _select(_AgentReportRow.id, _AgentReportRow.phase_id)
            .where(_AgentReportRow.user_id == user_id)
            .where(_AgentReportRow.decision_id == decision_audit_token)
            .where(_AgentReportRow.agent_role == "fund_manager")
            .order_by(_AgentReportRow.id.desc())
            .limit(1)
        ).first()
        fm_report_id: int | None = fm_row[0] if fm_row is not None else None
        # Preserve the existing phase_id back-link (set by phase 5's
        # recorder call). Without this, the recorder below would
        # OVERWRITE the FM's phase_id to point at the verdict row,
        # severing the synthesis.phase_5 → fund_manager back-link that
        # the replay UI relies on.
        prior_fm_phase_id: int | None = fm_row[1] if fm_row is not None else None
        fm_ids: list[int] = [fm_report_id] if fm_report_id is not None else []

        # Cross-surface consistency: derive the audit row's approved /
        # reasons from ``decision_run.fund_manager_decision`` — the SINGLE
        # source of truth the promotion gate consults — NOT from the stale
        # local ``approved`` (set off the FM result at line ~1011). A
        # whole-artifact reader BLOCK can have flipped that field to
        # "rejected" AFTER the FM approved; the forensic/replay row must
        # tell the same story as the gate (output-trust doctrine), never
        # record approved=True while the gate says rejected.
        gate_decision = decision_run.fund_manager_decision
        gate_approved = gate_decision == "approved"
        _reader_blocked = (
            _reader_verdict is not None
            and _reader_verdict.overall_assessment == "BLOCK"
        )
        _reasons = [
            f"synthesis completed; draft_id={draft.id}",
            f"fund_manager verdict: {'approved' if approved else 'rejected'}",
            f"phase_4 risk verdict text length: {len(risk_verdict)}",
        ]
        if not gate_approved and approved and _reader_blocked:
            # FM approved but the reader tightened the gate — say why.
            _reasons.append(
                "whole-artifact reader BLOCK: "
                f"{len(_reader_verdict.findings)} coherence finding(s)"
            )
        verdict = FundManagerPlanRevisionDecision(
            approved=gate_approved,
            reasons=_reasons,
            cited_sources=["docs/design/SDD.md#§6.11"],
        )
        asyncio.run(record_negotiation_phase(
            user_id=user_id,
            decision_run_id=decision_run_id,
            kind="plan_synthesis.verdict",
            started_at=decision_run.started_at,
            agent_report_ids=fm_ids,
            verdict=verdict,
        ))

        # Restore the FM's original phase_id back-link if the recorder
        # clobbered it. The verdict row references the FM via its
        # participants_json (the columnar back-link is now duplicated in
        # the JSON), but the FM's authoritative phase_id stays at
        # synthesis.phase_5 — the actual debate phase it participated
        # in. Best-effort: any failure logs and continues so synthesis
        # itself never aborts because of audit-trail hygiene.
        if fm_report_id is not None and prior_fm_phase_id is not None:
            try:
                session.execute(
                    _update(_AgentReportRow)
                    .where(_AgentReportRow.id == fm_report_id)
                    .values(phase_id=prior_fm_phase_id)
                )
                session.commit()
            except Exception as restore_exc:  # noqa: BLE001
                log.warning(
                    "plan_synthesis.fm_phase_id_restore_failed",
                    user_id=user_id,
                    decision_run_id=decision_run_id,
                    error=str(restore_exc),
                )
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "plan_synthesis.record_phase_failed",
            user_id=user_id, decision_run_id=decision_run_id, error=str(exc),
        )

    return SynthesisResult(decision_run_id=decision_run_id, draft_id=draft.id)


# ----------------------------------------------------------------------
# Fire-and-forget cache warmer for FM objection translations.
#
# The user's first GET /api/plan/draft/objections previously paid 100+
# seconds for N parallel Sonnet translator calls (run #4131b69 measured
# 125 s for 6 objections). By warming the
# ``fm_objection_translations`` cache eagerly at synthesis completion,
# every subsequent /plan load returns instantly. Failures here are
# logged and swallowed — the on-demand path on the route is still the
# fallback.
#
# Why a separate Thread rather than asyncio.create_task?
#   ``run_synthesis`` is a sync function executed on a worker thread
#   (FastAPI BackgroundTasks → threadpool, or directly from sync code
#   in tests). There's no running event loop to attach to, and even if
#   there were, the cache helper's ``_run_async`` already spins up its
#   own loop. A daemon thread is the minimal, portable mechanism.
# ----------------------------------------------------------------------


def _schedule_fm_objection_translation_precompute(
    *,
    session: Session,
    user_id: str,
    plan_version_id: int,
    decision_run_id: int,
) -> None:
    """Fire-and-forget: schedule cache-warming of FM objection translations.

    Spawns a daemon thread that runs ``_precompute_fm_objection_translations``
    against a fresh session bound to the same engine as the orchestrator's
    session. The thread is daemonised so it doesn't block process exit if
    the user's first /plan load arrives before the precompute finishes
    (the on-demand path takes over as the fallback in that case).

    Early-exits BEFORE spawning the thread when:
      * the FM agent_report row for this decision_run is missing (nothing
        to parse — the trail ingest must have failed earlier), OR
      * cache rows already exist for ``plan_version_id`` (some other
        path already warmed it; the get_or_compute_translations helper
        has its own hash-based dedupe but we skip spawning a thread
        entirely as a cheaper guard).

    The actual translator parsing + dispatch lives in the thread function
    so the orchestrator's call site stays a near-constant-time scheduler.
    """
    from sqlalchemy import select as _select
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import AgentReport, FMObjectionTranslation

    # Early-exit guard #1: if the FM agent_report didn't make it to the
    # DB (trail ingest failed upstream), there's nothing to parse. Log
    # and bail without spawning a thread.
    decision_id_str = f"plan-synth-{decision_run_id}"
    fm_exists = session.execute(
        _select(AgentReport.id).where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "fund_manager",
        ).limit(1)
    ).scalar_one_or_none()
    if fm_exists is None:
        log.info(
            "fm_objection_translation_precompute.skipped_no_fm_report",
            user_id=user_id,
            decision_run_id=decision_run_id,
            plan_version_id=plan_version_id,
        )
        return

    # Early-exit guard #2: cache already warm for this draft. Cheap COUNT
    # before paying for a thread spawn.
    cached_count = session.execute(
        _select(FMObjectionTranslation.id).where(
            FMObjectionTranslation.plan_version_id == plan_version_id,
        ).limit(1)
    ).scalar_one_or_none()
    if cached_count is not None:
        log.info(
            "fm_objection_translation_precompute.skipped_already_cached",
            user_id=user_id,
            decision_run_id=decision_run_id,
            plan_version_id=plan_version_id,
        )
        return

    # Bind a fresh sessionmaker to the same engine as the orchestrator's
    # session. The orchestrator session itself is closed by its caller
    # after run_synthesis returns; the background thread can't reuse it.
    engine = session.get_bind()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    import threading

    thread = threading.Thread(
        target=_precompute_fm_objection_translations,
        kwargs={
            "session_factory": session_factory,
            "user_id": user_id,
            "plan_version_id": plan_version_id,
            "decision_run_id": decision_run_id,
        },
        daemon=True,
        name=f"fm-objection-precompute-{decision_run_id}",
    )
    thread.start()
    log.info(
        "fm_objection_translation_precompute.scheduled",
        user_id=user_id,
        decision_run_id=decision_run_id,
        plan_version_id=plan_version_id,
        thread_name=thread.name,
    )


def _precompute_fm_objection_translations(
    *,
    session_factory,
    user_id: str,
    plan_version_id: int,
    decision_run_id: int,
) -> None:
    """Background worker: parse FM objections + warm the translation cache.

    Mirrors the parsing logic in ``argosy.api.routes.plan.get_draft_objections``
    (same ``_parse_fm_response`` / ``_split_reason`` / ``_classify_severity``)
    so the rows it persists are byte-identical to what the on-demand path
    would compute on a cache miss — letting the route's hash-based
    invalidation use them as cache hits without re-translating.

    All exceptions are logged + swallowed. The on-demand route path
    remains the fallback when this warm-fill fails for any reason
    (translator down, DB locked, etc.).
    """
    # Import inside the worker so the orchestrator's import-time graph
    # stays free of the route module (circular import risk via Pydantic
    # response models that pull in plan_synthesizer_types).
    from sqlalchemy import select as _select

    from argosy.api.routes.plan import (
        _classify_severity,
        _parse_fm_response,
        _split_reason,
    )
    from argosy.services.fm_objection_translation_cache import (
        get_or_compute_translations,
    )
    from argosy.state.models import AgentReport

    log.info(
        "fm_objection_translation_precompute.started",
        user_id=user_id,
        decision_run_id=decision_run_id,
        plan_version_id=plan_version_id,
    )

    db = session_factory()
    try:
        decision_id_str = f"plan-synth-{decision_run_id}"
        fm_row = db.execute(
            _select(AgentReport).where(
                AgentReport.user_id == user_id,
                AgentReport.decision_id == decision_id_str,
                AgentReport.agent_role == "fund_manager",
            ).order_by(AgentReport.created_at.desc()).limit(1)
        ).scalar_one_or_none()

        if fm_row is None or not fm_row.response_text:
            log.info(
                "fm_objection_translation_precompute.skipped_no_fm_response_text",
                user_id=user_id,
                decision_run_id=decision_run_id,
                plan_version_id=plan_version_id,
            )
            return

        parsed = _parse_fm_response(fm_row.response_text)
        reasons = parsed.get("reasons") or []
        cited = [
            c for c in (parsed.get("cited_sources") or []) if isinstance(c, str)
        ]

        objections: list[dict] = []
        for r in reasons:
            if not isinstance(r, str) or not r.strip():
                continue
            topic, detail = _split_reason(r)
            sev = _classify_severity(topic, detail)
            objections.append(
                {"severity": sev, "topic": topic, "detail": detail}
            )

        if not objections:
            log.info(
                "fm_objection_translation_precompute.skipped_no_objections",
                user_id=user_id,
                decision_run_id=decision_run_id,
                plan_version_id=plan_version_id,
            )
            return

        translations = get_or_compute_translations(
            db,
            user_id=user_id,
            plan_version_id=plan_version_id,
            objections=objections,
            cited_sources=cited,
        )

        log.info(
            "fm_objection_translation_precompute.done",
            user_id=user_id,
            decision_run_id=decision_run_id,
            plan_version_id=plan_version_id,
            objections_count=len(objections),
            translations_count=len(translations),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort warm-fill
        log.warning(
            "fm_objection_translation_precompute.failed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            plan_version_id=plan_version_id,
            error=str(exc),
        )
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover — defensive
            pass


# ----------------------------------------------------------------------
# Phase implementations (default — call existing fleet agents)
# ----------------------------------------------------------------------


# Resolved at call time via the package's sys.modules entry so
# monkeypatch.setattr on the package-level names (used by tests) takes effect.
# Capturing the class refs in a tuple at import time would freeze them and
# bypass the patch.
# Names are alphabetical for deterministic log output.
_PHASE_1_AGENT_NAMES_CORE = (
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

# Phase 5 — topic-owner agents, gated behind the
# ``ARGOSY_PHASE5_AGENTS`` env var. When False (default), the fleet
# stays at its 10-member shape. When True, PlanCoverageAnalyst +
# WithdrawalSequencerAgent + EquityCompAnalystAgent join the fleet so
# they run on every cycle. See
# docs/plans/argosy-comprehensive-plan-integration.md §8.
_PHASE_5_AGENT_NAMES = (
    "PlanCoverageAnalyst",
    "WithdrawalSequencerAgent",
    "EquityCompAnalystAgent",
)


def _resolve_phase_1_agent_names() -> tuple[str, ...]:
    """Return the active Phase 1 agent class names.

    Includes Phase 5 agents only when ``settings.phase5_agents`` is
    True so the default fleet shape stays 10-member until live-LLM
    iteration validates the new agents' output quality.
    """
    try:
        from argosy.config import get_settings
        if get_settings().phase5_agents:
            return _PHASE_1_AGENT_NAMES_CORE + _PHASE_5_AGENT_NAMES
    except Exception:  # pragma: no cover — defensive on import-cycle paths
        pass
    return _PHASE_1_AGENT_NAMES_CORE


# Back-compat: existing code paths read ``_PHASE_1_AGENT_NAMES``
# directly; preserve that name as an alias of the resolver result.
# This is a module-level computation, so toggling the flag at runtime
# requires a process restart — acceptable for a feature flag.
_PHASE_1_AGENT_NAMES = _resolve_phase_1_agent_names()


# ----------------------------------------------------------------------
# Run-completeness gate (codex-reviewed design, 2026-06-03).
#
# Tiered by AGENT_ROLE — the runtime identity, NOT the class name. A
# name/identity mismatch (e.g. "withdrawal_sequencer" vs
# "WithdrawalSequencerAgent") would silently bypass the gate — codex's #1
# flagged risk — so ``test_run_completeness_gate`` pins these roles to the
# active fleet's real agent_role attributes.
#
#   CRITICAL  -> failure ABORTS the run after phase 1. The output is a
#                load-bearing derivation for the plan's headline numbers
#                (FI target / retirement age, NVDA glide path, spend, tax,
#                RSU income). Building or promoting a plan without it lets
#                the synthesizer fabricate the missing number.
#   REQUIRED_FOR_PROMOTION -> does not abort phase 1, but the adversarial
#                challenge must have run before /accept can promote.
#                (Accept-gate enforcement is a follow-up.)
#   everything else -> degrade-with-disclosure; never blocks.
# ----------------------------------------------------------------------
_CRITICAL_AGENT_ROLES = frozenset({
    "concentration",          # NVDA deconcentration glide path + caps
    "withdrawal_sequencer",   # FI bridge / retirement-funding sequence
    "household_budget",       # canonical spend basis
    "tax",                    # tax treatment driving net figures
    "equity_comp_analyst",    # RSU income stream
})
_REQUIRED_FOR_PROMOTION_ROLES = frozenset({"plan_critique"})


def _active_agent_roles() -> set[str]:
    """The ``agent_role`` of every agent in the currently-active phase-1 fleet."""
    _pkg_mod = sys.modules["argosy.orchestrator.flows.plan_synthesis"]
    roles: set[str] = set()
    for name in _resolve_phase_1_agent_names():
        cls = getattr(_pkg_mod, name, None)
        role = getattr(cls, "agent_role", None)
        if role:
            roles.add(role)
    return roles


def _failed_critical_agents(failed_roles: list[str]) -> list[str]:
    """Critical agent_roles that RAN and FAILED this phase-1 cycle.

    ``failed_roles`` is the structured failed-set returned by
    ``_run_phase_1_analysts`` (agents whose run raised — schema error,
    crash, citation-gate, or unavailable input). Only critical agents in
    the ACTIVE fleet count (the phase-5 agents aren't expected when
    ``ARGOSY_PHASE5_AGENTS`` is off). An agent skipped as 'not applicable'
    is never in ``failed_roles``, so it doesn't trip the gate (codex's
    not-applicable nuance, e.g. a user with no RSUs).
    """
    return sorted(set(failed_roles) & _CRITICAL_AGENT_ROLES & _active_agent_roles())


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


def _agent_report_to_row_dict(r: AgentReport) -> dict:
    """Project an ``AgentReport`` dataclass into the column-name dict the
    ``agent_reports`` ORM row constructor expects.

    Single source of truth for both the JSONL forensic trail (W1.C-v4) and
    the T0.1 per-phase DB persistence path. Keeping the projection here
    means both paths emit identical row shapes — important because
    ``_ingest_synthesis_trail`` constructs an ``AgentReportRow`` from this
    dict at end-of-flow as a fallback.
    """
    return {
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
                row = _agent_report_to_row_dict(r)
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

    # T0.1 — when ``_record_phase_completion`` has already persisted per-
    # phase rows to the DB (via its async sub-session), those rows carry
    # a ``run_correlation_id`` matching the JSONL entry. Skip lines whose
    # correlation id is already in the DB so the end-of-flow ingest stays
    # a defensive no-op for the happy path (and the fallback for crashes
    # / sub-session failures, where rows live ONLY in the JSONL).
    try:
        from sqlalchemy import select as _select

        existing_corr_ids: set[str] = set(
            session.execute(
                _select(AgentReportRow.run_correlation_id).where(
                    AgentReportRow.decision_id == decision_audit_token,
                    AgentReportRow.run_correlation_id.is_not(None),
                )
            ).scalars().all()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_synthesis.trail_dedup_query_failed",
            error=str(exc),
        )
        existing_corr_ids = set()

    try:
        inserted = 0
        skipped = 0
        for row_dict in rows:
            corr = row_dict.get("run_correlation_id")
            if corr and corr in existing_corr_ids:
                skipped += 1
                continue
            ar = AgentReportRow(**row_dict)
            session.add(ar)
            inserted += 1
            if corr:
                existing_corr_ids.add(corr)
        session.commit()
        log.info(
            "plan_synthesis.trail_ingested",
            count=inserted, skipped_dedup=skipped, trail=trail_path.name,
        )
        return inserted
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


async def _persist_phase_agent_reports_async(
    agent_report_rows: list[AgentReport],
) -> list[int]:
    """Persist a phase's ``AgentReport`` dataclasses to the DB via an
    async sub-session and return the assigned integer ids (T0.1).

    Sub-session pattern mirrors ``_safe_run_agent`` — opening a short-
    lived session for this phase boundary avoids contention with the
    orchestrator's main sync ``Session`` (which holds the writer lock
    for the full ~12-15 min synthesis). The async engine uses the same
    underlying SQLite file; WAL + busy_timeout (see
    ``argosy.state.db.init_engine``) handles the writer-serialization.

    Returns the ids in the same chronological order as the input list so
    the recorder's ``participants_json`` reflects the order the agents
    actually ran in.

    Best-effort: on any failure (engine not initialised in a test, FK
    violation, etc.) we return an empty list and let the caller fall
    back to ``agent_report_ids=[]`` for the recorder. The end-of-flow
    JSONL ingest will still pick up the rows from disk as a defensive
    last-line fallback (see ``_ingest_synthesis_trail``).
    """
    if not agent_report_rows:
        return []

    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportORM

    ids: list[int] = []
    async with db_mod.get_session() as session:
        for row in agent_report_rows:
            orm = AgentReportORM(**_agent_report_to_row_dict(row))
            session.add(orm)
            await session.flush()
            ids.append(orm.id)
        await session.commit()
    return ids


def _record_phase_completion(
    *,
    user_id: str,
    decision_run_id: int,
    phase_n: int,
    started_at: datetime,
    phase_output: str | dict,
    agent_report_rows: list[AgentReport] | None = None,
) -> None:
    """Persist a per-phase output row to ``decision_phases`` (T2.3).

    Synchronous wrapper over the async recorder. Best-effort — failure
    here logs + continues so synthesis isn't broken by a forensic gap.

    The persisted row uses ``kind='synthesis.phase_<N>'`` so the resume
    helper can look it up. ``phase_output`` is opaque text (the phase's
    rendered output): str for analyst/debate/risk/fm phases, JSON dump
    for the synthesizer's structured ``PlanSynthesisOutput``.

    T0.1 — ``agent_report_rows`` (when supplied) is the list of
    ``AgentReport`` dataclasses produced by THIS phase, in chronological
    order. The function persists them to the ``agent_reports`` table via
    an async sub-session BEFORE calling the recorder, then threads the
    resulting integer ids into ``record_negotiation_phase`` so the
    phase's ``participants_json`` is populated and the agent_reports
    rows are back-linked via ``phase_id``. Without this, every phase row
    has ``participants_json='[]'`` and the ``/decisions/[id]`` sequence
    diagram is empty. When the per-phase DB write fails (e.g. async
    engine not initialised in a unit test), we fall back to passing an
    empty id list — the end-of-flow JSONL ingest will still surface the
    rows so the audit trail is intact, just not phase-linked.
    """
    try:
        import asyncio
        from argosy.services.negotiation_recorder import (
            record_negotiation_phase,
        )

        async def _run() -> None:
            # Sub-session persist FIRST so the recorder can reference
            # the freshly-assigned ids when it writes the phase row.
            ids: list[int] = []
            if agent_report_rows:
                try:
                    ids = await _persist_phase_agent_reports_async(
                        agent_report_rows,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "plan_synthesis.phase_agent_reports_persist_failed",
                        user_id=user_id,
                        decision_run_id=decision_run_id,
                        phase=phase_n,
                        count=len(agent_report_rows),
                        error=str(exc),
                    )
                    ids = []

            await record_negotiation_phase(
                user_id=user_id,
                decision_run_id=decision_run_id,
                kind=f"synthesis.phase_{phase_n}",
                started_at=started_at,
                agent_report_ids=ids,
                verdict=None,
                phase_output=phase_output,
            )
            _output_chars: int
            if isinstance(phase_output, str):
                _output_chars = len(phase_output)
            elif isinstance(phase_output, dict):
                _output_chars = len(json.dumps(phase_output, default=str))
            else:
                _output_chars = 0
            log.info(
                "plan_synthesis.phase_recorded",
                user_id=user_id,
                decision_run_id=decision_run_id,
                phase=phase_n,
                output_chars=_output_chars,
                participants=len(ids),
            )

        asyncio.run(_run())
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
                           decision_run_id, guidance) -> tuple[str, list[AgentReport]]:
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
    # Wave 1 follow-up — thread the user's free-text guidance into the
    # kwargs bag as ``user_directive`` so ``_safe_run_agent``'s
    # per-agent signature narrowing delivers it ONLY to analysts that
    # declare a ``user_directive`` parameter on their build_prompt.
    # Today that is exclusively ``PlanCritiqueAgent``; the 9 other
    # single-ticker analysts (concentration, fx, fundamentals, news,
    # sentiment, technical, macro, tax, household_budget) are pure
    # data-gatherers and their build_prompt signatures don't declare
    # ``user_directive``, so the narrowing filter drops it for them.
    # Empty (no guidance) is also fine: PlanCritique's default value
    # is "" which produces a byte-identical prompt to the no-kwarg call.
    common_kwargs["user_directive"] = guidance or ""

    reports: list[str] = []
    collected: list[AgentReport] = []
    # Structured failed-set (codex-reviewed run-completeness gate): the
    # ``agent_role`` of every analyst whose run RAISED this cycle. The
    # orchestrator gates on the CRITICAL subset of this set after phase 1.
    # Tracking the role (not just a text "(FAILED)" marker) means the gate
    # keys on runtime identity and can't be fooled by prose.
    failed_roles: list[str] = []
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
                # Failure of one analyst is recoverable at THIS layer — we
                # continue running the others — but the role is recorded so
                # the run-completeness gate can abort if it was a CRITICAL
                # analyst. Note in the concatenated text so the synthesizer
                # (and audit trail) sees it too.
                failed_roles.append(getattr(cls, "agent_role", cls.__name__))
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
    # T0.1 — return the collected reports alongside the text so the
    # orchestrator can persist + thread their ids into the recorder.
    # Existing call sites that stub this function with ``lambda **kw:
    # "text"`` keep working: the orchestrator detects the tuple shape
    # and defaults to ``[]`` when the stub returns a bare string.
    #
    # Return shape is now (text, collected, failed_roles) — the third
    # element feeds the run-completeness gate. The orchestrator detects
    # 3-tuple / 2-tuple / bare-string shapes, so legacy stubs that return
    # a string or a 2-tuple keep working (failed_roles defaults to []).
    return "\n\n".join(reports), collected, failed_roles


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
                         baseline, prior_current, decision_run_id, trigger,
                         guidance: str = "",
                         ) -> tuple[str, list[AgentReport]]:
    """Run bull/bear/facilitator across all three horizons in parallel.

    Each horizon argues theses, not trades. Per-horizon facilitator
    extracts a structured DebateOutcome record.

    W1.C-v2: per-horizon helper now returns ``(text, reports)``; this
    function collects all reports across the three horizons and
    bulk-persists once at phase end (single sync writer, no aiosqlite
    contention).

    ``guidance``: free-text user directive carried forward from
    ``run_synthesis``. When non-empty, it is forwarded as
    ``user_directive`` to the bull/bear/facilitator build_prompt calls
    so per-horizon debaters can otherwise re-raise the same concern the
    user has already AGREED with. Without this thread the bull, bear,
    and facilitator agents would feed the synthesizer their unchanged
    reasoning and force the synthesizer to overrule them with extra
    tokens.
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
                guidance=guidance,
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

    # T0.1 — surface the collected reports so the orchestrator can
    # persist them in the DB and thread their ids into the recorder.
    return "\n\n".join(parts), collected


def _run_one_horizon_debate(*, horizon: str, user_id: str,
                             analyst_reports_text: str,
                             baseline, prior_current, decision_run_id: str,
                             trigger: str,
                             guidance: str = "",
                             ) -> tuple[str, list[AgentReport]]:
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
        user_directive=guidance,
        decision_id=decision_run_id,
    )
    bull_turn = bull_report.output if hasattr(bull_report, "output") else None
    bull_turn_dict = bull_turn.model_dump() if bull_turn is not None else {}

    # T2.6b — orchestrator-level retry on the bear_researcher claude.exe
    # exit-1 flake. BaseAgent already retries N=3 *inside* one SDK session;
    # this loop restarts the whole call (fresh subprocess) when that
    # internal budget is exhausted and the AgentRunError escapes anyway.
    # Live evidence (#29): two horizons died to back-to-back exit-1 flakes
    # despite the SDK-level retry, taking down 6 of 9 phase-2 reports. The
    # orchestrator retry handles ONLY the transient_exit1 + empty-stderr
    # fingerprint — see `_is_bear_transient_flake` — so deterministic
    # failures (schema validation, citation gate) still surface on the
    # first attempt without doubling cost.
    bear_report = None
    bear_last_exc: BaseException | None = None
    for _bear_attempt in range(_BEAR_RESEARCHER_MAX_ATTEMPTS):
        try:
            bear_report = bear.run_sync(
                analyst_reports=analyst_reports_payload,
                prior_rounds=[bull_turn_dict] if bull_turn_dict else [],
                round_index=1,
                n_max=2,
                ticker=ticker,
                user_directive=guidance,
                decision_id=decision_run_id,
            )
            break
        except AgentRunError as exc:
            bear_last_exc = exc
            is_final = _bear_attempt + 1 >= _BEAR_RESEARCHER_MAX_ATTEMPTS
            if not _is_bear_transient_flake(exc) or is_final:
                # Deterministic failure OR last attempt — surface.
                log.error(
                    "plan_synthesis.bear_researcher.retry_exhausted",
                    horizon=horizon,
                    decision_run_id=decision_run_id,
                    attempt=_bear_attempt + 1,
                    max_attempts=_BEAR_RESEARCHER_MAX_ATTEMPTS,
                    is_transient_flake=_is_bear_transient_flake(exc),
                    error=str(exc)[:500],
                )
                raise
            delay = _BEAR_RESEARCHER_RETRY_BACKOFF_SECONDS[_bear_attempt]
            log.warning(
                "plan_synthesis.bear_researcher.orchestrator_retry",
                horizon=horizon,
                decision_run_id=decision_run_id,
                attempt=_bear_attempt + 1,
                max_attempts=_BEAR_RESEARCHER_MAX_ATTEMPTS,
                delay_seconds=delay,
                error=str(exc)[:500],
            )
            time.sleep(delay)
    if bear_report is None:  # pragma: no cover - defensive; loop always sets or raises
        raise AgentRunError(
            f"bear_researcher: orchestrator retry exhausted without "
            f"raising; horizon={horizon}; last_exc={bear_last_exc}"
        )
    bear_turn = bear_report.output if hasattr(bear_report, "output") else None
    bear_turn_dict = bear_turn.model_dump() if bear_turn is not None else {}

    fac_report = fac.run_sync(
        bull_turns=[bull_turn_dict] if bull_turn_dict else [],
        bear_turns=[bear_turn_dict] if bear_turn_dict else [],
        rounds_run=1,
        ticker=ticker,
        user_directive=guidance,
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


# Spec C commit #6 — sources whose reliability informs the synthesizer's
# input weighting per §6.1. The banner is prepended to the synth's
# analyst_reports_text so the LLM can dim signals from sources with
# poor recent hit-rate. Order matters for readability; we keep internal
# producers first (they're the most-trusted signal family by
# construction — Argosy controls their prompts) and external sources
# after.
_SYNTH_RELIABILITY_SOURCES: tuple[str, ...] = (
    "internal_per_position_thesis",
    "internal_news_signal_analyst",
    "internal_state_observer",
    "discord",
    "news",
)


def _build_source_reliability_preamble(
    session, user_id: str
) -> str:
    """Render the per-source reliability banner per spec §6.1.

    Returns a multi-line string the synthesizer prepends to
    ``analyst_reports_text``. Empty string when the predictions ledger
    has no scored data for ANY of the tracked sources (the first
    fresh-install case — the synth then runs unchanged).

    Best-effort: any failure (FK violation on a half-seeded
    ``evaluation_method_registry`` in a legacy env, importerror) logs
    and returns ``""`` so synthesis never breaks because of a missing
    reliability surface.
    """
    try:
        from argosy.services.predictions.reliability import (
            get_source_reliability,
            get_weight_for_source,
        )
    except Exception:  # noqa: BLE001 — never break synth
        log.warning("plan_synthesis.reliability_preamble.import_failed")
        return ""

    try:
        # Single bulk query over all sources; the cache + view's GROUP BY
        # makes this a near-constant-time call. We filter to the
        # tracked sources in-Python.
        all_rows = get_source_reliability(session, user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_synthesis.reliability_preamble.query_failed",
            error=str(exc),
        )
        return ""

    if not all_rows:
        return ""

    # Pick ONE row per source (prefer fixed_lookahead family — the
    # default for most internal predictions; spec §6.1 doesn't pin a
    # family so the most-common one is the right default).
    rows_by_source: dict[str, object] = {}
    for r in all_rows:
        if r.source not in _SYNTH_RELIABILITY_SOURCES:
            continue
        # Prefer fixed_lookahead family when multiple families exist
        # for the same source. First-write-wins for other families.
        existing = rows_by_source.get(r.source)
        if existing is None:
            rows_by_source[r.source] = r
        elif (
            getattr(existing, "method_family", "") != "fixed_lookahead"
            and r.method_family == "fixed_lookahead"
        ):
            rows_by_source[r.source] = r

    if not rows_by_source:
        return ""

    lines: list[str] = ["Source reliability (recent scoring window):"]
    for source in _SYNTH_RELIABILITY_SOURCES:
        rel = rows_by_source.get(source)
        if rel is None:
            continue
        # The weight here is FOR-DISPLAY only — the synthesizer prompt
        # uses it to dim signals in its reasoning, NOT to multiply a
        # numeric score post-hoc. The predictions written by this
        # synth's downstream emit_thesis_predictions call carry
        # provenance_weights_applied=True so the next consumer skips
        # re-application.
        #
        # Codex review IMPORTANT 2 fix (2026-05-29) — query the weight
        # under the SAME family the displayed stats came from. The
        # earlier shape hardcoded ``"fixed_lookahead"`` which produced
        # source/family pairs that disagreed when the source had only
        # ``target_stop`` / ``multi_basket`` / ``unparseable`` data
        # (n + hit_rate from family A, weight from family B / 1.0
        # fallback). Now both numbers come from the same family slot.
        family = getattr(rel, "method_family", "fixed_lookahead")
        weight = get_weight_for_source(
            session, user_id, source, family,
            provenance_weights_applied=False,
        )
        n = rel.scored_predictions  # type: ignore[attr-defined]
        hr = rel.hit_rate  # type: ignore[attr-defined]
        if n < 10 or hr is None:
            lines.append(
                f"  {source} [{family}]: n={n} — "
                "insufficient sample, weight=1.00×"
            )
        else:
            lines.append(
                f"  {source} [{family}]: "
                f"{hr * 100:.0f}% hit_rate, n={n} → weight {weight:.2f}×"
            )
    lines.append(
        "Weight signals from each source proportionally when forming the plan. "
        "Per spec §6.6 anti-feedback-loop contract, the synth's own output "
        "is stamped provenance_weights_applied=1 so downstream consumers do "
        "NOT re-apply these weights."
    )
    return "\n".join(lines) + "\n\n"


_NUMERIC_METHODOLOGY_TOPICS = (
    "fabricat", "headline", "fi target", "fi_target", "methodolog", "yield",
    "swr", "withdrawal rate", "spend basis", "spend_basis", "net worth",
    "retirement age", "uncited", "contradict", "21m", "₪21", "derivation",
)


def _codex_first_numeric_topic(codex_opinion) -> str:
    """Short label of the FIRST numeric/methodology objection codex blocked on.

    Used purely for the visible reconcile marker on /decisions/[id] — gives
    the user a one-glance "what did codex push back on" hint. Returns "" when
    none can be identified (the marker still renders without a topic).
    """
    if codex_opinion is None:
        return ""
    for f in getattr(codex_opinion, "findings", None) or []:
        topic = (getattr(f, "topic", "") or "").lower()
        detail = (getattr(f, "detail", "") or "").lower()
        if any(t in f"{topic} {detail}" for t in _NUMERIC_METHODOLOGY_TOPICS):
            return getattr(f, "topic", "") or ""
    return ""


def _codex_numeric_reconcile_guidance(codex_opinion) -> str | None:
    """Return reconcile guidance when codex BLOCKED on a numeric/methodology
    finding, else None.

    The forcing loop only re-runs synthesis for findings the synthesizer can
    actually act on: a fabricated/uncited/contradictory headline number or an
    indefensible FI methodology. A BLOCKER on an unrelated topic (e.g. a tax-
    sequencing concern) is left to the Fund Manager — re-running synth would
    not address it and would just burn a round.
    """
    if codex_opinion is None:
        return None
    findings = getattr(codex_opinion, "findings", None) or []
    hits: list[str] = []
    for f in findings:
        sev = getattr(f, "severity", "")
        if sev not in ("BLOCKER", "AMBER"):
            continue
        topic = (getattr(f, "topic", "") or "").lower()
        detail = (getattr(f, "detail", "") or "").lower()
        blob = f"{topic} {detail}"
        if any(t in blob for t in _NUMERIC_METHODOLOGY_TOPICS):
            fix = getattr(f, "suggested_fix", "") or ""
            hits.append(
                f"- [{sev}] {getattr(f, 'topic', '')}: "
                f"{getattr(f, 'detail', '')}"
                + (f"  FIX: {fix}" if fix else "")
            )
    # Only force a reconcile when the overall verdict is a hard BLOCK and at
    # least one finding is numeric/methodology (AMBER-only → advisory, the FM
    # handles it).
    overall = getattr(codex_opinion, "overall_assessment", "")
    has_blocker = any(
        getattr(f, "severity", "") == "BLOCKER" for f in findings
        if any(
            t in f"{(getattr(f, 'topic', '') or '').lower()} "
            f"{(getattr(f, 'detail', '') or '').lower()}"
            for t in _NUMERIC_METHODOLOGY_TOPICS
        )
    )
    if overall == "BLOCK" and has_blocker and hits:
        return (
            "ADVERSARIAL REVIEW — numeric/methodology objections you MUST "
            "resolve in this revision. Every headline number must match the "
            "DERIVED HEADLINE NUMBERS block exactly (or be written "
            "`[derivation pending]`), and the FI framing must be consistent "
            "with the derived target + methodology. Objections:\n"
            + "\n".join(hits)
        )
    return None


# The whole-artifact reader's COHERENCE kinds — a finding of one of these kinds
# (or an "other" finding that quotes conflicting surfaces) is a prose-level
# coherence hole the synthesizer can actually CORRECT by re-running: reconcile
# the two labels/values, fix the stale date, qualify the fragile claim. The
# synthetic fail-closed BLOCK the reader emits on a timeout / unparseable /
# empty-artifact run is kind="other" with NO surfaces_cited — re-running
# synthesis cannot fix a reader that never produced a verdict, so it is
# EXCLUDED (it would only burn a 15-min reconcile round).
_READER_COHERENCE_KINDS = frozenset(
    {"contradiction", "cross_surface", "fragile_claim", "stale", "regression"}
)


def _reader_finding_is_fixable(finding) -> bool:
    """A reader finding the synthesizer can act on by re-running.

    True for any coherence-kind finding, and for an ``other`` finding that
    quotes conflicting surfaces (real signal). False for the reader's
    synthetic infra-failure BLOCK (kind=``other`` with empty ``surfaces_cited``
    — a timeout / dispatch failure / empty artifact), which re-synthesis cannot
    repair.
    """
    kind = (getattr(finding, "kind", "") or "").lower()
    if kind in _READER_COHERENCE_KINDS:
        return True
    surfaces = getattr(finding, "surfaces_cited", None) or []
    return kind == "other" and len(surfaces) > 0


def _reader_coherence_reconcile_guidance(reader_verdict) -> str | None:
    """Return reconcile guidance when the reader BLOCKED on a FIXABLE coherence
    hole, else None.

    Mirrors ``_codex_numeric_reconcile_guidance`` for the whole-artifact reader.
    Unlike codex (whose blockers span topics the synthesizer cannot fix, e.g.
    tax-sequencing), the reader's ENTIRE remit is coherence of the assembled
    prose — exactly what a re-synthesis addresses — so it fires on ANY hard
    BLOCK carrying at least one fixable BLOCKER finding. The conflicting
    surfaces are quoted verbatim so the synthesizer can reconcile them to one
    value/label/claim. Only a hard BLOCK forces a round (AMBER/YELLOW are
    advisory — the FM handles them); an infra-failure BLOCK is excluded (see
    ``_reader_finding_is_fixable``).
    """
    if reader_verdict is None:
        return None
    if getattr(reader_verdict, "overall_assessment", "") != "BLOCK":
        return None
    findings = getattr(reader_verdict, "findings", None) or []
    hits: list[str] = []
    for f in findings:
        if getattr(f, "severity", "") != "BLOCKER":
            continue
        if not _reader_finding_is_fixable(f):
            continue
        surfaces = getattr(f, "surfaces_cited", None) or []
        quoted = "; ".join(f"{s!r}" for s in surfaces)
        hits.append(
            f"- [{getattr(f, 'kind', '')}] {getattr(f, 'detail', '')}"
            + (f"  CONFLICTING SURFACES: {quoted}" if quoted else "")
        )
    if not hits:
        return None
    return (
        "ADVERSARIAL COHERENCE REVIEW — a hostile reader of the WHOLE assembled "
        "plan found contradictions / stale content / fragile headline claims "
        "you MUST resolve in this revision. For each: make every surface state "
        "the SAME concept with the SAME value AND the same (or an explicitly "
        "distinguished) label; render any past-due date as 'overdue', never "
        "'on-deck'/'0 days'; and qualify — do NOT delete or hide — any headline "
        "claim the plan's own risk/concentration/FX sections undercut. "
        "Coherence holes:\n"
        + "\n".join(hits)
    )


def _reader_first_objection_topic(reader_verdict) -> str:
    """Short label of the FIRST fixable coherence hole the reader blocked on.

    Used purely for the visible ``reader_reconcile`` marker on /decisions/[id]
    (mirrors ``_codex_first_numeric_topic``). Returns "" when none identified.
    """
    if reader_verdict is None:
        return ""
    for f in getattr(reader_verdict, "findings", None) or []:
        if getattr(f, "severity", "") == "BLOCKER" and _reader_finding_is_fixable(f):
            return (getattr(f, "kind", "") or "").strip()
    return ""


@dataclass
class _SurgicalPrepassResult:
    """Outcome of the surgical reconcile pre-pass."""

    corrected_text: str
    corrected_fact_ids: list
    structural_findings: list


def _surgical_reconcile_prepass(
    *, artifact_text, findings, ledger, canonical_values, gate_kwargs,
):
    """Fix renderable findings at their canonical fact + render sites BEFORE the
    full-resynth fallback. Deterministic for template/structured sites; structural
    / unattributable findings are returned for the caller to route to re-synth.

    Pure (no LLM here — prose-editor calls are the caller's responsibility for
    llm_prose sites). The whole-artifact reader + full re-synth remain downstream;
    this NEVER demotes them (spec: demotion waits until the invariant graph covers
    all run-106 classes — [8]-[10] still deferred).
    """
    from argosy.quality.fact_attribution import attribute_finding
    from argosy.quality.fact_correction import (
        apply_text_corrections,
        rerender_deterministic_sites,
        route_finding,
    )

    corrected = artifact_text or ""
    corrected_fact_ids: list = []
    structural: list = []

    for finding in findings or []:
        # Per-finding isolation: a malformed finding shape must never abort the
        # synthesis (the prepass is best-effort; full re-synth is the fallback).
        try:
            if route_finding(finding, ledger) == "structural":
                structural.append(finding)
                continue
            for loc in attribute_finding(finding, ledger):
                fid = loc.fact_id
                if fid is None or fid not in canonical_values:
                    continue
                patches = rerender_deterministic_sites(fid, canonical_values[fid], ledger)
                corrected = apply_text_corrections(corrected, patches, prose_edits=[])
                if fid not in corrected_fact_ids:
                    corrected_fact_ids.append(fid)
        except Exception:  # noqa: BLE001 — best-effort; route to structural fallback
            structural.append(finding)

    return _SurgicalPrepassResult(
        corrected_text=corrected,
        corrected_fact_ids=corrected_fact_ids,
        structural_findings=structural,
    )


def _assemble_draft_bodies(session, *, output, user_id, decision_run_id,
                           alternatives_sleeve):
    """Render every user-facing + audit body field of the draft from a synth
    ``output``.

    The SINGLE source for both the initial persist AND the reader-reconcile
    re-persist, so a reconciled draft is identical in shape to a normally-built
    one (a divergence between the two paths would itself be a new defect class:
    a reconciled body failing gates a normal body would not). Returns a dict of
    ``PlanVersion`` body fields.
    """
    import json as _json
    import os as _os
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    # Build the canonical instrument-level TargetAllocationDoc FIRST — BEFORE any
    # horizon/appendix render — so allocation sites render FROM the canonical doc
    # (codex v2 #6: today's order rendered markdown first then resolved the doc
    # after, which let IPS prose and the canonical allocation diverge). The
    # produced JSON is unchanged; only the ordering moves. Best-effort + never
    # fatal. T1.5 — persist so every surface projects ONE plan.
    from argosy.services.target_allocation_doc import (
        resolve_target_allocation_json,
    )

    _target_allocation_json = resolve_target_allocation_json(
        session, user_id, decision_run_id, datetime.now(timezone.utc).date(),
        alternatives_sleeve=alternatives_sleeve,
    )

    # v4 block B1 — assemble the three plan-doc appendices ONCE (sections are
    # global, not per-horizon) and append to the LONG horizon only (the
    # strategic frame anchors the evidence + assumption ledger).
    _long_md = _pkg._horizon_md_user(output.long)
    _appendices = _pkg.render_plan_appendices(
        output, session=session, decision_run_id=decision_run_id,
    )
    if _appendices:
        _long_md = _long_md.rstrip() + "\n\n" + _appendices
    _medium_md = _pkg._horizon_md_user(output.medium)
    _short_md = _pkg._horizon_md_user(output.short)
    # Build the sections JSON HERE (not at return) so it flows through the SAME
    # placeholder-render + fail-safe path as the horizon bodies. The structured
    # sections carry {{fact:}} tokens in their body_md/evidence too and are
    # client-facing (action items, derivation blocks) — they must not leak raw.
    _sections_json = _json.dumps(
        [s.model_dump(mode="json") for s in output.sections]
    )

    # #24 PRIMARY gate — scrub headline numbers against the deterministic
    # resolver manifest so a synth-fabricated/stale figure never reaches the
    # body. Best-effort: a resolver failure leaves it unscrubbed (the /accept
    # gate is the backstop).
    try:
        from argosy.quality.numeric_source_gate import (
            scrub_headline_numeric_source,
        )
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers

        _drun_int = _decision_run_int(decision_run_id)
        if _drun_int is not None:
            # include_canonical_ages=True so canonical-age placeholders
            # ({{fact:retirement.earliest_safe_age}} / preservation_age) + the
            # canonical MC spend / allocation resolve and render; without it those
            # tokens stay pending and leak raw into the client body.
            _manifest = resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=_drun_int,
                include_canonical_ages=True,
            )
            _scrubbed, _scrub_log = scrub_headline_numeric_source(
                {"long": _long_md, "medium": _medium_md, "short": _short_md},
                _manifest,
            )
            _long_md = _scrubbed["long"]
            _medium_md = _scrubbed["medium"]
            _short_md = _scrubbed["short"]
            if _scrub_log:
                log.warning(
                    "plan_synthesis.headline_numbers_scrubbed",
                    user_id=user_id, decision_run_id=decision_run_id,
                    count=len(_scrub_log), tokens=_scrub_log[:20],
                )
            # Canonical fact placeholders (default OFF — ARGOSY_FACT_PLACEHOLDERS=1).
            # When the synthesizer emits {{fact:key}} tokens, render them from the
            # SAME resolver manifest so the body's numbers ARE the canonical ones
            # (no LLM-typed drift). No-op on current output (no placeholders).
            # Non-strict here: an unresolved token is left for the gate to surface
            # rather than aborting this best-effort block.
            if _os.environ.get("ARGOSY_FACT_PLACEHOLDERS", "0") == "1":
                from argosy.quality.fact_registry import render_placeholders
                _long_md = render_placeholders(_long_md, _manifest, strict=False)
                _medium_md = render_placeholders(_medium_md, _manifest, strict=False)
                _short_md = render_placeholders(_short_md, _manifest, strict=False)
                # The rendered values (₪11.69M, 12.0%, age 46) carry no JSON-special
                # characters, so substituting tokens inside the serialized JSON
                # string keeps it valid JSON while single-sourcing the section bodies.
                _sections_json = render_placeholders(_sections_json, _manifest, strict=False)
    except Exception as exc:  # noqa: BLE001 — scrub is defense-in-depth
        log.warning(
            "plan_synthesis.headline_scrub_failed",
            user_id=user_id, error=str(exc),
        )

    # Fail-safe: a raw {{fact:key}} token must NEVER reach the client body. If the
    # render above was skipped (a failure in the try-block) or a key was genuinely
    # unresolved, downgrade any surviving placeholder to the sanctioned pending
    # literal rather than ship literal braces. Runs unconditionally so it cannot be
    # bypassed by an exception in the render path (the bug that leaked 77 tokens).
    if "{{fact:" in (_long_md + _medium_md + _short_md + _sections_json):
        import re as _re
        from argosy.quality.fact_registry import PENDING_LABEL as _PENDING
        _leftover = _re.compile(r"\{\{fact:[A-Za-z0-9_.]+\}\}")
        _n_left = sum(
            len(_leftover.findall(t))
            for t in (_long_md, _medium_md, _short_md, _sections_json)
        )
        log.warning(
            "plan_synthesis.fact_placeholders_unrendered",
            user_id=user_id, decision_run_id=decision_run_id, count=_n_left,
        )
        _long_md = _leftover.sub(_PENDING, _long_md)
        _medium_md = _leftover.sub(_PENDING, _medium_md)
        _short_md = _leftover.sub(_PENDING, _short_md)
        _sections_json = _leftover.sub(_PENDING, _sections_json)

    # De-jargon then strip history leaks over the FULL assembled body (the
    # appendix is included). Order: jargon → history, matching the prior inline.
    _long_md = _pkg._strip_history_leak(_pkg._strip_jargon(_long_md))
    _medium_md = _pkg._strip_history_leak(_pkg._strip_jargon(_medium_md))
    _short_md = _pkg._strip_history_leak(_pkg._strip_jargon(_short_md))

    return {
        "horizon_long_md": _long_md,
        "horizon_medium_md": _medium_md,
        "horizon_short_md": _short_md,
        "horizon_long_md_audit": _pkg._horizon_md_audit(output.long),
        "horizon_medium_md_audit": _pkg._horizon_md_audit(output.medium),
        "horizon_short_md_audit": _pkg._horizon_md_audit(output.short),
        "target_allocation_json": _target_allocation_json,
        "sections_json": _sections_json,
    }


def _decision_run_int(decision_run_id) -> int | None:
    """Extract the integer decision_run PK from the orchestrator's token.

    The orchestrator threads ``decision_run_id`` as the string audit token
    ``"plan-synth-<int>"`` through most helpers, but the resolver keys on the
    int. Tolerate either form; return None when neither parses (caller
    degrades gracefully).
    """
    if isinstance(decision_run_id, int):
        return decision_run_id
    if isinstance(decision_run_id, str):
        tail = decision_run_id.rsplit("-", 1)[-1]
        try:
            return int(tail)
        except (ValueError, TypeError):
            return None
    return None


def _run_phase_3_synthesizer(*, session, user_id, baseline, prior_current,
                             analyst_reports_text, debate_outcomes_text,
                             portfolio_summary, fills_summary,
                             decision_run_id,
                             speculation_cap_pct: float | None = None,
                             speculation_cap_concurrent: int | None = None,
                             guidance: str = "",
                             ) -> tuple[PlanSynthesisOutput, list[AgentReport]]:
    """Default Phase 3: call PlanSynthesizerAgent.

    ``speculation_cap_pct`` / ``speculation_cap_concurrent`` (Wave 3, Task
    3.2): when set, the synthesizer prompt includes a HARD CONSTRAINT
    block telling the model to keep speculative_candidates within those
    bounds.  Defense-in-depth: ``_enforce_speculation_cap`` re-validates
    after the model returns, so a model that fluffs the constraint cannot
    harm the user.  Both kwargs default to None for backwards compat with
    tests / call sites that don't load the cap.

    ``guidance``: free-text user directive carried forward from
    ``run_synthesis``. When non-empty, it is forwarded as
    ``user_directive`` to ``PlanSynthesizerAgent.build_prompt`` so the
    synthesizer's system prompt includes the AGREED / DISAGREED /
    DEFERRED stances the user recorded on the prior round (or the
    free-text guidance from /api/advisor/check-in). Without this thread,
    the user's resolved positions never reach the model and the FM
    re-rejects on objections the user has already accepted — the bug
    that produced 3 consecutive rejections of structurally similar
    drafts.
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

    # Spec C commit #6 — prepend the per-source reliability banner so
    # the synth can weight input signals per §6.1. Empty string when
    # the ledger has no scored data yet (fresh-install case); also
    # empty when the helper fails (best-effort; synthesis never breaks
    # because of a missing reliability surface).
    reliability_preamble = _build_source_reliability_preamble(
        session, user_id
    )
    weighted_analyst_reports_text = (
        reliability_preamble + analyst_reports_text
    )

    # Feed the deterministic headline numbers INTO the synth prompt so it
    # consumes them rather than authoring its own (the #1 reject). The
    # phase-1 analyst reports are already persisted under this run's
    # decision_id by now, so the resolver can derive the manifest. Best-
    # effort: a resolver failure leaves the block empty and the synth falls
    # back to its DERIVATION-OWNERSHIP prose rule + the post-synth scrub gate.
    resolved_numbers_block = ""
    try:
        from argosy.services.plan_numeric_resolver import (
            render_numbers_for_synth,
            resolve_plan_numbers,
        )

        _drun_int = _decision_run_int(decision_run_id)
        if _drun_int is not None:
            _resolved = resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=_drun_int,
                include_canonical_ages=True,
            )
            resolved_numbers_block = render_numbers_for_synth(_resolved)
    except Exception as exc:  # noqa: BLE001 — synth must not break on this
        log.warning(
            "plan_synthesis.resolved_numbers_block_failed",
            user_id=user_id, error=str(exc),
        )

    result = agent.run_sync(
        baseline_distillate_md=baseline_md,
        prior_current_md=prior_md,
        analyst_reports_text=weighted_analyst_reports_text,
        debate_outcomes_text=debate_outcomes_text,
        portfolio_snapshot_summary=portfolio_summary,
        recent_fills_summary=fills_summary,
        speculation_cap_pct=speculation_cap_pct,
        speculation_cap_concurrent=speculation_cap_concurrent,
        prior_items_index=prior_items_index,
        user_directive=guidance,
        resolved_numbers_block=resolved_numbers_block,
        decision_id=decision_run_id,
    )
    # W1.C-v2: single-agent phase still uses the uniform bulk-persist
    # pattern (one-element list) so every synthesis phase writes to
    # ``agent_reports`` via the same code path. Routed through the
    # package namespace so a test patching ``flow._persist_agent_reports``
    # is honoured. Stub agents return SimpleNamespace; the isinstance
    # guard in _persist_agent_reports filters those out.
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    collected: list[AgentReport] = (
        [result] if isinstance(result, AgentReport) else []
    )
    if collected:
        _pkg._persist_agent_reports(session, collected)
    # T0.1 — return the collected report so the orchestrator can persist
    # it + thread the id into the recorder.
    return result.output, collected  # type: ignore[attr-defined]


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


class RewriterInvariantError(RuntimeError):
    """Raised when ``PlanLanguageRewriter`` violated a §5.2 invariant.

    Aborts the synthesis cycle per Phase 2 of
    docs/plans/argosy-comprehensive-plan-integration.md. The
    ``violations`` attribute carries the structured list the gate
    surfaced; the orchestrator persists it onto the decision-run log
    so the audit pane can show what failed.
    """

    def __init__(self, violations: list[Any]) -> None:
        super().__init__(
            f"PlanLanguageRewriter produced {len(violations)} invariant "
            f"violation(s); see violations attribute for detail."
        )
        self.violations = violations


_REWRITER_FRESHNESS = {"long": "annual", "medium": "quarterly", "short": "monthly"}


def _rewriter_stub_horizon(name: str):
    """A minimal valid HorizonSection used to pad the slices the rewriter
    isn't rewriting on a given call. Carries no prose, so the model has
    nothing to translate for it; it exists only to satisfy
    PlanSynthesisOutput's required long/medium/short fields."""
    from argosy.agents.plan_synthesizer_types import HorizonSection

    return HorizonSection(
        horizon=name,  # type: ignore[arg-type]
        freshness_expected=_REWRITER_FRESHNESS[name],  # type: ignore[arg-type]
        status="no_change",
        posture="",
    )


def _rewrite_output_parallel(
    *,
    output: PlanSynthesisOutput,
    user_id: str,
    decision_id: int | None,
) -> PlanSynthesisOutput:
    """Rewrite the four prose-bearing slices concurrently and merge.

    The four slices — the ``long`` / ``medium`` / ``short`` horizons and
    the flat ``sections`` list — carry independent prose (the rewriter is
    a per-field jargon→plain-English translator; it never cross-references
    horizons). So each can be rewritten in its own smaller call. For each
    horizon slice we send a PlanSynthesisOutput carrying the REAL horizon
    plus empty stub horizons and ``sections=[]``; for the sections slice
    we send stub horizons plus the real ``sections``. We then take each
    rewritten piece back out and merge onto the original ``output`` (which
    keeps ``inputs`` and every structured field; the caller's
    force-preserve + invariant validator still run on the merged result).

    Any slice that raises propagates (fail-loud) — a partial rewrite must
    not be published, matching the single-call behaviour it replaces.
    """
    from argosy.agents.plan_language_rewriter import PlanLanguageRewriter

    rewriter = PlanLanguageRewriter(user_id=user_id)

    def _horizon_slice(hz: str) -> PlanSynthesisOutput:
        fields = {
            "long": _rewriter_stub_horizon("long"),
            "medium": _rewriter_stub_horizon("medium"),
            "short": _rewriter_stub_horizon("short"),
            "inputs": output.inputs,
            "sections": [],
            hz: getattr(output, hz),
        }
        return PlanSynthesisOutput(**fields)

    def _sections_slice() -> PlanSynthesisOutput:
        return PlanSynthesisOutput(
            long=_rewriter_stub_horizon("long"),
            medium=_rewriter_stub_horizon("medium"),
            short=_rewriter_stub_horizon("short"),
            inputs=output.inputs,
            sections=list(output.sections),
        )

    slices: dict[str, PlanSynthesisOutput] = {
        "long": _horizon_slice("long"),
        "medium": _horizon_slice("medium"),
        "short": _horizon_slice("short"),
        "sections": _sections_slice(),
    }

    results: dict[str, PlanSynthesisOutput] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            ex.submit(rewriter.run_sync, synth_output=mini, decision_id=decision_id): key
            for key, mini in slices.items()
        }
        for fut in as_completed(futures):
            key = futures[fut]
            results[key] = fut.result().output  # re-raises on slice failure

    merged = output.model_copy(update={
        "long": results["long"].long,
        "medium": results["medium"].medium,
        "short": results["short"].short,
        "sections": results["sections"].sections,
    })
    log.info(
        "plan_synthesis.rewriter_parallel_merged",
        user_id=user_id,
        decision_run_id=decision_id,
        slices=sorted(slices.keys()),
    )
    return merged


def _run_plan_language_rewriter(
    *,
    output: PlanSynthesisOutput,
    user_id: str,
    decision_run_id: int | None = None,
) -> PlanSynthesisOutput:
    """Run the Phase 2 prose rewriter and enforce its invariants.

    Returns the rewritten ``PlanSynthesisOutput`` on success. Raises
    ``RewriterInvariantError`` when:
      - the validator finds drift in the rewritten output, OR
      - the rewriter system itself fails (SDK error, timeout, etc.).

    Fail-loud on crash. The earlier draft of this wrapper fell back
    silently to the un-rewritten ``output`` on exception, but that is
    not safe: the un-rewritten output is precisely what carries
    ``TaxAnalyst`` / ``PlanCritique RED`` / ``substrate-gated`` prose
    (the rewriter exists to scrub it). Publishing the fallback would
    leak jargon to the user-facing horizon MD. We abort instead and
    let the caller's retry / banner machinery handle the failure
    visibly. The Phase-0 publication gate is the last-line check at
    the persist boundary; the rewriter is the load-bearing scrub
    upstream.
    """
    # Late import keeps the agent module out of the eager import graph
    # (PlanLanguageRewriter pulls the SDK, which the orchestrator
    # doesn't otherwise need to load until synthesis runs).
    from argosy.quality.rewriter_invariants import validate_rewriter_invariants
    from argosy.quality.gate_types import GateCheck, GateViolation

    try:
        # Rewrite the four prose-bearing slices (the three horizons +
        # the sections list) in PARALLEL rather than one giant call.
        # A single full-output rewrite re-emits ~40-50k tokens and
        # routinely hits the 600s SDK timeout (live: supervised re-synth
        # of drun 73 — attempt 1 timed out at 10 min, attempt 2 took ~9).
        # Each slice is ~1/4 the size, so the calls finish well inside
        # the timeout and the wall-clock is the slowest single slice.
        rewritten = _rewrite_output_parallel(
            output=output, user_id=user_id, decision_id=decision_run_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "plan_synthesis.rewriter_crashed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise RewriterInvariantError(violations=[
            GateViolation(
                check=GateCheck.JARGON_LEAK,
                detail=(
                    f"PlanLanguageRewriter raised "
                    f"{type(exc).__name__}: {exc}. Aborting rather "
                    "than publishing un-rewritten output (would leak "
                    "jargon to user-facing horizon MD)."
                ),
                locator="plan_language_rewriter.run_sync",
            )
        ]) from exc

    # Defense-in-depth: force-preserve the structured subtrees the
    # rewriter is supposed to leave alone but practically does touch
    # under live-LLM conditions (observed in supervised synth #67,
    # #69: rewriter mutated Target.source_section and
    # SectionEvidence subtree despite explicit prompt prohibition).
    # Restoring from `output` before validation guarantees the
    # validator never sees structural drift from the rewriter — only
    # genuine prose drift remains, which is the design intent.
    rewritten = _force_preserve_structured_fields(
        before=output, after=rewritten
    )

    violations = validate_rewriter_invariants(before=output, after=rewritten)
    if violations:
        # Split: structural drift (count change, preserved-field
        # mutation, evidence subtree mutation, inputs mutation) =
        # data corruption → must abort. Prose drift (residual
        # history/jargon in rewritten rationale / label / etc.) =
        # quality issue → log + use the (mostly-scrubbed) rewritten
        # output, let the Phase 0 publication gate at /accept catch
        # any residual the rewriter missed.
        #
        # Detection: structural-drift detail strings start with
        # "rewriter changed" / "rewriter modified" or describe
        # "subtree modified" / "count changed" / "(preserved field)".
        # Prose-drift detail comes straight from check_history_leak /
        # check_jargon_leak ("matched `...`").
        structural = [
            v for v in violations
            if (
                "rewriter changed" in v.detail
                or "rewriter modified" in v.detail
                or "subtree modified" in v.detail
                or "preserved field" in v.detail
                or "(provenance)" in v.detail
            )
        ]
        prose = [v for v in violations if v not in structural]
        if structural:
            log.error(
                "plan_synthesis.rewriter_structural_violations",
                user_id=user_id,
                decision_run_id=decision_run_id,
                structural_count=len(structural),
                prose_count=len(prose),
                first=structural[0].detail,
                first_locator=structural[0].locator,
            )
            raise RewriterInvariantError(violations=structural)
        # Prose-only violations — log and continue. The /accept gate
        # downstream catches anything that survives.
        log.warning(
            "plan_synthesis.rewriter_prose_violations",
            user_id=user_id,
            decision_run_id=decision_run_id,
            count=len(prose),
            first=prose[0].detail,
            first_locator=prose[0].locator,
            note=(
                "Rewriter scrubbed most jargon but left residual prose "
                "leaks. Synth proceeds; /accept gate will catch any "
                "horizon-MD-level violations."
            ),
        )
    return rewritten


def _force_preserve_structured_fields(
    *,
    before: PlanSynthesisOutput,
    after: PlanSynthesisOutput,
) -> PlanSynthesisOutput:
    """Force-restore preserved subtrees from `before` onto `after`.

    The rewriter prompt says "preserve evidence / inputs / deltas /
    speculative_candidates / source_section bit-for-bit". Under live
    LLM the model regularly violates these despite explicit prompts
    (observed: SectionEvidence.facts[N].text being translated;
    Target.source_section being mutated). Rather than aborting the
    synth cycle on what is fundamentally a model-discipline issue,
    we overwrite the rewriter's output with the original values for
    these specific fields.

    The validator still runs AFTER this restoration so prose drift
    in legitimately rewritable fields (labels, rationales, posture,
    body_md) still gets caught. We only restore the fields whose
    bit-preservation is part of the contract.
    """
    # Top-level: inputs (provenance) and the sections list (per-
    # section evidence + section_id + horizon).
    updates: dict = {"inputs": before.inputs}
    if before.sections:
        # Index by (section_id, horizon) for stable matching even if
        # the rewriter reordered sections.
        before_by_key = {
            (s.section_id, s.horizon): s for s in before.sections
        }
        restored_sections = []
        for s_after in after.sections:
            key = (s_after.section_id, s_after.horizon)
            s_before = before_by_key.get(key)
            if s_before is None:
                # New section invented by rewriter — keep as-is; the
                # validator will fail on the unexpected section_id.
                restored_sections.append(s_after)
                continue
            # Preserve evidence subtree bit-for-bit; rewrite title +
            # body_md from `after` (those are prose-rewritable).
            restored_sections.append(
                s_after.model_copy(update={"evidence": s_before.evidence})
            )
        updates["sections"] = restored_sections

    # Per-horizon: deltas_from_prior, speculative_candidates,
    # Target.source_section (the structured pointer; prose label is
    # still rewritable).
    for horizon in ("long", "medium", "short"):
        b = getattr(before, horizon)
        a = getattr(after, horizon)
        # Restore deltas_from_prior and speculative_candidates
        # subtrees (whole-list bit-equality).
        horizon_updates: dict = {
            "deltas_from_prior": b.deltas_from_prior,
            "speculative_candidates": b.speculative_candidates,
        }
        # Restore each Target.source_section by positional match
        # (count is preserved-or-fail-loud elsewhere).
        if len(b.targets) == len(a.targets):
            restored_targets = []
            for bt, at in zip(b.targets, a.targets):
                if bt.source_section != at.source_section:
                    restored_targets.append(
                        at.model_copy(update={"source_section": bt.source_section})
                    )
                else:
                    restored_targets.append(at)
            horizon_updates["targets"] = restored_targets
        updates[horizon] = a.model_copy(update=horizon_updates)

    return after.model_copy(update=updates)


def _run_phase_4_risk(*, session, user_id, draft_output: PlanSynthesisOutput,
                      analyst_reports_text: str, decision_run_id: str,
                      guidance: str = "",
                      ) -> tuple[str, list[AgentReport]]:
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
                      decision_run_id=decision_run_id,
                      guidance=guidance): stance
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
            user_directive=guidance,
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

    # T0.1 — surface the collected reports so the orchestrator can
    # thread their ids into the recorder.
    return "\n\n".join(parts), collected


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
                              decision_run_id: str,
                              guidance: str = "",
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

    # H7: feed the risk officer REAL user constraints + configured risk
    # caps instead of empty values. Lazy import via the package namespace
    # so a test monkeypatching ``flow.resolve_risk_inputs`` is honoured
    # and to avoid any module-load-time circular import. Best-effort: the
    # helper returns ("", {}) on any failure so the run never breaks.
    from argosy.orchestrator.flows import plan_synthesis as _pkg
    user_constraints, risk_caps = _pkg.resolve_risk_inputs(user_id)

    result = officer.run_sync(
        proposal=proposal,
        analyst_reports=analyst_reports_payload,
        user_constraints=user_constraints,
        risk_caps=risk_caps,
        prior_rounds=[],
        round_index=1,
        n_max=1,
        user_directive=guidance,
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
                              risk_verdict: str, decision_run_id: str,
                              guidance: str = "",
                              codex_second_opinion=None,
                              current_plan_version_id: int | None = None,
                              ) -> tuple[bool, list[AgentReport]]:
    """Final integrity check.

    Validates:
      - distillate hard-constraints honored
      - three horizons cohere
      - every target has rationale + cited source
      - 'no_change' justified by evidence if claimed

    ``guidance``: free-text user directive carried forward from
    ``run_synthesis``. When non-empty, it is forwarded as
    ``user_directive`` to ``FundManagerAgent.build_prompt`` so the FM
    sees the user's per-objection stances from the prior round and
    stops re-raising objections the user has already AGREED / DISAGREED
    with. Without this thread, the FM re-rejects on identical concerns
    round after round — the bug that produced 3 consecutive rejections
    of structurally similar drafts.

    ``codex_second_opinion``: optional ``CodexSecondOpinion`` from the
    Argosy ZigZag Phase 4.5 reviewer. When present, the FM is shown
    codex's findings + overall_assessment in its user prompt (verbatim
    JSON dump) so the FM can cite codex's reasoning in its verdict.
    None when codex was skipped (env var / pytest / kit unavailable /
    dispatch failure) — the FM's prompt is byte-identical to the
    pre-feature behavior in that case.

    Returns True to green-light the draft, False to reject.
    """
    from argosy.orchestrator.flows import plan_synthesis as _pkg

    # Wave 7 Piece B — pull the user's prior-draft resolutions so the
    # FM cannot silently re-raise concerns Ariel already answered.
    # Best-effort: if the lookup throws (test session with partial
    # schema, fetcher import failure on a stub run), we degrade to
    # the no-carry-forward path rather than blocking phase 5.
    prior_resolved_list: list = []
    try:
        from argosy.services.prior_resolved_concerns import (
            get_prior_resolved_concerns,
        )

        prior_resolved_list = get_prior_resolved_concerns(
            session,
            user_id=user_id,
            current_plan_version_id=current_plan_version_id,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.phase_5.prior_resolved_failed",
            user_id=user_id,
            decision_run_id=decision_run_id,
            error=str(exc),
        )

    log.info("plan_synthesis.phase_5.start",
             user_id=user_id, decision_run_id=decision_run_id,
             has_codex_opinion=codex_second_opinion is not None,
             prior_resolved_count=len(prior_resolved_list))
    fm = _pkg._make_fund_manager(user_id=user_id)
    fm_kwargs: dict = dict(
        decision_kind="plan_revision",
        draft_plan=draft_output.model_dump_json(),
        risk_verdict=risk_verdict,
        user_directive=guidance,
        decision_id=decision_run_id,
    )
    # Only pass codex_second_opinion when present so a test stub FM
    # whose run_sync doesn't accept the kwarg keeps working unchanged.
    if codex_second_opinion is not None:
        fm_kwargs["codex_second_opinion"] = codex_second_opinion
    # Same gating for prior_resolved_concerns — only thread it when
    # non-empty so legacy stubs without the kwarg keep working.
    if prior_resolved_list:
        fm_kwargs["prior_resolved_concerns"] = prior_resolved_list
    # Orchestrator-level transient-flake retry for the FM — same envelope
    # as bear_researcher. The FM is the LAST step of a ~25-min pipeline; a
    # bare claude.exe exit-1 flake here (observed: drun 78) would otherwise
    # throw away the entire run. _is_bear_transient_flake only matches the
    # empty-stderr exit-1 fingerprint, so deterministic FM failures still
    # surface immediately. (The SDK has its own inner retry; this restarts
    # the whole call fresh, which recovers flakes the SDK layer doesn't.)
    result = None
    for _fm_attempt in range(_BEAR_RESEARCHER_MAX_ATTEMPTS):
        try:
            result = fm.run_sync(**fm_kwargs)
            break
        except AgentRunError as exc:
            is_final = _fm_attempt + 1 >= _BEAR_RESEARCHER_MAX_ATTEMPTS
            if not _is_bear_transient_flake(exc) or is_final:
                log.error(
                    "plan_synthesis.fund_manager.retry_exhausted",
                    decision_run_id=decision_run_id,
                    attempt=_fm_attempt + 1,
                    max_attempts=_BEAR_RESEARCHER_MAX_ATTEMPTS,
                    is_transient_flake=_is_bear_transient_flake(exc),
                    error=str(exc)[:500],
                )
                raise
            delay = _BEAR_RESEARCHER_RETRY_BACKOFF_SECONDS[_fm_attempt]
            log.warning(
                "plan_synthesis.fund_manager.orchestrator_retry",
                decision_run_id=decision_run_id,
                attempt=_fm_attempt + 1,
                max_attempts=_BEAR_RESEARCHER_MAX_ATTEMPTS,
                delay_seconds=delay,
                error=str(exc)[:500],
            )
            time.sleep(delay)
    # W1.C-v2: uniform bulk-persist pattern. Phase 5 calls exactly one
    # agent; wrap its dataclass in a 1-element list and route through
    # the package namespace. Stub agents return SimpleNamespace; only
    # real AgentReport instances are persisted.
    collected: list[AgentReport] = (
        [result] if isinstance(result, AgentReport) else []
    )
    if collected:
        _pkg._persist_agent_reports(session, collected)
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
    # T0.1 — surface the collected report so the orchestrator can
    # thread its id into the recorder.
    return approved, collected
