"""Markdown export — one-pager snapshot of the user's plan + wealth dashboard.

Produces a single, downloadable markdown document the user can save / print /
share. Pulls together five live sources:

  * Current plan (pending draft, else accepted plan, else baseline).
  * Wealth dashboard (compute_wealth_dashboard).
  * Action items (dated short/medium horizon actions within ``window_days``).
  * FM objections (latest fund_manager agent_report for the pending draft).
  * Last synthesis run + codex second-opinion presence + fleet self-review counts.

Every section degrades gracefully — when a source is missing the section emits
a clearly-marked fallback line instead of vanishing. The caller (route layer)
just hands back the body and a ``Content-Disposition`` header.

Markdown only by design: no PDF generation here. Downstream tools (pandoc,
browser print-to-PDF) handle that.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.services.fact_token_render import render_plan_facts
from argosy.services.wealth_dashboard import (
    WealthDashboard,
    compute_wealth_dashboard,
)
from argosy.state.models import (
    AgentReport,
    FleetSelfReviewReport,
    PlanVersion,
)
from argosy.state.queries import get_current_plan, get_pending_draft


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_nis(value: float | None) -> str:
    """Format a NIS amount as e.g. ``11.17M NIS``, ``23.1K NIS`` or ``—``."""
    if value is None:
        return "—"
    abs_v = abs(value)
    if abs_v >= 1_000_000:
        return f"{value / 1_000_000:.2f}M NIS"
    if abs_v >= 1_000:
        return f"{value / 1_000:.1f}K NIS"
    return f"{value:.0f} NIS"


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    abs_v = abs(value)
    if abs_v >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def _fmt_pct(value: float | None, *, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


_QUICK_REF_RE = re.compile(
    # Heading shape: ``#{1,6} Quick Reference[: ...]\n``. ``[ \t]*`` instead
    # of ``\s*`` after the keyword so the regex engine can't greedily eat
    # the blank line + first bullet of the section body before claiming the
    # heading is over.
    r"(?im)^[ \t]*#{1,6}[ \t]*(quick[ \t]*reference|quick-ref)[ \t]*:?[^\n]*\n"
    r"([\s\S]*?)(?=^[ \t]*#{1,6}[ \t]|\Z)"
)


def _extract_quick_reference(markdown: str) -> str | None:
    """Find a ``## Quick Reference`` (or similar) section in plan markdown.

    Returns just the body (everything until the next heading) trimmed,
    or None when no such section is present.
    """
    if not markdown:
        return None
    m = _QUICK_REF_RE.search(markdown)
    if not m:
        return None
    body = m.group(2).strip()
    return body or None


def _format_action_items(items: list[Any]) -> list[str]:
    """Bullet lines for the Action Items section. ``items`` is a list of
    ``ActionItem`` pydantic models (or dataclasses with the same fields)."""
    lines: list[str] = []
    for it in items:
        dated = getattr(it, "dated", None)
        label = getattr(it, "label", "") or ""
        status = getattr(it, "status", "") or ""
        detail = getattr(it, "detail", "") or ""
        days = getattr(it, "days_until", None)
        dated_str = dated.isoformat() if isinstance(dated, date) else "?"
        days_str = ""
        if isinstance(days, int):
            if days < 0:
                days_str = f" (overdue by {-days}d)"
            elif days == 0:
                days_str = " (today)"
            else:
                days_str = f" (in {days}d)"
        bullet = f"- **{dated_str}** [{status}] {label}{days_str}"
        if detail:
            bullet += f" — {detail}"
        lines.append(bullet)
    return lines


def _resolve_plan(
    db: Session, user_id: str,
) -> tuple[PlanVersion | None, str]:
    """Pick the plan to export and a short status label.

    Preference: pending draft > accepted plan > baseline (most recent
    plan_version). Returns ``(plan_version, status_label)``.
    """
    draft = get_pending_draft(db, user_id)
    if draft is not None:
        return draft, "Pending draft (Fund Manager review)"
    current = get_current_plan(db, user_id)
    if current is not None:
        return current, "Accepted (current)"
    # Last-ditch fallback: the newest plan_version of any role for the user.
    fallback = db.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id)
        .order_by(desc(PlanVersion.imported_at))
        .limit(1)
    ).scalar_one_or_none()
    if fallback is not None:
        return fallback, f"Baseline / {fallback.role or 'unknown'}"
    return None, "No plan imported yet"


def _build_fm_objections(
    db: Session, plan: PlanVersion | None, user_id: str,
) -> list[dict[str, str]]:
    """Return objections (severity/topic/detail dicts) when the supplied
    plan is a draft with a fund_manager agent_report attached.

    Mirrors the parsing logic in ``argosy.api.routes.plan.get_draft_objections``
    but inline-only (no LLM translation cache; one DB read).
    """
    if plan is None or plan.role != "draft" or plan.decision_run_id is None:
        return []
    decision_id_str = f"plan-synth-{plan.decision_run_id}"
    row = db.execute(
        select(AgentReport)
        .where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "fund_manager",
        )
        .order_by(desc(AgentReport.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not row.response_text:
        return []

    # Lenient JSON parse — same pattern the route uses.
    text = row.response_text
    parsed: dict[str, Any] = {}
    decoder = json.JSONDecoder(strict=False)
    try:
        obj, _ = decoder.raw_decode(text)
        if isinstance(obj, dict):
            parsed = obj
    except json.JSONDecodeError:
        brace = text.find("{")
        if brace >= 0:
            try:
                obj, _ = decoder.raw_decode(text[brace:])
                if isinstance(obj, dict):
                    parsed = obj
            except json.JSONDecodeError:
                parsed = {}

    reasons = parsed.get("reasons") or []
    out: list[dict[str, str]] = []
    for r in reasons:
        if not isinstance(r, str) or not r.strip():
            continue
        # Parse same shapes ``_split_reason`` recognises in plan.py.
        topic, detail = _split_reason(r)
        sev = _classify_severity(topic, detail)
        out.append({"severity": sev, "topic": topic, "detail": detail})
    return out


_RED_KEYWORDS = (
    "hard constraint violation",
    "time-critical",
    "permanent-loss",
    "section 102",
    "statutory",
    "blocker",
    "catastrophic",
    "critical",
)
_AMBER_KEYWORDS = (
    "failure",
    "missing",
    "unquantified",
    "escalate",
    "unresolved",
    "conflation",
    "regression",
    "coherence gap",
    "amber",
)


def _classify_severity(topic: str, detail: str) -> str:
    blob = (topic + " " + detail).lower()
    if any(k in blob for k in _RED_KEYWORDS):
        return "BLOCKER"
    if any(k in blob for k in _AMBER_KEYWORDS):
        return "AMBER"
    return "YELLOW"


def _split_reason(reason: str) -> tuple[str, str]:
    """Split an FM reason into (topic, detail). Same shapes as plan.py."""
    m = re.match(r"^\s*\[([A-Z]+)\s+[—-]+\s+([^\]]+)\]\s*(.*)$", reason, re.DOTALL)
    if m:
        sev_label = m.group(1).strip()
        topic_inside = m.group(2).strip()
        detail = m.group(3).strip()
        topic = f"{sev_label} — {topic_inside}" if topic_inside else sev_label
        return (topic, detail or reason)
    for sep in (" — ", " -- ", " - "):
        if sep in reason:
            topic, detail = reason.split(sep, 1)
            return topic.strip(), detail.strip()
    return (reason.strip()[:80], reason.strip())


def _latest_self_review(
    db: Session, user_id: str,
) -> FleetSelfReviewReport | None:
    return db.execute(
        select(FleetSelfReviewReport)
        .where(FleetSelfReviewReport.user_id == user_id)
        .order_by(desc(FleetSelfReviewReport.generated_at))
        .limit(1)
    ).scalar_one_or_none()


def _latest_codex_opinion(
    db: Session, user_id: str, decision_run_id: int | None,
) -> AgentReport | None:
    """Return the codex_second_opinion agent_report for this synthesis run,
    or None when absent (codex wasn't dispatched, or this isn't a draft)."""
    if decision_run_id is None:
        return None
    decision_id_str = f"plan-synth-{decision_run_id}"
    return db.execute(
        select(AgentReport)
        .where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "codex_second_opinion",
        )
        .order_by(desc(AgentReport.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _parse_codex_assessment(row: AgentReport | None) -> str | None:
    """Extract a short ``agreement`` label from the codex row's response_text.

    The agent persists ``CodexSecondOpinion.model_dump_json(indent=2)``; we
    fish out the top-level ``agreement`` enum value if present, else return
    a short prefix of the response_text.
    """
    if row is None or not row.response_text:
        return None
    try:
        obj = json.loads(row.response_text)
    except json.JSONDecodeError:
        # Best-effort string preview.
        return row.response_text.strip().splitlines()[0][:120] or None
    if isinstance(obj, dict):
        agreement = obj.get("agreement")
        if isinstance(agreement, str) and agreement.strip():
            return agreement.strip()
    return None


# ---------------------------------------------------------------------------
# Coherence deliberation appendix
# ---------------------------------------------------------------------------


def render_coherence_deliberation_appendix(rows: list[dict]) -> str:
    """One row per coherence ruling: question -> resolution -> ruling -> surfaces.
    Internal metadata: ships in the user export, stripped from the reader artifact."""
    if not rows:
        return ""
    lines = ["## Appendix — Coherence deliberations", "",
             "| Subject | Question | Resolved by | Ruling | Surfaces conformed |",
             "|---|---|---|---|---|"]
    for r in rows:
        surfaces = ", ".join(r.get("conformed_surfaces") or [])
        q = (r.get("question") or "").replace("|", "\\|")[:80]
        ruling = (r.get("ruling") or "").replace("|", "\\|")[:120]
        lines.append(
            f"| {r.get('subject_type','')} | {q} | {r.get('resolved_by','')} | {ruling} | {surfaces} |"
        )
    return "\n".join(lines)


def _fm_dialogue_outcome(resolution: str, stance: str) -> str:
    """Readable outcome label (mirrors fm_objection_dialogue._terminal_state)."""
    s = (stance or "").strip().upper()
    if resolution == "FM_ACCEPTS_ANALYST":
        return "defect confirmed — change required" if s == "CONCEDE" else "✓ cleared (no change needed)"
    if resolution == "FM_REVISES_OBJECTION":
        return "objection revised — still open"
    if resolution == "ESCALATE_TO_USER":
        return "escalated to you"
    return "maintained — blocking"


def render_fm_dialogue_appendix(rows: list[dict]) -> str:
    """FM rejection → per-objection negotiation with the owning analyst, then the FM's
    ruling. One row per objection (latest dialogue wins). Internal metadata: ships in the
    user export, stripped from the reader artifact."""
    if not rows:
        return ""
    seen: set = set()
    ordered: list[dict] = []
    for r in rows:  # rows are newest-first
        n = r.get("notes", {}) or {}
        idx = n.get("objection_index")
        if idx in seen:
            continue
        seen.add(idx)
        ordered.append(n)
    if not ordered:
        return ""
    lines = [
        "## Appendix — FM objection dialogues (how the FM talked to the fleet)", "",
        "When the Fund Manager rejected the draft, each objection was routed to its owning "
        "analyst for a rebuttal; the FM then ruled. This is that exchange.", "",
        "| # | Objection | Owning agent | Analyst stance | FM verdict | Outcome |",
        "|---|---|---|---|---|---|",
    ]
    for n in sorted(ordered, key=lambda d: (d.get("objection_index") or 0)):
        topic = (n.get("objection_topic") or "").replace("|", "\\|")[:60]
        lines.append(
            f"| {n.get('objection_index','')} | {topic} | {n.get('analyst_role','') or '—'} "
            f"| {n.get('analyst_stance','') or '—'} | {n.get('resolution','') or '—'} "
            f"| {_fm_dialogue_outcome(n.get('resolution',''), n.get('analyst_stance',''))} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_plan_export_markdown(
    db: Session,
    *,
    user_id: str,
    today: date | None = None,
    window_days: int = 14,
    include_fm_objections: bool = True,
) -> str:
    """Build the one-pager markdown export for ``user_id``.

    Every section emits a clearly-marked fallback line when its source data
    is missing so the document is always cohesive end-to-end. Returns the
    full markdown body — the route layer wraps it in a ``Response`` with
    appropriate headers.

    ``include_fm_objections``: the "Pending FM objections" block is INTERNAL
    review metadata frozen at the fund-manager phase — it predates the
    reconcile/surgical edits, so it can contradict the FINAL body. The
    whole-artifact reader must review the PLAN, not this stale scratchpad, so
    the assembled-artifact path passes ``False``; the user-facing export keeps
    it (the objection-dialogue feature needs it).
    """
    today = today or date.today()
    today_iso = today.isoformat()

    # ----- Resolve current plan + status ----------------------------------
    plan, status_label = _resolve_plan(db, user_id)

    # ----- READ-time {{fact:key}} render (item I / SAME seam as /draft) ---
    # ``horizon_*_md`` (and the Quick Reference block, which is sourced from
    # ``horizon_long_md``) are persisted with unrendered ``{{fact:key}}``
    # tokens — the tokens are the drift protection; the stored text is never
    # mutated. Render them here through the existing seam
    # (``render_plan_facts`` / ``_render_text_with_provenance``) so every
    # consumer of this export (the download AND ``assemble_plan_artifact``,
    # which composes it) sees resolved numbers or the loud ``PENDING_LABEL``
    # fallback — never a raw unrendered token. Mirrors
    # ``argosy.api.routes.plan._apply_fact_token_render`` exactly; no second
    # renderer.
    horizon_long_md = plan.horizon_long_md if plan is not None else None
    horizon_medium_md = plan.horizon_medium_md if plan is not None else None
    horizon_short_md = plan.horizon_short_md if plan is not None else None
    if plan is not None:
        try:
            # use_cache=False: the export is called far less often than the
            # /draft route this seam was built for, and the process-global
            # render cache is keyed only on (plan_version_id, snapshot_id) —
            # ids that legitimately collide across distinct DBs (e.g. per-test
            # fixtures), so a cached bundle from a DIFFERENT database could
            # otherwise leak into this export. Skipping the cache costs one
            # extra resolve per export call and buys always-live numbers.
            _bundle = render_plan_facts(
                db, user_id=user_id, plan_version=plan, write_staleness_flag=True,
                use_cache=False,
            )
            horizon_long_md = _bundle.horizon_long_md
            horizon_medium_md = _bundle.horizon_medium_md
            horizon_short_md = _bundle.horizon_short_md
        except Exception as exc:  # noqa: BLE001 — never crash the export on render
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "plan_export.fact_token_render_failed user=%s plan=%s error=%s",
                user_id, getattr(plan, "id", None), str(exc)[:200],
            )

    # ----- Wealth dashboard -----------------------------------------------
    try:
        dash: WealthDashboard | None = compute_wealth_dashboard(
            db, user_id=user_id, today=today,
        )
    except Exception:  # noqa: BLE001 - defensive; never crash the export
        dash = None

    # ----- Action items ---------------------------------------------------
    # Reuse the route's collector so the bullets here match what the home
    # page shows verbatim.
    from argosy.api.routes.plan import _collect_action_items

    action_items_lines: list[str] = []
    if plan is not None:
        items = _collect_action_items(plan, today=today, window_days=window_days)
        action_items_lines = _format_action_items(items)

    # ----- FM objections (only when plan is a draft) ----------------------
    # Internal review metadata frozen at the FM phase — excluded from the
    # reader-facing artifact (it predates the final body and can contradict it).
    objections = _build_fm_objections(db, plan, user_id) if include_fm_objections else []

    # ----- Self-review counts ---------------------------------------------
    self_review = _latest_self_review(db, user_id)
    sr_counts: dict[str, int] = {}
    if self_review is not None and self_review.severity_summary_json:
        try:
            sr_counts = json.loads(self_review.severity_summary_json)
        except json.JSONDecodeError:
            sr_counts = {}

    # ----- Codex second opinion (for drafts) ------------------------------
    codex_row = _latest_codex_opinion(
        db,
        user_id,
        plan.decision_run_id if plan is not None else None,
    )
    codex_assessment = _parse_codex_assessment(codex_row)

    # ----- Assemble document ---------------------------------------------
    lines: list[str] = []
    push = lines.append

    push(f"# Argosy Plan Snapshot — {today_iso}")
    push("")

    # Current Plan ---------------------------------------------------------
    push("## Current Plan")
    if plan is None:
        push("_No plan imported yet._")
    else:
        push(f"Active: {plan.version_label or f'plan_version_id={plan.id}'}")
        push(f"Status: {status_label}")
    push("")

    # Canonical target allocation — rendered STRAIGHT from the plan's
    # structured TargetAllocationDoc (which already folds in durable
    # authored overrides). Scoped refinement edits (POST /api/plan/refine)
    # update the structured doc but copy prose forward verbatim, so prose
    # allocation numbers can lag until the next synthesis re-render —
    # observed live as plan_critiques #1 RED 1 (IPS prose "NVDA 12%" vs
    # the doc's 8%). Leading with the governing object + an explicit
    # authority ordering turns that lag from a silent cross-surface
    # contradiction into a labeled, reconcilable state. Numbers here come
    # from the structured plan object only — never hand-typed.
    if plan is not None:
        try:
            from argosy.services.target_allocation_doc import (
                load_plan_target_allocation,
            )

            alloc_doc = load_plan_target_allocation(plan)
        except Exception:  # noqa: BLE001 — defensive; never crash the export
            alloc_doc = None
        if alloc_doc is not None and getattr(alloc_doc, "classes", None):
            push("### Canonical target allocation (structured — governs)")
            push(
                "_Rendered directly from the plan's structured "
                "TargetAllocationDoc (durable authored overrides included). "
                "This is the plan's authoritative allocation. Prose sections "
                "are re-rendered only at synthesis and can lag scoped "
                "refinement edits; where a prose allocation number disagrees "
                "with this table, THIS TABLE governs._"
            )
            push("")
            push("| Sleeve | Target % |")
            push("|---|---|")
            for cls in alloc_doc.classes:
                label = getattr(cls, "label", None)
                pct = getattr(cls, "target_pct", None)
                if label is None or pct is None:
                    continue
                push(f"| {label} | {float(pct):.1f}% |")
            cap = getattr(alloc_doc, "nvda_cap_pct", None)
            if cap is not None:
                push("")
                push(
                    f"- Single-name hard cap (NVDA): {float(cap):.1f}% — the "
                    "NVDA sleeve target above is the steering target inside "
                    "this cap."
                )
            push("")

    push("### Quick Reference")
    qref: str | None = None
    if plan is not None:
        # Prefer the long-horizon markdown rendering (set by the synthesizer)
        # if present — it's the user-facing "this is your plan" text.
        if horizon_long_md:
            qref = horizon_long_md.strip()
        else:
            qref = _extract_quick_reference(plan.raw_markdown or "")
    if qref:
        push(qref)
    else:
        push("_Quick Reference section unavailable for this plan._")
    push("")

    # Wealth Dashboard -----------------------------------------------------
    push("## Wealth Dashboard")
    if dash is None:
        push("_Wealth dashboard unavailable._")
    else:
        ret = dash.retirement
        savings = dash.savings_rate
        runway = dash.cash_runway
        conc = dash.concentration
        estate = dash.estate_exposure

        nw_line = f"- Total net worth (incl. real estate): {_fmt_nis(ret.net_worth_nis)}"
        if ret.net_worth_usd is not None:
            nw_line += f" ({_fmt_usd(ret.net_worth_usd)})"
        push(nw_line)

        # Residence/property component — SHOWN and labeled. Output-trust
        # doctrine: the incl.-real-estate total rests on OWNER-ESTIMATE
        # property values (snapshot real_estate_json + payment-ledger
        # overrides) that no broker export can audit. Without this labeled
        # breakdown a blind raw-data auditor cannot bridge the total to the
        # investable book and must flag the headline UNVERIFIABLE (codex
        # draft-73 BLOCKER). Amount is computed from the same helper the
        # dashboard total binds to — never hand-typed.
        try:
            from sqlalchemy import select as _select

            from argosy.services.net_worth_bases import (
                real_estate_equity_for_snapshot,
            )
            from argosy.state.models import PortfolioSnapshotRow

            _snap = db.execute(
                _select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.user_id == user_id)
                .order_by(PortfolioSnapshotRow.imported_at.desc(), PortfolioSnapshotRow.id.desc())  # canonical head ordering (imported_at DESC, id DESC) — matches get_latest_snapshot_row; a bare id.desc() could pick a backfill/restore row over the true head (Sol BLOCK-6)
                .limit(1)
            ).scalar_one_or_none()
            _re_eq = real_estate_equity_for_snapshot(
                snapshot=_snap, session=db, user_id=user_id,
            )
        except Exception:  # noqa: BLE001 — defensive; never crash the export
            _re_eq = None
        if _re_eq is not None and _re_eq.properties:
            _re_nis = _re_eq.total_net_usd_k * 1000.0 * dash.assumptions.fx_usd_nis
            _props = ", ".join(p.name for p in _re_eq.properties)
            push(
                f"- of which real-estate NET equity (incl. primary residence): "
                f"{_fmt_nis(_re_nis)} ({_fmt_usd(_re_eq.total_net_usd_k * 1000.0)}) "
                f"— per-property home value minus outstanding loan ({_props}). "
                "OWNER-ESTIMATE property values (unaudited; source: ingested "
                "owner sheet real_estate_json + payment-ledger overrides) — "
                "not auditable from broker raw holdings and EXCLUDED from the "
                "plan's audited 'investable'/'liquid' figures."
            )

        # FX provenance — the dashboard converts at TODAY's canonical rate
        # (BoI FxRate cache walkback), while plan-body figures were computed
        # at the FX frozen when the plan was synthesized (a plan INPUT, not a
        # display rate). Without this label, a blind reviewer reconciling the
        # two implied rates flags a spurious cross-surface contradiction
        # (observed: plan_critiques #1 RED "dashboard implies ≈3.00 vs plan
        # 2.944"). Both values come from the dashboard compute — never
        # hand-typed here.
        assumptions = dash.assumptions
        push(
            f"- FX USD/NIS used in this section: {assumptions.fx_usd_nis:.3f} "
            f"(source: {assumptions.fx_source}; as of {today_iso})."
        )
        push(
            "- _Reconciliation note: plan-body NIS/USD figures use the FX "
            "rate frozen at plan synthesis — a planning input. FX drift "
            "between that frozen rate and this section's live rate is "
            "expected and is not a plan defect. Also compare like-for-like: "
            "the total above INCLUDES the primary residence; the plan's "
            "'investable'/'liquid' figures exclude it._"
        )

        surplus_pct = (
            (savings.rate_pct if savings.rate_pct is not None else None)
        )
        push(
            "- Monthly burn: "
            f"{_fmt_nis(ret.monthly_burn_nis)} / Income: "
            f"{_fmt_nis(ret.monthly_income_nis)} / Surplus: "
            f"{_fmt_nis(ret.monthly_surplus_nis)}"
            + (f" ({_fmt_pct(surplus_pct)})" if surplus_pct is not None else "")
        )

        runway_str = (
            f"{runway.months_of_runway:.1f} months"
            if runway.months_of_runway is not None
            else "—"
        )
        # Basis-explicit label: months_of_runway covers cash + SGOV (see
        # wealth_dashboard), a BROADER basis than the body's cash-only emergency
        # runway. Labeling the basis prevents a spurious cross-surface
        # contradiction (dashboard ~53mo vs body cash-only ~9mo are different,
        # both-valid baskets, not a conflict).
        push(
            f"- Liquid runway (cash + SGOV): {runway_str} — measured on ACTUAL "
            "current balances (not the ₪200,000 emergency-fund floor policy "
            "figure); divisor is the canonical tracked monthly burn (see "
            "Monthly burn line above), never gross income."
        )

        gap = None
        if conc.current_pct is not None and conc.target_pct is not None:
            gap = conc.current_pct - conc.target_pct
        gap_str = ""
        if gap is not None:
            gap_str = (
                f" (target: {_fmt_pct(conc.target_pct)}, "
                f"gap {gap:+.1f}pp)"
            )
        elif conc.target_pct is not None:
            gap_str = f" (target: {_fmt_pct(conc.target_pct)})"
        push(
            f"- {conc.symbol} concentration: "
            f"{_fmt_pct(conc.current_pct)}{gap_str}"
        )

        estate_line = (
            f"- US-situs estate exposure: {_fmt_usd(estate.us_situs_usd)}"
        )
        if estate.potential_liability_usd is not None and estate.potential_liability_usd > 0:
            estate_line += (
                f" (~{_fmt_usd(estate.potential_liability_usd)} potential liability)"
            )
        push(estate_line)
        push("")

        push("### Retirement age — publish BOTH readings (Ariel's ruling, 2026-08-18)")
        pres_age_disp = "[derivation pending]"
        esa_age_disp = "[derivation pending]"
        if plan is not None:
            try:
                from argosy.services.plan_numeric_resolver import resolve_plan_numbers

                _drun = getattr(plan, "decision_run_id", None)
                if _drun is not None:
                    _ages_resolved = resolve_plan_numbers(
                        db, user_id=user_id, decision_run_id=int(_drun),
                        include_canonical_ages=True,
                    )
                    _pres_rv = _ages_resolved.get("retirement.preservation_age")
                    _esa_rv = _ages_resolved.get("retirement.earliest_safe_age")
                    if _pres_rv is not None and _pres_rv.status == "resolved" and _pres_rv.value is not None:
                        pres_age_disp = f"{float(_pres_rv.value):.0f}"
                    if _esa_rv is not None and _esa_rv.status == "resolved" and _esa_rv.value is not None:
                        esa_age_disp = f"{float(_esa_rv.value):.0f}"
            except Exception:  # noqa: BLE001 — defensive; never crash the export
                pass
        push(
            f"- **Mandate case (capital preservation, no principal drawdown)**: "
            f"age **{pres_age_disp}** — `retirement.preservation_age`. Matches "
            "the household's explicit stated constraint."
        )
        push(
            f"- **Off-mandate case (typical drawdown, spends principal)**: "
            f"age **{esa_age_disp}** — `retirement.earliest_safe_age`. Shown for "
            "comparison only."
        )
        push(
            "_Neither age above is 'the' retirement age — state the pair. The "
            "scenario grid below recomputes the OFF-MANDATE (typical-drawdown, "
            "90% solvency to 95, PERMITS spending principal) reading under each "
            "scenario's central real return as μ — it is the SAME model as "
            "`retirement.drawdown_scenario_age` / `retirement.earliest_safe_age` "
            "above, never the mandate case. 'Unreachable' below means the sweep "
            "found no age up to 94 clearing 90% solvency to 95 at that μ — not "
            "that the search was cut off early._"
        )
        push("| Scenario | Real return | Years to safe retirement (off-mandate) | Off-mandate age (MC, this μ) |")
        push("|---|---|---|---|")
        for sc in ret.scenarios:
            y2t = sc.years_to_target
            if y2t is None:
                y2t_label = "Unreachable to age 94 at this μ"
            elif y2t <= 0:
                y2t_label = "At target"
            else:
                y2t_label = f"{y2t:.1f}"
            target_age_label = (
                str(sc.target_age) if sc.target_age is not None else "—"
            )
            push(
                f"| {sc.name.capitalize()} | {_fmt_pct(sc.real_return * 100, digits=1)} "
                f"| {y2t_label} | {target_age_label} |"
            )
    push("")

    # Action Items ---------------------------------------------------------
    push(f"## Action Items (next {window_days} days)")
    if action_items_lines:
        lines.extend(action_items_lines)
    else:
        push("_No dated action items in window._")
    push("")

    # FM Objections --------------------------------------------------------
    if plan is not None and plan.role == "draft" and objections:
        push("## Pending FM objections")
        for i, obj in enumerate(objections, start=1):
            push(f"{i}. [{obj['severity']}] {obj['topic']}")
            if obj["detail"]:
                # Indent detail under the numbered list.
                push(f"   {obj['detail']}")
        push("")

    # Long-horizon plan ----------------------------------------------------
    push("## Long-horizon plan")
    if plan is not None and horizon_long_md:
        push(horizon_long_md.strip())
    else:
        push("_No long-horizon detail available._")
    push("")

    # Medium-horizon plan --------------------------------------------------
    push("## Medium-horizon plan")
    if plan is not None and horizon_medium_md:
        push(horizon_medium_md.strip())
    else:
        push("_No medium-horizon detail available._")
    push("")

    # Short-horizon plan ---------------------------------------------------
    push("## Short-horizon plan (next 30 days)")
    if plan is not None and horizon_short_md:
        push(horizon_short_md.strip())
    else:
        push("_No short-horizon detail available._")
    push("")

    # Notes ----------------------------------------------------------------
    push("## Notes")
    if plan is not None and plan.decision_run_id is not None:
        synth_ts = plan.imported_at.isoformat() if plan.imported_at else "unknown time"
        push(
            f"- Synthesis last run: #{plan.decision_run_id} at {synth_ts}"
        )
    else:
        push("- Synthesis last run: _not applicable (plan was not synthesized)_")
    # Codex-status + self-review counts are internal QA/governance metadata
    # (was a second-opinion present? how many self-review flags?) — generation
    # provenance, not client-plan content. They churn out of sync with the final
    # body (e.g. "codex absent" vs receipts showing it ran) and the whole-artifact
    # reader flags that as a contradiction. Excluded from the reader artifact via
    # the same reader-view flag as the FM-objection scratchpad; the user export
    # keeps them.
    if include_fm_objections:
        if codex_assessment is not None:
            push(f"- Codex second-opinion: present, {codex_assessment}")
        else:
            push("- Codex second-opinion: absent")
        if sr_counts:
            red = int(sr_counts.get("RED", 0) or 0)
            amber = int(sr_counts.get("AMBER", 0) or 0)
            yellow = int(sr_counts.get("YELLOW", 0) or 0)
            push(f"- Self-review: {red} RED, {amber} AMBER, {yellow} YELLOW")
        else:
            push("- Self-review: _no recent self-review report_")
    push("")

    # FM-objection dialogues — how the FM negotiated each rejection with the owning
    # analyst (internal review metadata; stripped from the reader artifact).
    if include_fm_objections and plan is not None:
        try:
            from argosy.orchestrator.flows.fm_objection_dialogue import (
                list_dialogues_for_plan_version,
            )
            _dlg_app = render_fm_dialogue_appendix(
                list_dialogues_for_plan_version(db, user_id=user_id, plan_version_id=plan.id)
            )
            if _dlg_app:
                push("")
                push(_dlg_app)
                push("")
        except Exception:  # noqa: BLE001 — appendix is best-effort
            pass

    # Footer ---------------------------------------------------------------
    push("---")
    push(f"Generated by Argosy on {datetime.now().isoformat(timespec='seconds')}")
    push("")

    return "\n".join(lines)


def plan_markdown_for_review(db: Session, *, user_id: str, plan: Any) -> str:
    """The plan text a critique/review agent should read for ``plan``.

    Graph-authored plan versions (post living-plan cutover) carry an EMPTY
    ``raw_markdown`` — the plan lives in the derivation graph + the canonical
    TargetAllocationDoc, not a prose blob. Feeding ``raw_markdown`` directly
    to the critique agent therefore silently reviewed nothing (weekly_review
    no-op'd with ``weekly_review.no_plan``; ``plan_critiques`` stayed empty
    since the cutover). When the prose is present use it; otherwise render
    the canonical one-pager export — the same document the user downloads —
    so the critique reviews the REAL current plan.
    """
    md = (getattr(plan, "raw_markdown", "") or "").strip()
    if md:
        return md
    # Reader-facing artifact: exclude the stale internal FM-objection
    # scratchpad (see build_plan_export_markdown docstring).
    return build_plan_export_markdown(
        db, user_id=user_id, include_fm_objections=False,
    )


def export_filename(today: date | None = None) -> str:
    """Filename for the downloaded markdown — ``argosy-plan-YYYY-MM-DD.md``."""
    d = today or date.today()
    return f"argosy-plan-{d.isoformat()}.md"


__all__ = [
    "build_plan_export_markdown",
    "export_filename",
    "plan_markdown_for_review",
    "render_coherence_deliberation_appendix",
    "render_fm_dialogue_appendix",
]
