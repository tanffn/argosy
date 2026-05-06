"""Compact projection of the current plan, for advisor system-prompt injection.

Per spec §6.2: deterministic Python helper, no LLM call. Reads the
user's role='current' PlanVersion and emits a ~500-800 token markdown
block. Truncates themes/actions if oversize.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer_types import HorizonSection
from argosy.state.queries import get_current_plan

# Hard cap (chars) — projection must stay reasonable for prompt cache stability.
MAX_CHARS = 6000


def _section_block(section: HorizonSection) -> str:
    lines: list[str] = []
    lines.append(
        f"[{section.horizon}, freshness={section.freshness_expected}, "
        f"status={section.status}]"
    )
    if section.posture:
        lines.append(f"  Posture: {section.posture}")
    if section.targets:
        lines.append("  Top targets (with stated-at):")
        for t in section.targets[:5]:
            stated = t.stated_at.isoformat()
            revisit = t.revisit_after.isoformat()
            lines.append(
                f"    - {t.label}: {t.value} {t.unit} "
                f"(stated {stated}; revisit {revisit})"
            )
    if section.themes:
        lines.append("  Active themes:")
        for th in section.themes[:5]:
            lines.append(f"    - {th.label} ({th.direction})")
    if section.actions:
        kind = (
            "directional" if section.horizon == "long"
            else "parameterized" if section.horizon == "medium"
            else "dated"
        )
        lines.append(f"  Actions ({kind}):")
        for a in section.actions[:8]:
            trigger = f" [{a.trigger_or_date}]" if a.trigger_or_date else ""
            lines.append(f"    - {a.label}{trigger}: {a.detail}")
    if section.horizon == "short" and section.speculative_candidates:
        lines.append("  Speculative candidates surfaced:")
        for sc in section.speculative_candidates[:5]:
            lines.append(
                f"    - {sc.ticker}: max ${sc.suggested_position_usd:,.0f} "
                f"(= {sc.suggested_position_pct_of_net_worth*100:.2f}% NW) · "
                f"{sc.thesis_summary} · exit: {sc.exit_trigger}"
            )
    return "\n".join(lines)


def compact_projection(session: Session, *, user_id: str) -> str | None:
    """Return the compact markdown projection of the user's current plan,
    or None if no current plan exists.
    """
    pv = get_current_plan(session, user_id)
    if pv is None:
        return None

    parts: list[str] = []
    label = pv.version_label or "current"
    accepted = pv.accepted_at.isoformat() if pv.accepted_at else "(unaccepted)"
    parts.append(f"=== Your current plan ({label}; accepted {accepted}) ===")
    parts.append("")

    for horizon_field in ("horizon_long_json", "horizon_medium_json", "horizon_short_json"):
        raw = getattr(pv, horizon_field)
        if not raw:
            continue
        section = HorizonSection.model_validate_json(raw)
        parts.append(_section_block(section))
        parts.append("")

    parts.append("=== End plan ===")
    out = "\n".join(parts)
    if len(out) > MAX_CHARS:
        out = out[: MAX_CHARS - 100] + "\n\n[truncated to fit token budget]\n=== End plan ==="
    return out


__all__ = ["compact_projection", "MAX_CHARS"]
