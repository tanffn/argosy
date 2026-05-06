"""Markdown rendering helpers for plan synthesis outputs."""

from __future__ import annotations


def _horizon_md(section) -> str:
    """Render a HorizonSection to a markdown view used by the UI side sheet."""
    lines = [f"# {section.horizon.title()} horizon — status: {section.status}"]
    lines.append("")
    if section.posture:
        lines.append(f"**Posture.** {section.posture}")
        lines.append("")
    if section.targets:
        lines.append("## Targets")
        for t in section.targets:
            suffix = f" — {t.rationale}" if t.rationale else ""
            lines.append(
                f"- **{t.label}**: {t.value} {t.unit} "
                f"(stated {t.stated_at.isoformat()}; revisit {t.revisit_after.isoformat()})"
                f"{suffix}"
            )
        lines.append("")
    if section.themes:
        lines.append("## Themes")
        for th in section.themes:
            th_suffix = f" — {th.rationale}" if th.rationale else ""
            lines.append(f"- **{th.label}** ({th.direction}){th_suffix}")
        lines.append("")
    if section.actions:
        lines.append("## Actions")
        for a in section.actions:
            trigger = f" [{a.trigger_or_date}]" if a.trigger_or_date else ""
            lines.append(f"- **{a.label}**{trigger}: {a.detail} — {a.rationale}")
        lines.append("")
    if section.horizon == "short" and section.speculative_candidates:
        lines.append("## Speculative candidates")
        for sc in section.speculative_candidates:
            lines.append(
                f"- **{sc.ticker}**: max ${sc.suggested_position_usd:,.0f} "
                f"(= {sc.suggested_position_pct_of_net_worth*100:.2f}% NW) · "
                f"{sc.thesis_summary} · exit: {sc.exit_trigger}"
            )
        lines.append("")
    if section.deltas_from_prior:
        lines.append("## Deltas vs. prior current")
        for d in section.deltas_from_prior:
            lines.append(
                f"- [{d.change_kind}] {d.summary} ({d.item_kind} `{d.item_id}`)"
            )
        lines.append("")
    if section.rationale:
        lines.append("## Rationale")
        lines.append(section.rationale)
    return "\n".join(lines).rstrip() + "\n"
