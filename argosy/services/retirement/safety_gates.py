"""Safety gates for retirement plan approval.

Two gates ship in Wave 2:
  1. NraEstateGate — fires when US-situs assets exceed the IRS $60K
     exemption for non-US-persons. At Ariel's NVDA exposure (~$2M+
     directly in Schwab) this is the highest-impact unhedged risk: a
     sudden death would trigger ~$700-800K in US federal estate tax.
  2. LiquidityGate — fires when cash + HYSA balance is below the
     emergency-liquidity floor (default 12 months of essential expenses).

A third gate (ConflictScenarioGate) ships in Wave 3.6 because it depends
on the Monte Carlo / probability-of-ruin infrastructure built in Wave 3.

Each gate returns a ``GateVerdict`` with:
  - status: PASS | WARN | FAIL
  - value: the headline number wrapped in ValueWithRationale
  - threshold: what triggered the verdict
  - suggested_action: one-sentence next step (concrete, time-bounded
    where possible)

Plan: `docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md`
§ Wave 2.
"""
import json
import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import resolve
from argosy.services.wealth_dashboard import (
    _latest_household_budget_report,
    _latest_snapshot,
    _resolve_fx_usd_nis,
    _load_user_context_yaml,
)


log = logging.getLogger(__name__)

GateId = Literal["nra_estate", "emergency_liquidity", "conflict_scenario"]
GateStatus = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class GateVerdict:
    gate_id: GateId
    status: GateStatus
    value: ValueWithRationale  # headline number
    threshold: ValueWithRationale  # what the gate checks against
    suggested_action: ValueWithRationale
    detail_summary: str  # 1-sentence summary surfacing context


def _estate_domicile_split_usd(
    positions: list[dict], *, exclude_nvda: bool = False
) -> tuple[float, float]:
    """Return ``(us_situs_usd, estate_safe_usd)`` for liquid securities.

    US-situs is a property of the INSTRUMENT'S DOMICILE, not the broker —
    delegated to ``instrument_reference.estate_safe_for`` (same source as the
    portfolio route / allocation breakdown / deployment advisor).

    Counted as US-situs: US-domiciled securities + uncurated tickers
    (conservative). Counted as estate-safe: UCITS / non-US-domiciled /
    Israeli instruments (classifier True, or an explicit 'ucits' marker).
    Excluded from both: cash (portfolio-interest exemption), physical real
    estate (no tradable ticker), and deliberately unmanaged holdings
    (NVDA by convention) when ``exclude_nvda`` — the portfolio page's
    managed-book toggle. Estate GATES always call with ``exclude_nvda=False``
    so the total book (incl. NVDA) is measured against the $60k NRA exemption.

    The sum of the two sides is the securities-book denominator for
    "how much has already moved to UCITS."
    """
    from argosy.services.holding_books import is_managed_position
    from argosy.services.instrument_reference import estate_safe_for

    us_total = 0.0
    safe_total = 0.0
    for p in positions:
        asset_type = (p.get("asset_type") or "").lower()
        details = (p.get("details") or "")
        symbol = (p.get("symbol") or "").strip()
        if "cash" in asset_type:
            continue
        if exclude_nvda and not is_managed_position(p):
            continue
        # Physical / illiquid property (no tradable ticker) — not securities.
        if not symbol or symbol in {"-", "—", "n/a", "na", "none"}:
            continue
        try:
            value_usd = float(p.get("usd_value_k") or 0.0) * 1000.0
        except (TypeError, ValueError):
            continue
        if value_usd <= 0:
            continue

        if "ucits" in details.lower() or "ucits" in asset_type:
            safe_total += value_usd
            continue
        estate_safe = estate_safe_for(symbol, details)
        if estate_safe is True:
            safe_total += value_usd
            continue
        if estate_safe is None:
            log.warning(
                "us_situs.uncurated_instrument_counted symbol=%s details=%s "
                "(estate_safe_for returned None; counted as US-situs "
                "conservatively — add to instrument_reference for an "
                "authoritative domicile classification)",
                symbol, details[:80],
            )
        us_total += value_usd
    return us_total, safe_total


