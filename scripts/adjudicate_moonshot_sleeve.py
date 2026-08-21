#!/usr/bin/env python3
"""x10 moonshot-sleeve composition - deterministic facts pack + blind LLM pair.

Why this script exists (2026-08-21). The sleeve's author/blind-reviewer pair had
been written but NEVER invoked: ``MoonshotSleeveAuthorAgent``,
``MoonshotSleeveBlindReviewerAgent`` and ``moonshot_divergences`` had no callers
outside tests. So every moonshot name shipped on ONE agent's unchecked assertion.
That is how the 2026-08-21 deploy came to call RXRX "FLOORED: real drug-discovery
revenue" ($55M revenue at 34x sales, NEGATIVE gross profit) while calling OKLO
UNFLOORED despite OKLO holding the largest cash cushion of the four (31% of cap).

Discipline (binding, mirrors adjudicate_nvda_glide_schedule.py):
  * Determinism supplies ARITHMETIC FACTS only - market cap, revenue, gross
    profit, net cash, book/tangible book, operating cash flow. No judgment gates.
  * The LLM TEAM adjudicates: author + BLIND reviewer (same raw facts, never the
    author's picks), divergence compared IN CODE - including, since 2026-08-21,
    the FLOOR CLASSIFICATION, which is what the old comparison missed.
  * The verdict is NEVER auto-applied. Sleeve composition is a plan change.

The facts pack is itself the main INPUTS fix: the author previously had no
balance sheet in front of it and asserted floors from memory. It now cannot.

Run:  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/adjudicate_moonshot_sleeve.py
      [--facts-only] [--tickers RXRX,TEM,RGTI,OKLO]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

USER_ID = "ariel"
DEFAULT_TICKERS = ("RXRX", "TEM", "RGTI", "OKLO")

# The calibration case, stated accurately. The first version of this claimed SNDK
# traded below book; it did not - $4.999B of its $9.216B equity was goodwill.
SNDK_CALIBRATION = (
    "CALIBRATION - SanDisk (SNDK) on 2025-08-21, the trade this sleeve exists to "
    "catch: $45.50, market cap $6.66B on FY25 revenue $7.36B (P/S 0.91x). Twelve "
    "months later $1,600.62 (+3,418%). It was NOT protected by book value - "
    "$4.999B of the $9.216B equity was GOODWILL, so tangible book was $4.217B and "
    "it traded at 1.58x TANGIBLE book; NCAV was $1.317B. It was NOT 'losing "
    "money': FY25 gross profit +$2.212B, operating income +$0.507B, operating "
    "cash flow +$0.084B - the GAAP loss was a NONCASH goodwill impairment. What it "
    "WAS: a depressed cyclical/spinoff VALUATION on real cash-generating "
    "operations plus a demand catalyst. SURVIVOR-BIAS WARNING: one winner. Cheap "
    "cyclicals that never inflect, dilute or delist are the base rate."
)


def _num(info: dict, key: str):
    v = info.get(key)
    return v if isinstance(v, (int, float)) else None


def _fundamentals(tickers: tuple[str, ...]) -> list[dict]:
    """Deterministic arithmetic facts. No judgment, no ranking."""
    import yfinance as yf

    rows: list[dict] = []
    for t in tickers:
        row: dict = {"ticker": t}
        try:
            tk = yf.Ticker(t)
            info = tk.info or {}
            mc = _num(info, "marketCap")
            rev = _num(info, "totalRevenue")
            cash = _num(info, "totalCash") or 0.0
            debt = _num(info, "totalDebt") or 0.0
            row.update({
                "market_cap": mc,
                "revenue_ttm": rev,
                "gross_profit": _num(info, "grossProfits"),
                "price_to_book": _num(info, "priceToBook"),
                "ebitda": _num(info, "ebitda"),
                "net_cash": cash - debt,
                "net_cash_pct_of_cap": (cash - debt) / mc if mc else None,
                "ps_ratio": mc / rev if mc and rev else None,
            })
            try:
                bs = tk.balance_sheet
                wanted = (
                    ("Goodwill", "goodwill"),
                    ("Tangible Book Value", "tangible_book"),
                    ("Stockholders Equity", "equity"),
                    ("Current Assets", "current_assets"),
                    ("Total Liabilities Net Minority Interest", "total_liabilities"),
                )
                if bs is not None and not bs.empty:
                    for key, label in wanted:
                        if key in bs.index:
                            v = bs.loc[key].iloc[0]
                            row[label] = float(v) if v == v else None
                cf = tk.cashflow
                if cf is not None and not cf.empty and "Operating Cash Flow" in cf.index:
                    v = cf.loc["Operating Cash Flow"].iloc[0]
                    row["operating_cash_flow"] = float(v) if v == v else None
            except Exception:  # noqa: BLE001 - statements are best-effort
                pass
            ca, tl = row.get("current_assets"), row.get("total_liabilities")
            if ca is not None and tl is not None:
                row["ncav"] = ca - tl
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)[:160]
        rows.append(row)
    return rows


def _b(v) -> str:
    """Format a raw dollar figure as $X.XXXB, or n/a."""
    if not isinstance(v, (int, float)):
        return "n/a"
    return "${:,.3f}B".format(v / 1e9)


def _facts_md(rows: list[dict]) -> str:
    out = [
        "# x10 MOONSHOT SLEEVE - DETERMINISTIC FACTS PACK",
        "as_of: " + datetime.now(UTC).isoformat(timespec="seconds"),
        "",
        "These are ARITHMETIC FACTS, pulled fresh. They are NOT a ranking and NOT a",
        "recommendation. Every floor claim you make must cite numbers from here (or",
        "from a filing you fetch yourself) - never from memory.",
        "",
        SNDK_CALIBRATION,
        "",
        "## Current sleeve candidates",
        "",
    ]
    for r in rows:
        if r.get("error"):
            out.append("### {}\n  DATA ERROR: {}\n".format(r["ticker"], r["error"]))
            continue
        ps = r.get("ps_ratio")
        ncp = r.get("net_cash_pct_of_cap")
        gp = r.get("gross_profit")
        pb = r.get("price_to_book")
        rev_line = "  revenue TTM        " + _b(r.get("revenue_ttm"))
        if ps:
            rev_line += "   (P/S {:,.1f}x)".format(ps)
        gp_line = "  gross profit       " + _b(gp)
        if isinstance(gp, (int, float)) and gp < 0:
            gp_line += "   <-- NEGATIVE: costs more to deliver than it charges"
        nc_line = "  net cash           " + _b(r.get("net_cash"))
        if ncp is not None:
            nc_line += "   ({:.0%} of market cap)".format(ncp)
        eq_line = "  equity             " + _b(r.get("equity"))
        if r.get("goodwill"):
            eq_line += "   of which GOODWILL " + _b(r.get("goodwill"))
        out += [
            "### " + r["ticker"],
            "  market cap         " + _b(r.get("market_cap")),
            rev_line,
            gp_line,
            "  EBITDA             " + _b(r.get("ebitda")),
            "  operating cashflow " + _b(r.get("operating_cash_flow")),
            nc_line,
            eq_line,
            "  tangible book      " + _b(r.get("tangible_book")),
            "  NCAV (CA - TL)     " + _b(r.get("ncav")),
            "  price/book         " + ("{:,.2f}".format(pb) if pb else "n/a"),
            "",
        ]
    out += [
        "## What a FLOOR is, and is not",
        "",
        "Net cash is a CUSHION STATISTIC, not floor math - management spends the",
        "cash, and markets routinely assign negative enterprise value when expected",
        "destruction exceeds it. If you claim a floor, state it as:",
        "",
        "    floor equity = unrestricted cash - debt - burn to the next",
        "                   thesis-resolving milestone - unavoidable commitments",
        "                   + haircutted realizable assets",
        "",
        "then divide by fully diluted shares and give the implied downside % from",
        "today's price. Label every name FLOORED or UNFLOORED in downside_math.",
        "'No floor' is a legitimate answer; an UNDECLARED floor is scored as none.",
    ]
    return "\n".join(out)


def run_team(facts_md: str) -> dict:
    from argosy.agents.plan_change_team import (
        MoonshotSleeveAuthorAgent,
        MoonshotSleeveBlindReviewerAgent,
        moonshot_divergences,
    )
    from argosy.services.fleet_reliability import FleetRetryConfig, call_reliably_sync

    cfg = FleetRetryConfig(hard_timeout_s=900.0)
    totals = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}

    def _run(scope: str, agent_cls):
        def _attempt():
            agent = agent_cls(user_id=USER_ID)
            return agent.run_sync(current_sleeve=[], book={}, notes=facts_md)

        rep = call_reliably_sync(_attempt, scope=scope, config=cfg)
        totals["cost_usd"] += float(getattr(rep, "cost_usd", 0) or 0)
        totals["tokens_in"] += int(getattr(rep, "tokens_in", 0) or 0)
        totals["tokens_out"] += int(getattr(rep, "tokens_out", 0) or 0)
        return rep.output

    t0 = time.monotonic()
    print("[team] author composing ...", flush=True)
    author = _run("moonshot_sleeve_author", MoonshotSleeveAuthorAgent)
    print("[team] author: {} names".format(len(author.names)), flush=True)

    print("[team] blind reviewer re-deriving ...", flush=True)
    reviewer = _run("moonshot_sleeve_blind_reviewer", MoonshotSleeveBlindReviewerAgent)
    print("[team] reviewer: {} names".format(len(reviewer.names)), flush=True)

    divergences = moonshot_divergences(author, reviewer)
    return {
        "author": author.model_dump(),
        "reviewer": reviewer.model_dump(),
        "divergences": divergences,
        "elapsed_s": round(time.monotonic() - t0, 1),
        **totals,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-only", action="store_true")
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    rows = _fundamentals(tickers)
    facts = _facts_md(rows)

    payload: dict = {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "tickers": list(tickers),
        "fundamentals": rows,
        "facts_md": facts,
    }
    if not args.facts_only:
        payload["team"] = run_team(facts)

    # Durable side effects BEFORE printing (cp1252 console rule - a print crash
    # has silently killed runs in this repo before).
    out_path = Path(args.out) if args.out else ROOT / "scratchpad" / "moonshot_adjudication.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(facts)
    if "team" in payload:
        d = payload["team"]["divergences"]
        print()
        print("=" * 70)
        if d:
            print("DIVERGENCE ({}) - NOT auto-resolved. Reconcile before sizing:".format(len(d)))
            for x in d:
                print("  - " + x)
        else:
            print("No divergence: the two blind derivations agree on inclusion,")
            print("weights and floor classification.")
        print("cost ${:.2f} | {}s".format(payload["team"]["cost_usd"], payload["team"]["elapsed_s"]))
    print("\nwrote {}".format(out_path))


if __name__ == "__main__":
    main()
