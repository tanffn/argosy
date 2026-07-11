"""Shared contract for early-signal stream adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SignalNomination:
    ticker: str
    stream: str
    direction: str
    strength: float
    as_of: date
    evidence: dict[str, Any]
    dedup_key: str
    route_to_funnel: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("ticker must be non-empty")
        if not isinstance(self.stream, str) or not self.stream.strip():
            raise ValueError("stream must be non-empty")
        if self.direction not in {"long", "short"}:
            raise ValueError("direction must be 'long' or 'short'")
        if not isinstance(self.strength, (int, float)) or not 0 <= self.strength <= 1:
            raise ValueError("strength must be in [0, 1]")
        if not isinstance(self.as_of, date):
            raise TypeError("as_of must be a date")
        if not isinstance(self.evidence, dict):
            raise TypeError("evidence must be a dict")
        if not isinstance(self.dedup_key, str) or not self.dedup_key.strip():
            raise ValueError("dedup_key must be non-empty")
        if not isinstance(self.route_to_funnel, bool):
            raise TypeError("route_to_funnel must be a bool")


@runtime_checkable
class SignalStream(Protocol):
    def fetch(
        self, session: Any, *, since: date
    ) -> list[SignalNomination]: ...


__all__ = ["SignalNomination", "SignalStream"]
