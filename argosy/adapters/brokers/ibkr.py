"""IBKR adapter (SDD §9.1, Phase 4).

Wraps `ib_insync` over the TWS Gateway. Phase 4 implements the read
surface (positions, lots*, open orders), `place_order` for the standard
order types, `cancel_order`, and a connection lifecycle with
exponential-backoff reconnect.

* IBKR's `lots` notion is exposed via the FA / portfolio API; a simple
  Phase 4 implementation maps each open position to a single synthetic
  Lot (quantity = position quantity, cost_basis = quantity * avg_cost).
  Real per-lot tracking arrives when the user opts into the trading-acct
  per-lot reports in TWS.

Connection target: `localhost:7497` for paper, `localhost:7496` for live.
The actual gateway host/port is read from `ibkr_settings.yaml` per
account, falling back to those defaults.

Tests MUST mock `ib_insync.IB`. The module imports it lazily so test
runs without `ib_insync` installed don't fail at import time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import yaml

from argosy.adapters.brokers.types import (
    CancellationResult,
    ExecutionResult,
    Fill,
    Lot,
    OpenOrder,
    Position,
    ProposedOrder,
)
from argosy.config import get_settings
from argosy.execution.audit import record_audit_event, write_paper_fill
from argosy.logging import get_logger
from argosy.secrets import get_secret

_log = get_logger("argosy.adapters.brokers.ibkr")


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


@dataclass
class IBKRAccountConfig:
    """Per-account IBKR connection target."""

    account_id: str
    host: str = "localhost"
    paper_port: int = 7497
    live_port: int = 7496
    client_id: int = 1
    mode: str = "paper"  # "paper" or "live"


@dataclass
class IBKRSettings:
    accounts: dict[str, IBKRAccountConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, user_id: str) -> "IBKRSettings":
        """Load `configs/<user_id>/ibkr_settings.yaml` if present."""
        settings = get_settings()
        path = settings.configs_dir / user_id / "ibkr_settings.yaml"
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:  # pragma: no cover - defensive
            return cls()
        accts: dict[str, IBKRAccountConfig] = {}
        for acct_id, blob in (data.get("accounts") or {}).items():
            blob = blob or {}
            accts[acct_id] = IBKRAccountConfig(
                account_id=acct_id,
                host=blob.get("host", "localhost"),
                paper_port=int(blob.get("paper_port", 7497)),
                live_port=int(blob.get("live_port", 7496)),
                client_id=int(blob.get("client_id", 1)),
                mode=blob.get("mode", "paper"),
            )
        return cls(accounts=accts)

    def for_account(self, account_id: str) -> IBKRAccountConfig:
        if account_id in self.accounts:
            return self.accounts[account_id]
        return IBKRAccountConfig(account_id=account_id)


# ----------------------------------------------------------------------
# Connection lifecycle (lazy + reconnect with backoff)
# ----------------------------------------------------------------------


class IBKRConnectionError(RuntimeError):
    """Raised after retries exhausted; surfaces as a hard failure."""


def _import_ib_insync() -> Any:
    """Lazy import. Returns the `ib_insync` module or raises a friendly error.

    Tests inject a mock by setting `IBKRAdapter._ib_module_factory` to a
    callable returning a stub module.
    """
    try:
        import ib_insync  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only when missing
        raise IBKRConnectionError(
            "ib_insync is not installed. Install with `uv add ib_insync` "
            "or `pip install ib_insync`."
        ) from exc
    return ib_insync


# ----------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------


class IBKRAdapter:
    """IBKR adapter conforming to the BrokerAdapter Protocol."""

    name = "ibkr"

    # Tests override this to inject a mocked `ib_insync` module. When
    # None, the lazy importer pulls the real package.
    _ib_module_factory: Any = None

    # Reconnect policy (SDD §9.5).
    MAX_RETRIES = 5
    BACKOFF_INITIAL_SECONDS = 1.0
    BACKOFF_FACTOR = 2.0

    def __init__(
        self,
        *,
        user_id: str,
        settings: IBKRSettings | None = None,
    ) -> None:
        self.user_id = user_id
        self.settings = settings or IBKRSettings.load(user_id)
        self._ib: Any = None  # ib_insync.IB instance, lazy
        self._connected_to: tuple[str, int, int] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ib_module(self) -> Any:
        if self._ib_module_factory is not None:
            return self._ib_module_factory()
        return _import_ib_insync()

    def _ensure_client(self) -> Any:
        if self._ib is not None:
            return self._ib
        mod = self._ib_module()
        self._ib = mod.IB()
        return self._ib

    async def connect(self, account_id: str) -> Any:
        """Connect with exponential backoff. Returns the IB client."""
        cfg = self.settings.for_account(account_id)
        port = cfg.live_port if cfg.mode == "live" else cfg.paper_port
        target = (cfg.host, port, cfg.client_id)
        ib = self._ensure_client()

        if self._connected_to == target and getattr(ib, "isConnected", lambda: False)():
            return ib

        # Note IBKR username (read for audit; ib_insync uses TWS session not creds)
        _ = get_secret("argosy.ibkr.username")

        delay = self.BACKOFF_INITIAL_SECONDS
        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                connect = getattr(ib, "connectAsync", None)
                if connect is None:
                    # Some test mocks expose synchronous `connect`.
                    sync = getattr(ib, "connect")
                    sync(cfg.host, port, clientId=cfg.client_id)
                else:
                    await connect(cfg.host, port, clientId=cfg.client_id)
                self._connected_to = target
                _log.info(
                    "ibkr.connected",
                    host=cfg.host,
                    port=port,
                    client_id=cfg.client_id,
                    mode=cfg.mode,
                )
                return ib
            except Exception as exc:  # pragma: no cover - exercised via mocks
                last_exc = exc
                _log.warning(
                    "ibkr.connect_failed",
                    attempt=attempt,
                    delay=delay,
                    error=str(exc),
                )
                if attempt == self.MAX_RETRIES:
                    break
                await asyncio.sleep(delay)
                delay *= self.BACKOFF_FACTOR

        raise IBKRConnectionError(
            f"Failed to connect to IBKR at {cfg.host}:{port} after "
            f"{self.MAX_RETRIES} retries: {last_exc!r}"
        )

    async def disconnect(self) -> None:
        if self._ib is None:
            return
        try:
            disc = getattr(self._ib, "disconnect", None)
            if disc:
                disc()
        except Exception:  # pragma: no cover - defensive
            pass
        self._ib = None
        self._connected_to = None

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_positions(self, account_id: str) -> list[Position]:
        ib = self._sync_connect(account_id)
        positions_raw = ib.positions(account_id) if hasattr(ib, "positions") else []
        out: list[Position] = []
        for pos in positions_raw or []:
            contract = getattr(pos, "contract", None)
            ticker = getattr(contract, "symbol", "") if contract else ""
            qty = float(getattr(pos, "position", 0) or 0)
            avg = getattr(pos, "avgCost", None)
            out.append(
                Position(
                    account_id=account_id,
                    ticker=ticker,
                    quantity=qty,
                    avg_cost=float(avg) if avg is not None else None,
                    currency=getattr(contract, "currency", "USD") or "USD",
                    asset_class=_infer_asset_class(contract),
                )
            )
        return out

    def get_lots(self, account_id: str, ticker: str) -> list[Lot]:
        # IBKR exposes per-lot only via specific FA reports; map positions
        # to a synthetic lot for Phase 4. Real per-lot lands when the user
        # uploads the IBKR FA report.
        synth: list[Lot] = []
        for p in self.get_positions(account_id):
            if p.ticker.upper() != ticker.upper():
                continue
            cost = (p.avg_cost or 0.0) * p.quantity
            synth.append(
                Lot(
                    account_id=account_id,
                    ticker=p.ticker,
                    lot_id_external="ibkr-aggregate",
                    quantity=p.quantity,
                    cost_basis_usd=cost,
                    acquired_at=None,
                    source="ibkr_aggregate",
                )
            )
        return synth

    def get_open_orders(self, account_id: str) -> list[OpenOrder]:
        ib = self._sync_connect(account_id)
        orders_raw = (
            ib.openOrders() if hasattr(ib, "openOrders") else (
                ib.reqOpenOrders() if hasattr(ib, "reqOpenOrders") else []
            )
        )
        out: list[OpenOrder] = []
        for trade in orders_raw or []:
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None) or trade
            order_status = getattr(trade, "orderStatus", None)
            out.append(
                OpenOrder(
                    account_id=account_id,
                    broker_order_id=str(getattr(order, "orderId", "") or ""),
                    ticker=getattr(contract, "symbol", "") if contract else "",
                    action=_normalize_action(getattr(order, "action", "buy")),
                    order_type=_normalize_order_type(getattr(order, "orderType", "market")),
                    quantity=float(getattr(order, "totalQuantity", 0) or 0),
                    filled_quantity=float(
                        getattr(order_status, "filled", 0) if order_status else 0
                    ),
                    limit_price=getattr(order, "lmtPrice", None) or None,
                    stop_price=getattr(order, "auxPrice", None) or None,
                    time_in_force=_normalize_tif(getattr(order, "tif", "DAY")),
                    status=getattr(order_status, "status", "open") if order_status else "open",
                )
            )
        return out

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def place_order(
        self, order: ProposedOrder, paper: bool = True
    ) -> ExecutionResult:
        """Place (live) or PaperFill-log (paper) the order."""
        if paper:
            # Paper mode: write a PaperFill row + audit_log entry. Same
            # code shape as live except no broker call.
            paper_price = (
                order.limit_price
                if order.limit_price is not None
                else (order.stop_price if order.stop_price is not None else 0.0)
            )
            await write_paper_fill(
                user_id=order.user_id or self.user_id,
                broker=self.name,
                ticker=order.ticker,
                action=order.action,
                quantity=order.quantity,
                price=float(paper_price or 0.0),
                proposal_id=order.proposal_id,
                broker_order_id=order.client_order_id or uuid4().hex,
            )
            return ExecutionResult(
                status="paper",
                broker=self.name,
                paper=True,
                broker_order_id=order.client_order_id or "",
                reason="paper-mode placement; no broker call",
            )

        # Live mode.
        client_order_id = order.client_order_id or uuid4().hex
        ib = await self.connect(order.account_id)
        mod = self._ib_module()

        contract = self._make_contract(mod, order)
        ib_order = self._make_order(mod, order)
        ib_order.orderRef = client_order_id  # idempotency tag

        try:
            trade = ib.placeOrder(contract, ib_order)
        except Exception as exc:  # pragma: no cover - exercised via mocks
            await record_audit_event(
                user_id=order.user_id or self.user_id,
                event_type="order.place_failed",
                entity_type="proposal",
                entity_id=str(order.proposal_id) if order.proposal_id else "",
                payload={
                    "broker": self.name,
                    "client_order_id": client_order_id,
                    "error": str(exc),
                    "ticker": order.ticker,
                    "action": order.action,
                    "quantity": order.quantity,
                },
            )
            return ExecutionResult(
                status="rejected",
                broker=self.name,
                broker_order_id=client_order_id,
                reason=f"placeOrder raised: {exc}",
            )

        order_id = str(getattr(getattr(trade, "order", None), "orderId", "") or client_order_id)
        order_status = getattr(trade, "orderStatus", None)
        status = (getattr(order_status, "status", "submitted") if order_status else "submitted")

        await record_audit_event(
            user_id=order.user_id or self.user_id,
            event_type="order.placed",
            entity_type="proposal",
            entity_id=str(order.proposal_id) if order.proposal_id else "",
            payload={
                "broker": self.name,
                "broker_order_id": order_id,
                "client_order_id": client_order_id,
                "ticker": order.ticker,
                "action": order.action,
                "quantity": order.quantity,
                "order_type": order.order_type,
                "tif": order.time_in_force,
                "status": status,
            },
        )

        # Synchronous fills, if any (rare; mostly arrive via reconcile).
        fills: list[Fill] = []
        for raw_fill in getattr(trade, "fills", []) or []:
            execution = getattr(raw_fill, "execution", None)
            if execution is None:
                continue
            fills.append(
                Fill(
                    proposal_id=order.proposal_id,
                    broker=self.name,
                    broker_order_id=order_id,
                    ticker=order.ticker,
                    action=order.action,
                    quantity=float(getattr(execution, "shares", 0) or 0),
                    price=float(getattr(execution, "price", 0) or 0),
                    commission=float(
                        getattr(getattr(raw_fill, "commissionReport", None), "commission", 0)
                        or 0
                    ),
                )
            )

        return ExecutionResult(
            status="filled" if status.lower() == "filled" else "submitted",
            broker=self.name,
            broker_order_id=order_id,
            paper=False,
            fills=fills,
            reason=status,
        )

    async def cancel_order(self, order_id: str) -> CancellationResult:
        ib = self._ensure_client()
        if not getattr(ib, "isConnected", lambda: False)():
            return CancellationResult(
                status="rejected",
                broker=self.name,
                broker_order_id=order_id,
                reason="not connected to TWS Gateway",
            )
        try:
            cancel_fn = getattr(ib, "cancelOrder", None)
            if cancel_fn is None:
                return CancellationResult(
                    status="rejected",
                    broker=self.name,
                    broker_order_id=order_id,
                    reason="cancelOrder unavailable on client",
                )
            cancel_fn(order_id)
        except Exception as exc:  # pragma: no cover
            return CancellationResult(
                status="rejected",
                broker=self.name,
                broker_order_id=order_id,
                reason=str(exc),
            )
        return CancellationResult(
            status="cancelled",
            broker=self.name,
            broker_order_id=order_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sync_connect(self, account_id: str) -> Any:
        """Synchronous equivalent of `connect` for read-side methods."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.connect(account_id))
        # Already running; tests inject a connected mock so no real
        # connect should be needed. Return whatever client we have.
        return self._ensure_client()

    def _make_contract(self, mod: Any, order: ProposedOrder) -> Any:
        """Build an `ib_insync` contract from the proposed order."""
        if order.instrument == "stock" or order.instrument == "etf":
            stock_cls = getattr(mod, "Stock", None)
            if stock_cls is None:
                # Tests' mock module may simply forward symbols; fall back
                # to a plain object.
                return _SimpleContract(symbol=order.ticker, secType="STK")
            return stock_cls(order.ticker, "SMART", "USD")
        if order.instrument == "option":
            opt_cls = getattr(mod, "Option", None)
            if opt_cls is None:
                return _SimpleContract(symbol=order.ticker, secType="OPT")
            # Phase 4 doesn't trade options live; surface a friendly error.
            raise NotImplementedError(
                "Option live trading via IBKR is Phase 5+; supply a strike "
                "and expiry in a future call shape."
            )
        return _SimpleContract(symbol=order.ticker, secType="STK")

    def _make_order(self, mod: Any, order: ProposedOrder) -> Any:
        """Build an `ib_insync` order from the proposed order."""
        action = "BUY" if order.action == "buy" else "SELL"
        qty = float(order.quantity)
        tif = order.time_in_force
        ot = order.order_type

        if ot == "market":
            cls = getattr(mod, "MarketOrder", None)
            if cls is None:
                return _SimpleOrder(action=action, totalQuantity=qty, orderType="MKT", tif=tif)
            o = cls(action, qty)
            o.tif = tif
            return o
        if ot == "limit":
            cls = getattr(mod, "LimitOrder", None)
            if cls is None:
                return _SimpleOrder(
                    action=action, totalQuantity=qty, orderType="LMT", tif=tif,
                    lmtPrice=order.limit_price,
                )
            o = cls(action, qty, order.limit_price or 0.0)
            o.tif = tif
            return o
        if ot == "stop":
            cls = getattr(mod, "StopOrder", None)
            if cls is None:
                return _SimpleOrder(
                    action=action, totalQuantity=qty, orderType="STP", tif=tif,
                    auxPrice=order.stop_price,
                )
            o = cls(action, qty, order.stop_price or 0.0)
            o.tif = tif
            return o
        if ot == "stop-limit":
            cls = getattr(mod, "StopLimitOrder", None)
            if cls is None:
                return _SimpleOrder(
                    action=action, totalQuantity=qty, orderType="STP LMT", tif=tif,
                    lmtPrice=order.limit_price, auxPrice=order.stop_price,
                )
            o = cls(action, qty, order.limit_price or 0.0, order.stop_price or 0.0)
            o.tif = tif
            return o
        if ot == "moc":
            cls = getattr(mod, "MarketOnCloseOrder", None)
            if cls is None:
                return _SimpleOrder(
                    action=action, totalQuantity=qty, orderType="MOC", tif="DAY"
                )
            return cls(action, qty)
        if ot == "moo":
            # ib_insync uses MarketOrder with goodAfterTime hooks; for
            # simplicity surface a plain object.
            return _SimpleOrder(
                action=action, totalQuantity=qty, orderType="MOO", tif="DAY"
            )

        # Fallback: market.
        return _SimpleOrder(action=action, totalQuantity=qty, orderType="MKT", tif=tif)


