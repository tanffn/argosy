"""Snapshot self-refresh — reprice the latest portfolio snapshot in place.

Binding decision (Ariel, 2026-07-06): the "Family Finances Status" TSV is an
Argosy OUTPUT, never an input dependency. The client must NOT have to export
anything for Argosy to have a fresh portfolio picture. When holdings have not
changed, a fresh snapshot is the OLD snapshot's quantities re-priced with live
quotes + fresh FX.

``refresh_portfolio_snapshot`` takes the latest ``portfolio_snapshots`` row,
carries every QUANTITY unchanged, re-prices each priceable position with a
live quote (yfinance; UCITS tickers resolved via exchange suffixes — same
convention as ``deployment_funnel.from_plan.SnapshotOrLiveProvider``),
refreshes USD/NIS + USD/EUR FX, recomputes local values / USD conversions /
totals, and INSERTS a new row with ``snapshot_date=today`` and
``source_path="self-refresh:reprice-of-<old snapshot_date>"`` so provenance is
explicit.

Rules (fail-safe, never fabricate):

* Cash rows, real-estate lines, pensions, and unpriceable rows (Israeli funds
  without a feed — non-latin / multi-word symbols) carry over UNCHANGED.
* A quote miss carries the old values and is recorded in
  ``parse_warnings_json`` as ``reprice_miss:<symbol>``.
* A live quote must pass two sanity guards before it is trusted:
  currency agreement with the position (GBp/GBX pence listings are rejected,
  they would 100x the value) and a price-ratio band vs the old price
  (a wrong-listing / wrong-instrument quote is a miss, not a reprice).
* The totals recompute is an INDEPENDENT sum over the new positions (the
  ``PortfolioSnapshot.total_usd_value_k`` property), never old-total + delta.
* Allocations / NVDA-sales / pension sections are carried verbatim — they are
  quantity-shaped or self-reported; repricing does not change them.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from sqlalchemy.orm import Session

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot
from argosy.logging import get_logger
from argosy.services.portfolio_snapshot_store import (
    get_latest_snapshot_row,
    persist_snapshot,
    row_to_snapshot,
)
from argosy.state.models import PortfolioSnapshotRow

_log = get_logger("argosy.services.snapshot_refresh")

# A symbol we can plausibly quote: single latin token (BRK/B, CNDX, O, QQQM).
# Hebrew fund names, multi-word index names ("STOXX Europe 600"), '-' and ''
# are unpriceable rows that carry over silently.
_PRICEABLE_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9./-]{0,9}$")

# yfinance listing-suffix candidates: bare first (US listings), then the UCITS
# exchange suffixes (LSE / Amsterdam / Milan / Xetra / SIX). Same rationale as
# SnapshotOrLiveProvider._YF_QUOTE_SUFFIXES (+ .SW for the SIX-listed lines).
_YF_QUOTE_SUFFIXES: tuple[str, ...] = ("", ".L", ".AS", ".MI", ".DE", ".SW")

# Exchange hint in the Details column ("(ISH NASDAQ100 $A) CNDX LN") → try that
# listing first so we don't pick up a bare-symbol collision on a US exchange.
_EXCHANGE_HINT_SUFFIX = {
    "LN": ".L",
    "AS": ".AS",
    "MI": ".MI",
    "DE": ".DE",
    "GY": ".DE",
    "SW": ".SW",
}
_EXCHANGE_HINT_RE = re.compile(r"\b([A-Z]{2})\s*$")

# A fresh quote whose ratio to the old price falls outside this band is a
# wrong-listing / wrong-instrument / pence-unit artifact, not a market move
# (holdings are repriced on a days-to-weeks cadence — a genuine >2x move in
# that window is rare enough that carrying the old value + a warning is the
# correct fail-safe).
_PRICE_RATIO_BAND = (0.5, 2.0)

# Same plausibility band for a refreshed FX rate vs the stored one.
_FX_RATIO_BAND = (0.8, 1.25)

# Pence-quoted LSE lines: accepting one would inflate a USD position ~100x.
_PENCE_CURRENCIES = {"GBP_PENCE", "GBX", "GBP0.01", "GBp"}


# ---------------------------------------------------------------------------
# Live providers (injectable for tests)
# ---------------------------------------------------------------------------


def _hinted_suffixes(details: str) -> tuple[str, ...]:
    """Order the suffix candidates, trying the Details exchange hint first."""
    m = _EXCHANGE_HINT_RE.search((details or "").strip())
    if m:
        suffix = _EXCHANGE_HINT_SUFFIX.get(m.group(1))
        if suffix:
            return (suffix,) + tuple(
                s for s in _YF_QUOTE_SUFFIXES if s != suffix
            )
    return _YF_QUOTE_SUFFIXES


def _currencies_agree(position_currency: str, quote_currency: str | None) -> bool:
    """A quote is only trusted when its currency matches the position's.

    ``None`` quote currency is tolerated ONLY for USD positions (yfinance
    omits currency on some US listings); pence variants are always rejected.
    """
    pc = (position_currency or "USD").strip().upper()
    if pc == "NIS":
        pc = "ILS"
    if quote_currency is None:
        return pc == "USD"
    qc = quote_currency.strip()
    if qc in _PENCE_CURRENCIES or qc.upper() in {"GBX", "GBP_PENCE"}:
        return False
    return qc.upper() == pc


def default_quote_fn(symbol: str, *, currency: str, details: str) -> float | None:
    """Live yfinance quote for one position, or ``None`` (miss).

    Tries the exchange-hinted listing first, then the standard suffix chain.
    A listing whose quote currency disagrees with the position currency is
    skipped (next suffix), never unit-converted — we don't fabricate prices.
    """
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    yf_symbol = symbol.strip().upper().replace("/", "-").replace(".", "-")
    adapter = YFinanceAdapter()
    for suffix in _hinted_suffixes(details):
        try:
            q = asyncio.run(adapter.get_quote(f"{yf_symbol}{suffix}"))
        except Exception as exc:  # noqa: BLE001 — best-effort per listing
            _log.info(
                "snapshot_refresh.quote_error",
                symbol=f"{yf_symbol}{suffix}",
                err=str(exc),
            )
            continue
        price = getattr(q, "price", None)
        if price is None:
            continue
        if not _currencies_agree(currency, getattr(q, "currency", None)):
            _log.info(
                "snapshot_refresh.quote_currency_mismatch",
                symbol=f"{yf_symbol}{suffix}",
                position_currency=currency,
                quote_currency=getattr(q, "currency", None),
            )
            continue
        return float(price)
    return None


def default_fx_fn() -> dict[str, float | None]:
    """Fresh FX: ``{"usd_nis": <rate|None>, "usd_eur": <rate|None>}``.

    USD/NIS via the BoI adapter chain (BoI → FRED → yfinance USDILS=X);
    USD/EUR derived from yfinance EURUSD=X (fx_usd_eur is EUR-per-USD, the
    TSV's convention). A failed leg returns None — the caller carries the
    stored rate and records the miss.
    """
    out: dict[str, float | None] = {"usd_nis": None, "usd_eur": None}
    try:
        from argosy.adapters.data.boi_adapter import BoiAdapter
        from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

        payload = asyncio.run(BoiAdapter(yf=YFinanceAdapter()).get_usd_nis())
        rate = payload.get("rate")
        out["usd_nis"] = float(rate) if rate else None
    except Exception as exc:  # noqa: BLE001 — carry the stored rate
        _log.warning("snapshot_refresh.fx_usd_nis_failed", err=str(exc))
    try:
        from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

        q = asyncio.run(YFinanceAdapter().get_quote("EURUSD=X"))
        price = getattr(q, "price", None)
        out["usd_eur"] = (1.0 / float(price)) if price else None
    except Exception as exc:  # noqa: BLE001 — carry the stored rate
        _log.warning("snapshot_refresh.fx_usd_eur_failed", err=str(exc))
    return out


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


@dataclass
class RefreshResult:
    """What one self-refresh run did — for logs / job output / verification."""

    row: PortfolioSnapshotRow | None
    snapshot: PortfolioSnapshot | None
    old_snapshot_date: date | None = None
    old_total_usd_k: float = 0.0
    new_total_usd_k: float = 0.0
    repriced: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fx_usd_nis: float | None = None
    fx_usd_eur: float | None = None

    def summary(self) -> dict:
        return {
            "old_snapshot_date": (
                self.old_snapshot_date.isoformat() if self.old_snapshot_date else None
            ),
            "old_total_usd_k": round(self.old_total_usd_k, 2),
            "new_total_usd_k": round(self.new_total_usd_k, 2),
            "repriced": len(self.repriced),
            "carried": len(self.carried),
            "warnings": self.warnings,
            "fx_usd_nis": self.fx_usd_nis,
            "fx_usd_eur": self.fx_usd_eur,
        }


def _is_carry_only(p: PortfolioPosition) -> bool:
    """Rows we never attempt to reprice: cash + quantity-less/feed-less rows.

    Note: asset_type "Real Estate" alone does NOT force a carry — IWDP (a
    listed property-ETF) is classified Real Estate but is fully priceable.
    The actual property row ("Aborad", symbol "-") is excluded by the
    symbol pattern below.
    """
    at = (p.asset_type or "").strip().lower()
    if at == "cash":
        return True
    if p.shares is None or p.shares <= 0:
        return True
    sym = (p.symbol or "").strip()
    if not _PRICEABLE_SYMBOL_RE.match(sym):
        return True  # Israeli funds / index-name rows — no feed
    return False


def _internally_consistent(p: PortfolioPosition) -> bool:
    """Trust the shares×price arithmetic only when the OLD row obeys it.

    Some source rows carry a value that is NOT shares×price (e.g. the TASE
    IBI STOXX 600 tracker's agorot pricing). Repricing such a row via
    shares×live_price would fabricate a wildly wrong value.
    """
    if p.current_price is None or p.current_value_local is None:
        return True  # nothing to cross-check; the price band still guards
    if p.current_value_local <= 0:
        return False
    implied = (p.shares or 0.0) * p.current_price
    return 0.8 <= (implied / p.current_value_local) <= 1.2


def _to_usd_k(
    value_local: float,
    currency: str,
    *,
    fx_usd_nis: float | None,
    fx_usd_eur: float | None,
) -> float | None:
    c = (currency or "USD").strip().upper()
    if c in ("USD", "$", ""):
        return value_local / 1000.0
    if c in ("NIS", "ILS"):
        return (value_local / fx_usd_nis / 1000.0) if fx_usd_nis else None
    if c == "EUR":
        # fx_usd_eur is EUR-per-USD (TSV convention "USD to EUR") → USD = EUR / rate.
        return (value_local / fx_usd_eur / 1000.0) if fx_usd_eur else None
    return None


def _within_band(ratio: float, band: tuple[float, float]) -> bool:
    return band[0] <= ratio <= band[1]


def refresh_portfolio_snapshot(
    session: Session,
    *,
    user_id: str = "ariel",
    quote_fn: Callable[..., float | None] | None = None,
    fx_fn: Callable[[], dict[str, float | None]] | None = None,
    today: date | None = None,
    commit: bool = True,
) -> RefreshResult:
    """Reprice the latest snapshot and INSERT a fresh ``portfolio_snapshots`` row.

    Returns a :class:`RefreshResult`; ``result.row is None`` means there was
    no prior snapshot to refresh (nothing inserted).
    """
    quote_fn = quote_fn or default_quote_fn
    fx_fn = fx_fn or default_fx_fn
    today = today or date.today()

    old_row = get_latest_snapshot_row(session, user_id)
    if old_row is None:
        _log.warning("snapshot_refresh.no_prior_snapshot", user_id=user_id)
        return RefreshResult(row=None, snapshot=None)
    old = row_to_snapshot(old_row)

    result = RefreshResult(
        row=None,
        snapshot=None,
        old_snapshot_date=old.snapshot_date,
        old_total_usd_k=old.total_usd_value_k,
    )

    # ---- FX refresh (carry the stored rate on a miss, with plausibility band)
    fx = fx_fn() or {}
    fx_usd_nis = old.fx_usd_nis
    fresh_nis = fx.get("usd_nis")
    if fresh_nis:
        if old.fx_usd_nis and not _within_band(fresh_nis / old.fx_usd_nis, _FX_RATIO_BAND):
            result.warnings.append("fx_suspect:usd_nis")
        else:
            fx_usd_nis = float(fresh_nis)
    else:
        result.warnings.append("fx_miss:usd_nis")

    fx_usd_eur = old.fx_usd_eur
    fresh_eur = fx.get("usd_eur")
    if fresh_eur:
        if old.fx_usd_eur and not _within_band(fresh_eur / old.fx_usd_eur, _FX_RATIO_BAND):
            result.warnings.append("fx_suspect:usd_eur")
        else:
            fx_usd_eur = float(fresh_eur)
    else:
        result.warnings.append("fx_miss:usd_eur")

    result.fx_usd_nis = fx_usd_nis
    result.fx_usd_eur = fx_usd_eur

    # ---- Reprice positions (quantities NEVER change) -----------------------
    new_positions: list[PortfolioPosition] = []
    for p in old.positions:
        label = (p.symbol or "").strip() or (p.details or p.asset_type or "?")[:24]
        if _is_carry_only(p):
            new_positions.append(p.model_copy(deep=True))
            result.carried.append(label)
            continue
        if not _internally_consistent(p):
            new_positions.append(p.model_copy(deep=True))
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}:inconsistent-source-row")
            continue

        price = quote_fn(p.symbol, currency=p.currency, details=p.details)
        if price is None or price <= 0:
            new_positions.append(p.model_copy(deep=True))
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}")
            continue
        if p.current_price and not _within_band(price / p.current_price, _PRICE_RATIO_BAND):
            new_positions.append(p.model_copy(deep=True))
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}:price-out-of-band")
            continue

        new_value_local = float(p.shares or 0.0) * float(price)
        usd_k = _to_usd_k(
            new_value_local, p.currency, fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur,
        )
        if usd_k is None:
            new_positions.append(p.model_copy(deep=True))
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}:no-fx-for-{p.currency}")
            continue

        updated = p.model_copy(deep=True)
        updated.current_price = float(price)
        updated.current_value_local = new_value_local
        updated.usd_value_k = usd_k
        # pct_change / pct_yearly are carried verbatim: the source mixes units
        # (Schwab rows store 24.0 for 24%, Leumi rows store 0.24) — recomputing
        # into either unit would corrupt half the rows.
        new_positions.append(updated)
        result.repriced.append(label)

    # ---- Assemble + persist (totals = INDEPENDENT sum over new positions) --
    new_snap = PortfolioSnapshot(
        source_path=f"self-refresh:reprice-of-{old.snapshot_date or 'unknown'}",
        snapshot_date=today,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
        positions=new_positions,
        real_estate=[r.model_copy(deep=True) for r in old.real_estate],
        allocations=[a.model_copy(deep=True) for a in old.allocations],
        nvda_sales=[s.model_copy(deep=True) for s in old.nvda_sales],
        pensions=[pe.model_copy(deep=True) for pe in old.pensions],
        parse_warnings=list(result.warnings),
    )
    # `total_usd_value_k` / `cash_balances_usd_k` are computed properties over
    # `positions` — persist_snapshot serialises them into totals_json, so the
    # stored total is by construction an independent sum, never old+delta.
    result.new_total_usd_k = new_snap.total_usd_value_k

    row = persist_snapshot(session, user_id=user_id, snapshot=new_snap, commit=commit)
    result.row = row
    result.snapshot = new_snap
    _log.info("snapshot_refresh.done", user_id=user_id, **result.summary())
    return result


__all__ = [
    "RefreshResult",
    "default_fx_fn",
    "default_quote_fn",
    "refresh_portfolio_snapshot",
]
