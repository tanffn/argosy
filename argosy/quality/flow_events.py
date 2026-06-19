"""Structured event types for the multi-agent corrections & verification flow.

These record the *flow* of how a plan is corrected and verified — the
zig-zag negotiation between agent pairs, cross-model figure validation,
escalations to an arbiter, and the compliance findings produced along the
way — so the flow can be persisted as telemetry and rendered in the UI.

Pure data types only: frozen dataclasses + str-Enums, in the style of
`argosy/quality/gate_types.py`. No DB, no LLM, no I/O, and zero internal
argosy dependencies — owner/role/model fields are plain `str`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FlowEventKind(str, Enum):
    """The kinds of event recorded in the corrections & verification flow."""

    ZIGZAG_ROUND = "zigzag_round"
    CROSS_MODEL_VALIDATION = "cross_model_validation"
    ESCALATION = "escalation"
    COMPLIANCE_FINDING = "compliance_finding"


class ZigZagAction(str, Enum):
    """What one party did in a single round of a zig-zag negotiation."""

    ACCEPT = "accept"
    PUSHBACK = "pushback"
    TWEAK = "tweak"
    COUNTER = "counter"


# Allowed value sets for the validated string fields.
_VALID_VERDICTS = frozenset({"agree", "diverge"})
_VALID_SEVERITIES = frozenset({"blocker", "amber", "yellow"})
_VALID_DISPOSITIONS = frozenset(
    {"open", "routed", "remediated", "escalated", "accepted_risk"}
)
_VALID_MATERIALITY = frozenset({"high", "medium", "low"})
_VALID_FINDING_KINDS = frozenset({
    "contradiction", "unsupported", "stale", "unsuitable",
    "missing_disclosure", "bad_method", "narrative_conflict", "extraction_error",
})


@dataclass(frozen=True)
class ZigZagRound:
    """One round of negotiation across a directed agent edge.

    `edge` is a "from->to" pair (e.g. "tax->investment"). `before_value` /
    `after_value` capture the figure under negotiation when the round
    changed it.
    """

    edge: str
    round: int
    action: ZigZagAction
    objection: str | None = None
    evidence: tuple[str, ...] = ()
    before_value: str | None = None
    after_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple):
            object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def kind(self) -> FlowEventKind:
        return FlowEventKind.ZIGZAG_ROUND


@dataclass(frozen=True)
class CrossModelValidation:
    """One figure re-derived by a second model to check the first.

    `verdict` must be "agree" or "diverge". `divergence` describes the gap
    when the verdict is "diverge".
    """

    figure_id: str
    producer_model: str
    validator_model: str
    producer_value: str | None
    validator_value: str | None
    verdict: str
    divergence: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"verdict must be one of {sorted(_VALID_VERDICTS)}, "
                f"got {self.verdict!r}"
            )
        # A diverge verdict must explain the gap; an agree verdict must NOT carry
        # one — otherwise the telemetry contradicts itself (codex impl review #6).
        if self.verdict == "diverge" and not (self.divergence and self.divergence.strip()):
            raise ValueError("verdict 'diverge' requires a non-empty divergence")
        if self.verdict == "agree" and self.divergence:
            raise ValueError("verdict 'agree' must not carry a divergence")
        # Cross-model means two DIFFERENT models; same model is not a cross-check.
        if self.producer_model == self.validator_model:
            raise ValueError(
                "cross-model validation requires distinct producer/validator models, "
                f"both were {self.producer_model!r}"
            )

    @property
    def kind(self) -> FlowEventKind:
        return FlowEventKind.CROSS_MODEL_VALIDATION


@dataclass(frozen=True)
class Escalation:
    """A subject escalated to an arbiter who issued a ruling.

    `subject` is a figure-id or edge; `escalated_to` and `arbiter` are roles.
    """

    subject: str
    escalated_to: str
    arbiter: str
    ruling: str
    rationale: str = ""

    @property
    def kind(self) -> FlowEventKind:
        return FlowEventKind.ESCALATION


@dataclass(frozen=True)
class ComplianceFinding:
    """A compliance issue raised during the flow, with its disposition.

    `severity` must be one of blocker/amber/yellow; `disposition` must be
    one of open/routed/remediated/escalated/accepted_risk. `finding_kind`,
    `root_cause_owner`, and `materiality` are free-form strings (the
    finding-kind vocabulary is documented but not enforced here).
    """

    finding_kind: str
    root_cause_owner: str
    severity: str
    materiality: str
    evidence: tuple[str, ...] = ()
    disposition: str = "open"
    audit_ref: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(_VALID_SEVERITIES)}, "
                f"got {self.severity!r}"
            )
        if self.disposition not in _VALID_DISPOSITIONS:
            raise ValueError(
                f"disposition must be one of {sorted(_VALID_DISPOSITIONS)}, "
                f"got {self.disposition!r}"
            )
        # materiality + finding_kind are part of the schema vocabulary — enforce
        # them so a typo can't enter telemetry silently (codex impl review #6).
        if self.materiality not in _VALID_MATERIALITY:
            raise ValueError(
                f"materiality must be one of {sorted(_VALID_MATERIALITY)}, "
                f"got {self.materiality!r}"
            )
        if self.finding_kind not in _VALID_FINDING_KINDS:
            raise ValueError(
                f"finding_kind must be one of {sorted(_VALID_FINDING_KINDS)}, "
                f"got {self.finding_kind!r}"
            )
        if not isinstance(self.evidence, tuple):
            object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def kind(self) -> FlowEventKind:
        return FlowEventKind.COMPLIANCE_FINDING


def event_kind(event) -> FlowEventKind:
    """Return the `FlowEventKind` of any flow event.

    Lets a heterogeneous list of events be grouped/filtered by kind without
    isinstance ladders — every flow-event type exposes a `kind` property.
    """
    return event.kind