def _us_situs_assets_usd(
    positions: list[dict], *, exclude_nvda: bool = False
) -> float:
    """Sum US-situs assets per IRS NRA estate-tax rules."""
    us_usd, _safe = _estate_domicile_split_usd(
        positions, exclude_nvda=exclude_nvda,
    )
    return us_usd


def compute_nra_estate_gate(*, user_id: str, session: Session) -> GateVerdict:
    """Fires when US-situs assets > $60K exemption.

    PASS  at <= $60K (legal exposure: $0)
    WARN  at $60K - $200K (legal exposure exists; should plan migration)
    FAIL  at > $200K (mandatory UCITS migration plan for plan approval)
    """
    warn_threshold_vwr = resolve(
        "us_estate.nra_warn_threshold_usd",
        user_id=user_id, session=session,
    )
    fail_threshold_vwr = resolve(
        "us_estate.nra_fail_threshold_usd",
        user_id=user_id, session=session,
    )
    exemption_vwr = resolve(
        "us_estate.nra_exemption_usd",
        user_id=user_id, session=session,
    )
    warn_threshold = float(warn_threshold_vwr.value or 60000)
    fail_threshold = float(fail_threshold_vwr.value or 200000)
    exemption = float(exemption_vwr.value or 60000)

    snapshot = _latest_snapshot(session, user_id)
    if snapshot is None:
        value = 0.0
        situs_note = "No portfolio snapshot found; gate evaluated as PASS (no exposure)."
    else:
        try:
            from argosy.services.holding_books import (
                load_total_book,
                parse_positions_json,
            )

            raw = parse_positions_json(snapshot.positions_json)
            book = load_total_book(session, user_id, raw)
            if book.degraded:
                # NEVER publish incomplete/stale US-situs as PASS/WARN money.
                return GateVerdict(
                    gate_id="nra_estate",
                    status="FAIL",
                    value=ValueWithRationale(
                        value=None,
                        unit="USD",
                        source_id=None,
                        rationale=(
                            f"DEGRADED total book — refusing US-situs evaluation: "
                            f"{book.degrade_reason}"
                        ),
                        confidence="high",
                    ),
                    threshold=ValueWithRationale(
                        value=exemption,
                        unit="USD",
                        source_id="us_nra_estate_tax",
                        rationale=(
                            "IRS NRA estate-tax exemption — gate refuses to score "
                            "when the total book is degraded."
                        ),
                        confidence="high",
                    ),
                    suggested_action=ValueWithRationale(
                        value=(
                            "Restore a fresh unmanaged valuation (or a complete "
                            "snapshot including the unmanaged account) before "
                            "re-running the estate gate."
                        ),
                        unit="action",
                        source_id=None,
                        rationale="Refuse silent understatement of US-situs exposure.",
                        confidence="high",
                    ),
                    detail_summary=(
                        f"DEGRADED total book — US-situs refused: {book.degrade_reason}"
                    ),
                )
            positions = book.total
        except Exception as exc:  # noqa: BLE001 — refuse silent understatement
            log.error(
                "nra_estate_gate.total_book_failed user=%s err=%s",
                user_id, exc,
            )
            return GateVerdict(
                gate_id="nra_estate",
                status="FAIL",
                value=ValueWithRationale(
                    value=None,
                    unit="USD",
                    source_id=None,
                    rationale=f"total book load failed — refusing US-situs: {exc}",
                    confidence="high",
                ),
                threshold=ValueWithRationale(
                    value=exemption,
                    unit="USD",
                    source_id="us_nra_estate_tax",
                    rationale="IRS NRA estate-tax exemption",
                    confidence="high",
                ),
                suggested_action=ValueWithRationale(
                    value="Fix total-book load failure before re-running the estate gate.",
                    unit="action",
                    source_id=None,
                    rationale="Refuse silent understatement of US-situs exposure.",
                    confidence="high",
                ),
                detail_summary=f"total book load failed — US-situs refused: {exc}",
            )
        value = _us_situs_assets_usd(positions)
        situs_note = (
            "Sum of all US-domiciled securities across every broker (TOTAL book "
            "incl. deliberately unmanaged NVDA), classified by instrument "
            "domicile (IRS NRA estate-tax rules) — NVDA plus US-domiciled ETFs "
            "and single names at Schwab AND at the Israeli broker. UCITS / "
            "Israeli instruments and cash excluded."
        )

    if value > fail_threshold:
        status: GateStatus = "FAIL"
        action = (
            f"Begin UCITS migration NOW. Current US-situs exposure ${value:,.0f} "
            f"is ${value - exemption:,.0f} over the $60K NRA exemption — "
            f"sudden-death scenario triggers federal estate tax up to 40%."
        )
    elif value > warn_threshold:
        status = "WARN"
        action = (
            f"Plan UCITS migration within next 12 months. Current US-situs "
            f"${value:,.0f} is above the $60K legal exemption."
        )
    else:
        status = "PASS"
        action = (
            f"No action needed — US-situs assets ${value:,.0f} are below the "
            "$60K NRA exemption."
        )

    return GateVerdict(
        gate_id="nra_estate",
        status=status,
        value=ValueWithRationale(
            value=round(value, 2),
            unit="USD",
            source_id=None,
            rationale=situs_note,
            confidence="medium",
        ),
        threshold=ValueWithRationale(
            value=exemption,
            unit="USD",
            source_id="us_nra_estate_tax",
            rationale=(
                "IRS NRA estate-tax exemption — non-US-persons holding US-situs "
                "assets at death are exempt up to this threshold; above it, the "
                "estate is subject to graduated 18-40% federal estate tax."
            ),
            confidence="high",
        ),
        suggested_action=ValueWithRationale(
            value=action,
            unit="action",
            source_id=None,
            rationale="Concrete next step based on the gate verdict.",
            confidence="high",
        ),
        detail_summary=(
            f"US-situs exposure ${value:,.0f} vs $60K NRA exemption + $200K plan-block threshold."
        ),
    )


