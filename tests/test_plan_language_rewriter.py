"""Phase 2 — PlanLanguageRewriter + rewriter_invariants validator.

Covers the two new artifacts:

- ``argosy/agents/plan_language_rewriter.py`` — the rewriter agent.
- ``argosy/quality/rewriter_invariants.py`` — the validator that
  catches any drift the rewriter introduces.

Plus the orchestrator's ``_run_plan_language_rewriter`` wrapper
(invariant-violation abort + crash fallback).
"""
from __future__ import annotations

from datetime import date

import pytest

from argosy.agents.plan_language_rewriter import PlanLanguageRewriter
from argosy.agents.plan_synthesizer_types import (
    Action,
    Delta,
    HorizonSection,
    PlanSynthesisOutput,
    SpeculativeCandidate,
    SynthesisInputs,
    SynthTarget,
    Theme,
)
from argosy.orchestrator.flows.plan_synthesis import (
    RewriterInvariantError,
    _run_plan_language_rewriter,
)
from argosy.quality.gate_types import GateCheck
from argosy.quality.rewriter_invariants import validate_rewriter_invariants


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _horizon(name: str, *, posture: str = "", rationale: str = "",
             targets=None, themes=None, actions=None,
             speculative_candidates=None, deltas=None) -> HorizonSection:
    return HorizonSection(
        horizon=name,
        freshness_expected="quarterly" if name == "medium" else (
            "annual" if name == "long" else "monthly"),
        status="minor_revision",
        posture=posture,
        rationale=rationale,
        targets=targets or [],
        themes=themes or [],
        actions=actions or [],
        speculative_candidates=speculative_candidates or [],
        deltas_from_prior=deltas or [],
    )


def _plan(
    long_section: HorizonSection,
    medium_section: HorizonSection,
    short_section: HorizonSection,
) -> PlanSynthesisOutput:
    return PlanSynthesisOutput(
        long=long_section,
        medium=medium_section,
        short=short_section,
        inputs=SynthesisInputs(),
    )


def _make_baseline_plan() -> PlanSynthesisOutput:
    """A clean plan with structurally meaningful fields populated."""
    nvda_target = SynthTarget(
        label="NVDA share of portfolio",
        value=15,
        unit="pct_of_portfolio",
        stated_at=date(2026, 6, 2),
        revisit_after=date(2026, 9, 1),
        rationale="Glide toward strategic cap.",
    )
    theme = Theme(
        label="UCITS-first deployment",
        direction="lean_into",
        rationale="Estate-tax mitigation.",
    )
    action = Action(
        label="Sell 2500 NVDA shares",
        horizon_kind="dated",
        trigger_or_date="2026-09-15",
        detail="From pre-2024 grants only.",
        rationale="Section 102 capital-track eligible.",
    )
    spec = SpeculativeCandidate(
        ticker="AVGO",
        thesis_summary="Datacenter tailwind",
        suggested_position_usd=10000,
        suggested_position_pct_of_net_worth=0.005,
        risk_ceiling_check=True,
        horizon_days=30,
        expected_drawdown_pct=0.15,
        exit_trigger="-10% from entry",
    )
    delta = Delta(
        item_kind="target",
        item_id="medium.targets.nvda",
        horizon="medium",
        change_kind="modified",
        summary="Glide pace adjusted",
    )
    return _plan(
        long_section=_horizon(
            "long", posture="Long-horizon posture body.",
            rationale="Long rationale.",
            targets=[nvda_target.model_copy()],
            themes=[theme.model_copy()],
            actions=[action.model_copy()],
        ),
        medium_section=_horizon(
            "medium", posture="Medium-horizon centerpiece.",
            rationale="Medium rationale.",
            targets=[nvda_target.model_copy()],
            themes=[theme.model_copy()],
            actions=[action.model_copy()],
            deltas=[delta],
        ),
        short_section=_horizon(
            "short", posture="Short-horizon mechanics.",
            rationale="Short rationale.",
            targets=[],
            themes=[],
            actions=[],
            speculative_candidates=[spec],
        ),
    )


# ---------------------------------------------------------------------------
# Validator unit tests — happy path
# ---------------------------------------------------------------------------

