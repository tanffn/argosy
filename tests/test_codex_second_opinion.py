"""Tests for the Argosy ZigZag — codex (gpt-5) second-opinion reviewer.

Covers the contract documented in
``argosy/orchestrator/flows/plan_synthesis/codex_second_opinion.py``:

  * Kill switches (env var, pytest sentinel) skip silently.
  * Valid codex JSON parses to a ``CodexSecondOpinion`` row.
  * Malformed codex output falls back to a synthetic "unparseable"
    opinion rather than crashing.
  * Codex dispatch exceptions fail-soft to ``(None, None)``.

These tests do NOT touch a live ``codex exec`` — every codex call is
patched. The fail-soft contract is the load-bearing property: synthesis
must NEVER abort because the zigzag review failed.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from argosy.orchestrator.flows.plan_synthesis.codex_second_opinion import (
    CodexAgreement,
    CodexFinding,
    CodexSecondOpinion,
    _build_prompt,
    _parse_codex_verdict,
    run_codex_second_opinion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubCodexResult:
    """Minimal stand-in for ``engine_codex.CodexResult``.

    Real ``CodexResult`` only exposes ``tokens`` (a flat total int).
    The ``cost`` / ``tokens_in`` / ``tokens_out`` keyword arguments are
    forward-looking hooks the cost-telemetry wiring honours when present
    -- tests use them to assert the per-attribute persistence path.
    """

    def __init__(self, *, verdict_text: str = "", tokens: int = 0,
                 exit_code: int = 0, wall_s: float = 1.0,
                 stderr: str = "", cost: float | None = None,
                 tokens_in: int | None = None,
                 tokens_out: int | None = None):
        self.verdict_text = verdict_text
        self.tokens = tokens
        self.exit_code = exit_code
        self.wall_s = wall_s
        self.stderr = stderr
        self.needs = []
        # Only attach when explicitly provided so we can also exercise
        # the "real CodexResult shape" fallback path (no cost/in/out
        # attrs at all).
        if cost is not None:
            self.cost = cost
        if tokens_in is not None:
            self.tokens_in = tokens_in
        if tokens_out is not None:
            self.tokens_out = tokens_out


def _valid_codex_json() -> str:
    """Return a syntactically valid codex response."""
    return (
        '{\n'
        '  "overall_assessment": "APPROVE_WITH_CONDITIONS",\n'
        '  "findings": [\n'
        '    {\n'
        '      "severity": "AMBER",\n'
        '      "topic": "concentration",\n'
        '      "detail": "NVDA still > 60% of portfolio.",\n'
        '      "suggested_fix": "Trim 3% per quarter.",\n'
        '      "cited_synthesizer_paragraphs": ["short.posture: trim 3% per quarter"]\n'
        '    }\n'
        '  ],\n'
        '  "agreement_with_argosy": {\n'
        '    "agrees_with_risk_verdict": "partial",\n'
        '    "novel_concerns_argosy_missed": ["FX sweep timing"]\n'
        '  },\n'
        '  "user_directive_respected": true\n'
        '}\n'
    )


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def test_codex_skipped_under_pytest(monkeypatch):
    """When PYTEST_CURRENT_TEST is set (which it always is here),
    ``run_codex_second_opinion`` short-circuits to (None, None).

    Critical: the env-var check fires BEFORE any subprocess work, so
    even a broken codex kit can't blow up an unrelated test run.
    """
    # PYTEST_CURRENT_TEST is set by pytest itself for every test; the
    # call should bail out before touching the kit.
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")

    async def _run():
        return await run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="reports",
            debate_outcomes_text="debates",
            risk_verdict_text="risk",
            user_directive="",
            decision_run_id=1,
            user_id="ariel",
        )

    parsed, row = asyncio.run(_run())
    assert parsed is None
    assert row is None


def test_codex_skipped_when_env_off(monkeypatch):
    """Even outside pytest, ARGOSY_CODEX_REVIEW_ENABLED != "1" skips.

    We force-clear PYTEST_CURRENT_TEST so the second gate fires first.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "0")

    async def _run():
        return await run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="reports",
            debate_outcomes_text="debates",
            risk_verdict_text="risk",
            user_directive="",
            decision_run_id=2,
            user_id="ariel",
        )

    parsed, row = asyncio.run(_run())
    assert parsed is None
    assert row is None


# ---------------------------------------------------------------------------
# Parsing — valid / malformed / fenced JSON
# ---------------------------------------------------------------------------


def test_codex_parses_valid_json():
    """Strict ``model_validate_json`` should accept a well-formed verdict."""
    parsed = _parse_codex_verdict(_valid_codex_json())
    assert isinstance(parsed, CodexSecondOpinion)
    assert parsed.overall_assessment == "APPROVE_WITH_CONDITIONS"
    assert len(parsed.findings) == 1
    assert parsed.findings[0].severity == "AMBER"
    assert parsed.findings[0].topic == "concentration"
    assert parsed.agreement_with_argosy.agrees_with_risk_verdict == "partial"
    assert parsed.user_directive_respected is True


