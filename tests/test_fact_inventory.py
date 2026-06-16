from argosy.quality.fact_ledger import SiteKind
from argosy.quality.fact_inventory import RUN106_FACTS, FactSpec


def test_inventory_covers_the_run106_load_bearing_facts():
    expected = {
        "retirement.fi_status",
        "retirement.earliest_safe_age",
        "retirement.fi_age",
        "retirement.bridge_start_age",
        "allocation.target_weights",
        "allocation.nvda_cap_pct",
        "rsu.net_retention_pct",
        "event.rsu_tax_2026_06_17",
        "instrument.SGLN.wrapper_type",
    }
    assert expected.issubset(set(RUN106_FACTS))


def test_each_spec_names_a_derivation_and_at_least_one_site_kind():
    for fact_id, spec in RUN106_FACTS.items():
        assert isinstance(spec, FactSpec)
        assert spec.derivation, f"{fact_id} has no derivation"
        assert spec.surfaces, f"{fact_id} names no render surfaces"
        assert all(isinstance(k, SiteKind) for k in spec.site_kinds.values())


def test_allocation_facts_are_deterministic_sites():
    for fid in ("allocation.nvda_cap_pct", "allocation.target_weights"):
        kinds = set(RUN106_FACTS[fid].site_kinds.values())
        assert SiteKind.LLM_PROSE not in kinds or len(kinds) > 1
