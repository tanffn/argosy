"""Audit all saved calls without changing the production DB or recommendations.

Inventory only: python scripts/check_recommendation_history.py --inventory-only
Fetch and seal: python scripts/check_recommendation_history.py --out logs/<new>.json
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from argosy.services.recommendation_history import (  # noqa: E402
    HORIZONS,
    METHOD,
    compare_window,
    inventory,
    read_engine,
    seal_inventory,
    summarize,
)


def fetch(symbol: str, start: date, end: date) -> dict:
    import math

    import yfinance as yf

    try:
        ticker = yf.Ticker(symbol)
        frame = ticker.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                               auto_adjust=True, raise_errors=True, timeout=20)
        metadata = ticker.history_metadata or {}
        currency = metadata.get("currency")
        bars = {stamp.date().isoformat(): float(row["Close"]) for stamp, row in frame.iterrows()
                if stamp.date() <= end and math.isfinite(float(row["Close"])) and float(row["Close"]) > 0}
        return {"symbol": symbol, "currency": currency, "instrument_type": metadata.get("instrumentType"),
                "exchange_timezone": metadata.get("exchangeTimezoneName"),
                "fetched_at": datetime.now(UTC).isoformat(), "bars": bars,
                "status": "available" if bars and currency == "USD" else "unscorable",
                "reason": None if bars and currency == "USD" else "Missing history or non-USD/unknown quote currency; no FX invented."}
    except Exception as exc:
        return {"symbol": symbol, "status": "unscorable", "bars": {},
                "reason": f"{type(exc).__name__}: {exc}", "fetched_at": datetime.now(UTC).isoformat()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--symbol-map", type=Path, help="Explicit stored-ticker to provider-symbol JSON mapping")
    parser.add_argument("--price-cache", type=Path, help="Reuse sealed same-date available price evidence; missing identities are never guessed")
    args = parser.parse_args()
    if not args.inventory_only and args.out is None:
        parser.error("--out is required for a write-once price-evidence artifact")
    if args.out and args.out.exists():
        parser.error("Output exists; choose a new immutable artifact")
    engine = read_engine()
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN")
        document = inventory(db, user_id=args.user_id)
    engine.dispose()
    document["inventory_sha256"] = seal_inventory(document)
    document["started_at"] = datetime.now(UTC).isoformat()
    if args.inventory_only:
        print(json.dumps({"coverage": document["coverage"], "inventory_sha256": document["inventory_sha256"]}, indent=2))
        return 0
    mapping = json.loads(args.symbol_map.read_text(encoding="utf-8")) if args.symbol_map else {}
    if not isinstance(mapping, dict) or any(not isinstance(v, str) for v in mapping.values()):
        parser.error("Symbol map must be a JSON object with string values")
    # Never use today's incomplete close as a completed historical window.
    as_of = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    for symbol, evidence in document["provider_alias_evidence"].items():
        aliases = {e["provider_symbol"] for e in evidence}
        if len(aliases) == 1:
            mapping.setdefault(symbol, next(iter(aliases)))
    from argosy.services.instrument_reference import name_for
    unresolved = {r["ticker"] for r in document["calls"] if "UCITS" in name_for(r["ticker"])
                  and mapping.get(r["ticker"], r["ticker"]) == r["ticker"]}
    oldest = date.fromisoformat(document["coverage"]["oldest_event_at"][:10])
    symbols = sorted({mapping.get(r["ticker"], r["ticker"]) for r in document["calls"]
                      if r["ticker"] not in unresolved} | {"SPY"})
    cache = {}
    if args.price_cache:
        import hashlib
        raw = args.price_cache.read_bytes()
        seal = json.loads(args.price_cache.with_suffix(".seal.json").read_text(encoding="utf-8"))
        if seal.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise ValueError("Price cache changed after sealing")
        cached = json.loads(raw)
        if cached.get("as_of") != str(as_of) or cached.get("method") != METHOD:
            raise ValueError("Price cache has a different date or comparison contract")
        cache = {s: p for s, p in cached["price_evidence"].items() if p["status"] == "available"}
        document["price_cache_sha256"] = seal["sha256"]
    document.update(method=METHOD, as_of=str(as_of), provider="yfinance auto_adjust=True daily closes",
                    symbol_map=mapping, status="fetching", price_evidence={})
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for evidence in pool.map(lambda symbol: cache.get(symbol) or fetch(symbol, oldest, as_of), symbols):
            document["price_evidence"][evidence["symbol"]] = evidence
            print(json.dumps({"stage": "price_history", "symbol": evidence["symbol"],
                              "status": evidence["status"]}), flush=True)
    for symbol in unresolved:
        document["price_evidence"][symbol] = {"symbol": symbol, "status": "unscorable", "bars": {},
            "reason": "Foreign-fund identity requires a recorded venue/provider alias; bare-US ticker may be another security."}
    spy = document["price_evidence"]["SPY"]
    def bars(evidence):
        return ({date.fromisoformat(k): v for k, v in evidence["bars"].items()}
                if evidence["status"] == "available" else {})
    for row in document["calls"]:
        evidence = document["price_evidence"][mapping.get(row["ticker"], row["ticker"])]
        row["outcomes"] = [compare_window(event_at=row["event_at"], days=days,
                                          subject=bars(evidence), benchmark=bars(spy), as_of=as_of,
                                          exchange_timezone=evidence.get("exchange_timezone") or "America/New_York")
                           for days in HORIZONS]
        if evidence.get("status") != "available":
            row["price_coverage_reason"] = evidence.get("reason")
    document.update(summary=summarize(document["calls"]), status="complete",
                    finished_at=datetime.now(UTC).isoformat(), limitations=[
                        "Call-level gross adjusted-price diagnostics, not portfolio, execution or after-tax P&L.",
                        "USD histories only; missing/delisted/unmapped symbols remain in the denominator, never zero.",
                        "SELL comparison is an explicit sell-to-SPY versus keep-stock counterfactual, not a short trade.",
                        "First call per ticker/action/channel/policy in summaries; all repeated calls retained.",
                        "Cancelled, rejected, shadow and manual records are separately labelled, not verified live endorsements.",
                        "Cross-channel overlap is not pooled; surviving clocks do not prove notification delivery.",
                        "Short-term outcomes do not establish long-term thesis quality or an unbiased market edge.",
                        "No historical criterion version is invented for unversioned records.",
                    ])
    # Only this process's newly-created unsealed artifact is mutable.
    args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    import hashlib
    seal = {"sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(), "finished_at": document["finished_at"]}
    with args.out.with_suffix(".seal.json").open("x", encoding="utf-8") as handle:
        json.dump(seal, handle, indent=2)
    print(json.dumps({"coverage": document["coverage"], "summary_30d": [r for r in document["summary"] if r["horizon_days"] == 30],
                      "artifact": str(args.out)}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
