"""Markdown rendering helpers for plan synthesis outputs.

Two variants:

- ``_horizon_md_user``: the user-facing surface (Phase 1 of
  docs/plans/argosy-comprehensive-plan-integration.md). Drops the
  ``# Horizon — status: minor_revision`` header in favor of plain
  ``# Horizon``, drops ``(stated …; revisit …)`` parentheticals on
  target lines, and omits the ``## Deltas vs. prior current`` block
  entirely. A defense-in-depth regex strip (re-applying Phase 0's
  ``HISTORY_LEAK_PATTERNS``) runs at the tail so a future prompt
  regression cannot silently re-introduce revision narration.

- ``_horizon_md_audit``: the full-fidelity render for
  ``/decisions/<id>`` dev pane. Retains status header, revisit
  parentheticals, and the deltas block by design.

Persisted respectively to ``plan_versions.horizon_{long,medium,short}_md``
(user) and ``plan_versions.horizon_{long,medium,short}_md_audit``
(audit) on both the initial-synthesis and amendment-driven write
paths.
"""

from __future__ import annotations

import re

from argosy.quality.regex_patterns import HISTORY_LEAK_PATTERNS


def _emit_themes_actions_specs(section, lines: list[str]) -> None:
    """Shared body section emit — themes / actions /
    speculative_candidates render identically in both variants."""
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


def _strip_history_leak(md: str) -> str:
    """Defense-in-depth: re-apply Phase 0's HISTORY_LEAK_PATTERNS
    against the user-facing markdown.

    Matches are substituted with empty string (NOT a placeholder — a
    placeholder would itself look like revision narration). The
    renderer should already produce clean output by construction; this
    catches future regressions where a new structured field accidentally
    serializes history-bearing text into the prose surface.

    Whitespace cleanup runs after the substitutions to keep paragraph
    layout intact when matches were embedded mid-line.
    """
    out = md
    for pattern in HISTORY_LEAK_PATTERNS:
        out = pattern.sub("", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _horizon_md_user(section) -> str:
    """User-facing markdown render.

    Forbidden surfaces dropped vs. the audit variant:
    - line 1: the ``— status: <minor|major>_revision`` suffix on the
      H1 header (now plain ``# {Horizon}``).
    - per target: the ``(stated YYYY-MM-DD; revisit YYYY-MM-DD)``
      parenthetical metadata.
    - whole block: ``## Deltas vs. prior current`` (lives in audit
      pane only).
    """
    lines = [f"# {section.horizon.title()} horizon"]
    lines.append("")
    if section.posture:
        lines.append(f"**Posture.** {section.posture}")
        lines.append("")
    if section.targets:
        lines.append("## Targets")
        for t in section.targets:
            suffix = f" — {t.rationale}" if t.rationale else ""
            lines.append(f"- **{t.label}**: {t.value} {t.unit}{suffix}")
        lines.append("")
    _emit_themes_actions_specs(section, lines)
    if section.rationale:
        lines.append("## Rationale")
        lines.append(section.rationale)
    out = "\n".join(lines).rstrip() + "\n"
    return _strip_history_leak(out)


def _horizon_md_audit(section) -> str:
    """Full-fidelity render for ``/decisions/<id>`` audit pane.

    Retains the line-1 status suffix, the per-target stated/revisit
    parentheticals, and the ``## Deltas vs. prior current`` block —
    all intentionally available to the developer view.
    """
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
                f"(stated {t.stated_at.isoformat()}; "
                f"revisit {t.revisit_after.isoformat()})"
                f"{suffix}"
            )
        lines.append("")
    _emit_themes_actions_specs(section, lines)
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


# Back-compat alias: any stale import of ``_horizon_md`` resolves to the
# user variant. Phase 2/3 will migrate the in-tree callers; until then
# the alias keeps amendment / export / narrator code paths running on
# the cleaned output without an additional commit.
_horizon_md = _horizon_md_user
