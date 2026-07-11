"""Unit tests for whole-surface FI-shock section body_md qualification."""

from __future__ import annotations

import json

from argosy.agents.plan_synthesizer_types import (
    Assumption,
    Citation,
    FactClaim,
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthesisInputs,
)
from argosy.quality.coherence_gate import check_fi_sufficiency_under_shock
from argosy.quality.fi_fx_shock_gate import check_fi_sufficiency_under_fx_shock
from argosy.quality.fi_shock_qualifier import (
    qualify_reached_text,
    qualify_sections_json,
    section_bodies_plan_text,
)


def _section(section_id: str, horizon: str, body_md: str) -> Section:
    return Section(
        section_id=section_id, horizon=horizon, title=section_id.title(),
        body_md=body_md,
        evidence=SectionEvidence(
            facts=[FactClaim(
                text="a full-length qualitative claim here",
                kind="qualitative",
            )],
            source_span=[Citation(
                source_kind="inference", source_locator="test",
                supports_fact_index=0,
            )],
            assumptions=[Assumption(
                text="test default", default_value="x", rationale="test",
            )],
        ),
    )


def _breaking_nvda_shock():
    return {"shock_0.30": {"perpetuity_reached": False, "net_worth_nis": 9_000_000}}


def _breaking_fx_shock():
    return {"fx_shock_-0.10": {"total_reached": False, "net_worth_nis": 9_500_000}}


def test_qualify_reached_text_inserts_nvda_caveat():
    before = "Capital sufficiency reached on the liquid basis."
    after = qualify_reached_text(before, need_nvda=True, need_fx=False)
    assert "only at the full NVDA mark" in after
    assert check_fi_sufficiency_under_shock(
        shock_result=_breaking_nvda_shock(), plan_text=after,
    ) == []


def test_qualify_skips_already_qualified_and_negated():
    qualified = (
        "Capital sufficiency reached only at the full NVDA mark."
    )
    assert qualify_reached_text(
        qualified, need_nvda=True, need_fx=False,
    ) == qualified
    denied = "FI is not yet reached on the liquid basis."
    assert qualify_reached_text(
        denied, need_nvda=True, need_fx=False,
    ) == denied


def test_sections_json_qualified_when_horizon_already_is():
    """Draft-85 shape: horizon MD already carries the NVDA caveat; section
    body_md still has a bare 'reached' — qualify the section, leave horizon.
    """
    horizon = (
        "Capital sufficiency reached only at the full NVDA mark; "
        "a −30% NVDA shock would erase the surplus."
    )
    assert check_fi_sufficiency_under_shock(
        shock_result=_breaking_nvda_shock(), plan_text=horizon,
    ) == []

    sections = [
        _section(
            "capital_sufficiency", "long",
            "Capital sufficiency reached on the liquid basis this quarter.",
        ),
        _section(
            "ips", "long",
            "The IPS keeps the FX planning rate at the canonical figure.",
        ),
    ]
    sj = json.dumps([s.model_dump(mode="json") for s in sections])
    new_sj, n = qualify_sections_json(
        sj, shock_result=_breaking_nvda_shock(), fx_shock_result=None,
    )
    assert n == 1
    parsed = json.loads(new_sj)
    by_id = {s["section_id"]: s["body_md"] for s in parsed}
    assert "only at the full NVDA mark" in by_id["capital_sufficiency"]
    assert by_id["ips"] == sections[1].body_md  # untouched

    # Gate parity: horizon OK alone; joined with unqualified section would fail;
    # after qualify, joined text passes.
    synth = PlanSynthesisOutput(
        long=HorizonSection(
            horizon="long", freshness_expected="annual", status="no_change",
            posture=horizon, rationale="r",
        ),
        medium=HorizonSection(
            horizon="medium", freshness_expected="quarterly",
            status="no_change", posture="m", rationale="r",
        ),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly",
            status="no_change", posture="s", rationale="r",
        ),
        inputs=SynthesisInputs(),
        sections=[
            Section.model_validate(d) for d in parsed
        ],
    )
    joined = horizon + "\n\n" + section_bodies_plan_text(synth)
    assert check_fi_sufficiency_under_shock(
        shock_result=_breaking_nvda_shock(), plan_text=joined,
    ) == []


def test_sections_json_fx_qualifier_passes_fx_gate():
    sj = json.dumps([
        _section(
            "fi_bridge", "long",
            "You are financially independent on today's mark.",
        ).model_dump(mode="json"),
    ])
    new_sj, n = qualify_sections_json(
        sj, shock_result=None, fx_shock_result=_breaking_fx_shock(),
    )
    assert n == 1
    body = json.loads(new_sj)[0]["body_md"]
    assert "FX" in body or "currency" in body
    assert check_fi_sufficiency_under_fx_shock(
        fx_shock_result=_breaking_fx_shock(), plan_text=body,
    ) == []


def test_qualify_noop_when_shocks_clear():
    sj = json.dumps([
        _section(
            "capital_sufficiency", "long",
            "Capital sufficiency reached.",
        ).model_dump(mode="json"),
    ])
    out, n = qualify_sections_json(
        sj,
        shock_result={"shock_0.30": {"perpetuity_reached": True}},
        fx_shock_result={"fx_shock_-0.10": {"total_reached": True}},
    )
    assert n == 0 and out == sj
