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
class ReadinessVerdictSummary:
    """One readiness-policy reading distilled for the headline UI."""

    policy: str  # "returns_only" | "swr_3_5" | "swr_4_0"
    retire_ready_age: float | None
    rationale: str


@dataclass(frozen=True)
class HeadlineDerivation:
    """The assumptions that drove the retirement-readiness line, plus
    a small μ-sensitivity table so the user can see how fragile the
    retire-age conclusion is. Wave 8 v2 polish — codex round 2."""

    # Assumptions used to compute retire_ready_age_base.
    mu_nominal_annual: float
    sigma_annual: float
    tax_rate: float
    retirement_target_age: float
    # Per-(μ, retire-age) sensitivity. Computed by running the
    # cashflow projection at μ ∈ {0.04, 0.06, 0.08, 0.10} and
    # extracting the base-case retire-ready age each time. Helps the
    # user see how sensitive the headline is to the expected-return
    # assumption (which it is — strongly).
    sensitivity_by_mu: list[tuple[float, float | None]]
    # Plain-English explanation of where each number came from.
    sourced_from: str
    # Wave 8 v2.3 — per-policy retire-ready verdicts. Surfaces
    # alongside the headline so the user can compare the
    # capital-preservation reading (returns_only) with the plan's
    # explicit SWR readings (swr_3_5 = Bengen-style, swr_4_0 = more
    # aggressive). Empty list when the projection can't run.
    readiness_by_policy: list[ReadinessVerdictSummary] = field(
        default_factory=list
    )


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
    derivation: HeadlineDerivation | None
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


_SYSTEM_TASK_PREFIXES = (
    "dispatch ",
    "schedule domain-refresh",
    "schedule refresh",
    "queue refresh",
    "trigger refresh",
    "trigger domain",
    "trigger substrate",
    "kick off ",
    "ingest ",
    "re-ingest",
    "refresh tax memo",
    "refresh tax substrate",
    "refresh domain knowledge",
)

_SYSTEM_TASK_SUBSTRINGS = (
    "domain-refresh",
    "domain refresh",
    "substrate dispatcher",
    "kb refresh",
    "knowledge-base refresh",
)


def _is_system_task(label: str) -> bool:
    """Mirror of the UI-side ``isSystemTask`` heuristic. Argosy-
    internal housekeeping actions (refresh KB files, dispatch
    substrate workers, queue re-ingests) MUST NOT surface as
    user-facing actions in the headline or timeline. The
    orchestrator should auto-execute these via the scheduler
    eventually — surfacing them here was a v2.0 bug.
    """
    if not label:
        return False
    l = label.strip().lower()
    if any(l.startswith(p) for p in _SYSTEM_TASK_PREFIXES):
        return True
    return any(s in l for s in _SYSTEM_TASK_SUBSTRINGS)


def _all_actions_with_dates(
    horizons: Iterable[HorizonSection],
) -> list[tuple[date, Action]]:
    """Collect every ``Action`` with a parseable ISO ``trigger_or_date``
    across the given horizons, paired with the parsed date for sorting.

    Non-dated actions (``directional``, ``parameterized``) are skipped —
    only ``horizon_kind == "dated"`` actions surface in the headline's
    next-move / then lines because the headline needs a concrete date.

    Wave 8 v2.4 — Argosy-internal "system tasks" (dispatch domain-
    refresh, kb refresh, etc.) are filtered OUT so the headline's
    "Next big move" / "Then" lines don't surface system-internal
    housekeeping as user actions.
    """
    out: list[tuple[date, Action]] = []
    for h in horizons:
        for a in h.actions:
            if a.horizon_kind != "dated":
                continue
            if _is_system_task(a.label):
                continue
            d = _parse_iso_date(a.trigger_or_date)
            if d is None:
                continue
            out.append((d, a))
    out.sort(key=lambda pair: pair[0])
    return out


