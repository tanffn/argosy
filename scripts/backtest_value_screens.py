#!/usr/bin/env python3
"""Which 'value' screen actually predicts the SanDisk shape? Point-in-time test.

The question (Ariel, 2026-08-21): "there are always good value options out
there, we need to find a way to PREDICT it."

The claim on trial is NOT "cheap stocks go up". It is the specific asymmetry
claim: a name like SNDK a year ago "could have been 2x or 3x but not 0.1x".
That is a statement about the LEFT TAIL. So the headline metric here is the
downside distribution, not the average return -- a screen that doubles your
median but also doubles your wipeout rate has not delivered asymmetry.

Screens compared, all computed as of AS_OF using only data available then:
  asset_floor      price/tangible-book < 1        (classic deep value)
  operating_value  P/S < 1 AND gross profit > 0   (what SNDK actually was)
  cash_generative  P/S < 1 AND operating CF > 0    (stricter: real cash)
  pre_revenue      revenue < $50M                  (the archetype that failed)
  UNIVERSE         everything with usable data     (the baseline that matters)

A screen is only interesting if it beats UNIVERSE. Most "edges" do not.

KNOWN DEFECTS -- read before believing any number:
  * SURVIVORSHIP BIAS. The universe is built from names listed TODAY, so
    companies that delisted, went bankrupt or were acquired between AS_OF and
    HORIZON_END are INVISIBLE. This inflates every screen, and it inflates the
    deep-value screens MOST, because distress is exactly where delisting
    happens. Treat the left-tail numbers as a LOWER BOUND on the real damage.
  * ONE WINDOW. A single 12-month period is one draw. 2025-08 -> 2026-08 was a
    specific regime; a screen that wins here need not win generally.
  * COARSE AS-OF FUNDAMENTALS. yfinance exposes ~4 annual statements, so
    "as of AS_OF" means the last annual report filed before AS_OF -- up to 12
    months stale, which is how a real screener would see it, but coarse.
  * SHARES OUTSTANDING are today's, so market cap at AS_OF is approximated as
    shares_now x price_asof. Dilution over the window biases caps upward.

Run:  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/backtest_value_screens.py
      [--sample 250] [--out path.json]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

AS_OF = "2025-08-21"
HORIZON_END = "2026-08-20"
SEED = 20260821  # deterministic sample


def _universe() -> list[str]:
    """S&P 400 (mid) + 600 (small) + 500. Small/mid is where the cap-math test can
    plausibly find a 10x, so they matter more than the 500.

    Wikipedia 403s a bare urllib fetch, so we send a real User-Agent and hand the
    HTML to pandas. If that still fails the run ABORTS -- see _require_universe.
    """
    import io

    import pandas as pd
    import requests

    srcs = [
        ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "Symbol"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "Symbol"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
    ]
    headers = {"User-Agent": "Mozilla/5.0 (argosy-backtest; research use)"}
    out: list[str] = []
    for url, col in srcs:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            for tbl in pd.read_html(io.StringIO(resp.text)):
                if col in tbl.columns:
                    out += [str(s).replace(".", "-").strip() for s in tbl[col].tolist()]
                    break
        except Exception as exc:  # noqa: BLE001
            print("  universe source failed: {} ({})".format(url.rsplit("/", 1)[-1], exc))
    seen, uniq = set(), []
    for t in out:
        if t and t != "nan" and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _require(condition: bool, message: str) -> None:
    """Fail LOUD. An earlier version of this script printed a full, confident
    results table built from ZERO tickers after Wikipedia 403'd -- a silently
    empty run that looks exactly like a real one. Never again."""
    if not condition:
        raise SystemExit("ABORT: " + message)


def _asof_col(df, asof_ts):
    """The most recent statement column dated ON OR BEFORE asof. None if all are
    after it -- that is the look-ahead guard, and it must never be relaxed."""
    if df is None or getattr(df, "empty", True):
        return None
    cols = [c for c in df.columns if c is not None]
    usable = [c for c in cols if c <= asof_ts]
    return max(usable) if usable else None


def _val(df, col, row):
    try:
        if col is None or row not in df.index:
            return None
        v = df.loc[row, col]
        return float(v) if v == v else None
    except Exception:  # noqa: BLE001
        return None


def _features(ticker: str, asof_ts, hist):
    import yfinance as yf

    sub = hist[hist.index <= asof_ts] if hist is not None else None
    if sub is None or len(sub) < 60:
        return None
    fwd = hist[hist.index <= HORIZON_END]
    if not len(fwd):
        return None
    px = float(sub.iloc[-1]["Close"])
    fwd_ret = float(fwd.iloc[-1]["Close"]) / px - 1.0

    tk = yf.Ticker(ticker)
    try:
        info = tk.info or {}
        shares = info.get("sharesOutstanding")
        shares = float(shares) if isinstance(shares, (int, float)) else None
    except Exception:  # noqa: BLE001
        shares = None
    if not shares:
        return None
    cap = shares * px  # approximation -- see DEFECTS

    try:
        bs, fin, cf = tk.balance_sheet, tk.financials, tk.cashflow
    except Exception:  # noqa: BLE001
        return None
    bc, fc, cc = _asof_col(bs, asof_ts), _asof_col(fin, asof_ts), _asof_col(cf, asof_ts)
    if fc is None:
        return None

    rev = _val(fin, fc, "Total Revenue")
    gp = _val(fin, fc, "Gross Profit")
    tb = _val(bs, bc, "Tangible Book Value")
    ocf = _val(cf, cc, "Operating Cash Flow")
    if rev is None:
        return None
    return {
        "ticker": ticker, "price_asof": px, "market_cap_asof": cap,
        "revenue": rev, "gross_profit": gp, "tangible_book": tb,
        "operating_cf": ocf, "fwd_ret": fwd_ret,
        "ps": cap / rev if rev and rev > 0 else None,
        "ptb": cap / tb if tb and tb > 0 else None,
    }


SCREENS = {
    "asset_floor": lambda r: r["ptb"] is not None and r["ptb"] < 1.0,
    "operating_value": lambda r: (r["ps"] is not None and r["ps"] < 1.0
                                  and r["gross_profit"] is not None and r["gross_profit"] > 0),
    "cash_generative": lambda r: (r["ps"] is not None and r["ps"] < 1.0
                                  and r["operating_cf"] is not None and r["operating_cf"] > 0),
    "pre_revenue": lambda r: r["revenue"] is not None and r["revenue"] < 50e6,
}


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rets = sorted(r["fwd_ret"] for r in rows)
    n = len(rets)
    med = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
    return {
        "n": n,
        "median_fwd": round(med, 4),
        "pct_over_2x": round(sum(1 for x in rets if x >= 1.0) / n, 4),
        "pct_over_50pct": round(sum(1 for x in rets if x >= 0.5) / n, 4),
        "pct_down_50pct": round(sum(1 for x in rets if x <= -0.5) / n, 4),
        "pct_down_30pct": round(sum(1 for x in rets if x <= -0.3) / n, 4),
        "worst": round(rets[0], 4),
        "best": round(rets[-1], 4),
        "p10": round(rets[max(0, int(n * 0.10) - 1)], 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=250)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    import pandas as pd
    import yfinance as yf

    asof_ts = pd.Timestamp(AS_OF)
    print("building universe ...", flush=True)
    uni = _universe()
    print("  {} tickers".format(len(uni)), flush=True)
    _require(len(uni) >= 400,
             "universe has only {} tickers -- the index sources failed. A screen "
             "comparison on a truncated universe is worse than none.".format(len(uni)))
    rng = random.Random(SEED)
    sample = sorted(rng.sample(uni, min(args.sample, len(uni))))

    rows: list[dict] = []
    for i, t in enumerate(sample, 1):
        if i % 25 == 0:
            print("  {}/{} ... {} usable".format(i, len(sample), len(rows)), flush=True)
        try:
            h = yf.Ticker(t).history(period="5y", auto_adjust=True)
            if h is None or h.empty:
                continue
            if h.index.tz is not None:
                h.index = h.index.tz_localize(None)
            f = _features(t, asof_ts, h)
            if f:
                rows.append(f)
        except Exception:  # noqa: BLE001 -- one bad ticker must not kill the run
            continue

    _require(len(rows) >= 60,
             "only {} of {} sampled tickers yielded usable as-of fundamentals -- "
             "too few to compare screens against a baseline.".format(len(rows), len(sample)))

    results = {"UNIVERSE": _stats(rows)}
    members = {}
    for name, fn in SCREENS.items():
        hit = [r for r in rows if fn(r)]
        results[name] = _stats(hit)
        members[name] = sorted((r["ticker"] for r in hit))

    payload = {
        "as_of": AS_OF, "horizon_end": HORIZON_END,
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "sampled": len(sample), "usable": len(rows),
        "results": results, "members": members, "rows": rows,
    }
    out_path = Path(args.out) if args.out else ROOT / "scratchpad" / "value_screens.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    hdr = "{:<17s} {:>5s} {:>9s} {:>8s} {:>8s} {:>9s} {:>9s} {:>8s}".format(
        "screen", "n", "median", ">2x", ">+50%", "<-50%", "<-30%", "p10")
    print()
    print("=" * len(hdr))
    print("AS OF {} -> {}   (sampled {}, usable {})".format(
        AS_OF, HORIZON_END, len(sample), len(rows)))
    print("=" * len(hdr))
    print(hdr)
    for name in ["UNIVERSE"] + list(SCREENS):
        s = results[name]
        if not s.get("n"):
            print("{:<17s} {:>5d}   (no members)".format(name, 0))
            continue
        print("{:<17s} {:>5d} {:>8.1%} {:>8.1%} {:>8.1%} {:>9.1%} {:>9.1%} {:>8.1%}".format(
            name, s["n"], s["median_fwd"], s["pct_over_2x"], s["pct_over_50pct"],
            s["pct_down_50pct"], s["pct_down_30pct"], s["p10"]))
    print()
    print("READ THE LEFT TAIL FIRST (<-50%): the claim on trial is bounded")
    print("downside, not upside. And every number here is survivorship-inflated --")
    print("delisted failures are invisible. See DEFECTS in the module docstring.")
    print("\nwrote {}".format(out_path))


if __name__ == "__main__":
    main()
