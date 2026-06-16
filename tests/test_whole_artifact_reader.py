"""Tests for the whole-artifact adversarial reader (Task 6).

Mirrors the test style of ``test_codex_second_opinion.py``: focused on the
fail-closed parse contract (the S21 lesson — a timed-out / unparseable
reviewer must BLOCK, never soft-pass).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
    _parse_verdict,
)


class _StubCodexResult:
    """Minimal stand-in for ``engine_codex.CodexResult``."""

    def __init__(self, *, verdict_text: str = "", tokens: int = 0):
        self.verdict_text = verdict_text
        self.tokens = tokens
        self.exit_code = 0
        self.wall_s = 1.0


def test_reader_hard_ceiling_times_out_a_hung_dispatch(monkeypatch, tmp_path):
    """A hung codex subprocess must not block synthesis forever.

    The reader mirrors the codex second-opinion backstop: an
    ``asyncio.wait_for`` keyed on ``_HARD_CEILING_S`` around the executor
    await. Monkeypatch the ceiling tiny (0.2s) + a 5s-sleeping ``run_codex``
    stub; the dispatch must take the fail-soft ``(None, None)`` path within
    ~1s (a DISPATCH timeout — NOT the parse fail-closed-to-BLOCK path, which
    only fires when codex actually returns empty/garbage text). Without the
    ``wait_for`` wrap this test hangs for the full 5s.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    import argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader as war
    monkeypatch.setattr(war, "_HARD_CEILING_S", 0.2)

    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _hanging_run_codex(**kw):
        time.sleep(5.0)
        return _StubCodexResult(
            verdict_text='{"overall_assessment": "APPROVE", "findings": []}',
            tokens=1,
        )

    fake_mod.run_codex = _hanging_run_codex
    sys.modules["engine_codex"] = fake_mod

    async def _run():
        # Time the AWAIT itself — what the orchestrator experiences. (The
        # orphaned executor thread keeps sleeping per the wait_for caveat;
        # asyncio.run teardown joins it, but the real long-lived orchestrator
        # loop never does, so synthesis is not blocked.)
        t0 = time.monotonic()
        result = await war.run_whole_artifact_review(
            assembled_artifact="A plan document.",
            external_context="today is 2026-06-16",
            prior_plan_text="prior plan",
            decision_run_id=99,
            user_id="ariel",
        )
        return result, time.monotonic() - t0

    try:
        (parsed, row), elapsed = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    # Fail-soft DISPATCH path: (None, None), not a synthetic BLOCK verdict.
    assert parsed is None
    assert row is None
    assert elapsed < 2.0, f"await blocked for {elapsed:.2f}s — ceiling did not trip"


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
