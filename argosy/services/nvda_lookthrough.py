"""Economic NVDA look-through — the TRUE single-name concentration.

The deconcentration plan holds NVDA at a **12% DIRECT** ceiling, but the
diversifying equity sleeves are broad-market UCITS funds that THEMSELVES hold
NVDA (the Russell 1000 Growth index R1GR is ~14% NVDA; the S&P 500 CSPX and the
dividend-quality FUSA are ~8% each). So the household's *economic* exposure to a
single NVDA drawdown exceeds the 12% direct figure. There is no UCITS
true-ex-NVDA growth ETF, so the pass-through cannot be designed away — the honest
move is to MEASURE and SURFACE it (auto-memory ``feedback_output_trust_doctrine``:
every surfaced number must be Argosy-derived + auditable, never a fabricated
estimate).

Two layers, kept separate so the math is unit-testable in isolation:

1. :func:`compute_economic_nvda` — a PURE function. Given each sleeve's weight
   in the full book and a map of each fund's internal NVDA weight, it returns the
   direct + indirect + economic breakdown with a per-sleeve audit trail. It never
   fabricates: a fund whose holdings could not be fetched is reported as
   ``unresolved`` and EXCLUDED from the indirect sum (so the economic figure is an
   honest lower bound with the gap flagged), never silently treated as 0.

2. :func:`fetch_fund_nvda_weights` + :func:`compute_nvda_lookthrough` — the I/O
   layer that reads the current plan's sleeves, fetches live fund holdings from
   yfinance (cached in ``kv_cache`` with a multi-day TTL — fund compositions drift
   slowly), and calls the pure function.

Disclosed method limitations (auditable, not silent — codex review #4/#8/#9):
  * In-fund NVDA weight is read from yfinance ``top_holdings`` (the TOP TEN
    holdings). For the current book every NVDA-holding equity sleeve carries
    NVDA inside its top ten (it is a mega-cap), and the resolved-0 sleeves are
    ex-US / EM / property mandates that cannot hold it — so the figure is exact.
    For a hypothetical equity fund holding NVDA just BELOW its top ten, the
    weight would read 0; that residual is bounded by ``sleeve_weight × the
    smallest top-ten weight`` and is immaterial for broad funds.
  * Non-equity sleeves are excluded by ``sigma_class`` (cash / bonds /
    alternatives). A future *alternatives* sleeve with genuine equity/NVDA
    exposure would need instrument-level metadata to be looked through.
  * The yfinance ticker-suffix fallback accepts the first spelling that returns
    holdings; the canonical UCITS symbols resolve unambiguously today.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from argosy.logging import get_logger

log = get_logger(__name__)

DIRECT_SYMBOL = "NVDA"

# Sleeve sigma-classes that hold NO equities and therefore no NVDA — skipped so
# a gold ETC / bond / cash fund (which has no equity holdings to fetch) is not
# spuriously flagged "unresolved". Everything else is treated as an equity sleeve
# whose NVDA pass-through is measured from live holdings (and resolves to 0 when
# the fund genuinely holds no NVDA, e.g. an ex-US or EM index).
NON_EQUITY_SIGMA_CLASSES: frozenset[str] = frozenset({"cash", "bonds", "alternatives"})

# yfinance ticker-suffix candidates for UCITS funds (LSE first, then Amsterdam).
_YF_SUFFIXES: tuple[str, ...] = (".L", ".AS", ".MI", ".DE", "")

# kv_cache namespacing. Fund holdings change on (roughly) quarterly rebalances
# plus slow drift; a multi-day TTL keeps the look-through fresh without hammering
# yfinance on every page load.
_CACHE_PROVIDER = "yfinance_fund_holdings"
_CACHE_TTL_SECONDS = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Pure model + computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentWeight:
    """One plan instrument and its weight in the FULL tradeable book (percent
    points, e.g. 30.60 for a 30.6% sleeve)."""

    symbol: str
    class_label: str
    portfolio_weight_pct: float


@dataclass(frozen=True)
class SleeveContribution:
    """The NVDA pass-through from one fund sleeve (the per-sleeve audit row)."""

    symbol: str
    class_label: str
    portfolio_weight_pct: float
    # NVDA's weight INSIDE this fund (percent points), or None when the fund's
    # holdings could not be fetched (unresolved — never assumed 0).
    nvda_weight_in_fund_pct: float | None
    # portfolio_weight_pct * nvda_weight_in_fund_pct / 100 (percent points of the
    # full book), or None when unresolved.
    nvda_contribution_pct: float | None
    source: str


@dataclass(frozen=True)
class NvdaLookthrough:
    """The economic-NVDA breakdown. ``economic_pct`` counts only RESOLVED
    sleeves; ``fully_resolved`` is False (and ``unresolved_symbols`` non-empty)
    when a fund's holdings could not be fetched, so the UI can show the figure as
    a lower bound rather than overstate certainty."""

    direct_pct: float
    indirect_pct: float
    economic_pct: float
    contributions: list[SleeveContribution] = field(default_factory=list)
    unresolved_symbols: list[str] = field(default_factory=list)
    as_of: str | None = None
    fully_resolved: bool = True


def compute_economic_nvda(
    instruments: list[InstrumentWeight],
    holdings_map: dict[str, tuple[float | None, str | None]],
    *,
    direct_symbol: str = DIRECT_SYMBOL,
    as_of: str | None = None,
) -> NvdaLookthrough:
    """Pure economic-NVDA computation.

    Args:
        instruments: every plan instrument with its full-book weight (percent
            points). The instrument whose symbol == ``direct_symbol`` is the
            direct NVDA position.
        holdings_map: ``{fund_symbol_upper: (nvda_weight_in_fund_pct | None,
            source)}``. A symbol present with a float weight (0.0 = fund holds no
            NVDA) is RESOLVED; a symbol ABSENT from the map is UNRESOLVED (its
            sleeve appears in ``unresolved_symbols`` and is excluded from the
            indirect sum). The source string is carried into the audit row.

    Returns:
        :class:`NvdaLookthrough` — direct + indirect + economic, per-sleeve
        contributions, and the unresolved set.
    """
    direct_pct = 0.0
    contributions: list[SleeveContribution] = []
    unresolved: list[str] = []
    indirect = 0.0

    for inst in instruments:
        sym = inst.symbol.upper()
        if sym == direct_symbol.upper():
            direct_pct += inst.portfolio_weight_pct
            continue
        entry = holdings_map.get(sym)
        if entry is None:
            # Sleeve we attempted but could not resolve → flag, never assume 0.
            unresolved.append(inst.symbol)
            contributions.append(SleeveContribution(
                symbol=inst.symbol, class_label=inst.class_label,
                portfolio_weight_pct=inst.portfolio_weight_pct,
                nvda_weight_in_fund_pct=None, nvda_contribution_pct=None,
                source="unresolved (fund holdings unavailable)",
            ))
            continue
        nvda_w, src = entry
        if nvda_w is None:
            unresolved.append(inst.symbol)
            contributions.append(SleeveContribution(
                symbol=inst.symbol, class_label=inst.class_label,
                portfolio_weight_pct=inst.portfolio_weight_pct,
                nvda_weight_in_fund_pct=None, nvda_contribution_pct=None,
                source=src or "unresolved",
            ))
            continue
        contrib = inst.portfolio_weight_pct * nvda_w / 100.0
        indirect += contrib
        contributions.append(SleeveContribution(
            symbol=inst.symbol, class_label=inst.class_label,
            portfolio_weight_pct=inst.portfolio_weight_pct,
            nvda_weight_in_fund_pct=nvda_w, nvda_contribution_pct=contrib,
            source=src or "yfinance fund top_holdings",
        ))

    economic = direct_pct + indirect
    # Sort the audit rows by contribution desc (unresolved/None last).
    contributions.sort(
        key=lambda c: (c.nvda_contribution_pct is None, -(c.nvda_contribution_pct or 0.0))
    )
    # De-dup the unresolved list (a symbol can appear in two classes) while
    # preserving first-seen order — display hygiene (codex review #2).
    unresolved = list(dict.fromkeys(unresolved))
    return NvdaLookthrough(
        direct_pct=round(direct_pct, 4),
        indirect_pct=round(indirect, 4),
        economic_pct=round(economic, 4),
        contributions=contributions,
        unresolved_symbols=unresolved,
        as_of=as_of,
        fully_resolved=not unresolved,
    )


# ---------------------------------------------------------------------------
# I/O layer — live fund holdings (yfinance) + kv_cache + plan-doc loading
# ---------------------------------------------------------------------------


def _nvda_pct_from_holdings(holdings: object) -> float | None:
    """NVDA's in-fund weight (percent points) from a yfinance holdings table, or
    None when the table is unusable.

    Holdings weights are FRACTIONS (0.0788). Returns:
      * None  — when the table is empty / not a dict / NVDA's value is
        non-numeric or outside [0, 1] (corrupt). The caller refetches or flags
        unresolved rather than silently reading 0 (codex review #3 + #5).
      * 0.0   — a NON-empty table that simply lacks NVDA: a genuine resolved-0
        (an ex-US / EM / property mandate that cannot hold NVDA).
      * >0    — NVDA's weight × 100.
    """
    if not isinstance(holdings, dict) or not holdings:
        return None
    raw = holdings.get(DIRECT_SYMBOL, 0.0)
    try:
        frac = float(raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= frac <= 1.0):
        return None
    return round(frac * 100.0, 4)


def _fetch_fund_top_holdings_live(symbol: str) -> tuple[dict[str, float], str] | None:
    """Fetch a fund's top holdings from yfinance, trying UCITS ticker suffixes.

    Returns ``({underlying_symbol_upper: weight_fraction}, yf_ticker)`` or None
    when no spelling yields holdings. Weights are FRACTIONS (0.0788), as yfinance
    returns them.
    """
    try:
        import yfinance as yf
    except Exception:  # noqa: BLE001 — yfinance unavailable
        return None
    for suffix in _YF_SUFFIXES:
        yf_ticker = f"{symbol}{suffix}"
        try:
            fd = getattr(yf.Ticker(yf_ticker), "funds_data", None)
            if fd is None:
                continue
            th = fd.top_holdings  # pandas DataFrame indexed by holding symbol
            if th is None or len(th) == 0 or "Holding Percent" not in th.columns:
                continue
            holdings: dict[str, float] = {}
            for idx, pct in th["Holding Percent"].items():
                try:
                    holdings[str(idx).upper()] = float(pct)
                except (TypeError, ValueError):
                    continue
            if holdings:
                return holdings, yf_ticker
        except Exception:  # noqa: BLE001 — 404 / parse error for this spelling
            continue
    return None


def fetch_fund_nvda_weights(
    symbols: list[str], *, today: date | None = None
) -> dict[str, tuple[float | None, str]]:
    """Resolve each fund's INTERNAL NVDA weight (percent points), cached.

    Reads/writes ``kv_cache`` (provider ``yfinance_fund_holdings``, key = the
    symbol) via a short-lived sync engine — the same bridge ``purge_cache_entry``
    uses — so this can be called from sync resolver / derived-inputs code. On a
    cache miss the live holdings are fetched and persisted; on a fetch failure the
    symbol maps to ``(None, reason)`` (unresolved → the pure fn excludes it).

    Returns ``{symbol_upper: (nvda_weight_in_fund_pct | None, source)}``.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from argosy.config import get_settings
    from argosy.state.models import KvCacheEntry

    today = today or datetime.now(timezone.utc).date()
    now = datetime.now(timezone.utc)
    out: dict[str, tuple[float | None, str]] = {}

    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})

    try:
        with Session(engine) as s:
            for raw in symbols:
                sym = raw.upper()
                row = s.execute(
                    select(KvCacheEntry).where(
                        (KvCacheEntry.provider == _CACHE_PROVIDER)
                        & (KvCacheEntry.key == sym)
                    )
                ).scalar_one_or_none()
                expires = row.expires_at if row else None
                if expires is not None and expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if row is not None and expires is not None and expires > now:
                    try:
                        payload = json.loads(row.payload_json)
                        holdings = payload.get("holdings")
                        nvda_pct = _nvda_pct_from_holdings(holdings)
                        # A usable (non-empty, in-range) cached table → use it.
                        # An empty/corrupt one returns None: fall through and
                        # refetch rather than silently read resolved-0 (codex #3).
                        if nvda_pct is not None:
                            src = f"yfinance fund top_holdings ({payload.get('yf_ticker', sym)}, cached {payload.get('as_of', '?')})"
                            out[sym] = (nvda_pct, src)
                            continue
                    except Exception:  # noqa: BLE001 — corrupt cache → refetch
                        pass

                fetched = _fetch_fund_top_holdings_live(sym)
                if fetched is None:
                    log.warning("nvda_lookthrough.fund_holdings_unavailable symbol=%s", sym)
                    out[sym] = (None, "unresolved (yfinance holdings unavailable)")
                    continue
                holdings, yf_ticker = fetched
                nvda_pct = _nvda_pct_from_holdings(holdings)
                if nvda_pct is None:
                    # Live table came back empty/corrupt → unresolved, don't cache.
                    log.warning("nvda_lookthrough.fund_holdings_empty symbol=%s", sym)
                    out[sym] = (None, "unresolved (fund holdings empty/corrupt)")
                    continue
                payload = {
                    "holdings": holdings,
                    "yf_ticker": yf_ticker,
                    "as_of": today.isoformat(),
                }
                payload_json = json.dumps(payload, default=str)
                payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                expires_at = now + timedelta(seconds=_CACHE_TTL_SECONDS)
                if row is None:
                    s.add(KvCacheEntry(
                        provider=_CACHE_PROVIDER, key=sym, payload_json=payload_json,
                        retrieved_at=now, expires_at=expires_at, payload_hash=payload_hash,
                    ))
                else:
                    row.payload_json = payload_json
                    row.retrieved_at = now
                    row.expires_at = expires_at
                    row.payload_hash = payload_hash
                out[sym] = (
                    nvda_pct,
                    f"yfinance fund top_holdings ({yf_ticker}, {today.isoformat()})",
                )
            s.commit()
    finally:
        engine.dispose()
    return out


