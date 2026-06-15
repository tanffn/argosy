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
    # S18 — a symbol-level technical reading cited in the prose (e.g. "RSI
    # 73.4") must match the run's TechnicalAnalyst payload. Blocks the
    # stale-carry-forward fabrication the fund manager rejected (RSI 73.4
    # carried six versions while the live payload read 56.05).
    TECHNICAL_CITATION = "technical_citation"
    # S21 — the IPS/medium-horizon allocatable sleeve targets (unit
    # pct_of_portfolio) MUST sum to ~100%. Catches both the implicit-core
    # under-allocation (sleeves sum to 51%, FM-rejected draft 38) and the
    # redundant-descriptor over-allocation (a phase/floor roll-up emitted as a
    # pct_of_portfolio target double-counts → 108%, FM-missed draft 39). The
    # IPS allocation is a mechanical 100% partition; do not leave it to an LLM
    # reviewer to eyeball.
    IPS_ALLOCATION_SUM = "ips_allocation_sum"
    # S22 — the same concept (net worth, NVDA weight, FI margin, estate) must
    # carry the SAME value across every surface the user reads (body, dashboard,
    # appendices), or carry explicitly distinct labels. Catches the cross-surface
    # contradiction class (FI reached-vs-not; body 62.5% vs dashboard 56.9%) that
    # no per-surface agent owns. Deterministic — coherence is a property of the
    # whole, not eyeballed by an LLM reviewer.
    CROSS_SURFACE_COHERENCE = "cross_surface_coherence"
    # Task 4 — the compositional sufficiency check. A plan that asserts "FI
    # reached" / "capital sufficiency reached" must be robust to its OWN stated
    # NVDA concentration tail: if marking NVDA down by the plan's tail shock
    # (−30%) drops net worth below the perpetuity base, the unqualified
    # "reached" claim is false. Composes the synthesizer's sufficiency claim
    # with the risk officer's concentration tail — no single agent owns it.
    FI_SHOCK_SUFFICIENCY = "fi_shock_sufficiency"


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
