"""Pure-Python markdown rendering of a PlanDistillate.

Used in two places:
  1. Stored on `plan_versions.distillate_rendered` so the UI can render
     the distillate without parsing JSON.
  2. Available to any synthesis agent that wants a human-readable
     view alongside the structured payload.

No LLM calls. Deterministic.
"""

from __future__ import annotations

from argosy.agents.plan_distiller_types import PlanDistillate


def render_distillate(d: PlanDistillate) -> str:
    """Render a PlanDistillate to compact markdown.

    Target output size ~1500 tokens for a typical Jacobs-style plan.
    """
    lines: list[str] = []
    lines.append(f"# Plan distillate — {d.plan_label}")
    lines.append("")
    lines.append(f"_Distilled at: {d.distilled_at_iso}_")
    lines.append("")

    if d.goals:
        lines.append("## Goals")
        for g in d.goals:
            edited = " *(user-edited)*" if g.user_edited else ""
            value = f": {g.value}" if g.value else ""
            rationale = f" — {g.rationale}" if g.rationale else ""
            lines.append(f"- **{g.label}**{value}{rationale}{edited}")
            if g.source_section:
                lines.append(f"  · _source: {g.source_section}_")
        lines.append("")

    if d.principles:
        lines.append("## Principles")
        for p in d.principles:
            edited = " *(user-edited)*" if p.user_edited else ""
            rationale = f" — {p.rationale}" if p.rationale else ""
            lines.append(f"- **{p.label}**{rationale}{edited}")
            if p.source_section:
                lines.append(f"  · _source: {p.source_section}_")
        lines.append("")

    if d.risk_priorities:
        lines.append("## Risk priorities")
        lines.append("_(ordered; first item dominates)_")
        for i, r in enumerate(d.risk_priorities, 1):
            lines.append(f"{i}. {r}")
        lines.append("")

    if d.decision_rules:
        lines.append("## Decision rules")
        for r in d.decision_rules:
            edited = " *(user-edited)*" if r.user_edited else ""
            lines.append(f"- **{r.label}**: {r.rule}{edited}")
            if r.source_section:
                lines.append(f"  · _source: {r.source_section}_")
        lines.append("")

    if d.targets:
        lines.append("## Targets")
        lines.append("_(working assumptions, not eternal — each carries an as-of date)_")
        for t in d.targets:
            edited = " *(user-edited)*" if t.user_edited else ""
            stated = t.stated_at.isoformat()
            revisit = t.revisit_after.isoformat()
            rationale = f" — {t.rationale}" if t.rationale else ""
            lines.append(
                f"- **{t.label}**: {t.value} {t.unit} "
                f"(stated {stated}, revisit {revisit}){rationale}{edited}"
            )
            if t.source_section:
                lines.append(f"  · _source: {t.source_section}_")
        lines.append("")

    if d.constraints:
        lines.append("## Constraints")
        for c in d.constraints:
            edited = " *(user-edited)*" if c.user_edited else ""
            lines.append(f"- **{c.label}**: {c.detail}{edited}")
            if c.source_section:
                lines.append(f"  · _source: {c.source_section}_")
        lines.append("")

    if d.stress_tolerance:
        lines.append("## Stress tolerance")
        lines.append(d.stress_tolerance)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_distillate"]