def _plan_instruments(doc) -> tuple[list[InstrumentWeight], list[str]]:
    """Build the full-book instrument list + the equity-fund symbols to fetch.

    Each instrument's full-book weight = class.target_pct × weight_within_class.
    The direct NVDA position is included (handled by the pure fn); fund symbols to
    look through are the EQUITY-sleeve instruments excluding the direct NVDA and
    the non-equity classes (cash / bonds / gold — no NVDA to pass through).
    """
    instruments: list[InstrumentWeight] = []
    fetch_symbols: list[str] = []
    for c in doc.classes:
        is_non_equity = (c.sigma_class or "").lower() in NON_EQUITY_SIGMA_CLASSES
        for inst in c.instruments:
            if inst.role == "exit":
                continue
            sym = inst.symbol.upper()
            # Non-equity sleeves (cash / bonds / gold) hold no NVDA by
            # definition — exclude them from the look-through entirely so they
            # neither clutter the breakdown nor register as "unresolved". The
            # direct NVDA position and the equity funds are what matter.
            if is_non_equity and sym != DIRECT_SYMBOL:
                continue
            w = c.target_pct * float(inst.weight_within_class_pct) / 100.0
            instruments.append(InstrumentWeight(inst.symbol, c.label, round(w, 4)))
            if sym != DIRECT_SYMBOL:
                fetch_symbols.append(inst.symbol)
    return instruments, fetch_symbols


