"""Unit tests for the canonical allocation_plan service.

The target allocation is the multi-agent panel's agreed asset-class mix, with
the contested fixed-income weight DERIVED (not asserted) so the allocation is
self-consistent with the plan's steady-state volatility anchor
``SIGMA_DIVERSIFIED`` — the exact sigma the deconcentration optimizer used to
certify the earliest-safe retirement age.
"""
from __future__ import annotations

from datetime import date

import pytest

from argosy.services.allocation_plan import (
    CASH_FRAC_OF_FI,
    NVDA_TARGET_PCT,
    build_redistribution_schedule,
    build_target_allocation,
    derive_fi_weight,
    to_synth_targets,
    to_waypoint_targets,
)
from argosy.services.alternatives_types import (
    AlternativesSleeveDecision,
    VerificationEvidence,
    VerificationResult,
    VerifiedAlternativesCandidate,
)
from argosy.services.retirement.scenario_mc import SIGMA_DIVERSIFIED


def _green_candidate(symbol, domicile, isin, weight, asset_class):
    return VerifiedAlternativesCandidate(
        symbol=symbol, name=f"{symbol} fund", asset_class=asset_class,
        domicile=domicile, isin=isin, weight_within_sleeve_pct=weight,
        conviction="HIGH", thesis_md="diversifier",
        verification=VerificationResult(
            symbol=symbol, verified=True, severity="GREEN", reason="ok",
            evidence=VerificationEvidence(
                isin_checksum_ok=True, isin_prefix=isin[:2], domicile_coherent=True,
                registry_hit=True, source_url="https://issuer/factsheet",
            ),
            resolved_isin=isin, resolved_domicile=domicile,
        ),
    )


def _sleeve_decision(target_pct=3.0, sigma=0.268):
    return AlternativesSleeveDecision(
        target_pct=target_pct, sleeve_sigma=sigma,
        instruments=[
            _green_candidate("SGLD", "IE", "IE00B579F325", 80.0, "precious_metals"),
            _green_candidate("IGLN", "IE", "IE00B4ND3602", 20.0, "precious_metals"),
        ],
        decision="approve", rationale_md="team-sourced gold sleeve",
    )
from argosy.services.sigma_glidepath import (
    map_glidepath_class_to_sigma_class,
    sigma_from_composition,
)


class TestDeriveFiWeight:
    def test_blended_sigma_sits_on_the_plan_anchor(self) -> None:
        alloc = build_target_allocation()
        # The whole point of deriving FI: the allocation blends to the plan's
        # steady-state anchor (so age-47 stays self-consistent), not above it.
        assert alloc.blended_sigma <= SIGMA_DIVERSIFIED + 1e-6

    def test_fi_is_minimal_one_step_less_breaches_anchor(self) -> None:
        alloc = build_target_allocation(fi_step=0.5)
        # FI is the MINIMUM that clears the anchor — half a point less must
        # breach it (otherwise we're over-building conservatism).
        ratios = {c.label: c.target_pct for c in alloc.classes}
        fi_pct = alloc.fi_pct
        lighter = dict(ratios)
        # shift 0.5pp from cash back into the largest equity sleeve
        biggest = max(
            (c for c in alloc.classes if c.sigma_class == "us_equity"),
            key=lambda c: c.target_pct,
        ).label
        cash_label = next(c.label for c in alloc.classes if c.sigma_class == "cash")
        lighter[cash_label] -= 0.5
        lighter[biggest] += 0.5
        assert sigma_from_composition(lighter) > SIGMA_DIVERSIFIED
        assert fi_pct > 16.0  # strictly more than the panel's contested estimate

    def test_panel_sixteen_pct_would_breach_the_anchor(self) -> None:
        # Documents the finding: the panel's headline 16% FI blends ABOVE the
        # anchor in the real engine — i.e. it is NOT consistent with age 47.
        comp = {
            "US broad-market core": 28.0,
            "Dividend-quality income": 19.0,
            "US growth tilt (ex-NVDA)": 6.0,
            "International developed (ex-US)": 12.0,
            "US low-volatility equity": 6.0,
            "Strategic single-stock (NVDA)": 12.0,
            "Real assets (REIT/TIPS)": 1.0,
            "Cash & T-bills (incl. ILS tranche)": 16.0 * CASH_FRAC_OF_FI,
            "Short-duration IG bonds": 16.0 * (1.0 - CASH_FRAC_OF_FI),
        }
        assert sigma_from_composition(comp) > SIGMA_DIVERSIFIED


