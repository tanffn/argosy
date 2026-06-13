"""Wealth Dashboard — pure-Python aggregator that surfaces the
top-of-/portfolio retirement projection + 6 stat cards.

Inputs are read once from the DB; everything else is pure math + a small
amount of YAML/JSON parsing on the cached user_context blobs. No agent
calls; no LLM. The route layer is a thin wrapper around
``compute_wealth_dashboard``.

What it returns (one ``WealthDashboard`` dataclass):

  * ``retirement``: net_worth + monthly burn/income/surplus + 3 scenarios
    (bear/conservative/typical) with their wealth-trajectory curves and
    target retirement age (current_age + ceil(years_to_target)).
  * ``cash_runway``: months of expenses covered by cash + SGOV.
  * ``concentration``: NVDA % of portfolio + plan target % (latest draft).
  * ``savings_rate``: (income − burn) / income.
  * ``fx_exposure``: USD / NIS / other split.
  * ``rsu_income``: per-quarter NVDA RSU vest schedule (next 12 months,
    NIS value at current NVDA price × FX).
  * ``estate_exposure``: US-situs holdings × FX vs the $60K NRA exemption
    + 40% potential liability above it.
  * ``assumptions``: every constant the math uses (SWR, real_return
    table, current_age, FX rate source, plan target source). The UI
    cites these inline at the bottom of the retirement card.

Each computation tolerates missing data: when a precondition is absent,
the relevant block falls back to ``None`` plus a ``missing_reason``
string the UI surfaces as a tooltip.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

import yaml
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.services import instrument_reference

from argosy.state.models import (
    AgentReport,
    PlanVersion,
    PortfolioSnapshotRow,
    UserContext,
)


# ---------------------------------------------------------------------------
# Constants — all "assumptions" the dashboard makes are spelled out here
# so the UI can cite them at the bottom of the retirement card.
# ---------------------------------------------------------------------------

SWR = 0.035  # 3.5% real, per the plan's QUICK REFERENCE.

#: Non-central real-return bands for the 25-year wealth-trajectory chart and
#: the target-retirement-age solve. ``typical`` is NOT a literal here — it is
#: single-sourced from the canonical ``RetirementAssumptions.mu_real_typical``
#: via ``get_scenario_returns()`` so /portfolio cannot diverge from /plan +
#: /retirement (it was a stale 0.045 — which is actually the dual-track
#: CONSERVATIVE case, not the 5.0% central).
_SCENARIO_RETURN_BANDS: dict[str, float] = {
    "bear": 0.00,
    "conservative": 0.02,
}


def get_scenario_returns() -> dict[str, float]:
    """Real-return scenarios for the wealth chart + target-age solve.

    ``typical`` is sourced from the canonical ``RetirementAssumptions`` so the
    central return is single-source across surfaces. The import is lazy because
    ``cashflow_projection`` imports *this* module — a module-level import of the
    retirement engine would cycle.
    """
    from argosy.services.retirement.retirement_plan import RetirementAssumptions

    return {**_SCENARIO_RETURN_BANDS, "typical": RetirementAssumptions().mu_real_typical}

DEFAULT_CURRENT_AGE = 45  # fallback when no date_of_birth in identity_yaml.
DEFAULT_FX_USD_NIS = 3.10  # last-ditch fallback when DB has none.
PROJECTION_YEARS = 25  # x-axis cap for the wealth-trajectory chart.
MAX_SOLVE_YEARS = 60  # cap for years_to_target solver.
US_NRA_ESTATE_EXEMPTION_USD = 60_000.0
US_NRA_ESTATE_RATE = 0.40


# ---------------------------------------------------------------------------
# Dataclasses (JSON-encodable; the route layer wraps in pydantic).
# ---------------------------------------------------------------------------


@dataclass
class ScenarioCard:
    name: str  # "bear" / "conservative" / "typical"
    real_return: float
    years_to_target: float | None  # None when "never at this burn rate"
    target_age: int | None
    target_portfolio_nis: float | None


@dataclass
class TrajectoryPoint:
    year: int  # 0..PROJECTION_YEARS
    bear: float
    conservative: float
    typical: float


@dataclass
class RetirementBlock:
    net_worth_nis: float | None
    net_worth_usd: float | None
    monthly_burn_nis: float | None
    monthly_income_nis: float | None
    monthly_surplus_nis: float | None
    annual_expenses_nis: float | None
    target_portfolio_nis: float | None
    swr_rate: float
    current_age: int
    current_age_inferred: bool
    scenarios: list[ScenarioCard]
    trajectory: list[TrajectoryPoint]
    missing_reasons: list[str] = field(default_factory=list)


@dataclass
class CashRunwayBlock:
    cash_nis: float | None
    sgov_nis: float | None
    defensive_total_nis: float | None
    months_of_runway: float | None
    missing_reasons: list[str] = field(default_factory=list)


@dataclass
class ConcentrationBlock:
    symbol: str
    current_pct: float | None
    target_pct: float | None
    target_source: str | None  # e.g. "draft #11 horizon_medium" / null
    missing_reasons: list[str] = field(default_factory=list)


@dataclass
class SavingsRateBlock:
    monthly_income_nis: float | None
    monthly_burn_nis: float | None
    rate_pct: float | None
    missing_reasons: list[str] = field(default_factory=list)


@dataclass
class FxBucket:
    currency: str
    value_nis: float
    pct: float


@dataclass
class FxExposureBlock:
    buckets: list[FxBucket]
    usd_pct: float | None
    missing_reasons: list[str] = field(default_factory=list)


@dataclass
class RsuQuarter:
    period: str  # e.g. "June 2026"
    date: str  # ISO-ish (may be "YYYY-MM" partial)
    shares: float
    value_nis: float


@dataclass
class RsuIncomeBlock:
    next_12_months_nis: float | None
    quarters: list[RsuQuarter]
    nvda_price_usd: float | None
    fx_usd_nis: float | None
    missing_reasons: list[str] = field(default_factory=list)


@dataclass
class EstateExposureBlock:
    us_situs_usd: float | None
    us_situs_nis: float | None
    nra_exemption_usd: float
    above_exemption_usd: float | None
    potential_liability_usd: float | None
    potential_liability_nis: float | None
    missing_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompositionSlice:
    """One slice of a composition donut (asset class or sector).

    Carries the bucket name, absolute NIS value, percentage of the
    composition total, and the list of holdings (tickers/labels) that
    landed in this bucket. The UI surfaces ``holdings`` in the per-slice
    tooltip.
    """

    name: str
    value_nis: float
    pct: float
    holdings: list[str]


@dataclass
class Assumptions:
    swr_rate: float
    scenario_returns: dict[str, float]
    fx_usd_nis: float | None
    fx_source: str
    current_age: int
    current_age_source: str  # "identity_yaml.date_of_birth" / "default"
    nvda_target_pct: float | None
    nvda_target_source: str | None
    snapshot_date: str | None
    plan_version_id: int | None


@dataclass
class WealthDashboard:
    user_id: str
    generated_at: str
    retirement: RetirementBlock
    cash_runway: CashRunwayBlock
    concentration: ConcentrationBlock
    savings_rate: SavingsRateBlock
    fx_exposure: FxExposureBlock
    rsu_income: RsuIncomeBlock
    estate_exposure: EstateExposureBlock
    asset_class_composition: list[CompositionSlice]
    sector_composition: list[CompositionSlice]
    assumptions: Assumptions


# ---------------------------------------------------------------------------
# Composition taxonomy — STATIC ticker → sector map.
#
# This is a hand-curated taxonomy for the user's known holdings. It is
# intentionally narrow: unknown tickers fall into "Other". When the
# portfolio gains a new symbol that warrants its own bucket, add it
# here. The asset-class side is driven by ``PortfolioPosition.asset_type``
# (set during TSV ingest) with this map as a fallback for the cases
# where ``asset_type`` is blank.
# ---------------------------------------------------------------------------

#: Per-ticker sector classification. Israeli ETFs are NOT keyed here — they
#: get the "Israeli ETF" bucket via a name-pattern check (Hebrew-character
#: detection) which is more robust to label variations than enumerating
#: every Hebrew-named instrument.
_TICKER_TO_SECTOR: dict[str, str] = {
    # Mega-cap tech (incl. AI/cloud/consumer-internet large-caps).
    "NVDA": "Tech",
    "AMD": "Tech",
    "GOOG": "Tech",
    "GOOGL": "Tech",
    "AMZN": "Tech",
    "META": "Tech",
    "TSLA": "Tech",
    # Broad-market / index ETFs.
    "VOO": "ETF/Index",
    "VTI": "ETF/Index",
    "QQQM": "ETF/Index",
    "SCHG": "ETF/Index",
    "SPMO": "ETF/Index",
    "SCHD": "ETF/Index",
    "FWRA": "ETF/Index",
    "MSCI WORLD": "ETF/Index",
    "CSPX": "ETF/Index",
    "ACWD": "ETF/Index",
    "CNDX": "ETF/Index",
    "XZEW": "ETF/Index",
    # UCITS twins the domicile-aware canonical plan buys (non-US-situs).
    "FUSA": "ETF/Index",
    "EXUS": "ETF/Index",
    "R1GR": "ETF/Index",
    "SPMV": "ETF/Index",
    "DPYA": "ETF/Index",
    # Value ETF (kept separate per spec).
    "VTV": "Value ETF",
    # Cash equivalents / T-bills.
    "SGOV": "Cash/T-Bill",
    "IB01": "Cash/T-Bill",
    "IBTA": "Cash/T-Bill",
    # Healthcare / REIT (lumped into Other per spec).
    "BMY": "Other",
    "O": "Other",
    # Conglomerate.
    "BRK.B": "Conglomerate",
    # Crypto.
    "IBIT": "Crypto",
}

#: Per-ticker asset-class fallback used when a position's ``asset_type``
#: field is missing/blank. The primary signal is ``asset_type``; this map
#: only kicks in when that field is absent.
_TICKER_TO_ASSET_CLASS_FALLBACK: dict[str, str] = {
    "NVDA": "Equity",
    "AMD": "Equity",
    "GOOG": "Equity",
    "GOOGL": "Equity",
    "AMZN": "Equity",
    "META": "Equity",
    "TSLA": "Equity",
    "VOO": "Equity",
    "VTI": "Equity",
    "QQQM": "Equity",
    "SCHG": "Equity",
    "SPMO": "Equity",
    "SCHD": "Equity",
    "FWRA": "Equity",
    "CSPX": "Equity",
    "ACWD": "Equity",
    "CNDX": "Equity",
    "XZEW": "Equity",
    # UCITS twins the domicile-aware canonical plan buys (non-US-situs).
    "FUSA": "Equity",
    "EXUS": "Equity",
    "R1GR": "Equity",
    "SPMV": "Equity",
    "DPYA": "Equity",
    "VTV": "Equity",
    "BMY": "Equity",
    "O": "Equity",
    "BRK.B": "Equity",
    "SGOV": "Cash",
    "IB01": "Cash",
    "IBTA": "Cash",
    "IBIT": "Alternatives",
}

#: Canonical asset-class buckets. Used for deterministic ordering in the
#: composition list and for clamping unknown classifications to "Other".
_ASSET_CLASS_ORDER: tuple[str, ...] = (
    "Equity",
    "Fixed Income",
    "Cash",
    "Alternatives",
    "Real Estate",
    "Other",
)

_SECTOR_ORDER: tuple[str, ...] = (
    "Tech",
    "ETF/Index",
    "Value ETF",
    "Dividend ETF",
    "Israeli ETF",
    "Conglomerate",
    "Financials",
    "Healthcare",
    "Real Estate",
    "Cash/T-Bill",
    "Crypto",
    "Other",
)


def _classify_asset_class(asset_type: str, symbol: str) -> str:
    """Map a position's ``asset_type`` (+ symbol fallback) to one of the
    canonical asset-class buckets.

    Rules (in order):
      1. SGOV → Cash (special-cased; technically a T-bill ETF but
         commonly counted as a cash equivalent for runway/composition).
      2. ``asset_type`` keyword match — case-insensitive substring on
         "equity"/"growth"/"core equity"/"individual stocks"/"nvidia"
         → Equity, "fixed income"/"bond"/"defensive" → Fixed Income, etc.
      3. If ``asset_type`` is blank, fall back to per-ticker map.
      4. Otherwise → Other.
    """
    sym = (symbol or "").upper().strip()
    at = (asset_type or "").lower().strip()

    # Spec carve-out: SGOV is a T-bill ETF commonly counted as Cash.
    if sym == "SGOV":
        return "Cash"

    if at:
        if any(k in at for k in ("equity", "growth", "individual stocks", "nvidia", "dividend", "international", "value")):
            return "Equity"
        if any(k in at for k in ("fixed income", "bond", "defensive")):
            return "Fixed Income"
        if at in ("cash", "money market"):
            return "Cash"
        if any(k in at for k in ("alternative", "crypto")):
            return "Alternatives"
        if "real estate" in at or at == "reit":
            # Real Estate as its own bucket — clearer than lumping into
            # Alternatives or Other.
            return "Real Estate"
        # Fall through to fallback map / Other for unknown asset_type
        # strings (defensive: don't silently mis-classify).

    # asset_type missing → per-ticker fallback map.
    if sym in _TICKER_TO_ASSET_CLASS_FALLBACK:
        return _TICKER_TO_ASSET_CLASS_FALLBACK[sym]

    return "Other"


def _is_israeli_etf(symbol: str, details: str) -> bool:
    """Detect genuinely TASE-listed instruments by a Hebrew/non-latin
    *ticker*.

    The discriminator is the SYMBOL, not the description. A US holding
    bought through Leumi (AMD, VOO, SCHD, …) carries a Hebrew
    parenthetical name in ``details`` but a latin ticker in ``symbol`` —
    it is NOT Israeli. Only instruments whose ticker itself is Hebrew /
    non-latin (e.g. ``מחקה ת"א-200``, which has no latin ticker) are
    TASE-listed. Range U+0590..U+05FF is the Hebrew Unicode block.

    ``details`` is retained in the signature (callers pass it) but is
    deliberately not scanned: a Hebrew description is not evidence the
    instrument is Israeli.
    """
    del details  # intentionally unused — see docstring.
    return any("֐" <= ch <= "׿" for ch in (symbol or ""))


def _classify_sector(symbol: str, details: str) -> str:
    """Map a position's ``symbol`` (+ details fallback) to one of the
    canonical sector buckets.

    Rules (in order):
      1. Israeli ETF (Hebrew characters in symbol/details) → "Israeli ETF".
      2. Static per-ticker map lookup → that bucket.
      3. Otherwise → "Other".

    The static map is intentionally narrow: it covers the user's known
    holdings. New tickers warrant a per-PR taxonomy update.
    """
    if _is_israeli_etf(symbol, details):
        return "Israeli ETF"
    sym = (symbol or "").upper().strip()
    if sym in _TICKER_TO_SECTOR:
        return _TICKER_TO_SECTOR[sym]
    return "Other"


# ---------------------------------------------------------------------------
# Pure math.
# ---------------------------------------------------------------------------


def years_to_target(
    *,
    starting_portfolio: float,
    annual_contribution: float,
    real_return: float,
    target: float,
    max_years: int = MAX_SOLVE_YEARS,
) -> float | None:
    """Solve for the smallest t in [0, max_years] where the future value
    of ``starting_portfolio`` compounded at ``real_return`` plus
    ``annual_contribution`` contributions reaches ``target``.

    Returns:
      * None if ``target`` is unreachable (negative contribution + below
        target, or solve overshoots ``max_years``).
      * 0 if already at or above target.

    Math:
      r > 0: FV(t) = P0*(1+r)^t + C*((1+r)^t - 1)/r
      r = 0: FV(t) = P0 + C*t
    """
    if target is None or target <= 0:
        return None
    if starting_portfolio is None:
        return None
    if starting_portfolio >= target:
        return 0.0
    # Negative or zero contributions + below target => can only reach via
    # compounding alone; if return is also <=0, unreachable.
    if annual_contribution <= 0 and real_return <= 0:
        return None

    if real_return == 0:
        if annual_contribution <= 0:
            return None
        years = (target - starting_portfolio) / annual_contribution
        if years > max_years:
            return None
        return years

    # r > 0 — binary search rather than the closed-form log because the
    # annuity formula has edge cases when C is 0 or negative. Bisect on
    # t in [0, max_years] to find the smallest t where FV(t) >= target.
    def fv(t: float) -> float:
        growth = (1.0 + real_return) ** t
        return starting_portfolio * growth + annual_contribution * (growth - 1.0) / real_return

    lo, hi = 0.0, float(max_years)
    if fv(hi) < target:
        return None
    for _ in range(80):  # 80 iterations => well under 1e-15 precision.
        mid = (lo + hi) / 2.0
        if fv(mid) >= target:
            hi = mid
        else:
            lo = mid
    return hi


def project_wealth_curve(
    *,
    starting_portfolio: float,
    annual_contribution: float,
    real_return: float,
    years: int,
) -> list[float]:
    """Return the value at year 0, 1, ..., years (inclusive) under the
    same FV(t) math as ``years_to_target``."""
    if starting_portfolio is None:
        return []
    out: list[float] = []
    for t in range(years + 1):
        if real_return == 0:
            v = starting_portfolio + annual_contribution * t
        else:
            growth = (1.0 + real_return) ** t
            v = starting_portfolio * growth + annual_contribution * (growth - 1.0) / real_return
        out.append(v)
    return out


def compute_current_age(date_of_birth: str | None, *, today: date | None = None) -> tuple[int, bool]:
    """Return (age, inferred). ``inferred=False`` when date_of_birth parsed
    cleanly; ``True`` when we fell back to DEFAULT_CURRENT_AGE."""
    if not date_of_birth:
        return DEFAULT_CURRENT_AGE, True
    try:
        dob = date.fromisoformat(date_of_birth)
    except ValueError:
        return DEFAULT_CURRENT_AGE, True
    t = today or date.today()
    age = t.year - dob.year
    if (t.month, t.day) < (dob.month, dob.day):
        age -= 1
    return age, False


# ---------------------------------------------------------------------------
# DB reads — kept tiny so each block fails gracefully on missing data.
# ---------------------------------------------------------------------------


def _latest_snapshot(session: Session, user_id: str) -> PortfolioSnapshotRow | None:
    return session.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(desc(PortfolioSnapshotRow.snapshot_date), desc(PortfolioSnapshotRow.id))
        .limit(1)
    ).scalar_one_or_none()


def _latest_household_budget_report(session: Session, user_id: str) -> dict[str, Any] | None:
    """Parse the response_text from the freshest household_budget
    agent_report.

    Tolerates the ```json fenced``` wrapper the analyst's prompt currently
    produces and a couple of common variants. Returns None when no row
    exists or the body isn't parseable.
    """
    row = session.execute(
        select(AgentReport)
        .where(AgentReport.user_id == user_id, AgentReport.agent_role == "household_budget")
        .order_by(desc(AgentReport.id))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    text = (row.response_text or "").strip()
    if not text:
        return None
    # Strip ```json fences and a leading "json\n" if present.
    if text.startswith("```"):
        # Drop the first fence line and the trailing fence.
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: try to find the first { and last } and parse that.
        lo, hi = text.find("{"), text.rfind("}")
        if lo >= 0 and hi > lo:
            try:
                return json.loads(text[lo : hi + 1])
            except json.JSONDecodeError:
                return None
        return None


def _latest_draft_with_targets(
    session: Session, user_id: str
) -> PlanVersion | None:
    """Return the freshest plan_version with non-null horizon_medium_json
    for this user, scanning across draft / superseded / current / accepted.
    """
    return session.execute(
        select(PlanVersion)
        .where(
            PlanVersion.user_id == user_id,
            PlanVersion.horizon_medium_json.isnot(None),
        )
        .order_by(desc(PlanVersion.id))
        .limit(1)
    ).scalar_one_or_none()


def _load_user_context_yaml(session: Session, user_id: str) -> dict[str, Any]:
    """Return a single dict merged from identity_yaml + goals_yaml +
    constraints_yaml. Each YAML block is its own top-level mapping.

    On parse failure, the offending block is skipped (other blocks still
    populate the dict).
    """
    row = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for blob in (row.identity_yaml, row.goals_yaml, row.constraints_yaml):
        if not blob:
            continue
        try:
            parsed = yaml.safe_load(blob)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            out.update(parsed)
    return out


# ---------------------------------------------------------------------------
# Per-block computers.
# ---------------------------------------------------------------------------


def _resolve_fx_usd_nis(
    *,
    snapshot: PortfolioSnapshotRow | None,
    user_ctx: dict[str, Any],
) -> tuple[float, str]:
    """Pick the freshest USD/NIS rate available.

    Preference order:
      1. portfolio_snapshots.fx_usd_nis (parsed from latest TSV).
      2. user_context.identity_yaml::fx_rate.usd_nis (last manually-set rate).
      3. ``DEFAULT_FX_USD_NIS`` fallback.

    Returns ``(rate, source_label)``.
    """
    if snapshot and snapshot.fx_usd_nis:
        return float(snapshot.fx_usd_nis), f"snapshot {snapshot.snapshot_date}"
    fx_block = user_ctx.get("fx_rate") if isinstance(user_ctx.get("fx_rate"), dict) else None
    if fx_block and fx_block.get("usd_nis"):
        try:
            return float(fx_block["usd_nis"]), "identity_yaml.fx_rate.usd_nis"
        except (TypeError, ValueError):
            pass
    return DEFAULT_FX_USD_NIS, f"default {DEFAULT_FX_USD_NIS}"


def _net_worth(
    *, snapshot: PortfolioSnapshotRow | None, fx_usd_nis: float
) -> tuple[float | None, float | None]:
    """True net worth = investable holdings + real-estate NET EQUITY.

    The snapshot's ``total_usd_value_k`` carries only the legacy "$69K Aborad"
    real-estate stub. Real net worth replaces that with the full per-property
    net equity (Home − Loan, FX-converted) — the same figure the Real-estate
    panel shows — so net worth and the panel agree.
    """
    if snapshot is None:
        return None, None
    from types import SimpleNamespace

    from argosy.services.real_estate_equity import compute_real_estate_equity

    try:
        totals = json.loads(snapshot.totals_json or "{}")
    except json.JSONDecodeError:
        totals = {}
    total_usd_k = totals.get("total_usd_value_k")
    if total_usd_k is None:
        return None, None
    base_k = float(total_usd_k)

    # Swap the legacy real-estate stub (the "$69K Aborad" row in the position
    # block) for the full per-property net equity — so net worth includes real
    # estate properly and matches the Real-estate panel.
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        positions = []
    re_stub_k = sum(
        float(p.get("usd_value_k") or 0.0)
        for p in positions
        if isinstance(p, dict) and (p.get("asset_type") or "").lower() == "real estate"
    )
    re_net_k = 0.0
    try:
        re_rows = json.loads(snapshot.real_estate_json or "[]")
        re_objs = [SimpleNamespace(**r) for r in re_rows if isinstance(r, dict)]
        if re_objs:
            eq = compute_real_estate_equity(
                re_objs,
                fx_usd_nis=getattr(snapshot, "fx_usd_nis", None) or fx_usd_nis,
                fx_usd_eur=getattr(snapshot, "fx_usd_eur", None),
            )
            re_net_k = eq.total_net_usd_k
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    usd = (base_k - re_stub_k + re_net_k) * 1000.0
    if usd <= 0:
        return None, None
    return usd * fx_usd_nis, usd


def _retirement(
    *,
    session: Session,
    user_id: str,
    snapshot: PortfolioSnapshotRow | None,
    budget_report: dict[str, Any] | None,
    fx_usd_nis: float,
    current_age: int,
    current_age_inferred: bool,
) -> RetirementBlock:
    nw_nis, nw_usd = _net_worth(snapshot=snapshot, fx_usd_nis=fx_usd_nis)
    burn = None
    income = None
    missing: list[str] = []
    if budget_report is not None:
        burn = budget_report.get("monthly_burn_nis")
        income = budget_report.get("monthly_income_nis")
    if burn is None:
        missing.append("monthly_burn_nis: no household_budget agent_report")
    if income is None:
        missing.append("monthly_income_nis: no household_budget agent_report")

    burn_f = float(burn) if burn is not None else None
    income_f = float(income) if income is not None else None
    surplus = (
        income_f - burn_f if (income_f is not None and burn_f is not None) else None
    )
    annual_expenses = burn_f * 12.0 if burn_f is not None else None
    # FI capital target + SWR bind to the deterministic fi_methodology (the SAME
    # source as /plan + /retirement: permanent-equivalent spend ÷ 3.0% real SWR
    # + reserve), NOT a naive T12 ÷ 3.5% (age-coherence / output-trust doctrine).
    # Falls back to the naive number only when the methodology can't be sourced.
    swr_used = SWR
    target_portfolio = annual_expenses / SWR if annual_expenses is not None else None
    try:
        from argosy.services.fi_methodology import compute_fi_target
        _fi = compute_fi_target(session, user_id=user_id)
        if _fi is not None and _fi.fi_total_capital_nis:
            target_portfolio = float(_fi.fi_total_capital_nis)
            swr_used = float(_fi.swr_real_pct)
    except Exception:  # noqa: BLE001 — keep the naive fallback
        pass

    # Retirement age binds to the CANONICAL sequence-aware Monte-Carlo answer
    # (age-coherence 1b): the earliest age the base-case MC clears 90% solvency
    # with the finite-liability reserve earmarked — the SAME source as /plan +
    # /retirement, so no surface contradicts. Each scenario uses its own real
    # return as the central μ. Replaces the deterministic years-to-target that
    # reported the current age (43). Per-scenario target_portfolio stays the FI
    # capital implied by that scenario's draw.
    scenarios: list[ScenarioCard] = []
    for name, r in get_scenario_returns().items():
        y2t: float | None = None
        target_age: int | None = None
        try:
            from argosy.services.retirement.retirement_plan import (
                RetirementAssumptions,
                canonical_feasible_dual_track,
            )
            # Honest dual-track basis (sigma-glide + NVDA CGT + PV reserve + 10%
            # interim tax + healthcare-in-central-spend), this scenario's real μ.
            canon = canonical_feasible_dual_track(
                session=session, user_id=user_id, target_p_solvent=0.90,
                assumptions=RetirementAssumptions(mu_real_typical=r, n_paths=800),
            )
            if canon.earliest_feasible_age is not None:
                target_age = int(round(canon.earliest_feasible_age))
                y2t = max(0.0, canon.earliest_feasible_age - current_age)
        except Exception:  # noqa: BLE001 — defensive; leave age None (not feasible)
            target_age = None
        scenarios.append(
            ScenarioCard(
                name=name,
                real_return=r,
                years_to_target=y2t,
                target_age=target_age,
                target_portfolio_nis=target_portfolio,
            )
        )

    trajectory: list[TrajectoryPoint] = []
    if nw_nis is not None:
        annual_contrib = (surplus or 0.0) * 12.0
        curves: dict[str, list[float]] = {}
        for name, r in get_scenario_returns().items():
            curves[name] = project_wealth_curve(
                starting_portfolio=nw_nis,
                annual_contribution=annual_contrib,
                real_return=r,
                years=PROJECTION_YEARS,
            )
        for t in range(PROJECTION_YEARS + 1):
            trajectory.append(
                TrajectoryPoint(
                    year=t,
                    bear=curves["bear"][t],
                    conservative=curves["conservative"][t],
                    typical=curves["typical"][t],
                )
            )
    else:
        missing.append("trajectory: no portfolio snapshot")

    return RetirementBlock(
        net_worth_nis=nw_nis,
        net_worth_usd=nw_usd,
        monthly_burn_nis=burn_f,
        monthly_income_nis=income_f,
        monthly_surplus_nis=surplus,
        annual_expenses_nis=annual_expenses,
        target_portfolio_nis=target_portfolio,
        swr_rate=swr_used,
        current_age=current_age,
        current_age_inferred=current_age_inferred,
        scenarios=scenarios,
        trajectory=trajectory,
        missing_reasons=missing,
    )


def _cash_runway(
    *,
    snapshot: PortfolioSnapshotRow | None,
    burn_nis: float | None,
    fx_usd_nis: float,
) -> CashRunwayBlock:
    if snapshot is None:
        return CashRunwayBlock(
            cash_nis=None,
            sgov_nis=None,
            defensive_total_nis=None,
            months_of_runway=None,
            missing_reasons=["no portfolio snapshot"],
        )
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        positions = []

    cash_nis = 0.0
    sgov_usd_k = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        v_k = p.get("usd_value_k") or 0.0
        sym = (p.get("symbol") or "").upper()
        atype = (p.get("asset_type") or "").lower()
        currency = (p.get("currency") or "").upper()
        if atype == "real estate":
            # The "Aborad" property row has symbol "-"; without this guard the
            # sym=="-" cash heuristic below would count illiquid real estate as
            # cash and inflate the runway.
            continue
        if sym == "SGOV":
            sgov_usd_k += float(v_k)
            continue
        if atype == "cash" or sym == "-":
            # Both NIS- and USD-denominated cash are normalised to
            # usd_value_k in the snapshot; convert to NIS via FX once.
            if currency == "NIS":
                # current_value_local is NIS for NIS-denominated cash;
                # prefer it when present to avoid the round-trip.
                local = p.get("current_value_local")
                if local is not None:
                    cash_nis += float(local)
                    continue
            cash_nis += float(v_k) * 1000.0 * fx_usd_nis
    sgov_nis = sgov_usd_k * 1000.0 * fx_usd_nis
    defensive = cash_nis + sgov_nis
    months: float | None = None
    missing: list[str] = []
    if burn_nis is None or burn_nis <= 0:
        missing.append("burn unknown: cannot compute runway")
    else:
        months = defensive / burn_nis
    return CashRunwayBlock(
        cash_nis=cash_nis,
        sgov_nis=sgov_nis,
        defensive_total_nis=defensive,
        months_of_runway=months,
        missing_reasons=missing,
    )


def _concentration(
    *,
    snapshot: PortfolioSnapshotRow | None,
    plan: PlanVersion | None,
    symbol: str = "NVDA",
) -> tuple[ConcentrationBlock, float | None, str | None]:
    """Return the concentration block plus the (target_pct, target_source)
    pair so the caller can echo it in the top-level Assumptions block.
    """
    missing: list[str] = []
    current_pct: float | None = None
    if snapshot is None:
        missing.append("no portfolio snapshot")
    else:
        try:
            positions = json.loads(snapshot.positions_json or "[]")
            totals = json.loads(snapshot.totals_json or "{}")
        except json.JSONDecodeError:
            positions = []
            totals = {}
        total_usd_k = totals.get("total_usd_value_k") or 0.0
        sym_usd_k = 0.0
        for p in positions:
            if isinstance(p, dict) and (p.get("symbol") or "").upper() == symbol:
                sym_usd_k += float(p.get("usd_value_k") or 0.0)
        if total_usd_k > 0:
            current_pct = (sym_usd_k / float(total_usd_k)) * 100.0
        else:
            missing.append("snapshot has no total_usd_value_k")

    target_pct: float | None = None
    target_source: str | None = None
    if plan is not None and plan.horizon_medium_json:
        try:
            horizon = json.loads(plan.horizon_medium_json)
        except json.JSONDecodeError:
            horizon = {}
        for t in horizon.get("targets", []) if isinstance(horizon, dict) else []:
            if not isinstance(t, dict):
                continue
            label = (t.get("label") or "").upper()
            unit = (t.get("unit") or "").lower()
            if symbol in label and unit in ("pct_of_portfolio", "pct_of_net_worth"):
                try:
                    target_pct = float(t.get("value"))
                    target_source = f"plan #{plan.id} horizon_medium"
                    break
                except (TypeError, ValueError):
                    continue
    if target_pct is None:
        missing.append(f"{symbol} target_pct: no matching horizon_medium target")

    return (
        ConcentrationBlock(
            symbol=symbol,
            current_pct=current_pct,
            target_pct=target_pct,
            target_source=target_source,
            missing_reasons=missing,
        ),
        target_pct,
        target_source,
    )


def _savings_rate(
    *, burn: float | None, income: float | None
) -> SavingsRateBlock:
    missing: list[str] = []
    rate: float | None = None
    if income is None or burn is None:
        if income is None:
            missing.append("monthly_income_nis missing")
        if burn is None:
            missing.append("monthly_burn_nis missing")
    elif income <= 0:
        missing.append("monthly_income_nis is zero")
    else:
        rate = ((income - burn) / income) * 100.0
    return SavingsRateBlock(
        monthly_income_nis=income,
        monthly_burn_nis=burn,
        rate_pct=rate,
        missing_reasons=missing,
    )


def _fx_exposure(
    *, snapshot: PortfolioSnapshotRow | None, fx_usd_nis: float
) -> FxExposureBlock:
    if snapshot is None:
        return FxExposureBlock(
            buckets=[], usd_pct=None, missing_reasons=["no portfolio snapshot"],
        )
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        positions = []

    by_currency: dict[str, float] = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        cur = (p.get("currency") or "OTHER").upper()
        # Normalise to NIS so the stacked bar is in a single unit.
        local = p.get("current_value_local")
        if cur == "NIS" and local is not None:
            v = float(local)
        else:
            v_k = p.get("usd_value_k") or 0.0
            v = float(v_k) * 1000.0 * fx_usd_nis
        by_currency[cur] = by_currency.get(cur, 0.0) + v

    total = sum(by_currency.values())
    buckets: list[FxBucket] = []
    if total <= 0:
        return FxExposureBlock(
            buckets=[], usd_pct=None,
            missing_reasons=["no positions with positive value"],
        )
    for cur, v in sorted(by_currency.items(), key=lambda kv: -kv[1]):
        buckets.append(FxBucket(currency=cur, value_nis=v, pct=(v / total) * 100.0))
    usd_pct = next((b.pct for b in buckets if b.currency == "USD"), 0.0)
    return FxExposureBlock(buckets=buckets, usd_pct=usd_pct, missing_reasons=[])


def _rsu_income(
    *,
    user_ctx: dict[str, Any],
    snapshot: PortfolioSnapshotRow | None,
    fx_usd_nis: float,
) -> RsuIncomeBlock:
    """Bar chart per quarter for the next 12 months.

    Data lives at ``user_context.identity_yaml::rsu_vest_schedule.quarterly_vests``
    which carries shares + value_usd at a historical NVDA price. We
    prefer the snapshot's NVDA spot price when present so the headline NIS
    figure reflects today's price, falling back to the recorded
    ``value_usd`` when no spot is available.
    """
    sched = user_ctx.get("rsu_vest_schedule")
    if not isinstance(sched, dict):
        return RsuIncomeBlock(
            next_12_months_nis=None, quarters=[], nvda_price_usd=None,
            fx_usd_nis=fx_usd_nis,
            missing_reasons=["no rsu_vest_schedule in identity_yaml"],
        )
    raw_qs = sched.get("quarterly_vests") or []
    if not raw_qs:
        return RsuIncomeBlock(
            next_12_months_nis=None, quarters=[], nvda_price_usd=None,
            fx_usd_nis=fx_usd_nis,
            missing_reasons=["rsu_vest_schedule.quarterly_vests is empty"],
        )

    # Try to look up current NVDA price from the snapshot.
    nvda_price_usd: float | None = None
    if snapshot is not None:
        try:
            positions = json.loads(snapshot.positions_json or "[]")
        except json.JSONDecodeError:
            positions = []
        for p in positions:
            if isinstance(p, dict) and (p.get("symbol") or "").upper() == "NVDA":
                price = p.get("current_price")
                if price:
                    nvda_price_usd = float(price)
                    break

    quarters: list[RsuQuarter] = []
    total_nis = 0.0
    today = date.today()
    cutoff = date(today.year + 1, today.month, today.day)
    for q in raw_qs[:8]:  # cap at 8 entries to be safe; we filter by date below.
        if not isinstance(q, dict):
            continue
        period = str(q.get("period") or "")
        d_raw = q.get("date")
        d_str = str(d_raw) if d_raw is not None else ""
        # Best-effort date parse — entries like "2027-03" parse to first of month.
        d_parsed: date | None = None
        for fmt in ("%Y-%m-%d", "%Y-%m"):
            try:
                d_parsed = datetime.strptime(d_str, fmt).date()
                break
            except ValueError:
                continue
        if d_parsed is None:
            # Skip undated entries.
            continue
        if d_parsed < today or d_parsed > cutoff:
            continue
        shares = float(q.get("shares") or 0)
        if nvda_price_usd is not None:
            value_usd = shares * nvda_price_usd
        else:
            value_usd = float(q.get("value_usd") or 0)
        value_nis = value_usd * fx_usd_nis
        quarters.append(
            RsuQuarter(
                period=period, date=d_str, shares=shares, value_nis=value_nis,
            )
        )
        total_nis += value_nis

    return RsuIncomeBlock(
        next_12_months_nis=total_nis if quarters else None,
        quarters=quarters,
        nvda_price_usd=nvda_price_usd,
        fx_usd_nis=fx_usd_nis,
        missing_reasons=[] if quarters else ["no vests within next 12 months"],
    )


def _estate_exposure(
    *, snapshot: PortfolioSnapshotRow | None, fx_usd_nis: float
) -> EstateExposureBlock:
    """Estimate US-situs holdings exposure relative to the NRA exemption.

    Heuristic: positions with ``location`` containing "schwab" are
    treated as US-domiciled (Schwab US brokerage). This omits the
    edge case of US-domiciled ETFs held in Israeli brokerage accounts
    which would also count as US-situs but are harder to detect; we
    surface that limitation in ``missing_reasons``.
    """
    if snapshot is None:
        return EstateExposureBlock(
            us_situs_usd=None, us_situs_nis=None,
            nra_exemption_usd=US_NRA_ESTATE_EXEMPTION_USD,
            above_exemption_usd=None,
            potential_liability_usd=None,
            potential_liability_nis=None,
            missing_reasons=["no portfolio snapshot"],
        )
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        positions = []
    us_usd = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        loc = (p.get("location") or "").lower()
        if "schwab" in loc:
            us_usd += float(p.get("usd_value_k") or 0.0) * 1000.0
    us_nis = us_usd * fx_usd_nis
    above = max(us_usd - US_NRA_ESTATE_EXEMPTION_USD, 0.0)
    liability_usd = above * US_NRA_ESTATE_RATE
    liability_nis = liability_usd * fx_usd_nis
    return EstateExposureBlock(
        us_situs_usd=us_usd,
        us_situs_nis=us_nis,
        nra_exemption_usd=US_NRA_ESTATE_EXEMPTION_USD,
        above_exemption_usd=above,
        potential_liability_usd=liability_usd,
        potential_liability_nis=liability_nis,
        missing_reasons=[
            "US-situs estimated from Schwab-location holdings only; "
            "US-domiciled ETFs in Israeli brokerage may also count",
        ],
    )


def _compositions(
    *, snapshot: PortfolioSnapshotRow | None, fx_usd_nis: float,
    exclude_nvda: bool = False,
) -> tuple[list[CompositionSlice], list[CompositionSlice]]:
    """Return (asset_class_composition, sector_composition).

    Each composition is a list of ``CompositionSlice`` sorted by absolute
    value descending, with ``pct`` summing to ~100% across the list.
    Holdings within each slice are de-duplicated tickers (e.g. multiple
    VOO lots → one "VOO" entry) sorted alphabetically.

    Positions with non-positive ``usd_value_k`` are skipped — they
    contribute nothing to either composition. Real-estate rows (symbol
    "-", asset_type "Real estate") flow into "Real Estate" / "Other"
    naturally via the classifiers.
    """
    if snapshot is None:
        return [], []
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        return [], []

    # Accumulate by bucket: name -> (total NIS, set of holding labels).
    asset_buckets: dict[str, dict[str, Any]] = {}
    sector_buckets: dict[str, dict[str, Any]] = {}

    for p in positions:
        if not isinstance(p, dict):
            continue
        v_k = p.get("usd_value_k") or 0.0
        try:
            v_k_f = float(v_k)
        except (TypeError, ValueError):
            continue
        if v_k_f <= 0:
            continue

        symbol = (p.get("symbol") or "").strip()
        # Exclude NVDA so its ~61% RSU concentration doesn't flatten every
        # other slice (the same toggle the allocation card uses).
        if exclude_nvda and (
            symbol.upper() == "NVDA"
            or "nvidia" in (p.get("asset_type") or "").lower()
        ):
            continue
        v_nis = v_k_f * 1000.0 * fx_usd_nis

        details = (p.get("details") or "").strip()
        asset_type = p.get("asset_type") or ""

        # Display label preference: ticker, else asset_type ("Cash") or details.
        if symbol and symbol != "-":
            label = symbol
        elif asset_type:
            label = asset_type
        elif details:
            label = details
        else:
            label = "(unlabeled)"

        # The curated instrument reference is the primary authority (keyed
        # off the resolved ticker); it overrides the unreliable source
        # asset_type (e.g. equity ETFs the export labels "REIT"). Raw-field
        # heuristics are the fallback for instruments not in the table.
        ref = instrument_reference.lookup(symbol, details)
        if ref is not None:
            ac = ref.asset_class
            sec = ref.sector
        else:
            ac = _classify_asset_class(asset_type, symbol)
            sec = _classify_sector(symbol, details)

        ab = asset_buckets.setdefault(ac, {"value": 0.0, "holdings": set()})
        ab["value"] += v_nis
        ab["holdings"].add(label)

        sb = sector_buckets.setdefault(sec, {"value": 0.0, "holdings": set()})
        sb["value"] += v_nis
        sb["holdings"].add(label)

    def _finalise(
        buckets: dict[str, dict[str, Any]], order: tuple[str, ...],
    ) -> list[CompositionSlice]:
        total = sum(b["value"] for b in buckets.values())
        if total <= 0:
            return []
        # Sort: known buckets in canonical order first, unknown alphabetical.
        def sort_key(name: str) -> tuple[int, str]:
            try:
                return (order.index(name), name)
            except ValueError:
                return (len(order), name)

        slices: list[CompositionSlice] = []
        for name in sorted(buckets.keys(), key=sort_key):
            b = buckets[name]
            slices.append(
                CompositionSlice(
                    name=name,
                    value_nis=b["value"],
                    pct=(b["value"] / total) * 100.0,
                    holdings=sorted(b["holdings"]),
                )
            )
        # Final sort: by value descending (UI expects "Equity ~84%" first).
        slices.sort(key=lambda s: -s.value_nis)
        return slices

    return (
        _finalise(asset_buckets, _ASSET_CLASS_ORDER),
        _finalise(sector_buckets, _SECTOR_ORDER),
    )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def compute_wealth_dashboard(
    session: Session,
    *,
    user_id: str,
    today: date | None = None,
    exclude_nvda: bool = False,
) -> WealthDashboard:
    """Build the full WealthDashboard for ``user_id`` from the live DB.

    Each block is computed independently; a failure in one (e.g. no
    household_budget agent_report) doesn't bring the others down. The
    UI tolerates ``None`` everywhere and shows "—" with a tooltip
    drawn from the per-block ``missing_reasons``.
    """
    snapshot = _latest_snapshot(session, user_id)
    user_ctx = _load_user_context_yaml(session, user_id)
    budget_report = _latest_household_budget_report(session, user_id)
    plan = _latest_draft_with_targets(session, user_id)

    fx_usd_nis, fx_source = _resolve_fx_usd_nis(snapshot=snapshot, user_ctx=user_ctx)
    # Prefer the current Bank-of-Israel rate (cache) so /portfolio net worth +
    # estate + FX cards match /plan and /retirement — the snapshot fx (2.94) was
    # an erroneous value (codex FX review 2026-06-04). Falls back to the
    # snapshot/identity resolution when BOI is uncached.
    try:
        from datetime import date as _date
        from argosy.services.fx import cache as _fxcache
        fx_usd_nis = float(_fxcache.find_walkback(session, today or _date.today(), "USD", max_days=10))
        fx_source = "boi USD/NIS current (FxRate cache)"
    except Exception:  # noqa: BLE001 — keep the snapshot/identity fallback
        pass

    # current_age resolution (in order of preference):
    #   1. identity_yaml.user_date_of_birth — canonical for the primary user
    #      (Ariel). Note: identity_yaml ALSO carries spouse_date_of_birth
    #      so we must look up by the user_* key explicitly, not blanket
    #      ``date_of_birth``.
    #   2. identity_yaml.date_of_birth (legacy/intake-form key).
    #   3. identity_yaml.user_age_current — integer fallback used by some
    #      intake flows that captured age but not DOB.
    #   4. DEFAULT_CURRENT_AGE.
    dob: str | None = None
    age_override: int | None = None
    age_source: str = ""
    for key in ("user_date_of_birth", "date_of_birth"):
        v = user_ctx.get(key)
        if isinstance(v, str) and v:
            dob = v
            age_source = f"identity_yaml.{key}"
            break
    if dob is None:
        v = user_ctx.get("user_age_current")
        if isinstance(v, (int, float)) and v > 0:
            age_override = int(v)
            age_source = "identity_yaml.user_age_current"
    if age_override is not None:
        current_age = age_override
        age_inferred = False
    else:
        current_age, age_inferred = compute_current_age(dob, today=today)
        if not age_inferred and not age_source:
            age_source = "identity_yaml.user_date_of_birth"

    retirement = _retirement(
        session=session,
        user_id=user_id,
        snapshot=snapshot,
        budget_report=budget_report,
        fx_usd_nis=fx_usd_nis,
        current_age=current_age,
        current_age_inferred=age_inferred,
    )
    cash_runway = _cash_runway(
        snapshot=snapshot, burn_nis=retirement.monthly_burn_nis, fx_usd_nis=fx_usd_nis,
    )
    concentration, target_pct, target_source = _concentration(
        snapshot=snapshot, plan=plan, symbol="NVDA",
    )
    savings_rate = _savings_rate(
        burn=retirement.monthly_burn_nis, income=retirement.monthly_income_nis,
    )
    fx_exposure = _fx_exposure(snapshot=snapshot, fx_usd_nis=fx_usd_nis)
    rsu_income = _rsu_income(
        user_ctx=user_ctx, snapshot=snapshot, fx_usd_nis=fx_usd_nis,
    )
    estate_exposure = _estate_exposure(snapshot=snapshot, fx_usd_nis=fx_usd_nis)
    asset_class_composition, sector_composition = _compositions(
        snapshot=snapshot, fx_usd_nis=fx_usd_nis, exclude_nvda=exclude_nvda,
    )

    assumptions = Assumptions(
        swr_rate=SWR,
        scenario_returns=get_scenario_returns(),
        fx_usd_nis=fx_usd_nis,
        fx_source=fx_source,
        current_age=current_age,
        current_age_source=(
            "default (no date_of_birth)" if age_inferred else (age_source or "identity_yaml")
        ),
        nvda_target_pct=target_pct,
        nvda_target_source=target_source,
        snapshot_date=(
            snapshot.snapshot_date.isoformat()
            if snapshot and snapshot.snapshot_date
            else None
        ),
        plan_version_id=plan.id if plan is not None else None,
    )

    return WealthDashboard(
        user_id=user_id,
        generated_at=datetime.now().isoformat(),
        retirement=retirement,
        cash_runway=cash_runway,
        concentration=concentration,
        savings_rate=savings_rate,
        fx_exposure=fx_exposure,
        rsu_income=rsu_income,
        estate_exposure=estate_exposure,
        asset_class_composition=asset_class_composition,
        sector_composition=sector_composition,
        assumptions=assumptions,
    )


def wealth_dashboard_to_dict(d: WealthDashboard) -> dict[str, Any]:
    """Cheap JSON-friendly serialisation. Used by the route layer."""
    return asdict(d)


__all__ = [
    "WealthDashboard",
    "RetirementBlock",
    "ScenarioCard",
    "TrajectoryPoint",
    "CashRunwayBlock",
    "ConcentrationBlock",
    "SavingsRateBlock",
    "FxExposureBlock",
    "FxBucket",
    "RsuIncomeBlock",
    "RsuQuarter",
    "EstateExposureBlock",
    "CompositionSlice",
    "Assumptions",
    "compute_wealth_dashboard",
    "wealth_dashboard_to_dict",
    "years_to_target",
    "project_wealth_curve",
    "compute_current_age",
    "SWR",
    "get_scenario_returns",
    "DEFAULT_CURRENT_AGE",
    "DEFAULT_FX_USD_NIS",
    "PROJECTION_YEARS",
    "MAX_SOLVE_YEARS",
    "US_NRA_ESTATE_EXEMPTION_USD",
    "US_NRA_ESTATE_RATE",
]
