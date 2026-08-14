"""The contract that stops a non-running verifier from reading as a pass."""

from __future__ import annotations

import pytest

from argosy.quality.verification import (
    GateOutcome,
    GateStatus,
    blocks_promotion,
    summarize,
)


def test_did_not_run_is_not_a_pass() -> None:
    """The whole point: absence of a result must not read as a good result."""
    out = GateOutcome.did_not_run("codex_math", "codex 401 — kit unavailable")
    assert out.status is GateStatus.DID_NOT_RUN
    assert out.blocks() is True
    assert blocks_promotion([out]) == [out]


def test_pass_does_not_block() -> None:
    out = GateOutcome.passed("codex_math", "re-derived within 0.1%")
    assert out.blocks() is False
    assert blocks_promotion([out]) == []


def test_block_blocks() -> None:
    out = GateOutcome.blocked("plan_invariants", "sleeves sum to 110%")
    assert out.blocks() is True


def test_mixed_set_reports_only_the_offenders() -> None:
    outcomes = [
        GateOutcome.passed("codex_math"),
        GateOutcome.passed("publish_gate"),
        GateOutcome.did_not_run("whole_artifact_reader", "codex hung past ceiling"),
    ]
    blocking = blocks_promotion(outcomes)
    assert [o.gate for o in blocking] == ["whole_artifact_reader"]


def test_explicit_override_unblocks_but_preserves_status() -> None:
    """An override is a decision on the record, not a rewrite of history."""
    out = GateOutcome.did_not_run("whole_artifact_reader", "codex hung").with_override(
        by="ariel", reason="accepted risk; reviewed the draft by hand"
    )
    assert out.blocks() is False
    assert out.status is GateStatus.DID_NOT_RUN  # still visible on the receipt
    assert out.overridden is True
    assert blocks_promotion([out]) == []


def test_override_without_reason_is_rejected() -> None:
    with pytest.raises(ValueError, match="reason"):
        GateOutcome(
            gate="codex_math",
            status=GateStatus.DID_NOT_RUN,
            detail="kit missing",
            override_by="ariel",
        )


def test_non_pass_must_explain_itself() -> None:
    with pytest.raises(ValueError, match="must say why"):
        GateOutcome(gate="codex_math", status=GateStatus.BLOCK)


def test_empty_gate_name_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GateOutcome(gate="", status=GateStatus.PASS)


def test_summarize_names_the_gate_that_did_not_run() -> None:
    line = summarize(
        [
            GateOutcome.passed("codex_math"),
            GateOutcome.did_not_run("whole_artifact_reader", "codex hung"),
        ]
    )
    assert "1/2 gates passed" in line
    assert "whole_artifact_reader DID_NOT_RUN" in line
    assert "codex hung" in line


def test_summarize_marks_overrides() -> None:
    line = summarize(
        [
            GateOutcome.did_not_run("codex_math", "kit down").with_override(
                by="ariel", reason="hand-checked"
            )
        ]
    )
    assert "overridden by ariel" in line


def test_outcomes_are_immutable() -> None:
    out = GateOutcome.passed("codex_math")
    with pytest.raises(Exception):
        out.status = GateStatus.BLOCK  # type: ignore[misc]
