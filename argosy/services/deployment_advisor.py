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
    type: str            # P1 emits "ETF" | "Stock" only; richer types in P3
    amount_usd: float
    timing: str          # P1: always "now"
    is_new: bool         # NEW vs already-held in the aggregate tradeable book
    tier: TierName
    horizon: str         # "10yr+" | "5-10yr" | "<=5yr"
    estate: EstateTag
    cap_note: str
    net_of_tax_caveat: str
    rationale: str
    cites: tuple[str, ...] = ()
    # Current aggregate (whole-book, cross-account) holding value of this symbol,
    # so the NEW/held call is auditable. is_new == (held_value_usd <= 0).
    held_value_usd: float = 0.0
    # P2 market-aware pacing rationale. Empty string when market_context is None
    # (P1 behavior preserved). Set by pace_for_line when context is supplied.
    pace_rationale: str = ""


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
    # Estate exposure of the PLANNED BUYS only (not whole-book), split so the
    # sanctioned NVDA sleeve is never conflated with real RED estate exposure.
    us_situs_exposed_usd: float        # unsanctioned US-domiciled buys (RED)
    us_situs_sanctioned_usd: float     # the sanctioned NVDA sleeve
    # Cash the engine could NOT place against plan targets — surfaced explicitly
    # so nothing is silently lost. deployed_total + remainder == deploy_amount.
    undeployed_remainder_usd: float
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


def _remainder_caveat(remainder_usd: float) -> str:
    """Caveat shown when the engine could not place the full deploy amount."""
    return (
        f"${remainder_usd:,.0f} could not be placed against current plan targets "
        f"and is shown as an undeployed remainder (not silently dropped)."
    )


def _instrument_type(doc, symbol: str) -> str:
    """Coarse instrument type for the SYMBOL|TYPE column.

    P1 only distinguishes the sanctioned single-stock sleeve (NVDA) from
    everything else (which is UCITS ETFs in the current plan). This is a P1
    stub — it CANNOT yet emit "Gold ETC" / "T-bill" etc.
    TODO(P3): derive the real type from ``AllocationInstrument`` once it carries
    an ``asset_type`` (gold ETC + bond/T-bill classes arrive in P3).
    """
    if symbol in SANCTIONED_US_SITUS:
        return "Stock"
    return "ETF"


# ---------------------------------------------------------------------------
# P2 T6: market-aware per-line pacing
# ---------------------------------------------------------------------------

# Minimum per-installment ticket — an execution floor (not a smoothing rule):
# a DCA window is capped so each weekly chunk is at least this size.
DCA_MIN_INSTALLMENT_USD: float = 1_000.0


def _snap_value(snapshot, key: str, default: float) -> float:
    """Read a float from a market-context snapshot, tolerating either a plain
    float or a ``(value, DataFreshness)`` tuple, with a default when absent."""
    raw = snapshot.get(key) if snapshot else None
    if raw is None:
        return default
    return float(raw[0]) if isinstance(raw, tuple) else float(raw)


