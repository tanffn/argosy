"""Deployment advisor (P1) — deterministic plan-bound deploy-cash service."""
import pytest
from argosy.services.deployment_advisor import (
    DeploymentLine,
    DeploymentPlan,
    DeploymentTier,
    EstateTag,
    TIER_NAMES,
    DEPLOY_TIER_CAPS,
    classify_tier,
    build_estate_map,
    NET_OF_TAX_CAVEAT,
    cap_note_for,
    assemble_deployment_plan,
)


class TestContracts:
    def test_tier_names_are_reserve_core_medium_high_in_carve_order(self):
        # reserve is carved first; then core, medium, high.
        assert TIER_NAMES == ("reserve", "core", "medium", "high")

    def test_deployment_line_holds_the_table_columns(self):
        line = DeploymentLine(
            symbol="CSPX", type="ETF", amount_usd=1000.0, timing="now",
            is_new=False, tier="core", horizon="10yr+",
            estate=EstateTag(domicile="IE", status="estate_safe", note="UCITS"),
            cap_note="fills US broad-market core", net_of_tax_caveat="net of Israeli CGT",
            rationale="gap-fill",
        )
        assert line.symbol == "CSPX"
        assert line.tier == "core"
        assert line.estate.status == "estate_safe"

    def test_tier_total_sums_its_lines(self):
        lines = (
            DeploymentLine("CSPX", "ETF", 600.0, "now", False, "core", "10yr+",
                           EstateTag("IE", "estate_safe", ""), "", "", ""),
            DeploymentLine("EXUS", "ETF", 400.0, "now", True, "core", "10yr+",
                           EstateTag("IE", "estate_safe", ""), "", "", ""),
        )
        tier = DeploymentTier(name="core", cap_pct=70.0, lines=lines)
        assert tier.total_usd == 1000.0

    def test_plan_deployed_total_sums_all_tiers(self):
        core = DeploymentTier("core", 70.0, (
            DeploymentLine("CSPX", "ETF", 1000.0, "now", False, "core", "10yr+",
                           EstateTag("IE", "estate_safe", ""), "", "", ""),
        ))
        empty = lambda n, c: DeploymentTier(n, c, ())
        plan = DeploymentPlan(
            deploy_amount_usd=1000.0, as_of=__import__("datetime").date(2026, 6, 12),
            tiers=(empty("reserve", 0.0), core, empty("medium", 25.0), empty("high", 5.0)),
            us_situs_exposed_usd=0.0, us_situs_sanctioned_usd=0.0,
            undeployed_remainder_usd=0.0, market_context_age=None, caveats=(), note="",
        )
        assert plan.deployed_total_usd == 1000.0


class TestTierClassification:
    def test_plan_bound_gap_fill_is_core_in_p1(self):
        # Every cash_only_deploy candidate is plan-bound gap-fill -> core.
        assert classify_tier(kind="BUY", symbol="CSPX", is_plan_instrument=True) == "core"
        assert classify_tier(kind="SWAP", symbol="IB01", is_plan_instrument=True) == "core"

    def test_non_plan_instrument_would_be_tactical_but_p1_emits_none(self):
        # A symbol NOT in the canonical plan would be medium (tactical). cash_only_deploy
        # never emits these in P1, but the classifier is honest about the rule.
        assert classify_tier(kind="BUY", symbol="PLTR", is_plan_instrument=False) == "medium"

    def test_tier_caps_are_70_25_5_on_tactical_post_reserve(self):
        assert DEPLOY_TIER_CAPS == {"core": 70.0, "medium": 25.0, "high": 5.0}


def _doc_with(instruments_by_class):
    """Build a minimal TargetAllocationDoc for estate tests."""
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, TargetAllocationDoc,
    )
    classes = []
    for label, instruments in instruments_by_class.items():
        classes.append(AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="us_equity",
            target_pct=10.0,
            instruments=[AllocationInstrument(symbol=s, role="primary",
                                              weight_within_class_pct=100.0,
                                              rationale="", domicile=d)
                         for s, d in instruments],
            agreement="", rationale="", dissent="",
        ))
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test", classes=classes, glide=[],
    )


class TestEstateAnnotation:
    def test_ucits_ie_instrument_is_estate_safe(self):
        doc = _doc_with({"US broad-market core": [("CSPX", "IE")]})
        emap = build_estate_map(doc)
        assert emap["CSPX"].status == "estate_safe"
        assert emap["CSPX"].domicile == "IE"

    def test_nvda_us_situs_is_sanctioned_not_exposed(self):
        doc = _doc_with({"Strategic single-stock (NVDA)": [("NVDA", "US")]})
        emap = build_estate_map(doc)
        assert emap["NVDA"].status == "us_situs_sanctioned"

    def test_unsanctioned_us_domicile_is_exposed_red(self):
        doc = _doc_with({"US growth": [("VOO", "US")]})
        emap = build_estate_map(doc)
        assert emap["VOO"].status == "us_situs_exposed"

    def test_unstamped_domicile_is_unstamped_yellow(self):
        doc = _doc_with({"Mystery": [("XXXX", None)]})
        emap = build_estate_map(doc)
        assert emap["XXXX"].status == "unstamped"


