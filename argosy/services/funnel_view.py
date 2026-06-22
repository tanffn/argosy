"""Two views off the same funnel trace (P0 / D3).

1. ``build_run_detail`` — the DEBUG view: the run + every per-stage, per-name
   row (incl. dropped names + reasons + model/tokens) + the immutable
   snapshots. Powers ``GET /api/decisions/funnel/runs/{id}``.
2. ``build_client_narrative`` — the plain-language "what Argosy did for me"
   view for the /proposals transparency section: "Scanned the market
   (semis -3%, risk-off) -> flagged NVDA + the index sleeve -> reviewed NVDA
   deeply -> proposed a trim. 48 names: no action."

Both are pure functions over the trace rows so they are trivially testable and
the frontend never re-derives the funnel's logic.
"""

from __future__ import annotations

import json
from typing import Any


def _loads(blob: Any) -> Any:
    if blob in (None, ""):
        return None
    if isinstance(blob, (dict, list)):
        return blob
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def run_summary(run: Any) -> dict[str, Any]:
    """Compact run header (list view + detail header)."""
    return {
        "run_id": run.id,
        "user_id": run.user_id,
        "trigger": run.trigger,
        "shadow": bool(run.shadow),
        "status": run.status,
        "policy_version": run.policy_version,
        "ips_version": run.ips_version,
        "plan_version_id": run.plan_version_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "totals": _loads(run.totals_json) or {},
    }


def _stage_row(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "stage": row.stage,
        "subject": row.subject,
        "subject_type": row.subject_type,
        "decision": row.decision,
        "reason": row.reason,
        "signal_or_rule": row.signal_or_rule,
        "inputs": _loads(row.inputs_json),
        "model": row.model,
        "prompt_hash": row.prompt_hash,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "cost_usd": row.cost_usd,
        "duration_ms": row.duration_ms,
        "snapshot_id": row.snapshot_id,
        "proposal_id": row.proposal_id,
    }


def _snapshot(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "ticker": row.ticker,
        "dedup_key": row.dedup_key,
        "decision": _loads(row.decision_json),
        "model_name": row.model_name,
        "model_version": row.model_version,
        "prompt_template_hash": row.prompt_template_hash,
        "temperature": row.temperature,
        "seed": row.seed,
        "policy_version": row.policy_version,
        "policy": _loads(row.policy_json),
        "portfolio_snapshot": _loads(row.portfolio_snapshot_json),
        "market_snapshot": _loads(row.market_snapshot_json),
        "source_refs": _loads(row.source_refs_json),
        "unchanged_explanation": row.unchanged_explanation,
        "why_not_act": row.why_not_act,
        "execution_drift": _loads(row.execution_drift_json),
        "human_action_state": row.human_action_state,
        "proposal_id": row.proposal_id,
        "decision_run_id": row.decision_run_id,
    }


def build_run_detail(
    run: Any, stage_rows: list[Any], snapshots: list[Any]
) -> dict[str, Any]:
    """Full debug view: run header + per-stage rows (ordered) + snapshots."""
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for r in stage_rows:
        by_stage.setdefault(r.stage, []).append(_stage_row(r))
    return {
        **run_summary(run),
        "macro_read": _loads(run.macro_read_json),
        "error_message": run.error_message,
        "stages": by_stage,
        "snapshots": [_snapshot(s) for s in snapshots],
    }


def build_client_narrative(run: Any, stage_rows: list[Any]) -> dict[str, Any]:
    """Plain-language transparency summary for the client surface.

    Returns a structured object (not just a string) so the UI can render it:
    a one-line headline, the per-stage counts, the names acted on, and the
    count of names with no action. Self-resolved work is summarised here, never
    pushed to the active to-do list (feedback_client_in_loop_only_when_needed).
    """
    macro = _loads(run.macro_read_json) or {}
    considered = [r for r in stage_rows if r.stage in ("stage1", "stage2", "stage3")]
    routed = [r for r in stage_rows if r.stage == "stage1" and r.decision == "routed"]
    deep = [r for r in stage_rows if r.stage == "stage3"]
    proposed = [r for r in stage_rows if r.decision == "proposed"]
    surfaced = [r for r in stage_rows if r.stage == "surface" and r.decision == "surfaced"]

    # Names with no action = considered subjects that never reached a proposal.
    acted_subjects = {r.subject for r in proposed}
    no_action_subjects = {
        r.subject for r in considered if r.subject_type in ("holding", "watch")
    } - acted_subjects

    headline_bits: list[str] = []
    macro_line = macro.get("summary") or macro.get("headline")
    if macro_line:
        headline_bits.append(f"Scanned the market ({macro_line})")
    else:
        headline_bits.append("Scanned the market")
    if routed:
        names = ", ".join(sorted({r.subject for r in routed})[:5])
        headline_bits.append(f"flagged {names}")
    if deep:
        names = ", ".join(sorted({r.subject for r in deep})[:5])
        headline_bits.append(f"reviewed {names} deeply")
    if proposed:
        acts = ", ".join(
            f"{(_loads(r.inputs_json) or {}).get('action', 'action')} {r.subject}"
            for r in proposed[:5]
        )
        headline_bits.append(f"proposed {acts}")
    headline = " -> ".join(headline_bits)
    if no_action_subjects:
        headline += f". {len(no_action_subjects)} names: no action."

    return {
        "run_id": run.id,
        "shadow": bool(run.shadow),
        "as_of": run.finished_at.isoformat() if run.finished_at else (
            run.started_at.isoformat() if run.started_at else None
        ),
        "headline": headline,
        "macro": macro,
        "counts": {
            "routed": len(routed),
            "deep_reviewed": len(deep),
            "proposed": len(proposed),
            "surfaced": len(surfaced),
            "no_action": len(no_action_subjects),
        },
        "proposed": [
            {
                "subject": r.subject,
                "proposal_id": r.proposal_id,
                "reason": r.reason,
            }
            for r in proposed
        ],
    }


__all__ = ["run_summary", "build_run_detail", "build_client_narrative"]
