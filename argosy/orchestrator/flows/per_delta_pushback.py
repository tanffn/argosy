"""Per-delta slim re-debate flow (T4.3).

When the user clicks "Push back" on a single delta in ``/plan``, we
don't want to fire another full ~30-minute / ~$3-4 synthesis. Instead
we run a SLIM debate scoped to exactly that one delta + the user's
free-form feedback:

  * One ``BullResearcherAgent`` run — "argue this delta should stay"
  * One ``BearResearcherAgent`` run — "argue this delta should be
    modified or dropped, in light of the user's pushback"
  * One ``ResearcherFacilitatorAgent`` run — "issue a final verdict
    (KEEP / MODIFY / DROP) with an optional revised_value"

Total ≈ 3 LLM calls, ~$0.30-$1.00 per run; well under the
``ARGOSY_SYNTHESIS_COST_CAP_USD`` soft cap (defaults to $10).

Critical guarantees:
  * **Not full synthesis.** We do NOT run analysts, risk officers, or
    the Fund Manager. The slim verdict is advisory; the user still
    accepts/rejects on the /plan page.
  * **Pinned to one horizon.** The horizon is derived from the
    ``item_id`` prefix (``"short.targets.foo"`` -> ``"short"``); we do
    not fan out across all three horizons.
  * **Cost-cap aware.** Before dispatching agents we check
    ``$ARGOSY_SYNTHESIS_COST_CAP_USD`` against the per-run JSONL trail
    (same mechanism used by ``plan_synthesis``). If projected spend
    would exceed the cap we refuse with ``CostCapExceededError``.
  * **Idempotent.** Two clicks on the same ``(user_id, draft_id,
    item_id)`` within ``IDEMPOTENCY_WINDOW_SECONDS`` (default 30s)
    return the same ``decision_run_id`` instead of starting a second
    run. The in-flight registry is process-local; a server restart
    breaks idempotency (acceptable trade-off — synthesis is a single-
    user, single-instance system).

The flow writes a ``decision_runs`` row with
``decision_kind="delta_pushback"`` and ``notes_json`` carrying:

    {
      "delta_item_id": str,
      "original_value": <whatever the proposed.value was>,
      "user_feedback": str,
      "revised_value": ... (populated after the facilitator verdict),
      "verdict": "KEEP" | "MODIFY" | "DROP"
    }

The UI's ``kindLabel()`` (T4.4) reads ``delta_item_id`` and renders
``"pushback · <item_id>"`` in ``/decisions``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from argosy.agents.base import AgentReport
from argosy.logging import get_logger
from argosy.state.models import DecisionRun, PlanVersion

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Public types
# ----------------------------------------------------------------------


Verdict = Literal["KEEP", "MODIFY", "DROP"]


@dataclass
class DeltaPushbackOutcome:
    """Structured outcome of a single slim re-debate."""

    verdict: Verdict
    revised_value: Any = None
    rationale_md: str = ""
    cited_sources: list[str] = field(default_factory=list)


class DeltaPushbackError(Exception):
    """Base error for the slim-debate flow."""


class DeltaNotFoundError(DeltaPushbackError):
    """The requested ``item_id`` doesn't exist in the current draft."""


class CostCapExceededError(DeltaPushbackError):
    """Cumulative cost would cross ``$ARGOSY_SYNTHESIS_COST_CAP_USD``."""


# ----------------------------------------------------------------------
# Idempotency: in-flight registry
# ----------------------------------------------------------------------
#
# Maps ``(user_id, draft_id, item_id) -> (decision_run_id, started_at_epoch)``.
# Within ``IDEMPOTENCY_WINDOW_SECONDS`` of the same key being kicked off,
# subsequent calls return the in-flight decision_run_id rather than
# starting a second run. The window only matters while the flow is still
# running (or shortly after); once the cap elapses, a new pushback fires
# a new flow. The registry is process-local: we don't try to coordinate
# across replicas because Argosy is a single-user system.


IDEMPOTENCY_WINDOW_SECONDS = 30.0