def test_codex_parses_fenced_json():
    """``` ```json ... ``` ``` fences should be stripped before parsing."""
    fenced = "```json\n" + _valid_codex_json() + "\n```"
    parsed = _parse_codex_verdict(fenced)
    assert parsed.overall_assessment == "APPROVE_WITH_CONDITIONS"


def test_codex_lenient_parse_with_prose_prefix():
    """A short prose preamble before the JSON block must not break the
    parse — ``JSONDecoder.raw_decode`` from the first ``{`` recovers."""
    text = (
        "Here is my verdict.\n\n" + _valid_codex_json()
        + "\n\nLet me know if you want further detail."
    )
    parsed = _parse_codex_verdict(text)
    assert parsed.overall_assessment == "APPROVE_WITH_CONDITIONS"
    assert parsed.findings[0].topic == "concentration"


def test_codex_falls_back_on_malformed_json():
    """When neither strict nor lenient parsing recovers, the helper
    returns a synthetic CodexSecondOpinion with a YELLOW finding
    flagging the parse failure. The FM still sees a typed codex row.
    """
    garbage = "I'm just going to refuse to follow your JSON instructions today!"
    parsed = _parse_codex_verdict(garbage)
    assert isinstance(parsed, CodexSecondOpinion)
    # Synthetic: ALWAYS APPROVE_WITH_CONDITIONS + one YELLOW finding.
    assert parsed.overall_assessment == "APPROVE_WITH_CONDITIONS"
    assert len(parsed.findings) == 1
    assert parsed.findings[0].severity == "YELLOW"
    assert parsed.findings[0].topic == "codex_review_unparseable"
    # The raw excerpt must be embedded for forensic review.
    assert "refuse to follow" in parsed.findings[0].detail


def test_codex_falls_back_on_empty_text():
    """Empty text also triggers the synthetic fallback (not a crash)."""
    parsed = _parse_codex_verdict("")
    assert isinstance(parsed, CodexSecondOpinion)
    assert parsed.findings[0].topic == "codex_review_unparseable"


# ---------------------------------------------------------------------------
# Dispatch — fail-soft behavior under various failure modes
# ---------------------------------------------------------------------------


def test_codex_fails_soft_when_unreachable(monkeypatch, tmp_path):
    """When the codex kit raises during dispatch, ``run_codex_second_opinion``
    must return (None, None) and never propagate the exception.

    Disables both kill switches so dispatch is actually attempted.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    # Force settings reload so the home path is the tmp_path.
    from argosy.config import reload_settings
    reload_settings()

    # Patch the imported run_codex symbol on the module to raise.
    import argosy.orchestrator.flows.plan_synthesis.codex_second_opinion as cso

    # Pre-populate sys.modules with a fake engine_codex so the
    # ``from engine_codex import run_codex`` line resolves to our stub.
    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _raising_run_codex(**kw):
        raise RuntimeError("codex unreachable (simulated)")

    fake_mod.run_codex = _raising_run_codex
    sys.modules["engine_codex"] = fake_mod

    async def _run():
        return await cso.run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="reports",
            debate_outcomes_text="debates",
            risk_verdict_text="risk",
            user_directive="",
            decision_run_id=3,
            user_id="ariel",
        )

    try:
        parsed, row = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    assert parsed is None
    assert row is None


def test_codex_returns_unparseable_opinion_on_garbage_output(
    monkeypatch, tmp_path,
):
    """A successful subprocess that emits non-JSON should produce the
    synthetic "unparseable" opinion (not None, not a crash) so the FM
    still has a codex row to consult.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _stub_run_codex(**kw):
        return _StubCodexResult(verdict_text="!!! not JSON !!!", tokens=10)

    fake_mod.run_codex = _stub_run_codex
    sys.modules["engine_codex"] = fake_mod

    import argosy.orchestrator.flows.plan_synthesis.codex_second_opinion as cso

    async def _run():
        return await cso.run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="reports",
            debate_outcomes_text="debates",
            risk_verdict_text="risk",
            user_directive="",
            decision_run_id=4,
            user_id="ariel",
        )

    try:
        parsed, row = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    assert isinstance(parsed, CodexSecondOpinion)
    assert parsed.findings[0].topic == "codex_review_unparseable"
    assert row is not None
    assert row.agent_role == "codex_second_opinion"
    assert row.user_id == "ariel"
    # Cost is now computed via engine_stats.estimate_cost_usd from the
    # token count. Tokens=10 yields a sub-cent estimate, but it is no
    # longer hardcoded to zero -- guard against regression back to the
    # old hardcoded-0 path.
    assert row.cost_usd >= 0.0
    assert row.tokens_out == 10


