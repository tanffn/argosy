"""Plan output quality gate.

Five checks that enforce the contract laid out in
docs/plans/argosy-comprehensive-plan-integration.md:

1. history_leak              — no prior-version narration in user prose
2. jargon_leak               — no internal agent / class / status names
3. section_coverage          — canonical 18-section IDs present per threshold
4. evidence_per_section      — facts + citations + assumptions per section
5. distillate_section_binding — non-empty distillate fields are actually used

The gate runs as a regression artifact: it must fail RED on the
persisted v20 fixture (Phase 0 ships the failure, Phases 1-4 land the
fixes that flip each check to GREEN in turn).
"""
from __future__ import annotations

from argosy.quality.gate_types import (
    GateCheck,
    GateVerdict,
    GateViolation,
)
from argosy.quality.numeric_source_gate import check_headline_numeric_source
from argosy.quality.plan_output_gate import (
    check_distillate_section_binding,
    check_evidence_per_section,
    check_history_leak,
    check_jargon_leak,
    check_section_coverage,
    gate_plan_output,
)

__all__ = [
    "GateCheck",
    "GateVerdict",
    "GateViolation",
    "check_distillate_section_binding",
    "check_evidence_per_section",
    "check_headline_numeric_source",
    "check_history_leak",
    "check_jargon_leak",
    "check_section_coverage",
    "gate_plan_output",
]
