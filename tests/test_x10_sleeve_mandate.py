"""The x10-ASYMMETRY sleeve mandate (Ariel, 2026-07-06) — binding criterion for
the permanent ~5% high-growth / moonshot sleeve: cap-math x10 test, accepted
per-name loss = 100% (a SIZING rule), rank = (upside x plausibility) / DOWNSIDE
with a written floor, growth stories eligible at a smaller cut, deploy fill
order = asymmetry-first.

These tests pin the mandate into every surface that grades, ranks, sizes, or
fills the sleeve, so a future tranche can't quietly revert to safety-first
(the failure that sent the first live tranche into $70-120B maybe-2x names).
"""
from __future__ import annotations

from argosy.services.high_potential_sleeve import X10_SLEEVE_MANDATE


# --- the mandate text itself -------------------------------------------------

def test_mandate_encodes_all_four_clauses():
    m = X10_SLEEVE_MANDATE
    # (a) cap-math test, with the mechanical size preference + the >$50B bar
    assert "CAP-MATH" in m and "10x" in m and "5-10 years" in m
    assert "$20-30B" in m and "$50B" in m and "EXTRAORDINARY" in m.upper()
    # (b) 100% accepted per-name loss is a SIZING rule. Quality/defensibility
    # (a compounder trait) still must not boost rank -- but a DOWNSIDE FLOOR is
    # a different thing and MUST boost it. Reversed 2026-08-21: the old mandate
    # conflated the two and so forbade crediting the floor, which is precisely
    # what made SanDisk (0.72x book, P/S 0.91x) an asymmetry rather than a bet.
    assert "100%" in m and "defensibility" in m.lower()
    assert "SIZING rule, not a ranking" in m
    assert "MUST boost rank" in m
    assert "conflate" in m.lower()
    # (c) rank is a RATIO -- upside alone is variance, and variance is symmetric
    assert "/ plausible DOWNSIDE" in m and "RATIO" in m
    assert "VARIANCE" in m and "symmetric" in m
    assert "floored name MUST outrank an unfloored one" in m
    # (c2) the floor must be written down; undeclared == none
    assert "WRITE THE FLOOR DOWN" in m and "no floor" in m
    # (c3) the SanDisk calibration, stated ACCURATELY. Corrected 2026-08-21:
    # the first version claimed SNDK traded below book. It did not -- $4.999B of
    # its $9.216B equity was goodwill, so tangible book was $4.217B and the
    # stock was at 1.58x TANGIBLE book. It was also not "losing money": FY25
    # gross profit +$2.212B, operating income +$0.507B, operating cash flow
    # +$0.084B; the GAAP loss was a noncash goodwill impairment. The real
    # pattern is a depressed cyclical/spinoff valuation on cash-generating
    # operations -- NOT liquidation-value protection. These asserts exist so the
    # false version cannot come back.
    assert "SanDisk" in m and "0.91x" in m
    assert "0.72x BOOK" not in m           # the false claim, banned
    assert "1.58x TANGIBLE book" in m and "GOODWILL" in m
    assert "NONCASH goodwill impairment" in m
    assert "NOT liquidation-value protection" in m
    # and the base-rate caveat, so one winner is never read as a rule
    assert "SURVIVOR-BIAS WARNING" in m and "base rate" in m
    # (c4) growth stories stay eligible but take a smaller cut (Ariel 2026-08-21)
    assert "BOTH ARCHETYPES ARE ELIGIBLE" in m
    assert "SMALLER cut" in m and "HALF the weight" in m and "ONE " in m
    # (d) asymmetry-first fill order for deploy tranches
    assert "asymmetry-first" in m and "never safety-first" in m
    # the anti-goal is spelled out (maybe-2x large caps are the opposite job)
    assert "2x" in m and "OPPOSITE" in m


# --- discovery/triage graders embed the mandate -------------------------------

def test_quick_estimator_prompt_carries_mandate():
    from argosy.agents.quick_estimator import QuickEstimatorAgent

    a = QuickEstimatorAgent(user_id="ariel")
    system, user = a.build_prompt(candidate={
        "ticker": "RKLB", "name": "Rocket Lab", "score": 0.9,
        "families": ["space"], "price": 30.0, "market_cap": 15e9,
        "dollar_volume": 2e8, "pct_change": 1.0, "reasons": ["backlog"],
    })
    assert X10_SLEEVE_MANDATE in system
    # conviction is redefined as the asymmetry grade, not safety
    assert "ASYMMETRY" in system
    assert "defensibility" in system.lower()


def test_discovery_grader_prompt_carries_mandate():
    from argosy.services.discovery_grader import DiscoveryGraderAgent

    a = DiscoveryGraderAgent(user_id="ariel")
    system, user = a.build_prompt(ticker="RKLB", analyst_reports=[])
    assert X10_SLEEVE_MANDATE in system
    # BUY is tied to passing cap-math; a safe maybe-2x compounder is a PASS
    assert "cap-math" in system.lower()
    assert "PASS" in system and "2x" in system


# --- deploy path: packet carries the mandate, author prompt renders it --------

class _Inst:
    def __init__(self, symbol, domicile="", weight=0.0):
        self.symbol = symbol
        self.domicile = domicile
        self.weight_within_class_pct = weight