def test_validator_passes_when_rewrite_only_touches_prose():
    """Rewriter changes posture / rationale / theme.label etc. but
    preserves every structured field — validator returns []."""
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.long.posture = "Long-horizon posture, plain English."
    after.medium.rationale = "Medium rationale, simpler wording."
    after.short.themes = before.short.themes  # bit-equal
    violations = validate_rewriter_invariants(before, after)
    assert violations == [], (
        f"clean rewrite produced violations: "
        f"{[v.detail for v in violations[:5]]}"
    )


# ---------------------------------------------------------------------------
# Validator unit tests — structural preservation
# ---------------------------------------------------------------------------

def test_validator_catches_added_theme():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.long.themes = list(after.long.themes) + [
        Theme(label="Bogus extra theme", direction="lean_into")
    ]
    violations = validate_rewriter_invariants(before, after)
    assert any(
        "long.themes" in v.detail and "count" in v.detail
        for v in violations
    ), f"expected long.themes count violation; got: {[v.detail for v in violations]}"


def test_validator_catches_target_value_change():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.medium.targets[0].value = 20  # was 15
    violations = validate_rewriter_invariants(before, after)
    assert any(
        "medium.target" in v.detail and ".value" in v.detail
        for v in violations
    )


def test_validator_catches_action_trigger_change():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.medium.actions[0].trigger_or_date = "2026-12-15"  # was 2026-09-15
    violations = validate_rewriter_invariants(before, after)
    assert any(
        "trigger_or_date" in v.detail for v in violations
    )


def test_validator_catches_theme_direction_change():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.long.themes[0].direction = "lean_away_from"
    violations = validate_rewriter_invariants(before, after)
    assert any("direction" in v.detail for v in violations)


def test_validator_catches_speculative_candidate_mutation():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.short.speculative_candidates[0].suggested_position_usd = 99999
    violations = validate_rewriter_invariants(before, after)
    assert any("speculative_candidates" in v.detail for v in violations)


def test_validator_catches_delta_mutation():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.medium.deltas_from_prior[0].summary = "DIFFERENT summary"
    violations = validate_rewriter_invariants(before, after)
    assert any("deltas_from_prior" in v.detail for v in violations)


# ---------------------------------------------------------------------------
# Validator unit tests — jargon / history checks on rewritten prose
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field_path,setter", [
    (
        "theme.rationale",
        lambda p, t: setattr(p.long.themes[0], "rationale", t),
    ),
    (
        "theme.label",
        lambda p, t: setattr(p.long.themes[0], "label", t),
    ),
    (
        "action.detail",
        lambda p, t: setattr(p.long.actions[0], "detail", t),
    ),
    (
        "action.rationale",
        lambda p, t: setattr(p.long.actions[0], "rationale", t),
    ),
    (
        "action.label",
        lambda p, t: setattr(p.long.actions[0], "label", t),
    ),
    (
        "target.rationale",
        lambda p, t: setattr(p.long.targets[0], "rationale", t),
    ),
    (
        "target.label",
        lambda p, t: setattr(p.long.targets[0], "label", t),
    ),
    (
        "posture",
        lambda p, t: setattr(p.medium, "posture", t),
    ),
    (
        "rationale",
        lambda p, t: setattr(p.medium, "rationale", t),
    ),
])
def test_validator_catches_jargon_in_every_rewritable_prose_field(
    field_path: str, setter,
):
    """The §5.2 contract requires the validator to scan EVERY rewritable
    prose path, not just posture/rationale. Plant a banned string in
    each path and verify the validator catches it."""
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    setter(after, "TaxAnalyst flagged this as critical concern.")
    violations = validate_rewriter_invariants(before, after)
    jargon_violations = [v for v in violations if v.check == GateCheck.JARGON_LEAK]
    assert jargon_violations, (
        f"jargon not detected in {field_path}; violations: "
        f"{[v.detail for v in violations[:5]]}"
    )


def test_validator_catches_history_narration_in_action_rationale():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.long.actions[0].rationale = (
        "This retracts the prior framing from synth #19 — wave 8 piece B."
    )
    violations = validate_rewriter_invariants(before, after)
    history_violations = [v for v in violations if v.check == GateCheck.HISTORY_LEAK]
    assert history_violations, (
        f"history narration not detected in action.rationale; got: "
        f"{[v.detail for v in violations[:5]]}"
    )