class TestCapAndTaxAnnotation:
    def test_cap_note_names_the_class_the_buy_fills(self):
        doc = _doc_with({"US broad-market core": [("CSPX", "IE")]})
        note = cap_note_for(doc, symbol="CSPX")
        assert "US broad-market core" in note

    def test_cap_note_flags_nvda_against_the_cap(self):
        doc = _doc_with({"Strategic single-stock (NVDA)": [("NVDA", "US")]})
        note = cap_note_for(doc, symbol="NVDA")
        assert "13" in note  # nvda_cap_pct surfaced

    def test_net_of_tax_caveat_is_a_nonempty_static_string(self):
        assert "net" in NET_OF_TAX_CAVEAT.lower()
        assert "tax" in NET_OF_TAX_CAVEAT.lower()


class TestAssemble:
    def _doc_holdings(self):
        # cash_only_deploy requires class percentages to sum to ~100.
        # Two classes at 50% each satisfies the conservation invariant.
        from argosy.services.target_allocation_doc import (
            AllocationClassDoc, AllocationInstrument, TargetAllocationDoc,
        )
        def _cls(label, symbol, domicile):
            return AllocationClassDoc(
                label=label, snapshot_category=label, sigma_class="us_equity",
                target_pct=50.0,
                instruments=[AllocationInstrument(
                    symbol=symbol, role="primary",
                    weight_within_class_pct=100.0, rationale="", domicile=domicile,
                )],
                agreement="", rationale="", dissent="",
            )
        doc = TargetAllocationDoc(
            anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
            provenance="test",
            classes=[
                _cls("US broad-market core", "CSPX", "IE"),
                _cls("International developed (ex-US)", "EXUS", "IE"),
            ],
            glide=[],
        )
        holdings = {"CSPX": 1000.0}  # CSPX held, EXUS new
        return doc, holdings

    def test_buckets_sum_to_deploy_amount(self):
        from datetime import date
        doc, holdings = self._doc_holdings()
        plan = assemble_deployment_plan(
            doc=doc, holdings=holdings, deploy_amount_usd=10_000.0,
            as_of=date(2026, 6, 12),
        )
        assert plan.deployed_total_usd == pytest.approx(10_000.0, abs=1.0)

    def test_all_lines_are_core_in_p1_and_reserve_is_zero(self):
        from datetime import date
        doc, holdings = self._doc_holdings()
        plan = assemble_deployment_plan(
            doc=doc, holdings=holdings, deploy_amount_usd=10_000.0,
            as_of=date(2026, 6, 12),
        )
        by_name = {t.name: t for t in plan.tiers}
        assert by_name["reserve"].total_usd == 0.0
        assert by_name["medium"].total_usd == 0.0
        assert by_name["high"].total_usd == 0.0
        assert by_name["core"].total_usd == pytest.approx(10_000.0, abs=1.0)
        assert all(l.tier == "core" for l in by_name["core"].lines)

    def test_new_vs_held_flagged_per_line(self):
        from datetime import date
        doc, holdings = self._doc_holdings()
        plan = assemble_deployment_plan(
            doc=doc, holdings=holdings, deploy_amount_usd=10_000.0,
            as_of=date(2026, 6, 12),
        )
        lines = {l.symbol: l for t in plan.tiers for l in t.lines}
        assert lines["EXUS"].is_new is True
        assert lines["CSPX"].is_new is False

    def test_each_line_carries_estate_cap_tax_horizon(self):
        from datetime import date
        doc, holdings = self._doc_holdings()
        plan = assemble_deployment_plan(
            doc=doc, holdings=holdings, deploy_amount_usd=10_000.0,
            as_of=date(2026, 6, 12),
        )
        line = next(l for t in plan.tiers for l in t.lines)
        assert line.estate.status in {
            "estate_safe", "us_situs_sanctioned", "us_situs_exposed", "unstamped"}
        assert line.cap_note
        assert line.net_of_tax_caveat == NET_OF_TAX_CAVEAT
        assert line.horizon == "10yr+"      # core default
        assert line.timing == "now"

    def test_no_plan_returns_empty_plan_with_note(self):
        from datetime import date
        plan = assemble_deployment_plan(
            doc=None, holdings={}, deploy_amount_usd=10_000.0, as_of=date(2026, 6, 12),
        )
        assert plan.deployed_total_usd == 0.0
        assert "plan" in plan.note.lower()
        # Nothing-lost: with no plan the whole amount is an explicit remainder.
        assert plan.undeployed_remainder_usd == pytest.approx(10_000.0)


