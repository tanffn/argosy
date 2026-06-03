"""Public types for the plan_synthesis package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Trigger = Literal["scheduled", "check_in", "quarterly", "annual"]


class NoBaselineError(Exception):
    """Raised when a user has no active baseline plan."""


class IncompleteFleetError(Exception):
    """Raised when one or more CRITICAL phase-1 analysts failed to produce a
    valid report — the run-completeness gate.

    A critical agent's output is a load-bearing derivation for the plan's
    headline decisions (FI target / retirement age, NVDA concentration glide
    path, spend basis, tax, RSU income). If such an agent fails, the run is
    ABORTED after phase 1 — we never build/promote a plan on missing critical
    data, and the synthesizer never gets the chance to fabricate the missing
    number. Non-critical agents degrade-with-disclosure instead (see the tier
    registry in the orchestrator).
    """

    def __init__(self, missing_critical: list[str]):
        self.missing_critical = sorted(missing_critical)
        super().__init__(
            "synthesis aborted (run-completeness gate): critical agents "
            f"failed to produce a valid report: {', '.join(self.missing_critical)}. "
            "Fix the agent(s) and re-run; the current plan is left untouched."
        )


@dataclass
class SynthesisResult:
    decision_run_id: int
    draft_id: int