def test_validator_locator_points_to_offending_field():
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.medium.themes[0].rationale = "PlanCritique RED on FX staleness."
    violations = validate_rewriter_invariants(before, after)
    assert any(
        v.locator and "medium.themes" in v.locator for v in violations
    ), f"expected locator pointing to medium.themes; got: {[v.locator for v in violations]}"


# ---------------------------------------------------------------------------
# Rewriter agent surface
# ---------------------------------------------------------------------------

def test_rewriter_build_prompt_carries_structured_input():
    """The rewriter feeds the entire ``PlanSynthesisOutput`` JSON to
    the model in the user prompt. System prompt has the translation
    rubric."""
    rewriter = PlanLanguageRewriter(user_id="ariel")
    plan = _make_baseline_plan()
    sys_prompt, user_prompt = rewriter.build_prompt(
        synth_output=plan, decision_id=42,
    )
    # System prompt holds the rubric.
    assert "TaxAnalyst" in sys_prompt
    assert "substrate" in sys_prompt
    assert "REVISION-NARRATION BAN" in sys_prompt
    # User prompt wraps the plan JSON in <plan_input> tags.
    assert "<plan_input>" in user_prompt
    assert "</plan_input>" in user_prompt
    assert "NVDA share of portfolio" in user_prompt
    # Tag-escape applied to body.
    assert "</plan_input>" in user_prompt  # legitimate closer present
    # The escape transforms "</" to "‹/" INSIDE the body — the legit
    # closer at the end is not affected because it sits OUTSIDE the
    # escaped body region. Verify there's no accidental nesting issue.
    inner_block_end = user_prompt.index("</plan_input>")
    inner_block_start = user_prompt.index("<plan_input>")
    body = user_prompt[inner_block_start:inner_block_end]
    assert "</" not in body, (
        "rewriter must escape '</' inside <plan_input> so untrusted "
        "prose can't close the wrapper"
    )


def test_rewriter_class_metadata():
    rewriter = PlanLanguageRewriter(user_id="ariel")
    assert rewriter.agent_role == "plan_language_rewriter"
    assert rewriter.output_model is PlanSynthesisOutput
    assert rewriter.use_structured_output is True
    assert rewriter.require_citations is False


# ---------------------------------------------------------------------------
# Orchestrator wrapper — _run_plan_language_rewriter
# ---------------------------------------------------------------------------

class _StubResult:
    def __init__(self, output: PlanSynthesisOutput) -> None:
        self.output = output


def _stub_rewriter_returning(modified_plan: PlanSynthesisOutput):
    """Build a stub PlanLanguageRewriter class that returns
    `modified_plan` from run_sync."""
    class _Stub:
        def __init__(self, *, user_id: str) -> None:
            self.user_id = user_id

        def run_sync(self, **kwargs):
            return _StubResult(modified_plan)

    return _Stub


def test_orchestrator_uses_rewriter_output(monkeypatch):
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.long.posture = "Cleaned posture, no jargon."
    after.medium.posture = "Cleaned medium posture."

    monkeypatch.setattr(
        "argosy.agents.plan_language_rewriter.PlanLanguageRewriter",
        _stub_rewriter_returning(after),
    )
    out = _run_plan_language_rewriter(
        output=before, user_id="ariel", decision_run_id=42,
    )
    assert out.long.posture == "Cleaned posture, no jargon."
    assert out.medium.posture == "Cleaned medium posture."


def test_orchestrator_aborts_on_invariant_violation(monkeypatch):
    """When the rewriter returns a drifted output, validator raises
    RewriterInvariantError and the synth cycle aborts."""
    before = _make_baseline_plan()
    bad = before.model_copy(deep=True)
    # Drift: changed a Target value (which the rewriter MUST NOT touch).
    bad.medium.targets[0].value = 99

    monkeypatch.setattr(
        "argosy.agents.plan_language_rewriter.PlanLanguageRewriter",
        _stub_rewriter_returning(bad),
    )
    with pytest.raises(RewriterInvariantError) as exc:
        _run_plan_language_rewriter(
            output=before, user_id="ariel", decision_run_id=42,
        )
    assert exc.value.violations
    assert any(
        ".value" in v.detail for v in exc.value.violations
    )


