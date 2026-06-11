"""Internal types for the plan output gate.

These are dataclasses, not Pydantic models — the gate is internal
infrastructure that produces structured violations for CI / UI;
nothing here is persisted or serialized to JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GateCheck(str, Enum):
    """The canonical output-gate checks."""

    HISTORY_LEAK = "history_leak"
    JARGON_LEAK = "jargon_leak"
    SECTION_COVERAGE = "section_coverage"
    EVIDENCE_PER_SECTION = "evidence_per_section"
    DISTILLATE_SECTION_BINDING = "distillate_section_binding"
    # #24 — every user-facing headline number must trace to a RESOLVED
    # value from the deterministic resolver, or be rendered
    # "[derivation pending]". Kills the synth-fabricated-number reject.
    HEADLINE_NUMERIC_SOURCE = "headline_numeric_source"
    # S18 — the canonical instruments must not add US-situs estate exposure for
    # a non-US-person (the missing check behind the US-domiciled-ETF ship). RED =
    # a non-sanctioned US-domiciled primary; blocks promotion. Runs on the
    # STRUCTURED TargetAllocationDoc, the one artifact that commits to tickers.
    INSTRUMENT_DOMICILE = "instrument_domicile"


@dataclass(frozen=True)
class GateViolation:
    """A single check failure.

    Attributes:
        check: which check produced this violation.
        detail: human-readable explanation, e.g. "regex `\\bprior\\s+draft\\b`
            matched at position 412".
        locator: optional structured pointer (horizon name, section_id,
            character offset, etc.) — useful for UI surfacing but not
            required.
    """

    check: GateCheck
    detail: str
    locator: str | None = None


@dataclass
class GateVerdict:
    """Aggregate result across all five checks.

    `violations` is grouped by check kind. `passes` returns True only
    when every list is empty. Callers should not mutate this directly
    after construction — use `add` from inside the gate module.
    """

    violations: dict[GateCheck, list[GateViolation]] = field(
        default_factory=lambda: {c: [] for c in GateCheck}
    )

    @property
    def passes(self) -> bool:
        return all(not v for v in self.violations.values())

    @property
    def total_violations(self) -> int:
        return sum(len(v) for v in self.violations.values())

    def add(self, violation: GateViolation) -> None:
        self.violations[violation.check].append(violation)

    def extend(self, violations: list[GateViolation]) -> None:
        for v in violations:
            self.add(v)

    def for_check(self, check: GateCheck) -> list[GateViolation]:
        return list(self.violations[check])

    def summary(self) -> str:
        """One-line summary for logs and CI output."""
        if self.passes:
            return f"GATE PASS — all {len(GateCheck)} checks clean."
        bits = []
        for check in GateCheck:
            n = len(self.violations[check])
            if n:
                bits.append(f"{check.value}={n}")
        return f"GATE FAIL — {', '.join(bits)}"
