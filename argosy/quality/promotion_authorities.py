# argosy/quality/promotion_authorities.py
"""Read the per-run authority verdicts the fail-closed promote_gate consults.

The synthesis flow persists each authority's verdict as a ``decision_phases`` row:

    synthesis.phase_45  -> codex                 (phase_output_json["overall_assessment"])
    synthesis.phase_53  -> deterministic gate    (NOT read here — /accept re-runs the
                                                   enforce-filtered gate on the live draft)
    synthesis.phase_5   -> fund_manager          (read from DecisionRun.fund_manager_decision)
    synthesis.phase_55  -> whole_artifact_reader (phase_output_json["overall_assessment"])

This module extracts the two PHASE-ONLY authorities (codex + whole-artifact reader)
and returns their raw assessment string, which ``promote_gate.evaluate_promotion``
maps to clear/block (``APPROVE`` / ``APPROVE_WITH_CONDITIONS`` clear; ``BLOCK`` blocks).
A phase that is absent or unparseable returns ``None`` so the caller FAILS CLOSED.

Reconcile rounds can persist codex/reader more than once; we take the LATEST row
(highest ``seq``) — the verdict on the final draft, not a superseded round.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

CODEX_PHASE = 45
READER_PHASE = 55


def _latest_phase_assessment(
    session: Any, decision_run_id: int, phase_n: int,
) -> str | None:
    """Return ``overall_assessment`` from the latest ``synthesis.phase_<n>`` row,
    or ``None`` when the phase is missing / unparseable / carries no assessment."""
    if decision_run_id is None:
        return None
    row = session.execute(
        text(
            "SELECT phase_output_json FROM decision_phases "
            "WHERE decision_run_id = :r AND kind = :k "
            "ORDER BY seq DESC LIMIT 1"
        ),
        {"r": decision_run_id, "k": f"synthesis.phase_{phase_n}"},
    ).first()
    if not row or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        assessment = payload.get("overall_assessment")
        return str(assessment) if assessment is not None else None
    return None


def read_codex_verdict(session: Any, decision_run_id: int) -> str | None:
    """codex Phase-4.5 ``overall_assessment`` (APPROVE / APPROVE_WITH_CONDITIONS / BLOCK)."""
    return _latest_phase_assessment(session, decision_run_id, CODEX_PHASE)


def read_reader_verdict(session: Any, decision_run_id: int) -> str | None:
    """Whole-artifact reader / coherence deliberation ``overall_assessment``."""
    return _latest_phase_assessment(session, decision_run_id, READER_PHASE)


__all__ = ["read_codex_verdict", "read_reader_verdict", "CODEX_PHASE", "READER_PHASE"]
