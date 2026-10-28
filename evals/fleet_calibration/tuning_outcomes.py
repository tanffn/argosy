"""Post-audit historical screen diagnostics. Never an executed strategy return."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from argosy.services.instrument_reference import name_for
from argosy.services.recommendation_history import METHOD, compare_window
from evals.fleet_calibration.tuning import TUNING_PROTOCOL, load_sealed, save_new, seal
from scripts.check_recommendation_history import fetch

NUMERIC_TOLERANCE_PP = 1e-9  # IEEE rounding guard, not a changed economic threshold.


def cache_covers(evidence, start, end):
    return (evidence.get("status") == "available" and evidence.get("currency") == "USD"
            and evidence.get("requested_start", "9999") <= str(start)
            and evidence.get("requested_end", "0000") >= str(end))


def fetch_window(symbol, start, end):
    return {**fetch(symbol, start, end), "requested_start": str(start), "requested_end": str(end)}


def require_completed_audit(snapshot, snapshot_hash, audit):
    if (audit.get("kind") != "screen_reasoning_audit" or audit.get("status") != "completed"
            or audit.get("snapshot_sha256") != snapshot_hash
            or audit.get("selected_report_ids") != snapshot["audit_screen_ids"]
            or any(b.get("status") != "completed" or not b.get("coverage_verified") for b in audit.get("batches", []))):
        raise ValueError("Matching completed/sealed pre-price audit required; no prices fetched")


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["partition"], row["original_policy_hash"], row["original_go"])].append(row)
    result = []
    for (part, policy, go), members in sorted(groups.items(), key=lambda item: str(item[0])):
        scored = [r for r in members if r["outcome"]["status"] == "available"]
        result.append({"partition": part, "original_policy_hash": policy, "original_investigate": go,
            "denominator": len(members), "scored": len(scored),
            "status_counts": dict(Counter(r["outcome"]["status"] for r in members)),
            "mean_gross_stock_return_pct": sum(r["outcome"]["subject_return_pct"] for r in scored) / len(scored) if scored else None,
            "mean_spy_return_pct": sum(r["outcome"]["benchmark_return_pct"] for r in scored) / len(scored) if scored else None,
            "rose20pct_and_beat_spy": sum(r["outcome"]["subject_return_pct"] >= 20 - NUMERIC_TOLERANCE_PP and r["outcome"]["buy_vs_spy_pp"] > NUMERIC_TOLERANCE_PP for r in scored),
            "fell20pct": sum(r["outcome"]["subject_return_pct"] <= -20 + NUMERIC_TOLERANCE_PP for r in scored)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--price-cache", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.out.with_suffix(".seal.json").exists():
        parser.error("Write-once output required")
    snapshot, snapshot_hash = load_sealed(args.snapshot)
    audit, audit_hash = load_sealed(args.audit)
    require_completed_audit(snapshot, snapshot_hash, audit)
    selected = [r for r in snapshot["first_screens"] if r["partition"] in ("development", "reserved")]
    as_of = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    cache = {}
    mapping = {}
    cache_hash = None
    if args.price_cache:
        raw = args.price_cache.read_bytes()
        receipt = json.loads(args.price_cache.with_suffix(".seal.json").read_text(encoding="utf-8"))
        cache_hash = hashlib.sha256(raw).hexdigest()
        if receipt["sha256"] != cache_hash:
            parser.error("Price cache seal mismatch")
        cached = json.loads(raw)
        if cached.get("as_of") != str(as_of) or cached.get("method") != METHOD:
            parser.error("Cache date/comparison method mismatch")
        mapping = cached.get("symbol_map", {})
        cache = {s: p for s, p in cached["price_evidence"].items() if p["status"] == "available"}
    unresolved = set()
    for row in selected:
        original = snapshot["raw_records"][str(row["report_id"])]
        if ("ucits" in ((original["user_prompt"] or "") + name_for(row["ticker"])).lower()
                and mapping.get(row["ticker"], row["ticker"]) == row["ticker"]):
            unresolved.add(row["ticker"])
    symbols = {mapping.get(r["ticker"], r["ticker"]) for r in selected if r["ticker"] not in unresolved} | {"SPY"}
    oldest = min(date.fromisoformat(r["created_at"][:10]) for r in selected)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {s: pool.submit(fetch_window, s, oldest, as_of) for s in sorted(symbols)
                   if not cache_covers(cache.get(s, {}), oldest, as_of)}
        prices = {s: cache[s] if cache_covers(cache.get(s, {}), oldest, as_of) else futures[s].result()
                  for s in sorted(symbols)}
    spy = {date.fromisoformat(k): v for k, v in prices["SPY"].get("bars", {}).items()}
    rows = []
    for row in selected:
        original = snapshot["raw_records"][str(row["report_id"])]
        symbol = mapping.get(row["ticker"], row["ticker"])
        evidence = prices.get(symbol, {}) if row["ticker"] not in unresolved else {}
        subject = {date.fromisoformat(k): v for k, v in evidence.get("bars", {}).items()} if evidence.get("status") == "available" else {}
        outcome = compare_window(event_at=row["created_at"], days=30, subject=subject,
            benchmark=spy if prices["SPY"].get("status") == "available" else {}, as_of=as_of,
            exchange_timezone=evidence.get("exchange_timezone") or "America/New_York")
        rows.append({**row, "original_policy_hash": original["system_prompt_id"],
                     "original_go": (original["parsed"] or {}).get("go"),
                     "original_rationale": (original["parsed"] or {}).get("one_line") or "Unparseable original decision; no rejection inferred",
                     "provider_symbol": symbol, "identity_unresolved": row["ticker"] in unresolved,
                     "outcome": outcome})
    doc = {"protocol": TUNING_PROTOCOL, "kind": "historical_screen_diagnostics", "snapshot_sha256": snapshot_hash,
           "audit_sha256": audit_hash, "price_cache_sha256": cache_hash, "method": METHOD,
           "as_of": str(as_of), "fetched_at": datetime.now(UTC).isoformat(), "rows": rows,
           "price_evidence": prices, "summary": summarize(rows),
           "numeric_threshold_tolerance_pp": NUMERIC_TOLERANCE_PP,
           "limitations": ["Original INVESTIGATE decisions, not BUY calls, fills, strategy P&L or after-tax performance.",
                           "Recorded screen universe only; survivors/identity changes may bias retrospective provider history.",
                           "Original policy hashes stay separate; these are not outcomes of the newly repaired current policy.",
                           "A subsequent jump does not prove it was predictable or that the earlier decision was wrong.",
                           "Operator previously saw other recommendation outcomes; this is not a pristine unseen market backtest.",
                           "Missing/delisted/unmapped and immature horizons remain in the denominator."]}
    save_new(args.out, doc)
    seal(args.out, "complete")
    print(json.dumps({"denominator": len(rows), "summary": doc["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
