"""Trend radar — sources high-potential / "trending" single-name candidates.

This is the SOURCING engine for the high-potential satellite sleeve. The hard
part the user named is finding high-growth names early ("ride trends"); this
fans out across independent, no-API-key signal families and only surfaces a
name when MORE THAN ONE family corroborates it (a pump guard) AND it clears a
liquidity floor.

Signal families (each best-effort, blind to the others):
  * MOMENTUM — yfinance predefined screeners (gainers / small-cap / growth-tech
    / most-active). The screener payload carries price/mktcap/volume, so no
    fragile per-ticker fetch is needed.
  * ATTENTION — ApeWisdom (Reddit + 4chan mention counts + 24h trend),
    StockTwits trending, Yahoo trending. A name is "rising" only if mentions
    grew vs 24h ago or its rank jumped — a level alone is not a signal.
  * GROWTH — Finviz fundamental screen (EPS-growth-next-5y > 15%, liquidity
    floors) via finvizfinance.
  * MOMENTUM_SUSTAINED — cross-sectional 12-1 relative strength (return from
    t-252 to t-21, skipping the most recent month) over a broad, hand-curated
    cross-sector universe (`_BROAD_MOMENTUM_UNIVERSE`), not the day-gainer
    screeners. Added 2026-08-21 after a look-ahead-free backtest
    (scripts/backtest_radar.py) showed the existing families have NO
    sustained-momentum signal and missed a +530% median-forward-return move
    entirely because it never spiked on a single day. A name with too little
    history for full 12-1 gets a short-window fallback, flagged
    ``insufficient_history`` rather than being silently dropped.

Scoring + guards (deterministic, see :func:`score_and_filter`):
  * weighted family score (MOMENTUM 35 / ATTENTION 30 / GROWTH 25 /
    MOMENTUM_SUSTAINED 40) plus a small same-day %-change bonus (capped 10);
  * pump guard: a name needs >= 2 families to reach the shortlist, in EITHER
    lane below;
  * liquidity filter, TWO LANES:
    - "satellite" (default lane): price >= $5, market cap in [cap_min,
      cap_max] (default $300M-$30B, the Ariel-approved moonshot band —
      proposal 68), $-volume/day >= $10M.
    - "thematic" (large-cap re-rating carve-out; OFF by default — see
      ``thematic_lane_enabled()`` / ``ARGOSY_THEMATIC_LANE_ENABLED``): market
      cap in [$30B, $500B], $-volume/day >= $25M. Candidates are tagged
      ``TrendCandidate.lane="thematic"`` end to end so downstream sizing
      never treats a $130bn name like a moonshot (see
      high_potential_sleeve.X10_SLEEVE_MANDATE).

Network I/O lives in the ``_gather_*`` helpers; the scoring core
(:func:`score_and_filter`) is pure and unit-tested without a network. The
single-name candidates this surfaces are US-situs by construction — they are
the small CARVE-OUT of the sleeve, never its core (which is UCITS thematic).
Every name here is meant to be paired with the live daily monitor +
stop-loss (see :mod:`argosy.services.speculative_monitor`); these are
high-risk, exit-disciplined positions, not buy-and-forget holds.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from argosy.logging import get_logger

log = get_logger(__name__)

# Family score weights.
#   MOMENTUM_SUSTAINED (40) outranks the day-gainer MOMENTUM family (35) on
#   purpose: the 2026-08-21 look-ahead-free backtest
#   (scripts/backtest_radar.py) showed the WDC/STX/MU winners median +530%
#   forward return vs +11% for a 14-name control, using 12-1 cross-sectional
#   relative strength alone — a stronger single signal than any of the
#   existing (spike-chasing) families. It still needs >=2-family
#   corroboration like every other family; it is not a bypass of the pump
#   guard.
_FAMILY_SCORE = {
    "MOMENTUM": 35.0,
    "ATTENTION": 30.0,
    "GROWTH": 25.0,
    "MOMENTUM_SUSTAINED": 40.0,
}

# Liquidity band defaults — tuned for a high-potential SATELLITE (small/mid cap
# with real upside, but tradeable). cap_max $30B aligns the radar band to the
# sleeve doctrine's sub-$30B moonshot gate (proposal 68, Ariel-approved
# 2026-07-10 — the prior $8B ceiling excluded the sleeve's own upper band);
# still far below the megacaps already in the core.
DEFAULT_MIN_PRICE = 5.0
DEFAULT_CAP_MIN = 300e6
DEFAULT_CAP_MAX = 30e9
DEFAULT_MIN_DOLLAR_VOLUME = 10e6

# --- THEMATIC lane (large-cap thematic re-ratings; e.g. MU at $130bn is a
# core-sleeve name, not a moonshot) --------------------------------------
# Deliberately does NOT touch the satellite band above (Ariel-approved
# 2026-07-10, proposal 68). This is a SECOND, separately-tagged lane so
# downstream sizing (high_potential_sleeve's X10_SLEEVE_MANDATE explicitly
# bans sizing a >$50B "maybe 2x" name like a moonshot) can treat it
# differently. Starts just above the satellite ceiling so the two bands
# tile without a gap or overlap.
THEMATIC_CAP_MIN = DEFAULT_CAP_MAX
THEMATIC_CAP_MAX = 500e9
THEMATIC_MIN_DOLLAR_VOLUME = 25e6  # large-caps are expected to be more liquid

# OFF by default — Ariel decides policy, we ship the capability. Turn on with:
#   ARGOSY_THEMATIC_LANE_ENABLED=true (or "1"/"yes")
_THEMATIC_ENV_VAR = "ARGOSY_THEMATIC_LANE_ENABLED"


def thematic_lane_enabled() -> bool:
    return os.environ.get(_THEMATIC_ENV_VAR, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_UA = {"User-Agent": "Mozilla/5.0 (Argosy trend-radar)"}


@dataclass
class RawSignal:
    """Mutable accumulator for one ticker across the signal families."""

    ticker: str
    name: str = ""
    price: float | None = None
    market_cap: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    pct_change: float | None = None
    families: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)
    # Set by _gather_momentum_sustained when a name has < LOOKBACK+SKIP days
    # of history and the short-window relative-strength fallback was used
    # instead of full 12-1 momentum (task C: newly-listed names must not be
    # silently dropped, but the number IS based on less data).
    insufficient_history: bool = False


@dataclass(frozen=True)
class TrendCandidate:
    """A scored, filtered trend candidate (immutable result)."""

    ticker: str
    name: str
    score: float
    families: tuple[str, ...]
    reasons: tuple[str, ...]
    price: float | None
    market_cap: float | None
    dollar_volume: float | None
    pct_change: float | None
    stream: str | None = None
    event_id: str | None = None
    evidence: dict | None = None
    # "satellite" (default, small/mid cap carve-out) or "thematic" (large-cap
    # re-rating lane, see THEMATIC_CAP_MIN/MAX above). Downstream sizing MUST
    # branch on this — a thematic-lane name is never sized like a moonshot.
    lane: str = "satellite"
    insufficient_history: bool = False


@dataclass(frozen=True)
class ScanResult:
    shortlist: tuple[TrendCandidate, ...]
    # (ticker, reason-it-was-quarantined)
    quarantine: tuple[tuple[str, str], ...]
    source_counts: dict[str, object]


@dataclass(frozen=True)
class LiquidityFilter:
    min_price: float = DEFAULT_MIN_PRICE
    cap_min: float = DEFAULT_CAP_MIN
    cap_max: float = DEFAULT_CAP_MAX
    min_dollar_volume: float = DEFAULT_MIN_DOLLAR_VOLUME

    def dollar_volume(self, sig: RawSignal) -> float | None:
        vol = sig.avg_volume or sig.volume
        if sig.price and vol:
            return sig.price * vol
        return None

    def passes(self, sig: RawSignal) -> bool:
        """A signal clears the liquidity floor. Unknown fields are tolerated
        (we only reject on a KNOWN-bad value), so a name missing a market cap
        is not silently dropped — the pump guard (>=2 families) is the
        primary quality bar."""
        if sig.price is not None and sig.price < self.min_price:
            return False
        if sig.market_cap is not None and (
            sig.market_cap < self.cap_min or sig.market_cap > self.cap_max
        ):
            return False
        dv = self.dollar_volume(sig)
        if dv is not None and dv < self.min_dollar_volume:
            return False
        return True


# The thematic lane's own band — see THEMATIC_CAP_MIN/MAX comment above.
THEMATIC_LIQUIDITY_FILTER = LiquidityFilter(
    min_price=DEFAULT_MIN_PRICE,
    cap_min=THEMATIC_CAP_MIN,
    cap_max=THEMATIC_CAP_MAX,
    min_dollar_volume=THEMATIC_MIN_DOLLAR_VOLUME,
)


# ---------------------------------------------------------------------------
# Pure scoring core (unit-tested, no network)
# ---------------------------------------------------------------------------


def score_signal(sig: RawSignal) -> float:
    """Weighted family score + capped same-day %-change bonus."""
    score = sum(_FAMILY_SCORE.get(f, 0.0) for f in sig.families)
    if sig.pct_change:
        score += min(10.0, abs(sig.pct_change) / 3.0)
    return round(score, 1)


def score_and_filter(
    universe: dict[str, RawSignal],
    *,
    filters: LiquidityFilter | None = None,
    thematic_filters: LiquidityFilter | None = None,
    thematic_lane_enabled: bool = False,
    source_counts: dict[str, object] | None = None,
    limit: int = 25,
) -> ScanResult:
    """Score the universe, apply the pump guard + liquidity filter.

    A name reaches the shortlist only with >= 2 corroborating families AND a
    clean liquidity profile IN AT LEAST ONE LANE. A name is tried against the
    satellite band first; if it fails ONLY because it is too large (and the
    thematic lane is enabled), it is retried against the thematic band and,
    if it passes there, tagged ``lane="thematic"`` instead of dropped.
    Attention-only names, or any name failing liquidity in every enabled
    lane, drop to the quarantine with a reason. Pure: no I/O.
    """
    filt = filters or LiquidityFilter()
    them_filt = thematic_filters or THEMATIC_LIQUIDITY_FILTER
    shortlist: list[TrendCandidate] = []
    quarantine: list[tuple[str, str]] = []

    for ticker, sig in universe.items():
        n_fams = len(sig.families)
        # Drop obvious non-tickers: a 1-2 char symbol with no price and no
        # corroborating MOMENTUM/GROWTH/MOMENTUM_SUSTAINED family is almost
        # always an ApeWisdom false positive ("A", "DD", "CEO"...).
        if (
            not sig.price
            and len(ticker) <= 2
            and "GROWTH" not in sig.families
            and "MOMENTUM" not in sig.families
            and "MOMENTUM_SUSTAINED" not in sig.families
        ):
            quarantine.append((ticker, "ambiguous-short-symbol"))
            continue
        satellite_ok = filt.passes(sig)
        thematic_ok = thematic_lane_enabled and them_filt.passes(sig)
        lane = "satellite" if satellite_ok else ("thematic" if thematic_ok else None)
        liquid = satellite_ok or thematic_ok
        if n_fams >= 2 and lane is not None:
            active_filt = filt if lane == "satellite" else them_filt
            shortlist.append(
                TrendCandidate(
                    ticker=ticker,
                    name=sig.name,
                    score=score_signal(sig),
                    families=tuple(sorted(sig.families)),
                    reasons=tuple(sig.reasons[:5]),
                    price=sig.price,
                    market_cap=sig.market_cap,
                    dollar_volume=active_filt.dollar_volume(sig),
                    pct_change=sig.pct_change,
                    lane=lane,
                    insufficient_history=sig.insufficient_history,
                )
            )
        elif sig.families == {"ATTENTION"}:
            quarantine.append((ticker, "attention-only"))
        elif not liquid:
            quarantine.append((ticker, "failed-liquidity"))
        # single-family momentum/growth without corroboration: silently held
        # back (not interesting enough to surface, not noteworthy enough to log)

    shortlist.sort(key=lambda c: -c.score)
    return ScanResult(
        shortlist=tuple(shortlist[:limit]),
        quarantine=tuple(quarantine),
        source_counts=source_counts or {},
    )


# ---------------------------------------------------------------------------
# Network source helpers (best-effort; each failure is isolated + logged)
# ---------------------------------------------------------------------------


def _get_json(url: str, timeout: int = 20) -> object:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


def _rec(universe: dict[str, RawSignal], ticker: str) -> RawSignal:
    t = (ticker or "").upper().strip()
    return universe.setdefault(t, RawSignal(ticker=t))


def _gather_yfinance_screeners(
    universe: dict[str, RawSignal], counts: dict[str, object]
) -> None:
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        counts["yfinance"] = f"import-error: {exc!r}"[:80]
        return
    for scr in (
        "aggressive_small_caps", "small_cap_gainers", "day_gainers",
        "growth_technology_stocks", "most_actives",
    ):
        try:
            res = yf.screen(scr, count=50)
            quotes = res.get("quotes", []) if isinstance(res, dict) else (res or [])
            counts[f"yf:{scr}"] = len(quotes)
            for q in quotes:
                t = q.get("symbol")
                if not t:
                    continue
                r = _rec(universe, t)
                r.name = r.name or (q.get("shortName") or q.get("longName") or "")
                r.price = q.get("regularMarketPrice") or r.price
                r.market_cap = q.get("marketCap") or r.market_cap
                r.volume = q.get("regularMarketVolume") or r.volume
                r.avg_volume = q.get("averageDailyVolume3Month") or r.avg_volume
                if q.get("regularMarketChangePercent") is not None:
                    r.pct_change = q.get("regularMarketChangePercent")
                r.families.add("MOMENTUM")
                if f"yf:{scr}" not in r.reasons:
                    r.reasons.append(f"yf:{scr}")
        except Exception as exc:  # noqa: BLE001
            counts[f"yf:{scr}"] = f"error: {exc!r}"[:60]


def _gather_apewisdom(
    universe: dict[str, RawSignal], counts: dict[str, object]
) -> None:
    agg: dict[str, dict[str, float]] = {}
    for filt in ("all-stocks", "wallstreetbets", "4chan"):
        try:
            for page in (1, 2):
                d = _get_json(
                    f"https://apewisdom.io/api/v1.0/filter/{filt}/page/{page}"
                )
                rows = d.get("results", []) if isinstance(d, dict) else []
                for row in rows:
                    t = (row.get("ticker") or "").upper()
                    if not t:
                        continue
                    m = row.get("mentions") or 0
                    m24 = row.get("mentions_24h_ago") or 0
                    drank = (row.get("rank_24h_ago") or 0) - (row.get("rank") or 0)
                    prev = agg.get(t, {"m": 0, "m24": 0, "drank": 0})
                    agg[t] = {
                        "m": max(prev["m"], m),
                        "m24": max(prev["m24"], m24),
                        "drank": max(prev["drank"], drank),
                    }
            counts[f"apewisdom:{filt}"] = "ok"
        except Exception as exc:  # noqa: BLE001
            counts[f"apewisdom:{filt}"] = f"error: {exc!r}"[:60]
    for t, a in agg.items():
        rising = (a["m"] > a["m24"] * 1.3 and a["m"] >= 15) or a["drank"] >= 8
        if rising:
            r = _rec(universe, t)
            r.families.add("ATTENTION")
            note = f"reddit:{int(a['m24'])}->{int(a['m'])}m"
            if a["drank"] >= 8:
                note += f",rank+{int(a['drank'])}"
            r.reasons.append(note)


def _gather_stocktwits(
    universe: dict[str, RawSignal], counts: dict[str, object]
) -> None:
    try:
        d = _get_json("https://api.stocktwits.com/api/2/trending/symbols.json")
        syms = d.get("symbols", []) if isinstance(d, dict) else []
        counts["stocktwits"] = len(syms)
        for s in syms:
            t = (s.get("symbol") or "").upper()
            if not t:
                continue
            r = _rec(universe, t)
            r.families.add("ATTENTION")
            r.reasons.append(f"stocktwits(wl={s.get('watchlist_count')})")
    except Exception as exc:  # noqa: BLE001
        counts["stocktwits"] = f"error: {exc!r}"[:60]


def _gather_yahoo_trending(
    universe: dict[str, RawSignal], counts: dict[str, object]
) -> None:
    try:
        d = _get_json(
            "https://query1.finance.yahoo.com/v1/finance/trending/US?count=25"
        )
        qs = d["finance"]["result"][0].get("quotes", [])
        counts["yahoo_trending"] = len(qs)
        for q in qs:
            t = (q.get("symbol") or "").upper()
            if not t:
                continue
            r = _rec(universe, t)
            r.families.add("ATTENTION")
            r.reasons.append("yahoo_trending")
    except Exception as exc:  # noqa: BLE001
        counts["yahoo_trending"] = f"error: {exc!r}"[:60]


def _gather_finviz_growth(
    universe: dict[str, RawSignal], counts: dict[str, object]
) -> None:
    try:
        from finvizfinance.screener.overview import Overview

        ov = Overview()
        ov.set_filter(filters_dict={
            "Market Cap.": "+Small (over $300mln)",
            "EPS growthnext 5 years": "Over 15%",
            "Average Volume": "Over 500K",
            "Price": "Over $5",
        })
        df = ov.screener_view(order="Change", limit=40, verbose=0)
        counts["finviz_growth"] = 0 if df is None else len(df)
        if df is not None:
            for t in df["Ticker"].tolist():
                r = _rec(universe, str(t).upper())
                r.families.add("GROWTH")
                r.reasons.append("finviz:eps_growth>15%")
    except Exception as exc:  # noqa: BLE001
        counts["finviz_growth"] = f"error: {exc!r}"[:80]


# --- MOMENTUM_SUSTAINED: cross-sectional 12-1 relative strength -----------
#
# Cross-sector static roster used to broaden the momentum universe beyond
# whatever the day-gainer screeners happen to surface that day — the whole
# point of this family: WDC/STX/MU never showed up in the gainers screens
# during their multi-month re-rating (scripts/backtest_radar.py, 2026-08-21).
# No paid data source; hand-maintained, refresh occasionally. Deliberately
# spans many sectors (not just semis/storage) so the family isn't just "the
# names that already won" replayed on the next cycle.
_BROAD_MOMENTUM_UNIVERSE: tuple[str, ...] = (
    # semis / storage / memory
    "MU", "WDC", "STX", "SNDK", "NVDA", "AMD", "INTC", "TSM", "AVGO", "QCOM",
    "MRVL", "ON", "MCHP", "TXN", "LRCX", "AMAT", "KLAC", "ASML",
    # mega-cap tech
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NFLX", "CRM", "ORCL", "ADBE",
    # industrials / materials
    "CAT", "DE", "BA", "GE", "HON", "LMT", "RTX", "FCX", "NUE",
    # financials
    "JPM", "BAC", "GS", "MS", "V", "MA", "AXP", "SCHW",
    # healthcare
    "UNH", "LLY", "PFE", "MRK", "ABBV", "TMO", "ISRG",
    # consumer
    "COST", "WMT", "HD", "NKE", "SBUX", "MCD", "DIS", "TGT",
    # energy
    "XOM", "CVX", "COP", "SLB",
    # comms / media
    "T", "TMUS", "CMCSA",
)

_MOMENTUM_LOOKBACK = 252   # 12 months of trading days
_MOMENTUM_SKIP = 21        # skip most recent month (standard 12-1 construction)
_MOMENTUM_MIN_HISTORY = 60  # newly-listed fallback floor (task C)
_MOMENTUM_TOP_PCTILE = 0.80  # long the top quintile cross-sectionally
_MOMENTUM_CAP_FILL_LIMIT = 25  # cap extra per-ticker fast_info calls (sane count)

# In-process, date-keyed cache: repeat calls the same day (multiple funnel
# runs) don't re-download the whole universe's history.
_MOM_HISTORY_CACHE: dict[tuple, object] = {}


def _momentum_history(tickers: tuple[str, ...]):
    """Batch-download ~15mo of daily OHLCV for ``tickers``, cached per-day."""
    key = (date.today().isoformat(), tickers)
    cached = _MOM_HISTORY_CACHE.get(key)
    if cached is not None:
        return cached
    import yfinance as yf

    df = yf.download(
        list(tickers), period="15mo", interval="1d", group_by="ticker",
        auto_adjust=True, threads=True, progress=False,
    )
    _MOM_HISTORY_CACHE.clear()  # only ever keep the latest day's fetch
    _MOM_HISTORY_CACHE[key] = df
    return df


def _ticker_frame(hist, ticker: str):
    """Pull one ticker's OHLCV out of a (possibly multi-ticker) yf.download
    frame. Returns None if the ticker is missing (delisted / fetch miss)."""
    import pandas as pd

    if isinstance(hist.columns, pd.MultiIndex):
        top = hist.columns.get_level_values(0)
        if ticker not in top:
            return None
        return hist[ticker]
    return hist  # single-ticker frame (only possible if len(tickers) == 1)


def _gather_momentum_sustained(
    universe: dict[str, RawSignal], counts: dict[str, object]
) -> None:
    """MOMENTUM_SUSTAINED: cross-sectional 12-1 relative strength (return from
    t-252 to t-21, skipping the most recent month — the standard construction
    that avoids short-term reversal) over a BROAD universe (today's other
    gather hits + the static cross-sector roster above), not the day-gainer
    screeners. A name with < LOOKBACK+SKIP days of history gets a
    short-window relative-strength fallback instead of being silently
    dropped (task C), flagged ``insufficient_history`` so the fleet knows the
    number is based on less data. Pure arithmetic on price only — never
    invents a price or cap; missing data is excluded with a reason.
    """
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        counts["momentum_sustained"] = f"import-error: {exc!r}"[:80]
        return

    tickers = tuple(sorted(set(universe.keys()) | set(_BROAD_MOMENTUM_UNIVERSE)))
    try:
        hist = _momentum_history(tickers)
    except Exception as exc:  # noqa: BLE001
        counts["momentum_sustained"] = f"error: {exc!r}"[:80]
        return

    # ticker -> (momentum, insufficient_history, price, dollar_volume)
    scored: dict[str, tuple[float, bool, float, float | None]] = {}
    for t in tickers:
        sub = _ticker_frame(hist, t)
        if sub is None or "Close" not in sub:
            continue
        closes = sub["Close"].dropna()
        vols = sub["Volume"].dropna() if "Volume" in sub else None
        if closes.empty:
            continue
        px = float(closes.iloc[-1])
        vol20 = float(vols.tail(20).mean()) if vols is not None and not vols.empty else None
        dollar_vol = px * vol20 if vol20 else None
        insufficient = False
        if len(closes) >= _MOMENTUM_LOOKBACK + _MOMENTUM_SKIP:
            p_start = float(closes.iloc[-(_MOMENTUM_LOOKBACK + _MOMENTUM_SKIP)])
            p_end = float(closes.iloc[-_MOMENTUM_SKIP])
        elif len(closes) >= _MOMENTUM_MIN_HISTORY:
            # Newly-listed fallback: whatever window is available, still
            # skip a short recent slice to dodge same-week reversal noise.
            skip = min(5, max(1, len(closes) // 10))
            p_start = float(closes.iloc[0])
            p_end = float(closes.iloc[-skip])
            insufficient = True
        else:
            continue  # genuinely insufficient data -> excluded, not guessed
        if p_start <= 0:
            continue
        scored[t] = (p_end / p_start - 1.0, insufficient, px, dollar_vol)

    counts["momentum_sustained"] = len(scored)
    if not scored:
        return

    ranked = sorted(scored.items(), key=lambda kv: kv[1][0])
    cutoff_idx = int(len(ranked) * _MOMENTUM_TOP_PCTILE)
    leaders = ranked[cutoff_idx:]  # top (1 - pctile) cross-sectionally

    filled_caps = 0
    for t, (mom, insufficient, px, dollar_vol) in leaders:
        r = _rec(universe, t)
        r.families.add("MOMENTUM_SUSTAINED")
        r.price = r.price or px
        if dollar_vol is not None and r.avg_volume is None and px:
            r.avg_volume = dollar_vol / px
        r.insufficient_history = r.insufficient_history or insufficient
        tag = "12-1" if not insufficient else "short-window-fallback(insufficient_history)"
        r.reasons.append(f"mom_sustained:{tag}:{mom:+.0%}")
        # Backfill market cap only for names not already seen by another
        # gatherer, and only within the (small) leader set — keeps the extra
        # per-ticker request count sane.
        if r.market_cap is None and filled_caps < _MOMENTUM_CAP_FILL_LIMIT:
            filled_caps += 1
            try:
                fi = yf.Ticker(t).fast_info
                mc = getattr(fi, "market_cap", None)
                if mc:
                    r.market_cap = float(mc)
            except Exception:  # noqa: BLE001
                pass


def _conviction_for(score: float) -> str:
    if score >= 70.0:
        return "HIGH"
    if score >= 55.0:
        return "MEDIUM"
    return "LOW"


def to_sleeve_candidates(
    candidates: "tuple[TrendCandidate, ...] | list[TrendCandidate]",
    *,
    max_names: int = 4,
    held_tickers: frozenset[str] | None = None,
):
    """Map scored trend candidates onto the sleeve's ``SleeveCandidate`` shape.

    These are the single-name CARVE-OUT of the high-potential sleeve — US-situs
    by construction (the user accepts the estate-tax hit on this small slice;
    the sleeve CORE stays UCITS thematic). Conviction is derived from the radar
    score, never hand-picked. Each thesis names the corroborating families and
    flags the mandatory exit discipline (these are monitor-and-stop-loss
    positions, not buy-and-hold).

    Only ``lane="satellite"`` candidates are mapped here — this is the
    moonshot carve-out (SleeveCandidate has no lane field of its own), and
    X10_SLEEVE_MANDATE explicitly bans sizing a large "maybe 2x" name like a
    moonshot. Thematic-lane candidates (see ``TrendCandidate.lane``) are for
    a future core/growth-sleeve consumer, not this one.
    """
    from argosy.services.high_potential_sleeve import SleeveCandidate

    held = held_tickers or frozenset()
    pool = [c for c in candidates if c.lane == "satellite"]
    out: list[SleeveCandidate] = []
    for c in pool[:max_names]:
        fam = ", ".join(c.families).lower()
        why = "; ".join(c.reasons[:3])
        hist_note = (
            " Momentum figure uses a short-window fallback (newly listed, "
            "insufficient 12-1 history) — treat as lower-confidence."
            if c.insufficient_history else ""
        )
        out.append(SleeveCandidate(
            ticker=c.ticker,
            name=c.name or c.ticker,
            vehicle="single_name",
            conviction=_conviction_for(c.score),  # type: ignore[arg-type]
            us_situs=True,
            held_today=c.ticker.upper() in held,
            source="trend_radar",
            thesis=(
                f"Trend-radar pick (score {c.score:.0f}/100): corroborated by "
                f"{fam} signal families [{why}]. High-risk satellite carve-out — "
                "MUST be paired with the live daily monitor + trailing stop-loss; "
                "exit on a thesis break or stop trigger, not buy-and-hold. "
                "US-situs single name (small accepted estate-tax slice)."
                f"{hist_note}"
            ),
        ))
    return tuple(out)


def scan_trends(
    *,
    filters: LiquidityFilter | None = None,
    thematic_filters: LiquidityFilter | None = None,
    thematic_lane: bool | None = None,
    limit: int = 25,
) -> ScanResult:
    """Run every signal family, then score + filter. Network-bound; each
    source is best-effort so a single dead endpoint never aborts the scan.

    ``thematic_lane`` defaults to ``thematic_lane_enabled()`` (env-gated, OFF
    by default) when not explicitly passed.
    """
    universe: dict[str, RawSignal] = {}
    counts: dict[str, object] = {}
    _gather_yfinance_screeners(universe, counts)
    _gather_apewisdom(universe, counts)
    _gather_stocktwits(universe, counts)
    _gather_yahoo_trending(universe, counts)
    _gather_finviz_growth(universe, counts)
    _gather_momentum_sustained(universe, counts)
    lane_on = thematic_lane if thematic_lane is not None else thematic_lane_enabled()
    result = score_and_filter(
        universe,
        filters=filters,
        thematic_filters=thematic_filters,
        thematic_lane_enabled=lane_on,
        source_counts=counts,
        limit=limit,
    )
    log.info(
        "trend_radar.scan_done",
        sources=counts,
        shortlist=len(result.shortlist),
        quarantine=len(result.quarantine),
        thematic_lane_enabled=lane_on,
    )
    return result


__all__ = [
    "RawSignal",
    "TrendCandidate",
    "ScanResult",
    "LiquidityFilter",
    "THEMATIC_LIQUIDITY_FILTER",
    "THEMATIC_CAP_MIN",
    "THEMATIC_CAP_MAX",
    "thematic_lane_enabled",
    "score_signal",
    "score_and_filter",
    "scan_trends",
    "to_sleeve_candidates",
]