def compute_liquidity_gate(*, user_id: str, session: Session) -> GateVerdict:
    """Fires when cash + HYSA < floor months of essential expenses.

    PASS  at >= 12 months (default)
    WARN  at 6-12 months
    FAIL  at < 6 months
    """
    floor_months_vwr = resolve(
        "emergency_liquidity.floor_months",
        user_id=user_id, session=session,
    )
    essential_fraction_vwr = resolve(
        "emergency_liquidity.essential_fraction",
        user_id=user_id, session=session,
    )
    floor_months = int(floor_months_vwr.value or 12)
    essential_fraction = float(essential_fraction_vwr.value or 0.6)

    # Pull cash + monthly burn
    snapshot = _latest_snapshot(session, user_id)
    user_ctx = _load_user_context_yaml(session, user_id)
    fx_usd_nis, _ = _resolve_fx_usd_nis(snapshot=snapshot, user_ctx=user_ctx)
    cash_nis = 0.0
    if snapshot is not None:
        try:
            positions = json.loads(snapshot.positions_json or "[]")
        except (json.JSONDecodeError, TypeError):
            positions = []
        for p in positions:
            atype = (p.get("asset_type") or "").lower()
            if "cash" not in atype:
                continue
            currency = (p.get("currency") or "").upper()
            local_val = p.get("current_value_local")
            try:
                local_f = float(local_val) if local_val is not None else 0.0
            except (TypeError, ValueError):
                continue
            if currency == "NIS":
                cash_nis += local_f
            elif currency == "USD":
                cash_nis += local_f * fx_usd_nis
            else:
                # Other currencies: ignore for now (rare in user's profile)
                continue

    budget = _latest_household_budget_report(session, user_id) or {}
    monthly_burn_nis = 0.0
    try:
        monthly_burn_nis = float(budget.get("monthly_burn_nis") or 0.0)
    except (TypeError, ValueError):
        monthly_burn_nis = 0.0

    essential_monthly_burn = monthly_burn_nis * essential_fraction
    if essential_monthly_burn <= 0:
        months_covered = 0.0
        warn_threshold_months = floor_months / 2.0  # treat 6mo as warn fallback
    else:
        months_covered = cash_nis / essential_monthly_burn
        warn_threshold_months = floor_months / 2.0

    if essential_monthly_burn <= 0:
        status: GateStatus = "WARN"
        action = (
            "No household_budget report found; can't compute essential burn. "
            "Update budget data to evaluate the liquidity floor."
        )
    elif months_covered < warn_threshold_months:
        status = "FAIL"
        gap_months = floor_months - months_covered
        gap_nis = max(0.0, gap_months * essential_monthly_burn)
        action = (
            f"Increase cash buffer by ₪{gap_nis:,.0f} (~{gap_months:.1f} months) "
            f"to reach the {floor_months}-month floor. Current buffer covers "
            f"{months_covered:.1f} months of essential expenses."
        )
    elif months_covered < floor_months:
        status = "WARN"
        gap_months = floor_months - months_covered
        gap_nis = gap_months * essential_monthly_burn
        action = (
            f"Build cash buffer by ₪{gap_nis:,.0f} (~{gap_months:.1f} months) "
            f"to reach the {floor_months}-month target. Currently at "
            f"{months_covered:.1f} months."
        )
    else:
        status = "PASS"
        action = (
            f"Cash buffer is healthy at {months_covered:.1f} months of essential "
            f"expenses (target: {floor_months})."
        )

    return GateVerdict(
        gate_id="emergency_liquidity",
        status=status,
        value=ValueWithRationale(
            value=round(months_covered, 1),
            unit="months",
            source_id=None,
            rationale=(
                f"Cash + HYSA (₪{cash_nis:,.0f}) divided by essential monthly burn "
                f"(₪{essential_monthly_burn:,.0f} = burn × {essential_fraction:.0%})."
            ),
            confidence="high",
        ),
        threshold=ValueWithRationale(
            value=floor_months,
            unit="months",
            source_id=floor_months_vwr.source_id,
            rationale=floor_months_vwr.rationale,
            confidence=floor_months_vwr.confidence,
        ),
        suggested_action=ValueWithRationale(
            value=action,
            unit="action",
            source_id=None,
            rationale="Concrete next step based on the gate verdict.",
            confidence="high",
        ),
        detail_summary=(
            f"Cash buffer covers {months_covered:.1f} months of essential expenses; "
            f"target {floor_months}, WARN at {warn_threshold_months:.0f}."
        ),
    )


