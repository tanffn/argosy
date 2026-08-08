"""Confidence delta observation — record only; never mutate agent judgment.

Blocker 6 / doctrine: confidence is agent judgment, not a conservation
invariant. Determinism may *observe* that a downstream band exceeds the
min of its inputs and persist that as an auditable ``confidence_delta``
row. It must NOT overwrite the agent's emitted confidence.
"""

from __future__ import annotations

from typing import Iterable

from argosy.agents.base import ConfidenceBand

CONFIDENCE_RANK: dict[str, int] = {
    "LOW": 0,
    "MED": 1,
    "MEDIUM": 1,
    "HIGH": 2,
}

_RANK_TO_BAND: dict[int, ConfidenceBand] = {
    0: ConfidenceBand.LOW,
    1: ConfidenceBand.MEDIUM,
    2: ConfidenceBand.HIGH,
}


def normalize_confidence(raw: str | ConfidenceBand | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, ConfidenceBand):
        return raw.value
    s = str(raw).strip().upper()
    if s == "MED":
        return ConfidenceBand.MEDIUM.value
    if s in CONFIDENCE_RANK:
        return (
            ConfidenceBand.MEDIUM.value
            if s == "MEDIUM"
            else (ConfidenceBand.HIGH.value if s == "HIGH" else ConfidenceBand.LOW.value)
        )
    return None


def min_confidence(values: Iterable[str | ConfidenceBand | None]) -> ConfidenceBand | None:
    ranks: list[int] = []
    for v in values:
        norm = normalize_confidence(v)
        if norm is None:
            continue
        ranks.append(CONFIDENCE_RANK[norm])
    if not ranks:
        return None
    return _RANK_TO_BAND[min(ranks)]


def observe_confidence_delta(
    emitted: str | ConfidenceBand | None,
    input_confidences: Iterable[str | ConfidenceBand | None],
) -> tuple[ConfidenceBand | None, bool, ConfidenceBand | None]:
    """Observe whether ``emitted`` exceeds ``min(inputs)``.

    Returns ``(emitted_band, rose_above_floor, floor)``.
    Never returns a replaced band — the emitted value is preserved.
    """
    emitted_norm = normalize_confidence(emitted)
    floor = min_confidence(input_confidences)
    if emitted_norm is None:
        return None, False, floor
    emitted_band = ConfidenceBand(emitted_norm)
    if floor is None:
        return emitted_band, False, None
    rose = CONFIDENCE_RANK[emitted_norm] > CONFIDENCE_RANK[floor.value]
    return emitted_band, rose, floor


def apply_confidence_cap(
    emitted: str | ConfidenceBand | None,
    input_confidences: Iterable[str | ConfidenceBand | None],
) -> tuple[ConfidenceBand | None, bool, ConfidenceBand | None]:
    """DEPRECATED alias for ``observe_confidence_delta`` (no mutation)."""
    return observe_confidence_delta(emitted, input_confidences)


__all__ = [
    "CONFIDENCE_RANK",
    "apply_confidence_cap",
    "min_confidence",
    "normalize_confidence",
    "observe_confidence_delta",
]
