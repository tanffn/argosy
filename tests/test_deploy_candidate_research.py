"""Fetch-before-buy: the decision packet carries fresh per-candidate research and
the author prompt renders it (additive — absent research changes nothing)."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.agents.deployment_author import DeploymentAuthorAgent
from argosy.services.allocation_author.packet import build_decision_packet


def _doc():
    return SimpleNamespace(nvda_cap_pct=13.0, classes=[])


def test_packet_carries_candidate_research_and_drops_empties():
    pkt = build_decision_packet(
        doc=_doc(), holdings_usd={}, deployable_usd=1000.0,
        candidate_research={"NU": "last price 12.3 | news: strong quarter", "RKLB": ""},
    )
    assert pkt["candidate_research"] == {"NU": "last price 12.3 | news: strong quarter"}


def test_packet_candidate_research_defaults_empty():
    pkt = build_decision_packet(doc=_doc(), holdings_usd={}, deployable_usd=1000.0)
    assert pkt["candidate_research"] == {}


def _prompt_for(research):
    agent = DeploymentAuthorAgent.__new__(DeploymentAuthorAgent)
    packet = {
        "deployable_usd": 1000.0, "nvda": {}, "reserve": {}, "plan_menu": [],
        "instrument_facts": [], "holdings": {}, "policy_signals": {},
        "candidate_research": research, "discovery_candidates": [],
        "user_constraints": "",
    }
    return DeploymentAuthorAgent.build_prompt(agent, packet=packet)[1]  # user prompt


def test_author_prompt_renders_research_when_present():
    user = _prompt_for({"NU": "last price 12.3 | news: strong quarter"})
    assert "FRESH PER-CANDIDATE RESEARCH" in user
    assert "NU: last price 12.3 | news: strong quarter" in user


def test_author_prompt_omits_research_section_when_empty():
    user = _prompt_for({})
    assert "FRESH PER-CANDIDATE RESEARCH" not in user


def test_fresh_search_candidates_are_separate_from_plan_and_eligible():
    discovery = [
        {
            "ticker": "RGTI",
            "search_source": "trend_scan_state",
            "score": 88.0,
            "rank": 1,
            "fresh_as_of": "2026-08-25T12:00:00+00:00",
        }
    ]
    pkt = build_decision_packet(
        doc=_doc(),
        holdings_usd={},
        deployable_usd=1000.0,
        extra_known_symbols={"RGTI"},
        discovery_candidates=discovery,
    )
    assert pkt["discovery_candidates"] == discovery
    assert "RGTI" in pkt["known_symbols"]


def test_author_prompt_allows_fresh_discovery_but_rejects_underweight_as_thesis():
    agent = DeploymentAuthorAgent.__new__(DeploymentAuthorAgent)
    packet = {
        "deployable_usd": 1000.0,
        "nvda": {},
        "reserve": {},
        "plan_menu": [],
        "instrument_facts": [],
        "holdings": {},
        "policy_signals": {},
        "candidate_research": {},
        "discovery_candidates": [
            {
                "ticker": "RGTI",
                "score": 88.0,
                "rank": 1,
                "fresh_as_of": "2026-08-25T12:00:00+00:00",
            }
        ],
        "user_constraints": "",
    }
    user = DeploymentAuthorAgent.build_prompt(agent, packet=packet)[1]
    assert "RGTI: search score 88.0" in user
    assert "Underweight is NOT a thesis" in user