# Prior in-code boundaries — kept ONLY as the fail-safe fallback when the
# plan/IPS reference can't be resolved. The authoritative source is the
# reference YAML key ``risk.conflict_ruin_{fail,warn}_threshold`` (per-user
# overridable). See _conflict_ruin_thresholds.
_FALLBACK_CONFLICT_RUIN_FAIL = 0.50
_FALLBACK_CONFLICT_RUIN_WARN = 0.30


def _conflict_ruin_thresholds(user_id: str, session: Session) -> tuple[float, float]:
    """(fail, warn) P(ruin) boundaries for the conflict gate, IPS-owned via the
    hybrid resolver (per-user override -> shipped provisional default). Falls back
    to the prior in-code constants if a key is missing/unresolvable — a fail-safe
    default, never a HARDER boundary. Warn is clamped below fail so a mis-set pair
    can't invert the verdict ordering."""
    from argosy.services.retirement.reference import ResolveError, resolve

    def _get(key: str, fallback: float) -> float:
        try:
            v = resolve(key, user_id=user_id, session=session)
            return float(v.value) if v.value is not None else fallback
        except (ResolveError, ValueError, TypeError):
            return fallback

    fail = _get("risk.conflict_ruin_fail_threshold", _FALLBACK_CONFLICT_RUIN_FAIL)
    warn = _get("risk.conflict_ruin_warn_threshold", _FALLBACK_CONFLICT_RUIN_WARN)
    warn = min(warn, fail)  # warn can never exceed fail
    return fail, warn