def _format_retirement_readiness(
    *,
    retire_ready_age_base: float | None,
    retire_ready_age_bear: float | None,
    retirement_target_age: float | None,
    current_age: float | None,
) -> str:
    """Render the retirement-readiness line with explicit semantics
    around what's "earliest" vs "planned". When the portfolio is
    already FI-capable today (earliest <= current age), tell the
    user that explicitly — that's the case the Jacobs plan calls
    "FI achieved on paper, continuing employment".
    """
    if retire_ready_age_base is None:
        return (
            "Retirement age not yet projected — the cashflow model "
            "couldn't find a crossing within the horizon."
        )
    target_str = (
        f" Your plan targets retirement at age {retirement_target_age:.0f}."
        if retirement_target_age is not None
        else ""
    )
    # Already-FI case: portfolio income covers expenses today.
    if (
        current_age is not None
        and retire_ready_age_base <= current_age + 0.5
    ):
        return (
            "Financial independence is achieved on paper: at today's "
            f"portfolio + expense levels, returns + pension already "
            f"cover spending."
            + target_str
        )
    # Otherwise quote both base and bear when they're meaningfully apart.
    base_str = f"age {retire_ready_age_base:.0f}"
    if (
        retire_ready_age_bear is not None
        and abs(retire_ready_age_bear - retire_ready_age_base) >= 0.5
    ):
        earliest = (
            f"The earliest your portfolio can carry you is {base_str} "
            f"(base case), age {retire_ready_age_bear:.0f} (bear case)."
        )
    else:
        earliest = (
            f"The earliest your portfolio can carry you is {base_str}."
        )
    return earliest + target_str


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
    retirement_target_age: float | None = None,
    current_age: float | None = None,
) -> HeadlineLines:
    """Produce the three-line headline from the synthesizer's horizons
    + the cashflow service's retire-ready ages.

    Pure: no DB, no LLM, no clock.

    Wave 8 v2 polish (codex deep-audit #1): the headline now
    explicitly distinguishes ``retire_ready_age`` (earliest age the
    portfolio can support expenses on its own) from
    ``retirement_target_age`` (the user's stated retirement plan).
    These are different concepts; confusing them was the source of
    the "44 vs 49 — which is right?" user complaint.
    """
    horizons_list = list(horizons)
    dated = _all_actions_with_dates(horizons_list)

    # Line 1 — retirement readiness, plain-English with explicit
    # earliest-vs-planned distinction so the user doesn't read "44"
    # as conflict with their goals_yaml retirement_target_age=49.
    line1 = _format_retirement_readiness(
        retire_ready_age_base=retire_ready_age_base,
        retire_ready_age_bear=retire_ready_age_bear,
        retirement_target_age=retirement_target_age,
        current_age=current_age,
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
    db: Session, user_id: str, *, assumptions
) -> tuple[float | None, float | None]:
    """Run the existing cashflow projection at the calibrated
    assumptions and return (retire_ready_age_base, retire_ready_age_bear).

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
            retirement_age=assumptions.retirement_age.value,
            years=30,
            mu_nominal_annual=assumptions.mu_nominal_annual.value,
            sigma_annual=assumptions.sigma_annual.value,
            lifestyle_drift_annual=assumptions.lifestyle_drift_annual.value,
            tax_rate=assumptions.tax_rate.value,
            life_events=[],
        )
        return proj.retire_ready_age_base, proj.retire_ready_age_bear
    except Exception:  # pragma: no cover - defensive degradation
        return None, None


def _canonical_feasible(db: Session, user_id: str, *, assumptions):
    """The ONE canonical retirement-age result: the earliest age the TYPICAL
    regime clears 90% solvency to 95 under the honest dual-track assumptions
    (sigma-glide + NVDA-sale CGT + PV-discounted reserve + 5% real + 10% interim
    tax + healthcare-in-central-spend), with the capital-preservation age carried
    in ``basis``. Supersedes the optimistic sigma-flat / no-CGT canonical that
    reported 49. None on failure.
    """
    try:
        from argosy.services.retirement.retirement_plan import (
            canonical_feasible_dual_track,
        )
        return canonical_feasible_dual_track(
            session=db, user_id=user_id, target_p_solvent=0.90,
            operational_target_age=assumptions.retirement_age.value,
        )
    except Exception:  # pragma: no cover - defensive
        return None


def _readiness_anchors(canon) -> list[ReadinessVerdictSummary]:
    """The three LABELED retirement-age anchors that all surfaces agree on —
    replacing the deterministic 'by readiness policy' tiles that wrongly read
    the current age. earliest-feasible (MC 90%, reserve-netted) /
    operational-target / statutory (age-coherence 1b)."""
    if canon is None:
        return []
    ef = canon.earliest_feasible_age
    ef_str = f"{ef:.0f}" if ef is not None else "not within horizon"
    p = canon.p_solvent_at_age
    p_str = f" ({p*100:.0f}% MC solvency@95)" if p is not None else ""
    basis = canon.basis or {}
    pres = basis.get("preservation_age")
    pres_p = basis.get("preservation_p")
    pres_p_str = f" ({pres_p*100:.0f}% MC solvency@95)" if pres_p is not None else ""
    gap = int(pres - ef) if (pres is not None and ef is not None) else None
    gap_str = f" — about {gap} more working years than retiring ASAP" if gap else ""
    # Only the two COMPUTED tracks are shown as tiles. The statutory age (67) is
    # kept in the calc (annuity + BL credited from 67) but no longer a tile; the
    # operational/planned target is an INPUT, demoted to a caption in the UI.
    anchors = [
        ReadinessVerdictSummary(
            policy="Retire ASAP — drawdown (MC 90%)",
            retire_ready_age=ef,
            rationale=(
                f"WHY: the earliest age the typical-market Monte Carlo (5% real) "
                f"clears 90% solvency to age 95 while DRAWING THE PORTFOLIO DOWN — "
                f"on the deconcentrated (NVDA sold to its cap, volatility glided "
                f"34%→18%) and reserve-netted basis{p_str}. "
                "WHAT IT MEANS: you could stop working at this age and the money "
                "lasts to 95 in ~9 of 10 market paths; the worst 10% is sequence-"
                "of-returns risk, cushioned by your pension + Bituach Leumi from 67."
            ),
        ),
    ]
    if pres is not None:
        anchors.append(ReadinessVerdictSummary(
            policy="Leave it to the kids — capital-preservation",
            retire_ready_age=pres,
            rationale=(
                f"WHY: the earliest age at which even the WORST-10% market path "
                f"still leaves your principal intact in real (inflation-adjusted) "
                f"terms by age 95{pres_p_str}. "
                "WHAT IT MEANS: you live off returns instead of spending the nest "
                "egg down, so you hand roughly today's real wealth (or more) to the "
                f"kids{gap_str}. A what-if to see the tradeoff, not a constraint."
            ),
        ))
    return anchors


def _compute_mu_sensitivity(
    db: Session, user_id: str, *, assumptions
) -> list[tuple[float, float | None]]:
    """Run the deterministic cashflow projection at μ ∈ {4%, 6%, 8%,
    10%} and collect the resulting base-case retire-ready age for
    each. Helps the user see how sensitive the headline conclusion is
    to the expected-return assumption.

    Other assumptions held at the calibrated values so this is a
    pure-μ sensitivity sweep, not a multi-knob explosion.
    """
    out: list[tuple[float, float | None]] = []
    try:
        from argosy.services.cashflow_projection import (
            extract_household_state,
            extract_pension_state,
            project_cashflow,
        )

        hh = extract_household_state(db, user_id)
        pen = extract_pension_state(db, user_id)
        for mu in (0.04, 0.06, 0.08, 0.10):
            try:
                proj = project_cashflow(
                    household=hh,
                    pensions=pen,
                    retirement_age=assumptions.retirement_age.value,
                    years=50,  # extended so 4%-scenario can find a crossing
                    mu_nominal_annual=mu,
                    sigma_annual=assumptions.sigma_annual.value,
                    lifestyle_drift_annual=assumptions.lifestyle_drift_annual.value,
                    tax_rate=assumptions.tax_rate.value,
                    life_events=[],
                )
                out.append((mu, proj.retire_ready_age_base))
            except Exception:  # pragma: no cover - per-mu defensive
                out.append((mu, None))
    except Exception:  # pragma: no cover - defensive
        return []
    return out


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

    # Wave 8 v2 polish — surface the assumptions that drove the
    # retirement-age line + a μ sensitivity sweep so the user can
    # see how fragile the conclusion is. The sweep runs the
    # deterministic projection 4 times (<30ms each); cheap relative
    # to the MC route the recap also fetches.
    from argosy.services.cashflow_assumptions import get_default_assumptions

    try:
        assumptions = get_default_assumptions(session=db, user_id=user_id)
    except Exception:  # pragma: no cover - defensive
        assumptions = None

    if assumptions is not None:
        # Age-coherence (1b): ALL surfaces bind to the canonical MC-based
        # earliest-feasible age (reserve-netted, sequence-aware), NOT the
        # deterministic income-crossing that reported the current age (44).
        canon = _canonical_feasible(db, user_id, assumptions=assumptions)
        base_age = canon.earliest_feasible_age if canon is not None else None
        # The bear earliest-feasible is shown on the /retirement MC scenario
        # card (base/bull/bear); the headline quotes the single canonical age.
        bear_age = None
        # The μ→age sensitivity is dropped here: it was the deterministic
        # 67→44 cliff, and the coherent return-sensitivity now lives on
        # /retirement (the μ-grid of P-solvent). Empty → UI hides the strip.
        sensitivity = []
        readiness_by_policy = _readiness_anchors(canon)
        # Read current age once for the "FI already on paper" framing
        # so the headline doesn't read "retire at 44" when the user
        # is already 44.
        try:
            from argosy.services.cashflow_projection import extract_household_state
            current_age_yrs: float | None = (
                extract_household_state(db, user_id).current_age_years
            )
        except Exception:  # pragma: no cover - defensive
            current_age_yrs = None
        derivation = HeadlineDerivation(
            mu_nominal_annual=assumptions.mu_nominal_annual.value,
            sigma_annual=assumptions.sigma_annual.value,
            tax_rate=assumptions.tax_rate.value,
            retirement_target_age=assumptions.retirement_age.value,
            sensitivity_by_mu=sensitivity,
            sourced_from=(
                f"μ from {assumptions.mu_nominal_annual.source}, "
                f"σ from {assumptions.sigma_annual.source}, "
                f"tax from {assumptions.tax_rate.source}, "
                f"target age from {assumptions.retirement_age.source}."
            ),
            readiness_by_policy=readiness_by_policy,
        )
    else:
        base_age, bear_age = None, None
        derivation = None
        current_age_yrs = None

    target_age_for_headline = (
        assumptions.retirement_age.value if assumptions is not None else None
    )
    headline = compute_headline_lines(
        horizons,
        base_age,
        bear_age,
        retirement_target_age=target_age_for_headline,
        current_age=current_age_yrs,
    )
    accepted = summarize_accepted_deltas(horizons)
    portfolio_value = _portfolio_value_from_snapshot(
        _latest_portfolio_snapshot(db, user_id)
    )
    gaps = _compute_insurance_gaps_for_user(db, user_id)
    insurance = summarize_insurance_gaps(gaps)
    audit = _audit_from_plan(pv)

    return RecapSummary(
        headline=headline,
        derivation=derivation,
        accepted_deltas=accepted,
        portfolio_value=portfolio_value,
        insurance_gaps=insurance,
        audit=audit,
    )
