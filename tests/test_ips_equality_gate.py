"""Tests for the run-106 finding [5] IPS-equality gate.

Finding [5] (AMBER contradiction): the IPS claims to be a 100%-summing
instrument map, but the named weights total ~106 before an unspecified residual
absorption — executable target weights are incoherent.

Two checks under test:
  (1) PROSE SELF-SUM — the IPS instrument-map weights rendered in prose must
      themselves sum to ~100% (±IPS_SUM_TOLERANCE_PCT).
  (2) PROSE-vs-CANONICAL EQUALITY — each sleeve present in BOTH the prose and the
      canonical target_allocation_doc must agree within tolerance.
"""
from __future__ import annotations

from types import SimpleNamespace

from argosy.quality.gate_types import GateCheck
from argosy.quality.ips_equality_gate import check_ips_equality


# A run-106-shaped IPS instrument-map prose block whose named weights total ~106.
_PLAN_106 = """\
## Investment Policy Statement — instrument map
- NVDA 13%
- Global equity 35%
- US growth tilt 18%
- International developed 15%
- Gold 5%
- REIT 5%
- Short-duration IG bonds 8%
- Cash & T-bills 7%
The above sleeves form a 100% partition of the tradeable book.
"""  # 13+35+18+15+5+5+8+7 = 106

# A clean IPS instrument map summing to exactly 100.
_PLAN_100 = """\
## Investment Policy Statement — instrument map
- NVDA 13%
- Global equity 35%
- US growth tilt 15%
- International developed 12%
- Gold 5%
- REIT 5%
- Short-duration IG bonds 8%
- Cash & T-bills 7%
The above sleeves form a 100% partition of the tradeable book.
"""  # 13+35+15+12+5+5+8+7 = 100


def _doc(**label_to_pct: float):
    """A duck-typed stand-in for TargetAllocationDoc: .classes -> [.label, .target_pct]."""
    return SimpleNamespace(
        classes=[
            SimpleNamespace(label=label, target_pct=pct)
            for label, pct in label_to_pct.items()
        ]
    )


def test_prose_self_sum_106_flags():
    """(1) Planted run-106 defect: prose weights summing to ~106 → violation."""
    violations = check_ips_equality(plan_text=_PLAN_106)
    assert violations, "expected an IPS_EQUALITY violation for a ~106% prose sum"
    assert all(v.check is GateCheck.IPS_EQUALITY for v in violations)
    assert any("106" in v.detail for v in violations)


def test_prose_self_sum_100_clean():
    """(2) Clean: prose weights summing to 100% → []."""
    assert check_ips_equality(plan_text=_PLAN_100) == []


def test_prose_vs_doc_divergence_flags():
    """(3) Prose-vs-doc divergence beyond tolerance → violation."""
    # Doc says Global equity 30%, prose says 35% — a 5pp divergence.
    doc = _doc(**{
        "NVDA": 13.0,
        "Global equity": 30.0,
        "US growth tilt": 20.0,
        "International developed": 12.0,
        "Gold": 5.0,
        "REIT": 5.0,
        "Short-duration IG bonds": 8.0,
        "Cash & T-bills": 7.0,
    })  # doc sums to 100, so check (1) is clean; only the equality check fires
    violations = check_ips_equality(plan_text=_PLAN_100, target_allocation_doc=doc)
    assert violations, "expected an IPS_EQUALITY violation for prose-vs-doc divergence"
    assert all(v.check is GateCheck.IPS_EQUALITY for v in violations)
    assert any("Global equity" in v.detail for v in violations)


def test_prose_vs_doc_agreement_clean():
    """(4) Prose-vs-doc agreement (all overlapping sleeves match) → []."""
    doc = _doc(**{
        "NVDA": 13.0,
        "Global equity": 35.0,
        "US growth tilt": 15.0,
        "International developed": 12.0,
        "Gold": 5.0,
        "REIT": 5.0,
        "Short-duration IG bonds": 8.0,
        "Cash & T-bills": 7.0,
    })
    assert check_ips_equality(plan_text=_PLAN_100, target_allocation_doc=doc) == []


def test_doc_without_classes_falls_back_gracefully():
    """A doc whose shape is not discoverable disables check (2), never raises."""
    weird = SimpleNamespace(not_classes=[1, 2, 3])
    # Clean prose + unrecognizable doc → no check (2), and check (1) is clean.
    assert check_ips_equality(plan_text=_PLAN_100, target_allocation_doc=weird) == []
