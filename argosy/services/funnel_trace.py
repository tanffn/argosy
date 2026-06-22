"""Decision-funnel trace recorder (P0 observability).

The autonomous funnel cannot be a black box. This module is the single writer
for the three trace tables (``funnel_runs``, ``funnel_stage_rows``,
``decision_snapshots``) so every run is fully replayable: a human can trace
each name from "considered" to "acted / dropped" with the reason and the model
that decided it, and answer "why did it (not) act on X today?" without a
re-run.

It reuses the existing infra (structured JSON logging + the same sync-session
pattern the cadence loops use) — NOT a parallel logging stack. The recorder is
deliberately small and side-effect-only; the funnel orchestrator owns the
control flow and calls these helpers.

Idempotency / immutability:
- ``open_run`` is get-or-create on a per-(user, day, trigger) idempotency key,
  so a re-fired daily tick attaches to the same run instead of duplicating it.
- ``record_snapshot`` is get-or-create on a FULL decision-input fingerprint
  (day + policy + model + prompt + portfolio + market), so a legitimate
  same-day re-decision after any input change gets its own immutable row while
  an identical re-run is deduped.
- ``record_stage_row`` is append-only (no dedup): the per-name audit is a log.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from argosy.logging import get_logger
from argosy.state.models import DecisionSnapshot, FunnelRun, FunnelStageRow

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.funnel_trace")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint(payload: Any) -> str:
    """Stable short hash of an arbitrary JSON-able payload (for dedup keys)."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def run_idempotency_key(*, user_id: str, day: str, trigger: str) -> str:
    """One run per (user, calendar day, trigger kind)."""
    return f"funnel|{user_id}|{day}|{trigger}"


def snapshot_dedup_key(
    *,
    user_id: str,
    ticker: str,
    day: str,
    policy_version: str,
    model_name: str,
    prompt_template_hash: str,
    portfolio_fp: str,
    market_fp: str,
) -> str:
    """Full decision-input fingerprint (codex BLOCKER 3).

    Two decisions collide ONLY when every replay-relevant input is identical;
    any change to the market, the portfolio, the policy, the model, or the
    prompt yields a fresh key so the new decision is recorded, not lost.
    """
    return (
        f"funnel|{user_id}|{ticker.upper()}|{day}|{policy_version}|"
        f"{model_name}|{prompt_template_hash}|{portfolio_fp}|{market_fp}"
    )


# ----------------------------------------------------------------------
# Run lifecycle
# ----------------------------------------------------------------------


