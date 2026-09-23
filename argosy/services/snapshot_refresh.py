"""Snapshot self-refresh — reprice the latest portfolio snapshot in place.

Binding decision (Ariel, 2026-07-06): the "Family Finances Status" TSV is an
Argosy OUTPUT, never an input dependency. The client must NOT have to export
anything for Argosy to have a fresh portfolio picture. When holdings have not
changed, a fresh snapshot is the OLD snapshot's quantities re-priced with live
quotes + fresh FX.

``refresh_portfolio_snapshot`` takes the latest ``portfolio_snapshots`` row,
carries every QUANTITY unchanged, re-prices each priceable position with a
live quote (yfinance; explicit listings and broker ticker/venue hints are
preserved, never replaced by guesses on other exchanges),
refreshes USD/NIS + USD/EUR FX, recomputes local values / USD conversions /
totals, and INSERTS a new row with ``snapshot_date=today`` and
``source_path="self-refresh:reprice-of-<old snapshot_date>"`` so provenance is
explicit.

Rules (fail-safe, never fabricate):

* Cash rows, real-estate lines, pensions, and unpriceable rows (Israeli funds
  without a feed — non-latin / multi-word symbols) carry their QUANTITIES and
  local values unchanged, but ``usd_value_k`` is RE-DERIVED from
  ``current_value_local`` at the refreshed FX whenever the currency is
  convertible. ``usd_value_k`` is a derived projection, never source data —
  carrying it verbatim propagated a stale ``usd_value_k`` from an upstream
  row whose local value had moved (live incident: the post-SGOV-sale row's
  Leumi USD cash showed local $3,655 but usd_value_k −16.4, and the
  self-refresh row inherited a phantom-negative cash total).
* A quote miss carries the old values BUT keeps the prior ``valued_as_of``
  and stamps ``mark_stale=True`` — never laundering a July mark into a
  snapshot dated today. Consumers that publish current money must reprice
  or refuse stale marks.
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

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from argosy.async_bridge import run_async_from_sync
from argosy.execution.fill_evidence import canonical_currency
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


def _exchange_hint(details: str, symbol: str = ""):
    text = (details or "").strip()
    match = _EXCHANGE_HINT_RE.search(text)
    if match and symbol:
        prefix = text[:match.start()].rstrip()
        names = {symbol.upper(), symbol.upper().split(".")[0]}
        if not any(re.search(rf"(?<![A-Z0-9]){re.escape(name)}$", prefix) for name in names):
            # Broker listing notation is a ticker + venue pair. Company names
            # ending in CO/AG/SA, or a lone two-letter ticker, aren't venues.
            return None
    return match


def _hinted_suffixes(details: str, currency: str = "USD", *, symbol: str = "") -> tuple[str, ...]:
    """Use the stated listing, never guess another exchange after a miss.

    Currency alone cannot identify a European listing. Unhinted USD symbols
    retain their bare US identity; other unhinted currencies need mapping.
    """
    pc = (currency or "USD").strip().upper()
    m = _exchange_hint(details, symbol)
    if m:
        suffix = _EXCHANGE_HINT_SUFFIX.get(m.group(1))
        if suffix:
            return (suffix,)
        return ()  # Explicit but unsupported venue is not a US listing.
    return ("",) if pc == "USD" else ()


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


def resolved_quote_symbols(symbol: str, *, currency: str, details: str) -> tuple[str, ...]:
    """Single listing-identity resolver shared by repricing and receipt application."""
    from argosy.adapters.data.symbols import to_yahoo_symbol

    symbol = symbol.strip()
    yf_symbol = to_yahoo_symbol(symbol)
    hint = _exchange_hint(details, symbol)
    hint_suffix = _EXCHANGE_HINT_SUFFIX.get(hint.group(1)) if hint else None
    if hint and (hint_suffix is None or ("." in yf_symbol and not yf_symbol.endswith(hint_suffix))):
        return ()
    suffixes = ("",) if "." in yf_symbol else _hinted_suffixes(details, currency, symbol=symbol)
    return tuple(f"{yf_symbol}{suffix}" for suffix in suffixes)


def default_quote_fn(symbol: str, *, currency: str, details: str) -> float | None:
    """Live yfinance quote for one position, or ``None`` (miss).

    Uses the explicit symbol listing or the exchange hint from the broker.
    A listing whose quote currency disagrees with the position currency is
    skipped (next suffix), never unit-converted — we don't fabricate prices.
    """
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    adapter = YFinanceAdapter()
    candidates = resolved_quote_symbols(symbol, currency=currency, details=details)
    if not candidates:
        _log.info("snapshot_refresh.listing_unresolved", symbol=symbol, currency=currency)
    for candidate in candidates:
        try:
            q = run_async_from_sync(
                lambda candidate=candidate: adapter.get_quote(candidate)
            )
        except Exception as exc:  # noqa: BLE001 — best-effort per listing
            _log.info(
                "snapshot_refresh.quote_error",
                symbol=candidate,
                err=str(exc),
            )
            continue
        price = getattr(q, "price", None)
        if price is None:
            continue
        if not _currencies_agree(currency, getattr(q, "currency", None)):
            _log.info(
                "snapshot_refresh.quote_currency_mismatch",
                symbol=candidate,
                position_currency=currency,
                quote_currency=getattr(q, "currency", None),
            )
            continue
        return float(price)
    return None


def reprice_quantity(
    *,
    symbol: str,
    shares: float,
    currency: str = "USD",
    details: str = "",
    old_price: float | None = None,
    quote_fn: Callable[..., float | None] | None = None,
    fx_usd_nis: float | None = None,
    fx_usd_eur: float | None = None,
) -> tuple[float, float] | None:
    """Reprice a known share count via the managed-book quote path.

    Returns ``(live_price, usd_value_k)`` or ``None`` on a quote miss / band
    rejection / FX miss. Quantities never change — only the mark. Used by the
    unmanaged durable-book restore so a 25-day-old *price* is never published
    as current money while the share count remains a durable fact.
    """
    qfn = quote_fn or default_quote_fn
    sym = (symbol or "").strip()
    if not sym or shares is None or float(shares) <= 0:
        return None
    if not _PRICEABLE_SYMBOL_RE.match(sym):
        return None
    price = qfn(sym, currency=currency, details=details or "")
    if price is None or price <= 0:
        return None
    if old_price and float(old_price) > 0:
        if not _within_band(float(price) / float(old_price), _PRICE_RATIO_BAND):
            return None
    new_value_local = float(shares) * float(price)
    usd_k = _to_usd_k(
        new_value_local, currency, fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur,
    )
    if usd_k is None:
        return None
    return float(price), float(usd_k)


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

        adapter = BoiAdapter(yf=YFinanceAdapter())
        payload = run_async_from_sync(adapter.get_usd_nis)
        rate = payload.get("rate")
        out["usd_nis"] = float(rate) if rate else None
    except Exception as exc:  # noqa: BLE001 — carry the stored rate
        _log.warning("snapshot_refresh.fx_usd_nis_failed", err=str(exc))
    try:
        from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

        adapter = YFinanceAdapter()
        q = run_async_from_sync(lambda: adapter.get_quote("EURUSD=X"))
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


def _recompute_allocations(
    positions: list[PortfolioPosition],
    prior_allocations: list,
) -> list:
    """Re-derive the 'Current allocation' block from the NEW positions.

    A derived table carried forward verbatim goes stale the moment
    positions change — a fills-applied row that still showed the
    pre-deploy Cash current/delta fed the unallocated-cash detector a
    ~$98k excess that no longer existed, and the directive fleet authored
    a deploy of already-deployed money. Currents must always be re-summed
    from the row's own positions.

    Contract (mirrors the TSV table's semantics): each prior category
    keeps its ``target_pct``/``target_k`` verbatim; ``usd_value_k`` is the
    sum of positions whose ``asset_type`` equals the category (0.0 when
    none remain); ``pct`` is against the grand total of ALL positions
    (the table is a partial view over the book); ``delta_k`` is
    ``target_k - usd_value_k``. A 'Grand Total' row carries the full
    positions sum. No prior allocations → returns [] unchanged (a
    snapshot without the block never grows one here).
    """
    if not prior_allocations:
        return []
    by_type: dict[str, float] = {}
    grand_total = 0.0
    for p in positions:
        v = float(getattr(p, "usd_value_k", 0.0) or 0.0)
        grand_total += v
        t = (getattr(p, "asset_type", "") or "").strip()
        if t:
            by_type[t] = by_type.get(t, 0.0) + v
    out = []
    for a in prior_allocations:
        new = a.model_copy(deep=True)
        if (a.category or "").strip().lower() == "grand total":
            new.usd_value_k = round(grand_total, 2)
            new.pct = 100.0
            # A total row has no meaningful target gap — don't carry a
            # stale delta from the prior copy.
            new.delta_k = (
                round(a.target_k - grand_total, 2)
                if a.target_k is not None else None
            )
            out.append(new)
            continue
        current = round(by_type.get((a.category or "").strip(), 0.0), 2)
        new.usd_value_k = current
        new.pct = (
            round(current / grand_total * 100.0, 2) if grand_total > 0 else None
        )
        if a.target_k is not None:
            new.delta_k = round(a.target_k - current, 2)
        out.append(new)
    return out


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
    def _carry(p: PortfolioPosition) -> PortfolioPosition:
        """Copy a carried row; re-derive its USD projection from the local value.

        ``current_value_local`` (+ currency) is the canonical quantity-shaped
        fact; ``usd_value_k`` is derived. Re-deriving at the refreshed FX
        both applies fresh FX to NIS/EUR carries and heals a stale
        ``usd_value_k`` left behind by an upstream writer that moved the
        local value without recomputing the projection. Unconvertible
        currencies (or a missing local value) keep the old projection.
        """
        c = p.model_copy(deep=True)
        if c.current_value_local is not None:
            usd = _to_usd_k(
                c.current_value_local, c.currency,
                fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur,
            )
            if usd is not None:
                c.usd_value_k = usd
        return c

    new_positions: list[PortfolioPosition] = []
    prior_as_of = old.snapshot_date
    for p in old.positions:
        label = (p.symbol or "").strip() or (p.details or p.asset_type or "?")[:24]
        if _is_carry_only(p):
            carried = _carry(p)
            # Preserve prior mark date — do not launder a stale mark into today.
            if carried.valued_as_of is None:
                # Row-first (see the reprice-miss branches below): keep the
                # position's own mark date; fall back to prior_as_of only when
                # the row never had one.
                carried.valued_as_of = getattr(p, "valued_as_of", None) or prior_as_of
            if carried.observed_as_of is None:
                carried.observed_as_of = (
                    getattr(p, "observed_as_of", None) or prior_as_of
                )
            new_positions.append(carried)
            result.carried.append(label)
            continue
        if not _internally_consistent(p):
            carried = _carry(p)
            # ROW-FIRST: keep the position's OWN (true, possibly stale) mark
            # date; only fall back to prior_as_of when the row never had one.
            # prior_as_of-first laundered a July-13 mark into Aug-8 on the
            # SECOND consecutive quote miss (each refresh advanced prior_as_of),
            # defeating the staleness guard (Sol BLOCK-3, upstream source).
            carried.valued_as_of = getattr(p, "valued_as_of", None) or prior_as_of
            carried.observed_as_of = getattr(p, "observed_as_of", None) or prior_as_of
            carried.mark_stale = True
            new_positions.append(carried)
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}:inconsistent-source-row")
            continue

        price = quote_fn(p.symbol, currency=p.currency, details=p.details)
        if price is None or price <= 0:
            carried = _carry(p)
            # ROW-FIRST: keep the position's OWN (true, possibly stale) mark
            # date; only fall back to prior_as_of when the row never had one.
            # prior_as_of-first laundered a July-13 mark into Aug-8 on the
            # SECOND consecutive quote miss (each refresh advanced prior_as_of),
            # defeating the staleness guard (Sol BLOCK-3, upstream source).
            carried.valued_as_of = getattr(p, "valued_as_of", None) or prior_as_of
            carried.observed_as_of = getattr(p, "observed_as_of", None) or prior_as_of
            carried.mark_stale = True
            new_positions.append(carried)
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}")
            continue
        if p.current_price and not _within_band(price / p.current_price, _PRICE_RATIO_BAND):
            carried = _carry(p)
            # ROW-FIRST: keep the position's OWN (true, possibly stale) mark
            # date; only fall back to prior_as_of when the row never had one.
            # prior_as_of-first laundered a July-13 mark into Aug-8 on the
            # SECOND consecutive quote miss (each refresh advanced prior_as_of),
            # defeating the staleness guard (Sol BLOCK-3, upstream source).
            carried.valued_as_of = getattr(p, "valued_as_of", None) or prior_as_of
            carried.observed_as_of = getattr(p, "observed_as_of", None) or prior_as_of
            carried.mark_stale = True
            new_positions.append(carried)
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}:price-out-of-band")
            continue

        new_value_local = float(p.shares or 0.0) * float(price)
        usd_k = _to_usd_k(
            new_value_local, p.currency, fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur,
        )
        if usd_k is None:
            carried = _carry(p)
            # ROW-FIRST: keep the position's OWN (true, possibly stale) mark
            # date; only fall back to prior_as_of when the row never had one.
            # prior_as_of-first laundered a July-13 mark into Aug-8 on the
            # SECOND consecutive quote miss (each refresh advanced prior_as_of),
            # defeating the staleness guard (Sol BLOCK-3, upstream source).
            carried.valued_as_of = getattr(p, "valued_as_of", None) or prior_as_of
            carried.observed_as_of = getattr(p, "observed_as_of", None) or prior_as_of
            carried.mark_stale = True
            new_positions.append(carried)
            result.carried.append(label)
            result.warnings.append(f"reprice_miss:{label}:no-fx-for-{p.currency}")
            continue

        updated = p.model_copy(deep=True)
        updated.current_price = float(price)
        updated.current_value_local = new_value_local
        updated.usd_value_k = usd_k
        updated.valued_as_of = today
        updated.observed_as_of = getattr(p, "observed_as_of", None) or prior_as_of
        updated.mark_stale = False
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
        allocations=_recompute_allocations(new_positions, old.allocations),
        nvda_sales=[s.model_copy(deep=True) for s in old.nvda_sales],
        pensions=[pe.model_copy(deep=True) for pe in old.pensions],
        parse_warnings=list(result.warnings),
    )
    # `total_usd_value_k` / `cash_balances_usd_k` are computed properties over
    # `positions` — persist_snapshot serialises them into totals_json, so the
    # stored total is by construction an independent sum, never old+delta.
    result.new_total_usd_k = new_snap.total_usd_value_k

    row = persist_snapshot(session, user_id=user_id, snapshot=new_snap, commit=commit,
                           expected_prior_id=old_row.id)
    result.row = row
    result.snapshot = new_snap
    _log.info("snapshot_refresh.done", user_id=user_id, **result.summary())
    return result


# ---------------------------------------------------------------------------
# Broker-fill application (executed trades → new snapshot row)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """One executed trade with cash charges in its explicitly stated currency.

    ``symbol`` must be the SNAPSHOT convention (bare ticker, e.g. "CSPX" not
    "CSPX.L"); put the listing hint in ``details`` ("... CSPX LN") so the
    self-refresh repricer can quote the right exchange later.
    """

    symbol: str
    shares: float
    price: float
    asset_type: str = ""
    details: str = ""
    location: str = "Leumi"
    currency: str = "USD"
    action: str = "buy"
    commission: float = 0.0
    tax_withheld: float | None = None
    net_cash_delta: float | None = None
    observed_as_of: date | None = None

    @property
    def cost(self) -> float:
        from argosy.execution.fill_evidence import ledger_amount

        return float(ledger_amount(self.shares) * ledger_amount(self.price))

    @property
    def cash_delta(self) -> Decimal:
        from argosy.execution.fill_evidence import ledger_amount

        gross = ledger_amount(self.shares) * ledger_amount(self.price)
        fees = ledger_amount(self.commission, positive=False)
        if self.action not in {"buy", "sell"}:
            raise ValueError("fill action must be buy or sell")
        if self.action == "sell" and self.tax_withheld is None:
            raise ValueError("sell requires explicit broker tax withheld, including confirmed zero")
        withheld = ledger_amount(self.tax_withheld or 0, positive=False)
        if withheld < 0:
            raise ValueError("tax withheld cannot be negative")
        computed = (-gross if self.action == "buy" else gross) - fees - withheld
        if self.net_cash_delta is not None:
            reported = ledger_amount(self.net_cash_delta, positive=False)
            if abs(computed - reported) > Decimal("0.01"):
                raise ValueError("reported net cash does not reconcile with gross, fees and withholding")
            return reported
        return computed


@dataclass
class ApplyFillsResult:
    """What one apply-fills run did — for logs / verification."""

    row: PortfolioSnapshotRow | None
    snapshot: PortfolioSnapshot | None
    old_total_usd_k: float = 0.0
    new_total_usd_k: float = 0.0
    cash_before_local: float | None = None
    cash_after_local: float | None = None
    merged: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _fill_mark_consistent(position: PortfolioPosition) -> bool:
    """Accounting identity, not the quote resolver's price plausibility band."""
    from argosy.execution.fill_evidence import number

    if position.shares == 0:
        return position.current_value_local is not None and abs(number(position.current_value_local)) <= Decimal("0.01")
    if any(value is None for value in (position.shares, position.current_price, position.current_value_local)):
        return False
    return abs(number(position.shares) * number(position.current_price)
               - number(position.current_value_local)) <= Decimal("0.01")


def matching_fill_positions(positions, *, symbol: str, location: str, currency: str):
    """Use identical holding identity for settlement checks and book mutation."""
    return [p for p in positions if (
        (p.symbol or "").strip().upper() == symbol.strip().upper()
        and (p.location or "").strip().lower() == location.strip().lower()
        and canonical_currency(p.currency) == canonical_currency(currency)
        and (p.asset_type or "").strip().lower() != "cash")]


def _pct_unit_is_percent(p: PortfolioPosition) -> bool:
    """Detect the row's pct_change unit (Schwab stores 24.0, Leumi 0.24)."""
    if p.pct_change is None or not p.avg_price or not p.current_price:
        return False
    frac = p.current_price / p.avg_price - 1.0
    return abs(p.pct_change - frac * 100.0) < abs(p.pct_change - frac)


def apply_fills_to_snapshot(
    session: Session,
    *,
    fills: list[Fill],
    source_tag: str,
    user_id: str = "ariel",
    cash_location: str = "Leumi",
    cash_currency: str = "USD",
    extra_warnings: tuple[str, ...] | list[str] = (),
    today: date | None = None,
    commit: bool = True,
) -> ApplyFillsResult:
    """Fold explicitly supplied trade facts into a snapshot; INSERT a new row.

    This low-level helper is NOT an idempotent receipt consumer. Its caller
    must own durable receipt application and statement reconciliation.
    Sell cash uses reported withholding, never an invented tax estimate.

    Rules (conservation — cash becomes positions, nothing appears/vanishes):

    * A buy whose (symbol, location, currency) matches a held non-cash
      position MERGES: shares add, ``avg_price`` is the honest blended
      average ``(old_sh*old_avg + fill_sh*fill_px) / total_sh``, and the
      position is re-valued at the snapshot's ``current_price`` (fresher
      than the fill print when the snapshot was repriced the same day).
      An unknown prior average remains unknown. A sale subtracts shares,
      retaining the display average without claiming tax-lot accounting.
    * A non-matching buy ADDS a new position with
      ``avg_price = current_price = fill price``.
    * The single cash position at (``cash_location``, ``cash_currency``)
      changes by signed proceeds/cost less fees and reported withholding.
      Missing or ambiguous cash row → ``ValueError``
      (never invent a funding source). A negative resulting balance is
      NOT an error — the executed fills are facts — but it is recorded
      loudly as ``cash_overdraft:...`` in ``parse_warnings`` (stale
      snapshot cash vs the real broker balance; next ingest reconciles).
    * Totals are an INDEPENDENT sum over the new positions (model
      property), never old-total ± delta.
    * Every applied fill is recorded in ``parse_warnings`` as
      ``fill-applied:<sym>:<shares>@<price>``; callers append
      closed-loop expectation notes via ``extra_warnings``. A
      machine-readable ``closed_loop_expectations:{json}`` entry is
      ALSO written (expected final share counts + post-fill cash) —
      ``argosy.services.closed_loop`` verifies it on the next real
      ingest; the prose stays for humans.
    """
    today = today or date.today()
    old_row = get_latest_snapshot_row(session, user_id)
    if old_row is None:
        raise ValueError(f"no prior portfolio snapshot for user {user_id!r}")
    old = row_to_snapshot(old_row)

    result = ApplyFillsResult(
        row=None, snapshot=None, old_total_usd_k=old.total_usd_value_k,
    )
    if not fills:
        result.row, result.snapshot = old_row, old
        result.new_total_usd_k = result.old_total_usd_k
        return result

    from argosy.execution.fill_evidence import ledger_amount

    # Use one canonical NUMERIC(18,4) representation for holdings, cash and
    # expectation receipts, including accepted floating-point transport noise.
    fills = [replace(fill, shares=float(ledger_amount(fill.shares)),
                     price=float(ledger_amount(fill.price)),
                     commission=float(ledger_amount(fill.commission, positive=False)),
                     tax_withheld=(float(ledger_amount(fill.tax_withheld, positive=False))
                                   if fill.tax_withheld is not None else None),
                     net_cash_delta=(float(ledger_amount(fill.net_cash_delta, positive=False))
                                     if fill.net_cash_delta is not None else None)) for fill in fills]

    positions = [p.model_copy(deep=True) for p in old.positions]
    # A trade changes quantities, not the age of unrelated price observations.
    for position in positions:
        if old.snapshot_date is None and (position.observed_as_of is None or position.valued_as_of is None):
            raise ValueError("prior snapshot date is required to preserve undated mark provenance")
        position.observed_as_of = position.observed_as_of or old.snapshot_date
        position.valued_as_of = position.valued_as_of or old.snapshot_date

    from argosy.execution.fill_evidence import number

    ccy = canonical_currency(cash_currency)
    rate = 1 if ccy in {"USD", "$"} else old.fx_usd_nis if ccy in {"NIS", "ILS"} else old.fx_usd_eur if ccy == "EUR" else None
    if rate is None or number(rate) <= 0:
        raise ValueError(f"finite positive FX conversion is required for {cash_currency}")

    def _require_consistent_projection(position: PortfolioPosition) -> None:
        if (position.current_value_local is None or position.usd_value_k is None
                or abs(number(position.current_value_local) / number(rate)
                       - number(position.usd_value_k) * 1000) > Decimal("0.01")):
            raise ValueError(f"{position.symbol or 'cash'}: existing USD projection is inconsistent")

    # One funding currency/account per call. An FX transfer is a separate
    # broker fact; silently subtracting USD from NIS is not conservation.
    total_delta = Decimal(0)
    for fill in fills:
        if (fill.location.strip().lower() != cash_location.strip().lower()
                or canonical_currency(fill.currency) != canonical_currency(cash_currency)):
            raise ValueError("fill and cash must have the same explicit custody and currency")
        total_delta += fill.cash_delta  # Validate before changing the in-memory book.

    def _find_position(fill: Fill) -> PortfolioPosition | None:
        matches = matching_fill_positions(positions, symbol=fill.symbol, location=fill.location, currency=fill.currency)
        if len(matches) > 1:
            raise ValueError(f"ambiguous holding rows for {fill.symbol} at {fill.location}")
        return matches[0] if matches else None

    for fill in fills:
        if fill.shares <= 0 or fill.price <= 0:
            raise ValueError(f"non-positive fill for {fill.symbol}: {fill}")
        held = _find_position(fill)
        if held is not None and held.shares is None:
            raise ValueError(f"cannot apply {fill.symbol}: held quantity is unknown")
        if held is not None:
            # The quote path's +/-20% plausibility band is not conservation.
            # A fill cannot silently correct a pre-existing valuation mismatch.
            if not _fill_mark_consistent(held):
                raise ValueError(f"{fill.symbol}: existing holding price/value units are inconsistent")
            _require_consistent_projection(held)
        if fill.action == "sell":
            if held is None or held.shares is None:
                raise ValueError(f"cannot sell {fill.symbol}: held quantity is unknown")
            from argosy.execution.fill_evidence import ledger_amount, number

            remaining = number(held.shares) - ledger_amount(fill.shares)
            if remaining < 0:
                raise ValueError(f"cannot apply {fill.symbol}: sale exceeds recorded holding")
            held.shares = float(remaining)
            price_basis = held.current_price
            held.current_value_local = float(remaining * number(price_basis))
            held.usd_value_k = _to_usd_k(
                held.current_value_local, held.currency,
                fx_usd_nis=old.fx_usd_nis, fx_usd_eur=old.fx_usd_eur,
            )
            # A sale does not establish the remaining lots' tax basis. Keep
            # the old display average; do not average the sale price into it.
            result.merged.append(fill.symbol)
            continue
        if held is not None:
            from argosy.execution.fill_evidence import ledger_amount, number

            old_sh = number(held.shares)
            if old_sh < 0:
                raise ValueError(f"cannot apply {fill.symbol}: short-position accounting is not supported")
            old_avg = number(held.avg_price) if held.avg_price is not None else (Decimal(0) if old_sh == 0 else None)
            if held.avg_price is None and old_sh > 0:
                result.warnings.append(f"fill_merge_no_avg:{fill.symbol}")
            total_sh = old_sh + ledger_amount(fill.shares)
            blended = ((old_sh * old_avg + ledger_amount(fill.shares) * ledger_amount(fill.price)) / total_sh
                       if old_avg is not None else None)
            price_basis = (
                held.current_price if old_sh > 0 else fill.price
            )
            pct_is_percent = _pct_unit_is_percent(held)  # judge on OLD row
            held.shares = float(total_sh)
            held.avg_price = float(round(blended, 4)) if blended is not None else None
            held.current_price = float(price_basis)
            if old_sh == 0:
                held.valued_as_of = held.observed_as_of = fill.observed_as_of or today
            held.current_value_local = float(total_sh * number(price_basis))
            held.usd_value_k = _to_usd_k(
                held.current_value_local, held.currency,
                fx_usd_nis=old.fx_usd_nis, fx_usd_eur=old.fx_usd_eur,
            )
            if held.avg_price:
                frac = float(price_basis) / held.avg_price - 1.0
                held.pct_change = round(frac * 100.0 if pct_is_percent else frac, 4)
            else:
                held.pct_change = None
            result.merged.append(fill.symbol)
        else:
            value_local = fill.cost
            positions.append(
                PortfolioPosition(
                    location=fill.location,
                    currency=fill.currency,
                    asset_type=fill.asset_type,
                    details=fill.details,
                    symbol=fill.symbol,
                    shares=float(fill.shares),
                    current_price=float(fill.price),
                    avg_price=float(fill.price),
                    current_value_local=value_local,
                    usd_value_k=_to_usd_k(
                        value_local, fill.currency,
                        fx_usd_nis=old.fx_usd_nis, fx_usd_eur=old.fx_usd_eur,
                    ),
                    pct_change=0.0,
                    valued_as_of=fill.observed_as_of or today,
                    observed_as_of=fill.observed_as_of or today,
                )
            )
            result.added.append(fill.symbol)

    # ---- Cash deduction (single funding source, fail-loud if absent) -------
    cash_matches = [
            p for p in positions
            if (p.asset_type or "").strip().lower() == "cash"
            and (p.location or "").strip().lower() == cash_location.strip().lower()
            and canonical_currency(p.currency) == canonical_currency(cash_currency)
    ]
    if len(cash_matches) > 1:
        raise ValueError(f"ambiguous cash positions at {cash_location}/{cash_currency}")
    cash_pos = cash_matches[0] if cash_matches else None
    if cash_pos is None or cash_pos.current_value_local is None:
        raise ValueError(
            f"no cash position at {cash_location}/{cash_currency} to fund "
            f"the reported fills"
        )
    result.cash_before_local = cash_pos.current_value_local
    _require_consistent_projection(cash_pos)
    from argosy.execution.fill_evidence import number

    cash_pos.current_value_local = float(number(cash_pos.current_value_local) + total_delta)
    cash_pos.usd_value_k = _to_usd_k(
        cash_pos.current_value_local, cash_pos.currency,
        fx_usd_nis=old.fx_usd_nis, fx_usd_eur=old.fx_usd_eur,
    )
    result.cash_after_local = cash_pos.current_value_local
    if cash_pos.current_value_local < 0:
        result.warnings.append(
            f"cash_overdraft:{cash_location}:{cash_currency}:"
            f"{cash_pos.current_value_local:,.2f} — snapshot cash was stale "
            f"vs the real broker balance; next real ingest must reconcile"
        )

    fill_notes = [
        f"fill-applied:{'SELL:' if f.action == 'sell' else ''}{f.symbol}:{f.shares:g}@{f.price:g}" for f in fills
    ]

    # Machine-readable closed-loop expectations blob (the prose entries above
    # are kept for humans; this is what argosy.services.closed_loop parses
    # losslessly — expected FINAL share counts from the post-fill book, the
    # funding account's post-fill balance, and the caller's manual notes).
    import json as _json

    touched_keys = {
        (f.symbol.strip().upper(), f.location.strip().lower(),
         canonical_currency(f.currency))
        for f in fills
    }
    expected_positions = [
        {
            "symbol": (p.symbol or "").strip().upper(),
            "location": p.location,
            "currency": canonical_currency(p.currency),
            "shares": p.shares,
            "price": p.current_price,
            "shares_delta": sum((-f.shares if f.action == "sell" else f.shares)
                                for f in fills if f.symbol.strip().upper() == (p.symbol or "").strip().upper()
                                and f.location.strip().lower() == p.location.strip().lower()
                                and canonical_currency(f.currency) == canonical_currency(p.currency)),
        }
        for p in positions
        if (p.asset_type or "").strip().lower() != "cash"
        and (
            (p.symbol or "").strip().upper(),
            (p.location or "").strip().lower(),
            canonical_currency(p.currency),
        ) in touched_keys
    ]
    expectations_blob = "closed_loop_expectations:" + _json.dumps({
        "v": 1,
        "source_tag": source_tag,
        "fills": [
            {"symbol": f.symbol, "shares": f.shares, "price": f.price,
             "location": f.location, "currency": f.currency, "action": f.action,
             "commission": f.commission, "tax_withheld": f.tax_withheld,
             "net_cash_delta": f.net_cash_delta,
             "cash_delta": str(f.cash_delta)}
            for f in fills
        ],
        "expected_positions": expected_positions,
        "cash": {
            "location": cash_location,
            "currency": cash_currency,
            "after_local": result.cash_after_local,
        },
        "manual": [str(w) for w in extra_warnings],
    }, default=str)

    new_snap = PortfolioSnapshot(
        source_path=source_tag,
        snapshot_date=today,
        fx_usd_nis=old.fx_usd_nis,
        fx_usd_eur=old.fx_usd_eur,
        positions=positions,
        real_estate=[r.model_copy(deep=True) for r in old.real_estate],
        allocations=_recompute_allocations(positions, old.allocations),
        nvda_sales=[s.model_copy(deep=True) for s in old.nvda_sales],
        pensions=[pe.model_copy(deep=True) for pe in old.pensions],
        parse_warnings=(
            fill_notes + list(result.warnings) + list(extra_warnings)
            + [expectations_blob]
        ),
    )
    result.new_total_usd_k = new_snap.total_usd_value_k

    row = persist_snapshot(session, user_id=user_id, snapshot=new_snap, commit=commit,
                           expected_prior_id=old_row.id, require_book_sync=True)
    result.row = row
    result.snapshot = new_snap
    _log.info(
        "snapshot_refresh.fills_applied",
        user_id=user_id,
        source_tag=source_tag,
        merged=result.merged,
        added=result.added,
        old_total_usd_k=round(result.old_total_usd_k, 2),
        new_total_usd_k=round(result.new_total_usd_k, 2),
        cash_after_local=result.cash_after_local,
        warnings=result.warnings,
    )
    return result


__all__ = [
    "ApplyFillsResult",
    "Fill",
    "RefreshResult",
    "apply_fills_to_snapshot",
    "default_fx_fn",
    "default_quote_fn",
    "refresh_portfolio_snapshot",
]