# ----------------------------------------------------------------------
# Tiny placeholder objects used when the test mock doesn't expose the
# canonical ib_insync constructors. These mimic the relevant attribute
# surface so tests can assert on it.
# ----------------------------------------------------------------------


class _SimpleContract:
    def __init__(self, *, symbol: str, secType: str = "STK") -> None:
        self.symbol = symbol
        self.secType = secType
        self.currency = "USD"
        self.exchange = "SMART"

    def __repr__(self) -> str:  # pragma: no cover
        return f"_SimpleContract(symbol={self.symbol!r}, secType={self.secType!r})"


class _SimpleOrder:
    def __init__(
        self,
        *,
        action: str,
        totalQuantity: float,
        orderType: str,
        tif: str = "DAY",
        lmtPrice: float | None = None,
        auxPrice: float | None = None,
    ) -> None:
        self.action = action
        self.totalQuantity = totalQuantity
        self.orderType = orderType
        self.tif = tif
        self.lmtPrice = lmtPrice
        self.auxPrice = auxPrice
        self.orderRef = ""

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"_SimpleOrder(action={self.action}, qty={self.totalQuantity}, "
            f"type={self.orderType}, tif={self.tif})"
        )


def _infer_asset_class(contract: Any) -> str:
    sec = (getattr(contract, "secType", "") or "").upper()
    return {"STK": "stock", "ETF": "etf", "OPT": "option"}.get(sec, "other")


def _normalize_action(s: str) -> str:
    return "buy" if (s or "").lower() in ("buy", "b") else "sell"


def _normalize_order_type(s: str) -> str:
    s = (s or "").lower()
    mapping = {
        "mkt": "market",
        "market": "market",
        "lmt": "limit",
        "limit": "limit",
        "stp": "stop",
        "stop": "stop",
        "stp lmt": "stop-limit",
        "stop-limit": "stop-limit",
        "moc": "moc",
        "moo": "moo",
    }
    return mapping.get(s, "market")


def _normalize_tif(s: str) -> str:
    s = (s or "").upper()
    return s if s in ("DAY", "GTC", "IOC", "FOK") else "DAY"


__all__ = [
    "IBKRAccountConfig",
    "IBKRAdapter",
    "IBKRConnectionError",
    "IBKRSettings",
]
