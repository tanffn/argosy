"""Tests for the flow-events telemetry types (argosy/quality/flow_events.py).

Pure data types: construction, defaults, the `kind` property, and the
`__post_init__` validation. No DB, no LLM, no I/O.
"""
from __future__ import annotations

import dataclasses

import pytest

from argosy.quality.flow_events import (
    ComplianceFinding,
    CrossModelValidation,
    Escalation,
    FlowEventKind,
    ZigZagAction,
    ZigZagRound,
    event_kind,
)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
def test_flow_event_kind_members():
    assert FlowEventKind.ZIGZAG_ROUND == "zigzag_round"
    assert FlowEventKind.CROSS_MODEL_VALIDATION == "cross_model_validation"
    assert FlowEventKind.ESCALATION == "escalation"
    assert FlowEventKind.COMPLIANCE_FINDING == "compliance_finding"
    # str-Enum: value is a plain string
    assert FlowEventKind.ZIGZAG_ROUND.value == "zigzag_round"
    assert isinstance(FlowEventKind.ESCALATION, str)


def test_zigzag_action_members():
    assert ZigZagAction.ACCEPT == "accept"
    assert ZigZagAction.PUSHBACK == "pushback"
    assert ZigZagAction.TWEAK == "tweak"
    assert ZigZagAction.COUNTER == "counter"
    assert isinstance(ZigZagAction.ACCEPT, str)


# --------------------------------------------------------------------------
# ZigZagRound
# --------------------------------------------------------------------------
def test_zigzag_round_construction_and_defaults():
    r = ZigZagRound(edge="tax->investment", round=1, action=ZigZagAction.PUSHBACK)
    assert r.edge == "tax->investment"
    assert r.round == 1
    assert r.action is ZigZagAction.PUSHBACK
    assert r.objection is None
    assert r.evidence == ()
    assert r.before_value is None
    assert r.after_value is None


def test_zigzag_round_full():
    r = ZigZagRound(
        edge="tax->investment",
        round=2,
        action=ZigZagAction.TWEAK,
        objection="basis ignores wash sale",
        evidence=("ledger:line42", "schwab.csv"),
        before_value="100",
        after_value="92",
    )
    assert r.objection == "basis ignores wash sale"
    assert r.evidence == ("ledger:line42", "schwab.csv")
    assert r.before_value == "100"
    assert r.after_value == "92"


def test_zigzag_round_kind():
    r = ZigZagRound(edge="a->b", round=1, action=ZigZagAction.ACCEPT)
    assert r.kind is FlowEventKind.ZIGZAG_ROUND