def open_run(
    session: "Session",
    *,
    user_id: str,
    day: str,
    trigger: str = "scheduler",
    shadow: bool,
    policy_version: str | None,
    ips_version: str | None,
    plan_version_id: int | None,
    started_at: datetime | None = None,
) -> FunnelRun:
    """Get-or-create the funnel_run for (user, day, trigger). Returns the row
    (committed). A re-fired tick attaches to the existing run."""
    key = run_idempotency_key(user_id=user_id, day=day, trigger=trigger)
    existing = session.execute(
        select(FunnelRun).where(FunnelRun.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = FunnelRun(
        user_id=user_id,
        trigger=trigger,
        shadow=1 if shadow else 0,
        status="running",
        policy_version=policy_version,
        ips_version=ips_version,
        plan_version_id=plan_version_id,
        started_at=started_at or _utcnow(),
        idempotency_key=key,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    _log.info(
        "funnel_trace.run_opened",
        run_id=row.id, user_id=user_id, day=day, trigger=trigger,
        shadow=bool(shadow), policy_version=policy_version, ips_version=ips_version,
    )
    return row


def close_run(
    session: "Session",
    *,
    run_id: int,
    status: str,
    totals: dict[str, Any] | None = None,
    macro_read: dict[str, Any] | None = None,
    error_message: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    """Finalise a funnel_run (status ok|error|killed) with its per-stage totals
    and the Stage-0 macro read."""
    row = session.get(FunnelRun, run_id)
    if row is None:
        return
    row.status = status
    row.finished_at = finished_at or _utcnow()
    if totals is not None:
        row.totals_json = json.dumps(totals, default=str)
    if macro_read is not None:
        row.macro_read_json = json.dumps(macro_read, default=str)
    if error_message is not None:
        row.error_message = error_message[:4000]
    session.commit()
    _log.info(
        "funnel_trace.run_closed", run_id=run_id, status=status,
        totals=totals or {},
    )


# ----------------------------------------------------------------------
# Per-name stage audit (append-only)
# ----------------------------------------------------------------------


def record_stage_row(
    session: "Session",
    *,
    run_id: int,
    stage: str,
    subject: str,
    subject_type: str,
    decision: str,
    reason: str = "",
    signal_or_rule: str | None = None,
    inputs: Any = None,
    model: str | None = None,
    prompt_hash: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
    snapshot_id: int | None = None,
    proposal_id: int | None = None,
    commit: bool = True,
) -> FunnelStageRow:
    """Append one per-name audit row. Nothing drops silently — every name the
    funnel considers gets a row with the rule/signal that fired (or "no_match")
    and the source-cited inputs."""
    row = FunnelStageRow(
        run_id=run_id,
        stage=stage,
        subject=subject[:64],
        subject_type=subject_type,
        decision=decision,
        reason=reason,
        signal_or_rule=signal_or_rule,
        inputs_json=json.dumps(inputs, default=str) if inputs is not None else None,
        model=model,
        prompt_hash=prompt_hash,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        snapshot_id=snapshot_id,
        proposal_id=proposal_id,
    )
    session.add(row)
    if commit:
        session.commit()
        session.refresh(row)
    return row


# ----------------------------------------------------------------------
# Immutable per-decision snapshot
# ----------------------------------------------------------------------


def record_snapshot(
    session: "Session",
    *,
    run_id: int,
    user_id: str,
    ticker: str,
    day: str,
    decision: dict[str, Any],
    portfolio_snapshot: dict[str, Any],
    market_snapshot: dict[str, Any],
    policy_version: str,
    policy: dict[str, Any],
    model_name: str,
    prompt_template_hash: str,
    model_version: str | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    model_inputs: Any = None,
    source_refs: Any = None,
    unchanged_explanation: str | None = None,
    why_not_act: str | None = None,
    decision_run_id: int | None = None,
    proposal_id: int | None = None,
) -> DecisionSnapshot:
    """Write (or return the existing) IMMUTABLE decision snapshot.

    Dedup is on the FULL decision-input fingerprint, so an identical re-run is
    deduped while any real input change records a fresh, independent row.
    """
    portfolio_fp = fingerprint(portfolio_snapshot)
    market_fp = fingerprint(market_snapshot)
    key = snapshot_dedup_key(
        user_id=user_id, ticker=ticker, day=day, policy_version=policy_version,
        model_name=model_name, prompt_template_hash=prompt_template_hash,
        portfolio_fp=portfolio_fp, market_fp=market_fp,
    )
    existing = session.execute(
        select(DecisionSnapshot).where(DecisionSnapshot.dedup_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = DecisionSnapshot(
        run_id=run_id,
        user_id=user_id,
        ticker=ticker.upper(),
        dedup_key=key,
        decision_json=json.dumps(decision, default=str),
        model_name=model_name,
        model_version=model_version,
        prompt_template_hash=prompt_template_hash,
        temperature=temperature,
        seed=seed,
        model_inputs_json=(
            json.dumps(model_inputs, default=str) if model_inputs is not None else None
        ),
        source_refs_json=(
            json.dumps(source_refs, default=str) if source_refs is not None else None
        ),
        portfolio_snapshot_json=json.dumps(portfolio_snapshot, default=str),
        market_snapshot_json=json.dumps(market_snapshot, default=str),
        policy_version=policy_version,
        policy_json=json.dumps(policy, default=str),
        unchanged_explanation=unchanged_explanation,
        why_not_act=why_not_act,
        human_action_state="proposed",
        decision_run_id=decision_run_id,
        proposal_id=proposal_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    _log.info(
        "funnel_trace.snapshot_recorded",
        snapshot_id=row.id, run_id=run_id, ticker=ticker.upper(),
        model=model_name, policy_version=policy_version,
        action=decision.get("action"),
    )
    return row


__all__ = [
    "fingerprint",
    "run_idempotency_key",
    "snapshot_dedup_key",
    "open_run",
    "close_run",
    "record_stage_row",
    "record_snapshot",
]
