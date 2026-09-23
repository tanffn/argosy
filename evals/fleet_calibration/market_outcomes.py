"""Post-decision, fixed-horizon market diagnostics, NOT simulated trade P&L.

No tax lots, execution assumptions or personal data are inferred. No DB access.
Missing/delisted history stays unknown. Prices never flow back into fleet inputs.
"""
from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Callable

HORIZONS = (6, 12, 24)
MAX_GAP_DAYS = 7
History = dict[date, float]


def validate_identity(packet: dict, symbol: str) -> None:
    import re
    if packet.get("synthetic"):
        raise ValueError("Fictional controls have no real market outcomes")
    # Legacy fixture syntax has ONE identity slot before '@', e.g.
    # 'Boot Barn (BOOT) @ ...' or 'TTCF @ ...'. Never scan later narrative.
    real = str(packet.get("real", ""))
    identity = real.split("@", 1)[0].strip() if "@" in real else ""
    match = re.fullmatch(r"(?:[^()]+\(([A-Z][A-Z0-9.-]*)\)|([A-Z][A-Z0-9.-]*))", identity)
    dedicated = packet.get("market_symbol")
    if dedicated is not None and (not isinstance(dedicated, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]*", dedicated)):
        raise ValueError("Invalid frozen market_symbol")
    expected = dedicated or (next((s for s in match.groups() if s), None) if match else None)
    if expected is None or symbol.strip().upper() != str(expected).strip().upper():
        raise ValueError("Symbol must occur in the frozen case identity; no alias-to-price guessing")


def add_months(day: date, months: int) -> date:
    year, month0 = divmod(day.year * 12 + day.month - 1 + months, 12)
    month = month0 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def fetch_history(symbol: str, start: date, end: date) -> History:
    """Direct provider call: deliberately no production adapter/cache/DB writes."""
    import yfinance as yf
    frame = yf.Ticker(symbol).history(start=start.isoformat(), end=end.isoformat(),
                                      auto_adjust=True, raise_errors=True)
    return {stamp.date(): float(row["Close"]) for stamp, row in frame.iterrows()
            if math.isfinite(float(row["Close"])) and float(row["Close"]) > 0}


def compare_horizons(*, freeze: date, subject: History, benchmark: History,
                     as_of: date) -> list[dict]:
    def valid(bars: History) -> History:
        return {d: p for d, p in bars.items() if isinstance(p, (float, int)) and
                not isinstance(p, bool) and math.isfinite(p) and p > 0}

    subject, benchmark = valid(subject), valid(benchmark)
    common = sorted(set(subject) & set(benchmark))
    # Day-only public-source cutoffs don't establish pre-close availability.
    # The earliest hypothetical entry is therefore AFTER the freeze date.
    candidates = [d for d in common if freeze < d <= min(as_of, freeze + timedelta(days=MAX_GAP_DAYS))]
    entry = candidates[0] if candidates else None
    rows = []
    for months in HORIZONS:
        target = add_months(freeze, months)
        row = {"months": months, "target_date": target.isoformat(), "status": "missing_price_data",
               "subject_return_pct": None, "benchmark_return_pct": None,
               "excess_return_pp": None, "subject_max_close_drawdown_pct": None,
               "after_tax_return_pct": None}
        rows.append(row)
        if target > as_of:
            row["status"] = "not_due"
            continue
        exits = [d for d in common if target - timedelta(days=MAX_GAP_DAYS) <= d <= target]
        if entry is None or not exits or exits[-1] <= entry:
            row["reason"] = "No timely common entry/exit close; missing/delisted is not a zero return"
            continue
        end = exits[-1]
        path = sorted(d for d in subject if entry <= d <= end)
        subject_return = (subject[end] / subject[entry] - 1) * 100
        benchmark_return = (benchmark[end] / benchmark[entry] - 1) * 100
        peak, drawdown = subject[entry], 0.0
        for day in path:
            peak = max(peak, subject[day])
            drawdown = min(drawdown, (subject[day] / peak - 1) * 100)
        row.update(status="available", entry_date=entry.isoformat(), exit_date=end.isoformat(),
                   subject_entry_close=subject[entry], subject_exit_close=subject[end],
                   benchmark_entry_close=benchmark[entry], benchmark_exit_close=benchmark[end],
                   subject_return_pct=subject_return, benchmark_return_pct=benchmark_return,
                   excess_return_pp=subject_return - benchmark_return,
                   subject_max_close_drawdown_pct=drawdown,
                   observed_closes=len(path),
                   drawdown_caveat="Observed adjusted closes only; intraday/missing observations may hide deeper losses")
    return rows


def build_outcomes(run_path: Path, *, case_id: str, symbol: str,
                   fetcher: Callable[[str, date, date], History] = fetch_history,
                   as_of: date | None = None) -> dict:
    from evals.fleet_calibration.lab import digest, summarize
    raw = run_path.read_bytes()
    seal = json.loads(run_path.with_suffix(".lab.json").read_text(encoding="utf-8"))
    run_hash = hashlib.sha256(raw).hexdigest()
    if seal.get("run_sha256") != run_hash:
        raise ValueError("Run changed after independent replay report was sealed")
    document = json.loads(raw)
    summary = summarize(document)
    if not summary["controls_passed"]:
        raise ValueError("Synthetic controls did not pass; historical interpretation is disabled")
    match = [r for r in summary["cases"] if r["case_id"] == case_id]
    if not match or not match[0]["qualified"] or match[0]["synthetic"]:
        raise ValueError("Choose a qualified historical case from this sealed run")
    packet = next(e["packet"] for e in document["lab_manifest"]["cases"] if e["packet"]["case_id"] == case_id)
    # Require an explicit identity mapping present in the frozen evidence;
    # never infer a price symbol from an alias or accept an unrelated ticker.
    validate_identity(packet, symbol)
    freeze = date.fromisoformat(packet["freeze_date"])
    today = as_of or datetime.now(timezone.utc).date()
    end = min(today, add_months(freeze, 24)) + timedelta(days=1)
    errors = []
    histories = {}
    for ticker in (symbol.upper(), "SPY"):
        try:
            histories[ticker] = fetcher(ticker, freeze + timedelta(days=1), end) if end > freeze else {}
        except Exception as exc:
            histories[ticker] = {}
            errors.append({"symbol": ticker, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "case_id": case_id, "symbol": symbol.upper(), "benchmark": "SPY",
        "action": match[0]["action"], "confidence": match[0]["confidence"],
        "freeze_date": freeze.isoformat(), "run_sha256": run_hash,
        "packet_sha256": digest(packet), "fetched_at": datetime.now(timezone.utc).isoformat(),
        "provider": "yfinance adjusted daily close", "as_of": today.isoformat(),
        "basis": "gross adjusted-price diagnostic, not acted-on portfolio P&L",
        "tax_status": "not_computed: no historical tax lots/FX/execution policy; do not infer net proceeds",
        "selection_bias": "Curated diagnostic case, not random-universe performance evidence",
        "errors": errors,
        "horizons": compare_horizons(freeze=freeze, subject=histories[symbol.upper()],
                                      benchmark=histories["SPY"], as_of=today),
        "price_evidence": {ticker: {d.isoformat(): p for d, p in sorted(bars.items())}
                           for ticker, bars in histories.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--out", type=Path, help="Default: <run>.<case>.outcomes.json beside the sealed run")
    args = parser.parse_args()
    args.out = args.out or args.run.with_name(f"{args.run.stem}.{args.case}.outcomes.json")
    from evals.fleet_calibration.lab import save_new
    try:
        if args.out.exists():
            raise ValueError("Outcome evidence is write-once; choose a new --out")
        report = build_outcomes(args.run, case_id=args.case, symbol=args.symbol)
        save_new(args.out, report)
        save_new(args.out.with_suffix(".sha256"), {"sha256": hashlib.sha256(args.out.read_bytes()).hexdigest()})
        print(json.dumps({k: v for k, v in report.items() if k != "price_evidence"}, indent=2))
        return 2 if report["errors"] or any(h["status"] == "missing_price_data" for h in report["horizons"]) else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
