import json
import math

from argosy.quality.derivation_graph import NodeKind
from argosy.quality.graph_hydration import (
    KNOWN_RECIPE_ARGMAP,
    KNOWN_RECIPE_KEYS,
    MANIFEST_EDGES,
    MISSING_PREFIX,
    _kind_for_key,
    add_surface_nodes,
    build_manifest_nodes,
    defective_surfaces,
    hydrate_current_plan,
    recompute_safe,
    surface_key,
)
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


# --------------------------------------------------------------------------
# Task 1: INPUT/DERIVED node-kind classification
# --------------------------------------------------------------------------
def test_known_recipe_key_is_derived():
    assert "fi_margin_liquid_nis" in KNOWN_RECIPE_KEYS.values()
    assert _kind_for_key("retirement.fi_margin_signed_nis", has_upstream=True) is NodeKind.DERIVED


def test_key_with_no_upstream_is_input():
    assert _kind_for_key("spend.annual_t12_nis", has_upstream=False) is NodeKind.INPUT


def test_key_with_upstream_is_derived():
    assert _kind_for_key("concentration.nvda_target_sh", has_upstream=True) is NodeKind.DERIVED


# --------------------------------------------------------------------------
# Task 2: declared upstream edges
# --------------------------------------------------------------------------
def test_fi_margin_edges_match_resolver_derivation():
    assert MANIFEST_EDGES["retirement.fi_margin_signed_nis"] == (
        "portfolio.liquid_net_worth_nis",
        "retirement.fi_total_capital_nis",
    )


def test_nvda_deconcentration_edges_match_resolver_derivation():
    assert MANIFEST_EDGES["concentration.nvda_target_sh"] == (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    )
    assert MANIFEST_EDGES["concentration.nvda_sell_sh"] == (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    )


def test_key_without_declared_edges_has_none():
    assert "spend.annual_t12_nis" not in MANIFEST_EDGES


# --------------------------------------------------------------------------
# Task 3: build INPUT + DERIVED nodes from the resolver manifest
# --------------------------------------------------------------------------
def _rv(key, value, unit="nis"):
    return ResolvedValue(key=key, value=value, unit=unit, status="resolved",
                         source_locator=f"{key} (test)")


def _manifest() -> ResolvedPlanNumbers:
    vals = {v.key: v for v in [
        _rv("portfolio.liquid_net_worth_nis", 11_500_000.0),
        _rv("retirement.fi_total_capital_nis", 11_650_000.0),
        _rv("retirement.fi_margin_signed_nis", -150_000.0),
        _rv("spend.annual_t12_nis", 600_000.0),
    ]}
    return ResolvedPlanNumbers(values=vals)


def test_leaf_key_becomes_input_node_with_value():
    g = build_manifest_nodes(_manifest())
    n = g.get("spend.annual_t12_nis")
    assert n.kind is NodeKind.INPUT
    assert n.value == 600_000.0


def test_derived_key_becomes_derived_node_with_edges_and_recipe():
    g = build_manifest_nodes(_manifest())
    n = g.get("retirement.fi_margin_signed_nis")
    assert n.kind is NodeKind.DERIVED
    assert n.inputs == ("portfolio.liquid_net_worth_nis",
                        "retirement.fi_total_capital_nis")
    assert n.recipe is not None


def test_pending_key_becomes_valueless_input_node():
    rv = ResolvedValue.pending("retirement.fi_age", "age", "pending")
    g = build_manifest_nodes(ResolvedPlanNumbers(values={rv.key: rv}))
    n = g.get("retirement.fi_age")
    assert n.kind is NodeKind.INPUT
    assert n.value is None


