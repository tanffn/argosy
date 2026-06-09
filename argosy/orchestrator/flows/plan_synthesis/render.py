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


def _fmt_target_value(t) -> str:
    """Human-readable ``value + unit`` for a SynthTarget.

    Renders rates as ``3%`` (unit 'pct'), allocation weights as ``15% of net
    worth``, true multiples as ``2.5×`` (unit 'ratio'), and currency/time/share
    units sensibly — so a rate never shows as the raw "3.0 ratio" (codex
    residual). Unknown units fall back to the raw ``value unit`` form.
    """
    v = t.value
    u = t.unit
    if u == "pct":
        return f"{v:g}%"
    if u in ("pct_of_portfolio", "pct_of_net_worth", "pct_of_liquid"):
        scope = {
            "pct_of_portfolio": "of portfolio",
            "pct_of_net_worth": "of net worth",
            "pct_of_liquid": "of liquid",
        }[u]
        return f"{v:g}% {scope}"
    if u == "ratio":
        return f"{v:g}×"
    if u == "nis":
        return f"₪{v:,.0f}"
    if u == "usd":
        return f"${v:,.0f}"
    if u == "shares":
        return f"{v:g} sh"
    if u in ("years", "months", "days"):
        return f"{v:g} {u}"
    return f"{v} {u}"


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
            lines.append(f"- **{t.label}**: {_fmt_target_value(t)}{suffix}")
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
                f"- **{t.label}**: {_fmt_target_value(t)} "
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
        "id": "A1", "name": "Expected real return (trajectory growth only)",
        "value": "5.0% real", "source": "macro_analyst + plan_critique",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Trajectory growth + earliest-feasible-FI year (does NOT size the FI target)",
    },
    {
        "id": "A2", "name": "Perpetual real after-tax SWR (FI target sizing)",
        "value": "3.0% real", "source": "fi_methodology (codex-reviewed; band 2.4-3.5%)",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "FI perpetuity ₪10.39M = permanent-equivalent spend ₪311,584 / 3.0%",
    },
    {
        "id": "A3", "name": "FI total capital target (perpetuity + reserve)",
        "value": "₪11.84M", "source": "fi_methodology",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Full capital sufficiency = perpetuity ₪10.39M + finite-liability reserve ₪1.45M",
    },
    {
        "id": "A4", "name": "Inflation (IL CPI, long-run)",
        "value": "2.5%/yr nominal", "source": "macro_analyst",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "Nominal-to-real conversion across all horizons",
    },
    {
        # Value/source are placeholders — overwritten from the BOI-resolved
        # fx.usd_nis in _ledger_rows_with_manifest. NEVER a hardcoded rate: a
        # cold cache renders [derivation pending], not a stale 3.45 (codex FX
        # final review).
        "id": "A5", "name": "FX USD/NIS spot (planning anchor)",
        "value": "[derivation pending]", "source": "Bank of Israel daily representative rate",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "USD-denominated holdings translation; RSU net",
    },
    {
        "id": "A6", "name": "FX USD/NIS planning band (low → high)",
        "value": "[derivation pending]", "source": "BOI USD/NIS 90-day low → high",
        "year": "2026", "confidence": "MEDIUM",
        "affects": "FX strategy block; RSU stream sensitivity",
    },
    {
        "id": "A7", "name": "RSU net retention (after IL Section 102 + US)",
        "value": "47%", "source": "tax_analyst + equity_comp_analyst",
        "year": "2026", "confidence": "MEDIUM-HIGH",
        "affects": "Active-grant net stream; the ₪307,852/yr net savings floor (A8)",
    },
    {
        "id": "A8", "name": "RSU net savings (conservative known-grants floor)",
        "value": "₪307,852/yr net", "source": "equity_comp_analyst (known_grants_only)",
        "year": "2026-2029", "confidence": "MEDIUM-HIGH",
        "affects": "Working-years savings; FI bridge (conservative floor, not optimistic steady-state)",
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
        "value": "13% of portfolio",
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
        "id": "A13", "name": "Phase-2 active-retirement spend (stress only)",
        "value": "₪341k/yr",
        "source": "household_budget_analyst + life_events smoothing",
        "year": "2033-2055", "confidence": "MEDIUM-HIGH",
        "affects": "Conservative FI stress check (₪341k/3.0% ≈ ₪11.4M); the BINDING FI basis is the permanent-equivalent ₪311,584 (A2), NOT this peak",
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


def _ledger_rows_with_manifest(resolved) -> list[dict[str, str]]:
    """Return the ledger rows with FI/cap/savings values overridden from the
    resolver manifest when available — so the ledger can't drift from the
    deterministic single source of truth (the stale-FI-threshold defect codex
    caught). Falls back to the methodology-consistent static values when a key
    is pending / no manifest is supplied.
    """
    import copy

    rows = copy.deepcopy(_ASSUMPTION_LEDGER_V1)
    if resolved is None:
        return rows

    def _rv(key: str):
        v = resolved.get(key)
        return v.value if (v is not None and v.status == "resolved" and v.value is not None) else None

    def _nis(x: float) -> str:
        return f"₪{x/1e6:.2f}M" if abs(x) >= 1e6 else f"₪{x:,.0f}"

    by_id = {r["id"]: r for r in rows}
    swr = _rv("retirement.required_real_yield_pct")
    perp = _rv("retirement.fi_target_nis")
    total = _rv("retirement.fi_total_capital_nis")
    reserve = _rv("retirement.liquidity_reserve_nis")
    spend = _rv("spend.fi_basis_nis")
    ret = _rv("retirement.return_assumption_pct")
    cap = _rv("concentration.nvda_cap_pct")
    savings = _rv("savings.annual_net_nis")
    t12 = _rv("spend.annual_t12_nis")
    fx_spot = _rv("fx.usd_nis")
    fx_lo = _rv("fx.usd_nis_band_low")
    fx_hi = _rv("fx.usd_nis_band_high")

    if ret is not None and "A1" in by_id:
        by_id["A1"]["value"] = f"{ret*100:.1f}% real"
    if swr is not None and perp is not None and spend is not None and "A2" in by_id:
        by_id["A2"]["value"] = f"{swr*100:.1f}% real"
        by_id["A2"]["affects"] = (
            f"FI perpetuity {_nis(perp)} = permanent-equivalent spend "
            f"{_nis(spend)} / {swr*100:.1f}%"
        )
    if total is not None and perp is not None and reserve is not None and "A3" in by_id:
        by_id["A3"]["value"] = _nis(total)
        by_id["A3"]["affects"] = (
            f"Full capital sufficiency = perpetuity {_nis(perp)} + "
            f"finite-liability reserve {_nis(reserve)}"
        )
    if savings is not None and "A8" in by_id:
        by_id["A8"]["value"] = f"{_nis(savings)}/yr net"
    if cap is not None and "A10" in by_id:
        by_id["A10"]["value"] = f"{cap*100:.0f}% of portfolio"
    if t12 is not None and "A12" in by_id:
        by_id["A12"]["value"] = f"{_nis(t12)}/yr"
    # FX rows — derive from BOI (kills the hardcoded 3.45 / 3.20-3.80 that
    # contradicted the rate the agents actually computed at).
    if fx_spot is not None and "A5" in by_id:
        by_id["A5"]["value"] = f"{fx_spot:.3f} NIS/USD"
        by_id["A5"]["source"] = "Bank of Israel daily representative rate"
    if fx_lo is not None and fx_hi is not None and "A6" in by_id:
        by_id["A6"]["value"] = f"{fx_lo:.2f} → {fx_hi:.2f}"
        by_id["A6"]["source"] = "BOI USD/NIS 90-day low → high"
    return rows


def render_assumption_ledger_appendix(resolved=None) -> str:
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
    rows = _ledger_rows_with_manifest(resolved)
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
        else:
            # Structured output with no summary-style key (e.g. withdrawal_sequencer,
            # whose fields are fi_bridge / withdrawal_schedule / fi_base / …). Do NOT
            # fall through to line-1 of the raw JSON — that yields a bare "{". Build a
            # compact finding from the object's scalar fields, else its field names.
            # Generic, so it fixes EVERY structured-output agent at once (no per-symptom).
            scalars = [
                f"{k}: {v}"
                for k, v in parsed.items()
                if isinstance(v, (str, int, float, bool)) and str(v).strip()
            ]
            text = (
                "; ".join(scalars[:3])
                if scalars
                else f"(structured output: {', '.join(list(parsed)[:6])})"
            )
    elif isinstance(parsed, list) and parsed:
        text = f"(structured output — {len(parsed)} items)"
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


def _pending_label() -> str:
    """The single literal the renderer emits for any un-derived figure."""
    return "[derivation pending]"


def _fmt_nis_m(rv) -> str:
    """Format a resolver NIS value in ₪M, or the pending label."""
    if rv is None or rv.status != "resolved" or rv.value is None:
        return _pending_label()
    return f"₪{rv.value / 1_000_000.0:.2f}M"


def _fmt_nis(rv) -> str:
    """Format a resolver NIS value as ``₪123,456``, or the pending label."""
    if rv is None or rv.status != "resolved" or rv.value is None:
        return _pending_label()
    return f"₪{rv.value:,.0f}"


def render_trajectory_reconciliation_appendix(
    *,
    session: "Session | None" = None,
    user_id: str = "ariel",
    decision_run_id: int | None = None,
) -> str:
    """Render the "How we get from today → target" reconciliation block.

    This block answers two questions the user asked explicitly:
      1. How does the plan explain getting from today's portfolio to the
         derived FI target?
      2. Why does the UI show one retire-age while the plan targets the
         derived FI age?

    Both are math/assumption questions. Every headline figure here is
    pulled from :func:`resolve_plan_numbers` — the shared deterministic
    resolver the synth, renderer, and UI all read — so NOTHING is a
    hardcoded constant. The renderer keeps only the deterministic FV math
    (``fv()``) and the snapshot-derived starting point; each input number
    traces to a resolver key + source_locator. Any input that resolves to
    ``pending`` renders ``[derivation pending]`` and the dependent
    trajectory rows are annotated/skipped rather than fabricated.

    Returns ``""`` when no snapshot exists, when the DB session is missing
    (dry-run tests), or when ``decision_run_id`` is absent (no run to
    resolve agent-derived numbers against).
    """
    if session is None or decision_run_id is None:
        return ""

    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

    try:
        resolved = resolve_plan_numbers(
            session, user_id=user_id, decision_run_id=decision_run_id
        )
    except Exception:  # pragma: no cover - resolver is itself defensive
        return ""

    # All headline values come from the resolver — single source of truth.
    nw = resolved.get("portfolio.net_worth_nis")
    fi_target = resolved.get("retirement.fi_target_nis")
    fi_total = resolved.get("retirement.fi_total_capital_nis")
    fi_reserve = resolved.get("retirement.liquidity_reserve_nis")
    fi_age = resolved.get("retirement.fi_age")
    req_yield = resolved.get("retirement.required_real_yield_pct")
    ret_assumption = resolved.get("retirement.return_assumption_pct")
    fi_spend = resolved.get("spend.fi_basis_nis")
    savings = resolved.get("savings.annual_net_nis")
    t12_spend = resolved.get("spend.annual_t12_nis")

    # Starting point: the snapshot-derived net worth, in ₪M. When net
    # worth is pending we cannot draw any trajectory at all.
    if nw.status != "resolved" or nw.value is None:
        return ""
    a0_nis_m = nw.value / 1_000_000.0  # portfolio in millions of NIS

    # FV at r real, annuity contribution C, starting P0, over n yrs:
    #   FV(n) = P0·(1+r)^n + C·((1+r)^n - 1)/r
    def fv(p0: float, c: float, r: float, n: float) -> float:
        if r == 0:
            return p0 + c * n
        gr = (1.0 + r) ** n
        return p0 * gr + c * (gr - 1.0) / r

    # Earliest n (years) to reach `target` from `p0` at `r` with annuity `c`.
    def years_to(p0: float, target: float, c: float, r: float) -> float | None:
        if p0 >= target:
            return 0.0
        if r == 0:
            return (target - p0) / c if c > 0 else None
        # n = ln((target + c/r) / (p0 + c/r)) / ln(1+r)
        import math
        num = target + c / r
        den = p0 + c / r
        if num <= 0 or den <= 0 or num <= den:
            return 0.0
        return math.log(num / den) / math.log(1 + r)

    # Derived inputs for the FV math. The contribution (annual savings)
    # and the FI target are the load-bearing values; both are resolver-
    # sourced. When savings is pending the forward trajectory cannot be
    # drawn (no annuity term) — we render the table header but mark every
    # forward row pending. The real-return assumption used to project is
    # the resolver's ``return_assumption_pct`` when present, else the
    # trajectory rows that depend on it are marked pending.
    savings_m: float | None = (
        savings.value / 1_000_000.0
        if savings.status == "resolved" and savings.value is not None
        else None
    )
    r_real: float | None = (
        ret_assumption.value
        if ret_assumption.status == "resolved" and ret_assumption.value is not None
        else None
    )
    fi_target_m: float | None = (
        fi_target.value / 1_000_000.0
        if fi_target.status == "resolved" and fi_target.value is not None
        else None
    )
    # Current age = derived FI age minus the years the FV math says it
    # takes to reach the FI target. Only computable when every input is
    # resolved; otherwise the per-year age annotation is suppressed.
    n_to_fi: float | None = None
    if (
        savings_m is not None
        and r_real is not None
        and fi_target_m is not None
    ):
        n_to_fi = years_to(a0_nis_m, fi_target_m, savings_m, r_real)
    current_age: float | None = None
    if (
        n_to_fi is not None
        and fi_age.status == "resolved"
        and fi_age.value is not None
    ):
        current_age = fi_age.value - n_to_fi

    base_year = 2026

    def year_of(n: float | None) -> str:
        if n is None:
            return _pending_label()
        if n <= 0:
            return "today"
        yr = base_year + int(round(n))
        if current_age is None:
            return f"{yr}"
        return f"{yr} (~age {int(round(current_age + n))})"

    lines: list[str] = []
    lines.append("## Appendix — Trajectory & Retirement-Age Reconciliation")
    lines.append("")
    fi_age_disp = (
        f"age {fi_age.value:.0f}"
        if fi_age.status == "resolved" and fi_age.value is not None
        else _pending_label()
    )
    lines.append(
        "This block answers two questions the plan must own: (1) how do "
        "we get from today's portfolio to the FI target, and (2) how the "
        "earliest math-feasible retire age relates to the plan's derived "
        f"FI age (**{fi_age_disp}**). Both are pure-math answers under the "
        "DERIVED assumptions. Every headline number below is pulled from "
        "the shared plan-numeric resolver (single source of truth across "
        "the synth, this appendix, and the UI) — no hardcoded constants, "
        "no LLM judgement. Any figure the fleet has not yet derived shows "
        f"as `{_pending_label()}`."
    )
    lines.append("")

    lines.append("### Where you are today")
    lines.append("")
    lines.append(
        f"- Current portfolio (net worth): **{_fmt_nis_m(nw)}** "
        f"(`portfolio.net_worth_nis` — {nw.source_locator})."
    )
    lines.append(
        f"- FI spend basis: **{_fmt_nis(fi_spend)}/yr** "
        f"(`spend.fi_basis_nis` — the spend the FI target funds)."
    )
    lines.append(
        f"- Tracked T12 household burn: **{_fmt_nis(t12_spend)}/yr** "
        f"(`spend.annual_t12_nis` — monthly_burn × 12)."
    )
    lines.append(
        f"- Net annual savings (RSU known-grants-only conservative floor): "
        f"**{_fmt_nis(savings)}/yr** "
        f"(`savings.annual_net_nis`)."
    )
    lines.append(
        f"- FI perpetuity base: **{_fmt_nis_m(fi_target)}** "
        f"(`retirement.fi_target_nis` — {fi_target.formula or fi_target.source_locator})."
    )
    lines.append(
        f"- FI total capital target: **{_fmt_nis_m(fi_total)}** = perpetuity "
        f"base + finite-liability reserve **{_fmt_nis_m(fi_reserve)}** "
        f"(`retirement.fi_total_capital_nis`). Full capital sufficiency is "
        f"this total, NOT the perpetuity base alone."
    )
    lines.append("")

    # Year-by-year trajectory. Requires savings + return assumption.
    r_disp = f"{r_real * 100:.1f}% real" if r_real is not None else _pending_label()
    lines.append(
        f"### Year-by-year trajectory (deterministic, r = {r_disp})"
    )
    lines.append("")
    lines.append("| Year | End-of-year balance (₪M, real) |")
    lines.append("|---|---|")
    if savings_m is not None and r_real is not None:
        for offset in range(0, 6):
            year_label = base_year + offset
            end_bal = fv(a0_nis_m, savings_m, r_real, offset)
            lines.append(f"| {year_label} | {end_bal:.2f} |")
        c_disp = _fmt_nis(savings)
        lines.append("")
        lines.append(
            f"Formula: `FV(n) = P0·(1+r)^n + C·((1+r)^n - 1)/r` with "
            f"P0 = {_fmt_nis_m(nw)}, C = {c_disp}/yr, r = {r_disp}. All "
            "inputs resolver-sourced."
        )
    else:
        # Cannot project without both savings (C) and the return rate (r).
        for offset in range(0, 6):
            lines.append(f"| {base_year + offset} | {_pending_label()} |")
        lines.append("")
        lines.append(
            f"Trajectory rows are `{_pending_label()}` because at least one "
            "FV input is unresolved: net annual savings "
            f"(`savings.annual_net_nis`, status={savings.status}) and/or the "
            "real-return assumption "
            f"(`retirement.return_assumption_pct`, status={ret_assumption.status})."
        )
    lines.append("")

    # Total-capital crossing (perpetuity + reserve) — the FULL sufficiency
    # bar, distinct from the perpetuity base. Net worth can clear the base
    # while still short of the total; say so honestly rather than "past FI".
    fi_total_m: float | None = (
        fi_total.value / 1_000_000.0
        if fi_total.status == "resolved" and fi_total.value is not None
        else None
    )
    n_to_total: float | None = None
    if savings_m is not None and r_real is not None and fi_total_m is not None:
        n_to_total = years_to(a0_nis_m, fi_total_m, savings_m, r_real)

    lines.append("### When does the portfolio cross the FI target?")
    lines.append("")
    lines.append("| Anchor | Threshold (₪M) | Crossed at | Source |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| FI perpetuity base | {_fmt_nis_m(fi_target).lstrip('₪')} | "
        f"{year_of(n_to_fi)} | `retirement.fi_target_nis` "
        f"({fi_target.source_locator}) |"
    )
    lines.append(
        f"| FI total capital (base + reserve) | "
        f"{_fmt_nis_m(fi_total).lstrip('₪')} | {year_of(n_to_total)} | "
        f"`retirement.fi_total_capital_nis` |"
    )
    lines.append("")
    # Honest reconciliation of which bar net worth currently clears.
    if (
        nw.status == "resolved" and nw.value is not None
        and fi_target.status == "resolved" and fi_target.value is not None
        and fi_total.status == "resolved" and fi_total.value is not None
    ):
        if nw.value >= fi_total.value:
            lines.append(
                "Net worth already covers the FULL total capital target "
                "(perpetuity base + reserve) — capital sufficiency reached."
            )
        elif nw.value >= fi_target.value:
            short = fi_total.value - nw.value
            lines.append(
                f"Net worth clears the PERPETUITY BASE today, but full "
                f"capital sufficiency (total {_fmt_nis_m(fi_total)}) is NOT "
                f"yet reached — short by **₪{short:,.0f}** "
                f"(the finite-liability reserve). This is not 'past FI'."
            )
        else:
            short = fi_target.value - nw.value
            lines.append(
                f"Net worth is below even the perpetuity base — short by "
                f"**₪{short:,.0f}** before any reserve."
            )
    lines.append("")

    lines.append("### How the math-feasible age relates to the plan's FI age")
    lines.append("")
    lines.append(
        "Different surfaces answer different questions; each is math-"
        "correct at its inputs. The numbers here are the resolver's "
        "derived values, not constants:"
    )
    lines.append("")
    lines.append(
        "| Quantity | Value | Source key |"
    )
    lines.append(
        "|---|---|---|"
    )
    lines.append(
        f"| Derived FI age | {fi_age_disp} | `retirement.fi_age` |"
    )
    req_disp = (
        f"{req_yield.value * 100:.2f}%"
        if req_yield.status == "resolved" and req_yield.value is not None
        else _pending_label()
    )
    ret_disp = (
        f"{ret_assumption.value * 100:.2f}%"
        if ret_assumption.status == "resolved" and ret_assumption.value is not None
        else _pending_label()
    )
    lines.append(
        f"| Required real yield (spend / target) | {req_disp} | "
        "`retirement.required_real_yield_pct` |"
    )
    lines.append(
        f"| Real-return assumption | {ret_disp} | "
        "`retirement.return_assumption_pct` |"
    )
    lines.append(
        f"| Earliest math-feasible year (FV) | {year_of(n_to_fi)} | "
        "derived: years_to(net_worth, fi_target, savings, r) |"
    )
    lines.append("")
    lines.append(
        "**Honest reconciliation**: the FI age is the earliest age at "
        "which the projected portfolio first reaches the derived FI "
        "target under the resolver's return assumption. It already "
        "encodes margin against return-rate uncertainty, sequence risk, "
        "NVDA concentration, and life-event spend spikes via the spend "
        "basis the withdrawal sequencer used — it is not a round "
        "marketing number."
    )
    return "\n".join(lines).rstrip() + "\n"


def render_number_derivations_appendix(
    *,
    session: "Session | None" = None,
    user_id: str = "ariel",
    decision_run_id: int | None = None,
    resolved=None,
) -> str:
    """Render the "show your work" appendix: every headline number built from
    its RAW inputs, step by step, each line sourced. No placeholders, no
    magic numbers — the audit trail behind the prose. Empty when neither the
    FI methodology nor the resolver manifest can be computed.
    """
    if session is None:
        return ""
    try:
        from argosy.services.fi_methodology import compute_fi_target
    except Exception:  # pragma: no cover
        return ""

    # Use the resolver's tracked-T12 as the spend basis when available so the
    # derivation matches the rest of the plan; else fall back to identity.
    t12 = None
    if resolved is not None:
        rv = resolved.get("spend.annual_t12_nis")
        if rv is not None and rv.status == "resolved" and rv.value:
            t12 = float(rv.value)
    try:
        m = compute_fi_target(session, user_id=user_id, spend_t12_nis=t12)
    except Exception:  # noqa: BLE001
        m = None
    if m is None:
        return ""

    def _n(x: float) -> str:
        return f"₪{x:,.0f}"

    lines: list[str] = ["## Appendix — Number Derivations (show your work)", ""]
    lines.append(
        "Every headline number is built from raw inputs below — no "
        "placeholders, no magic numbers. Each step cites its source so it can "
        "be re-derived and audited."
    )
    lines.append("")

    # --- FI spend basis: raw T12 rollup → permanent-equivalent. -------------
    lines.append(f"### FI spend basis — {_n(m.permanent_annual_spend_nis)}/yr (permanent-equivalent, real)")
    lines.append("")
    lines.append(
        f"**Step 1 — tracked T12 household burn: {_n(m.baseline_annual_nis)}/yr** "
        f"(source: `{m.baseline_source}`). Raw category rollup:"
    )
    lines.append("")
    if m.baseline_breakdown:
        lines.append("| Category | ₪/yr | share |")
        lines.append("|---|---:|---:|")
        for label, amt in m.baseline_breakdown:
            share = (amt / m.baseline_annual_nis * 100.0) if m.baseline_annual_nis else 0.0
            lines.append(f"| {label} | {amt:,.0f} | {share:.1f}% |")
        lines.append(f"| **Total tracked T12** | **{m.baseline_annual_nis:,.0f}** | 100% |")
    else:
        lines.append("| (raw category breakdown not available in identity_yaml) |")
    lines.append("")
    lines.append(
        f"**Step 2 — lift to permanent-equivalent: {_n(m.permanent_annual_spend_nis)}/yr** "
        "(smooths amortized life-event phases into a perpetual-equivalent spend):"
    )
    lines.append("")
    lines.append("| Component | ₪/yr | source | confidence |")
    lines.append("|---|---:|---|---|")
    for c in m.components:
        if c.kind != "permanent":
            continue
        lines.append(f"| {c.label} | {c.annual_nis:+,.0f} | {c.source} | {c.confidence} |")
    lines.append(f"| **Permanent-equivalent total** | **{m.permanent_annual_spend_nis:,.0f}** | | |")
    lines.append("")

    # --- FI capital target: perpetuity + reserve. ---------------------------
    lines.append(f"### FI capital target — {_n(m.fi_perpetuity_nis)} perpetuity base")
    lines.append("")
    lines.append(
        f"- **Perpetuity base** = permanent spend {_n(m.permanent_annual_spend_nis)} ÷ "
        f"{m.swr_real_pct*100:.1f}% real after-tax perpetual SWR = **{_n(m.fi_perpetuity_nis)}** "
        f"(SWR band {m.swr_band[0]*100:.1f}–{m.swr_band[1]*100:.1f}%; "
        f"the {m.return_assumption_real_pct*100:.1f}% expected return is trajectory-only, NOT used to size the target)."
    )
    finite = [c for c in m.components if c.kind == "finite"]
    if finite:
        lines.append(
            f"- **Liquidity reserve** = {_n(m.finite_liability_reserve_nis)} of finite "
            "liabilities, held SEPARATELY (not capitalized into the perpetuity):"
        )
        lines.append("")
        lines.append("| Finite liability | ₪ | source | confidence |")
        lines.append("|---|---:|---|---|")
        for c in finite:
            lines.append(f"| {c.label} | {c.reserve_nis:,.0f} | {c.source} | {c.confidence} |")
        lines.append(f"| **Reserve total** | **{m.finite_liability_reserve_nis:,.0f}** | | |")
    lines.append("")
    lines.append(
        f"- **FI total capital** = perpetuity {_n(m.fi_perpetuity_nis)} + reserve "
        f"{_n(m.finite_liability_reserve_nis)} = **{_n(m.fi_total_capital_nis)}**."
    )
    lines.append("")

    # --- FIRE bridge: retirement → first pension unlock (age 60). -----------
    # Derived here from the permanent-equivalent spend (NOT the lower tracked
    # T12 burn) so the plan never states a fabricated bridge figure.
    ret_rv = resolved.get("retirement.fi_age") if resolved is not None else None
    if ret_rv is not None and ret_rv.status == "resolved" and ret_rv.value is not None:
        from argosy.services.cashflow_projection import LUMP_PENSION_AGE
        ret_age = float(ret_rv.value)
        bridge_years = max(0.0, float(LUMP_PENSION_AGE) - ret_age)
        bridge_nis = bridge_years * m.permanent_annual_spend_nis
        lines.append(
            f"### FIRE bridge — {_n(bridge_nis)} liquid drawdown "
            f"(age {ret_age:.0f}→{LUMP_PENSION_AGE})"
        )
        lines.append("")
        lines.append(
            f"- **Bridge requirement** = ({LUMP_PENSION_AGE} − {ret_age:.0f}) "
            f"= {bridge_years:.0f} yrs × permanent-equivalent spend "
            f"{_n(m.permanent_annual_spend_nis)}/yr = **{_n(bridge_nis)}** — the liquid "
            "capital that must fund spend BEFORE the age-60 partial pension unlock. "
            "Sized on the permanent-equivalent basis, not the lower tracked T12 burn."
        )
        lines.append("")

    # --- Currency-mismatch / FX risk (assets ~USD, spend NIS). --------------
    # The household holds USD assets but spends in NIS, so a strengthening
    # shekel erodes NIS purchasing power. Surface the split, sensitivity,
    # break-even FX, and a scenario band rather than burying FX in one anchor.
    fx_rv = resolved.get("fx.usd_nis") if resolved is not None else None
    fx_spot = (
        float(fx_rv.value)
        if (fx_rv is not None and fx_rv.status == "resolved" and fx_rv.value is not None)
        else None
    )
    usd_assets_usd = nis_assets_nis = None
    holdings_as_of = None
    if session is not None and fx_spot:
        try:
            from sqlalchemy import select
            from argosy.state.models import PortfolioSnapshotRow
            snap = session.execute(
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.user_id == user_id)
                .order_by(PortfolioSnapshotRow.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if snap is not None:
                holdings_as_of = getattr(snap, "snapshot_date", None)
                positions = json.loads(snap.positions_json or "[]")
                snap_fx = float(snap.fx_usd_nis or fx_spot)
                usd_assets_usd = sum(
                    float(p.get("usd_value_k") or 0.0) * 1000.0
                    for p in positions if (p.get("currency") or "").upper() == "USD"
                )
                # NIS-origin positions: recover native NIS (don't re-translate
                # as USD exposure — codex FX review).
                nis_assets_nis = sum(
                    float(p.get("usd_value_k") or 0.0) * 1000.0 * snap_fx
                    for p in positions if (p.get("currency") or "").upper() != "USD"
                )
        except Exception:  # noqa: BLE001 — FX-risk block is best-effort
            usd_assets_usd = None

    if fx_spot and usd_assets_usd:
        nw_now = usd_assets_usd * fx_spot + (nis_assets_nis or 0.0)
        total_assets = usd_assets_usd * fx_spot + (nis_assets_nis or 0.0)
        usd_share = (usd_assets_usd * fx_spot) / total_assets if total_assets else 0.0
        sens_per_010 = usd_assets_usd * 0.10  # ₪ per 0.10 USD/NIS move
        fi_total = m.fi_total_capital_nis
        break_even = (fi_total - (nis_assets_nis or 0.0)) / usd_assets_usd if usd_assets_usd else 0.0
        lines.append("### Currency-mismatch / FX risk")
        lines.append("")
        as_of_txt = holdings_as_of.isoformat() if holdings_as_of else "latest snapshot"
        lines.append(
            f"Assets are **{usd_share*100:.0f}% USD / {(1-usd_share)*100:.0f}% NIS** but spend is "
            f"100% NIS — a strengthening shekel erodes NIS purchasing power. FX = "
            f"**{fx_spot:.3f}** (BOI); holdings as of **{as_of_txt}** (provisional — refresh "
            "holdings before a GO/NO-GO)."
        )
        lines.append("")
        lines.append(
            f"- **Net worth @ current FX** = ${usd_assets_usd/1e6:.2f}M USD × {fx_spot:.3f} "
            f"+ {_n(nis_assets_nis or 0.0)} NIS = **{_n(nw_now)}** (vs FI total {_n(fi_total)} → "
            f"gap {_n(nw_now - fi_total)})."
        )
        lines.append(
            f"- **FX sensitivity**: every 0.10 move in USD/NIS = **{_n(sens_per_010)}** of net worth."
        )
        lines.append(
            f"- **Break-even FX** to reach the {_n(fi_total)} total target on current holdings: "
            f"**{break_even:.3f} USD/NIS**."
        )
        lines.append("")
        lines.append("| FX scenario | USD/NIS | Net worth | FI gap |")
        lines.append("|---|---:|---:|---:|")
        for label, mult in (("−10% (shekel strengthens)", 0.90), ("base", 1.0), ("+10% (shekel weakens)", 1.10)):
            fx_s = fx_spot * mult
            nw_s = usd_assets_usd * fx_s + (nis_assets_nis or 0.0)
            lines.append(f"| {label} | {fx_s:.3f} | {_n(nw_s)} | {_n(nw_s - fi_total)} |")
        lines.append("")

    # --- Other headline numbers from the resolver (formula + source). -------
    if resolved is not None:
        rows: list[tuple[str, str]] = []
        for key, label in (
            ("portfolio.net_worth_nis", "Net worth"),
            ("savings.annual_net_nis", "Annual net savings"),
            ("concentration.nvda_cap_pct", "NVDA concentration cap"),
            ("concentration.nvda_current_pct", "NVDA current weight"),
            ("retirement.fi_age", "Full-FI / perpetuity target age"),
        ):
            rv = resolved.get(key)
            if rv is None or rv.status != "resolved" or rv.value is None:
                continue
            if rv.unit == "nis":
                val = _n(float(rv.value))
            elif rv.unit == "pct":
                val = f"{float(rv.value)*100:.1f}%"
            elif rv.unit == "age":
                val = f"age {float(rv.value):.1f}"
            else:
                val = f"{rv.value}"
            formula = rv.formula or rv.source_locator
            rows.append((label, f"{val} — {formula} (source: `{rv.source_locator}`; conf {rv.confidence})"))
        if rows:
            lines.append("### Other headline numbers")
            lines.append("")
            for label, body in rows:
                lines.append(f"- **{label}**: {body}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_plan_coverage_appendix(
    session: Session, *, decision_run_id: int
) -> str:
    """Render the ``Appendix — Coverage self-assessment`` block (T3.5).

    The PlanCoverageAnalyst persists a ``plan_coverage`` agent report saying
    which canonical sections it could NOT baseline (``unfilled_section_ids``)
    and what data is still missing inside the sections it did baseline
    (per-section ``missing_data``). That output was previously persisted but
    never surfaced — this renders it so the user sees where the plan is thin
    (the gap self-assessment), in line with the trust contract.

    Returns the empty string when no coverage report exists for the run or it
    has nothing to flag, so callers can append unconditionally.
    """
    from sqlalchemy import select

    from argosy.agents.plan_coverage_analyst import PlanCoverageOutput
    from argosy.state.models import AgentReport

    decision_id = f"plan-synth-{decision_run_id}"
    row = session.execute(
        select(AgentReport)
        .where(AgentReport.decision_id == decision_id)
        .where(AgentReport.agent_role == "plan_coverage")
        .order_by(AgentReport.id.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return ""
    try:
        out = PlanCoverageOutput.model_validate_json(row.response_text or "{}")
    except Exception:  # noqa: BLE001 — never break the plan render on a bad blob
        return ""

    unfilled = list(out.unfilled_section_ids or [])
    missing_by_section: list[tuple[str, str, list[str]]] = []
    for s in out.baseline_sections or []:
        md = list(getattr(getattr(s, "evidence", None), "missing_data", None) or [])
        if md:
            missing_by_section.append((s.section_id, s.title, md))
    if not unfilled and not missing_by_section:
        return ""

    conf = getattr(out.confidence, "value", out.confidence)
    lines = ["## Appendix — Coverage self-assessment", ""]
    lines.append(
        "Where the plan is thin, per Argosy's own coverage analyst "
        f"(confidence: {conf}). These are open items, not asserted facts."
    )
    lines.append("")
    if unfilled:
        lines.append(
            "**Sections not yet baselined** (need your input or a defensible "
            "default before they carry weight):"
        )
        lines.append("")
        for sid in unfilled:
            lines.append(f"- `{sid}`")
        lines.append("")
    if missing_by_section:
        lines.append("**Known data gaps inside covered sections:**")
        lines.append("")
        for sid, title, md in missing_by_section:
            lines.append(f"- **{title}** (`{sid}`): {'; '.join(md)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_plan_appendices(
    output: PlanSynthesisOutput,
    *,
    session: Session | None = None,
    decision_run_id: int | None = None,
    user_id: str = "ariel",
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
    # Trajectory & retire-age reconciliation FIRST — answers the two
    # questions the user asks before drilling into anything else.
    trajectory = render_trajectory_reconciliation_appendix(
        session=session, user_id=user_id, decision_run_id=decision_run_id,
    )
    if trajectory:
        parts.append(trajectory)
    # Source the ledger's FI/cap/savings rows from the resolver manifest so
    # they can't drift from the deterministic single source of truth.
    _resolved = None
    if session is not None and decision_run_id is not None:
        try:
            from argosy.services.plan_numeric_resolver import resolve_plan_numbers
            _resolved = resolve_plan_numbers(
                session, user_id=user_id, decision_run_id=decision_run_id,
            )
        except Exception:  # noqa: BLE001 — ledger falls back to static values
            _resolved = None
    ledger = render_assumption_ledger_appendix(_resolved)
    if ledger:
        parts.append(ledger)
    derivations = render_number_derivations_appendix(
        session=session, user_id=user_id, decision_run_id=decision_run_id,
        resolved=_resolved,
    )
    if derivations:
        parts.append(derivations)
    sections = render_section_evidence_appendix(output)
    if sections:
        parts.append(sections)
    # Coverage self-assessment (T3.5) — surface where the plan is thin before
    # the forensic receipts. User-relevant, so it precedes the receipts block.
    if session is not None and decision_run_id is not None:
        coverage = render_plan_coverage_appendix(
            session, decision_run_id=decision_run_id,
        )
        if coverage:
            parts.append(coverage)
        receipts = render_fleet_receipts_appendix(
            session, decision_run_id=decision_run_id,
        )
        if receipts:
            parts.append(receipts)
    return "\n".join(parts).rstrip() + "\n" if parts else ""
