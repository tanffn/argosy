"""Argosy broker adapters (Phase 4).

Three adapters live here, all conforming to the `BrokerAdapter` Protocol
(SDD §9.2):

  - `IBKRAdapter`        — full read+write via TWS Gateway (`ib_insync`)
  - `SchwabCSVAdapter`   — read-only via cost-basis CSV import
  - `LeumiTSVAdapter`    — read-only wrapping the existing TSV importer

Universal rule (SDD §9.2): every adapter honors `paper=True` by writing a
`PaperFill` audit row instead of placing a real order. The live and paper
code paths must stay symmetric so tests of one exercise the other.
"""

from argosy.adapters.brokers.base import BrokerAdapter
from argosy.adapters.brokers.types import (
    CancellationResult,
    ExecutionResult,
    Fill,
    Lot,
    OpenOrder,
    Position,
    ProposedOrder,
)

__all__ = [
    "BrokerAdapter",
    "CancellationResult",
    "ExecutionResult",
    "Fill",
    "Lot",
    "OpenOrder",
    "Position",
    "ProposedOrder",
]