# ----------------------------------------------------------------------
# Remediation of codex money-math review (deploy_assemble_review):
# (1) sum invariant ENFORCED via an explicit undeployed remainder,
# (2) estate headline splits sanctioned NVDA from real RED exposure,
# (3) BUY-leg funding is defensively enforced (raise on non-cash),
# (4) per-line held_value_usd is exposed (audit the NEW/held call).
# These use a monkeypatched engine to drive deterministic leg shapes.
# ----------------------------------------------------------------------
def _candidate(*legs):
    from argosy.services.contracts import AllocationCandidate, AllocationLeg
    return AllocationCandidate(
        kind="BUY",
        legs=tuple(
            AllocationLeg(side=s, symbol=sym, account_id="ibkr", currency="USD",
                          notional_usd=n, funding_source=f)
            for (s, sym, n, f) in legs
        ),
        horizon="now", rationale="probe",
    )


class TestRemediation:
    def _doc(self):
        return _doc_with({
            "US broad-market core": [("CSPX", "IE")],
            "Strategic single-stock (NVDA)": [("NVDA", "US")],
            "US growth (ex-NVDA)": [("VOO", "US")],  # unsanctioned US -> RED
        })

    def test_underdeployment_surfaces_explicit_remainder(self, monkeypatch):
        from datetime import date
        import argosy.services.allocation_engine as eng
        # Engine places only $50k of a $250k deploy (e.g. thin/ malformed targets).
        monkeypatch.setattr(eng, "cash_only_deploy",
                            lambda *a, **k: [_candidate(("BUY", "CSPX", 50_000.0, "cash"))])
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={}, deploy_amount_usd=250_000.0,
            as_of=date(2026, 6, 12),
        )
        assert plan.deployed_total_usd == pytest.approx(50_000.0)
        assert plan.undeployed_remainder_usd == pytest.approx(200_000.0)
        # Enforced invariant: deployed + remainder == entered amount (to the cent).
        assert plan.deployed_total_usd + plan.undeployed_remainder_usd == pytest.approx(
            250_000.0, abs=0.01)
        # Nothing hidden: the remainder is called out in the caveats.
        assert any("remainder" in c.lower() or "undeployed" in c.lower()
                   for c in plan.caveats)

    def test_estate_headline_splits_sanctioned_from_exposed(self, monkeypatch):
        from datetime import date
        import argosy.services.allocation_engine as eng
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [_candidate(
            ("BUY", "NVDA", 40_000.0, "cash"),   # sanctioned US-situs
            ("BUY", "VOO", 30_000.0, "cash"),    # unsanctioned US-situs (RED)
            ("BUY", "CSPX", 30_000.0, "cash"),   # estate-safe
        )])
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={}, deploy_amount_usd=100_000.0,
            as_of=date(2026, 6, 12),
        )
        assert plan.us_situs_sanctioned_usd == pytest.approx(40_000.0)
        assert plan.us_situs_exposed_usd == pytest.approx(30_000.0)  # NVDA NOT folded in

    def test_buy_leg_with_noncash_funding_raises(self, monkeypatch):
        from datetime import date
        import argosy.services.allocation_engine as eng
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [_candidate(
            ("BUY", "CSPX", 10_000.0, "trim_proceeds"),  # NOT cash -> must fail loud
        )])
        with pytest.raises(ValueError):
            assemble_deployment_plan(
                doc=self._doc(), holdings={}, deploy_amount_usd=10_000.0,
                as_of=date(2026, 6, 12),
            )

    def test_line_exposes_held_value(self, monkeypatch):
        from datetime import date
        import argosy.services.allocation_engine as eng
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [_candidate(
            ("BUY", "CSPX", 5_000.0, "cash"),
        )])
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={"CSPX": 12_345.0}, deploy_amount_usd=5_000.0,
            as_of=date(2026, 6, 12),
        )
        line = next(l for t in plan.tiers for l in t.lines if l.symbol == "CSPX")
        assert line.held_value_usd == pytest.approx(12_345.0)
        assert line.is_new is False


# ----------------------------------------------------------------------
# P2 T6: market-aware size + math pacing
# Verifies:
# (a) market_context=None -> P1 behavior unchanged (timing="now", pace_rationale="",
#     market_context_age=None)
# (b) market_context supplied + large line -> "DCA Nwk" timing + non-empty rationale
# (c) market_context supplied + small line (<=5k) -> "now" (lump)
# (d) is_any_stale context -> staleness caveat in plan.caveats
# ----------------------------------------------------------------------

