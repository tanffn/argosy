"""Deployment advisor (P1) — deterministic, plan-bound "deploy this cash" service.

Turns a net-of-tax deploy amount + the current canonical plan + current holdings
into a risk-tiered, estate-annotated BUY list, by wrapping the deterministic
``allocation_engine.cash_only_deploy`` and annotating each buy. P1 is plan-bound
only (every buy is the ``core`` tier); medium/high tactical tiers + an agent-sized
reserve arrive in P3/P4/P2 respectively. See
docs/superpowers/plans/2026-06-12-deployment-advisor.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

TierName = Literal["reserve", "core", "medium", "high"]
# Carve order: reserve first, then core, then tactical tiers.
TIER_NAMES: tuple[TierName, ...] = ("reserve", "core", "medium", "high")

EstateStatus = Literal[
    "estate_safe", "us_situs_sanctioned", "us_situs_exposed", "unstamped"
]


@dataclass(frozen=True)
class EstateTag:
    domicile: str | None
    status: EstateStatus
    note: str


@dataclass(frozen=True)
class DeploymentLine:
    symbol: str
    type: str            # "ETF" | "Stock" | "Gold ETC" | "T-bill" ...
    amount_usd: float
    timing: str          # P1: always "now"
    is_new: bool         # NEW vs already-held
    tier: TierName
    horizon: str         # "10yr+" | "5-10yr" | "<=5yr"
    estate: EstateTag
    cap_note: str
    net_of_tax_caveat: str
    rationale: str
    cites: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeploymentTier:
    name: TierName
    cap_pct: float       # advisory ceiling for tactical tiers; 0 for reserve in P1
    lines: tuple[DeploymentLine, ...] = ()

    @property
    def total_usd(self) -> float:
        return round(sum(l.amount_usd for l in self.lines), 2)


@dataclass(frozen=True)
class DeploymentPlan:
    deploy_amount_usd: float
    as_of: date
    tiers: tuple[DeploymentTier, ...]
    us_situs_total_usd: float
    market_context_age: str | None   # P1: None ("plan-only"); P2 fills cached-read age
    caveats: tuple[str, ...]
    note: str = ""

    @property
    def deployed_total_usd(self) -> float:
        return round(sum(t.total_usd for t in self.tiers), 2)


# Advisory tier ceilings (% of post-reserve deploy capital). Enforced only once
# the tactical (medium/high) tiers are populated (P3/P4). In P1 only `core` is
# filled, so core absorbs the remainder — the safe plan-bound default.
DEPLOY_TIER_CAPS: dict[str, float] = {"core": 70.0, "medium": 25.0, "high": 5.0}


SANCTIONED_US_SITUS: frozenset[str] = frozenset({"NVDA"})


def build_estate_map(doc) -> dict[str, EstateTag]:
    """Per-symbol :class:`EstateTag` for every instrument in the canonical doc.

    Reuses ``validate_instrument_domicile`` for the RED/YELLOW verdict, then maps
    each symbol to a deploy-surface estate status. Symbols with no violation and a
    non-US domicile are ``estate_safe``; sanctioned US-situs (NVDA) is
    ``us_situs_sanctioned``.
    """
    from argosy.services.target_allocation_doc import validate_instrument_domicile

    violations = {
        v.symbol: v for v in validate_instrument_domicile(
            doc, non_us_person=True, sanctioned_us_situs=SANCTIONED_US_SITUS
        )
    }
    out: dict[str, EstateTag] = {}
    for cls in doc.classes:
        for inst in cls.instruments:
            sym = inst.symbol
            dom = inst.domicile
            v = violations.get(sym)
            if v is not None and v.severity == "RED":
                status: EstateStatus = "us_situs_exposed"
                note = v.reason
            elif v is not None and v.severity == "YELLOW":
                status, note = "unstamped", v.reason
            elif sym in SANCTIONED_US_SITUS:
                status, note = "us_situs_sanctioned", "sanctioned US-situs sleeve (NVDA)"
            else:
                status, note = "estate_safe", f"non-US-situs ({dom})"
            out[sym] = EstateTag(domicile=dom, status=status, note=note)
    return out


def classify_tier(*, kind: str, symbol: str, is_plan_instrument: bool) -> TierName:
    """Assign a deploy line to a risk tier.

    P1 rule: a buy of a canonical-plan instrument (UCITS/cap/glide gap-fill from
    ``cash_only_deploy``) is plan-bound -> ``core``. A buy of a symbol NOT in the
    plan is a tactical deviation -> ``medium`` (the screen that would surface
    these arrives in P3/P4; cash_only_deploy emits none in P1).
    """
    if is_plan_instrument:
        return "core"
    return "medium"


# Decision 8: the entered amount is already net of Israeli CGT — Argosy models no
# holdback. This is a per-line reminder only, never a sizing input.
NET_OF_TAX_CAVEAT = (
    "Amount assumed net of Israeli capital gains tax (CGT); confirm deployable cash before ordering."
)


def cap_note_for(doc, *, symbol: str) -> str:
    """One-line cap/class context for a deploy line.

    Names the canonical class the buy fills and, for the sanctioned NVDA sleeve,
    surfaces the plan's NVDA cap. The correlated-exposure cap (NVDA/semis/AI) is P4.
    """
    for cls in doc.classes:
        if any(inst.symbol == symbol for inst in cls.instruments):
            if symbol in SANCTIONED_US_SITUS:
                return f"fills {cls.label}; NVDA cap {doc.nvda_cap_pct:.0f}% of book"
            return f"fills {cls.label}"
    return "not in canonical plan (tactical)"


# Default hold-horizon by tier (decision 6; user override is P4).
_TIER_HORIZON: dict[str, str] = {
    "reserve": "<=1yr", "core": "10yr+", "medium": "5-10yr", "high": "<=5yr"
}

_CAVEATS: tuple[str, ...] = (
    NET_OF_TAX_CAVEAT,
    "Single-name US-situs holdings carry US estate exposure above the $60k "
    "non-resident exemption; estate status is shown per line.",
)


def _instrument_type(doc, symbol: str) -> str:
    """Coarse instrument type for the SYMBOL|TYPE column."""
    if symbol in SANCTIONED_US_SITUS:
        return "Stock"
    return "ETF"


def assemble_deployment_plan(
    *, doc, holdings: dict[str, float], deploy_amount_usd: float, as_of: date,
) -> DeploymentPlan:
    """Build the P1 deploy plan: plan-bound ``cash_only_deploy`` buys, each
    annotated with tier/estate/cap/tax/horizon, grouped into tiers that sum to
    ``deploy_amount_usd``. P1: reserve=0, medium/high empty, core = full amount.
    """
    if doc is None:
        empty = tuple(DeploymentTier(n, DEPLOY_TIER_CAPS.get(n, 0.0)) for n in TIER_NAMES)
        return DeploymentPlan(
            deploy_amount_usd=deploy_amount_usd, as_of=as_of, tiers=empty,
            us_situs_total_usd=0.0, market_context_age=None, caveats=_CAVEATS,
            note="No current canonical plan — accept a plan first.",
        )

    from argosy.services.allocation_engine import cash_only_deploy

    estate_map = build_estate_map(doc)
    plan_symbols = set(estate_map)
    candidates = cash_only_deploy(doc, holdings, deploy_amount_usd, as_of=as_of)

    core_lines: list[DeploymentLine] = []
    us_situs_total = 0.0
    for cand in candidates:
        for leg in cand.legs:
            if leg.side != "BUY":
                continue
            sym = leg.symbol
            is_plan = sym in plan_symbols
            tier = classify_tier(kind=cand.kind, symbol=sym, is_plan_instrument=is_plan)
            estate = estate_map.get(
                sym, EstateTag(domicile=None, status="unstamped", note="not in plan"))
            amt = round(abs(leg.notional_usd), 2)
            if estate.status in {"us_situs_exposed", "us_situs_sanctioned"}:
                us_situs_total += amt
            line = DeploymentLine(
                symbol=sym, type=_instrument_type(doc, sym), amount_usd=amt,
                timing="now", is_new=(sym not in holdings or holdings.get(sym, 0.0) == 0.0),
                tier=tier, horizon=_TIER_HORIZON[tier], estate=estate,
                cap_note=cap_note_for(doc, symbol=sym),
                net_of_tax_caveat=NET_OF_TAX_CAVEAT, rationale=cand.rationale,
                cites=cand.cites,
            )
            # P1: only core is populated; a non-core classification would be a
            # tactical line cash_only_deploy should never emit. Keep it in core
            # but the tier label stays honest.
            core_lines.append(line)

    tiers = (
        DeploymentTier("reserve", 0.0, ()),
        DeploymentTier("core", DEPLOY_TIER_CAPS["core"], tuple(core_lines)),
        DeploymentTier("medium", DEPLOY_TIER_CAPS["medium"], ()),
        DeploymentTier("high", DEPLOY_TIER_CAPS["high"], ()),
    )
    return DeploymentPlan(
        deploy_amount_usd=round(deploy_amount_usd, 2), as_of=as_of, tiers=tiers,
        us_situs_total_usd=round(us_situs_total, 2), market_context_age=None,
        caveats=_CAVEATS,
        note=("Plan-only deploy (P1): live market context and tactical sleeves "
              "arrive in later phases."),
    )