# --------------------------------------------------------------------------
# Task 4 + 5: round-trip
# --------------------------------------------------------------------------
def test_echo_derived_roundtrips_to_manifest_value():
    vals = {v.key: v for v in [
        _rv("portfolio.liquid_net_worth_nis", 11_500_000.0),
        _rv("retirement.fi_total_capital_nis", 11_650_000.0),
        _rv("spend.annual_t12_nis", 600_000.0),
        _rv("savings.annual_net_nis", 821_000.0),
    ]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    g.recompute()
    for k, expected in (("spend.annual_t12_nis", 600_000.0),
                        ("savings.annual_net_nis", 821_000.0)):
        assert g.get(k).value == expected


def test_known_recipe_fi_margin_roundtrips_via_real_recipe():
    liquid, total = 11_500_000.0, 11_650_000.0
    expected_margin = liquid - total  # -150_000.0
    vals = {v.key: v for v in [
        _rv("portfolio.liquid_net_worth_nis", liquid),
        _rv("retirement.fi_total_capital_nis", total),
        _rv("retirement.fi_margin_signed_nis", expected_margin),
    ]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    g.recompute()
    got = g.get("retirement.fi_margin_signed_nis").value
    assert math.isclose(float(got), expected_margin, abs_tol=1.0)
    assert g.is_closed() is True


def test_argmap_renames_manifest_keys_to_recipe_args():
    amap = KNOWN_RECIPE_ARGMAP["retirement.fi_margin_signed_nis"]
    assert amap == {
        "portfolio.liquid_net_worth_nis": "liquid_nw_nis",
        "retirement.fi_total_capital_nis": "fi_total_capital_nis",
    }


# --------------------------------------------------------------------------
# Task 6: SURFACE nodes + defect case
# --------------------------------------------------------------------------
def _section(section_id, horizon, locator):
    from argosy.agents.plan_synthesizer_types import (
        Citation, FactClaim, Section, SectionEvidence,
    )
    return Section(
        section_id=section_id, horizon=horizon, title="t",
        body_md="body text long enough",
        evidence=SectionEvidence(
            facts=[FactClaim(text="a sufficiently long fact claim", kind="numeric",
                             value="1", unit="nis")],
            source_span=[Citation(source_kind="analyst_report",
                                  source_locator=locator,
                                  extract="extract>=8 chars",
                                  supports_fact_index=0)],
        ),
    )


def test_surface_node_edges_inferred_from_citation_locator():
    vals = {v.key: v for v in [_rv("portfolio.liquid_net_worth_nis", 11_500_000.0)]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    sec = _section("concentration", "long",
                   "portfolio.liquid_net_worth_nis (analyst extract)")
    add_surface_nodes(g, [sec])
    n = g.get(surface_key(sec))
    assert n.kind is NodeKind.SURFACE
    assert "portfolio.liquid_net_worth_nis" in n.inputs
    g.recompute()
    assert g.is_valid(surface_key(sec)) is True


def test_surface_citing_unresolved_key_is_invalid_not_silently_valid():
    g = build_manifest_nodes(ResolvedPlanNumbers(values={}))
    sec = _section("fi_bridge", "long",
                   "retirement.fi_age (cited but never resolved)")
    add_surface_nodes(g, [sec])
    skey = surface_key(sec)
    assert any(i.startswith(MISSING_PREFIX) for i in g.get(skey).inputs)
    recompute_safe(g)
    assert g.is_valid(skey) is False


# --------------------------------------------------------------------------
# Task 7: defective_surfaces + acyclicity
# --------------------------------------------------------------------------
def test_defective_surfaces_lists_the_unresolved_citation_surface():
    g = build_manifest_nodes(ResolvedPlanNumbers(values={}))
    sec = _section("fi_bridge", "long",
                   "retirement.fi_age (never resolved)")
    add_surface_nodes(g, [sec])
    recompute_safe(g)
    defects = defective_surfaces(g)
    assert surface_key(sec) in defects


def test_hydrated_graph_is_acyclic():
    vals = {v.key: v for v in [_rv("portfolio.liquid_net_worth_nis", 1.0)]}
    g = build_manifest_nodes(ResolvedPlanNumbers(values=vals))
    g.check_acyclic()  # must not raise


# --------------------------------------------------------------------------
# Task 8: DB-reading wrapper
# --------------------------------------------------------------------------
def test_hydrate_current_plan_builds_surface_from_sections_json():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, PlanVersion

    # Hermetic in-memory DB (sync session, the verify_run.py create_engine
    # pattern) so the test does not collide with the dev DB's UNIQUE(user_id)
    # row on plan_versions.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db_session = Session()
    try:
        section = {
            "section_id": "concentration", "horizon": "long", "title": "t",
            "body_md": "body text long enough",
            "evidence": {
                "facts": [{"text": "a sufficiently long fact claim",
                           "kind": "numeric", "value": "1", "unit": "nis"}],
                "source_span": [{"source_kind": "analyst_report",
                                 "source_locator": "concentration.nvda_cap_pct (extract)",
                                 "extract": "extract>=8 chars",
                                 "supports_fact_index": 0}],
                "assumptions": [], "missing_data": [],
            },
        }
        pv = PlanVersion(user_id="ariel", role="current", version_label="t",
                         sections_json=json.dumps([section]))
        db_session.add(pv)
        db_session.commit()

        g = hydrate_current_plan(db_session, user_id="ariel",
                                 decision_run_id=getattr(pv, "decision_run_id", None) or 0)
        assert g.get("surface:long:concentration") is not None
    finally:
        db_session.close()
        engine.dispose()


# --------------------------------------------------------------------------
# Task 9: public exports
# --------------------------------------------------------------------------
def test_public_exports_present():
    import argosy.quality.graph_hydration as gh
    for name in ("hydrate_current_plan", "hydrate_graph_from_manifest",
                 "build_manifest_nodes", "add_surface_nodes", "recompute_safe",
                 "defective_surfaces", "surface_key", "MANIFEST_EDGES",
                 "KNOWN_RECIPE_KEYS", "KNOWN_RECIPE_ARGMAP", "MISSING_PREFIX"):
        assert name in gh.__all__
