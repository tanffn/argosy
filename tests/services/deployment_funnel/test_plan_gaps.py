from types import SimpleNamespace

from argosy.services.deployment_funnel.plan_gaps import detect_missing_classes


def _doc(labels):
    return SimpleNamespace(
        classes=[SimpleNamespace(label=lbl, target_pct=1.0, instruments=[])
                 for lbl in labels]
    )


def test_missing_gold_is_detected():
    doc = _doc(["US broad-market core", "Cash & T-bills (incl. ILS tranche)",
                "Emerging-markets equity"])
    gaps = detect_missing_classes(doc)
    assert len(gaps) == 1
    g = gaps[0]
    assert "gold" in g.asset_class
    assert g.proposed_target_pct is None       # weight is engine-derived, not invented
    assert g.current_target_pct == 0.0


def test_plan_with_gold_has_no_structural_gap():
    doc = _doc(["US broad-market core", "Gold / Alternatives", "Cash & T-bills"])
    assert detect_missing_classes(doc) == []


def test_alternatives_label_also_satisfies():
    doc = _doc(["US core", "Real assets & commodities"])
    assert detect_missing_classes(doc) == []


def test_none_doc_returns_empty():
    assert detect_missing_classes(None) == []
