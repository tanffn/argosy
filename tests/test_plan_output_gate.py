"""Phase 0 — failing CI gate tests.

These tests are the load-bearing artifact for the integration plan:
they MUST fail RED on the persisted plan v20 fixture, and must
remain GREEN on legitimate financial-advice prose corpus.

As Phases 1-4 ship, the v20-fail tests stay red (v20 fixture is
frozen), but freshly-synthesized plans flip the same checks to
GREEN. New `test_v21_passes_*` tests will be added per phase.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from argosy.quality import (
    GateCheck,
    check_distillate_section_binding,
    check_evidence_per_section,
    check_history_leak,
    check_jargon_leak,
    check_section_coverage,
    gate_plan_output,
)
from argosy.quality.canonical_sections import (
    CANONICAL_SECTION_IDS,
    MVP_COVERAGE_THRESHOLD,
)
from argosy.quality.gate_types import GateVerdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "plan_v20_horizons"


@pytest.fixture
def v20_horizon_text() -> dict[str, str]:
    """The three horizon markdowns from persisted plan_version=20."""
    return {
        "long":   (FIXTURE_DIR / "long.md").read_text(encoding="utf-8"),
        "medium": (FIXTURE_DIR / "medium.md").read_text(encoding="utf-8"),
        "short":  (FIXTURE_DIR / "short.md").read_text(encoding="utf-8"),
    }


@pytest.fixture
def v20_synth_output() -> Any:
    """Reconstruct a PlanSynthesisOutput-like object from the persisted
    horizon_*_json fixtures. Returns a SimpleNamespace tree (not full
    Pydantic round-trip) because v20's JSON shape predates the
    section_id / evidence fields and a strict Pydantic load would fail
    on missing fields — which is precisely the failure mode under test.
    """
    def _ns(d: Any) -> Any:
        if isinstance(d, dict):
            return SimpleNamespace(**{k: _ns(v) for k, v in d.items()})
        if isinstance(d, list):
            return [_ns(v) for v in d]
        return d

    horizons = {}
    for name in ("long", "medium", "short"):
        raw = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
        horizons[name] = _ns(json.loads(raw)) if raw.strip() else SimpleNamespace()
    return SimpleNamespace(
        long=horizons["long"],
        medium=horizons["medium"],
        short=horizons["short"],
        inputs=SimpleNamespace(),
    )


# Corpus of legitimate financial-advice sentences. The history_leak and
# jargon_leak checks MUST produce zero matches on every line here. If a
# pattern false-positives on this corpus, the pattern needs tightening.
FINANCIAL_ADVICE_CORPUS: list[str] = [
    "The portfolio gained 12% during the prior year.",
    "Tax loss from a former employer's stock can offset gains.",
    "Net worth increased after the revised tax calculation.",
    "These funds originally from the 401(k) rollover are now in the IRA.",
    "Holdings include shares received earlier in 2024 via grant.",
    "Earlier this year, the fed paused rate cuts.",
    "Former tax brackets included a 10% bracket.",
    "Prior to retirement, max out the keren hishtalmut.",
    "Quarterly review of asset allocation is reasonable.",
    "The fund's expense ratio decreased from 0.30% to 0.07%.",
    "Returns are revised quarterly by the fund administrator.",
    "Income was originally invested in low-cost ETFs.",
    "A red flag for any portfolio is single-stock concentration.",
    "Real estate has appreciated relative to liquid assets.",
    "The yield on treasury bonds remains attractive.",
    "Consider securities lending to add incremental income.",
    "Charitable giving via donor-advised funds is tax-efficient.",
    "The IPS specifies an equity range of 50% to 80%.",
    "Israeli pension fund mandatory contribution caps apply.",
    "Section 102 capital track requires 24-month trustee holding.",
]


# ---------------------------------------------------------------------------
# v20 fixture must fail RED — Phase 0's load-bearing assertions
# ---------------------------------------------------------------------------

class TestV20FailsAllChecks:
    """The four failure assertions that lock v20 as a regression fixture."""

    def test_v20_fails_history_leak(self, v20_horizon_text: dict[str, str]):
        total_violations = 0
        for name, text in v20_horizon_text.items():
            violations = check_history_leak(text)
            assert violations, (
                f"v20 {name} horizon expected to fail history_leak but "
                "produced zero violations — gate is not detecting the "
                "v20 leak patterns."
            )
            total_violations += len(violations)
        # v20 should produce many violations across three horizons —
        # this is the "many smoking guns" assertion.
        assert total_violations >= 10, (
            f"v20 produced only {total_violations} history_leak violations; "
            "expected ≥10 across three horizons (status: minor_revision "
            "headers, (stated ...; revisit ...) parentheticals, "
            "## Deltas vs. prior current blocks, lineage references)."
        )

    def test_v20_fails_jargon_leak(self, v20_horizon_text: dict[str, str]):
        total_violations = 0
        for name, text in v20_horizon_text.items():
            violations = check_jargon_leak(text)
            assert violations, (
                f"v20 {name} horizon expected to fail jargon_leak but "
                "produced zero violations."
            )
            total_violations += len(violations)
        assert total_violations >= 5, (
            f"v20 produced only {total_violations} jargon_leak violations; "
            "expected ≥5 (TaxAnalyst, ConcentrationAnalyst, PlanCritique, "
            "substrate, etc.)."
        )

    def test_v20_fails_section_coverage(self, v20_synth_output: Any):
        violations = check_section_coverage(
            v20_synth_output, threshold=MVP_COVERAGE_THRESHOLD
        )
        assert violations, (
            "v20 PlanSynthesisOutput predates section_id field — "
            "coverage check must fail."
        )
        # The violation should explicitly note coverage < threshold
        assert any(
            "below threshold" in v.detail for v in violations
        ), f"expected coverage-below-threshold violation; got: {violations}"

    def test_v20_fails_evidence_per_section(self, v20_synth_output: Any):
        violations = check_evidence_per_section(v20_synth_output)
        assert violations, (
            "v20 has no Section[] entries — evidence check must fail."
        )

    def test_v20_fails_aggregate_gate(
        self,
        v20_horizon_text: dict[str, str],
        v20_synth_output: Any,
    ):
        verdict = gate_plan_output(
            horizon_text=v20_horizon_text,
            synth=v20_synth_output,
            distillate=None,  # check 5 skipped at Phase 0
            coverage_threshold=MVP_COVERAGE_THRESHOLD,
        )
        assert not verdict.passes
        # All four runnable checks should have at least one violation
        for check in (
            GateCheck.HISTORY_LEAK,
            GateCheck.JARGON_LEAK,
            GateCheck.SECTION_COVERAGE,
            GateCheck.EVIDENCE_PER_SECTION,
        ):
            assert verdict.for_check(check), (
                f"v20 expected to fail {check.value} but produced no "
                f"violations. Summary: {verdict.summary()}"
            )


# ---------------------------------------------------------------------------
# False-positive guards: legitimate prose MUST pass
# ---------------------------------------------------------------------------

class TestNoFalsePositivesOnLegitimateProse:

    @pytest.mark.parametrize("sentence", FINANCIAL_ADVICE_CORPUS)
    def test_history_leak_no_false_positives(self, sentence: str):
        violations = check_history_leak(sentence)
        assert not violations, (
            f"history_leak false-positive on legitimate sentence: "
            f"`{sentence}` → {[v.detail for v in violations]}"
        )

    @pytest.mark.parametrize("sentence", FINANCIAL_ADVICE_CORPUS)
    def test_jargon_leak_no_false_positives(self, sentence: str):
        violations = check_jargon_leak(sentence)
        assert not violations, (
            f"jargon_leak false-positive on legitimate sentence: "
            f"`{sentence}` → {[v.detail for v in violations]}"
        )


# ---------------------------------------------------------------------------
# Evidence content-gate unit tests (Phase 3 spec, ship in Phase 0 so the
# contract is locked before code exists to satisfy it)
# ---------------------------------------------------------------------------

def _section_ns(
    section_id: str = "tax_plan",
    facts: list[Any] | None = None,
    source_span: list[Any] | None = None,
    assumptions: list[Any] | None = None,
    missing_data: list[str] | None = None,
) -> Any:
    """Build a stub Section namespace for content-gate tests."""
    evidence = SimpleNamespace(
        facts=facts or [],
        source_span=source_span or [],
        assumptions=assumptions or [],
        missing_data=missing_data or [],
    )
    return SimpleNamespace(section_id=section_id, evidence=evidence)


def _synth_with_sections(sections: list[Any]) -> Any:
    """Build a PlanSynthesisOutput-like ns with sections under long
    horizon only (simplest fixture for content-gate tests)."""
    return SimpleNamespace(
        long=SimpleNamespace(sections=sections),
        medium=SimpleNamespace(sections=[]),
        short=SimpleNamespace(sections=[]),
        inputs=SimpleNamespace(),
    )


class TestEvidenceContentGate:

    def test_section_with_no_facts_and_no_missing_data_fails(self):
        section = _section_ns(facts=[], source_span=[], missing_data=[])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any("silent empty" in v.detail for v in violations)

    def test_fact_without_citation_fails(self):
        fact = SimpleNamespace(
            text="NVDA is 18% of liquid portfolio",
            kind="numeric",
            value=18,
            unit="pct",
        )
        section = _section_ns(facts=[fact], source_span=[])  # no cite
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any("no citation" in v.detail for v in violations)

    def test_evidence_extract_support_numeric_fails_when_value_missing(self):
        fact = SimpleNamespace(
            text="NVDA is 18% of liquid portfolio",
            kind="numeric",
            value=18,
            unit="pct",
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Concentration",
            extract="NVDA concentration remains elevated.",  # missing "18"
            supports_fact_index=0,
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any("not present in citation extract" in v.detail for v in violations)

    def test_evidence_extract_support_numeric_passes_when_value_in_extract(self):
        fact = SimpleNamespace(
            text="NVDA is 18 percent of liquid portfolio",
            kind="numeric",
            value=18,
            unit="pct",
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Concentration",
            extract="NVDA concentration at 18 percent of liquid book.",
            supports_fact_index=0,
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        # No "value not present" violation expected.
        assert not any("not present in citation extract" in v.detail for v in violations)

    def test_evidence_extract_support_categorical_fails_low_overlap(self):
        fact = SimpleNamespace(
            text="The household prefers UCITS-domiciled ETF holdings",
            kind="categorical",
            value=None,
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Preferences",
            extract="Sky is blue today and tomorrow weather looks fine.",
            supports_fact_index=0,
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any("content tokens with fact" in v.detail for v in violations)

    def test_evidence_extract_support_categorical_passes_high_overlap(self):
        fact = SimpleNamespace(
            text="The household prefers UCITS-domiciled ETF holdings",
            kind="categorical",
            value=None,
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Preferences",
            extract="Prefers UCITS-domiciled ETF holdings over US-domiciled.",
            supports_fact_index=0,
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert not any("content tokens with fact" in v.detail for v in violations)

    def test_inference_requires_assumption(self):
        fact = SimpleNamespace(
            text="Healthcare costs likely grow at 5 percent real",
            kind="qualitative",
            value=None,
        )
        cite = SimpleNamespace(
            source_kind="inference",
            source_locator="inference:agent_baseline",
            extract=None,
            supports_fact_index=0,
        )
        section = _section_ns(
            facts=[fact],
            source_span=[cite],
            assumptions=[],  # missing!
        )
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any(
            "uses inference/agent_baseline citations but declares no assumptions"
            in v.detail
            for v in violations
        )


# ---------------------------------------------------------------------------
# Distillate-section binding unit tests
# ---------------------------------------------------------------------------

class TestDistillateSectionBinding:

    def test_missing_section_fails(self):
        distillate = SimpleNamespace(
            real_estate_plan=[{"id": "atlanta_sfr"}],  # non-empty
            goals=[], plan_assumptions=[], cashflow_phases=[],
            capital_sufficiency=None, ips=None, withdrawal_schedule=[],
            monte_carlo_grid=None, tax_schedule=[], insurance_matrix=[],
            healthcare_cost_plan=None, estate_documents=None,
            cross_border=None, equity_comp_grants=[], fi_bridge=[],
            life_events=[], priority_matrix=[], fx_strategy=None,
            etf_reference=[], securities_lending=None,
            charitable_giving=[],
        )
        # synth with no net_worth section
        synth = _synth_with_sections([
            _section_ns(section_id="cover_assumptions"),
        ])
        violations = check_distillate_section_binding(synth, distillate)
        assert any(
            "real_estate_plan" in v.detail and "net_worth" in v.detail
            for v in violations
        )

    def test_present_section_but_unused_field_fails(self):
        distillate = SimpleNamespace(
            charitable_giving=[{"item": "DAF NVDA donation"}],
            goals=[], plan_assumptions=[], cashflow_phases=[],
            capital_sufficiency=None, ips=None, withdrawal_schedule=[],
            monte_carlo_grid=None, tax_schedule=[], insurance_matrix=[],
            healthcare_cost_plan=None, estate_documents=None,
            cross_border=None, equity_comp_grants=[], fi_bridge=[],
            life_events=[], priority_matrix=[], fx_strategy=None,
            etf_reference=[], securities_lending=None,
            real_estate_plan=[],
        )
        # Section is present but cites something else, NOT
        # distillate.charitable_giving
        unrelated_cite = SimpleNamespace(
            source_kind="analyst_report",
            source_locator="analyst_report:TaxAnalyst:line_4",
            extract="Tax loss harvesting available for IBIT lots.",
            supports_fact_index=0,
        )
        fact = SimpleNamespace(
            text="TLH available for IBIT",
            kind="qualitative",
            value=None,
        )
        section = _section_ns(
            section_id="tax_plan",
            facts=[fact],
            source_span=[unrelated_cite],
        )
        synth = _synth_with_sections([section])
        violations = check_distillate_section_binding(synth, distillate)
        assert any(
            "appears unused" in v.detail and "charitable_giving" in v.detail
            for v in violations
        )

    def test_present_section_with_proper_citation_passes(self):
        distillate = SimpleNamespace(
            charitable_giving=[{"item": "DAF NVDA donation"}],
            goals=[], plan_assumptions=[], cashflow_phases=[],
            capital_sufficiency=None, ips=None, withdrawal_schedule=[],
            monte_carlo_grid=None, tax_schedule=[], insurance_matrix=[],
            healthcare_cost_plan=None, estate_documents=None,
            cross_border=None, equity_comp_grants=[], fi_bridge=[],
            life_events=[], priority_matrix=[], fx_strategy=None,
            etf_reference=[], securities_lending=None,
            real_estate_plan=[],
        )
        proper_cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="distillate.charitable_giving[0]",
            extract="DAF NVDA donation strategy proposed in baseline.",
            supports_fact_index=0,
        )
        fact = SimpleNamespace(
            text="DAF NVDA donation reduces realized gain",
            kind="qualitative",
            value=None,
        )
        section = _section_ns(
            section_id="tax_plan",
            facts=[fact],
            source_span=[proper_cite],
        )
        synth = _synth_with_sections([section])
        violations = check_distillate_section_binding(synth, distillate)
        assert not violations

    def test_distillate_none_skips_check(self):
        synth = _synth_with_sections([])
        violations = check_distillate_section_binding(synth, None)
        assert violations == []


# ---------------------------------------------------------------------------
# Canonical sections sanity
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Strengthened pattern-detection tests (one per spec-mandated phrase)
# ---------------------------------------------------------------------------

class TestPatternDetectionPerPhrase:
    """Codex round on Phase 0 flagged that v20 RED assertions only
    require low totals — deleting many required patterns still passes.
    These tests assert each spec phrase is detected individually."""

    @pytest.mark.parametrize("phrase", [
        "the policy was reversed last quarter",
        "see synth #19 for the prior context",
        "this draft #18 is the active baseline",
        "wave 8 ships next month",                # wave \d+
        "the v2.4 build was tagged today",         # v\d+\.\d+ (isolated)
        "the v2.4.3 build is in CI",               # v\d+\.\d+\.\d+
        "Piece B follows Piece A in the roadmap",
        "lineage to prior draft is preserved",
        "preserved from prior round",
        "accepted prior-round delta is rendered",
        "this retracts the previous framing",
        "(stated 2026-06-02; revisit 2026-07-01)",
        "## Deltas vs. prior current",
        "# Long horizon — status: minor_revision",
        "supersedes the prior plan",
        "deprecated as of last cycle",
        "changed from 25% to 18% strategic",
        "no longer applies under capital preservation",
        "instead of the previous approach",
        "originally proposed in late 2024",
    ])
    def test_history_leak_detects_phrase(self, phrase: str):
        violations = check_history_leak(phrase)
        assert violations, f"history_leak did not detect: `{phrase}`"

    @pytest.mark.parametrize("phrase", [
        "TaxAnalyst flagged this",
        "ConcentrationAnalyst notes",
        "FXAnalyst reports",
        "PlanCritique says",
        "PlanSynthesizer emitted",
        "the substrate is repaired",
        "substrate-gated decision",
        "self-flagged LOW",
        "the analyst fleet",
        "our orchestrator runs",
        "synthesizer output",
        "gate check passed",
        "the distillate carries",
        "PlanCritique RED on FX",
        "RED on the data",
        "GREEN status confirmed",
        "=== TaxAnalyst (FAILED) ===",
    ])
    def test_jargon_leak_detects_phrase(self, phrase: str):
        violations = check_jargon_leak(phrase)
        assert violations, f"jargon_leak did not detect: `{phrase}`"


# ---------------------------------------------------------------------------
# Top-level synth.sections shape (Phase 3 canonical)
# ---------------------------------------------------------------------------

def _synth_with_top_level_sections(sections: list[Any]) -> Any:
    """Phase 3 canonical shape: PlanSynthesisOutput.sections is a flat
    list across horizons, each Section carrying its own `horizon`."""
    return SimpleNamespace(sections=sections, inputs=SimpleNamespace())


class TestTopLevelSectionsShape:
    """The Phase-3 canonical shape is `synth.sections` (flat list).
    The gate must read that, not the per-horizon attribute."""

    def test_top_level_sections_coverage_counts(self):
        sections = [
            SimpleNamespace(section_id="cover_assumptions", horizon="long"),
            SimpleNamespace(section_id="client_goals", horizon="long"),
            SimpleNamespace(section_id="net_worth", horizon="long"),
        ]
        synth = _synth_with_top_level_sections(sections)
        violations = check_section_coverage(synth, threshold=2)
        assert not violations  # 3 ≥ 2

    def test_top_level_sections_coverage_below_threshold(self):
        synth = _synth_with_top_level_sections([
            SimpleNamespace(section_id="cover_assumptions", horizon="long"),
        ])
        violations = check_section_coverage(synth, threshold=12)
        assert violations
        assert any("below threshold" in v.detail for v in violations)

    def test_top_level_sections_unknown_id_flagged(self):
        sections = [
            SimpleNamespace(section_id="not_canonical_id", horizon="long"),
        ]
        synth = _synth_with_top_level_sections(sections)
        violations = check_section_coverage(synth, threshold=0)
        assert any("unknown" in v.detail for v in violations)


# ---------------------------------------------------------------------------
# FactClaim.text length validator + invalid supports_fact_index
# ---------------------------------------------------------------------------

class TestFactClaimLengthAndIndex:

    def test_short_fact_text_fails_12char_rule(self):
        # Text shorter than 12 chars — even if otherwise well-formed
        fact = SimpleNamespace(
            text="NVDA 18%",  # 8 chars
            kind="categorical",
            value=None,
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Concentration",
            extract="NVDA concentration at 18 percent of liquid book.",
            supports_fact_index=0,
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any(
            "shorter than 12 chars" in v.detail
            or "single-token fluency forbidden" in v.detail
            for v in violations
        ), f"expected 12-char rule violation; got: {[v.detail for v in violations]}"

    def test_long_enough_fact_text_passes_length_rule(self):
        fact = SimpleNamespace(
            text="NVDA is at 18 percent of liquid book",  # >12 chars
            kind="numeric",
            value=18,
            unit="pct",
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Concentration",
            extract="NVDA concentration at 18 percent of liquid book.",
            supports_fact_index=0,
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert not any("shorter than 12 chars" in v.detail for v in violations)

    def test_invalid_supports_fact_index_fails_loudly(self):
        # Citation points to fact index that doesn't exist
        fact = SimpleNamespace(
            text="NVDA at 18 percent of liquid book",
            kind="numeric",
            value=18,
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Concentration",
            extract="NVDA concentration at 18 percent.",
            supports_fact_index=99,  # out of bounds
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any(
            "invalid" in v.detail and "supports_fact_index" in v.detail
            for v in violations
        )

    def test_non_int_supports_fact_index_fails_loudly(self):
        fact = SimpleNamespace(
            text="NVDA at 18 percent of liquid book",
            kind="numeric",
            value=18,
        )
        cite = SimpleNamespace(
            source_kind="plan_doc",
            source_locator="plan_doc:H2:Concentration",
            extract="NVDA concentration at 18 percent.",
            supports_fact_index=None,  # not an int
        )
        section = _section_ns(facts=[fact], source_span=[cite])
        synth = _synth_with_sections([section])
        violations = check_evidence_per_section(synth)
        assert any(
            "invalid" in v.detail and "supports_fact_index" in v.detail
            for v in violations
        )


# ---------------------------------------------------------------------------
# Multi-horizon section_id grouping for the binding gate
# ---------------------------------------------------------------------------

class TestDistillateBindingMultiHorizon:
    """A section_id can appear in multiple horizons. A citation in
    ANY one of them satisfies the binding-USE rule."""

    def test_citation_in_one_of_two_horizons_passes(self):
        distillate = SimpleNamespace(
            charitable_giving=[{"item": "DAF NVDA donation"}],
            goals=[], plan_assumptions=[], cashflow_phases=[],
            capital_sufficiency=None, ips=None, withdrawal_schedule=[],
            monte_carlo_grid=None, tax_schedule=[], insurance_matrix=[],
            healthcare_cost_plan=None, estate_documents=None,
            cross_border=None, equity_comp_grants=[], fi_bridge=[],
            life_events=[], priority_matrix=[], fx_strategy=None,
            etf_reference=[], securities_lending=None,
            real_estate_plan=[],
        )
        # Two "tax_plan" sections (different horizons). Only the second
        # carries the distillate.charitable_giving citation.
        unrelated = SimpleNamespace(
            section_id="tax_plan",
            horizon="medium",
            evidence=SimpleNamespace(
                facts=[SimpleNamespace(
                    text="Tax loss harvesting opportunities",
                    kind="qualitative",
                    value=None,
                )],
                source_span=[SimpleNamespace(
                    source_kind="analyst_report",
                    source_locator="analyst_report:tax:line_3",
                    extract="TLH on IBIT available now.",
                    supports_fact_index=0,
                )],
                assumptions=[],
                missing_data=[],
            ),
        )
        proper = SimpleNamespace(
            section_id="tax_plan",
            horizon="long",
            evidence=SimpleNamespace(
                facts=[SimpleNamespace(
                    text="Charitable giving via DAF reduces gain",
                    kind="qualitative",
                    value=None,
                )],
                source_span=[SimpleNamespace(
                    source_kind="plan_doc",
                    source_locator="distillate.charitable_giving[0]",
                    extract="DAF NVDA donation strategy",
                    supports_fact_index=0,
                )],
                assumptions=[],
                missing_data=[],
            ),
        )
        synth = _synth_with_top_level_sections([unrelated, proper])
        violations = check_distillate_section_binding(synth, distillate)
        # No violation for charitable_giving — the long-horizon
        # instance carries the distillate.charitable_giving citation.
        assert not any(
            "charitable_giving" in v.detail for v in violations
        )


def test_canonical_section_ids_count_is_18():
    assert len(CANONICAL_SECTION_IDS) == 18, (
        f"Spec mandates 18 canonical sections, found {len(CANONICAL_SECTION_IDS)}"
    )


def test_mvp_threshold_is_12():
    assert MVP_COVERAGE_THRESHOLD == 12


# ---------------------------------------------------------------------------
# IPS allocation sum (S21) — the medium-horizon sleeves must sum to ~100%.
# Reproduces the FM-rejected draft 38 (under, 51%) and FM-MISSED draft 39
# (over, 108% from a redundant "defensive floor" descriptor target).
# ---------------------------------------------------------------------------
from argosy.quality import check_ips_allocation_sum  # noqa: E402


def _synth_with_medium_targets(targets: list[Any]) -> Any:
    # SimpleNamespace tree — the check only reads synth.medium.targets[].unit/
    # value/label (matches this file's other synth fixtures).
    return SimpleNamespace(medium=SimpleNamespace(targets=targets))


def _pct(label: str, value: float) -> Any:
    return SimpleNamespace(label=label, value=value, unit="pct_of_portfolio")


# The 11 real allocatable sleeves that sum to exactly 100.0 (draft 39's set).
_CLEAN_SLEEVES = [
    _pct("US broad-market core (CSPX)", 28.5),
    _pct("Dividend-quality (FUSA)", 11.2),
    _pct("International developed ex-US (EXUS)", 11.2),
    _pct("EM (EIMI)", 4.1),
    _pct("Growth ex-NVDA (R1GR)", 13.2),
    _pct("US low-volatility (SPMV)", 6.1),
    _pct("Real assets (DPYA)", 2.0),
    _pct("Gold (SGLN)", 3.0),
    _pct("Cash & T-bills", 6.1),
    _pct("Short-duration IG bonds", 2.6),
    _pct("NVDA IPS sleeve", 12.0),
]


def test_ips_sum_passes_at_100():
    synth = _synth_with_medium_targets(list(_CLEAN_SLEEVES))
    assert check_ips_allocation_sum(synth) == []


def test_ips_sum_flags_over_allocation_from_redundant_descriptor():
    """Draft 39's bug: a 'defensive floor 8.0' descriptor on top of the cash
    + bonds sleeves makes the list sum to 108%."""
    synth = _synth_with_medium_targets(
        [_pct("Defensive sleeve accumulation-phase floor", 8.0)] + list(_CLEAN_SLEEVES)
    )
    viol = check_ips_allocation_sum(synth)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.IPS_ALLOCATION_SUM
    assert "108" in viol[0].detail and "OVER" in viol[0].detail


def test_ips_sum_flags_under_allocation_implicit_core():
    """Draft 38's bug: only the 6 changed sleeves declared (~51%)."""
    synth = _synth_with_medium_targets([
        _pct("NVDA", 12.0), _pct("Defensive", 8.0), _pct("Growth", 13.0),
        _pct("Dividend", 11.0), _pct("EM", 4.0), _pct("Gold", 3.0),
    ])
    viol = check_ips_allocation_sum(synth)
    assert len(viol) == 1
    assert "UNDER" in viol[0].detail