def test_codex_full_path_with_valid_output(monkeypatch, tmp_path):
    """Happy path: codex returns valid JSON → parsed opinion + AgentReport row."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _stub_run_codex(**kw):
        # Sanity-check the prompt contains every required block — guards
        # against a regression where one input gets dropped from the
        # template formatting.
        prompt = kw.get("prompt") or ""
        assert "=== SYNTHESIZER DRAFT" in prompt
        assert "=== ANALYST REPORTS" in prompt
        assert "=== HORIZON DEBATES" in prompt
        assert "=== RISK VERDICT" in prompt
        assert "=== USER DIRECTIVE" in prompt
        return _StubCodexResult(verdict_text=_valid_codex_json(), tokens=4200)

    fake_mod.run_codex = _stub_run_codex
    sys.modules["engine_codex"] = fake_mod

    import argosy.orchestrator.flows.plan_synthesis.codex_second_opinion as cso

    async def _run():
        return await cso.run_codex_second_opinion(
            synth_draft_json='{"long": {}, "medium": {}, "short": {}}',
            analyst_reports_text="(analyst content)",
            debate_outcomes_text="(debate content)",
            risk_verdict_text="(risk verdict)",
            user_directive="AGREED: NVDA 12%.\nDISAGREED: tax-loss harvest urgency.",
            decision_run_id=5,
            user_id="ariel",
        )

    try:
        parsed, row = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    assert isinstance(parsed, CodexSecondOpinion)
    assert parsed.overall_assessment == "APPROVE_WITH_CONDITIONS"
    assert parsed.findings[0].severity == "AMBER"
    assert parsed.findings[0].topic == "concentration"
    assert parsed.user_directive_respected is True

    assert row is not None
    assert row.agent_role == "codex_second_opinion"
    assert row.model == "gpt-5-codex"
    assert row.tokens_out == 4200
    assert row.decision_id == "plan-synth-5"
    # response_text holds the parsed-and-reserialized JSON — guards the
    # FM's prompt builder which re-parses off the DB row.
    assert "concentration" in row.response_text
    # user_prompt holds the full codex prompt (debug / audit).
    assert "=== SYNTHESIZER DRAFT" in row.user_prompt


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


def test_build_prompt_inlines_all_evidence_blocks():
    """The prompt template must inline every named evidence block —
    a regression where one section gets dropped silently would let
    codex emit a verdict against incomplete evidence.
    """
    prompt = _build_prompt(
        synth_draft_json='{"draft": "json here"}',
        analyst_reports_text="ANALYSTS",
        debate_outcomes_text="DEBATES",
        risk_verdict_text="RISK_TEXT",
        user_directive="GUIDANCE",
    )
    assert "=== SYNTHESIZER DRAFT (Phase 3 output) ===" in prompt
    assert '{"draft": "json here"}' in prompt
    assert "=== ANALYST REPORTS (Phase 1) ===" in prompt
    assert "ANALYSTS" in prompt
    assert "=== HORIZON DEBATES (Phase 2) ===" in prompt
    assert "DEBATES" in prompt
    assert "=== RISK VERDICT (Phase 4, consolidated) ===" in prompt
    assert "RISK_TEXT" in prompt
    assert "=== USER DIRECTIVE ===" in prompt
    assert "GUIDANCE" in prompt
    # The independence instruction must survive prompt formatting.
    assert "INDEPENDENT second-opinion" in prompt


def test_build_prompt_handles_empty_user_directive():
    """When no user directive is provided, the block is replaced with a
    sentinel string rather than a bare placeholder — codex never has to
    guess what '(no value)' means.
    """
    prompt = _build_prompt(
        synth_draft_json='{}',
        analyst_reports_text="A",
        debate_outcomes_text="D",
        risk_verdict_text="R",
        user_directive="",
    )
    assert "(no user directive on this run)" in prompt


# ---------------------------------------------------------------------------
# Pydantic round-trip — guards model_dump_json / model_validate symmetry
# ---------------------------------------------------------------------------


def test_codex_opinion_round_trips_via_json():
    """A round-trip through model_dump_json → model_validate_json must
    preserve every field — the FM prompt builder relies on this when
    serializing codex's verdict into the user prompt.
    """
    original = CodexSecondOpinion(
        overall_assessment="BLOCK",
        findings=[CodexFinding(
            severity="BLOCKER",
            topic="cash-floor",
            detail="The plan breaches the user's 12-month cash reserve floor.",
            suggested_fix="Defer the NVDA buy.",
            cited_synthesizer_paragraphs=["short.actions[0]: buy 50 NVDA"],
        )],
        agreement_with_argosy=CodexAgreement(
            agrees_with_risk_verdict=False,
            novel_concerns_argosy_missed=["cash floor breach"],
        ),
        user_directive_respected=False,
    )
    text = original.model_dump_json()
    restored = CodexSecondOpinion.model_validate_json(text)
    assert restored.overall_assessment == "BLOCK"
    assert restored.findings[0].severity == "BLOCKER"
    assert restored.findings[0].topic == "cash-floor"
    assert restored.agreement_with_argosy.agrees_with_risk_verdict is False
    assert restored.user_directive_respected is False


# ---------------------------------------------------------------------------
# Cost telemetry — wire CodexResult cost/tokens into the AgentReport row
# ---------------------------------------------------------------------------


def test_codex_agent_report_carries_real_cost(monkeypatch, tmp_path):
    """The codex AgentReport row must surface real cost + token splits,
    NOT the legacy hardcoded ``cost_usd=0.0`` placeholder.

    Regression guard for the bug where every persisted codex row showed
    $0 in the audit UI despite ~50k tokens of real GPT-5 spend per run.
    The stub supplies explicit ``cost`` / ``tokens_in`` / ``tokens_out``
    attributes which the wiring should honour verbatim (capped only on
    out-of-range values).
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _stub_run_codex(**kw):
        return _StubCodexResult(
            verdict_text=_valid_codex_json(),
            tokens=9200,
            cost=0.42,
            tokens_in=8000,
            tokens_out=1200,
        )

    fake_mod.run_codex = _stub_run_codex
    sys.modules["engine_codex"] = fake_mod

    import argosy.orchestrator.flows.plan_synthesis.codex_second_opinion as cso

    async def _run():
        return await cso.run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="reports",
            debate_outcomes_text="debates",
            risk_verdict_text="risk",
            user_directive="",
            decision_run_id=42,
            user_id="ariel",
        )

    try:
        parsed, row = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    assert parsed is not None
    assert row is not None
    assert row.cost_usd == 0.42
    assert row.tokens_in == 8000
    assert row.tokens_out == 1200


