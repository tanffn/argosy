"""Leumi read-only adapter (SDD §9.1, Phase 4).

Leumi has no customer API. All reads come from the existing Family
Finances Status TSV that the user already maintains; per-lot data is not
available because the TSV is position-aggregated.

`place_order` and `cancel_order` always return `manual_required`. The
adapter exists so the execution router can ask "what does the user hold
in Leumi?" without conditional logic on broker type.
"""

from __future__ import annotations

from pathlib import Path

from argosy.adapters.brokers.types import (
    CancellationResult,
    ExecutionResult,
    Lot,
    OpenOrder,
    Position,
    ProposedOrder,
)
from argosy.ingest.tsv import parse_portfolio_tsv


class LeumiTSVAdapter:
    """Read-only adapter wrapping the existing TSV ingestor."""

    name = "leumi_tsv"

    def __init__(
        self,
        *,
        user_id: str,
        tsv_path: str | Path | None = None,
        default_account_id: str = "leumi",
    ) -> None:
        self.user_id = user_id
        self.tsv_path = Path(tsv_path) if tsv_path else None
        self.default_account_id = default_account_id

    def _snapshot(self) -> object | None:
        if self.tsv_path is None or not self.tsv_path.is_file():
            return None
        return parse_portfolio_tsv(self.tsv_path)

    # --- Read -----------------------------------------------------------
    def get_positions(self, account_id: str) -> list[Position]:
        snap = self._snapshot()
        if snap is None:
            return []
        out: list[Position] = []
        for p in getattr(snap, "positions", []):
            location = (p.location or "").lower()
            # Filter to Leumi-located positions; Schwab rows live alongside
            # in the same TSV but belong to a different adapter.
            if "leumi" not in location and account_id != "all":
                continue
            symbol = (p.symbol or p.details or p.asset_type or "").strip()
            if not symbol:
                continue
            qty = p.shares if p.shares is not None else 0.0
            out.append(
                Position(
                    account_id=account_id,
                    ticker=symbol.upper(),
                    quantity=float(qty or 0.0),
                    avg_cost=p.avg_price,
                    market_value=(
                        (p.usd_value_k or 0.0) * 1000.0 if p.usd_value_k else None
                    ),
                    currency=p.currency or "NIS",
                    asset_class="other",
                )
            )
        return out

    def get_lots(self, account_id: str, ticker: str) -> list[Lot]:
        # Leumi TSV is position-aggregated; no per-lot data is available.
        return []

    def get_open_orders(self, account_id: str) -> list[OpenOrder]:
        # No customer API; cannot enumerate open orders.
        return []

    # --- Write (always manual per SDD §9.1) -----------------------------
    async def place_order(
        self, order: ProposedOrder, paper: bool = True
    ) -> ExecutionResult:
        return ExecutionResult(
            status="manual_required",
            broker=self.name,
            paper=paper,
            reason="Leumi has no customer API; place this order via the Leumi UI.",
        )

    async def cancel_order(self, order_id: str) -> CancellationResult:
        return CancellationResult(
            status="manual_required",
            broker=self.name,
            broker_order_id=order_id,
            reason="Leumi has no customer API; cancel via the Leumi UI.",
        )


__all__ = ["LeumiTSVAdapter"]
