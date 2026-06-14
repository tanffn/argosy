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
    _ALTERNATIVES_LABEL,
    _blended_sigma_for,
    _renormalise,
    anchor_sigma_for_phase,
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
        # The whole point of deriving FI: the allocation's COVARIANCE-blended
        # sigma sits at/under the steady-state anchor, not above it.
        assert alloc.blended_sigma <= SIGMA_DIVERSIFIED + 1e-6

    def test_accumulation_fi_is_the_policy_floor(self) -> None:
        # Under the covariance blend the diversified book's true sigma is well
        # below 0.18, so the anchor would size FI to ~7% — the 8% liquidity
        # policy floor binds in the accumulation phase.
        alloc = build_target_allocation()  # default = accumulation anchor
        assert alloc.fi_pct == pytest.approx(8.0, abs=0.01)

    def test_fi_is_minimal_one_step_less_breaches_a_binding_anchor(self) -> None:
        # At accumulation the 8% floor binds; to test the solver's minimality use
        # a near-retirement phase whose lower anchor binds ABOVE the floor.
        anch = anchor_sigma_for_phase(1.0)
        fi = derive_fi_weight(anchor_sigma=anch, fi_step=0.001)
        assert fi > 8.0  # the anchor binds here, not the floor
        at = _renormalise(nvda_pct=NVDA_TARGET_PCT, fi_pct=fi)
        lighter = _renormalise(nvda_pct=NVDA_TARGET_PCT, fi_pct=fi - 0.05)
        assert sigma_from_composition(at) <= anch + 1e-9
        assert sigma_from_composition(lighter) > anch

    def test_rounded_fi_return_actually_clears_the_anchor(self) -> None:
        # Regression: rounding the raw solver value to 2dp must not land back
        # UNDER the anchor (the fi_step<0.01 rounding-down bug). The RETURNED FI,
        # recomputed, must clear — for both the plain book and an alternatives one.
        anch = 0.165
        fi = derive_fi_weight(anchor_sigma=anch, fi_lo=2.0, fi_step=0.001)
        assert sigma_from_composition(_renormalise(nvda_pct=NVDA_TARGET_PCT, fi_pct=fi)) <= anch + 1e-9
        fi_alt = derive_fi_weight(
            anchor_sigma=SIGMA_DIVERSIFIED, alternatives_pct=3.0,
            alternatives_sigma=0.16, fi_lo=2.0, fi_step=0.001,
        )
        w = _renormalise(nvda_pct=NVDA_TARGET_PCT, fi_pct=fi_alt, alternatives_pct=3.0)
        assert _blended_sigma_for(w, alt_label=_ALTERNATIVES_LABEL, alt_sigma=0.16) <= SIGMA_DIVERSIFIED + 1e-9

    def test_fi_monotonically_non_increasing_in_anchor(self) -> None:
        # Codex guardrail: a HIGHER risk tolerance (anchor) must never require
        # MORE fixed income. Sweep anchors and assert FI is non-increasing.
        anchors = [0.15, 0.155, 0.16, 0.165, 0.17, 0.18, 0.20]
        fis = [derive_fi_weight(anchor_sigma=a, fi_lo=2.0, fi_step=0.01) for a in anchors]
        for a, b in zip(fis, fis[1:]):
            assert b <= a + 1e-9, fis

    def test_sixteen_pct_fi_now_over_reserves(self) -> None:
        # Under the covariance blend the panel's headline 16% FI mix blends BELOW
        # the anchor (over-reserved) — the opposite of the linear-blend reading.
        # The derived accumulation FI is the ~8% floor instead.
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
        assert sigma_from_composition(comp) < SIGMA_DIVERSIFIED

    def test_phase_anchor_glides_fi_up_toward_retirement(self) -> None:
        # Accumulation ~8%, gliding to ~15% as retirement nears, ~20%+ in drawdown.
        accum = build_target_allocation(years_to_retirement=5.0).fi_pct
        near = build_target_allocation(years_to_retirement=0.5).fi_pct
        drawdown = build_target_allocation(years_to_retirement=0.0).fi_pct
        assert accum == pytest.approx(8.0, abs=0.01)
        assert 12.0 <= near <= 16.0
        assert drawdown >= 18.0
        assert accum < near < drawdown


class TestBuildTargetAllocation:
    def test_weights_sum_to_100(self) -> None:
        alloc = build_target_allocation()
        assert sum(c.target_pct for c in alloc.classes) == pytest.approx(100.0, abs=0.05)

    def test_nvda_held_at_user_pick(self) -> None:
        alloc = build_target_allocation()
        nvda = next(c for c in alloc.classes if "NVDA" in c.label and c.sigma_class == "concentrated_equity")
        assert nvda.target_pct == pytest.approx(NVDA_TARGET_PCT)

    def test_fi_in_accumulation_phase_band(self) -> None:
        alloc = build_target_allocation()  # accumulation (no withdrawals)
        assert 8.0 <= alloc.fi_pct <= 10.0  # phase-aware accumulation band

    def test_every_class_is_explicitly_correlation_modeled(self) -> None:
        # Codex guardrail: no class the allocation uses may fall through to the
        # conservative ρ=1 fallback by accident — each must be explicitly modeled.
        from argosy.services.sigma_glidepath import KNOWN_CORR_CLASSES

        alloc = build_target_allocation(alternatives_sleeve=_sleeve_decision(3.0))
        for c in alloc.classes:
            assert c.sigma_class in KNOWN_CORR_CLASSES, c.sigma_class

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

    def test_low_corr_diversifier_does_not_raise_required_fi(self) -> None:
        # Under the covariance blend a low-correlation diversifier (alts ρ≈0.25
        # to equity) that DISPLACES higher-correlation equity does NOT force more
        # FI — it can lower it. (The linear blend wrongly forced more FI for any
        # sleeve σ above cash.) fi_lo=2 so the 8% policy floor doesn't mask it.
        base = derive_fi_weight(alternatives_pct=0.0, alternatives_sigma=0.0, fi_lo=2.0, fi_step=0.001)
        with_alts = derive_fi_weight(alternatives_pct=3.0, alternatives_sigma=0.16, fi_lo=2.0, fi_step=0.001)
        assert with_alts <= base + 1e-9

    def test_sourced_sigma_flows_into_solver(self) -> None:
        # The FI solver consumes the SOURCED sleeve sigma: a higher-σ sleeve
        # (80/20 gold/BTC, 0.268) requires strictly more FI than a gold-only one
        # (0.16). fi_lo=2 so the floor doesn't flatten both onto 8%.
        gold_only = derive_fi_weight(alternatives_pct=3.0, alternatives_sigma=0.16, fi_lo=2.0, fi_step=0.001)
        with_btc = derive_fi_weight(alternatives_pct=3.0, alternatives_sigma=0.268, fi_lo=2.0, fi_step=0.001)
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
