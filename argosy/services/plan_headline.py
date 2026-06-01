"""Plain-English recap-summary service for the /plan recap view (Wave 8 Piece G).

Produces the three-line headline + four at-a-glance blocks that sit at
the top of the /plan recap layout when a current accepted plan exists.

Pure-Python where possible:
  - The math half (`compute_headline_lines`, `summarize_accepted_deltas`,
    `summarize_insurance_gaps`) is dataclass-in / dataclass-out and
    unit-testable without a DB.
  - The orchestrator `compute_recap_summary(db, user_id)` reads the
    current PlanVersion + latest portfolio_snapshot + reuses the
    existing cashflow_projection service for retire-ready ages and
    insurance_gaps service for coverage gaps.

Returns ``None`` when no current plan exists; callers (the FastAPI
route) translate that into HTTP 200 + null per the project's "absence
of data" convention.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.agents.plan_synthesizer_types import Action, Delta, HorizonSection
from argosy.state.models import PlanVersion, PortfolioSnapshotRow
from argosy.state.queries import get_current_plan


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadlineLines:
    """The three-line plain-English headline."""

    # "You can safely retire at age 49 (base) / age 51 (bear)." or a
    # fallback ("Retirement age not yet projected.") when the cashflow
    # service couldn't produce a crossing.
    retirement_readiness: str
    # "Next big move: cross-border attorney retainer by 2026-06-15."
    # Null when no dated actions exist across any horizon.
    next_big_move: str | None
    # "Then: NVDA tranche window opens 2026-06-17." Null when there's
    # only one (or zero) dated actions.
    then: str | None


@dataclass(frozen=True)
class AcceptedDeltaSummary:
    """One row in the accepted_deltas at-a-glance block."""

    horizon: str  # "long" | "medium" | "short"
    item_kind: str  # "target" | "theme" | "action" | "speculative_candidate"
    summary: str


@dataclass(frozen=True)
class PortfolioValueAnchor:
    """Total portfolio value anchor (in USD) + snapshot date."""

    total_usd_value_k: float | None
    snapshot_date: str | None


@dataclass(frozen=True)
class InsuranceGapsSummary:
    """One-line insurance gaps roll-up suitable for a tile."""

    one_line: str
    has_data: bool


@dataclass(frozen=True)
class AuditLine:
    """plan_version_id + decision_run_id + accepted_at + drill-in link."""

    plan_version_id: int
    decision_run_id: int | None
    approved_at: str | None  # ISO timestamp
    synthesis_trail_link: str | None  # e.g. "/decisions/123" or None


@dataclass(frozen=True)
class RecapSummary:
    headline: HeadlineLines
    accepted_deltas: list[AcceptedDeltaSummary]
    portfolio_value: PortfolioValueAnchor
    insurance_gaps: InsuranceGapsSummary
    audit: AuditLine


# ---------------------------------------------------------------------------
# Pure-function math layer (DB-free; unit-testable in isolation)
# ---------------------------------------------------------------------------


def _horizon_from_json(raw: str | None) -> HorizonSection | None:
    """Parse a ``plan_versions.horizon_*_json`` cell into a typed
    HorizonSection, returning None when the cell is empty / malformed.

    Per-cell defensive parse — a single bad horizon doesn't kill the
    whole summary.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        return HorizonSection.model_validate(payload)
    except Exception:  # pydantic ValidationError or any other
        return None


def _all_actions_with_dates(
    horizons: Iterable[HorizonSection],
) -> list[tuple[date, Action]]:
    """Collect every ``Action`` with a parseable ISO ``trigger_or_date``
    across the given horizons, paired with the parsed date for sorting.

    Non-dated actions (``directional``, ``parameterized``) are skipped —
    only ``horizon_kind == "dated"`` actions surface in the headline's
    next-move / then lines because the headline needs a concrete date.
    """
    out: list[tuple[date, Action]] = []
    for h in horizons:
        for a in h.actions:
            if a.horizon_kind != "dated":
                continue
            d = _parse_iso_date(a.trigger_or_date)
            if d is None:
                continue
            out.append((d, a))
    out.sort(key=lambda pair: pair[0])
    return out