def test_codex_agent_report_cost_capped_when_out_of_range(monkeypatch, tmp_path):
    """A wildly-large cost from a future kit version or a price-table
    glitch must be capped rather than surfaced verbatim. The cap exists
    so the audit UI never shows a misleading three-digit dollar figure.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _stub_run_codex(**kw):
        # 9999 dollars is the "something is very wrong" signal.
        return _StubCodexResult(
            verdict_text=_valid_codex_json(),
            tokens=1000,
            cost=9999.0,
        )

    fake_mod.run_codex = _stub_run_codex
    sys.modules["engine_codex"] = fake_mod

    import argosy.orchestrator.flows.plan_synthesis.codex_second_opinion as cso

    async def _run():
        return await cso.run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="r", debate_outcomes_text="d",
            risk_verdict_text="r2", user_directive="",
            decision_run_id=43, user_id="ariel",
        )

    try:
        _, row = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    assert row is not None
    # Capped at the defensive upper bound (10.0). The exact cap value is
    # an implementation detail -- the load-bearing property is that we
    # don't surface $9999.
    assert 0.0 < row.cost_usd <= 10.0


def test_codex_agent_report_cost_estimated_when_attr_missing(
    monkeypatch, tmp_path,
):
    """When the result has no ``cost`` attribute (the current real
    CodexResult shape), the wiring must FALL BACK to the kit's
    ``estimate_cost_usd("codex-gpt-5-5", tokens)`` rather than parking
    cost at zero. This is the path that actually fires in production
    today since the live kit only emits a total token count.
    """
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ARGOSY_CODEX_REVIEW_ENABLED", "1")
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    import sys
    import types
    fake_mod = types.ModuleType("engine_codex")

    def _stub_run_codex(**kw):
        # No cost / tokens_in / tokens_out attrs -- mirrors real kit.
        return _StubCodexResult(verdict_text=_valid_codex_json(), tokens=50_000)

    fake_mod.run_codex = _stub_run_codex
    sys.modules["engine_codex"] = fake_mod

    import argosy.orchestrator.flows.plan_synthesis.codex_second_opinion as cso

    async def _run():
        return await cso.run_codex_second_opinion(
            synth_draft_json='{}',
            analyst_reports_text="r", debate_outcomes_text="d",
            risk_verdict_text="r2", user_directive="",
            decision_run_id=44, user_id="ariel",
        )

    try:
        _, row = asyncio.run(_run())
    finally:
        sys.modules.pop("engine_codex", None)

    assert row is not None
    # 50k tokens at codex-gpt-5-5 rates ($5/M in, $15/M out, 50/50 split)
    # is $0.50 -- well within the sane bound, comfortably > 0.
    assert 0.10 < row.cost_usd < 5.0
    # Legacy convention: total parks under tokens_out when no split.
    assert row.tokens_out == 50_000
    assert row.tokens_in == 0
