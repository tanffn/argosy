"""Unit tests for the whole-artifact READER reconcile-guidance helper.

Mirrors ``test_numeric_reconcile_guidance`` (the codex forcing loop). The reader
owns COHERENCE OF THE WHOLE; when it BLOCKS on a fixable coherence hole
(contradiction / cross-surface / fragile-claim / stale / regression), the
orchestrator folds the finding into synthesizer guidance and re-runs synthesis
to CORRECT it — instead of only blocking forever.
"""

from __future__ import annotations

from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
    _reader_coherence_reconcile_guidance,
)
from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)


def _v(assessment, findings):
    return WholeArtifactVerdict(overall_assessment=assessment, findings=findings)


def test_blocker_contradiction_triggers_reconcile():
    v = _v("BLOCK", [
        CoherenceFinding(
            kind="contradiction",
            severity="BLOCKER",
            detail="net worth stated as two different values",
            surfaces_cited=["NW = 11.95M", "NW = 14.15M"],
        ),
    ])
    g = _reader_coherence_reconcile_guidance(v)
    assert g is not None
    # The conflicting surfaces are quoted so the synthesizer can reconcile them.
    assert "11.95M" in g and "14.15M" in g


def test_stale_blocker_triggers_reconcile():
    v = _v("BLOCK", [
        CoherenceFinding(
            kind="stale",
            severity="BLOCKER",
            detail="2026-06-10 retainer rendered on-deck though overdue",
            surfaces_cited=["attorney retainer — on-deck (0 days)"],
        ),
    ])
    assert _reader_coherence_reconcile_guidance(v) is not None


def test_fragile_claim_blocker_triggers_reconcile():
    # A "reached" claim undercut by the plan's own tail is fixable by
    # qualifying it — that is legitimate (honest qualification), so it should
    # feed back into synthesis, not just block.
    v = _v("BLOCK", [
        CoherenceFinding(
            kind="fragile_claim",
            severity="BLOCKER",
            detail="FI 'reached' undercut by a -10% FX shortfall",
            surfaces_cited=["capital sufficiency reached"],
        ),
    ])
    assert _reader_coherence_reconcile_guidance(v) is not None


def test_amber_only_does_not_trigger():
    # AMBER/YELLOW only → advisory; no hard BLOCK, so no forced re-synth.
    v = _v("APPROVE_WITH_CONDITIONS", [
        CoherenceFinding(kind="cross_surface", severity="AMBER",
                         detail="minor label drift", surfaces_cited=["a", "b"]),
    ])
    assert _reader_coherence_reconcile_guidance(v) is None


def test_approve_does_not_trigger():
    assert _reader_coherence_reconcile_guidance(_v("APPROVE", [])) is None


def test_none_verdict_does_not_trigger():
    assert _reader_coherence_reconcile_guidance(None) is None


def test_infra_failure_block_does_not_trigger():
    # The synthetic fail-closed BLOCK (reader timed out / unparseable / empty
    # artifact) is an INFRA failure — re-running synthesis cannot fix a reader
    # that never produced a verdict, so it must NOT burn a reconcile round.
    v = _v("BLOCK", [
        CoherenceFinding(
            kind="other",
            severity="BLOCKER",
            detail=(
                "The whole-artifact reader returned NO output (timeout / "
                "dispatch failure) — the holistic coherence review did not run."
            ),
            surfaces_cited=[],
        ),
    ])
    assert _reader_coherence_reconcile_guidance(v) is None