def _parse_iso_date(s: str | None) -> date | None:
    """Liberal ISO-date parser.

    Accepts ``YYYY-MM-DD`` (preferred), ``YYYY-MM-DDTHH:MM:SS`` (the
    synthesizer occasionally emits the full timestamp), and bare
    ``YYYY-MM``. Anything else → None (treated as not-dated).
    """
    if not s:
        return None
    s = s.strip()
    # Strip a trailing trigger expression after the date if present
    # (e.g. "2026-06-15 — review tranche size"). Take the first token.
    head = s.split()[0] if s else s
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def compute_headline_lines(
    horizons: Iterable[HorizonSection],
    retire_ready_age_base: float | None,
    retire_ready_age_bear: float | None,
) -> HeadlineLines:
    """Produce the three-line headline from the synthesizer's horizons
    + the cashflow service's retire-ready ages.

    Pure: no DB, no LLM, no clock.
    """
    horizons_list = list(horizons)
    dated = _all_actions_with_dates(horizons_list)

    # Line 1 — retirement readiness
    if retire_ready_age_base is not None:
        base_str = f"age {retire_ready_age_base:.0f} (base case)"
        if (
            retire_ready_age_bear is not None
            and abs(retire_ready_age_bear - retire_ready_age_base) >= 0.5
        ):
            line1 = (
                f"You can safely retire at {base_str}, "
                f"age {retire_ready_age_bear:.0f} (bear case)."
            )
        else:
            line1 = f"You can safely retire at {base_str}."
    else:
        line1 = (
            "Retirement age not yet projected — the cashflow service "
            "couldn't find a base-case crossing within the projection horizon."
        )

    # Line 2 — next big move
    line2: str | None = None
    if dated:
        d, a = dated[0]
        line2 = f"Next big move: {a.label} by {d.isoformat()}."

    # Line 3 — then
    line3: str | None = None
    if len(dated) >= 2:
        d, a = dated[1]
        line3 = f"Then: {a.label} by {d.isoformat()}."

    return HeadlineLines(
        retirement_readiness=line1,
        next_big_move=line2,
        then=line3,
    )


def summarize_accepted_deltas(
    horizons: Iterable[HorizonSection],
) -> list[AcceptedDeltaSummary]:
    """Collect every accepted Delta across all horizons + return a
    one-line-each summary list, preserving horizon ordering
    (long → medium → short) for stable UI rendering."""
    out: list[AcceptedDeltaSummary] = []
    for h in horizons:
        for d in h.deltas_from_prior:
            if not d.accepted:
                continue
            out.append(
                AcceptedDeltaSummary(
                    horizon=d.horizon,
                    item_kind=d.item_kind,
                    summary=d.summary,
                )
            )
    return out


def summarize_insurance_gaps(
    gaps: list,  # list[InsuranceGap] from insurance_gaps service
) -> InsuranceGapsSummary:
    """Roll up a list of InsuranceGap dataclasses into one line.

    Logic:
      - Categories with a non-zero gap are flagged "missing/short"
      - Categories with zero gap are listed as "covered"
      - "No major gaps" returned when EVERY category is at-or-below
        recommended coverage.
    """
    if not gaps:
        return InsuranceGapsSummary(
            one_line="Coverage not assessed.",
            has_data=False,
        )
    short: list[str] = []
    covered: list[str] = []
    for g in gaps:
        gap_value = g.gap_nis.value
        try:
            gap_amount = float(gap_value)
        except (TypeError, ValueError):
            gap_amount = 0.0
        if gap_amount > 0:
            short.append(g.insurance_type.replace("_", " "))
        else:
            covered.append(g.insurance_type.replace("_", " "))
    if not short:
        return InsuranceGapsSummary(one_line="No major gaps.", has_data=True)
    one_line = (
        f"Short: {', '.join(short)}."
        if not covered
        else f"Short: {', '.join(short)}. Covered: {', '.join(covered)}."
    )
    return InsuranceGapsSummary(one_line=one_line, has_data=True)


# ---------------------------------------------------------------------------
# DB-aware orchestrator
# ---------------------------------------------------------------------------


def _latest_portfolio_snapshot(
    db: Session, user_id: str
) -> PortfolioSnapshotRow | None:
    """Return the most-recent portfolio_snapshots row for the user."""
    return (
        db.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(desc(PortfolioSnapshotRow.imported_at))
            .limit(1)
        )
    ).scalar_one_or_none()