class TestBuildTargetAllocation:
    def test_weights_sum_to_100(self) -> None:
        alloc = build_target_allocation()
        assert sum(c.target_pct for c in alloc.classes) == pytest.approx(100.0, abs=0.05)

    def test_nvda_held_at_user_pick(self) -> None:
        alloc = build_target_allocation()
        nvda = next(c for c in alloc.classes if "NVDA" in c.label and c.sigma_class == "concentrated_equity")
        assert nvda.target_pct == pytest.approx(NVDA_TARGET_PCT)

    def test_fi_derived_above_panel_estimate(self) -> None:
        alloc = build_target_allocation()
        assert 18.0 <= alloc.fi_pct <= 24.0  # derived band; not the panel's 16

    def test_every_label_maps_to_its_intended_sigma_class(self) -> None:
        # Auditability gate: no label may silently mis-map (the ex-NVDA /
        # defensive-as-bonds traps must be dead).
        alloc = build_target_allocation()
        for c in alloc.classes:
            assert map_glidepath_class_to_sigma_class(c.label) == c.sigma_class, c.label

    def test_contested_classes_carry_dissent(self) -> None:
        alloc = build_target_allocation()
        fi = next(c for c in alloc.classes if c.sigma_class == "cash")
        nvda = next(c for c in alloc.classes if c.sigma_class == "concentrated_equity")
        assert fi.agreement == "contested" and fi.dissent
        assert nvda.agreement == "contested" and nvda.dissent


_TODAY_FULL_BOOK = {
    "Strategic single-stock (NVDA)": 60.47,
    "US growth tilt (ex-NVDA)": 11.04,
    "US broad-market core": 10.53,
    "Dividend-quality income": 7.01,
    "Cash & T-bills (incl. ILS tranche)": 4.95,
    "Short-duration IG bonds": 3.29,
    "Real assets (REIT/TIPS)": 1.82,
    "International developed (ex-US)": 0.90,
}


class TestRedistributionSchedule:
    def _sched(self, quarters: int = 8):
        alloc = build_target_allocation()
        return build_redistribution_schedule(
            today_composition=_TODAY_FULL_BOOK,
            target=alloc,
            start=date(2026, 6, 8),
            quarters=quarters,
        )

    def test_nvda_tapers_from_today_to_target(self) -> None:
        sched = self._sched()
        nvda = [w for w in sched.waypoints if w.label == "Strategic single-stock (NVDA)"]
        nvda.sort(key=lambda w: w.quarter)
        # monotone decreasing, ends at the 12% target
        assert nvda[0].pct < 60.47  # already moving down by Q1
        assert nvda[-1].pct == pytest.approx(NVDA_TARGET_PCT, abs=0.01)
        for a, b in zip(nvda, nvda[1:]):
            assert b.pct <= a.pct + 1e-9

    def test_every_quarter_composition_sums_to_100(self) -> None:
        sched = self._sched()
        for q in range(1, sched.quarters + 1):
            total = sum(w.pct for w in sched.waypoints if w.quarter == q)
            assert total == pytest.approx(100.0, abs=0.05)

    def test_underweight_class_rises_monotonically(self) -> None:
        sched = self._sched()
        intl = sorted(
            (w for w in sched.waypoints if w.label == "International developed (ex-US)"),
            key=lambda w: w.quarter,
        )
        assert intl[0].pct > 0.90  # rising from today's ~0.9%
        for a, b in zip(intl, intl[1:]):
            assert b.pct >= a.pct - 1e-9

    def test_waypoint_targets_quarterly_pct_of_portfolio(self) -> None:
        sched = self._sched(quarters=8)
        targets = to_waypoint_targets(sched, stated_at=date(2026, 6, 8))
        assert all(t.unit == "pct_of_portfolio" for t in targets)
        # 8 quarterly waypoints per class
        nvda_targets = [t for t in targets if t.label == "Strategic single-stock (NVDA)"]
        assert len(nvda_targets) == 8
        # revisit dates strictly increasing per class
        dts = [t.revisit_after for t in nvda_targets]
        assert dts == sorted(dts) and len(set(dts)) == 8