# Estimated lower-bound cost per slim run. Used to decide whether to
# refuse the dispatch when remaining headroom under the cost cap is
# tight. Conservative: a real bull/bear/facilitator triad typically lands
# ~$0.30 on Opus, but we round up to $0.50 so a near-cap user gets a
# clean refusal rather than a $9.98 -> $11.00 surprise.
ESTIMATED_RUN_COST_USD = 0.50

_in_flight_lock = threading.Lock()
_in_flight: dict[tuple[str, int, str], tuple[int, float]] = {}


def _claim_inflight_or_get(
    *, user_id: str, draft_id: int, item_id: str, decision_run_id: int
) -> int | None:
    """Try to claim the in-flight slot for this triple.

    Returns ``None`` on a fresh claim (caller should proceed with the
    flow). Returns the existing ``decision_run_id`` when a recent run
    is still in-flight for the same triple (caller short-circuits).
    """
    key = (user_id, draft_id, item_id)
    now = time.monotonic()
    with _in_flight_lock:
        existing = _in_flight.get(key)
        if existing is not None:
            run_id, started = existing
            if (now - started) <= IDEMPOTENCY_WINDOW_SECONDS:
                return run_id
            # Window elapsed — overwrite so the new caller wins.
        _in_flight[key] = (decision_run_id, now)
        return None


def _release_inflight(*, user_id: str, draft_id: int, item_id: str) -> None:
    """Drop the registry entry when the flow finishes (success or fail)."""
    key = (user_id, draft_id, item_id)
    with _in_flight_lock:
        _in_flight.pop(key, None)


def _peek_inflight(
    *, user_id: str, draft_id: int, item_id: str
) -> int | None:
    """Read-only peek used by the API route to short-circuit a double click.

    Returns the existing decision_run_id when a fresh run is still in
    flight, else None. Does NOT mutate the registry.
    """
    key = (user_id, draft_id, item_id)
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


def _horizon_from_item_id(item_id: str) -> str:
    """Derive the horizon (long/medium/short) from the item_id prefix.

    Item IDs follow the convention ``"<horizon>.<kind>.<slug>"`` so we
    can split on the first dot. Falls back to ``"medium"`` (the
    strategic centerpiece) if the prefix isn't recognised — better to
    have the slim debate run against the most-likely-relevant horizon
    than abort the whole flow on a slightly-malformed id.
    """
    if not item_id:
        return "medium"
    prefix = item_id.split(".", 1)[0].lower()
    if prefix in ("long", "medium", "short"):
        return prefix
    return "medium"


def _find_delta(
    pv: PlanVersion, item_id: str
) -> tuple[str, dict, dict] | None:
    """Locate ``item_id`` inside one of the draft's three horizon JSONs.

    Returns ``(horizon, full_payload_dict, delta_dict)`` on match, else
    ``None``. Mirrors ``_find_delta_horizon_field`` in routes/plan.py
    but keyed by the human ``horizon`` string rather than the column
    name so the flow can stamp the right value into notes_json.
    """
    for horizon, field_name in (
        ("long", "horizon_long_json"),
        ("medium", "horizon_medium_json"),
        ("short", "horizon_short_json"),
    ):
        raw = getattr(pv, field_name, None)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for d in payload.get("deltas_from_prior") or []:
            if isinstance(d, dict) and d.get("item_id") == item_id:
                return horizon, payload, d
    return None


def _read_pushback_trail_cost(decision_audit_token: str) -> float:
    """Sum cost_usd from the slim flow's JSONL trail.

    Mirrors ``plan_synthesis.orchestrator._read_synthesis_trail_costs``
    so the same observability lens (per-run trail file on disk) works
    for the slim debate. Trail lives under
    ``${ARGOSY_HOME}/logs/synthesis/<token>.jsonl`` so the existing
    ingestion / replay tooling picks it up without modification.
    """
    from argosy.config import get_settings

    settings = get_settings()
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


def _total_recent_cost_usd(session: Session, *, user_id: str) -> float:
    """Sum cost_usd across recent agent_reports for the user.

    Used as the "current spend" reference when deciding whether the
    slim flow has enough headroom under the cap. Bounded to a 24-hour
    look-back so the cap rolls forward with time and doesn't permanently
    block a user once they cross it once. Best-effort — any query
    failure returns 0.0 so the flow doesn't fail closed on observability
    plumbing.
    """
    try:
        from datetime import timedelta

        from sqlalchemy import func, select
        from argosy.state.models import AgentReport as AgentReportORM

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        spent = session.execute(
            select(func.coalesce(func.sum(AgentReportORM.cost_usd), 0)).where(
                AgentReportORM.user_id == user_id,
                AgentReportORM.created_at >= cutoff,
            )
        ).scalar_one()
        return float(spent or 0.0)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "per_delta_pushback.cost_lookup_failed",
            user_id=user_id, error=str(exc),
        )
        return 0.0


