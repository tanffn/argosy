"""Live execution-fact collection for the canonical order sheet."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from argosy.services.order_sheet import MarketEvidence
from argosy.services.order_sheet_builder import ExecutionFacts

QuoteFactsFn = Callable[[str], dict[str, Any] | None]

_YAHOO_SUFFIXES: tuple[str, ...] = ("", ".L", ".AS", ".MI", ".DE", ".SW")


def _is_foreign_plan_etf(symbol: str, plan_domicile: str | None) -> bool:
    domicile = (plan_domicile or "").strip().upper()
    if not domicile or domicile in {"US", "USA", "UNITED STATES"}:
        return False
    try:
        from argosy.services.instrument_reference import STRUCT_ETF, lookup

        ref = lookup(symbol)
        return ref is not None and ref.structure == STRUCT_ETF
    except Exception:  # noqa: BLE001 - unknown identity must not be invented
        return False


def _quote_ticker_candidates(symbol: str, plan_domicile: str | None) -> tuple[str, ...]:
    """Provider tickers in identity-safe order.

    A foreign-domiciled ETF in the canonical plan is a specific UCITS security.
    Its bare ticker may resolve to an unrelated US ETF (observed live for EXUS),
    so bare-US fallback is not eligible for that identity. Foreign operating
    companies/ADRs remain bare-first (for example CMPS).
    """

    suffixes = _YAHOO_SUFFIXES
    if _is_foreign_plan_etf(symbol, plan_domicile):
        suffixes = tuple(suffix for suffix in suffixes if suffix)
    return tuple(f"{symbol}{suffix}" for suffix in suffixes)


def _live_quote_facts(
    symbol: str,
    plan_domicile: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the first identity-eligible Yahoo listing with a positive quote."""

    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    adapter = YFinanceAdapter()
    for ticker in _quote_ticker_candidates(symbol, plan_domicile):
        payload = asyncio.run(
            adapter.get_quote_with_fundamentals(ticker, ttl_seconds=300)
        )
        if payload and float(payload.get("price") or 0.0) > 0:
            payload = dict(payload)
            payload["resolved_ticker"] = ticker
            return payload
    return None


def _plan_domiciles(doc: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for cls in getattr(doc, "classes", []) or []:
        for inst in getattr(cls, "instruments", []) or []:
            symbol = (getattr(inst, "symbol", "") or "").strip().upper()
            domicile = (getattr(inst, "domicile", "") or "").strip()
            if symbol and domicile:
                out[symbol] = domicile
    return out


def collect_execution_facts(
    symbols: list[str],
    *,
    doc: Any,
    default_venue: str | None = None,
    quote_facts_fn: QuoteFactsFn | None = None,
    observed_at: datetime | None = None,
) -> tuple[dict[str, ExecutionFacts], dict[str, str]]:
    """Collect current facts; return ``(facts, failures_by_symbol)``.

    There is no stale/snapshot fallback here.  A missing live quote or venue is
    reported as a failure, making the selected line ineligible for the order
    sheet.  Plan domicile may establish a fund's incorporation/domicile; a new
    single stock still needs a live profile country from the data provider.
    """

    fetch = quote_facts_fn
    now = observed_at or datetime.now(UTC)
    domiciles = _plan_domiciles(doc)
    facts: dict[str, ExecutionFacts] = {}
    failures: dict[str, str] = {}

    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        try:
            payload = (
                fetch(symbol)
                if fetch is not None
                else _live_quote_facts(symbol, domiciles.get(symbol))
            )
        except Exception as exc:  # noqa: BLE001 - fact miss must be surfaced
            failures[symbol] = f"live quote fetch failed: {exc}"
            continue
        if not payload or float(payload.get("price") or 0.0) <= 0:
            failures[symbol] = "no positive live-verified price"
            continue

        venue = str(payload.get("exchange") or default_venue or "").strip()
        if not venue:
            failures[symbol] = "execution venue unresolved"
            continue
        quote_ts = payload.get("timestamp_utc")
        try:
            price_as_of = (
                datetime.fromisoformat(str(quote_ts).replace("Z", "+00:00")) if quote_ts else now
            )
        except ValueError:
            price_as_of = now

        country = str(payload.get("country") or domiciles.get(symbol) or "").strip()
        market_cap = payload.get("market_cap")
        market_cap_f = float(market_cap) if market_cap not in (None, "") else None
        source_ticker = payload.get("resolved_ticker") or payload.get("ticker") or symbol
        if _is_foreign_plan_etf(symbol, domiciles.get(symbol)):
            resolved = str(source_ticker).strip().upper()
            if resolved == symbol or not resolved.startswith(f"{symbol}."):
                failures[symbol] = (
                    f"listing identity mismatch: plan requires the foreign-domiciled "
                    f"ETF, provider resolved {source_ticker!s}"
                )
                continue
        source = f"yfinance:{source_ticker} retrieved {now.isoformat()}"

        from argosy.services.instrument_reference import estate_safe_for

        estate_safe = estate_safe_for(symbol)
        situs = "non_US" if estate_safe is True else ("US" if estate_safe is False else "unknown")
        quote_type = str(payload.get("quote_type") or "").upper()
        instrument_type = "etf" if quote_type == "ETF" else "stock"
        facts[symbol] = ExecutionFacts(
            evidence=MarketEvidence(
                price_usd=float(payload["price"]),
                price_as_of=price_as_of,
                price_source=source,
                market_cap_usd=market_cap_f,
                market_cap_as_of=now if market_cap_f is not None else None,
                market_cap_source=source if market_cap_f is not None else None,
                incorporation_country=country or None,
                incorporation_as_of=now if country else None,
                incorporation_source=(
                    source
                    if payload.get("country")
                    else "canonical plan instrument domicile"
                    if domiciles.get(symbol)
                    else None
                ),
            ),
            venue=venue,
            instrument_type=instrument_type,
            estate_situs=situs,
            # Whole shares are the conservative executable default. A broker or
            # venue adapter may explicitly provide a smaller supported increment.
            quantity_increment=float(payload.get("quantity_increment") or 1.0),
        )
    return facts, failures


__all__ = ["collect_execution_facts"]
