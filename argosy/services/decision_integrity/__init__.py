"""Data-integrity + provenance gates for decision runs (Stream A)."""

from argosy.services.decision_integrity.as_of import (
    LOAD_BEARING_FINANCIAL_FIELDS,
    MARKET_DATA_FIELDS,
    AsOfValue,
    attach_provenance_sidecar,
    format_as_of_label,
    format_field_for_prompt,
    stamp_fundamentals_payload,
    unwrap_as_of,
)
from argosy.services.decision_integrity.confidence_cap import (
    CONFIDENCE_RANK,
    apply_confidence_cap,
    min_confidence,
    normalize_confidence,
    observe_confidence_delta,
)
from argosy.services.decision_integrity.gates import (
    IntegrityGateResult,
    collect_facilitator_conditions,
    collect_remediation_requests_from_reports,
    evaluate_green_light_integrity,
)
from argosy.services.decision_integrity.overrides import (
    debate_action_contradicts_winning_side,
    record_confidence_delta,
    record_confidence_cap_override,
    record_debate_winner_override,
)
from argosy.services.decision_integrity.remediation_store import (
    auto_resolve_on_fresh_pass,
    clear_remediation,
    has_open_remediation,
    list_open_remediations,
    override_remediation,
    persist_remediation_requests,
    resolve_remediation,
)
from argosy.services.decision_integrity.vintage_gate import (
    VintageGateResult,
    evaluate_vintage_gate,
)

__all__ = [
    "AsOfValue",
    "CONFIDENCE_RANK",
    "IntegrityGateResult",
    "LOAD_BEARING_FINANCIAL_FIELDS",
    "MARKET_DATA_FIELDS",
    "VintageGateResult",
    "apply_confidence_cap",
    "attach_provenance_sidecar",
    "auto_resolve_on_fresh_pass",
    "clear_remediation",
    "collect_facilitator_conditions",
    "collect_remediation_requests_from_reports",
    "debate_action_contradicts_winning_side",
    "evaluate_green_light_integrity",
    "evaluate_vintage_gate",
    "format_as_of_label",
    "format_field_for_prompt",
    "has_open_remediation",
    "list_open_remediations",
    "min_confidence",
    "normalize_confidence",
    "observe_confidence_delta",
    "override_remediation",
    "persist_remediation_requests",
    "record_confidence_cap_override",
    "record_confidence_delta",
    "record_debate_winner_override",
    "resolve_remediation",
    "stamp_fundamentals_payload",
    "unwrap_as_of",
]
