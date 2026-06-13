"""Tests for the Alternatives sourcer agent + its estate/domicile gate.

The Alternatives sleeve must be AGENT-DERIVED and ESTATE-GATED, not hardcoded.
These tests cover the schema, the prompt's hard constraints, the domicile gate
(RED US-situs rejected, all-non-US clean), and the agent's role/model wiring.
No live LLM is called — build_prompt / schema / gate are deterministic.
"""

from __future__ import annotations

import pytest

from argosy.agents.alternatives_sourcer import (
    AlternativesProposal,
    AlternativesSourcerAgent,
    AssetProposal,
)
from argosy.services.alternatives_sourcing import gate_proposal


def _asset(symbol="IGLN", domicile="IE", weight=60.0, **kw) -> AssetProposal:
    base = dict(
        symbol=symbol,
        name=f"{symbol} fund",
        asset_class="precious_metals",
        domicile=domicile,
        isin="IE00B4ND3602",
        weight_within_sleeve_pct=weight,
        conviction="HIGH",
        thesis_md="diversifier",
        cites=["domain_knowledge/tax/us/estate_tax_nonresidents.md"],
    )
    base.update(kw)
    return AssetProposal(**base)


def _proposal(assets, sleeve_pct=4.0) -> AlternativesProposal:
    return AlternativesProposal(
        sleeve_pct=sleeve_pct,
        rationale_md="small diversifier sleeve",
        proposals=assets,
        cited_sources=["domain_knowledge/tax/us/estate_tax_nonresidents.md"],
    )


# --- schema ---------------------------------------------------------------


def test_schema_validates_and_weights_sum_helper() -> None:
    prop = _proposal(
        [_asset("IGLN", "IE", 60.0), _asset("IB1T", "CH", 40.0,
                                            asset_class="crypto", isin="XS2940466316")]
    )
    assert prop.sleeve_pct == 4.0
    assert len(prop.proposals) == 2
    assert prop.weights_sum() == 100.0
    # round-trips through JSON
    AlternativesProposal.model_validate_json(prop.model_dump_json())


def test_asset_proposal_isin_optional() -> None:
    a = _asset("XAUF", "DE", 100.0, isin=None)
    assert a.isin is None


# --- prompt ---------------------------------------------------------------


def test_build_prompt_includes_hard_constraints() -> None:
    agent = AlternativesSourcerAgent(user_id="ariel")
    system, user = agent.build_prompt(
        macro_context={"regime": "late-cycle"},
        sleeve_pct=4.0,
        constraints="keep small; estate-safe only",
    )
    blob = (system + "\n" + user).lower()
    # non-US-domicile hard constraint
    assert "non-us-domiciled" in blob or "non-us domiciled" in blob
    assert "us-situs" in blob
    assert "estate" in blob
    # weights sum to 100
    assert "sum to 100" in blob
    # the user is NOT to be asked — team decides
    assert "not to be asked" in system.lower()
    # domicile + ISIN + source required
    assert "isin" in blob
    assert "domicile" in blob
    # flags US-only-good exposures rather than smuggling them
    assert "flag" in blob


# --- gate -----------------------------------------------------------------


def test_gate_rejects_us_domiciled_red() -> None:
    prop = _proposal(
        [
            _asset("IGLN", "IE", 50.0),
            _asset("IBIT", "US", 50.0, asset_class="crypto", isin="US4642875235"),
        ]
    )
    clean, violations = gate_proposal(prop)
    # the US-domiciled pick is excluded from clean
    clean_syms = {i.symbol for i in clean}
    assert "IBIT" not in clean_syms
    assert "IGLN" in clean_syms
    # and surfaces as a RED violation
    reds = [v for v in violations if v.severity == "RED"]
    assert any(v.symbol == "IBIT" for v in reds)


def test_gate_passes_all_non_us_clean() -> None:
    prop = _proposal(
        [
            _asset("IGLN", "IE", 60.0),
            _asset("IB1T", "CH", 40.0, asset_class="crypto", isin="XS2940466316"),
        ]
    )
    clean, violations = gate_proposal(prop)
    assert violations == []
    assert {i.symbol for i in clean} == {"IGLN", "IB1T"}
    # domicile stamped through to the typed instrument
    assert {i.domicile for i in clean} == {"IE", "CH"}


def test_gate_yellow_for_unstamped_domicile() -> None:
    # An unrecognised domicile string normalises to None -> YELLOW, excluded.
    prop = _proposal([_asset("MYSTERY", "Atlantis", 100.0)])
    clean, violations = gate_proposal(prop)
    assert clean == []
    assert len(violations) == 1
    assert violations[0].severity == "YELLOW"
    assert violations[0].symbol == "MYSTERY"


# --- agent wiring ---------------------------------------------------------


def test_agent_role_and_opus_model() -> None:
    agent = AlternativesSourcerAgent(user_id="ariel")
    assert agent.agent_role == "alternatives_sourcer"
    assert "opus" in agent.model.lower()
    assert agent.output_model is AlternativesProposal
    assert agent.require_citations is True
