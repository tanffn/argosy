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

Scoring + guards (deterministic, see :func:`score_and_filter`):
  * weighted family score (MOMENTUM 35 / ATTENTION 30 / GROWTH 25) plus a
    small same-day %-change bonus (capped 10);
  * pump guard: a name needs >= 2 families to reach the shortlist;
  * liquidity filter: price >= $5, market cap in [cap_min, cap_max] (default
    $300M–$8B for a satellite-appropriate band), $-volume/day >= $10M.

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
import urllib.request
from dataclasses import dataclass, field

from argosy.logging import get_logger

log = get_logger(__name__)

# Family score weights.
_FAMILY_SCORE = {"MOMENTUM": 35.0, "ATTENTION": 30.0, "GROWTH": 25.0}

# Liquidity band defaults — tuned for a high-potential SATELLITE (small/mid cap
# with real upside, but tradeable). cap_max deliberately tight ($8B) so the
# radar surfaces genuine high-growth names, not megacaps already in the core.
DEFAULT_MIN_PRICE = 5.0
DEFAULT_CAP_MIN = 300e6
DEFAULT_CAP_MAX = 8e9
DEFAULT_MIN_DOLLAR_VOLUME = 10e6

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
    source_counts: dict[str, object] | None = None,
    limit: int = 25,
) -> ScanResult:
    """Score the universe, apply the pump guard + liquidity filter.

    A name reaches the shortlist only with >= 2 corroborating families AND a
    clean liquidity profile. Attention-only names, or any name failing
    liquidity, drop to the quarantine with a reason. Pure: no I/O.
    """
    filt = filters or LiquidityFilter()
    shortlist: list[TrendCandidate] = []
    quarantine: list[tuple[str, str]] = []

    for ticker, sig in universe.items():
        n_fams = len(sig.families)
        # Drop obvious non-tickers: a 1-2 char symbol with no price and no
        # corroborating MOMENTUM/GROWTH family is almost always an ApeWisdom
        # false positive ("A", "DD", "CEO"...).
        if (
            not sig.price
            and len(ticker) <= 2
            and "GROWTH" not in sig.families
            and "MOMENTUM" not in sig.families
        ):
            quarantine.append((ticker, "ambiguous-short-symbol"))
            continue
        liquid = filt.passes(sig)
        if n_fams >= 2 and liquid:
            shortlist.append(
                TrendCandidate(
                    ticker=ticker,
                    name=sig.name,
                    score=score_signal(sig),
                    families=tuple(sorted(sig.families)),
                    reasons=tuple(sig.reasons[:5]),
                    price=sig.price,
                    market_cap=sig.market_cap,
                    dollar_volume=filt.dollar_volume(sig),
                    pct_change=sig.pct_change,
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
    """
    from argosy.services.high_potential_sleeve import SleeveCandidate

    held = held_tickers or frozenset()
    out: list[SleeveCandidate] = []
    for c in candidates[:max_names]:
        fam = ", ".join(c.families).lower()
        why = "; ".join(c.reasons[:3])
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
            ),
        ))
    return tuple(out)


def scan_trends(
    *,
    filters: LiquidityFilter | None = None,
    limit: int = 25,
) -> ScanResult:
    """Run every signal family, then score + filter. Network-bound; each
    source is best-effort so a single dead endpoint never aborts the scan."""
    universe: dict[str, RawSignal] = {}
    counts: dict[str, object] = {}
    _gather_yfinance_screeners(universe, counts)
    _gather_apewisdom(universe, counts)
    _gather_stocktwits(universe, counts)
    _gather_yahoo_trending(universe, counts)
    _gather_finviz_growth(universe, counts)
    result = score_and_filter(
        universe, filters=filters, source_counts=counts, limit=limit
    )
    log.info(
        "trend_radar.scan_done",
        sources=counts,
        shortlist=len(result.shortlist),
        quarantine=len(result.quarantine),
    )
    return result


__all__ = [
    "RawSignal",
    "TrendCandidate",
    "ScanResult",
    "LiquidityFilter",
    "score_signal",
    "score_and_filter",
    "scan_trends",
    "to_sleeve_candidates",
]
