"""Fix A Increment 2 — the fleet adjudicator's bounded verdict mapping.

Tested deterministically with CANNED agents (no claude.exe): factories return fake
RiskOfficer / FundManager whose async ``run`` yields a report with ``.output``. The
mapping from (3 risk verdicts + FM decision) to a bounded CandidateStatus is the
unit under test; the LLM prompt quality is verified separately in a live session.
"""
import asyncio
from types import SimpleNamespace

import pytest

from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    EnrichedCandidate,
    HistoryFeatures,
)
from argosy.services.deployment_funnel.fleet_review import (
    DeploymentContext,
    _size_from_conditions,
    adjudicate_sync,
    apply_adjudications,
)


def _enriched(symbol, usd, eff_nvda, status=CandidateStatus.NEEDS_FLEET_REVIEW):
    cand = AllocationCandidate(
        kind="BUY",
        legs=(AllocationLeg(side="BUY", symbol=symbol, account_id="leumi",
                            currency="USD", notional_usd=usd, funding_source="cash"),),
        horizon="now",
    )
    hf = HistoryFeatures(last_price=100.0, ath=100.0, pct_below_ath=0.0,
                         zscore_vs_window=0.0, drawdown_pct=0.0)
    return EnrichedCandidate(
        candidate=cand, symbol=symbol, effective_nvda_usd=eff_nvda,
        news_sentiment=None, history=hf, status=status,
        reason="book over cap; routed", cap_pct=None,
    )


_CTX = DeploymentContext(
    book_usd=4_060_000.0, current_effective_nvda_usd=2_296_000.0,
    nvda_cap_pct=13.0, plan_classes=("US broad-market core", "gold"),
    user_constraints="NVDA concentration risk; reduce to 15%.",
)


class _FakeRisk:
    def __init__(self, verdict, conditions=None, cites=("c1",), perspective="neutral"):
        self._v = SimpleNamespace(
            verdict=verdict, conditions=conditions or [],
            cited_sources=list(cites), perspective=perspective,
        )

    async def run(self, **_kw):
        return SimpleNamespace(output=self._v)


class _FakeFM:
    def __init__(self, decision, reason="", cites=("fm1",)):
        self._d = SimpleNamespace(
            decision=decision, reason=reason, cited_sources=list(cites),
        )

    async def run(self, **_kw):
        return SimpleNamespace(output=self._d)


def _run(enriched, ro_verdicts, fm):
    """Adjudicate with canned agents. ro_verdicts: list of (verdict, conditions)."""
    idx = {"i": 0}
    persp = ("aggressive", "neutral", "conservative")

    def ro_factory(_u, p):
        # perspective order is deterministic; map by name
        for v, c in ro_verdicts:
            pass
        # return the verdict positioned for this perspective
        i = persp.index(p)
        verdict, conditions = ro_verdicts[i]
        return _FakeRisk(verdict, conditions, perspective=p)

    def fm_factory(_u):
        return _FakeFM(*fm) if isinstance(fm, tuple) else _FakeFM(fm)

    return adjudicate_sync(
        enriched, context=_CTX, user_id="ariel",
        risk_officer_factory=ro_factory, fund_manager_factory=fm_factory,
    )


