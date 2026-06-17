# argosy/quality/plan_model.py
"""Canonical typed plan-decision object.

INPUTS (facts only the user/brokerage can supply) and DERIVED (targets/rates the team
computes) are distinct. A DERIVED value may NEVER be seeded from an INPUT or a prior-doc
"target" — construction rejects it. This is the structural fix for the class of error
where the NVDA cadence ``3,000 sh/yr`` (a tracking-cell copied from Ariel's spreadsheet)
was laundered, via a ``plan_doc`` citation, into the plan's prescription.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class InheritedTargetError(ValueError):
    """Raised when a derived value is seeded from past behavior / a prior-doc target
    instead of being computed from inputs."""


# Provenance sources that are NEVER valid for a derived value — a derived target must be
# computed from inputs, not copied from a prior plan/spreadsheet/past behavior.
FORBIDDEN_DERIVED_SOURCES = frozenset(
    {"plan_doc", "prior_target", "spreadsheet", "past_behavior", "prior_plan"}
)


@dataclass(frozen=True)
class Input:
    """A fact about the current state / goal / constraint — the user's to supply."""

    key: str
    value: Any
    source: str          # e.g. "ffs_export", "resolver", "broker_csv", "goal"
    as_of: str = ""
    unit: str = ""
    kind: str = "input"


@dataclass(frozen=True)
class Derived:
    """A team-computed target/rate. MUST carry a formula + the inputs it consumed; may
    NOT be seeded from a forbidden source."""

    key: str
    value: Any
    formula: str                       # human-readable derivation
    inputs_used: tuple[str, ...]       # keys of Inputs/Derived consumed
    unit: str = ""
    seeded_from: str = ""              # if set and forbidden -> reject
    kind: str = "derived"

    def __post_init__(self) -> None:
        if self.seeded_from in FORBIDDEN_DERIVED_SOURCES:
            raise InheritedTargetError(
                f"derived {self.key!r} seeded from forbidden source "
                f"{self.seeded_from!r} — derive it from inputs, don't inherit"
            )
        if not self.formula or not self.inputs_used:
            raise InheritedTargetError(
                f"derived {self.key!r} must carry a formula + inputs_used "
                "(no orphan/inherited numbers)"
            )


@dataclass
class PlanDecisionModel:
    """Inputs + derived values; every surface renders FROM this object."""

    inputs: dict[str, Input] = field(default_factory=dict)
    derived: dict[str, Derived] = field(default_factory=dict)

    def add_input(self, inp: Input) -> None:
        self.inputs[inp.key] = inp

    def add_derived(self, d: Derived) -> None:
        for k in d.inputs_used:
            if k not in self.inputs and k not in self.derived:
                raise InheritedTargetError(
                    f"derived {d.key!r} cites unknown input/derived {k!r}"
                )
        self.derived[d.key] = d

    def to_dict(self) -> dict:
        return {
            "inputs": {k: vars(v) for k, v in self.inputs.items()},
            "derived": {k: vars(v) for k, v in self.derived.items()},
        }