def _portfolio_value_from_snapshot(
    snap: PortfolioSnapshotRow | None,
) -> PortfolioValueAnchor:
    if snap is None:
        return PortfolioValueAnchor(
            total_usd_value_k=None, snapshot_date=None
        )
    try:
        totals = json.loads(snap.totals_json) if snap.totals_json else {}
    except json.JSONDecodeError:
        totals = {}
    raw_value = totals.get("total_usd_value_k") if isinstance(totals, dict) else None
    try:
        value = float(raw_value) if raw_value is not None else None
    except (TypeError, ValueError):
        value = None
    snap_date: str | None = None
    if snap.snapshot_date is not None:
        snap_date = snap.snapshot_date.isoformat()
    return PortfolioValueAnchor(total_usd_value_k=value, snapshot_date=snap_date)


def _compute_retire_ready_ages(
    db: Session, user_id: str
) -> tuple[float | None, float | None]:
    """Run the existing cashflow projection with defaults and return
    (retire_ready_age_base, retire_ready_age_bear).

    Wrapped in try/except so a missing-fixture / empty-DB user doesn't
    block the rest of the headline; we just degrade to None and the
    headline renders the "not yet projected" fallback.
    """
    try:
        from argosy.services.cashflow_projection import (
            extract_household_state,
            extract_pension_state,
            project_cashflow,
        )

        hh = extract_household_state(db, user_id)
        pen = extract_pension_state(db, user_id)
        proj = project_cashflow(
            household=hh,
            pensions=pen,
            retirement_age=49.0,
            years=30,
            mu_nominal_annual=0.08,
            sigma_annual=0.18,
            lifestyle_drift_annual=0.0,
            tax_rate=0.25,
            life_events=[],
        )
        return proj.retire_ready_age_base, proj.retire_ready_age_bear
    except Exception:  # pragma: no cover - defensive degradation
        return None, None


def _compute_insurance_gaps_for_user(db: Session, user_id: str) -> list:
    """Best-effort insurance-gaps computation. Returns an empty list when
    inputs aren't available (which the summarizer interprets as
    "Coverage not assessed.")."""
    try:
        from argosy.services.cashflow_projection import extract_household_state
        from argosy.services.retirement.insurance_gaps import compute_insurance_gaps

        hh = extract_household_state(db, user_id)
        # v1: we only have monthly_expenses from cashflow extractor.
        # Defaults of 0 on income / coverage produce "missing" gaps
        # which is a defensible-on-its-face conservative starting point.
        return compute_insurance_gaps(
            monthly_income_nis=0.0,
            monthly_expenses_nis=hh.monthly_expenses_nis,
            dependents_count=0,
            has_kids_under_18=False,
            assets_nis=hh.portfolio_value_nis,
            actual_life_coverage_nis=0.0,
            actual_disability_monthly_nis=0.0,
            actual_ltc_monthly_nis=0.0,
            actual_health_supplementary=False,
        )
    except Exception:  # pragma: no cover - defensive degradation
        return []


def _audit_from_plan(pv: PlanVersion) -> AuditLine:
    approved = pv.accepted_at.isoformat() if pv.accepted_at is not None else None
    link = (
        f"/decisions/{pv.decision_run_id}"
        if pv.decision_run_id is not None
        else None
    )
    return AuditLine(
        plan_version_id=pv.id,
        decision_run_id=pv.decision_run_id,
        approved_at=approved,
        synthesis_trail_link=link,
    )


def compute_recap_summary(
    db: Session, user_id: str
) -> RecapSummary | None:
    """Top-level entry. Returns a fully-populated RecapSummary or None
    when no current plan exists for ``user_id``."""
    pv = get_current_plan(db, user_id)
    if pv is None:
        return None

    horizons = [
        h
        for h in (
            _horizon_from_json(pv.horizon_long_json),
            _horizon_from_json(pv.horizon_medium_json),
            _horizon_from_json(pv.horizon_short_json),
        )
        if h is not None
    ]

    base_age, bear_age = _compute_retire_ready_ages(db, user_id)
    headline = compute_headline_lines(horizons, base_age, bear_age)
    accepted = summarize_accepted_deltas(horizons)
    portfolio_value = _portfolio_value_from_snapshot(
        _latest_portfolio_snapshot(db, user_id)
    )
    gaps = _compute_insurance_gaps_for_user(db, user_id)
    insurance = summarize_insurance_gaps(gaps)
    audit = _audit_from_plan(pv)

    return RecapSummary(
        headline=headline,
        accepted_deltas=accepted,
        portfolio_value=portfolio_value,
        insurance_gaps=insurance,
        audit=audit,
    )
