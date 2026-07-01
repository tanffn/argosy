"""detect_missing_classes is a plan-structural gap detector. Its required-class
set is deliberately EMPTY (owner decided gold is intentionally excluded), so it
is a no-op today; these tests lock that no-assertion behaviour + the mechanism."""
from types import SimpleNamespace

import argosy.services.deployment_funnel.plan_gaps as pg
from argosy.services.deployment_funnel.plan_gaps import detect_missing_classes


def _doc(labels):
    return SimpleNamespace(
        classes=[SimpleNamespace(label=lbl, target_pct=1.0, instruments=[])
                 for lbl in labels]
    )


def test_no_required_classes_means_no_gaps():
    # Gold absent, but we do NOT flag it — the owner excluded it deliberately.
    doc = _doc(["US broad-market core", "Cash & T-bills", "Emerging-markets equity"])
    assert detect_missing_classes(doc) == []


def test_none_doc_returns_empty():
    assert detect_missing_classes(None) == []


def test_expected_classes_is_empty_no_hardcoded_gold():
    # Regression: nothing is hardcoded as required (no re-introduced gold gap).
    assert pg._EXPECTED_CLASSES == ()


def test_mechanism_detects_when_a_class_IS_required(monkeypatch):
    # The extension point still works if a class is ever genuinely required.
    monkeypatch.setattr(
        pg, "_EXPECTED_CLASSES", (("tips", ("tips", "inflation-linked")),)
    )
    doc = _doc(["US broad-market core", "Cash & T-bills"])
    gaps = detect_missing_classes(doc)
    assert len(gaps) == 1 and gaps[0].asset_class == "tips"
    assert gaps[0].proposed_target_pct is None
