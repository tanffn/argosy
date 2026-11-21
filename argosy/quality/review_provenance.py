"""Track draft authorship across processes and resumes for cross-engine review."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path

from argosy.config import get_settings


_reconcile_run = ContextVar("argosy_reconcile_run", default=None)
_AUTHOR_ROLES = frozenset({
    "plan_synthesizer", "plan_language_rewriter", "prose_editor",
    "owner_remediation", "coherence_arbitrator", "coherence_facilitator",
    "coherence_panelist", "plan_coherence_closer",
})


@contextmanager
def plan_reconcile_scope(decision_run_id: int):
    """Associate edits made by the synchronous reconciliation helpers with the draft."""
    contributors = set()
    token = _reconcile_run.set((decision_run_id, contributors))
    try:
        yield contributors
    finally:
        _reconcile_run.reset(token)


def _marker(decision_run_id: int) -> Path:
    return get_settings().home / "decisions" / str(int(decision_run_id)) / "codex-contributed"


def record_codex_plan_author(decision_id: object, agent_role: str, output) -> None:
    if agent_role not in _AUTHOR_ROLES:
        return
    scope = _reconcile_run.get()
    if not decision_id and scope is None:
        return
    output._codex_contributed = True
    if scope and decision_id in (None, "", scope[0], f"plan-synth-{scope[0]}"):
        scope[1].add(agent_role)


def set_plan_authorship(decision_run_id: int, has_codex_author: bool) -> None:
    """Replace the receipt only when the orchestrator adopts a draft revision."""
    marker = _marker(decision_run_id)
    if has_codex_author:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    else:
        marker.unlink(missing_ok=True)


def codex_authored(output) -> bool:
    return getattr(output, "_codex_contributed", False) is True


def phase_three_snapshot(output, reports, decision_run_id: int) -> str:
    """Persist authorship alongside the exact phase-3 bytes eligible for resume."""
    used_codex = codex_authored(output) or any(codex_authored(r.output) for r in reports)
    output._codex_contributed = used_codex
    set_plan_authorship(decision_run_id, used_codex)
    if not used_codex:
        return output.model_dump_json()
    return json.dumps({**output.model_dump(mode="json"), "_codex_contributed": True})


def codex_contributed(decision_run_id: int) -> bool:
    try:
        _marker(decision_run_id).stat()
    except FileNotFoundError:
        return False
    return True
