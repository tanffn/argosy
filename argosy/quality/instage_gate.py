"""Run the deterministic plan-output gate IN-STAGE (during synthesis), on the
just-persisted draft, before the expensive LLM whole-artifact reader.

This is the shift-left point ("Layer B" in the checks-all-the-way design): the
same checks /accept runs at promotion, run here at synthesis time so a cheap
deterministic defect (IPS sum, cross-surface divergence, stale date, FX unit,
fabricated number, cap regression) is surfaced before the reader spends ~80 min
finding it. Surfaces only — does not auto-correct (that is a later slice).

Dependency-injected ``assemble`` / ``resolve`` / ``current_plan`` callables
default to the real services so the orchestrator calls it with no extra wiring,
while tests inject stubs and avoid a live synthesis.
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date
from typing import Any, Callable

from argosy.quality.gate_types import GateVerdict
from argosy.quality.plan_output_gate import gate_plan_output

log = logging.getLogger(__name__)


def _nvda_cap(plan: Any) -> float | None:
    raw = getattr(plan, "target_allocation_json", None)
    if not raw:
        return None
    try:
        return json.loads(raw).get("nvda_cap_pct")
    except Exception:  # noqa: BLE001 — best-effort
        return None


def run_deterministic_gate_instage(
    *,
    session: Any,
    user_id: str,
    draft: Any,
    decision_run_id: int,
    today: _date | None = None,
    snapshot_date: _date | None = None,
    assemble: Callable[[Any, str], Any] | None = None,
    resolve: Callable[[Any, str, int], Any] | None = None,
    current_plan: Callable[[Any, str], Any] | None = None,
) -> GateVerdict:
    """Assemble the persisted draft + run the deterministic gate suite. Never
    raises — a gathering failure degrades to an empty-input gate call (which
    simply runs fewer checks), so synthesis is never aborted by this surface."""
    if assemble is None:
        from argosy.services.assembled_artifact import assemble_plan_artifact as assemble
    if resolve is None:
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers as resolve
    if current_plan is None:
        from argosy.state.queries import get_current_plan as current_plan

    today = today or _date.today()

    # NOTE: the real services take user_id / decision_run_id as KEYWORD-ONLY
    # args (assemble_plan_artifact(session, *, user_id);
    # resolve_plan_numbers(session, *, user_id, decision_run_id)). Call them by
    # keyword so the live default path works — a positional call raises and would
    # silently degrade the gate to artifact=None/resolved=None (skipping the
    # cross-surface + resolver-based invariants). Test stubs use matching param
    # names, so keyword passing is compatible with both.
    artifact = None
    try:
        artifact = assemble(session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("instage_gate.assemble_failed user=%s err=%s", user_id, exc)

    resolved = None
    try:
        resolved = resolve(session, user_id=user_id, decision_run_id=decision_run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("instage_gate.resolve_failed user=%s err=%s", user_id, exc)

    prior_cap = None
    try:
        prior = current_plan(session, user_id)
        prior_cap = _nvda_cap(prior) if prior is not None else None
    except Exception as exc:  # noqa: BLE001
        log.warning("instage_gate.current_plan_failed user=%s err=%s", user_id, exc)

    fx_usd_nis = None
    try:
        rv = resolved.get("fx.usd_nis") if resolved is not None else None
        if rv is not None and getattr(rv, "status", None) == "resolved" and getattr(rv, "value", None) is not None:
            fx_usd_nis = float(rv.value)
    except Exception:  # noqa: BLE001
        fx_usd_nis = None

    horizon_text = {
        "long": getattr(draft, "horizon_long_md", "") or "",
        "medium": getattr(draft, "horizon_medium_md", "") or "",
        "short": getattr(draft, "horizon_short_md", "") or "",
    }
    return gate_plan_output(
        horizon_text=horizon_text,
        synth=None,
        distillate=None,
        resolved=resolved,
        artifact=artifact,
        today=today,
        snapshot_date=snapshot_date,
        fx_usd_nis=fx_usd_nis,
        current_nvda_cap_pct=_nvda_cap(draft),
        prior_nvda_cap_pct=prior_cap,
    )
