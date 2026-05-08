"""Transcript bundle writer for the negotiation log (Wave C — provenance).

For each multi-agent phase boundary the negotiation recorder calls
``write_phase_bundle`` to produce a four-file mirror on disk:

  ``<ARGOSY_HOME>/transcripts/<user_id>/<YYYY-MM-DD>/<run_id>__<kind>/``
    ├── TLDR.md       — human-scannable verdict summary
    ├── transcript.md — chronological dump of every agent's response_text
    ├── verdict.json  — model_dump() of the parsed pydantic verdict DTO
    └── sequence.mmd  — Mermaid sequenceDiagram of the agent timeline

Templates are deterministic (no LLM, no rendering library) so this
module is pure-Python and unit-testable. The verdict DTOs live in
``argosy/agents/`` next to their owners and are never redefined here —
this module just dispatches on ``type(verdict).__name__``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from argosy.config import get_settings
from argosy.logging import get_logger

log = get_logger(__name__)


@dataclass
class ParticipantRef:
    """One agent invocation that participated in a phase."""

    agent_role: str
    agent_report_id: int
    response_text: str
    side: str | None = None        # bull / bear, when applicable
    perspective: str | None = None  # aggressive / neutral / conservative
    round: int | None = None        # 1, 2, ...
    confidence: str | None = None
    model: str | None = None


def _slug(s: str) -> str:
    """Filesystem-safe slug for the bundle dir name."""
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", s).strip("_") or "unknown"


def _bundle_path(
    *, user_id: str, decision_run_id: int, phase_kind: str, started_at: datetime,
) -> Path:
    home = Path(get_settings().home)
    date = started_at.strftime("%Y-%m-%d")
    name = f"{decision_run_id}__{_slug(phase_kind)}"
    return home / "transcripts" / user_id / date / name


# ----------------------------------------------------------------------
# TLDR templates (one per known DTO type)
# ----------------------------------------------------------------------


def _tldr_for_debate_outcome(v: Any) -> str:
    return (
        f"## Debate verdict\n\n"
        f"- **Winning side:** `{v.winning_side}`\n"
        f"- **Rounds run:** {v.rounds_run}\n"
        f"- **Confidence:** {getattr(v.confidence, 'value', v.confidence)}\n\n"
        f"**Synthesis:** {v.synthesis}\n\n"
        + (
            "**Cited evidence:**\n" + "\n".join(f"- {e}" for e in v.cited_evidence)
            if v.cited_evidence else ""
        )
    )


def _tldr_for_risk_outcome(v: Any) -> str:
    cond = (
        "\n".join(f"- {c}" for c in v.consolidated_conditions)
        if v.consolidated_conditions else "_none_"
    )
    return (
        f"## Risk verdict\n\n"
        f"- **Consensus:** `{v.consensus_verdict}`\n"
        f"- **Rounds run:** {v.rounds_run}\n"
        f"- **Confidence:** {getattr(v.confidence, 'value', v.confidence)}\n\n"
        f"**Conditions:**\n{cond}\n\n"
        + (f"**Dissent:** {v.dissent_summary}\n" if v.dissent_summary else "")
    )


def _tldr_for_fund_manager_decision(v: Any) -> str:
    rc = (
        "\n".join(f"- {c}" for c in v.required_conditions)
        if v.required_conditions else "_none_"
    )
    pec = (
        "\n".join(f"- {c}" for c in v.post_execution_checks)
        if v.post_execution_checks else "_none_"
    )
    return (
        f"## Fund manager verdict\n\n"
        f"- **Decision:** `{v.decision}`\n"
        f"- **Confidence:** {getattr(v.confidence, 'value', v.confidence)}\n\n"
        f"**Reason:** {v.reason}\n\n"
        f"**Required conditions:**\n{rc}\n\n"
        f"**Post-execution checks:**\n{pec}\n"
    )


def _tldr_for_fund_manager_plan_revision(v: Any) -> str:
    reasons = (
        "\n".join(f"- {r}" for r in v.reasons) if v.reasons else "_none_"
    )
    return (
        f"## Fund manager plan-revision verdict\n\n"
        f"- **Approved:** `{v.approved}`\n\n"
        f"**Reasons:**\n{reasons}\n"
    )


_TLDR_DISPATCH: dict[str, Any] = {
    "DebateOutcome": _tldr_for_debate_outcome,
    "RiskOutcome": _tldr_for_risk_outcome,
    "FundManagerDecision": _tldr_for_fund_manager_decision,
    "FundManagerPlanRevisionDecision": _tldr_for_fund_manager_plan_revision,
}


def _tldr_generic(v: BaseModel) -> str:
    """Fallback TL;DR for any pydantic model: dump the model_dump as a
    fenced JSON block. Not pretty, but always works.
    """
    return (
        f"## {type(v).__name__}\n\n"
        f"```json\n{v.model_dump_json(indent=2)}\n```\n"
    )


def render_tldr(verdict: BaseModel | None, phase_kind: str) -> str:
    """Render a deterministic TL;DR from the verdict DTO."""
    if verdict is None:
        return (
            f"## Phase: {phase_kind}\n\n"
            "_This phase has no facilitator verdict; see transcript.md for "
            "the per-agent outputs._\n"
        )
    fn = _TLDR_DISPATCH.get(type(verdict).__name__)
    return fn(verdict) if fn is not None else _tldr_generic(verdict)


# ----------------------------------------------------------------------
# Transcript + Mermaid renderers
# ----------------------------------------------------------------------


def _participant_label(p: ParticipantRef) -> str:
    bits: list[str] = [p.agent_role]
    if p.side:
        bits.append(f"side={p.side}")
    if p.perspective:
        bits.append(f"perspective={p.perspective}")
    if p.round is not None:
        bits.append(f"round={p.round}")
    return " · ".join(bits)


def render_transcript(participants: list[ParticipantRef], phase_kind: str) -> str:
    """Chronological markdown transcript of every agent that participated."""
    out = [f"# Transcript — `{phase_kind}`\n"]
    for i, p in enumerate(participants, start=1):
        out.append(f"\n## {i}. {_participant_label(p)}\n")
        meta_bits = []
        if p.confidence:
            meta_bits.append(f"confidence={p.confidence}")
        if p.model:
            meta_bits.append(f"model={p.model}")
        if meta_bits:
            out.append("`" + " · ".join(meta_bits) + "`\n\n")
        out.append(p.response_text or "_(no response text)_")
        out.append("\n")
    return "".join(out)


def render_sequence_mmd(
    participants: list[ParticipantRef],
    phase_kind: str,
    *,
    verdict: BaseModel | None = None,
) -> str:
    """Mermaid sequenceDiagram body for the phase.

    Renders one ``participant`` per distinct agent_role plus a synthetic
    ``User`` actor. Each agent invocation becomes a User->>agent prompt
    arrow followed by an agent-->>User reply arrow with a one-line
    snippet of the confidence/role label. The verdict is rendered as a
    final ``Note over`` line so a reader can see the conclusion at a
    glance.
    """
    out = ["sequenceDiagram"]
    out.append(f"    participant U as User")
    seen_roles: list[str] = []
    for p in participants:
        if p.agent_role not in seen_roles:
            seen_roles.append(p.agent_role)
            safe_id = _slug(p.agent_role)
            out.append(f"    participant {safe_id} as {p.agent_role}")
    for p in participants:
        safe_id = _slug(p.agent_role)
        round_label = f" round {p.round}" if p.round is not None else ""
        side_label = f" ({p.side})" if p.side else ""
        out.append(f"    U->>{safe_id}: prompt{round_label}{side_label}")
        snippet = (p.confidence or "respond").lower()
        out.append(f"    {safe_id}-->>U: {snippet}")
    if verdict is not None and seen_roles:
        last_role = _slug(seen_roles[-1])
        verdict_kind = type(verdict).__name__
        verdict_summary = _short_verdict_summary(verdict)
        out.append(f"    Note over {last_role}: {verdict_kind} → {verdict_summary}")
    out.append(f"    Note over U: phase = {phase_kind}")
    return "\n".join(out) + "\n"


def _short_verdict_summary(v: Any) -> str:
    """One-line summary of the verdict for the mermaid Note line."""
    name = type(v).__name__
    if name == "DebateOutcome":
        return f"winner={v.winning_side}"
    if name == "RiskOutcome":
        return f"verdict={v.consensus_verdict}"
    if name == "FundManagerDecision":
        return f"decision={v.decision}"
    if name == "FundManagerPlanRevisionDecision":
        return f"approved={v.approved}"
    return name


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def write_phase_bundle(
    *,
    user_id: str,
    decision_run_id: int,
    phase_kind: str,
    started_at: datetime,
    finished_at: datetime,
    verdict: BaseModel | None,
    participants: list[ParticipantRef],
) -> tuple[Path, str, str]:
    """Write the four-file mirror bundle and return (bundle_dir, tldr_md, sequence_mmd).

    The returned ``tldr_md`` and ``sequence_mmd`` are also persisted in
    the ``decision_phases`` row so the API can render them without
    reading disk for the common case.
    """
    bundle = _bundle_path(
        user_id=user_id,
        decision_run_id=decision_run_id,
        phase_kind=phase_kind,
        started_at=started_at,
    )
    bundle.mkdir(parents=True, exist_ok=True)

    tldr = render_tldr(verdict, phase_kind)
    transcript = render_transcript(participants, phase_kind)
    sequence = render_sequence_mmd(participants, phase_kind, verdict=verdict)

    # Header on TL;DR with timing.
    duration = (finished_at - started_at).total_seconds()
    header = (
        f"# Phase: {phase_kind}\n"
        f"\n"
        f"- **Run:** {decision_run_id}\n"
        f"- **Started:** {started_at.isoformat()}\n"
        f"- **Finished:** {finished_at.isoformat()}\n"
        f"- **Duration:** {duration:.1f}s\n"
        f"- **Participants:** {len(participants)}\n"
        f"\n---\n\n"
    )
    full_tldr = header + tldr

    (bundle / "TLDR.md").write_text(full_tldr, encoding="utf-8")
    (bundle / "transcript.md").write_text(transcript, encoding="utf-8")
    (bundle / "sequence.mmd").write_text(sequence, encoding="utf-8")
    if verdict is not None:
        (bundle / "verdict.json").write_text(
            json.dumps(verdict.model_dump(), indent=2, default=str),
            encoding="utf-8",
        )
    else:
        (bundle / "verdict.json").write_text("{}", encoding="utf-8")

    log.info(
        "transcript_writer.bundle_written",
        user_id=user_id,
        decision_run_id=decision_run_id,
        phase_kind=phase_kind,
        bundle_dir=str(bundle),
        participants=len(participants),
    )
    return bundle, full_tldr, sequence


__all__ = [
    "ParticipantRef",
    "render_tldr",
    "render_transcript",
    "render_sequence_mmd",
    "write_phase_bundle",
]
