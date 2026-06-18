from argosy.quality.publish_gate import can_publish_plan, OpenFlag


_CLEAR_AUTHORITIES = {
    "codex": "APPROVE",
    "deterministic_gate": "pass",
    "fund_manager": "approve",
    "whole_artifact_reader": "approve",
    "rederivation": "ok",
}


def test_publishable_when_all_clear_and_no_open_flag():
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=[])
    assert decision.can_promote is True
    assert decision.blocking_authorities == []


def test_open_hard_flag_blocks_even_when_authorities_clear():
    flags = [OpenFlag(node_key="fi_margin_liquid_nis", kind="hard")]
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=flags)
    assert decision.can_promote is False
    assert any("fi_margin_liquid_nis" in r for r in decision.reasons)


def test_open_coherence_flag_blocks():
    flags = [OpenFlag(node_key="wealth_dashboard", kind="coherence")]
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=flags)
    assert decision.can_promote is False


def test_non_hard_non_coherence_flag_does_not_block():
    flags = [OpenFlag(node_key="appendix_note", kind="info")]
    decision = can_publish_plan(authorities=_CLEAR_AUTHORITIES, open_flags=flags)
    assert decision.can_promote is True


def test_missing_authority_still_blocks_via_promote_gate():
    partial = dict(_CLEAR_AUTHORITIES)
    del partial["rederivation"]
    decision = can_publish_plan(authorities=partial, open_flags=[])
    assert decision.can_promote is False
    assert "rederivation" in decision.blocking_authorities