def compute_nvda_lookthrough(
    session, *, user_id: str, today: date | None = None
) -> NvdaLookthrough | None:
    """Economic-NVDA look-through for the user's CURRENT plan.

    Loads the current (or freshest draft) plan's target allocation, fetches each
    equity sleeve's internal NVDA weight (cached), and computes the breakdown.
    Returns None when there is no plan composition to read.
    """
    from sqlalchemy import desc, select

    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.models import PlanVersion

    today = today or datetime.now(timezone.utc).date()
    pv = session.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id, PlanVersion.role == "current")
        .order_by(desc(PlanVersion.id)).limit(1)
    ).scalar_one_or_none()
    if pv is None:
        pv = session.execute(
            select(PlanVersion)
            .where(PlanVersion.user_id == user_id, PlanVersion.role == "draft")
            .order_by(desc(PlanVersion.id)).limit(1)
        ).scalar_one_or_none()
    if pv is None:
        return None
    doc = load_plan_target_allocation(pv)
    if doc is None or not doc.classes:
        return None

    instruments, fetch_symbols = _plan_instruments(doc)
    holdings_map = fetch_fund_nvda_weights(fetch_symbols, today=today) if fetch_symbols else {}
    return compute_economic_nvda(instruments, holdings_map, as_of=today.isoformat())


__all__ = [
    "InstrumentWeight",
    "SleeveContribution",
    "NvdaLookthrough",
    "compute_economic_nvda",
    "fetch_fund_nvda_weights",
    "compute_nvda_lookthrough",
    "DIRECT_SYMBOL",
    "NON_EQUITY_SIGMA_CLASSES",
]
