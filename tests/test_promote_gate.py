from argosy.quality.promote_gate import (
    evaluate_promotion, relabel_on_promote,
)


def _all_clear():
    return {
        "codex": "APPROVE", "deterministic_gate": True, "fund_manager": "approved",
        "whole_artifact_reader": "APPROVE_WITH_CONDITIONS", "rederivation": True,
    }


def test_all_authorities_clear_promotes():
    assert evaluate_promotion(_all_clear()).can_promote is True


def test_any_block_refuses():
    a = _all_clear(); a["codex"] = "BLOCK"
    d = evaluate_promotion(a)
    assert d.can_promote is False and "codex" in d.blocking_authorities


def test_missing_authority_is_fail_closed():
    a = _all_clear(); del a["rederivation"]
    d = evaluate_promotion(a)
    assert d.can_promote is False and "rederivation" in d.blocking_authorities


def test_draft45_scenario_cannot_promote_even_with_reader_approve():
    # The exact draft-45 state: reader APPROVE but codex BLOCK + gate FAIL + FM rejected.
    a = {
        "codex": "BLOCK", "deterministic_gate": False, "fund_manager": "rejected",
        "whole_artifact_reader": "APPROVE_WITH_CONDITIONS", "rederivation": True,
    }
    d = evaluate_promotion(a)
    assert d.can_promote is False
    assert set(d.blocking_authorities) == {"codex", "deterministic_gate", "fund_manager"}


def test_relabel_strips_stale_suffix():
    assert relabel_on_promote("synth-2026-06-17-0356-fm-rejected") == "synth-2026-06-17-0356"
    assert relabel_on_promote("synth-2026-06-18-clean") == "synth-2026-06-18-clean"
