"""Corrective patch-synthesis — classifier + merge unit tests.

Design: docs/design/corrective_patch_synthesis.md §5 (classifier unit tests:
each FULL precedence rule in isolation, occurrence widening, spread bound,
directive mapping; merge tests: adversarial stub cannot perturb unimplicated
items, provenance hashes, pydantic round-trip).

Pure — no DB, no LLM, no orchestrator.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from argosy.agents.plan_synthesizer_types import (
    Action,
    Assumption,
    Citation,
    Delta,
    FactClaim,
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthesisInputs,
    SynthTarget,
)
from argosy.quality.patch_reachability import (
    classify_patch_reachability,
    parse_plan_item_ref,
    synthetic_item_id,
)


def _target(label: str, value: float, rationale: str = "") -> SynthTarget:
    return SynthTarget(
        label=label, value=value, unit="pct_of_portfolio",
        stated_at=date(2026, 7, 1), revisit_after=date(2026, 10, 1),
        rationale=rationale,
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


def _prior() -> PlanSynthesisOutput:
    return PlanSynthesisOutput(
        long=HorizonSection(
            horizon="long", freshness_expected="annual", status="no_change",
            posture="Stay the course on the diversified core.",
            rationale="Long-run FX planning assumes 3.00 shekels per dollar.",
            targets=[_target("Core diversification floor", 55.0)],
        ),
        medium=HorizonSection(
            horizon="medium", freshness_expected="quarterly",
            status="minor_revision",
            posture="Continue the NVDA glide.",
            rationale="The NVDA target weight anchors the deconcentration.",
            targets=[_target("NVDA target weight", 12.0,
                             rationale="glide anchor")],
            actions=[Action(
                label="Sell tranche monthly", horizon_kind="directional",
                detail="continue monthly tranches", rationale="pace",
            )],
            deltas_from_prior=[Delta(
                item_kind="target",
                item_id="medium.targets.nvda_target_weight",
                horizon="medium", change_kind="modified",
                summary="NVDA weight trimmed",
            )],
        ),
        short=HorizonSection(
            horizon="short", freshness_expected="monthly",
            status="major_revision",
            posture="Deploy the idle cash this month.",
            rationale="Cash drag is the near-term cost.",
        ),
        inputs=SynthesisInputs(),
        sections=[
            _section("ips", "long", "The IPS keeps the FX planning rate."),
            _section("concentration", "medium", "Concentration is on-waypoint."),
        ],
    )


def _corr(index=1, ref="medium.targets.nvda_target_weight",
          canonical=None, wrong=None, **kw):
    return {
        "index": index,
        "severity": "RED",
        "topic": kw.get("topic", "nvda-weight"),
        "plan_item_ref": ref,
        "summary": kw.get("summary", "value stale"),
        "canonical_facts": [["nvda_weight_pct", v] for v in (canonical or [])],
        "wrong_values": list(wrong or []),
    }


# ----------------------------------------------------------------------
# FULL precedence rules — each in isolation
# ----------------------------------------------------------------------


def test_snapshot_class_forces_full():
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[9.0])], directives=[],
        prior=_prior(), forces_full_tier=True,
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "snapshot-class" in r.reason


def test_unaddressable_ref_forces_full():
    r = classify_patch_reachability(
        corrections=[_corr(ref="estate.us_situs.widget", canonical=[999999])],
        directives=[], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "unaddressable" in r.reason


def test_substance_only_correction_forces_full():
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[], wrong=[])],
        directives=[], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "no concrete edit" in r.reason


def test_cross_cutting_spread_forces_full():
    # "3.00" planted in long only in _prior(); plant it in medium + short too
    prior = _prior()
    prior.medium.rationale += " Budgeting still uses 3.00 per dollar."
    prior.short.rationale += " Near-term conversions at 3.00."
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[2.944], wrong=["3.00"])],
        directives=[], prior=prior,
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "cross-cutting spread" in r.reason


def test_required_statement_is_a_concrete_edit():
    """FIX 2 (Ariel 2026-07-08): a correction with NO canonical/wrong values
    but an explicit REQUIRED-statement instruction (FM 'restate as X') is a
    concrete edit — rule 2 admits it; scope comes from the resolvable ref."""
    c = _corr(canonical=[], wrong=[])
    c["required_statement"] = (
        "hold the adjudicated glide anchor through the eligible-core window"
    )
    r = classify_patch_reachability(
        corrections=[c], directives=[], prior=_prior(),
    )
    assert r.verdict == "PATCH"
    assert r.implicated_groups == ("medium",)
    assert "medium.targets.nvda_target_weight" in r.implicated_item_ids


def test_required_statement_blank_still_substance_only():
    """Whitespace-only required_statement is NOT a concrete edit — pure
    observations keep routing FULL (FULL-first precedence unchanged)."""
    c = _corr(canonical=[], wrong=[])
    c["required_statement"] = "   "
    r = classify_patch_reachability(
        corrections=[c], directives=[], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "no concrete edit" in r.reason


def test_required_statement_still_needs_addressable_surface():
    """FULL-first precedence holds: an instruction-concrete correction whose
    ref resolves to nothing (and carries no values to locate by occurrence)
    is still unaddressable → FULL."""
    c = _corr(ref="estate.us_situs.widget", canonical=[], wrong=[])
    c["required_statement"] = "state the widget exposure explicitly"
    r = classify_patch_reachability(
        corrections=[c], directives=[], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "unaddressable" in r.reason


def test_status_flip_forces_full():
    r = classify_patch_reachability(
        corrections=[_corr(wrong=["no_change"])],
        directives=[], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "status-class flip" in r.reason


def test_status_field_ref_forces_full():
    r = classify_patch_reachability(
        corrections=[_corr(ref="long.status", canonical=[1.0])],
        directives=[], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "status-class flip" in r.reason


def test_union_spread_forces_full():
    prior = _prior()
    c1 = _corr(index=1, ref="medium.targets.nvda_target_weight",
               canonical=[9.0])
    c2 = _corr(index=2, ref="long.targets.core_diversification_floor",
               canonical=[60.0], topic="core-floor")
    c3 = _corr(index=3, ref="short", canonical=[161000], topic="cash",
               summary="deploy figure stale")
    r = classify_patch_reachability(
        corrections=[c1, c2, c3], directives=[], prior=prior,
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "union" in r.reason


# ----------------------------------------------------------------------
# PATCH verdict + occurrence widening + directive mapping
# ----------------------------------------------------------------------


def test_patch_verdict_single_slice():
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[9.0])], directives=[], prior=_prior(),
    )
    assert r.verdict == "PATCH"
    assert r.implicated_groups == ("medium",)
    assert "medium.targets.nvda_target_weight" in r.implicated_item_ids


def test_occurrence_widens_to_unnamed_slice():
    # Ref names the medium target, but the wrong value "3.00" occurs in the
    # LONG rationale — the long slice must join the implicated set.
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[2.944], wrong=["3.00"])],
        directives=[], prior=_prior(),
    )
    assert r.verdict == "PATCH"
    assert "long" in r.implicated_groups
    assert "medium" in r.implicated_groups


def test_directive_maps_to_targets_and_occurrences():
    prior = _prior()
    prior.medium.targets[0].rationale = "sell 4,136 shares this tax year"
    directive = {
        "index": 1, "proposal_id": 49, "kind": "update_plan_assumption",
        "summary": "glide schedule adjudication",
        "detail": "Replace the 12-month glide with 4,136 / 5,094 / 592.",
        "target_refs": ["medium.targets.nvda_target_weight"],
        "superseded_values": [4136],
    }
    r = classify_patch_reachability(
        corrections=[], directives=[directive], prior=prior,
    )
    assert r.verdict == "PATCH"
    assert "medium" in r.implicated_groups
    assert "medium.targets.nvda_target_weight" in r.implicated_item_ids


def test_directive_without_addressing_forces_full():
    directive = {
        "index": 1, "proposal_id": 49, "kind": "update_plan_assumption",
        "summary": "glide schedule adjudication",
        "detail": "apply the schedule verbatim",
        "target_refs": [], "superseded_values": [],
    }
    r = classify_patch_reachability(
        corrections=[], directives=[directive], prior=_prior(),
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "unaddressable" in r.reason


def test_section_ref_implicates_section_slice():
    r = classify_patch_reachability(
        corrections=[_corr(ref="section:ips", canonical=[2.944],
                           topic="ips-fx")],
        directives=[], prior=_prior(),
    )
    assert r.verdict == "PATCH"
    assert ("ips", "long") in r.implicated_sections
    assert "sections" in r.implicated_groups


def test_rendered_surface_occurrence_widens_group():
    # The wrong value lives ONLY in the prior plan's RENDERED short-horizon
    # markdown (e.g. an appendix) — the short slice must join the set.
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[2.944], wrong=["7.77"])],
        directives=[], prior=_prior(),
        rendered_surfaces={"short": "appendix: conversion at 7.77 assumed"},
    )
    assert r.verdict == "PATCH"
    assert "short" in r.implicated_groups


def test_wrong_value_in_global_surface_forces_full():
    r = classify_patch_reachability(
        corrections=[_corr(canonical=[2.944], wrong=["7.77"])],
        directives=[], prior=_prior(),
        global_surfaces={"target_allocation_json": '{"fx": "7.77"}'},
    )
    assert r.verdict == "FULL_RESYNTH"
    assert "render-only surface" in r.reason


def test_directive_check_payload_carries_superseded_values():
    from argosy.services.corrective_context import Directive

    d = Directive(
        index=1, proposal_id=49, kind="update_plan_assumption",
        summary="glide schedule", detail="apply verbatim",
        target_refs=["medium.targets.nvda_target_weight"],
        superseded_values=[4136],
    )
    payload = d.check_payload()
    assert payload["wrong_values"] == [4136]
    assert payload["canonical_values"] == []
    assert "directive #49" in payload["topic"]


def test_parse_plan_item_ref_shapes():
    p = parse_plan_item_ref("medium.targets.nvda")
    assert (p.group, p.item_kind, p.slug) == ("medium", "targets", "nvda")
    p = parse_plan_item_ref("section:ips")
    assert (p.group, p.section_id) == ("sections", "ips")
    p = parse_plan_item_ref("long.status")
    assert (p.group, p.horizon_field) == ("long", "status")
    assert parse_plan_item_ref("").group is None


# ----------------------------------------------------------------------
# Deterministic merge — adversarial stub cannot perturb unimplicated items
# ----------------------------------------------------------------------


def _adversarial_medium(prior: PlanSynthesisOutput) -> HorizonSection:
    """A hostile model output: mutates EVERYTHING in the medium slice."""
    return HorizonSection(
        horizon="medium", freshness_expected="monthly",   # mutated (restored)
        status="major_revision",                          # mutated (restored)
        posture="PATCHED POSTURE",                        # prose — accepted
        rationale="PATCHED RATIONALE",                    # prose — accepted
        targets=[
            _target("NVDA target weight", 9.0, rationale="corrected"),
        ],
        actions=[Action(                                  # mutated — restored
            label="Sell tranche monthly", horizon_kind="dated",
            trigger_or_date="2026-08-01", detail="MUTATED", rationale="MUTATED",
        )],
        themes=[],
        speculative_candidates=[],
        deltas_from_prior=[
            Delta(  # implicated item's delta — accepted
                item_kind="target",
                item_id="medium.targets.nvda_target_weight",
                horizon="medium", change_kind="modified",
                summary="NVDA weight corrected to 9%",
            ),
            Delta(  # invented delta for an unimplicated item — dropped
                item_kind="action", item_id="medium.actions.invented",
                horizon="medium", change_kind="added", summary="invented",
            ),
        ],
    )


def test_merge_adversarial_stub_cannot_perturb_unimplicated():
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _merge_patched_output,
    )

    prior = _prior()
    implicated = {"medium.targets.nvda_target_weight"}
    merged = _merge_patched_output(
        prior=prior,
        horizon_patches={"medium": _adversarial_medium(prior)},
        section_patches={},
        implicated_item_ids=implicated,
    )
    # Slice prose accepted from the model.
    assert merged.medium.posture == "PATCHED POSTURE"
    assert merged.medium.rationale == "PATCHED RATIONALE"
    # Slice-level structured fields byte-restored.
    assert merged.medium.status == prior.medium.status
    assert merged.medium.freshness_expected == prior.medium.freshness_expected
    # Implicated target accepted.
    assert merged.medium.targets[0].value == 9.0
    # Unimplicated action byte-identical despite the mutation attempt.
    assert (
        merged.medium.actions[0].model_dump_json()
        == prior.medium.actions[0].model_dump_json()
    )
    # Implicated item's delta from the model; invented delta dropped.
    delta_ids = [d.item_id for d in merged.medium.deltas_from_prior]
    assert delta_ids == ["medium.targets.nvda_target_weight"]
    assert merged.medium.deltas_from_prior[0].summary == (
        "NVDA weight corrected to 9%"
    )
    # Unpatched slices byte-identical.
    assert merged.long.model_dump_json() == prior.long.model_dump_json()
    assert merged.short.model_dump_json() == prior.short.model_dump_json()
    # Sections untouched; inputs byte-restored.
    assert [s.model_dump_json() for s in merged.sections] == [
        s.model_dump_json() for s in prior.sections
    ]
    assert merged.inputs.model_dump_json() == prior.inputs.model_dump_json()
    # Round-trips pydantic (the merge itself re-validates; belt-and-braces).
    PlanSynthesisOutput.model_validate(json.loads(merged.model_dump_json()))


def test_merge_model_added_items_dropped_and_omitted_implicated_kept():
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _merge_patched_output,
    )

    prior = _prior()
    model = _adversarial_medium(prior)
    # Model invents a brand-new target AND drops the implicated one. The
    # MODEL merge has NO positional fallback (codex patch-review major #3):
    # an id-mismatched model item is dropped and the prior kept — the
    # corrections floor then fails loud instead of trusting a guess.
    model = model.model_copy(update={
        "targets": [_target("Invented moonshot sleeve", 5.0)],
    })
    merged = _merge_patched_output(
        prior=prior,
        horizon_patches={"medium": model},
        section_patches={},
        implicated_item_ids={"medium.targets.nvda_target_weight"},
    )
    assert len(merged.medium.targets) == 1
    assert (
        merged.medium.targets[0].model_dump_json()
        == prior.medium.targets[0].model_dump_json()
    )
    # POST-REWRITE merge (counts+order invariant-enforced) opts into the
    # positional fallback — the relabelled row pairs with the implicated slot.
    merged_rw = _merge_patched_output(
        prior=prior,
        horizon_patches={"medium": model},
        section_patches={},
        implicated_item_ids={"medium.targets.nvda_target_weight"},
        allow_positional_fallback=True,
    )
    assert merged_rw.medium.targets[0].label == "Invented moonshot sleeve"
    # Count mismatch: model emits nothing — prior version kept regardless.
    model2 = model.model_copy(update={"targets": []})
    merged2 = _merge_patched_output(
        prior=prior,
        horizon_patches={"medium": model2},
        section_patches={},
        implicated_item_ids={"medium.targets.nvda_target_weight"},
        allow_positional_fallback=True,
    )
    assert (
        merged2.medium.targets[0].model_dump_json()
        == prior.medium.targets[0].model_dump_json()
    )


def test_merge_patched_section_pinned_identity_others_restored():
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _merge_patched_output,
    )

    prior = _prior()
    patched = _section("concentration", "short", "PATCHED BODY with 2.944")
    # Model even tries to relabel the section — identity is pinned to the key.
    merged = _merge_patched_output(
        prior=prior,
        horizon_patches={},
        section_patches={("ips", "long"): patched},
        implicated_item_ids=set(),
    )
    by_key = {(s.section_id, s.horizon): s for s in merged.sections}
    assert by_key[("ips", "long")].body_md == "PATCHED BODY with 2.944"
    assert by_key[("ips", "long")].section_id == "ips"
    assert by_key[("ips", "long")].horizon == "long"
    # Untouched section (evidence subtree included) byte-identical.
    assert (
        by_key[("concentration", "medium")].model_dump_json()
        == prior.sections[1].model_dump_json()
    )


def test_provenance_rows_and_unpatched_hash_guard():
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _merge_patched_output,
        _patch_provenance,
        _sha256_text,
    )

    prior = _prior()
    implicated = {"medium.targets.nvda_target_weight"}
    reach = classify_patch_reachability(
        corrections=[_corr(canonical=[9.0])], directives=[], prior=prior,
    )
    assert reach.verdict == "PATCH"
    final = _merge_patched_output(
        prior=prior,
        horizon_patches={"medium": _adversarial_medium(prior)},
        section_patches={},
        implicated_item_ids=implicated,
    )
    prov = _patch_provenance(
        prior=prior, final=final, reachability=reach,
        patched_horizons=["medium"], patched_sections=[],
    )
    rows = prov["patched_surfaces"]
    prose = [r for r in rows if r.get("surface") == "prose"]
    assert len(prose) == 1 and prose[0]["slice"] == "medium"
    assert prose[0]["before_sha256"] != prose[0]["after_sha256"]
    assert prose[0]["correction_indices"] == [1]
    item_rows = [r for r in rows if r.get("item_id")]
    assert any(
        r["item_id"] == "medium.targets.nvda_target_weight" for r in item_rows
    )
    # Unpatched-slice hashes prove non-perturbation affirmatively.
    unpatched = {u["slice"]: u for u in prov["unpatched_slice_hashes"]
                 if u["slice"] in ("long", "short")}
    assert unpatched["long"]["matches_prior"] is True
    assert unpatched["long"]["sha256"] == _sha256_text(
        prior.long.model_dump_json()
    )
    # A perturbed unpatched slice raises (the caller degrades to full).
    broken = final.model_copy(update={
        "long": final.long.model_copy(update={"posture": "PERTURBED"}),
    })
    with pytest.raises(RuntimeError, match="unpatched"):
        _patch_provenance(
            prior=prior, final=broken, reachability=reach,
            patched_horizons=["medium"], patched_sections=[],
        )


def test_preserve_unimplicated_sections_keeps_prior_coverage():
    """Full corrective regen emits ~11 of 18 sections — prior sections the
    model omitted must be restored so section_coverage cannot regress."""
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _preserve_unimplicated_sections,
    )
    from argosy.quality.canonical_sections import CANONICAL_SECTION_IDS

    ids = list(CANONICAL_SECTION_IDS.keys())
    assert len(ids) == 18
    prior = _prior().model_copy(update={
        "sections": [
            _section(sid, "long", f"prior body for {sid}") for sid in ids
        ],
    })
    model_ids = ids[:11]
    model = _prior().model_copy(update={
        "sections": [
            _section(sid, "long", f"MODEL body for {sid}") for sid in model_ids
        ],
        "medium": _prior().medium.model_copy(update={
            "posture": "MODEL POSTURE",
        }),
    })
    merged = _preserve_unimplicated_sections(prior=prior, model=model)
    assert len(merged.sections) == 18
    by_key = {(s.section_id, s.horizon): s for s in merged.sections}
    for sid in model_ids:
        assert by_key[(sid, "long")].body_md == f"MODEL body for {sid}"
    for sid in ids[11:]:
        assert by_key[(sid, "long")].body_md == f"prior body for {sid}"
    # Horizons stay the model's (full regen owns those).
    assert merged.medium.posture == "MODEL POSTURE"


def test_preserve_unimplicated_sections_keeps_model_new():
    """Model-new sections (e.g. healthcare/insurance missing from a short
    prior) must survive the merge — otherwise they can never regenerate."""
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _preserve_unimplicated_sections,
    )

    prior = _prior().model_copy(update={
        "sections": [_section("concentration", "long", "prior concentration")],
    })
    model = _prior().model_copy(update={
        "sections": [
            _section("concentration", "long", "MODEL concentration"),
            _section("healthcare", "long", "MODEL healthcare"),
            _section("insurance", "medium", "MODEL insurance"),
        ],
    })
    merged = _preserve_unimplicated_sections(prior=prior, model=model)
    by_key = {(s.section_id, s.horizon): s for s in merged.sections}
    assert set(by_key) == {
        ("concentration", "long"),
        ("healthcare", "long"),
        ("insurance", "medium"),
    }
    assert by_key[("healthcare", "long")].body_md == "MODEL healthcare"
    assert by_key[("insurance", "medium")].body_md == "MODEL insurance"
    assert by_key[("concentration", "long")].body_md == "MODEL concentration"


def test_preserve_unimplicated_sections_dedupes_duplicate_keys():
    """July-11 chain carried concentration twice — merge collapses by key."""
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _preserve_unimplicated_sections,
    )

    prior = _prior().model_copy(update={
        "sections": [
            _section("concentration", "long", "prior A"),
            _section("concentration", "long", "prior B duplicate"),
            _section("tax_plan", "long", "prior tax"),
        ],
    })
    model = _prior().model_copy(update={
        "sections": [
            _section("concentration", "long", "MODEL first"),
            _section("concentration", "long", "MODEL second wins"),
        ],
    })
    merged = _preserve_unimplicated_sections(prior=prior, model=model)
    keys = [(s.section_id, s.horizon) for s in merged.sections]
    assert keys.count(("concentration", "long")) == 1
    assert ("tax_plan", "long") in keys
    by_key = {(s.section_id, s.horizon): s for s in merged.sections}
    assert by_key[("concentration", "long")].body_md == "MODEL second wins"
    assert by_key[("tax_plan", "long")].body_md == "prior tax"


def test_preserve_unimplicated_sections_skips_implicated_prior():
    """An implicated stale prior section the model omitted is NOT restored."""
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _preserve_unimplicated_sections,
    )

    prior = _prior().model_copy(update={
        "sections": [
            _section("concentration", "long", "STALE implicated concentration"),
            _section("healthcare", "long", "prior healthcare"),
        ],
    })
    model = _prior().model_copy(update={
        "sections": [
            _section("insurance", "long", "MODEL insurance new"),
        ],
    })
    merged = _preserve_unimplicated_sections(
        prior=prior, model=model,
        implicated_sections={("concentration", "long")},
    )
    by_key = {(s.section_id, s.horizon): s for s in merged.sections}
    assert ("concentration", "long") not in by_key
    assert by_key[("healthcare", "long")].body_md == "prior healthcare"
    assert by_key[("insurance", "long")].body_md == "MODEL insurance new"


def test_synthetic_item_id_matches_prior_items_index_slug():
    assert synthetic_item_id("medium", "targets", "NVDA target weight") == (
        "medium.targets.nvda_target_weight"
    )
