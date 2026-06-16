"""Tests for the run-106 finding [7] instrument-taxonomy coherence gate.

The defect: the plan correctly states SGLN is NOT a UCITS fund (a physical-gold
ETC), then routes the SAME ticker into an action described as a migration INTO
UCITS. A ticker's wrapper TYPE asserted in its description must not be
contradicted by its action text.
"""
from __future__ import annotations

from argosy.quality.gate_types import GateCheck
from argosy.quality.instrument_taxonomy_gate import check_instrument_taxonomy


def test_run106_defect_sgln_not_ucits_but_migrated_into_ucits() -> None:
    plan_text = (
        "SGLN is a physical-gold ETC, not a UCITS fund.\n"
        "Action: migrate SGLN into the UCITS wrapper."
    )
    violations = check_instrument_taxonomy(plan_text=plan_text)
    assert len(violations) == 1
    v = violations[0]
    assert v.check == GateCheck.INSTRUMENT_TAXONOMY
    assert "SGLN" in v.detail


def test_clean_sgln_not_ucits_and_not_in_any_migration_action() -> None:
    plan_text = (
        "SGLN is a physical-gold ETC, not a UCITS fund. It is retained as the "
        "gold sleeve and is not part of any consolidation action."
    )
    assert check_instrument_taxonomy(plan_text=plan_text) == []


def test_clean_genuine_ucits_instrument_in_a_ucits_migration_action() -> None:
    plan_text = (
        "VWRA is an Irish-domiciled UCITS ETF.\n"
        "Action: migrate VWRA into the UCITS wrapper to consolidate the core."
    )
    assert check_instrument_taxonomy(plan_text=plan_text) == []


def test_clean_mixed_clause_migrated_ticker_distinct_from_etc_ticker() -> None:
    # Reviewer's false-positive case: one clause names BOTH a legitimate UCITS
    # ticker being migrated (VWRA) AND a separate ETC that stays put (SGLN).
    # The not-UCITS cue ("ETC") is adjacent to SGLN (which is NOT migrated), and
    # the migration action's object is VWRA (which is never asserted non-UCITS).
    # No single ticker is both not-UCITS AND a migration object → no violation.
    plan_text = (
        "Migrate VWRA into the UCITS wrapper; note the existing gold ETC SGLN "
        "stays put."
    )
    assert check_instrument_taxonomy(plan_text=plan_text) == []