def test_zigzag_round_frozen():
    r = ZigZagRound(edge="a->b", round=1, action=ZigZagAction.ACCEPT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.round = 2  # type: ignore[misc]


# --------------------------------------------------------------------------
# CrossModelValidation
# --------------------------------------------------------------------------
def test_cross_model_validation_agree():
    c = CrossModelValidation(
        figure_id="net_worth",
        producer_model="opus",
        validator_model="sonnet",
        producer_value="1.2M",
        validator_value="1.2M",
        verdict="agree",
    )
    assert c.figure_id == "net_worth"
    assert c.producer_model == "opus"
    assert c.validator_model == "sonnet"
    assert c.producer_value == "1.2M"
    assert c.validator_value == "1.2M"
    assert c.verdict == "agree"
    assert c.divergence is None


def test_cross_model_validation_diverge():
    c = CrossModelValidation(
        figure_id="fi_margin",
        producer_model="opus",
        validator_model="sonnet",
        producer_value="62.5%",
        validator_value="56.9%",
        verdict="diverge",
        divergence="5.6pp gap",
    )
    assert c.verdict == "diverge"
    assert c.divergence == "5.6pp gap"


def test_cross_model_validation_allows_none_values():
    c = CrossModelValidation(
        figure_id="x",
        producer_model="opus",
        validator_model="sonnet",
        producer_value=None,
        validator_value=None,
        verdict="agree",
    )
    assert c.producer_value is None
    assert c.validator_value is None


def test_cross_model_validation_kind():
    c = CrossModelValidation(
        figure_id="x",
        producer_model="opus",
        validator_model="sonnet",
        producer_value="1",
        validator_value="1",
        verdict="agree",
    )
    assert c.kind is FlowEventKind.CROSS_MODEL_VALIDATION


def test_cross_model_validation_bad_verdict_raises():
    with pytest.raises(ValueError):
        CrossModelValidation(
            figure_id="x",
            producer_model="opus",
            validator_model="sonnet",
            producer_value="1",
            validator_value="1",
            verdict="maybe",
        )


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------
def test_escalation_construction_and_default():
    e = Escalation(
        subject="tax->investment",
        escalated_to="fund_manager",
        arbiter="audit",
        ruling="hold the 18% cap",
    )
    assert e.subject == "tax->investment"
    assert e.escalated_to == "fund_manager"
    assert e.arbiter == "audit"
    assert e.ruling == "hold the 18% cap"
    assert e.rationale == ""


def test_escalation_full():
    e = Escalation(
        subject="net_worth",
        escalated_to="fund_manager",
        arbiter="audit",
        ruling="use resolver value",
        rationale="resolver is canonical",
    )
    assert e.rationale == "resolver is canonical"


def test_escalation_kind():
    e = Escalation(subject="x", escalated_to="r1", arbiter="r2", ruling="ok")
    assert e.kind is FlowEventKind.ESCALATION


# --------------------------------------------------------------------------
# ComplianceFinding
# --------------------------------------------------------------------------
def test_compliance_finding_construction_and_defaults():
    f = ComplianceFinding(
        finding_kind="contradiction",
        root_cause_owner="synthesizer",
        severity="blocker",
        materiality="high",
    )
    assert f.finding_kind == "contradiction"
    assert f.root_cause_owner == "synthesizer"
    assert f.severity == "blocker"
    assert f.materiality == "high"
    assert f.evidence == ()
    assert f.disposition == "open"
    assert f.audit_ref is None


def test_compliance_finding_full():
    f = ComplianceFinding(
        finding_kind="stale",
        root_cause_owner="macro",
        severity="amber",
        materiality="medium",
        evidence=("snapshot:2026-05-01",),
        disposition="routed",
        audit_ref="audit:42",
    )
    assert f.evidence == ("snapshot:2026-05-01",)
    assert f.disposition == "routed"
    assert f.audit_ref == "audit:42"


def test_compliance_finding_kind():
    f = ComplianceFinding(
        finding_kind="unsupported",
        root_cause_owner="bull",
        severity="yellow",
        materiality="low",
    )
    assert f.kind is FlowEventKind.COMPLIANCE_FINDING


def test_compliance_finding_bad_severity_raises():
    with pytest.raises(ValueError):
        ComplianceFinding(
            finding_kind="contradiction",
            root_cause_owner="synthesizer",
            severity="critical",
            materiality="high",
        )


def test_compliance_finding_bad_disposition_raises():
    with pytest.raises(ValueError):
        ComplianceFinding(
            finding_kind="contradiction",
            root_cause_owner="synthesizer",
            severity="blocker",
            materiality="high",
            disposition="ignored",
        )


@pytest.mark.parametrize("disp", ["open", "routed", "remediated", "escalated", "accepted_risk"])
def test_compliance_finding_all_dispositions_ok(disp):
    f = ComplianceFinding(
        finding_kind="contradiction",
        root_cause_owner="synthesizer",
        severity="blocker",
        materiality="high",
        disposition=disp,
    )
    assert f.disposition == disp


@pytest.mark.parametrize("sev", ["blocker", "amber", "yellow"])
def test_compliance_finding_all_severities_ok(sev):
    f = ComplianceFinding(
        finding_kind="contradiction",
        root_cause_owner="synthesizer",
        severity=sev,
        materiality="high",
    )
    assert f.severity == sev


# --------------------------------------------------------------------------
# event_kind helper
# --------------------------------------------------------------------------
def test_event_kind_helper_over_heterogeneous_list():
    events = [
        ZigZagRound(edge="a->b", round=1, action=ZigZagAction.ACCEPT),
        CrossModelValidation(
            figure_id="x",
            producer_model="opus",
            validator_model="sonnet",
            producer_value="1",
            validator_value="1",
            verdict="agree",
        ),
        Escalation(subject="x", escalated_to="r1", arbiter="r2", ruling="ok"),
        ComplianceFinding(
            finding_kind="contradiction",
            root_cause_owner="synthesizer",
            severity="blocker",
            materiality="high",
        ),
    ]
    kinds = [event_kind(e) for e in events]
    assert kinds == [
        FlowEventKind.ZIGZAG_ROUND,
        FlowEventKind.CROSS_MODEL_VALIDATION,
        FlowEventKind.ESCALATION,
        FlowEventKind.COMPLIANCE_FINDING,
    ]