def pace_for_line(
    amount_usd: float, market_context, *, book_usd: float, tranche_usd: float,
) -> tuple[str, str]:
    """Return ``(timing, pace_rationale)`` for one deploy line.

    Codex-reviewed rule (codex_pacing_verdict): **lump-now is the default**;
    DCA is a bounded regret-control concession used ONLY when the program is
    material (>=0.5% of the post-deploy book), the market is stretched
    (S&P > +8% vs trend), AND volatility is elevated (VIX >= 20). High VIX alone
    never slows buying; a below-trend market never triggers DCA — for a
    long-hold retirement-maximizing investor that points to FASTER deployment.
    The materiality boundary is % of book (not a fixed-dollar floor). FX
    conversion is paced WITH the equity buy (no separate currency bet).
    """
    if amount_usd <= 0:
        return ("now", "no positive buy amount")
    snap = market_context.snapshot
    vix = _snap_value(snap, "vix", 20.0)
    sp_vs_trend_pct = _snap_value(snap, "sp_vs_trend_pct", 0.0)
    scope_pct = (tranche_usd / book_usd) if book_usd > 0 else 0.0

    if scope_pct < 0.005:
        return ("now", "immaterial vs book — timing risk not retirement-material")
    if sp_vs_trend_pct <= 8.0:
        return ("now", f"market not stretched (S&P {sp_vs_trend_pct:+.1f}% vs trend) — lump-now is the EV default")
    if vix < 20.0:
        return ("now", f"extended but VIX={vix:.0f}<20 — not turbulent enough to justify DCA")

    # Stretched AND volatile: DCA concession. N grows with stretch, vol, and size.
    if sp_vs_trend_pct <= 15.0 and vix < 30.0:
        n = 2
    elif sp_vs_trend_pct <= 15.0 or vix < 30.0:
        n = 4
    else:
        n = 6
    if scope_pct >= 0.05:
        n += 4
    elif scope_pct >= 0.02:
        n += 2
    n = min(n, 8)
    # Execution floor: don't slice below the min ticket.
    n = max(1, min(n, int(amount_usd // DCA_MIN_INSTALLMENT_USD) or 1))

    if n == 1:
        return ("now", f"stretched (S&P {sp_vs_trend_pct:+.1f}%, VIX {vix:.0f}) but line too small to slice")
    return (
        f"DCA {n}wk",
        f"stretched (S&P {sp_vs_trend_pct:+.1f}% vs trend) + elevated VIX {vix:.0f}; "
        f"spread over {n} equal weekly buys (FX converted with each)",
    )


def assemble_deployment_plan(
    *, doc, holdings: dict[str, float], deploy_amount_usd: float, as_of: date,
    market_context=None,
) -> DeploymentPlan:
    """Build the deploy plan: plan-bound ``cash_only_deploy`` buys, each
    annotated with tier/estate/cap/tax/horizon/pacing, grouped into tiers that
    sum to ``deploy_amount_usd``.

    P1 (``market_context=None``): reserve=0, medium/high empty, core = full
    amount; all lines get ``timing="now"`` and ``pace_rationale=""``.
    P2 (``market_context`` provided): lines are paced via ``pace_for_line``;
    staleness is surfaced as a caveat.
    """
    amount = round(deploy_amount_usd, 2)

    # Resolve market_context_age up front.
    mca: str | None = market_context.overall_age_label if market_context is not None else None

    if doc is None:
        empty = tuple(DeploymentTier(n, DEPLOY_TIER_CAPS.get(n, 0.0)) for n in TIER_NAMES)
        return DeploymentPlan(
            deploy_amount_usd=amount, as_of=as_of, tiers=empty,
            us_situs_exposed_usd=0.0, us_situs_sanctioned_usd=0.0,
            undeployed_remainder_usd=amount, market_context_age=mca,
            caveats=_CAVEATS + (_remainder_caveat(amount),) if amount > 0.005 else _CAVEATS,
            note="No current canonical plan — accept a plan first.",
        )

    from argosy.services.allocation_engine import cash_only_deploy

    estate_map = build_estate_map(doc)
    plan_symbols = set(estate_map)
    candidates = cash_only_deploy(doc, holdings, deploy_amount_usd, as_of=as_of)
    # Post-deploy investable book — the materiality denominator for pacing.
    book_usd = round(sum(holdings.values()) + amount, 2)

    core_lines: list[DeploymentLine] = []
    exposed_total = 0.0
    sanctioned_total = 0.0
    for cand in candidates:
        for leg in cand.legs:
            if leg.side != "BUY":
                continue
            # Fail loud: this path is cash-only. A BUY funded by trim proceeds (or
            # any non-cash source) would miscount non-cash buys against the entered
            # cash amount — never silently absorb it (trust doctrine). Read the
            # required field directly so a malformed leg raises, not slips through.
            if leg.funding_source != "cash":
                raise ValueError(
                    f"deploy-cash expects cash-funded BUY legs only; got "
                    f"{leg.symbol!r} funded by {leg.funding_source!r} (kind={cand.kind})"
                )
            sym = leg.symbol
            is_plan = sym in plan_symbols
            tier = classify_tier(kind=cand.kind, symbol=sym, is_plan_instrument=is_plan)
            estate = estate_map.get(
                sym, EstateTag(domicile=None, status="unstamped", note="not in plan"))
            amt = round(abs(leg.notional_usd), 2)
            if estate.status == "us_situs_exposed":
                exposed_total += amt
            elif estate.status == "us_situs_sanctioned":
                sanctioned_total += amt
            held_value = round(float(holdings.get(sym, 0.0)), 2)
            if market_context is not None:
                timing, p_rationale = pace_for_line(
                    amt, market_context, book_usd=book_usd, tranche_usd=amount)
            else:
                timing, p_rationale = "now", ""
            line = DeploymentLine(
                symbol=sym, type=_instrument_type(doc, sym), amount_usd=amt,
                timing=timing, is_new=(held_value <= 0.0),
                tier=tier, horizon=_TIER_HORIZON[tier], estate=estate,
                cap_note=cap_note_for(doc, symbol=sym),
                net_of_tax_caveat=NET_OF_TAX_CAVEAT, rationale=cand.rationale,
                cites=cand.cites, held_value_usd=held_value,
                pace_rationale=p_rationale,
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
    deployed = round(sum(t.total_usd for t in tiers), 2)
    if deployed - amount > 0.01:
        # Over-deploy is never allowed — the engine water-fills to <= cash.
        raise ValueError(
            f"deploy-cash over-allocated: buys total {deployed} > amount {amount}"
        )
    remainder = round(max(0.0, amount - deployed), 2)
    caveats = _CAVEATS
    # Surface the caveat only for a MATERIAL remainder; sub-dollar drift is just
    # pro-rata rounding noise (the exact figure is still on undeployed_remainder_usd).
    if remainder >= 1.0:
        caveats = caveats + (_remainder_caveat(remainder),)
    # P2: loud staleness caveat when any context feed is stale.
    if market_context is not None and market_context.is_any_stale:
        caveats = caveats + (
            f"WARNING: market context data is stale (age: {mca}). "
            "Pacing decisions are based on potentially outdated market data — "
            "refresh market context before executing.",
        )
    if market_context is None:
        note = ("Plan-only deploy (P1): live market context and tactical sleeves "
                "arrive in later phases.")
    else:
        note = f"Market-aware deploy (P2): context age {mca}."
    return DeploymentPlan(
        deploy_amount_usd=amount, as_of=as_of, tiers=tiers,
        us_situs_exposed_usd=round(exposed_total, 2),
        us_situs_sanctioned_usd=round(sanctioned_total, 2),
        undeployed_remainder_usd=remainder, market_context_age=mca,
        caveats=caveats,
        note=note,
    )
