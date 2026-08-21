"""Look-ahead-free backtest of the trend-radar's SELECTION RULES.

Productionized from the 2026-08-21 prototype (Ariel's ask: "would Argosy's
radar band + scoring have admitted the names that went on to be big
winners?"). Repeatable harness — parameterize universe / as-of dates /
horizon instead of editing constants inline.

Design constraints that make the answer mean anything (kept from the
prototype, do not remove):
  * ANONYMIZED. Tickers are replaced by opaque ids before any judgement is
    formed; the mapping is only revealed in the final scoring table. An LLM
    (or a human) told "SNDK" already knows the answer; told "A17" it does
    not. Every gate here is arithmetic on price/volume/cap/history only.
  * AS-OF. Every feature at date T uses ONLY rows with index <= T. Market cap
    at T is reconstructed as (shares_now * price_T), never today's cap.
  * CONTROL SET. Winners are meaningless without names that did nothing. We
    score both, so we get a hit RATE and a false-positive rate, not an
    anecdote.

What it tests: the radar's LIQUIDITY BANDS (satellite + thematic, imported
from the live module so this harness tracks the code, never re-declares the
constants) and the MOMENTUM_SUSTAINED family (12-1 cross-sectional relative
strength, same lookback/skip as the live gatherer). It cannot replay the
ATTENTION/social feeds (no historical archive), so it answers the narrower,
honest question: would the *band(s)* have let these names through, and would
sustained momentum have flagged them -- not "would the full >=2-family pump
guard have fired" (that needs a family we cannot replay historically).

Usage:
    .venv/Scripts/python.exe scripts/backtest_radar.py
    .venv/Scripts/python.exe scripts/backtest_radar.py --thematic
    .venv/Scripts/python.exe scripts/backtest_radar.py \
        --winners SNDK,MU,WDC,STX --as-of 2025-08-21,2025-11-21 \
        --horizon-end 2026-08-20 --out scratch.json

Side effects (writing the JSON) happen BEFORE printing (cp1252 console rule
-- a print crash must never silently swallow a completed backtest run).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

from argosy.services.trend_radar import (  # noqa: E402
    DEFAULT_CAP_MAX,
    DEFAULT_CAP_MIN,
    DEFAULT_MIN_DOLLAR_VOLUME,
    DEFAULT_MIN_PRICE,
    THEMATIC_CAP_MAX,
    THEMATIC_CAP_MIN,
    THEMATIC_MIN_DOLLAR_VOLUME,
    _MOMENTUM_LOOKBACK,
    _MOMENTUM_MIN_HISTORY,
    _MOMENTUM_SKIP,
    _MOMENTUM_TOP_PCTILE,
)

DEFAULT_WINNERS = ["SNDK", "MU", "WDC", "STX"]
DEFAULT_CONTROL = ["KO", "PG", "JNJ", "VZ", "CSCO", "IBM", "TGT", "MDLZ",
                   "GIS", "KMB", "SO", "DUK", "ADM", "F"]
DEFAULT_AS_OF = ["2025-08-21", "2025-11-21", "2026-02-20"]
DEFAULT_HORIZON_END = "2026-08-20"


def _fetch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for t in tickers:
        try:
            h = yf.Ticker(t).history(period="5y", auto_adjust=True)
            if len(h) > 100:
                out[t] = h
        except Exception:
            pass
    return out


def _shares_out(t: str) -> float | None:
    try:
        fi = yf.Ticker(t).fast_info
        mc = getattr(fi, "market_cap", None)
        px = getattr(fi, "last_price", None)
        if mc and px:
            return mc / px
    except Exception:
        pass
    return None


def features_asof(h: pd.DataFrame, shares: float | None, asof: str) -> dict | None:
    """Every value uses only rows <= asof. No look-ahead. Mirrors the live
    ``_gather_momentum_sustained`` construction (full 12-1, else a
    short-window fallback flagged insufficient_history -- never dropped for
    having *some* history, only for having none at all)."""
    sub = h[h.index <= asof]
    if len(sub) < _MOMENTUM_MIN_HISTORY:
        return None
    px = float(sub.iloc[-1]["Close"])
    vol20 = float(sub["Volume"].tail(20).mean())
    dollar_vol = px * vol20
    cap = px * shares if shares else None
    insufficient = False
    if len(sub) >= _MOMENTUM_LOOKBACK + _MOMENTUM_SKIP:
        p_start = float(sub.iloc[-(_MOMENTUM_LOOKBACK + _MOMENTUM_SKIP)]["Close"])
        p_end = float(sub.iloc[-_MOMENTUM_SKIP]["Close"])
    else:
        skip = min(5, max(1, len(sub) // 10))
        p_start = float(sub.iloc[0]["Close"])
        p_end = float(sub.iloc[-skip]["Close"])
        insufficient = True
    mom = (p_end / p_start - 1.0) if p_start > 0 else None
    return {"price": px, "dollar_volume": dollar_vol, "market_cap": cap,
            "mom_12_1": mom, "insufficient_history": insufficient}


def band_ok(f: dict, *, thematic_lane: bool) -> tuple[bool, str, str | None]:
    """The radar's OWN liquidity bands, imported from the live module.
    Returns (admitted, reject_reason_if_any, lane_if_admitted). The thematic
    band is only ever consulted when ``thematic_lane`` is True -- mirrors
    ``score_and_filter(..., thematic_lane_enabled=...)`` exactly: OFF means
    OFF, never a silent fallback."""
    if f["price"] is None or f["price"] < DEFAULT_MIN_PRICE:
        return False, "price", None
    if f["market_cap"] is None:
        return False, "no-cap", None
    cap = f["market_cap"]
    dv = f["dollar_volume"]
    if DEFAULT_CAP_MIN <= cap <= DEFAULT_CAP_MAX:
        if dv is None or dv < DEFAULT_MIN_DOLLAR_VOLUME:
            return False, "dollar-vol", None
        return True, "ok", "satellite"
    if thematic_lane and THEMATIC_CAP_MIN <= cap <= THEMATIC_CAP_MAX:
        if dv is None or dv < THEMATIC_MIN_DOLLAR_VOLUME:
            return False, "dollar-vol-thematic", None
        return True, "ok-thematic", "thematic"
    return False, "cap-band", None


def forward_return(h: pd.DataFrame, asof: str, horizon_end: str) -> float | None:
    sub = h[h.index <= asof]
    fwd = h[h.index <= horizon_end]
    if not len(sub) or not len(fwd):
        return None
    return float(fwd.iloc[-1]["Close"]) / float(sub.iloc[-1]["Close"]) - 1.0


def run_backtest(
    winners: list[str],
    control: list[str],
    as_of_dates: list[str],
    horizon_end: str,
    *,
    thematic_lane: bool,
) -> tuple[pd.DataFrame, dict[str, str]]:
    universe = winners + control
    hist = _fetch(universe)
    shares = {t: _shares_out(t) for t in hist}

    # ---- anonymize BEFORE any judgement --------------------------------
    anon = {t: f"A{i:02d}" for i, t in enumerate(sorted(hist), start=1)}

    rows = []
    for asof in as_of_dates:
        # Cross-sectional momentum percentile within THIS as-of universe --
        # mirrors the live gatherer's "top quintile" cross-sectional cut.
        feats = {t: features_asof(h, shares.get(t), asof) for t, h in hist.items()}
        moms = sorted(
            (t, f["mom_12_1"]) for t, f in feats.items()
            if f and f["mom_12_1"] is not None
        )
        pctile_rank = {t: i / max(1, len(moms) - 1) for i, (t, _) in enumerate(
            sorted(moms, key=lambda kv: kv[1])
        )} if len(moms) > 1 else {}

        for t, h in hist.items():
            f = feats.get(t)
            if not f:
                continue
            admitted, reason, lane = band_ok(f, thematic_lane=thematic_lane)
            in_band_satellite_only = lane == "satellite"
            fam_fires = pctile_rank.get(t, 0.0) >= _MOMENTUM_TOP_PCTILE
            rows.append({
                "as_of": asof, "id": anon[t], "ticker": t,
                "in_band": in_band_satellite_only, "admitted_any_lane": admitted,
                "lane": lane, "reject": reason,
                "cap_bn": round(f["market_cap"] / 1e9, 2) if f["market_cap"] else None,
                "mom_12_1": round(f["mom_12_1"], 3) if f["mom_12_1"] is not None else None,
                "mom_pctile": round(pctile_rank.get(t, 0.0), 2),
                "momentum_sustained_fires": bool(fam_fires),
                "insufficient_history": f["insufficient_history"],
                "fwd_ret": (
                    round(forward_return(h, asof, horizon_end), 3)
                    if forward_return(h, asof, horizon_end) is not None else None
                ),
            })

    return pd.DataFrame(rows), anon


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--winners", default=",".join(DEFAULT_WINNERS))
    ap.add_argument("--control", default=",".join(DEFAULT_CONTROL))
    ap.add_argument("--as-of", default=",".join(DEFAULT_AS_OF))
    ap.add_argument("--horizon-end", default=DEFAULT_HORIZON_END)
    ap.add_argument("--thematic", action="store_true",
                     help="Also admit names via the thematic lane (off by "
                          "default, mirrors ARGOSY_THEMATIC_LANE_ENABLED).")
    ap.add_argument("--out", default="backtest_radar.json")
    args = ap.parse_args()

    winners = [t.strip().upper() for t in args.winners.split(",") if t.strip()]
    control = [t.strip().upper() for t in args.control.split(",") if t.strip()]
    as_of_dates = [d.strip() for d in args.as_of.split(",") if d.strip()]

    df, anon = run_backtest(
        winners, control, as_of_dates, args.horizon_end,
        thematic_lane=args.thematic,
    )

    # ---- durable side effect BEFORE printing (cp1252 console rule) --------
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(df.to_dict(orient="records"), fh, indent=2)

    win_set = set(winners)
    print("=" * 78)
    print(f"ANONYMIZED VIEW (thematic lane {'ON' if args.thematic else 'OFF'})"
          " -- selection decided on these columns only")
    print("=" * 78)
    for asof in as_of_dates:
        d = df[df.as_of == asof].sort_values("mom_12_1", ascending=False)
        print(f"\n--- as of {asof} ---")
        print(f"{'id':5s} {'cap$bn':>8s} {'mom12-1':>8s} {'pctile':>7s} "
              f"{'admit':>6s} {'lane':>10s} {'reject':>12s} {'insuf':>6s}")
        for _, r in d.iterrows():
            print(f"{r['id']:5s} {str(r['cap_bn']):>8s} {str(r['mom_12_1']):>8s} "
                  f"{str(r['mom_pctile']):>7s} {str(r['admitted_any_lane']):>6s} "
                  f"{str(r['lane']):>10s} {str(r['reject']):>12s} "
                  f"{str(r['insufficient_history']):>6s}")

    print()
    print("=" * 78)
    print("UNBLINDED SCORING")
    print("=" * 78)
    for asof in as_of_dates:
        d = df[df.as_of == asof]
        w = d[d.ticker.isin(win_set)]
        c = d[~d.ticker.isin(win_set)]
        print(f"\n--- as of {asof} ---")
        print(f"  winners admitted (any enabled lane): {int(w.admitted_any_lane.sum())}/{len(w)}"
              f"   median fwd {w.fwd_ret.median():+.0%}" if len(w) else "  (no winners with data)")
        print(f"  control admitted (any enabled lane): {int(c.admitted_any_lane.sum())}/{len(c)}"
              f"   median fwd {c.fwd_ret.median():+.0%}" if len(c) else "  (no control with data)")
        miss = w[~w.admitted_any_lane]
        if len(miss):
            print("  winners REJECTED (all enabled lanes):")
            for _, r in miss.iterrows():
                print(f"     {r['ticker']:5s} cap ${r['cap_bn']}bn  reason={r['reject']}"
                      f"  fwd {r['fwd_ret']:+.0%}" if r['fwd_ret'] is not None
                      else f"     {r['ticker']:5s} cap ${r['cap_bn']}bn  reason={r['reject']}")
        fam_hit = w.momentum_sustained_fires.sum()
        print(f"  MOMENTUM_SUSTAINED fires for {int(fam_hit)}/{len(w)} winners "
              f"(top {(1 - _MOMENTUM_TOP_PCTILE):.0%} cross-sectionally)")
        ranked = d.sort_values("mom_12_1", ascending=False).head(5)
        hit = ranked.ticker.isin(win_set).sum()
        print(f"  top-5 by 12-1 momentum contains {hit}/5 winners "
              f"({', '.join(ranked.ticker)})")
    print()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
