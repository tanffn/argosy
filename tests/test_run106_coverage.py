"""Run-106 acceptance backbone — each reader finding maps to a NAMED invariant.

The whole-artifact LLM reader BLOCKed run 106 (draft 42) on 11 findings
(``tmp_review/overnight_synth_report_run5.txt``). The shift-left contract: every
MECHANICAL/SEMANTIC finding-class must be caught by a NAMED deterministic
invariant BEFORE the reader sees the draft — "the reader might catch it" does
NOT count. This test plants a minimal defect per finding and asserts the named
invariant fires with the expected ``GateCheck``.

Findings [8] (SOFI evidence-readiness), [9] (action-level estate routing) and
[10] (coverage-status prose) are EXPLICITLY DEFERRED (lower priority in the
session handover). They are listed in ``DEFERRED_FINDINGS`` so the gap is loud,
not silent — this test FAILS LOUDLY if someone claims full coverage without
building them.
"""
from __future__ import annotations

from types import SimpleNamespace

from argosy.quality.gate_types import GateCheck
from argosy.quality.fi_timeline_gate import check_fi_timeline_coherence
from argosy.quality.fi_fx_shock_gate import check_fi_sufficiency_under_fx_shock
from argosy.quality.retirement_age_label_gate import check_retirement_age_labels
from argosy.quality.rsu_retention_gate import check_rsu_retention_consistency
from argosy.quality.event_currency_gate import check_event_currency_consistency
from argosy.quality.ips_equality_gate import check_ips_equality
from argosy.quality.instrument_taxonomy_gate import check_instrument_taxonomy
from argosy.quality.stale_reviewer_text_gate import check_stale_reviewer_text


# The findings caught in-stage today, by a named deterministic invariant.
COVERED_FINDINGS = {0, 1, 2, 3, 4, 5, 6, 7}
# Lower-priority finding-classes NOT yet built (handover-deferred). Loud, not
# silent: documented here so "full coverage" can't be claimed prematurely.
DEFERRED_FINDINGS = {
    8: "evidence_readiness_gate (SOFI promoted while news adapter missing)",
    9: "estate_routing_gate (estate precondition vs SGOV at Schwab)",
    10: "coverage_status_gate (coverage appendix confidence contradiction)",
}


def _has(violations, check: GateCheck) -> bool:
    return any(v.check == check for v in violations)


def test_finding_0_fi_fragile_under_fx_shock():
    fx_shock_result = {
        "base": {"net_worth_nis": 11_950_000, "perpetuity_reached": True},
        "fx_shock_-0.10": {"net_worth_nis": 9_900_000, "perpetuity_reached": False},
    }
    plan_text = "Capital sufficiency reached. The plan is fully funded."
    assert _has(
        check_fi_sufficiency_under_fx_shock(fx_shock_result=fx_shock_result, plan_text=plan_text),
        GateCheck.FI_FX_SHOCK_SUFFICIENCY,
    )


def test_finding_1_fi_timeline_contradiction():
    plan_text = (
        "Financial independence has already been crossed today. "
        "Deterministic FI age is 47. Typical-scenario FI age is 45 with 2.0 years remaining."
    )
    assert _has(
        check_fi_timeline_coherence(plan_text=plan_text),
        GateCheck.FI_TIMELINE_COHERENCE,
    )


def test_finding_2_retirement_age_bridge_regression():
    # Headline/sizing age 46 but the bridge is sized from 47 — a dropped year.
    assert _has(
        check_retirement_age_labels(
            plan_text="Retirement age 46. Bridge sleeve sized from age 47 to 60.",
            earliest_safe_age=46,
            fi_age=46,
            bridge_start_age=47,
        ),
        GateCheck.RETIREMENT_AGE_LABEL,
    )


def test_finding_3_rsu_retention_inconsistent():
    plan_text = (
        "RSU net retention is 47% after tax. "
        "The equity-comp evidence shows net retention of 65%."
    )
    assert _has(
        check_rsu_retention_consistency(plan_text=plan_text),
        GateCheck.RSU_RETENTION_CONSISTENCY,
    )


def test_finding_4_event_currency_flip():
    plan_text = (
        "The June 17 RSU tax is estimated at ₪180,000. "
        "Later: the June 17 RSU tax of $52,000 is due."
    )
    assert _has(
        check_event_currency_consistency(plan_text=plan_text),
        GateCheck.EVENT_CURRENCY_CONSISTENCY,
    )


def test_finding_5_ips_weights_overshoot_100():
    plan_text = (
        "IPS instrument map:\n"
        "NVDA 13%\nGlobal equity 60%\nGold 18%\nBonds 15%\n"
        "These sum to a 100% partition."
    )
    assert _has(
        check_ips_equality(plan_text=plan_text),
        GateCheck.IPS_EQUALITY,
    )


def test_finding_6_stale_fm_objection():
    objection = "The medium target is still 3,000 sh/yr, which is too aggressive."
    plan_text = "Medium target: 5,600 sh/yr deconcentration cadence."
    assert _has(
        check_stale_reviewer_text(plan_text=plan_text, objection_text=objection),
        GateCheck.STALE_REVIEWER_TEXT,
    )


def test_finding_7_instrument_taxonomy_contradiction():
    plan_text = (
        "SGLN is a physical-gold ETC, not a UCITS fund. "
        "Action: migrate SGLN into the UCITS wrapper."
    )
    assert _has(
        check_instrument_taxonomy(plan_text=plan_text),
        GateCheck.INSTRUMENT_TAXONOMY,
    )


def test_deferred_findings_are_explicitly_tracked():
    """The 3 lower-priority finding-classes are not yet built. This asserts the
    gap is documented (loud), and that covered + deferred partition all 11."""
    assert COVERED_FINDINGS | set(DEFERRED_FINDINGS) == set(range(11))
    assert COVERED_FINDINGS.isdisjoint(DEFERRED_FINDINGS)
