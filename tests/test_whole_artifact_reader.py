"""Tests for the whole-artifact adversarial reader (Task 6).

Mirrors the test style of ``test_codex_second_opinion.py``: focused on the
fail-closed parse contract (the S21 lesson — a timed-out / unparseable
reviewer must BLOCK, never soft-pass).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
    _parse_verdict,
)


def test_reader_parse_fails_closed_on_empty():
    v = _parse_verdict("")
    assert v.overall_assessment == "BLOCK"  # timeout/unparseable => fail closed (S21 lesson)
    assert any(f.severity == "BLOCKER" for f in v.findings)


def test_reader_parse_valid_json_with_contradiction():
    raw = (
        '{"overall_assessment": "BLOCK", "findings": ['
        '{"kind": "contradiction", "severity": "BLOCKER", '
        '"detail": "Net worth stated as X in one place and Y in another.", '
        '"surfaces_cited": ["X", "Y"]}]}'
    )
    v = _parse_verdict(raw)
    assert isinstance(v, WholeArtifactVerdict)
    assert v.overall_assessment == "BLOCK"
    assert len(v.findings) == 1
    f = v.findings[0]
    assert f.kind == "contradiction"
    assert f.severity == "BLOCKER"
    assert f.surfaces_cited == ["X", "Y"]


def test_reader_parse_strips_json_fences():
    raw = (
        "```json\n"
        '{"overall_assessment": "APPROVE", "findings": []}\n'
        "```"
    )
    v = _parse_verdict(raw)
    assert v.overall_assessment == "APPROVE"
    assert v.findings == []


def test_reader_parse_lenient_prose_with_embedded_json():
    raw = (
        "Here is my verdict after reading the whole document.\n"
        '{"overall_assessment": "APPROVE_WITH_CONDITIONS", "findings": ['
        '{"kind": "stale", "severity": "AMBER", "detail": "Date is old."}]}'
        "\nThat concludes my review."
    )
    v = _parse_verdict(raw)
    assert v.overall_assessment == "APPROVE_WITH_CONDITIONS"
    assert len(v.findings) == 1
    assert v.findings[0].kind == "stale"


def test_reader_parse_non_json_prose_fails_closed():
    raw = "I read the document and everything looks fine to me, no issues at all."
    v = _parse_verdict(raw)
    assert v.overall_assessment == "BLOCK"  # fail closed — no recoverable object
    assert any(f.severity == "BLOCKER" and f.kind == "other" for f in v.findings)


def test_coherence_finding_defaults():
    f = CoherenceFinding(kind="fragile_claim", severity="YELLOW", detail="x")
    assert f.surfaces_cited == []


def test_reader_parses_regression_kind():
    raw = (
        '{"overall_assessment": "BLOCK", "findings": ['
        '{"kind": "regression", "severity": "AMBER", '
        '"detail": "A hedge present in the prior plan was dropped.", '
        '"surfaces_cited": ["prior hedge", "current omission"]}]}'
    )
    v = _parse_verdict(raw)
    assert isinstance(v, WholeArtifactVerdict)
    assert v.overall_assessment == "BLOCK"
    assert len(v.findings) == 1
    f = v.findings[0]
    assert f.kind == "regression"  # now an allowed kind, preserved
    assert f.severity == "AMBER"
    assert f.surfaces_cited == ["prior hedge", "current omission"]


def test_reader_salvages_unknown_kind():
    raw = (
        '{"overall_assessment": "APPROVE_WITH_CONDITIONS", "findings": ['
        '{"kind": "totally_new_kind", "severity": "YELLOW", '
        '"detail": "Some new coherence issue.", '
        '"surfaces_cited": ["excerpt"]}]}'
    )
    v = _parse_verdict(raw)
    # SALVAGED — not fail-closed, not discarded.
    assert v.overall_assessment == "APPROVE_WITH_CONDITIONS"
    assert len(v.findings) == 1
    f = v.findings[0]
    assert f.kind == "other"  # unknown kind coerced defensively
    assert f.detail == "Some new coherence issue."
    assert f.surfaces_cited == ["excerpt"]


def test_reader_real_run102_recovers_all_findings():
    fixture = (
        Path(__file__).resolve().parents[1]
        / "logs" / "synthesis" / "whole_artifact_reader"
        / "run_102_3b1490df" / "result.md"
    )
    if not fixture.exists():
        pytest.skip(f"run-102 raw fixture not present at {fixture}")
    raw = fixture.read_text(encoding="utf-8")
    v = _parse_verdict(raw)
    assert v.overall_assessment == "BLOCK"
    assert len(v.findings) == 8  # all 8 real findings preserved, not discarded
    assert any(f.kind == "regression" for f in v.findings)
    assert any("net worth" in f.detail.lower() for f in v.findings)