def _stub_market_context(*, vix: float, sp500: float, is_stale: bool):
    """Build a minimal DeploymentMarketContext stub for pacing tests."""
    from argosy.services.deployment_market_context import (
        DataFreshness, DeploymentMarketContext, NvdaVerification,
    )
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    freshness = DataFreshness(
        field="vix", fetched_at=now_iso,
        age_seconds=999_999.0 if is_stale else 1.0,
        source="test", is_stale=is_stale,
    )
    return DeploymentMarketContext(
        snapshot={"vix": vix, "sp500": sp500},
        freshness=(freshness,),
        nvda=None,
        overall_age_label="live" if not is_stale else "cached (stale)",
    )


class TestP2Pacing:
    """P2 T6: market_context wiring + pace_for_line behaviour."""

    def _doc(self):
        return _doc_with({
            "US broad-market core": [("CSPX", "IE")],
            "International developed (ex-US)": [("EXUS", "IE")],
        })

    # ------------------------------------------------------------------
    # (a) P1 unchanged when market_context=None
    # ------------------------------------------------------------------
    def test_no_context_all_lines_timing_now_pace_rationale_empty(self, monkeypatch):
        """market_context=None -> P1 unchanged: timing 'now', pace_rationale '', age None."""
        from datetime import date
        import argosy.services.allocation_engine as eng
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [_candidate(
            ("BUY", "CSPX", 50_000.0, "cash"),
        )])
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={}, deploy_amount_usd=50_000.0,
            as_of=date(2026, 6, 12),
        )
        assert plan.market_context_age is None
        lines = [l for t in plan.tiers for l in t.lines]
        assert all(l.timing == "now" for l in lines)
        assert all(l.pace_rationale == "" for l in lines)

    # ------------------------------------------------------------------
    # (b) Large line + high VIX -> DCA Nwk, non-empty pace_rationale
    # ------------------------------------------------------------------
    def test_large_line_high_vix_gets_dca_timing(self, monkeypatch):
        """Large line + high VIX + stretched S&P -> DCA Nwk timing + non-empty rationale."""
        from datetime import date
        import argosy.services.allocation_engine as eng
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [_candidate(
            ("BUY", "CSPX", 50_000.0, "cash"),  # large line
        )])
        ctx = _stub_market_context(vix=35.0, sp500=6_000.0, is_stale=False)
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={}, deploy_amount_usd=50_000.0,
            as_of=date(2026, 6, 12), market_context=ctx,
        )
        assert plan.market_context_age == "live"
        lines = [l for t in plan.tiers for l in t.lines]
        assert len(lines) == 1
        line = lines[0]
        assert line.timing.startswith("DCA"), f"expected DCA timing, got {line.timing!r}"
        assert "wk" in line.timing
        assert line.pace_rationale  # non-empty
        assert "VIX" in line.pace_rationale

    # ------------------------------------------------------------------
    # (c) Small line (<= 5k) -> lump ("now") even with context
    # ------------------------------------------------------------------
    def test_small_line_stays_now_with_context(self, monkeypatch):
        """Lines <= DCA_LUMP_THRESHOLD_USD always get timing='now' regardless of context."""
        from datetime import date
        import argosy.services.allocation_engine as eng
        # Patch to emit one small line and one large line.
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [
            _candidate(("BUY", "CSPX", 4_000.0, "cash")),   # small -> lump
            _candidate(("BUY", "EXUS", 20_000.0, "cash")),  # large -> DCA
        ])
        ctx = _stub_market_context(vix=35.0, sp500=6_000.0, is_stale=False)
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={}, deploy_amount_usd=24_000.0,
            as_of=date(2026, 6, 12), market_context=ctx,
        )
        lines = {l.symbol: l for t in plan.tiers for l in t.lines}
        assert lines["CSPX"].timing == "now", "small line must stay lump"
        assert lines["CSPX"].pace_rationale == "small line — deploy whole"
        assert lines["EXUS"].timing.startswith("DCA"), "large line must be DCA"

    # ------------------------------------------------------------------
    # (d) Stale context -> staleness caveat in plan.caveats
    # ------------------------------------------------------------------
    def test_stale_context_adds_staleness_caveat(self, monkeypatch):
        """is_any_stale context -> WARNING caveat in plan.caveats."""
        from datetime import date
        import argosy.services.allocation_engine as eng
        monkeypatch.setattr(eng, "cash_only_deploy", lambda *a, **k: [_candidate(
            ("BUY", "CSPX", 10_000.0, "cash"),
        )])
        ctx = _stub_market_context(vix=20.0, sp500=5_500.0, is_stale=True)
        plan = assemble_deployment_plan(
            doc=self._doc(), holdings={}, deploy_amount_usd=10_000.0,
            as_of=date(2026, 6, 12), market_context=ctx,
        )
        assert ctx.is_any_stale is True
        stale_caveats = [c for c in plan.caveats if "stale" in c.lower() or "WARNING" in c]
        assert stale_caveats, f"expected staleness caveat; got caveats={plan.caveats}"
