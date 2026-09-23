"""Bounded live quote/cooldown diagnostic; never changes holdings or orders."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    from argosy.adapters.data.yahoo_access import default_access
    from argosy.config import get_settings
    from argosy.services.snapshot_refresh import default_quote_fn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--symbol", help="Probe ONE held symbol using its actual broker listing metadata")
    parser.add_argument("--check-inbox", action="store_true", help="Read real Inbox only if cooldown is active")
    args = parser.parse_args()
    guard = default_access()
    result = {"before": guard.status()}
    if args.symbol:
        settings = get_settings()
        with sqlite3.connect(f"file:{settings.db_file.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT positions_json FROM portfolio_snapshots WHERE user_id=? ORDER BY imported_at DESC,id DESC LIMIT 1", (args.user_id,)).fetchone()
        positions = json.loads(row[0]) if row else []
        matches = [p for p in positions if str(p.get("symbol", "")).upper() == args.symbol.upper()]
        identities = {(p.get("symbol"), p.get("currency"), p.get("details")) for p in matches}
        if len(identities) != 1:
            raise ValueError("A single unambiguous held listing is required")
        symbol, currency, details = next(iter(identities))
        result["quote"] = {"symbol": symbol, "price": default_quote_fn(
            symbol, currency=currency or "USD", details=details or "",
        )}
    after_probe = guard.status()
    blocked = bool(after_probe["retry_at"] and after_probe["retry_at"] > time.time())
    if args.check_inbox and blocked:
        with urlopen("http://127.0.0.1:8000/api/inbox?" + urlencode({"user_id": args.user_id}), timeout=90) as response:
            feed = json.load(response)
        result["inbox"] = {"issue_codes": [item["code"] for item in feed.get("issues", [])],
                           "trade_plan_available": feed.get("trade_plan") is not None}
        result["inbox_additional_guarded_sdk_operations"] = guard.status()["sdk_operations"] - after_probe["sdk_operations"]
    elif args.check_inbox:
        result["inbox"] = "Skipped: cooldown not active; avoid triggering a full-book network refresh"
    result["after"] = guard.status()
    result["prices_restored"] = False  # One quote/cooldown probe cannot prove a current book.
    print(json.dumps(result))
    return int(result.get("inbox_additional_guarded_sdk_operations", 0) != 0)


if __name__ == "__main__":
    raise SystemExit(main())
