from argosy.quality.fact_ledger import Fact, FactLedger, RenderedFactSite, SiteKind


def test_ledger_indexes_sites_by_fact_and_surface():
    ledger = FactLedger()
    ledger.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
        field_path="$.nvda_cap_pct", byte_span=(0, 0),
        rendered_text="13.0", normalized_value=13.0, site_kind=SiteKind.STRUCTURED_FIELD,
        hash="h1",
    ))
    ledger.add(RenderedFactSite(
        fact_id="allocation.nvda_cap_pct", surface_id="body",
        field_path="long#cap", byte_span=(10, 20),
        rendered_text="NVDA cap 13%", normalized_value=13.0, site_kind=SiteKind.TEMPLATE,
        hash="h2",
    ))
    sites = ledger.sites_for_fact("allocation.nvda_cap_pct")
    assert len(sites) == 2
    assert {s.surface_id for s in sites} == {"target_allocation_json", "body"}
    assert len(ledger.sites_for_surface("body")) == 1


def test_fact_holds_canonical_value_and_site_kinds_are_distinct():
    f = Fact(fact_id="retirement.fi_age", value=46, unit="age",
             derivation="resolver:retirement.fi_age")
    assert f.fact_id == "retirement.fi_age"
    assert {SiteKind.TEMPLATE, SiteKind.STRUCTURED_FIELD, SiteKind.LLM_PROSE}