class TestInstruments:
    """T1.2 — each class is instrument-level (named tickers), so the canonical
    doc can say WHAT to buy, not just an abstract asset-class weight. Tickers are
    the panel's agreed names (sourced from each sleeve's rationale), never magic."""

    def test_every_class_carries_instruments_summing_to_100(self) -> None:
        alloc = build_target_allocation()
        for c in alloc.classes:
            assert c.instruments, f"{c.label} has no instruments"
            total = sum(i.weight_within_class_pct for i in c.instruments)
            assert total == pytest.approx(100.0, abs=0.01), c.label

    def test_nvda_class_is_just_nvda(self) -> None:
        alloc = build_target_allocation()
        nvda = next(c for c in alloc.classes if c.sigma_class == "concentrated_equity")
        assert [i.symbol for i in nvda.instruments] == ["NVDA"]
        assert nvda.instruments[0].weight_within_class_pct == pytest.approx(100.0)

    def test_named_sleeves_carry_their_panel_tickers(self) -> None:
        alloc = build_target_allocation()
        core = next(c for c in alloc.classes if c.label == "US broad-market core")
        div = next(c for c in alloc.classes if c.label == "Dividend-quality income")
        # UCITS-preferred (domicile-aware): Irish-domiciled twins, NOT US-domiciled
        # VOO/SCHD, so the canonical plan does not add US-situs estate exposure for a
        # non-US-person (cite estate_tax_nonresidents.md / feedback_canonical_allocation_ucits_preferred).
        assert "CSPX" in {i.symbol for i in core.instruments}
        assert "FUSA" in {i.symbol for i in div.instruments}

    def test_no_canonical_class_uses_a_us_domiciled_primary(self) -> None:
        """Guardrail: the canonical instrument layer must stay UCITS-preferred.

        US-domiciled ETF shares are US-situs for a non-US-person and re-introducing
        them would silently rebuild the ~$1M estate-tax tail the plan exists to shrink.
        NVDA is the one sanctioned US-situs holding (managed down by the trim glide).
        """
        us_domiciled = {"VOO", "SCHD", "VEA", "SCHG", "USMV", "VNQ", "SGOV", "VGSH",
                        "VTI", "VXUS", "VIG", "SCHP", "QQQM", "IBIT"}
        alloc = build_target_allocation()
        for c in alloc.classes:
            for i in c.instruments:
                if i.symbol == "NVDA":
                    continue
                assert i.symbol not in us_domiciled, (
                    f"{c.label}/{i.symbol} is a US-domiciled primary — use the UCITS twin"
                )

    def test_every_instrument_is_sourced_with_a_rationale(self) -> None:
        alloc = build_target_allocation()
        for c in alloc.classes:
            for i in c.instruments:
                assert i.rationale, f"{c.label}/{i.symbol} instrument lacks a sourced rationale"


class TestAlternativesSleeve:
    """The Alternatives sleeve is TEAM-SOURCED, not hardcoded. The engine consumes
    a supplied AlternativesSleeveDecision (size + instruments + sourced sigma);
    with no decision (or a 0% one) there is no alternatives class at all. FI stays
    the sigma-solver and absorbs the sourced sleeve to hold the anchor."""

    def test_none_sleeve_has_no_alternatives_class(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=None)
        assert not any(c.sigma_class == "alternatives" for c in alloc.classes)

    def test_none_sleeve_weights_sum_to_100(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=None)
        assert sum(c.target_pct for c in alloc.classes) == pytest.approx(100.0, abs=0.05)

    def test_supplied_sleeve_present_at_target_pct(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=_sleeve_decision(3.0))
        alt = next(c for c in alloc.classes if c.sigma_class == "alternatives")
        assert alt.target_pct == pytest.approx(3.0, abs=0.02)

    def test_supplied_sleeve_instruments_threaded(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=_sleeve_decision(3.0))
        alt = next(c for c in alloc.classes if c.sigma_class == "alternatives")
        by_sym = {i.symbol: i.weight_within_class_pct for i in alt.instruments}
        assert by_sym == {"SGLD": pytest.approx(80.0), "IGLN": pytest.approx(20.0)}

    def test_supplied_sleeve_holds_anchor(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=_sleeve_decision(3.0, 0.268))
        assert alloc.blended_sigma <= SIGMA_DIVERSIFIED + 1e-6

    def test_fi_rises_with_supplied_sleeve(self) -> None:
        baseline = derive_fi_weight(alternatives_pct=0.0, alternatives_sigma=0.0)
        with_alts = derive_fi_weight(alternatives_pct=3.0, alternatives_sigma=0.268)
        assert with_alts > baseline

    def test_sourced_sigma_flows_into_solver(self) -> None:
        # A gold-only sleeve (sourced sigma 0.16) must force LESS FI than an
        # 80/20 gold/BTC sleeve (0.268) — the FI solver consumes the SOURCED
        # sigma, not a fixed 0.268.
        gold_only = derive_fi_weight(alternatives_pct=3.0, alternatives_sigma=0.16)
        with_btc = derive_fi_weight(alternatives_pct=3.0, alternatives_sigma=0.268)
        assert gold_only < with_btc

    def test_supplied_sleeve_estate_clean_non_us(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=_sleeve_decision(3.0))
        for c in alloc.classes:
            for i in c.instruments:
                if i.symbol != "NVDA":
                    assert i.domicile != "US"
                assert i.symbol != "IBIT"

    def test_weights_sum_to_100_with_supplied_sleeve(self) -> None:
        alloc = build_target_allocation(alternatives_sleeve=_sleeve_decision(3.0))
        assert sum(c.target_pct for c in alloc.classes) == pytest.approx(100.0, abs=0.05)


class TestToSynthTargets:
    def test_targets_are_pct_of_portfolio_with_rationale(self) -> None:
        alloc = build_target_allocation()
        targets = to_synth_targets(
            alloc, stated_at=date(2026, 6, 8), revisit_after=date(2028, 6, 8)
        )
        assert len(targets) == len(alloc.classes)
        assert all(t.unit == "pct_of_portfolio" for t in targets)
        assert all(t.rationale for t in targets)
        assert all(t.label == c.label for t, c in zip(targets, alloc.classes))
