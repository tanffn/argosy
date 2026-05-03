"""Shared pydantic models for broker adapters (SDD §9.2, Phase 4).

These models are the lingua franca of the brokerage layer. Every adapter
returns them; the execution router consumes them.

Numeric quantities use float for ergonomics. The DB persists them as
NUMERIC(18,4) for precision. Prefer `round(...)`/`Decimal` at boundaries
that touch money, not in transport models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# Read-side models
# ----------------------------------------------------------------------


class Position(BaseModel):
    """A current holding in a broker account.

    `quantity` is signed: positive long, negative short. `avg_cost` is in
    `currency` per share. `market_value` and `unrealized_pnl` are in the
    account's reporting currency (typically USD).
    """

    account_id: str
    ticker: str
    quantity: float
    avg_cost: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    currency: str = "USD"
    asset_class: Literal["stock", "etf", "option", "cash", "other"] = "stock"


class Lot(BaseModel):
    """One tax lot (one acquisition event)."""

    account_id: str
    ticker: str
    lot_id_external: str = ""  # broker's lot id, if it issues one
    quantity: float
    cost_basis_usd: float
    acquired_at: datetime | None = None
    source: str = ""  # "schwab_csv", "leumi_tsv", "ibkr_api", ...


class OpenOrder(BaseModel):
    """An order resting at the broker that has not fully filled."""

    account_id: str
    broker_order_id: str
    ticker: str
    action: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop", "stop-limit", "moc", "moo"] = "market"
    quantity: float
    filled_quantity: float = 0.0
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: Literal["DAY", "GTC", "IOC", "FOK"] = "DAY"
    status: str = "open"  # broker-specific; "open"/"partial"/"working"
    submitted_at: datetime | None = None


# ----------------------------------------------------------------------
# Write-side models
# ----------------------------------------------------------------------


class ProposedOrder(BaseModel):
    """An order ready for placement.

    Built from an APPROVED `Proposal`. `client_order_id` is the engine's
    UUID for idempotency (SDD §10.5); the adapter MUST pass it as the
    broker's client order id when the broker supports one.
    """

    account_id: str
    ticker: str
    action: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop", "stop-limit", "moc", "moo"] = "market"
    quantity: float
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: Literal["DAY", "GTC", "IOC", "FOK"] = "DAY"
    instrument: Literal["stock", "etf", "option"] = "stock"
    client_order_id: str = ""  # caller fills with uuid4().hex
    proposal_id: int | None = None
    user_id: str = ""


class Fill(BaseModel):
    """A single fill event (a partial or full execution)."""

    proposal_id: int | None = None
    broker: str
    broker_order_id: str
    ticker: str
    action: Literal["buy", "sell"]
    quantity: float
    price: float
    commission: float = 0.0
    filled_at: datetime = Field(default_factory=_utcnow)
    paper: bool = False


class ExecutionResult(BaseModel):
    """Outcome of a `BrokerAdapter.place_order()` call.

    `status` semantics:
      - "submitted"        — accepted by broker; awaiting fill
      - "filled"           — fully filled synchronously (rare; mostly paper)
      - "rejected"         — broker rejected pre-acceptance
      - "manual_required"  — adapter has no write API; user must place
                              the order via the broker's UI
      - "paper"            — paper-mode PaperFill written instead of live placement
    """

    status: Literal[
        "submitted",
        "filled",
        "rejected",
        "manual_required",
        "paper",
    ]
    broker: str
    broker_order_id: str = ""
    paper: bool = False
    fills: list[Fill] = Field(default_factory=list)
    reason: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class CancellationResult(BaseModel):
    """Outcome of a `BrokerAdapter.cancel_order()` call."""

    status: Literal["cancelled", "not_found", "rejected", "manual_required"]
    broker: str
    broker_order_id: str = ""
    reason: str = ""


__all__ = [
    "CancellationResult",
    "ExecutionResult",
    "Fill",
    "Lot",
    "OpenOrder",
    "Position",
    "ProposedOrder",
]