def test_ips_sum_skips_when_no_pct_targets():
    synth = _synth_with_medium_targets([
        SimpleNamespace(label="FI age", value=46.0, unit="years"),
    ])
    assert check_ips_allocation_sum(synth) == []


# ---------------------------------------------------------------------------
# Task 8 — aggregate coherence + shock + freshness into gate_plan_output.
# These wire the deterministic Phase-1 checks into the central aggregator,
# following its skip-when-absent pattern.
# ---------------------------------------------------------------------------
from datetime import date  # noqa: E402


def _artifact(surface_values: dict, extraction_errors: dict | None = None) -> Any:
    """Minimal AssembledArtifact-shaped stub for the aggregator."""
    return SimpleNamespace(
        full_text="",
        surface_values=surface_values,
        extraction_errors=extraction_errors or {},
    )


class TestAggregateCrossSurfaceCoherence:

    def test_gate_flags_divergent_surface_artifact(self):
        art = _artifact(
            surface_values={
                "nvda_weight_pct": [("body", 62.5), ("dashboard", 56.9)],
            },
            extraction_errors={},
        )
        verdict = gate_plan_output({}, artifact=art)
        viols = verdict.for_check(GateCheck.CROSS_SURFACE_COHERENCE)
        assert viols, "divergent surfaces must yield a cross-surface violation"
        assert any("nvda_weight_pct" in v.detail for v in viols)

    def test_gate_flags_extraction_error_failloud(self):
        # A collapsed surface (extraction error) must NOT pass vacuously.
        art = _artifact(
            surface_values={},
            extraction_errors={"dashboard": "boom"},
        )
        verdict = gate_plan_output({}, artifact=art)
        viols = verdict.for_check(GateCheck.CROSS_SURFACE_COHERENCE)
        assert viols, "an extraction error must surface as a violation, not a silent pass"
        assert any("dashboard" in v.detail and "boom" in v.detail for v in viols)

    def test_gate_skips_coherence_when_no_artifact(self):
        verdict = gate_plan_output({})
        assert verdict.for_check(GateCheck.CROSS_SURFACE_COHERENCE) == []

    def test_gate_coherence_clean_when_surfaces_agree(self):
        art = _artifact(
            surface_values={
                "net_worth_nis": [("body", 11_950_000.0), ("dashboard", 11_950_000.0)],
            },
            extraction_errors={},
        )
        verdict = gate_plan_output({}, artifact=art)
        assert verdict.for_check(GateCheck.CROSS_SURFACE_COHERENCE) == []


class TestAggregateInputFreshness:

    def test_gate_flags_stale_snapshot(self):
        verdict = gate_plan_output(
            {}, today=date(2026, 6, 15), snapshot_date=date(2026, 6, 12),
        )
        assert verdict.for_check(GateCheck.INPUT_FRESHNESS), (
            "a 3-day-old snapshot (>2-day window) must flag input_freshness"
        )

    def test_gate_skips_freshness_when_no_today(self):
        verdict = gate_plan_output({}, snapshot_date=date(2026, 6, 12))
        assert verdict.for_check(GateCheck.INPUT_FRESHNESS) == []

    def test_gate_fresh_snapshot_clean(self):
        verdict = gate_plan_output(
            {}, today=date(2026, 6, 15), snapshot_date=date(2026, 6, 14),
        )
        assert verdict.for_check(GateCheck.INPUT_FRESHNESS) == []