def compute_conflict_scenario_gate(
    *,
    user_id: str,
    session: Session,
    retirement_age: float = 49.0,
    seed: int | None = 42,
) -> GateVerdict:
    """Stress-test the projection under an Israel-specific conflict pack.

    Stressed parameters (Wave 3.6):
      - sigma_annual = 0.40 (turbulent regime baseline)
      - inflation_annual = 0.06 (post-conflict inflation spike)
      - lifestyle_drift_annual = 0.02 (security premium on essentials)

    Threshold (per master plan §"Wave 3.6"):
      - PASS when P(ruin at 85) <= 30%
      - WARN at 30% < P(ruin at 85) <= 50%
      - FAIL above 50%
    """
    # Local import to avoid the safety_gates → ruin_probability → cashflow
    # → wealth_dashboard cycle that would form if imported at module top.
    from argosy.services.retirement.ruin_probability import (
        compute_ruin_probability,
    )

    verdict = compute_ruin_probability(
        user_id=user_id,
        session=session,
        retirement_age=retirement_age,
        years=40,
        target_p_solvent=0.70,  # under conflict, anything ≥ 70% solvent at 85 is ok
        n_paths=1000,  # smaller for speed; CI not used for this gate's verdict
        bootstrap_ci_samples=50,
        sigma_annual=0.40,
        inflation_annual=0.06,
        lifestyle_drift_annual=0.02,
        seed=seed,
    )

    p_ruin_at_85 = 1.0 - float(verdict.p_solvent_at_85.value or 0.0)

    # Risk-tolerance BOUNDARIES are IPS-owned, not code constants: the stress
    # parameters above (sigma/inflation) are the scenario; "how much ruin risk is
    # acceptable" is a plan judgment. Resolve from the plan/IPS reference (per-user
    # override -> shipped provisional default), falling back to the prior in-code
    # values only if the reference is unavailable (fail-safe, never harder).
    fail_threshold, warn_threshold = _conflict_ruin_thresholds(user_id, session)

    if p_ruin_at_85 > fail_threshold:
        status: GateStatus = "FAIL"
        action = (
            f"FAIL under conflict scenario — P(ruin at 85) = {p_ruin_at_85:.0%} "
            f"(> the {fail_threshold:.0%} IPS fail boundary). "
            "Build cash reserves + diversify into NIS-denominated assets + "
            "consider sovereign-bond holdings as a hedge."
        )
    elif p_ruin_at_85 > warn_threshold:
        status = "WARN"
        action = (
            f"WARN under conflict scenario — P(ruin at 85) = {p_ruin_at_85:.0%}. "
            "Stress test passed by a thin margin; review NIS exposure + "
            "essential-expense cash buffer."
        )
    else:
        status = "PASS"
        action = (
            f"Resilient under conflict scenario — P(ruin at 85) = "
            f"{p_ruin_at_85:.0%} stays under the {warn_threshold:.0%} threshold."
        )

    return GateVerdict(
        gate_id="conflict_scenario",
        status=status,
        value=ValueWithRationale(
            value=round(p_ruin_at_85, 4),
            unit="fraction",
            source_id=None,
            rationale=(
                "P(ruin at 85) under stressed parameters σ=0.40 "
                "(turbulent regime), inflation=6%, lifestyle_drift=2% "
                "(security premium on essentials). Israel-specific tail "
                "events (extended conflict, capital controls, sanctions) "
                "can break liquidity + FX assumptions simultaneously; "
                "this gate forces a stress check."
            ),
            confidence="medium",
        ),
        threshold=ValueWithRationale(
            value=0.30,
            unit="fraction",
            source_id="argosy_derived",
            rationale=(
                "WARN threshold for P(ruin at 85) under conflict scenario. "
                "Calibrated to 'tail event survivable with adjustments'; "
                "tighten to 0.20 for very-low risk tolerance."
            ),
            confidence="medium",
        ),
        suggested_action=ValueWithRationale(
            value=action,
            unit="action",
            source_id=None,
            rationale="Concrete next step derived from the verdict.",
            confidence="high",
        ),
        detail_summary=(
            f"P(ruin at 85) {p_ruin_at_85:.0%} under conflict pack (σ=0.40, "
            f"inflation=6%, lifestyle_drift=2%)."
        ),
    )


def compute_safety_gates(
    *,
    user_id: str,
    session: Session,
    include_conflict: bool = True,
) -> list[GateVerdict]:
    """Returns NRA + Liquidity + (optionally) Conflict Scenario gates.

    Conflict scenario shipped in Wave 3.6 — depends on the Wave 3 ruin-
    probability infrastructure. Default is to include it; pass
    ``include_conflict=False`` to skip the expensive MC if you only need
    the cheap NRA + Liquidity checks.
    """
    gates = [
        compute_nra_estate_gate(user_id=user_id, session=session),
        compute_liquidity_gate(user_id=user_id, session=session),
    ]
    if include_conflict:
        gates.append(compute_conflict_scenario_gate(
            user_id=user_id, session=session,
        ))
    return gates
