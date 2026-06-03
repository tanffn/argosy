"""Markdown rendering helpers for plan synthesis outputs.

Two horizon variants (per SDD §6.11):

- ``_horizon_md_user``: the user-facing surface. v4 (renderer wins
  block B1, 2026-06-02): renders the ``## Deltas vs. prior current``
  block at the TOP of each horizon (was: stripped). Drops the
  ``# Horizon — status: minor_revision`` H1 suffix in favor of plain
  ``# Horizon``, drops ``(stated …; revisit …)`` parentheticals on
  target lines. The defense-in-depth regex strip (Phase 0's
  ``HISTORY_LEAK_PATTERNS``) is still applied at the tail, with the
  Deltas-block-header pattern carved out so the now-intentional top
  block survives. The status-header and revisit-parenthetical patterns
  are retained to catch future prompt regressions.

- ``_horizon_md_audit``: the full-fidelity render for
  ``/decisions/<id>`` dev pane. Retains status header, revisit
  parentheticals, and the deltas block at the bottom of the document
  (mirrors the v1-v3 audit shape so the developer view keeps a
  stable structure across plan revisions).

Persisted respectively to ``plan_versions.horizon_{long,medium,short}_md``
(user) and ``plan_versions.horizon_{long,medium,short}_md_audit``
(audit) on both the initial-synthesis and amendment-driven write
paths.

v4 plan-document appendices (block B1):

- ``render_section_evidence_appendix(output)`` — renders the synth's
  flat ``sections: list[Section]`` (Phase 3 §3.2 Check 3 output, up to
  20 entries) as a single ``## Appendix — Section-by-section evidence``
  block with each section's ``body_md`` followed by a collapsible
  ``<details>`` envelope containing the FactClaim / Citation /
  Assumption / missing_data sub-tree. Sections are global across all
  three horizons, so the appendix appears once — appended to
  ``horizon_long_md`` (no new column; the v1 schema decision is
  documented inline at the call site).
- ``render_assumption_ledger_appendix()`` — hard-coded v1 table of the
  15 canonical plan assumptions from ``tmp_review/plan_document_v4_spec.md``
  §2/§3 (real return, inflation, FX, tax rates, etc). v2 will derive
  this from agent outputs; ship-v1 is enough to surface the values to
  the user.
- ``render_fleet_receipts_appendix(session, decision_run_id)`` —
  queries ``agent_reports`` for the synth run and emits one row per
  agent (role / size / model / tokens / cost / first finding line).
  Lets the user see "26 agents ran for $X.YZ" instead of the
  ~21 KB user-facing markdown hiding the work entirely.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from argosy.quality.regex_patterns import HISTORY_LEAK_PATTERNS

if TYPE_CHECKING:  # pragma: no cover — type-checker hint only
    from sqlalchemy.orm import Session

    from argosy.agents.plan_synthesizer_types import (
        PlanSynthesisOutput,
        Section,
    )


# ----------------------------------------------------------------------
# Horizon-section render — internal helpers
# ----------------------------------------------------------------------


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


# Subset of ``HISTORY_LEAK_PATTERNS`` we still apply to the user-facing
# render in v4. The Deltas-block-header pattern (``^## Deltas vs. prior``)
# is intentionally EXCLUDED — v4 promotes the deltas block to the top of
# the user-facing horizon md, so stripping its header would silently
# delete the now-intentional content. The status-suffix and
# stated/revisit-parenthetical patterns stay because the renderer
# already builds clean prose by construction and those are noise.
_USER_HISTORY_LEAK_PATTERNS: list[re.Pattern[str]] = [
    p
    for p in HISTORY_LEAK_PATTERNS
    if "Deltas" not in p.pattern
]


def _strip_history_leak(md: str) -> str:
    """Defense-in-depth: re-apply the carved-down history-leak pattern
    set against the user-facing markdown.

    Matches are substituted with empty string (NOT a placeholder — a
    placeholder would itself look like revision narration). The renderer
    should already produce clean output by construction; this catches
    future regressions where a new structured field accidentally
    serializes history-bearing text into the prose surface.

    Whitespace cleanup runs after the substitutions to keep paragraph
    layout intact when matches were embedded mid-line.
    """
    out = md
    for pattern in _USER_HISTORY_LEAK_PATTERNS:
        out = pattern.sub("", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _emit_deltas_block(section, lines: list[str]) -> None:
    """Render the ``## Deltas vs. prior current`` block.

    Shared between the user-facing top render (v4) and the audit
    bottom render. Each delta gets one bullet:
    ``- [change_kind] summary (item_kind `item_id`)``.

    v4 (block B1, 2026-06-02) — user variant emits this at the TOP of
    each horizon md because the user explicitly asked for it as the
    first thing they see (counter-decision to the Phase 1 strip).
    Audit variant emits it at the bottom for the developer pane (where
    rationale + targets context is needed first).
    """
    if not section.deltas_from_prior:
        return
    lines.append("## Deltas vs. prior current")
    for d in section.deltas_from_prior:
        lines.append(
            f"- [{d.change_kind}] {d.summary} ({d.item_kind} `{d.item_id}`)"
        )
    lines.append("")


def _horizon_md_user(section) -> str:
    """User-facing markdown render.

    v4 (block B1, 2026-06-02): the ``## Deltas vs. prior current``
    block is rendered at the TOP of the document (after the H1) when
    the section carries deltas — the user explicitly asked to see
    "what changed since last time" before reading the new posture /
    targets / actions.

    Surfaces still dropped vs. the audit variant:
    - line 1: the ``— status: <minor|major>_revision`` suffix on the
      H1 header (now plain ``# {Horizon}``).
    - per target: the ``(stated YYYY-MM-DD; revisit YYYY-MM-DD)``
      parenthetical metadata.
    """
    lines = [f"# {section.horizon.title()} horizon"]
    lines.append("")
    # v4: Deltas block at TOP, before any other content. ``_emit_deltas_block``
    # is a no-op when ``deltas_from_prior`` is empty (e.g. baseline / first-
    # ever synth) so initial plans still render cleanly.
    _emit_deltas_block(section, lines)
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
    parentheticals, and the ``## Deltas vs. prior current`` block at
    the bottom of the document — all intentionally available to the
    developer view.
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
    _emit_deltas_block(section, lines)
    if section.rationale:
        lines.append("## Rationale")
        lines.append(section.rationale)
    return "\n".join(lines).rstrip() + "\n"


# Back-compat alias: any stale import of ``_horizon_md`` resolves to the
# user variant.
_horizon_md = _horizon_md_user


# ----------------------------------------------------------------------
# v4 plan-document appendices (block B1)
# ----------------------------------------------------------------------


def _render_section_evidence_subtree(section: Section) -> str:
    """Render one Section's FactClaim / Citation / Assumption / missing_data
    sub-tree as a markdown body suitable for embedding inside a
    ``<details>`` envelope.

    Markdown is preferred over JSON dumps so the result is readable
    inline in any GitHub-style renderer. Each evidence sub-list is
    introduced by a bold header; empty sub-lists are omitted entirely
    (avoiding noisy "(none)" lines for the common case where a
    well-cited section has no missing_data).
    """
    ev = section.evidence
    parts: list[str] = []
    if ev.facts:
        parts.append("**Facts**")
        for i, f in enumerate(ev.facts):
            value_part = ""
            if f.value is not None:
                unit_suffix = f" {f.unit}" if f.unit else ""
                value_part = f" — value: `{f.value}{unit_suffix}`"
            parts.append(f"{i}. *({f.kind})* {f.text}{value_part}")
        parts.append("")
    if ev.source_span:
        parts.append("**Citations**")
        for c in ev.source_span:
            extract = (c.extract or "").strip()
            extract_part = f" — extract: “{extract}”" if extract else ""
            parts.append(
                f"- fact[{c.supports_fact_index}] · `{c.source_kind}` · "
                f"`{c.source_locator}`{extract_part}"
            )
        parts.append("")
    if ev.assumptions:
        parts.append("**Assumptions**")
        for a in ev.assumptions:
            override_marker = "" if a.can_be_overridden else " (locked)"
            parts.append(
                f"- *{a.text}* — default `{a.default_value}`{override_marker}. "
                f"{a.rationale}"
            )
        parts.append("")
    if ev.missing_data:
        parts.append("**Missing data**")
        for m in ev.missing_data:
            parts.append(f"- {m}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_section_evidence_appendix(output: PlanSynthesisOutput) -> str:
    """Render the v4 ``Appendix — Section-by-section evidence`` block.

    The synthesizer's flat ``output.sections: list[Section]`` carries
    up to 20 entries (the full retirement-plan §1-§18 coverage, with
    some sections repeated across horizons). Each entry's ``body_md``
    is the prose surface the user wants to see; each entry's
    ``evidence`` sub-tree is the fact / citation / assumption /
    missing-data audit material — surfaced inside a collapsible
    ``<details>`` envelope so the appendix stays readable while still
    making the evidence reachable.

    Sections are sorted by ``(horizon, section_id)`` for a stable read
    order: horizon ascending (long → medium → short — long is first
    because the strategic frame anchors short-term tactics, not the
    other way around), section_id ascending within each horizon.

    Returns the empty string when ``output.sections`` is empty (legacy
    PlanSynthesisOutput rows produced before Phase 3 SectionEvidence
    landed) so callers can unconditionally append the result.
    """
    if not output.sections:
        return ""
    horizon_order = {"long": 0, "medium": 1, "short": 2}
    ordered = sorted(
        output.sections,
        key=lambda s: (horizon_order.get(s.horizon, 99), s.section_id),
    )
    lines = ["## Appendix — Section-by-section evidence", ""]
    lines.append(
        "Every section in the synthesizer's structured output, rendered "
        f"in full. {len(ordered)} sections across the three horizons. "
        "Each section's evidence sub-tree (facts, citations, "
        "assumptions, missing data) lives in a collapsible block "
        "underneath."
    )
    lines.append("")
    for s in ordered:
        lines.append(f"### {s.title} — `{s.section_id}` ({s.horizon})")
        lines.append("")
        lines.append(s.body_md.rstrip())
        lines.append("")
        evidence_body = _render_section_evidence_subtree(s)
        lines.append("<details>")
        lines.append("<summary>Evidence subtree</summary>")
        lines.append("")
        lines.append(evidence_body.rstrip())
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# v1 hard-coded ledger from ``tmp_review/plan_document_v4_spec.md`` §2/§3.
# v2 should derive these from agent outputs (each row already corresponds
# to a value some Phase-1 analyst computes). Punted on derivation for v1
# because the agent → assumption-row mapping is itself a non-trivial design
# pass (the values live in ``response_text`` blobs the renderer can't
# safely parse).
_ASSUMPTION_LEDGER_V1: list[dict[str, str]] = [
    {
        "id": "A1", "name": "Real return (high-equity post-deconcentration)",
        "value": "5.0% real", "source": "macro_analyst + plan_critique",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "FI threshold @ 6.82M; earliest-FI year",
    },
    {
        "id": "A2", "name": "Real return (conservative bond-tilted mix)",
        "value": "2.4% real", "source": "withdrawal_sequencer baseline",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Bare-FI threshold @ 14.21M; deterministic earliest year",
    },
    {
        "id": "A3", "name": "Capital-preservation required yield",
        "value": "1.32% real", "source": "fund_manager risk floor",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Cushion target @ 25.83M; late-2034 year",
    },
    {
        "id": "A4", "name": "Inflation (IL CPI, long-run)",
        "value": "2.5%/yr nominal", "source": "macro_analyst",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Nominal-to-real conversion across all horizons",
    },
    {
        "id": "A5", "name": "FX USD/NIS spot (planning anchor)",
        "value": "3.45 NIS/USD", "source": "fx_analyst (pinned 2026-06)",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "USD-denominated holdings translation; RSU net",
    },
    {
        "id": "A6", "name": "FX USD/NIS planning band (low → high)",
        "value": "3.20 → 3.80", "source": "fx_analyst range",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "FX strategy block; RSU stream sensitivity",
    },
    {
        "id": "A7", "name": "RSU net retention (after IL Section 102 + US)",
        "value": "47%", "source": "tax_analyst + equity_comp_analyst",
        "year": "2026", "confidence": "MEDIUM-HIGH",
        "affects": "Active-grant net stream; ₪500k/yr line",
    },
    {
        "id": "A8", "name": "RSU continuation level (steady-state)",
        "value": "₪500k/yr net", "source": "user S2 + equity_comp_analyst",
        "year": "2026-2029", "confidence": "MEDIUM-HIGH",
        "affects": "Working-years savings; FI bridge",
    },
    {
        "id": "A9", "name": "NVIDIA refresh-grant cut scenario",
        "value": "90% → 55% of base from 2026",
        "source": "web (news_analyst flagged)",
        "year": "2026+", "confidence": "MEDIUM",
        "affects": "Post-2029 RSU stream; sensitivity scenario only",
    },
    {
        "id": "A10", "name": "NVDA cap (single-stock concentration)",
        "value": "20% of portfolio",
        "source": "concentration_analyst (1-yr delay tolerance)",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Deconcentration plan; ~$1.7M USD glidepath",
    },
    {
        "id": "A11", "name": "NVDA implied volatility (σ, lognormal)",
        "value": "35.48%/yr", "source": "fundamentals_analyst + web",
        "year": "2026", "confidence": "MEDIUM-HIGH",
        "affects": "Concentration cap derivation; loss formula",
    },
    {
        "id": "A12", "name": "Tracked household spend (T12 baseline)",
        "value": "₪277k/yr", "source": "household_budget_analyst",
        "year": "2025-06 → 2026-05", "confidence": "HIGH",
        "affects": "Phase-1 spend; baseline for all forward phases",
    },
    {
        "id": "A13", "name": "Phase-2 binding spend (active retire)",
        "value": "₪341k/yr",
        "source": "household_budget_analyst + life_events smoothing",
        "year": "2033-2055", "confidence": "MEDIUM-HIGH",
        "affects": "FI threshold; the number all targets bind against",
    },
    {
        "id": "A14", "name": "MC success threshold",
        "value": "90%", "source": "user S3 (industry default)",
        "year": "—", "confidence": "HIGH",
        "affects": "MC-safe FI year; cushion sizing",
    },
    {
        "id": "A15", "name": "Lump-spike liquidity bucket (2043-2050)",
        "value": "₪0.8-1.2M", "source": "life_events + household_budget",
        "year": "2043-2050", "confidence": "MEDIUM",
        "affects": "Sequence-risk insulation around weddings + car + home",
    },
]


def render_assumption_ledger_appendix() -> str:
    """Render the v1 ``Appendix — Assumption ledger`` table.

    Hard-coded 15-row table sourced from
    ``tmp_review/plan_document_v4_spec.md`` §2/§3 (real return, inflation,
    FX, tax retention, NVDA cap formula inputs, T12 spend, MC threshold,
    lump-spike liquidity bucket). Each row exposes:

      | ID | Assumption | Value | Source | Year/version | Confidence | Affects |

    The values come from the same Phase-1 agent outputs the synth
    consumes, but the v1 implementation hard-codes them — derivation
    from agent output JSON is queued behind the renderer-wins block.
    Inline comment near ``_ASSUMPTION_LEDGER_V1`` documents the schema-
    change suggestion: add an ``AssumptionLedger`` model to
    ``plan_synthesizer_types.py`` and have each Phase-1 analyst emit
    its own rows.
    """
    rows = _ASSUMPTION_LEDGER_V1
    lines = ["## Appendix — Assumption ledger", ""]
    lines.append(
        f"{len(rows)} canonical plan assumptions, sourced from the "
        "Phase-1 analyst fleet. Edit via agent overrides — these are "
        "the numbers every other section binds against."
    )
    lines.append("")
    lines.append(
        "| ID | Assumption | Value | Source | Year | Confidence | Affects |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|"
    )
    for r in rows:
        # Escape ``|`` in any value so the markdown table can't be
        # broken by a stray pipe inside a value or affects cell.
        def _esc(s: str) -> str:
            return s.replace("|", "\\|")

        lines.append(
            f"| {_esc(r['id'])} "
            f"| {_esc(r['name'])} "
            f"| {_esc(r['value'])} "
            f"| {_esc(r['source'])} "
            f"| {_esc(r['year'])} "
            f"| {_esc(r['confidence'])} "
            f"| {_esc(r['affects'])} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _truncate_key_finding(response_text: str, max_chars: int = 200) -> str:
    """Pick a short single-line summary from an AgentReport.response_text.

    Strategy (first hit wins):
      1. If the response parses as JSON and has a ``verdict`` /
         ``headline`` / ``summary`` / ``conclusion`` string field,
         return that (truncated).
      2. Otherwise take the first non-empty line, strip markdown
         leading characters (``#``, ``-``, ``*``, leading whitespace),
         truncate.
      3. Return ``"(empty)"`` if response_text is blank.

    Defensive — never raises. The fleet-receipts row is informational
    only; if parsing fails we degrade to the first-line heuristic.
    """
    if not response_text:
        return "(empty)"
    text = response_text.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("verdict", "headline", "summary", "conclusion", "tldr"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
    # Take first non-empty line.
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("#-*> ").strip()
        if line:
            if len(line) > max_chars:
                return line[: max_chars - 1].rstrip() + "…"
            return line
    return "(empty)"


def _humanize_size(num_chars: int) -> str:
    """Compact size string: 1234 → ``1.2 KB``."""
    if num_chars < 1024:
        return f"{num_chars} B"
    return f"{num_chars / 1024:.1f} KB"


def render_fleet_receipts_appendix(
    session: Session, *, decision_run_id: int
) -> str:
    """Render the v4 ``Appendix — Fleet receipts`` block.

    Lists every ``agent_reports`` row tied to the synthesis run as
    ``decision_id='plan-synth-<decision_run_id>'`` (the audit token
    stamped by ``run_synthesis``). One row per agent with role, output
    size, model, tokens in/out, cost USD, and a truncated key finding.

    The fleet-receipts table makes the "26 agents ran here" reality
    visible to the user. Without it, the user-facing markdown
    completely hides the work that produced it.

    Returns the empty string when no rows exist (e.g. a test fixture
    with no DB-side trail) so callers can unconditionally append.
    """
    # Local import — avoids pulling SQLAlchemy into the module's
    # import-time graph (render.py is imported from contexts that
    # never need DB access, like the fixture-driven gate tests).
    from sqlalchemy import select

    from argosy.state.models import AgentReport

    decision_id = f"plan-synth-{decision_run_id}"
    rows = session.execute(
        select(AgentReport)
        .where(AgentReport.decision_id == decision_id)
        .order_by(AgentReport.id.asc())
    ).scalars().all()
    if not rows:
        return ""

    lines = ["## Appendix — Fleet receipts", ""]
    total_cost = sum(float(r.cost_usd or 0) for r in rows)
    total_tokens_in = sum(int(r.tokens_in or 0) for r in rows)
    total_tokens_out = sum(int(r.tokens_out or 0) for r in rows)
    lines.append(
        f"{len(rows)} agent invocations against this synthesis run. "
        f"Aggregate: {total_tokens_in:,} tokens in / "
        f"{total_tokens_out:,} tokens out / ${total_cost:.2f} total cost."
    )
    lines.append("")
    lines.append(
        "| # | Role | Output | Model | Tokens in | Tokens out "
        "| Cost USD | Key finding |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|"
    )
    for i, r in enumerate(rows, start=1):
        size = _humanize_size(len(r.response_text or ""))
        finding = _truncate_key_finding(r.response_text or "")
        # Escape pipes inside the finding so it can't break the table.
        finding_safe = finding.replace("|", "\\|").replace("\n", " ")
        model = (r.model or "—").replace("|", "\\|")
        cost = float(r.cost_usd or 0)
        lines.append(
            f"| {i} "
            f"| `{r.agent_role}` "
            f"| {size} "
            f"| {model} "
            f"| {int(r.tokens_in or 0):,} "
            f"| {int(r.tokens_out or 0):,} "
            f"| ${cost:.4f} "
            f"| {finding_safe} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_plan_appendices(
    output: PlanSynthesisOutput,
    *,
    session: Session | None = None,
    decision_run_id: int | None = None,
) -> str:
    """Assemble the three v4 appendices into one markdown block.

    Order: assumption ledger → section-by-section evidence → fleet
    receipts. The ledger goes first because its values anchor every
    section's body_md; the evidence block second because that's the
    "read more" surface; receipts last because they're forensic-only.

    Append-friendly: the result is empty-string-safe if every input is
    empty/missing. ``session`` + ``decision_run_id`` may both be None
    when called from a context without DB access (tests, dry-run
    renders); in that case the fleet-receipts block is skipped.

    Schema-change suggestion (v1 default): the appendices are appended
    to ``plan_versions.horizon_long_md`` rather than a new
    ``plan_versions.plan_doc_md`` column because (a) the v1 schema has
    no such column, (b) horizon_long_md is the closest existing
    surface to "the integrated plan doc", and (c) the appendix is
    global (not per-horizon) so duplicating it across long/medium/
    short would be wrong. If/when block B2+ needs a fourth column the
    migration is small and isolated.
    """
    parts: list[str] = []
    ledger = render_assumption_ledger_appendix()
    if ledger:
        parts.append(ledger)
    sections = render_section_evidence_appendix(output)
    if sections:
        parts.append(sections)
    if session is not None and decision_run_id is not None:
        receipts = render_fleet_receipts_appendix(
            session, decision_run_id=decision_run_id,
        )
        if receipts:
            parts.append(receipts)
    return "\n".join(parts).rstrip() + "\n" if parts else ""