class _Cls:
    def __init__(self, label, target_pct, snapshot_category, instruments,
                 sigma_class=""):
        self.label = label
        self.target_pct = target_pct
        self.snapshot_category = snapshot_category
        self.instruments = instruments
        self.sigma_class = sigma_class


class _Doc:
    nvda_cap_pct = 13.0

    def __init__(self, classes):
        self.classes = classes


def _doc_with_moonshot():
    return _Doc(classes=[
        _Cls("Ex-US developed", 15.0, "ex_us", [_Inst("EXUS", "IE")],
             sigma_class="ex_us_equity"),
        _Cls("High-growth / high-potential", 5.0, "Individual Stocks",
             [_Inst("RKLB", "US", 40.0), _Inst("RXRX", "US", 30.0),
              _Inst("OKLO", "US", 30.0)],
             sigma_class="high_growth_basket"),
    ])


def test_packet_attaches_mandate_to_moonshot_sleeve_only():
    from argosy.services.allocation_author.packet import build_decision_packet

    pkt = build_decision_packet(
        doc=_doc_with_moonshot(), holdings_usd={}, deployable_usd=10_000.0,
    )
    menu = {m["sleeve"]: m for m in pkt["plan_menu"]}
    assert menu["High-growth / high-potential"]["mandate"] == X10_SLEEVE_MANDATE
    assert "mandate" not in menu["Ex-US developed"]


def test_deployment_author_prompt_renders_sleeve_mandate():
    from argosy.agents.deployment_author import DeploymentAuthorAgent
    from argosy.services.allocation_author.packet import build_decision_packet

    pkt = build_decision_packet(
        doc=_doc_with_moonshot(), holdings_usd={}, deployable_usd=10_000.0,
    )
    a = DeploymentAuthorAgent(user_id="ariel")
    system, user = a.build_prompt(packet=pkt)
    # the mandate's load-bearing clauses reach the author verbatim
    assert "CAP-MATH" in user and "asymmetry-first" in user
    # and the system prompt makes per-sleeve mandates binding on fill order
    assert "SLEEVE MANDATES" in system and "ASYMMETRY-FIRST" in system


# --- plan-change team: re-sourcing agents embed the mandate + blind review ----

def test_moonshot_author_and_reviewer_prompts_carry_mandate():
    from argosy.agents.plan_change_team import (
        MoonshotSleeveAuthorAgent,
        MoonshotSleeveBlindReviewerAgent,
    )

    sleeve = [{"ticker": "NU", "weight_pct": 16.0, "thesis": "LatAm bank"}]
    book = {"book_usd": 1_000_000, "nvda_lookthrough_pct": 11.4,
            "us_facing_pct": 60.0, "holdings": {"NVDA": 300_000.0}}
    author = MoonshotSleeveAuthorAgent(user_id="ariel")
    a_sys, a_user = author.build_prompt(current_sleeve=sleeve, book=book)
    assert X10_SLEEVE_MANDATE in a_sys
    assert "NU" in a_user and "16.0%" in a_user

    reviewer = MoonshotSleeveBlindReviewerAgent(user_id="ariel")
    r_sys, r_user = reviewer.build_prompt(current_sleeve=sleeve, book=book)
    assert X10_SLEEVE_MANDATE in r_sys
    # blind: the reviewer is told it has NOT seen the author's composition
    assert "NOT seen" in r_sys
    # web verification of market caps is demanded of both
    assert "WebSearch" in a_user and "WebSearch" in r_user
    # exits must not force-sell existing small fills
    assert "never a forced sell" in a_sys and "never a forced sell" in r_sys


def test_moonshot_divergence_compared_in_code():
    from argosy.agents.plan_change_team import (
        MoonshotName,
        MoonshotSleeveComposition,
        moonshot_divergences,
    )

    author = MoonshotSleeveComposition(names=[
        MoonshotName(ticker="RKLB", action="KEEP", weight_pct=40.0),
        MoonshotName(ticker="OKLO", action="KEEP", weight_pct=60.0),
        MoonshotName(ticker="NU", action="EXIT", weight_pct=0.0),
    ])
    reviewer = MoonshotSleeveComposition(names=[
        MoonshotName(ticker="RKLB", action="KEEP", weight_pct=50.0),
        MoonshotName(ticker="RXRX", action="ADD", weight_pct=50.0),
    ])
    div = moonshot_divergences(author, reviewer)
    joined = "\n".join(div)
    assert "OKLO" in joined            # author-only keep
    assert "RXRX" in joined            # reviewer-only add
    assert "RKLB" in joined            # >5pp weight divergence
    assert "NU" not in joined          # both exclude it from the final sleeve
    # agreement case
    assert moonshot_divergences(author, author) == []


# --- refinement machinery: instruments override is honoured -------------------

def test_refinement_hg_override_replaces_instruments_only_when_sleeve_exists():
    """Unit-level: the override substitutes instruments but cannot conjure a
    sleeve when the current plan carries none (hg_pct == 0)."""
    from argosy.services.plan_refinement import _fixed_sleeves_from_current

    class _NoDoc:
        target_allocation_json = None

    pct, instruments = _fixed_sleeves_from_current(_NoDoc())
    assert pct == 0.0 and instruments == ()
