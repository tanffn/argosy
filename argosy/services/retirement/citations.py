"""ValueWithRationale — the single shape every retirement-related value
passes through on its way from the backend to the UI.

Every chart, table, and tooltip in the retirement companion needs to surface
(a) the value itself, (b) WHY this value, (c) the source. Without a uniform
wrapper, half the UI ends up with hover tooltips and half doesn't.

The serializer ``as_dict()`` strips ``None``-valued ``freshness_warning`` and
empty ``alternatives_considered`` so JSON payloads stay compact and the UI's
"show warning if present" logic is uniform. ``value`` and ``source_id`` are
preserved as ``None`` because both are semantic: ``value=None`` means "not
enough data"; ``source_id=None`` means derived/computed.
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DERIVED = None
"""Explicit marker for ``source_id`` when a value is computed, not sourced."""


@dataclass
class ValueWithRationale:
    """Wraps one user-facing value with provenance + rationale.

    Always serialize via :func:`as_dict` so optional fields with default
    values are stripped — JSON stays small and the UI logic stays uniform.
    """

    value: float | int | str | None
    unit: str
    source_id: str | None
    rationale: str
    alternatives_considered: list[str] = field(default_factory=list)
    as_of_date: str | None = None
    freshness_warning: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"

    def __post_init__(self) -> None:
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(
                f"confidence must be one of high/medium/low; got {self.confidence!r}"
            )


def as_dict(v: ValueWithRationale) -> dict[str, Any]:
    """Compact JSON-friendly serialization.

    Drops keys whose values are ``None`` or empty list, EXCEPT ``value`` and
    ``source_id`` which are kept even when None (those Nones are semantic;
    see module docstring).
    """
    d = asdict(v)
    out: dict[str, Any] = {}
    for k, val in d.items():
        if k in ("value", "source_id"):
            out[k] = val
            continue
        if val is None or val == []:
            continue
        out[k] = val
    return out