def test_orchestrator_aborts_on_rewriter_crash(monkeypatch):
    """Rewriter system error (SDK timeout, etc.) → fail loud.

    Codex Phase 2 review found that falling back to the un-rewritten
    output silently leaks jargon to user-facing horizon MD (the
    rewriter exists precisely to scrub it). The contract is now
    fail-loud: a rewriter crash raises ``RewriterInvariantError``
    with a synthetic violation describing the underlying exception.
    """
    before = _make_baseline_plan()

    class _CrashingRewriter:
        def __init__(self, *, user_id: str) -> None:
            pass

        def run_sync(self, **kwargs):
            raise RuntimeError("SDK exploded")

    monkeypatch.setattr(
        "argosy.agents.plan_language_rewriter.PlanLanguageRewriter",
        _CrashingRewriter,
    )
    with pytest.raises(RewriterInvariantError) as exc:
        _run_plan_language_rewriter(
            output=before, user_id="ariel", decision_run_id=42,
        )
    assert exc.value.violations
    assert any("RuntimeError" in v.detail for v in exc.value.violations)
    assert any("SDK exploded" in v.detail for v in exc.value.violations)


def test_orchestrator_soft_fails_on_prose_only_violations(monkeypatch, caplog):
    """Codex supervised-fixes review: prose-only violations (residual
    jargon in label / rationale / posture) log a warning and ship the
    rewritten output. The /accept gate downstream catches anything
    that survives. Only structural drift (count / value / preserved
    field) still aborts."""
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    # Rewriter scrubbed most jargon but left "substrate" in a target
    # label — exactly the symptom that prompted the soft-fail change.
    after.medium.targets[0].label = "substrate-gated NVDA share of portfolio"

    monkeypatch.setattr(
        "argosy.agents.plan_language_rewriter.PlanLanguageRewriter",
        _stub_rewriter_returning(after),
    )
    import logging
    with caplog.at_level(logging.WARNING):
        out = _run_plan_language_rewriter(
            output=before, user_id="ariel", decision_run_id=42,
        )
    # Rewritten output returned (not the un-rewritten original).
    assert out.medium.targets[0].label == "substrate-gated NVDA share of portfolio"
    # Warning was emitted.
    assert any(
        "rewriter_prose_violations" in r.message
        or "rewriter_prose_violations" in str(r.args)
        for r in caplog.records
    ), f"expected rewriter_prose_violations log; got {[r.message for r in caplog.records]}"


def test_orchestrator_still_aborts_on_structural_violations(monkeypatch):
    """The soft-fail behavior is prose-only. Any structural drift
    (count change, preserved-field mutation, evidence subtree mutation,
    inputs mutation) still raises RewriterInvariantError."""
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    # Structural drift: a Target.value was changed (which the rewriter
    # MUST NOT touch).
    after.medium.targets[0].value = 99

    monkeypatch.setattr(
        "argosy.agents.plan_language_rewriter.PlanLanguageRewriter",
        _stub_rewriter_returning(after),
    )
    with pytest.raises(RewriterInvariantError) as exc:
        _run_plan_language_rewriter(
            output=before, user_id="ariel", decision_run_id=42,
        )
    # Only structural violations should be in the aborted-cycle's
    # violations list — prose violations were partitioned out.
    assert all(
        "preserved field" in v.detail
        or "rewriter changed" in v.detail
        or "rewriter modified" in v.detail
        or "subtree modified" in v.detail
        or "(provenance)" in v.detail
        for v in exc.value.violations
    ), (
        f"expected only structural violations in raised exception; "
        f"got: {[v.detail for v in exc.value.violations]}"
    )


def test_validator_catches_inputs_field_mutation():
    """PlanSynthesisOutput.inputs (provenance: baseline_id,
    prior_current_id, etc.) is structured metadata. The rewriter
    MUST NOT touch it (codex Phase 2 review caught this gap)."""
    before = _make_baseline_plan()
    after = before.model_copy(deep=True)
    after.inputs = after.inputs.model_copy(update={"baseline_id": 999})
    violations = validate_rewriter_invariants(before, after)
    assert any(
        v.locator == "inputs" and "inputs" in v.detail
        for v in violations
    ), f"expected inputs-mutation violation; got: {[v.detail for v in violations]}"
