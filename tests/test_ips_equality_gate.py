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


# A single-sleeve IPS-cued section is a FOCUSED NOTE, not a 100% partition claim.
# Live pv55 false positive: the densest IPS-cued section was a NVDA note
# ("NVDA IPS sleeve target is 12.0% ...") whose lone surviving weight summed to
# 12.0% → the self-sum flagged "must be 100". A <3-sleeve section cannot be a
# partition; the self-sum must not run on it (the per-sleeve canonical check still does).
_PLAN_NVDA_NOTE = """\
## IPS — NVDA single-name policy
NVDA IPS sleeve target is 12.0% of the tradeable book; the binding
instrument-level cap sits ~1pp above at 13.0%. Current weight is 62.5%.
"""


def test_single_sleeve_ips_note_is_not_a_partition_self_sum():
    """A focused 1-sleeve IPS note (NVDA 12%) must NOT fire the 100%-partition
    self-sum (the live pv55 false positive)."""
    assert check_ips_equality(plan_text=_PLAN_NVDA_NOTE) == []


def test_single_sleeve_note_still_checked_against_canonical_doc():
    """The carve-out only skips the SELF-SUM; a prose sleeve still must agree with
    the canonical doc — a 12% prose NVDA vs a 30% doc NVDA still flags."""
    one_sleeve = "## IPS — NVDA single-name policy\n- NVDA 12%\n"
    viols = check_ips_equality(
        plan_text=one_sleeve, target_allocation_doc=_doc(NVDA=30.0)
    )
    assert any(v.check is GateCheck.IPS_EQUALITY for v in viols), (
        "the per-sleeve canonical-equality check must still run for a 1-sleeve section"
    )


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


# A non-map prose paragraph that merely MENTIONS "IPS" in running text and
# happens to carry loose percentages (35 + 18 ≈ 53). The old bare-\bIPS\b anchor
# scoped the "section" onto this prose and summed those numbers → spurious flag.
_PROSE_IPS_MENTION = """\
The IPS is reviewed quarterly. Equity returned 35% last year. Gold rose 18%.
"""

# An earlier in-prose "IPS" sentence BEFORE the real heading. The old anchor
# matched the prose "IPS" and stopped at the first heading, capturing only the
# narration and NEVER scanning the real instrument map below (a ~106% defect).
_PLAN_PROSE_THEN_MAP_106 = """\
Our IPS sets the policy. We revisit it each quarter.

## IPS Instrument Map
NVDA 13%
Global equity 60%
Gold 18%
Bonds 15%
"""  # 13+60+18+15 = 106


def test_prose_ips_mention_no_map_is_clean():
    """FALSE-POSITIVE guard: a prose paragraph mentioning 'IPS' with loose
    percentages but NO instrument-map heading must NOT be scoped/summed → []."""
    assert check_ips_equality(plan_text=_PROSE_IPS_MENTION) == []


def test_prose_ips_mention_before_real_map_still_flags():
    """FALSE-NEGATIVE catch: an earlier prose 'IPS' sentence must not shadow the
    real '## IPS Instrument Map' section — its ~106% sum must still flag."""
    violations = check_ips_equality(plan_text=_PLAN_PROSE_THEN_MAP_106)
    assert violations, "expected the real instrument map's ~106% sum to flag"
    assert all(v.check is GateCheck.IPS_EQUALITY for v in violations)
    assert any("106" in v.detail for v in violations)
