"""`BrokerAdapter` Protocol — the SDD §9.2 abstraction.

Every Argosy broker integration conforms to this Protocol. The execution
router consumes adapters via this interface only; tests verify each
adapter's conformance in `tests/test_broker_protocol.py`.

Universal rule: when called with `paper=True`, an adapter MUST NOT issue
any external broker call. It writes a `PaperFill`-shaped audit_log row
via `argosy.execution.audit.write_paper_fill` and returns an
`ExecutionResult(status="paper", paper=True, ...)`. The `paper` code path
must mirror the live one structurally.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from argosy.adapters.brokers.types import (
    CancellationResult,
    ExecutionResult,
    Lot,
    OpenOrder,
    Position,
    ProposedOrder,
)


@runtime_checkable
class BrokerAdapter(Protocol):
    """Common broker adapter interface (SDD §9.2)."""

    name: str  # short id: "ibkr", "schwab_csv", "leumi_tsv"

    def get_positions(self, account_id: str) -> list[Position]:
        ...

    def get_lots(self, account_id: str, ticker: str) -> list[Lot]:
        ...

    async def place_order(
        self, order: ProposedOrder, paper: bool = True
    ) -> ExecutionResult:
        ...

    async def cancel_order(self, order_id: str) -> CancellationResult:
        ...

    def get_open_orders(self, account_id: str) -> list[OpenOrder]:
        ...


__all__ = ["BrokerAdapter"]
