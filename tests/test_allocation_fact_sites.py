from types import SimpleNamespace

from argosy.quality.fact_ledger import SiteKind
from argosy.services.allocation_fact_sites import build_allocation_fact_sites


def _doc():
    cls = [
        SimpleNamespace(label="US broad-market core", target_pct=60.0),
        SimpleNamespace(label="Gold", target_pct=18.0),
        SimpleNamespace(label="Bonds", target_pct=22.0),
    ]
    return SimpleNamespace(nvda_cap_pct=13.0, classes=cls)


def test_emits_cap_and_weight_sites_from_canonical_doc():
    sites = build_allocation_fact_sites(_doc(), resolved=None)
    by_fact = {}
    for s in sites:
        by_fact.setdefault(s.fact_id, []).append(s)
    cap = by_fact["allocation.nvda_cap_pct"]
    assert any(s.surface_id == "target_allocation_json" and s.normalized_value == 13.0 for s in cap)
    assert all(s.site_kind in (SiteKind.STRUCTURED_FIELD, SiteKind.TEMPLATE) for s in cap)
    weights = by_fact["allocation.target_weights"]
    assert {s.normalized_value for s in weights} >= {60.0, 18.0, 22.0}


def test_no_doc_returns_empty():
    assert build_allocation_fact_sites(None, resolved=None) == []
