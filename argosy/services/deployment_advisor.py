"""Deployment advisor (P1) — deterministic, plan-bound "deploy this cash" service.

Turns a net-of-tax deploy amount + the current canonical plan + current holdings
into a risk-tiered, estate-annotated BUY list, by wrapping the deterministic
``allocation_engine.cash_only_deploy`` and annotating each buy. P1 is plan-bound
only (every buy is the ``core`` tier); medium/high tactical tiers + an agent-sized
reserve arrive in P3/P4/P2 respectively. See
docs/superpowers/plans/2026-06-12-deployment-advisor.md.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    # Item D — dry-powder earmark subtracted from deployable cash (0 when absent).
    # Conservation: deploy_amount + discovery_reserve + (cash_total - deploy_amount
    # - discovery_reserve if pre-split) … see ``cash_total_usd``.
    # Invariant when cash_total_usd is set:
    #   deploy_amount_usd + discovery_reserve_usd + undeployed_remainder_usd
    #   + (optional buys already in deploy_amount accounting)
    # Practically: cash_total = deploy_amount(=post-reserve attempt) + discovery_reserve
    # and deploy_amount = deployed_buys + undeployed_remainder.
    discovery_reserve_usd: float = 0.0
    cash_total_usd: float | None = None

    @property
    def deployed_total_usd(self) -> float:
        return round(sum(t.total_usd for t in self.tiers), 2)


# Advisory tier ceilings (% of post-reserve deploy capital). Enforced only once
# the tactical (medium/high) tiers are populated (P3/P4). In P1 only `core` is
# filled, so core absorbs the remainder — the safe plan-bound default.
DEPLOY_TIER_CAPS: dict[str, float] = {"core": 70.0, "medium": 25.0, "high": 5.0}


SANCTIONED_US_SITUS: frozenset[str] = frozenset({"NVDA"})


def _substitute_estate_tag(symbol: str) -> EstateTag:
    """Estate tag for a held substitute the engine tops up (not a plan instrument,
    so it isn't in the plan's estate map). Classified from the curated instrument
    reference: an estate-safe substitute is the only kind the topup path emits, but
    fall back to ``us_situs_exposed`` if the reference marks it unsafe, and
    ``unstamped`` when the symbol is uncurated."""
    from argosy.services.instrument_reference import lookup

    ref = lookup(symbol)
    if ref is None:
        return EstateTag(domicile=None, status="unstamped", note="not in reference")
    if ref.estate_safe:
        return EstateTag(domicile=ref.region, status="estate_safe",
                         note="held substitute covering a plan sleeve")
    return EstateTag(domicile=ref.region, status="us_situs_exposed",
                     note="US-situs held substitute")


def authored_estate_exposure(buys, doc=None) -> tuple[float, float]:
    """(us_situs_exposed_usd, us_situs_sanctioned_usd) recomputed from the
    AUTHORED buys — the allocation the team actually approved — never from the
    legacy deterministic tier list. Situs comes from per-instrument FACTS
    (curated instrument reference; plan-doc stamped domicile as fallback), not
    from the author's claimed weights. Uncurated symbols with no stamped
    domicile count as EXPOSED (conservative: the estate tail is never hidden
    by a missing table row)."""
    from argosy.services.instrument_reference import estate_safe_for

    doc_domicile: dict[str, str] = {}
    for c in getattr(doc, "classes", []) or []:
        for instr in getattr(c, "instruments", []) or []:
            d = getattr(instr, "domicile", None)
            sym = (getattr(instr, "symbol", "") or "").upper()
            if sym and d and d != "unknown":
                doc_domicile[sym] = d

    exposed = 0.0
    sanctioned = 0.0
    for b in buys or []:
        sym = (getattr(b, "symbol", "") or "").upper()
        amt = round(float(getattr(b, "amount_usd", 0.0) or 0.0), 2)
        if not sym or amt <= 0.0:
            continue
        if sym in SANCTIONED_US_SITUS:
            sanctioned += amt
            continue
        safe = estate_safe_for(sym)
        if safe is None:
            dom = doc_domicile.get(sym)
            safe = None if dom is None else dom != "US"
        if safe is None or not safe:
            exposed += amt
    return round(exposed, 2), round(sanctioned, 2)


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
# Per-line pacing (no market-timing policy — see pace_for_line)
# ---------------------------------------------------------------------------


def pace_for_line(
    amount_usd: float, market_context, *, book_usd: float, tranche_usd: float,
) -> tuple[str, str]:
    """Return ``(timing, pace_rationale)`` for one deploy line.

    The deterministic layer makes NO market-timing bet. ``lump-now`` is the
    neutral default for a long-hold, retirement-maximizing investor — the engine
    does not decide lump-vs-DCA from VIX / S&P-vs-trend thresholds, because that
    is a tactical investment JUDGMENT that belongs to the fleet (risk officer /
    fund manager) at money-decision time, fed the plan + live data — not to
    hand-coded index/volatility rules. A fleet pacing recommendation, when one
    exists, overrides this default upstream; absent that, deploy now (no timing
    bet). ``market_context`` / ``tranche_usd`` are retained for signature
    compatibility and staleness surfacing, but no longer drive a pacing decision.
    """
    if amount_usd <= 0:
        return ("now", "no positive buy amount")
    return ("now", "")


# Conviction labels differ between the discovery funnel (HIGH/MED/LOW on
# FleetPick/EstimatorVerdict) and the sleeve sizer (HIGH/MEDIUM/LOW). Normalise
# a cached pick's conviction onto the sleeve vocabulary so the EXISTING
# conviction-weight sizing applies unchanged.
_FLEET_CONVICTION_TO_SLEEVE: dict[str, str] = {
    "HIGH": "HIGH", "MED": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW",
}


def _cached_buy_sleeve_candidates(user_id: str):
    """Sleeve candidates from the CACHED discovery graded picks.

    Reads the persisted ScanState via the same accessor the /discovery GET uses
    (``argosy.api.routes.portfolio._load_discovery_state``), keeps only graded
    picks whose ``verdict == "BUY"``, and maps each :class:`FleetPick` onto a
    :class:`SleeveCandidate`. Synchronous + read-only — it never triggers a live
    funnel run. Returns ``None`` (caller falls back to the advisor seeds) when no
    cached BUY picks exist or the read fails.

    A graded discovery pick is a single US-listed name with no stamped UCITS
    domicile, so it maps to the ``single_name`` / ``us_situs=True`` carve-out
    leg of the sleeve (the same convention the seed single-names use).
    """
    from argosy.services.high_potential_sleeve import SleeveCandidate

    try:
        from argosy.api.routes.portfolio import _load_discovery_state

        picks, _estimated, _last = _load_discovery_state(user_id)
    except Exception:  # noqa: BLE001 — cached read is best-effort; fall back to seeds
        return None
    cands: list[SleeveCandidate] = []
    for p in picks:
        if (p.verdict or "").upper() != "BUY":
            continue
        cands.append(SleeveCandidate(
            ticker=p.ticker,
            name=p.ticker,
            vehicle="single_name",
            conviction=_FLEET_CONVICTION_TO_SLEEVE.get(
                (p.conviction or "").upper(), "MEDIUM"),
            thesis=p.thesis_md,
            us_situs=True,
            source="fleet_validated",
        ))
    return cands or None


def _sleeve_estate_tag(cand) -> EstateTag:
    """Estate tag for one sleeve candidate, via the EXISTING estate gate.

    Builds a one-instrument :class:`TargetAllocationDoc` (domicile derived from
    the candidate's ``us_situs`` flag: US single-name -> ``"US"``; UCITS thematic
    -> ``"IE"``) and runs it through :func:`build_estate_map`, which itself calls
    ``validate_instrument_domicile``. No new estate logic: UCITS thematic tags
    ``estate_safe``; an unsanctioned US single-name tags ``us_situs_exposed``.
    """
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, TargetAllocationDoc,
    )

    domicile = "US" if cand.us_situs else "IE"
    mini = TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="high_potential_sleeve",
        classes=[AllocationClassDoc(
            label="High-potential sleeve", snapshot_category="High-potential sleeve",
            sigma_class="us_equity", target_pct=100.0,
            instruments=[AllocationInstrument(
                symbol=cand.ticker, role="primary",
                weight_within_class_pct=100.0, rationale="", domicile=domicile)],
        )],
        glide=[],
    )
    return build_estate_map(mini)[cand.ticker]


def _high_potential_lines(
    *, sleeve_budget_usd: float, user_id: str, market_context, book_usd: float,
    tranche_usd: float, holdings: dict[str, float] | None = None,
) -> tuple[list[DeploymentLine], float, float]:
    """Build the ``high`` tier from the EXISTING high-potential sleeve sizer.

    Feeds CACHED discovery BUY picks (falling back to the seed candidates) into
    ``build_high_potential_sleeve`` and converts each :class:`SleeveAllocation`
    into a ``tier="high"`` :class:`DeploymentLine`, estate-tagged via the
    existing gate. Returns ``(lines, exposed_usd, sanctioned_usd)`` so the
    headline estate split stays consistent with the core path.
    """
    from argosy.services.high_potential_sleeve import build_high_potential_sleeve

    if sleeve_budget_usd <= 0:
        return [], 0.0, 0.0
    candidates = _cached_buy_sleeve_candidates(user_id)
    allocs = build_high_potential_sleeve(sleeve_budget_usd, candidates)
    # Case-normalised current book so a sleeve pick already held reads as a
    # top-up (ADD), not a brand-new position (NEW). Mirrors the core path's
    # held-value lookup — the sleeve must not pretend you don't own a name.
    held_by = {str(k).upper(): float(v) for k, v in (holdings or {}).items()}
    lines: list[DeploymentLine] = []
    exposed = 0.0
    sanctioned = 0.0
    for a in allocs:
        cand = a.candidate
        amt = round(a.amount_usd, 2)
        held_value = round(held_by.get((cand.ticker or "").upper(), 0.0), 2)
        estate = _sleeve_estate_tag(cand)
        if estate.status == "us_situs_exposed":
            exposed += amt
        elif estate.status == "us_situs_sanctioned":
            sanctioned += amt
        if market_context is not None:
            timing, p_rationale = pace_for_line(
                amt, market_context, book_usd=book_usd, tranche_usd=tranche_usd)
        else:
            timing, p_rationale = "now", ""
        rationale = (
            f"High-potential sleeve ({cand.conviction} conviction, {cand.vehicle}): "
            f"{cand.thesis}"
        )
        lines.append(DeploymentLine(
            symbol=cand.ticker,
            type=("ETF" if cand.vehicle == "ucits_thematic" else "Stock"),
            amount_usd=amt, timing=timing, is_new=(held_value <= 0.0), tier="high",
            horizon=_TIER_HORIZON["high"], estate=estate,
            cap_note=f"high-potential sleeve ({a.pct_of_sleeve:.1f}% of sleeve)",
            net_of_tax_caveat=NET_OF_TAX_CAVEAT, rationale=rationale,
            cites=(), held_value_usd=held_value, pace_rationale=p_rationale,
        ))
    return lines, round(exposed, 2), round(sanctioned, 2)


# Max over-allocation (USD) treated as per-leg cent-rounding noise rather than a
# sizing bug. Deliberately $1.00: the sleeve guard above already tolerates $0.50
# of renormalisation drift on its own, so a whole-plan ceiling below that would
# be self-contradictory. Anything larger is a real bug and must surface.
_OVER_ALLOCATION_SHAVE_MAX = 1.00


def _shave_overage(
    tiers: tuple["DeploymentTier", ...], over: float
) -> tuple["DeploymentTier", ...]:
    """Remove ``over`` dollars from the single largest line.

    Shaving the LARGEST line keeps the relative distortion smallest (a cent off
    a $20,000 core leg is 5e-7 of it). Only ever subtracts, so the invariant it
    protects — total <= cash — cannot be violated by the repair itself.
    """
    biggest: tuple[int, int, float] | None = None  # (tier_idx, line_idx, amount)
    for ti, tier in enumerate(tiers):
        for li, line in enumerate(tier.lines):
            if biggest is None or line.amount_usd > biggest[2]:
                biggest = (ti, li, line.amount_usd)
    if biggest is None:
        return tiers
    ti, li, amt = biggest
    out = list(tiers)
    lines = list(out[ti].lines)
    lines[li] = replace(lines[li], amount_usd=round(amt - over, 2))
    out[ti] = replace(out[ti], lines=tuple(lines))
    return tuple(out)


def assemble_deployment_plan(
    *, doc, holdings: dict[str, float], deploy_amount_usd: float, as_of: date,
    market_context=None, sleeve_pct: float = 5.0, use_high_potential: bool = True,
    user_id: str = "ariel", exposure_aware: bool = False,
) -> DeploymentPlan:
    """Build the deploy plan: plan-bound ``cash_only_deploy`` buys, each
    annotated with tier/estate/cap/tax/horizon/pacing, grouped into tiers that
    sum to ``deploy_amount_usd``.

    P1 (``market_context=None``): reserve=0, medium empty, core = post-sleeve
    amount; all core lines get ``timing="now"`` and ``pace_rationale=""``.
    P2 (``market_context`` provided): lines are paced via ``pace_for_line``;
    staleness is surfaced as a caveat.

    High-potential sleeve: when ``use_high_potential`` and ``sleeve_pct > 0``, a
    ``sleeve_budget = deploy_amount * sleeve_pct/100`` is carved off the TOP and
    routed to the ``high`` tier via the EXISTING ``build_high_potential_sleeve``
    (fed by cached discovery BUY picks, seed fallback); core/medium are computed
    on the REMAINDER. With ``use_high_potential=False`` (or ``sleeve_pct<=0``) the
    ``high`` tier stays empty and core is computed on the full amount — the P1/P2
    behaviour. Conservation holds in both modes:
    ``deployed_total + undeployed_remainder == deploy_amount`` (within $0.01) and
    ``sum(high-tier lines) == sleeve_budget`` (within $0.50).
    """
    amount = round(deploy_amount_usd, 2)
    cash_total = amount

    # Item D — discovery dry-powder earmark is not deployable general cash.
    from argosy.services.discovery_reserve import (
        DISCOVERY_RESERVE_LABEL,
        apply_discovery_reserve,
        labeled_exclusion,
        resolve_discovery_reserve_usd,
    )

    book_for_pct = round(sum(holdings.values()), 2)
    reserve_resolved = resolve_discovery_reserve_usd(doc, book_usd=book_for_pct)
    amount, discovery_reserve = apply_discovery_reserve(
        cash_total_usd=cash_total, reserve_usd=reserve_resolved,
    )

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
            discovery_reserve_usd=discovery_reserve,
            cash_total_usd=cash_total,
        )

    from argosy.services.allocation_engine import cash_only_deploy

    estate_map = build_estate_map(doc)
    plan_symbols = set(estate_map)

    # Carve the high-potential sleeve off the TOP: core/medium are computed on
    # the REMAINDER so the sleeve budget is never double-counted. The sleeve
    # budget is bounded to [0, amount] so a misconfigured pct can't carve more
    # than the entered cash.
    use_sleeve = bool(use_high_potential) and sleeve_pct > 0 and amount > 0
    if use_sleeve:
        sleeve_budget = min(amount, round(amount * sleeve_pct / 100.0, 2))
    else:
        sleeve_budget = 0.0
    core_capital = round(amount - sleeve_budget, 2)

    candidates = cash_only_deploy(
        doc, holdings, core_capital, as_of=as_of, exposure_aware=exposure_aware)
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
            # The sleeve this buy FILLS may differ from the emitted symbol when the
            # engine tops up a held substitute (exposure-aware): a FWRA buy that
            # fills the EXUS sleeve is plan-bound CORE, not tactical. Read the
            # sleeve from the ``plan_target:`` cite so tier + cap_note reflect it.
            sleeve_sym = sym
            for _cite in cand.cites:
                if _cite.startswith("plan_target:"):
                    sleeve_sym = _cite.split(":", 1)[1]
                    break
            is_plan = sleeve_sym in plan_symbols
            tier = classify_tier(kind=cand.kind, symbol=sleeve_sym, is_plan_instrument=is_plan)
            # Estate tag from the EMITTED symbol: a plan instrument uses the map; a
            # held substitute (topup) is estate-safe by construction — classify it
            # from the instrument reference rather than leaving it "unstamped".
            estate = estate_map.get(sym) or _substitute_estate_tag(sym)
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
                cap_note=cap_note_for(doc, symbol=sleeve_sym),
                net_of_tax_caveat=NET_OF_TAX_CAVEAT, rationale=cand.rationale,
                cites=cand.cites, held_value_usd=held_value,
                pace_rationale=p_rationale,
            )
            # P1: only core is populated; a non-core classification would be a
            # tactical line cash_only_deploy should never emit. Keep it in core
            # but the tier label stays honest.
            core_lines.append(line)

    # High-potential sleeve (carved off the top). Built from the EXISTING sizer;
    # its estate split folds into the headline totals so the sleeve's US-situs
    # carve-out is never hidden.
    high_lines: list[DeploymentLine] = []
    if sleeve_budget > 0:
        high_lines, sleeve_exposed, sleeve_sanctioned = _high_potential_lines(
            sleeve_budget_usd=sleeve_budget, user_id=user_id,
            market_context=market_context, book_usd=book_usd, tranche_usd=amount,
            holdings=holdings,
        )
        exposed_total += sleeve_exposed
        sanctioned_total += sleeve_sanctioned
        # Conservation (money-math): the high tier must place EXACTLY the carved
        # budget. build_high_potential_sleeve renormalises conviction weights, so
        # rounding drift across legs is bounded — assert it stays within $0.50.
        high_total = round(sum(l.amount_usd for l in high_lines), 2)
        if high_lines and abs(high_total - sleeve_budget) > 0.50:
            raise ValueError(
                f"high-potential sleeve sizing drift: lines total {high_total} "
                f"!= sleeve budget {sleeve_budget}"
            )

    tiers = (
        DeploymentTier("reserve", 0.0, ()),
        DeploymentTier("core", DEPLOY_TIER_CAPS["core"], tuple(core_lines)),
        DeploymentTier("medium", DEPLOY_TIER_CAPS["medium"], ()),
        DeploymentTier("high", DEPLOY_TIER_CAPS["high"], tuple(high_lines)),
    )
    deployed = round(sum(t.total_usd for t in tiers), 2)
    # Over-deploy is never allowed — the engine water-fills to <= cash. But the
    # legs are independently rounded to the cent (and the sleeve renormalises
    # conviction weights, tolerating up to $0.50 of drift just above), so a few
    # cents of float noise on a six-figure tranche is EXPECTED and must not fail
    # the whole plan. Absorb sub-dollar drift by SHAVING it off the largest line
    # — never by widening the ceiling, and never by adding to any line. Anything
    # larger is a real sizing bug and still raises.
    over = round(deployed - amount, 2)
    if over > 0:
        if over > _OVER_ALLOCATION_SHAVE_MAX:
            raise ValueError(
                f"deploy-cash over-allocated: buys total {deployed} > amount {amount}"
            )
        tiers = _shave_overage(tiers, over)
        deployed = round(sum(t.total_usd for t in tiers), 2)
        if deployed - amount > 0.005:  # post-condition: the shave must have worked
            raise ValueError(
                f"deploy-cash over-allocation shave failed: {deployed} > {amount}"
            )
    remainder = round(max(0.0, amount - deployed), 2)
    caveats = _CAVEATS
    # Surface the caveat only for a MATERIAL remainder; sub-dollar drift is just
    # pro-rata rounding noise (the exact figure is still on undeployed_remainder_usd).
    if remainder >= 1.0:
        caveats = caveats + (_remainder_caveat(remainder),)
    if discovery_reserve > 0:
        caveats = caveats + (labeled_exclusion(discovery_reserve),)
    # P2: loud staleness caveat when any context feed is stale.
    if market_context is not None and market_context.is_any_stale:
        caveats = caveats + (
            f"WARNING: market context data is stale (age: {mca}). "
            "Pacing decisions are based on potentially outdated market data — "
            "refresh market context before executing.",
        )
    if market_context is None:
        note = ("Plan-only deploy: live market context not requested (pass live=true "
                "for market-aware pacing); tactical sleeves arrive in later phases.")
    else:
        note = f"Market-aware deploy (P2): context age {mca}."
    if discovery_reserve > 0:
        note = (
            f"{note} {DISCOVERY_RESERVE_LABEL}: "
            f"${discovery_reserve:,.2f} of ${cash_total:,.2f} cash excluded."
        ).strip()
    return DeploymentPlan(
        deploy_amount_usd=amount, as_of=as_of, tiers=tiers,
        us_situs_exposed_usd=round(exposed_total, 2),
        us_situs_sanctioned_usd=round(sanctioned_total, 2),
        undeployed_remainder_usd=remainder, market_context_age=mca,
        caveats=caveats,
        note=note,
        discovery_reserve_usd=discovery_reserve,
        cash_total_usd=cash_total,
    )
