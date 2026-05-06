"""Public types for the plan_synthesis package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Trigger = Literal["scheduled", "check_in", "quarterly", "annual"]


class NoBaselineError(Exception):
    """Raised when a user has no active baseline plan."""


@dataclass
class SynthesisResult:
    decision_run_id: int
    draft_id: int
