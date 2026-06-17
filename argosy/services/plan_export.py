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
    push("### Quick Reference")
    qref: str | None = None
    if plan is not None:
        # Prefer the long-horizon markdown rendering (set by the synthesizer)
        # if present — it's the user-facing "this is your plan" text.
        if plan.horizon_long_md:
            qref = plan.horizon_long_md.strip()
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
        push(f"- Liquid runway (cash + SGOV): {runway_str}")

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

        push("### Retirement projection")
        push(
            "_FI age by scenario (deterministic crossing under each scenario's "
            "return assumption) — not the headline retirement age; the "
            "Typical-scenario row is the deterministic-trajectory point, "
            "distinct from the Monte-Carlo earliest-safe headline age._"
        )
        push("| Scenario | Real return | Years to FI target | FI age (this scenario) |")
        push("|---|---|---|---|")
        for sc in ret.scenarios:
            y2t = sc.years_to_target
            if y2t is None:
                y2t_label = "Unreachable at current burn"
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
    if plan is not None and plan.horizon_long_md:
        push(plan.horizon_long_md.strip())
    else:
        push("_No long-horizon detail available._")
    push("")

    # Medium-horizon plan --------------------------------------------------
    push("## Medium-horizon plan")
    if plan is not None and plan.horizon_medium_md:
        push(plan.horizon_medium_md.strip())
    else:
        push("_No medium-horizon detail available._")
    push("")

    # Short-horizon plan ---------------------------------------------------
    push("## Short-horizon plan (next 30 days)")
    if plan is not None and plan.horizon_short_md:
        push(plan.horizon_short_md.strip())
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

    # Footer ---------------------------------------------------------------
    push("---")
    push(f"Generated by Argosy on {datetime.now().isoformat(timespec='seconds')}")
    push("")

    return "\n".join(lines)


def export_filename(today: date | None = None) -> str:
    """Filename for the downloaded markdown — ``argosy-plan-YYYY-MM-DD.md``."""
    d = today or date.today()
    return f"argosy-plan-{d.isoformat()}.md"


__all__ = [
    "build_plan_export_markdown",
    "export_filename",
]