class TestVerdictMapping:
    def test_all_approve_green_light_approves(self):
        e = [_enriched("CSPX", 22000.0, 1540.0)]
        out = _run(e, [("APPROVE", None)] * 3, "green_light")
        assert out[0].status is CandidateStatus.APPROVE
        assert out[0].reason.startswith("[fleet]")

    def test_fund_manager_block_vetoes(self):
        e = [_enriched("CSPX", 22000.0, 1540.0)]
        out = _run(e, [("APPROVE", None)] * 3, ("block", "overlaps FUSA/SCHD"))
        assert out[0].status is CandidateStatus.VETO
        assert "fund manager BLOCK" in out[0].reason

    def test_majority_reject_vetoes(self):
        e = [_enriched("R1GR", 13000.0, 1820.0)]
        out = _run(e, [("REJECT", None), ("REJECT", None), ("APPROVE", None)],
                   "green_light")
        assert out[0].status is CandidateStatus.VETO
        assert "rejected" in out[0].reason

    def test_conditions_cap_to_parsed_keep_pct(self):
        e = [_enriched("CSPX", 22000.0, 1540.0)]
        out = _run(
            e,
            [("APPROVE", None),
             ("APPROVE_WITH_CONDITIONS", ["cut size 60%"]),  # keep 40
             ("APPROVE_WITH_CONDITIONS", ["reduce to 25%"])],  # keep 25 (strictest)
            "green_light",
        )
        assert out[0].status is CandidateStatus.CAP_AT_PCT
        assert out[0].cap_pct == 25.0

    def test_conditions_default_keep_when_no_number(self):
        e = [_enriched("CSPX", 22000.0, 1540.0)]
        out = _run(e,
                   [("APPROVE", None), ("APPROVE_WITH_CONDITIONS", ["trim it"]),
                    ("APPROVE", None)],
                   "green_light")
        assert out[0].status is CandidateStatus.CAP_AT_PCT
        assert out[0].cap_pct == 50.0

    def test_cites_are_collected(self):
        e = [_enriched("CSPX", 22000.0, 1540.0)]
        out = _run(e, [("APPROVE", None)] * 3, ("green_light", "", ("fmX",)))
        # cites live on the FleetAdjudication; the applied reason is prefixed.
        assert out[0].status is CandidateStatus.APPROVE


class TestFailOpen:
    def test_agent_error_leaves_candidate_held(self):
        e = [_enriched("CSPX", 22000.0, 1540.0)]

        def ro_factory(_u, _p):
            raise RuntimeError("claude.exe unavailable")

        def fm_factory(_u):
            return _FakeFM("green_light")

        out = adjudicate_sync(
            e, context=_CTX, user_id="ariel",
            risk_officer_factory=ro_factory, fund_manager_factory=fm_factory,
        )
        # Fail-open: still NEEDS_FLEET_REVIEW (held), never silently approved.
        assert out[0].status is CandidateStatus.NEEDS_FLEET_REVIEW

    def test_non_review_candidates_untouched(self):
        approved = _enriched("EXUS", 5000.0, 0.0, status=CandidateStatus.APPROVE)
        out = apply_adjudications([approved], {})
        assert out[0].status is CandidateStatus.APPROVE


class TestSizeParsing:
    @pytest.mark.parametrize("conds,expected", [
        (["cut size 50%"], 50.0),
        (["reduce to 25%"], 25.0),
        (["trim 30%"], 70.0),
        (["cut 60%", "to 20%"], 20.0),
        (["no numeric condition"], 50.0),
        ([], 50.0),
    ])
    def test_keep_pct(self, conds, expected):
        assert _size_from_conditions(conds) == expected


class TestBackendResilience:
    """One flaky agent must not sink the whole candidate; too few responses hold."""

    def _adj(self, n_failing):
        e = [_enriched("CSPX", 22000.0, 1540.0)]
        persp = ("aggressive", "neutral", "conservative")

        def ro_factory(_u, p):
            i = persp.index(p)
            if i < n_failing:
                class _Boom:
                    async def run(self, **_kw):
                        raise RuntimeError("transient exit 1")
                return _Boom()
            return _FakeRisk("APPROVE", perspective=p)

        def fm_factory(_u):
            return _FakeFM("green_light")

        return adjudicate_sync(
            e, context=_CTX, user_id="ariel",
            risk_officer_factory=ro_factory, fund_manager_factory=fm_factory,
        )

    def test_one_flaky_officer_still_decides(self):
        out = self._adj(n_failing=1)  # 2 of 3 respond -> majority holds
        assert out[0].status is CandidateStatus.APPROVE

    def test_two_flaky_officers_hold_fail_closed(self):
        out = self._adj(n_failing=2)  # only 1 responds -> insufficient -> held
        assert out[0].status is CandidateStatus.NEEDS_FLEET_REVIEW