def _persist_agent_reports_jsonl(
    *, decision_audit_token: str, reports: list[AgentReport]
) -> None:
    """Append each AgentReport to the per-run JSONL trail.

    Same shape as ``plan_synthesis._persist_agent_reports`` — keeping
    them parallel means the existing trail-ingest tooling reads slim
    runs without per-kind handling.
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
            "per_delta_pushback.trail_dir_mkdir_failed", error=str(exc),
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
            "per_delta_pushback.trail_write_failed",
            count=len(reports), error=str(exc),
        )


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------


def _render_delta_context(*, horizon: str, delta: dict) -> str:
    """Render the delta + its prior+proposed values as a compact block.

    Used as a synthetic "analyst report" we pass to the bull/bear so
    each side sees exactly the disputed item, no more no less. The
    full draft has additional context the user might invoke ("why
    didn't you change X too?") but for cost reasons the slim flow
    stays narrowly scoped to ONE delta.
    """
    proposed = delta.get("proposed") or {}
    prior = delta.get("prior") or {}
    label = (
        (proposed.get("label") if isinstance(proposed, dict) else None)
        or delta.get("summary")
        or "(unlabeled delta)"
    )
    summary = delta.get("summary") or ""
    rationale = delta.get("rationale") or ""
    change_kind = delta.get("change_kind") or "(unknown)"
    cited = delta.get("cited_sources") or []

    return (
        f"Horizon: {horizon}\n"
        f"Change kind: {change_kind}\n"
        f"Item id: {delta.get('item_id')}\n"
        f"Label: {label}\n"
        f"Summary: {summary}\n"
        f"Prior value: {json.dumps(prior, default=str)}\n"
        f"Proposed value: {json.dumps(proposed, default=str)}\n"
        f"Rationale (from synthesizer): {rationale}\n"
        f"Cited sources from synthesizer: {cited}\n"
    )


def _build_horizon_question(
    *, horizon: str, delta_text: str, user_feedback: str
) -> str:
    """The seed question the bull/bear debate is scoped to.

    Embedded into the synthetic ``analyst_reports`` payload so the
    existing researcher prompts (which iterate ``analyst_reports``)
    surface it without API changes. The facilitator gets the same
    question via the same payload route.
    """
    return (
        "RE-EVALUATE A SINGLE PLAN DELTA (slim re-debate, NOT full synthesis).\n\n"
        f"The user has pushed back on the following {horizon}-horizon delta from "
        "the latest draft. Re-evaluate whether the delta should STAY as-is "
        "(KEEP), be REVISED (MODIFY, with a revised value), or be DROPPED.\n\n"
        "=== THE DISPUTED DELTA ===\n"
        f"{delta_text}\n"
        "=== THE USER'S PUSHBACK ===\n"
        f"{user_feedback}\n\n"
        "Constraints:\n"
        "  - Treat the user's pushback as authoritative context but cite "
        "evidence for your case from the delta's own rationale + cited "
        "sources where possible.\n"
        "  - This is ONE delta only — do not propose changes to other items.\n"
        "  - Stay concrete: if MODIFY, name the new value (e.g. percentage, "
        "ticker, dollar amount) rather than 'revisit later'."
    )


# ----------------------------------------------------------------------
# Slim flow
# ----------------------------------------------------------------------


def _run_slim_redebate(
    *,
    user_id: str,
    horizon: str,
    delta: dict,
    user_feedback: str,
    decision_audit_token: str,
) -> tuple[DeltaPushbackOutcome, list[AgentReport]]:
    """Run the bull/bear/facilitator triad against one delta.

    Returns (outcome, collected_agent_reports). The collected reports
    are persisted by the caller via the JSONL trail (matching the
    synthesis pattern), AND via the negotiation recorder so the
    /decisions UI can drill into them.
    """
    from argosy.agents.researcher import (
        BearResearcherAgent,
        BullResearcherAgent,
    )
    from argosy.agents.researcher_facilitator import (
        ResearcherFacilitatorAgent,
    )

    delta_text = _render_delta_context(horizon=horizon, delta=delta)
    horizon_question = _build_horizon_question(
        horizon=horizon, delta_text=delta_text, user_feedback=user_feedback,
    )

    # Single synthetic "analyst report" carrying the disputed delta +
    # the user's pushback + the horizon-specific framing. Both
    # researchers iterate ``analyst_reports`` so the same payload
    # reaches them via the existing build_prompt signature with no API
    # change.
    analyst_payload = [{
        "agent_role": "delta_pushback_seed",
        "horizon": horizon,
        "horizon_question": horizon_question,
        "report_text": (
            f"User pushback (verbatim): {user_feedback}\n\n"
            f"Delta under dispute:\n{delta_text}"
        ),
    }]
    ticker = f"delta-{delta.get('item_id') or 'unknown'}"

    bull = BullResearcherAgent(user_id=user_id)
    bear = BearResearcherAgent(user_id=user_id)
    fac = ResearcherFacilitatorAgent(user_id=user_id)

    collected: list[AgentReport] = []

    bull_report = bull.run_sync(
        analyst_reports=analyst_payload,
        prior_rounds=[],
        round_index=1,
        n_max=1,
        ticker=ticker,
        decision_id=decision_audit_token,
    )
    if isinstance(bull_report, AgentReport):
        collected.append(bull_report)
    bull_turn = getattr(bull_report, "output", None)
    bull_turn_dict = (
        bull_turn.model_dump() if bull_turn is not None and hasattr(bull_turn, "model_dump") else {}
    )

    bear_report = bear.run_sync(
        analyst_reports=analyst_payload,
        prior_rounds=[bull_turn_dict] if bull_turn_dict else [],
        round_index=1,
        n_max=1,
        ticker=ticker,
        decision_id=decision_audit_token,
    )
    if isinstance(bear_report, AgentReport):
        collected.append(bear_report)
    bear_turn = getattr(bear_report, "output", None)
    bear_turn_dict = (
        bear_turn.model_dump() if bear_turn is not None and hasattr(bear_turn, "model_dump") else {}
    )

    fac_report = fac.run_sync(
        bull_turns=[bull_turn_dict] if bull_turn_dict else [],
        bear_turns=[bear_turn_dict] if bear_turn_dict else [],
        rounds_run=1,
        ticker=ticker,
        decision_id=decision_audit_token,
    )
    if isinstance(fac_report, AgentReport):
        collected.append(fac_report)

    fac_out = getattr(fac_report, "output", fac_report)

    # The facilitator agent produces a ``DebateOutcome`` with
    # ``winning_side``, ``synthesis``, ``cited_evidence``. We translate
    # to the T4.3 DeltaPushbackOutcome shape:
    #   bull wins  -> KEEP   (no revised_value)
    #   bear wins  -> MODIFY (revised value parsed from synthesis if present)
    #   split      -> MODIFY (signal to user that opinion is genuinely split)
    # The trade is conservative: only DROP when the bear's synthesis
    # explicitly says "drop" / "remove" / "abandon". Otherwise MODIFY so
    # the user sees a revised proposal rather than a binary yes/no.
    winning_side = getattr(fac_out, "winning_side", "split") or "split"
    synthesis_md = getattr(fac_out, "synthesis", "") or ""
    cited = list(getattr(fac_out, "cited_sources", []) or [])
    # cited_evidence is a list[str] alongside cited_sources. Surface both
    # to the user so the verdict isn't a bare claim.
    cited_evidence = list(getattr(fac_out, "cited_evidence", []) or [])
    if cited_evidence:
        synthesis_md = (
            synthesis_md
            + "\n\n**Evidence cited:**\n"
            + "\n".join(f"- {e}" for e in cited_evidence)
        )

    synthesis_lower = synthesis_md.lower()
    if winning_side == "bull":
        verdict: Verdict = "KEEP"
        revised_value: Any = None
    elif winning_side == "bear":
        if any(w in synthesis_lower for w in (" drop", "abandon", "remove ", "delete")):
            verdict = "DROP"
            revised_value = None
        else:
            verdict = "MODIFY"
            # The facilitator returns prose, not a structured value.
            # We surface the synthesis itself as the "revised value" so
            # the UI has something concrete to render; downstream the
            # user can manually transcribe a number / ticker into the
            # delta via the existing PATCH endpoint.
            revised_value = {
                "kind": "narrative",
                "text": synthesis_md.split("\n\n")[0][:400],
            }
    else:  # "split"
        verdict = "MODIFY"
        revised_value = {
            "kind": "narrative",
            "text": synthesis_md.split("\n\n")[0][:400],
        }

    outcome = DeltaPushbackOutcome(
        verdict=verdict,
        revised_value=revised_value,
        rationale_md=synthesis_md,
        cited_sources=cited,
    )
    return outcome, collected


# ----------------------------------------------------------------------
# Public dispatcher
# ----------------------------------------------------------------------


@dataclass
class StartResult:
    """Return shape of ``start_per_delta_pushback``.

    ``inflight=True`` means we returned an existing in-flight run id
    (the caller hit the idempotency window). ``inflight=False`` means
    a fresh run was started.
    """

    decision_run_id: int
    inflight: bool


def start_per_delta_pushback(
    session: Session,
    *,
    user_id: str,
    draft_id: int,
    item_id: str,
    user_feedback: str,
    run_inline: bool = False,
) -> StartResult:
    """Kick off the slim re-debate for one delta.

    Steps:
      1. Validate the delta exists in the current draft (else
         ``DeltaNotFoundError``).
      2. Check the per-user 24h spend against the soft cap
         (``$ARGOSY_SYNTHESIS_COST_CAP_USD``). If headroom <
         ``ESTIMATED_RUN_COST_USD`` raise ``CostCapExceededError``.
      3. Idempotency: if a slim run is already in flight for the
         same ``(user_id, draft_id, item_id)``, return its run_id with
         ``inflight=True`` and do NOT start a second one.
      4. Open a ``decision_runs`` row with ``decision_kind="delta_pushback"``
         and stamp ``notes_json`` with ``{delta_item_id, original_value,
         user_feedback}``.
      5. Dispatch the slim flow on a background thread (unless
         ``run_inline=True`` for tests).
      6. Return ``(decision_run_id, inflight=False)``.

    The caller (the FastAPI route) keeps the existing user_edit_note
    side-effect from the legacy pushback path; that mutation is
    orthogonal to this flow.
    """
    # 1. Locate the delta.
    pv = session.get(PlanVersion, draft_id)
    if pv is None or pv.user_id != user_id or pv.role != "draft":
        raise DeltaNotFoundError("draft not found")
    found = _find_delta(pv, item_id)
    if found is None:
        raise DeltaNotFoundError(
            f"item_id {item_id!r} not found in any horizon delta list"
        )
    horizon, _payload, delta = found
    proposed = delta.get("proposed") if isinstance(delta, dict) else {}
    original_value: Any = (
        proposed if isinstance(proposed, (dict, list, str, int, float)) else None
    )

    # 2. Cost-cap check. The soft cap is the same env var synthesis
    # uses (defaults to $10). We use the LAST 24 HOURS of agent_reports
    # as the spend reference and refuse if (spent + estimated_run_cost)
    # would exceed the cap. This gives the user a hard floor of
    # ~$0.50 headroom for the slim run.
    cost_cap_usd = float(os.environ.get("ARGOSY_SYNTHESIS_COST_CAP_USD", "10.0"))
    spent_so_far = _total_recent_cost_usd(session, user_id=user_id)
    headroom = cost_cap_usd - spent_so_far
    if headroom < ESTIMATED_RUN_COST_USD:
        log.warning(
            "per_delta_pushback.cost_cap_refused",
            user_id=user_id, spent_24h=spent_so_far, cap=cost_cap_usd,
            estimated_cost=ESTIMATED_RUN_COST_USD,
        )
        raise CostCapExceededError(
            f"spent ${spent_so_far:.2f} in last 24h vs cap ${cost_cap_usd:.2f}; "
            f"estimated slim-run cost ${ESTIMATED_RUN_COST_USD:.2f} would breach. "
            "Bump ARGOSY_SYNTHESIS_COST_CAP_USD or wait for the 24h window to roll."
        )

    # 3. Idempotency peek BEFORE opening a new DecisionRun. Avoids
    # leaving an orphan row if the second click is rejected.
    existing = _peek_inflight(user_id=user_id, draft_id=draft_id, item_id=item_id)
    if existing is not None:
        log.info(
            "per_delta_pushback.idempotent_short_circuit",
            user_id=user_id, draft_id=draft_id, item_id=item_id,
            existing_run_id=existing,
        )
        return StartResult(decision_run_id=existing, inflight=True)

    # 4. Open the DecisionRun row. notes_json must carry delta_item_id
    # (per T4.4 contract) so the UI's kindLabel() can render the
    # "pushback · <item_id>" chip on /decisions.
    notes = {
        "delta_item_id": item_id,
        "original_value": original_value,
        "user_feedback": user_feedback,
        "horizon": horizon,
    }
    run = DecisionRun(
        user_id=user_id,
        ticker="(plan)",  # T4.4 sentinel for synthesis-family runs
        tier=None,
        decision_kind="delta_pushback",
        started_at=datetime.now(timezone.utc),
        status="running",
        notes_json=json.dumps(notes, default=str),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    decision_run_id = run.id

    # 4b. Claim the in-flight slot. Race-safe: if a concurrent caller
    # beat us (created their own run in the same millisecond), use
    # theirs and roll back our DecisionRun row.
    claimed_existing = _claim_inflight_or_get(
        user_id=user_id, draft_id=draft_id, item_id=item_id,
        decision_run_id=decision_run_id,
    )
    if claimed_existing is not None and claimed_existing != decision_run_id:
        # Roll back our orphan row — the other caller's row is the
        # canonical one.
        log.info(
            "per_delta_pushback.race_lost_using_existing",
            our_run_id=decision_run_id, existing_run_id=claimed_existing,
        )
        run.status = "superseded"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        return StartResult(decision_run_id=claimed_existing, inflight=True)

    # 5. Dispatch the slim flow.
    if run_inline:
        # Used by tests so the test can assert on the persisted outcome
        # without racing a background thread.
        try:
            _execute_and_finalize(
                user_id=user_id,
                draft_id=draft_id,
                item_id=item_id,
                horizon=horizon,
                delta=delta,
                user_feedback=user_feedback,
                decision_run_id=decision_run_id,
            )
        finally:
            _release_inflight(
                user_id=user_id, draft_id=draft_id, item_id=item_id,
            )
    else:
        # Background thread — the FastAPI route returns immediately and
        # the UI subscribes to WS events for completion.
        t = threading.Thread(
            target=_thread_entry,
            kwargs={
                "user_id": user_id,
                "draft_id": draft_id,
                "item_id": item_id,
                "horizon": horizon,
                "delta": delta,
                "user_feedback": user_feedback,
                "decision_run_id": decision_run_id,
            },
            name=f"per-delta-pushback-{decision_run_id}",
            daemon=True,
        )
        t.start()

    return StartResult(decision_run_id=decision_run_id, inflight=False)


def _thread_entry(**kwargs: Any) -> None:
    """Background-thread wrapper around ``_execute_and_finalize``.

    Ensures the in-flight registry is always released, even on
    exception. The exception itself is logged and swallowed — the
    DecisionRun row carries ``status="failed"`` so the UI surfaces the
    error via the same /decisions surface.
    """
    user_id = kwargs["user_id"]
    draft_id = kwargs["draft_id"]
    item_id = kwargs["item_id"]
    try:
        _execute_and_finalize(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "per_delta_pushback.background_failed",
            user_id=user_id, draft_id=draft_id, item_id=item_id,
            error=str(exc),
        )
    finally:
        _release_inflight(user_id=user_id, draft_id=draft_id, item_id=item_id)


def _execute_and_finalize(
    *,
    user_id: str,
    draft_id: int,
    item_id: str,
    horizon: str,
    delta: dict,
    user_feedback: str,
    decision_run_id: int,
) -> None:
    """End-to-end execution of one slim run.

    Wraps ``_run_slim_redebate``, persists the outcome to the
    DecisionRun row's ``notes_json`` (with revised_value + verdict),
    writes the per-phase decision_phases row via the negotiation
    recorder, and stamps the row's finished_at + status.
    """
    from argosy.api.events import publish_event_threadsafe

    decision_audit_token = f"delta-pushback-{decision_run_id}"
    started_at = datetime.now(timezone.utc)

    publish_event_threadsafe(
        "plan.delta.pushback.started",
        {
            "user_id": user_id,
            "draft_id": draft_id,
            "item_id": item_id,
            "decision_run_id": decision_run_id,
        },
    )

    # The flow itself does the agent calls. Use a fresh sync session
    # for the finalization writes so we don't clash with the original
    # request's session.
    from argosy.state import db as db_mod

    outcome: DeltaPushbackOutcome | None = None
    collected: list[AgentReport] = []
    error_text: str | None = None

    try:
        outcome, collected = _run_slim_redebate(
            user_id=user_id,
            horizon=horizon,
            delta=delta,
            user_feedback=user_feedback,
            decision_audit_token=decision_audit_token,
        )
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
        log.exception(
            "per_delta_pushback.flow_failed",
            user_id=user_id, item_id=item_id, error=error_text,
        )

    # Persist the JSONL trail (matches synthesis convention so the
    # existing replay tooling sees the rows). Best-effort.
    _persist_agent_reports_jsonl(
        decision_audit_token=decision_audit_token, reports=collected,
    )

    # Record the phase via the negotiation recorder so /decisions can
    # drill into the slim run's participants. The recorder writes
    # agent_reports rows for participants in its own sub-session.
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
                        "per_delta_pushback.persist_agent_reports_failed",
                        error=str(exc),
                    )
            phase_kind = "delta_pushback.verdict"
            verdict_dto = None
            phase_output: str | dict = (
                {
                    "verdict": outcome.verdict,
                    "rationale_md": outcome.rationale_md,
                    "revised_value": outcome.revised_value,
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
                verdict=verdict_dto,
                phase_output=phase_output,
            )

        asyncio.run(_do_recorder())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "per_delta_pushback.recorder_failed",
            user_id=user_id, item_id=item_id, error=str(exc),
        )

    # Finalize the DecisionRun row + update notes_json with the verdict.
    # We use the existing async ``db_mod.get_session()`` path because it
    # already points at the right engine in both production and the test
    # fixture (``conftest.client_with_db`` calls
    # ``db_module.init_engine(async_url)`` against the same SQLite file
    # the sync route uses). Opening a fresh sync engine on
    # ``settings.database_url`` would resolve to the production DB path
    # under the test fixture's home override.
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
                    notes["verdict"] = outcome.verdict
                    notes["revised_value"] = outcome.revised_value
                    notes["rationale_md"] = outcome.rationale_md
                    notes["cited_sources"] = outcome.cited_sources
                    status_value = "completed"
                else:
                    notes["error"] = error_text or "flow_failed"
                    status_value = "failed"
                # Update via UPDATE statement so we don't rely on the
                # ORM's stale-instance machinery (we just fetched but
                # mutating via assignment + commit can race with other
                # writers on the same row in pathological cases).
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
            "per_delta_pushback.finalize_failed",
            user_id=user_id, item_id=item_id, error=str(exc),
        )

    publish_event_threadsafe(
        "plan.delta.pushback.completed",
        {
            "user_id": user_id,
            "draft_id": draft_id,
            "item_id": item_id,
            "decision_run_id": decision_run_id,
            "verdict": outcome.verdict if outcome is not None else None,
            "error": error_text,
        },
    )
    _ = db_mod  # silence linter; module imported for fresh-session symmetry


__all__ = [
    "CostCapExceededError",
    "DeltaNotFoundError",
    "DeltaPushbackError",
    "DeltaPushbackOutcome",
    "ESTIMATED_RUN_COST_USD",
    "IDEMPOTENCY_WINDOW_SECONDS",
    "StartResult",
    "Verdict",
    "_in_flight",
    "_in_flight_lock",
    "_peek_inflight",
    "_release_inflight",
    "_run_slim_redebate",
    "start_per_delta_pushback",
]
