"""ArgonautAccount: the Phase 5 limited-account wrapper.

Reads `agent_settings.limited_account` and (optionally) live position
data from `IBKRAdapter`. Provides:

  - `get_value_usd()`        — total account value (configured size +
                                positions delta if live data supplied)
  - `get_open_positions()`   — IBKR positions filtered by `account_id`
  - `is_autonomy_enabled()`  — True iff execution_mode is paper or live
                                AND `ARGOSY_KILL` is not set
  - `current_execution_mode()` — convenience accessor
  - `persist_daily_snapshot()` — writes one `argonaut_snapshots` row

The IBKRAdapter is injected so tests can pass a mock without touching
`ib_insync`. When no adapter is provided, the account works purely off
configuration values (size_usd, no live positions). This is the default
for Phase 5 paper-mode operation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date as _date_cls, datetime, timezone
from typing import Any, Iterable, Literal

from argosy.adapters.brokers.types import Position
from argosy.agent_settings import AgentSettings, load_agent_settings
from argosy.logging import get_logger


_log = get_logger("argosy.accounts.argonaut")


@dataclass
class ArgonautSnapshotPayload:
    """In-memory snapshot record. Mirrors `ArgonautSnapshot` ORM row."""

    user_id: str
    account_id: str
    date: str  # YYYY-MM-DD
    total_value_usd: float
    cash_usd: float
    positions_value_usd: float
    day_pnl_usd: float
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ArgonautAccount:
    """Phase 5 wrapper around the limited account configuration."""

    def __init__(
        self,
        *,
        user_id: str = "ariel",
        settings: AgentSettings | None = None,
        adapter: Any | None = None,  # IBKRAdapter (or mock)
    ) -> None:
        self.user_id = user_id
        self.settings = settings or load_agent_settings(user_id)
        self.adapter = adapter

    # ------------------------------------------------------------------
    # Configuration accessors
    # ------------------------------------------------------------------

    @property
    def account_id(self) -> str:
        return self.settings.limited_account.account_id or "argonaut"

    @property
    def configured_size_usd(self) -> float:
        return float(self.settings.limited_account.size_usd)

    @property
    def per_decision_max_pct(self) -> float:
        return float(self.settings.limited_account.per_decision_max_pct)

    @property
    def daily_loss_limit_pct(self) -> float:
        return float(self.settings.limited_account.daily_loss_limit_pct)

    def current_execution_mode(self) -> Literal["paper", "live", "queue_only"]:
        # Per-account override beats global default. Stored as a string;
        # validate to the literal set.
        mode = self.settings.limited_account.execution_mode
        return mode if mode in ("paper", "live", "queue_only") else "paper"  # type: ignore[return-value]

    def is_autonomy_enabled(self) -> bool:
        """Phase 5: autonomy is on iff mode is paper or live AND no kill switch.

        `queue_only` disables every auto-execute cell per SDD §10.1 hard
        rule. The `ARGOSY_KILL` env var halts new orders entirely.
        """
        if os.environ.get("ARGOSY_KILL") == "1":
            return False
        return self.current_execution_mode() in ("paper", "live")

    # ------------------------------------------------------------------
    # Live state
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list[Position]:
        """Positions in this account, via the injected adapter.

        Returns [] when no adapter is wired (paper-only configuration).
        Live positions are filtered to `self.account_id` so the adapter
        can be shared across multiple accounts.
        """
        if self.adapter is None:
            return []
        try:
            raw = self.adapter.get_positions(self.account_id)
        except Exception:  # pragma: no cover - defensive
            _log.exception("argonaut.get_positions_failed")
            return []
        return list(raw or [])

    def get_value_usd(
        self,
        *,
        positions: Iterable[Position] | None = None,
        last_prices: dict[str, float] | None = None,
        cash_usd: float | None = None,
    ) -> float:
        """Compute the account's USD value.

        Falls back to `configured_size_usd` if no live data supplied.
        With positions + last_prices, returns cash + sum(qty * price).

        Args:
          positions: caller-supplied list, else `get_open_positions()`.
          last_prices: ticker -> price dict; when missing for a ticker,
            fall back to position.avg_cost.
          cash_usd: idle cash; defaults to configured_size_usd when no
            live data is supplied (treats the configured size as cash).
        """
        positions_list = (
            list(positions) if positions is not None else self.get_open_positions()
        )
        if not positions_list:
            # No live data: account value == configured size (treated as cash).
            return self.configured_size_usd

        prices = last_prices or {}
        positions_value = 0.0
        for pos in positions_list:
            px = prices.get(pos.ticker)
            if px is None:
                px = pos.avg_cost or 0.0
            positions_value += float(px or 0.0) * float(pos.quantity)

        if cash_usd is None:
            cash_usd = max(0.0, self.configured_size_usd - positions_value)
        return float(cash_usd) + float(positions_value)

    # ------------------------------------------------------------------
    # Snapshot persistence
    # ------------------------------------------------------------------

    def build_snapshot(
        self,
        *,
        positions: Iterable[Position] | None = None,
        last_prices: dict[str, float] | None = None,
        cash_usd: float | None = None,
        prior_total_usd: float | None = None,
        on_date: _date_cls | None = None,
    ) -> ArgonautSnapshotPayload:
        positions_list = (
            list(positions) if positions is not None else self.get_open_positions()
        )
        prices = last_prices or {}
        positions_value = 0.0
        for pos in positions_list:
            px = prices.get(pos.ticker)
            if px is None:
                px = pos.avg_cost or 0.0
            positions_value += float(px or 0.0) * float(pos.quantity)

        if cash_usd is None:
            cash_usd = max(0.0, self.configured_size_usd - positions_value)
        total = float(cash_usd) + float(positions_value)
        day_pnl = (total - prior_total_usd) if prior_total_usd is not None else 0.0
        the_date = (on_date or _date_cls.today()).isoformat()
        return ArgonautSnapshotPayload(
            user_id=self.user_id,
            account_id=self.account_id,
            date=the_date,
            total_value_usd=total,
            cash_usd=float(cash_usd),
            positions_value_usd=float(positions_value),
            day_pnl_usd=float(day_pnl),
        )

    async def persist_daily_snapshot(
        self,
        *,
        positions: Iterable[Position] | None = None,
        last_prices: dict[str, float] | None = None,
        cash_usd: float | None = None,
        on_date: _date_cls | None = None,
    ) -> ArgonautSnapshotPayload:
        """Write today's snapshot row. Idempotent per (user, account, date)."""
        from argosy.accounts.persistence import (
            get_prior_total_usd,
            upsert_snapshot,
        )

        prior = await get_prior_total_usd(
            user_id=self.user_id, account_id=self.account_id, before=on_date
        )
        payload = self.build_snapshot(
            positions=positions,
            last_prices=last_prices,
            cash_usd=cash_usd,
            prior_total_usd=prior,
            on_date=on_date,
        )
        await upsert_snapshot(payload)
        return payload


__all__ = ["ArgonautAccount", "ArgonautSnapshotPayload"]
