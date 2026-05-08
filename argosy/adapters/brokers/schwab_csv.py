"""Schwab read-only adapter (SDD §9.1, Phase 4).

Schwab's API approval is OPEN-1 from the SDD; for v1 we ingest the
downloadable cost-basis CSV. Replaces the Phase 1 stub
`argosy.ingest.cost_basis.SchwabCostBasisImporter`.

Expected CSV columns (verified against a typical Schwab cost-basis
export; the actual user-attached export may need format tuning):

    Symbol, Quantity, Open Date, Cost/Share, Cost Basis, Account
    AAPL,   100,      2024-01-15, 175.20,    17520.00,    1234-5678
    NVDA,   50,       2023-06-30, 428.55,    21427.50,    1234-5678

Aliases handled: "Acquired" (= Open Date), "Cost Per Share" (= Cost/Share),
"Total Cost" (= Cost Basis), "Account Number" (= Account). Unknown
columns are ignored. Dollar signs and commas are stripped.

`place_order` is hard-wired to `manual_required` because we have no
write API. `cancel_order` likewise. Read methods return whatever was
imported into the `lots` table.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from argosy.adapters.brokers.types import (
    CancellationResult,
    ExecutionResult,
    Lot as LotModel,
    OpenOrder,
    Position,
    ProposedOrder,
)
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import Lot as LotRow, User

_log = get_logger("argosy.adapters.brokers.schwab_csv")


# ----------------------------------------------------------------------
# Header alias map. Lowered, stripped, non-alnum → ''.
# ----------------------------------------------------------------------

_HEADER_ALIASES: dict[str, str] = {
    "symbol": "symbol",
    "ticker": "symbol",
    "quantity": "quantity",
    "shares": "quantity",
    "qty": "quantity",
    "opendate": "acquired_at",
    "acquired": "acquired_at",
    "acquisitiondate": "acquired_at",
    "purchasedate": "acquired_at",
    "purchased": "acquired_at",
    "costshare": "cost_per_share",
    "costpershare": "cost_per_share",
    "pricepershare": "cost_per_share",
    "costbasis": "cost_basis_total",
    "totalcost": "cost_basis_total",
    "totalcostbasis": "cost_basis_total",
    "account": "account_id",
    "accountnumber": "account_id",
    "lotid": "lot_id_external",
    "id": "lot_id_external",
}


def _norm_header(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _strip_money(s: str) -> str:
    return (s or "").replace("$", "").replace(",", "").replace("\xa0", "").strip()


def _to_float(s: str) -> float | None:
    t = _strip_money(s)
    if not t or t in {"-", "—"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _parse_date(s: str) -> datetime | None:
    t = (s or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------
# Parsed-row dataclass (pre-DB)
# ----------------------------------------------------------------------


@dataclass
class _ParsedLot:
    symbol: str
    quantity: float
    cost_basis_total: float
    cost_per_share: float | None
    acquired_at: datetime | None
    account_id: str
    lot_id_external: str


def _parse_csv(path: Path) -> list[_ParsedLot]:
    """Parse the CSV and return lot rows. Raises on missing required columns."""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return []

    header = rows[0]
    norm = [_HEADER_ALIASES.get(_norm_header(h), "") for h in header]

    # Required: symbol + quantity + (cost_basis_total OR cost_per_share)
    if "symbol" not in norm or "quantity" not in norm:
        raise ValueError(
            f"Schwab CSV at {path} missing required columns: need at least "
            f"Symbol and Quantity (got header {header})"
        )

    out: list[_ParsedLot] = []
    for line_no, raw in enumerate(rows[1:], start=2):
        d: dict[str, str] = {}
        for col_name, value in zip(norm, raw):
            if col_name and col_name not in d:
                d[col_name] = value
        symbol = (d.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        qty = _to_float(d.get("quantity", "")) or 0.0
        if qty == 0:
            continue
        cps = _to_float(d.get("cost_per_share", ""))
        cbt = _to_float(d.get("cost_basis_total", ""))
        if cbt is None and cps is not None:
            cbt = cps * qty
        if cbt is None:
            cbt = 0.0
        out.append(
            _ParsedLot(
                symbol=symbol,
                quantity=qty,
                cost_basis_total=cbt,
                cost_per_share=cps,
                acquired_at=_parse_date(d.get("acquired_at", "")),
                account_id=(d.get("account_id") or "").strip(),
                lot_id_external=(d.get("lot_id_external") or "").strip()
                or f"{symbol}-{line_no}",
            )
        )
    return out


# ----------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------


class SchwabCSVAdapter:
    """Read-only Schwab adapter backed by a cost-basis CSV import."""

    name = "schwab_csv"

    def __init__(self, *, user_id: str, default_account_id: str = "schwab") -> None:
        self.user_id = user_id
        self.default_account_id = default_account_id

    # --- Read -----------------------------------------------------------
    def get_positions(self, account_id: str) -> list[Position]:
        """Aggregate `lots` into positions per ticker."""
        # Sync wrapper around an async DB call. Caller is sync per Protocol.

        async def _go() -> list[Position]:
            async with db_mod.get_session() as session:
                stmt = select(LotRow).where(
                    LotRow.user_id == self.user_id,
                    LotRow.account_id == account_id,
                )
                rows = (await session.execute(stmt)).scalars().all()
            agg: dict[str, dict[str, float]] = {}
            for r in rows:
                t = r.ticker.upper()
                a = agg.setdefault(t, {"qty": 0.0, "cost": 0.0})
                a["qty"] += float(r.quantity)
                a["cost"] += float(r.cost_basis_usd)
            out: list[Position] = []
            for ticker, a in agg.items():
                qty = a["qty"]
                avg = (a["cost"] / qty) if qty else None
                out.append(
                    Position(
                        account_id=account_id,
                        ticker=ticker,
                        quantity=qty,
                        avg_cost=avg,
                        currency="USD",
                    )
                )
            return out

        return _run(_go())

    def get_lots(self, account_id: str, ticker: str) -> list[LotModel]:
        async def _go() -> list[LotModel]:
            async with db_mod.get_session() as session:
                stmt = select(LotRow).where(
                    LotRow.user_id == self.user_id,
                    LotRow.account_id == account_id,
                    LotRow.ticker == ticker.upper(),
                )
                rows = (await session.execute(stmt)).scalars().all()
            return [
                LotModel(
                    account_id=r.account_id,
                    ticker=r.ticker,
                    lot_id_external=r.lot_id_external,
                    quantity=float(r.quantity),
                    cost_basis_usd=float(r.cost_basis_usd),
                    acquired_at=r.acquired_at,
                    source=r.source,
                )
                for r in rows
            ]

        return _run(_go())

    def get_open_orders(self, account_id: str) -> list[OpenOrder]:
        # Schwab CSV is a snapshot; no concept of open orders.
        return []

    # --- Write (always manual) ------------------------------------------
    async def place_order(
        self, order: ProposedOrder, paper: bool = True
    ) -> ExecutionResult:
        return ExecutionResult(
            status="manual_required",
            broker=self.name,
            paper=paper,
            reason=(
                "Schwab API approval is OPEN-1 (deferred). Place this order "
                "manually via the Schwab UI."
            ),
        )

    async def cancel_order(self, order_id: str) -> CancellationResult:
        return CancellationResult(
            status="manual_required",
            broker=self.name,
            broker_order_id=order_id,
            reason="Schwab adapter is read-only; cancel via the Schwab UI.",
        )

    # --- Import path ----------------------------------------------------
    async def import_cost_basis_csv(
        self,
        path: Path,
        *,
        account_id: str | None = None,
    ) -> int:
        """Parse the CSV and persist its rows to `lots`. Returns row count.

        Idempotency: this implementation is *additive* — it appends new
        rows on every call. The user is expected to clear prior imports
        for an account before re-importing (CLI helper or manual SQL).
        Phase 4 keeps it simple to avoid silently merging changing
        cost-basis data; Phase 5+ may add a deduper keyed on
        `lot_id_external`.

        Provenance Wave A: the raw CSV bytes are recorded in the catalog
        (kind='broker_csv', source='cost_basis_import') before parsing so
        the user can later answer "which CSV produced these lots?". Best-
        effort — catalog failure does not block the import.
        """
        try:
            from argosy.services.file_catalog import catalog_upload as _catalog_upload
            raw = Path(path).read_bytes()
            await _catalog_upload(
                user_id=self.user_id,
                raw_bytes=raw,
                original_name=Path(path).name,
                mime_type="text/csv",
                kind="broker_csv",
                source="cost_basis_import",
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal value-add
            _log.warning(
                "schwab_csv.catalog_failed",
                path=str(path), user_id=self.user_id, error=str(exc),
            )

        parsed = _parse_csv(Path(path))
        if not parsed:
            _log.warning("schwab_csv.empty", path=str(path))
            return 0

        async with db_mod.get_session() as session:
            # Ensure user row exists for FK integrity.
            existing_user = (
                await session.execute(select(User).where(User.id == self.user_id))
            ).scalar_one_or_none()
            if existing_user is None:
                session.add(User(id=self.user_id))
                await session.flush()

            for p in parsed:
                acct = account_id or p.account_id or self.default_account_id
                session.add(
                    LotRow(
                        user_id=self.user_id,
                        account_id=acct,
                        ticker=p.symbol,
                        lot_id_external=p.lot_id_external,
                        quantity=p.quantity,
                        cost_basis_usd=p.cost_basis_total,
                        acquired_at=p.acquired_at,
                        source="schwab_csv",
                    )
                )
            await session.commit()
        return len(parsed)


def _run(coro: Any) -> Any:
    """Synchronously drive an async coroutine.

    Used by the sync read methods (the Protocol matches the SDD signature
    where reads are synchronous). If we're already inside a running event
    loop this raises — the engine code paths that need read-while-async
    should call the async helpers in `argosy.execution.audit` directly.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # We're already in a loop. Most call sites in tests/CLI are sync, so
    # this branch is rare. Run on a fresh thread to avoid nested-loop
    # errors. Phase 4 keeps it simple; Phase 5 swaps to an async-native
    # accessor if needed.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as exe:
        fut = exe.submit(asyncio.run, coro)
        return fut.result()


__all__ = ["SchwabCSVAdapter"]
