"""HOLD/WAIT grading against CSPX (Stream C finding 7 — Option B).

A HOLD is a decision not to change exposure. It is correct when the name
kept pace with CSPX over the evaluation window (excess return >= -band),
and an opportunity-cost miss when it lagged beyond the band.

Directional hit-rate must never consume these outcomes — scorecard
``hold_*`` metrics are the only consumer.

Band choice (``HOLD_BENCHMARK_BAND_PCT = 0.03``)
===============================================

Grounded on read-only ``portfolio_snapshots`` (immutable open of
``db/argosy.db``). Review window **2026-07-18 → 2026-08-07** (20d),
CSPX +3.39%:

  name   name ret   excess vs CSPX   at 3% band
  SOFI   +4.75%     +1.36%           correct (pace)
  OKLO   +2.63%     -0.76%           correct (pace)
  CRM    +9.37%     +5.98%           correct (beat)
  CSPX   +3.39%      0.00%           EXCLUDED self-benchmark
  SCHD   +2.40%     -0.99%           correct (pace)
  META   -8.69%    -12.07%           incorrect

On that window band sensitivity is flat (1%/2%/3%/5%/10% all 4/5) —
only META is a miss. Band choice therefore uses a second full-coverage
window where near-tracking lags appear:

  **2026-07-14 → 2026-08-07** (SOFI excess −2.51%):
    band 1–2% → 2/5 (40%); band 3%/5%/10% → 3/5 (60%).
  **2026-07-06 → 2026-08-08** (META excess −1.87%, OKLO −9.10%):
    band 1% → 2/5; 2% → 3/5; 3%/5% → 4/5; 10% → 5/5.

3% is the tightest band that does not punish ~2–3% near-tracking lags
(SOFI-class) while still failing clear opportunity-cost misses
(META/OKLO-class). Named constant — not a magic number in call sites.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, Sequence


#: Benchmark ETF for HOLD opportunity-cost grading (Ariel 2026-08-08).
HOLD_BENCHMARK_TICKER: str = "CSPX"

#: Tolerance: name return − CSPX return >= −band → kept pace (correct).
HOLD_BENCHMARK_BAND_PCT: float = 0.03

#: Explicit equity / equity-sleeve labels (portfolio ``asset_type`` values).
HOLD_EQUITY_ASSET_TYPES: frozenset[str] = frozenset(
    {
        "equity",
        "stock",
        "individual stocks",
        "core equity",
        "growth",
        "dividend",
        "defensive",
        "international",
        "broad index",
        "low volatility",
        "nvidia",
        "reit",
        "etf",
        "etf/index",
        "adr",
    }
)

#: Substring tokens that mark equity-class sleeves (after denylist).
HOLD_EQUITY_TOKENS: tuple[str, ...] = (
    "equity",
    "stock",
    "etf",
    "dividend",
    "reit",
    "index",
)

#: Explicitly non-equity — CSPX comparison is not meaningful.
HOLD_NON_EQUITY_ASSET_TYPES: frozenset[str] = frozenset(
    {
        "cash",
        "real estate",
        "treasury",
        "treasury 1-3yr",
        "bond",
        "fixed income",
        "money market",
        "fx",
        "crypto",
        "alternative",
    }
)

HOLD_SELF_BENCHMARK_REASON: str = "hold_self_benchmark"
HOLD_NON_EQUITY_REASON: str = "hold_non_equity"
HOLD_INCOMPLETE_BENCHMARK_REASON: str = "hold_incomplete_benchmark"

HoldKind = Literal[
    "expired_neutral",
    "expired_positive",
    "expired_negative",
    "unparseable",
]


class _BarLike(Protocol):
    bar_date: date
    close: float


@dataclass(frozen=True)
class HoldScoreResult:
    kind: HoldKind
    pnl_pct: float | None = None
    entry_price_used: float | None = None
    exit_price_used: float | None = None
    exit_trigger_date: date | None = None
    notes: str | None = None
    evidence: dict[str, Any] | None = None


def normalize_asset_class(asset_class: str | None) -> str | None:
    if asset_class is None:
        return None
    return str(asset_class).strip().lower()


def is_equity_class_for_hold(asset_class: str | None) -> bool | None:
    """True/False when known; None when unspecified (default equity-eligible).

    Unspecified → eligible (writers may omit class). Explicit non-equity
    denylist wins. Known equity sleeves + token matches are eligible.
    Anything else → False (loud ``hold_non_equity``), never a quiet skip.
    """
    norm = normalize_asset_class(asset_class)
    if not norm:
        return None
    if norm in HOLD_NON_EQUITY_ASSET_TYPES:
        return False
    if norm.startswith("treasury"):
        return False
    if norm in HOLD_EQUITY_ASSET_TYPES:
        return True
    if any(tok in norm for tok in HOLD_EQUITY_TOKENS):
        return True
    return False


def asset_class_from_source_ref(source_ref: str | dict | None) -> str | None:
    if source_ref is None:
        return None
    if isinstance(source_ref, dict):
        data = source_ref
    else:
        try:
            data = json.loads(source_ref)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    raw = data.get("asset_class") or data.get("asset_type")
    return str(raw) if raw is not None else None


def _bar_on_or_before(bars: Sequence[_BarLike], day: date) -> _BarLike | None:
    chosen: _BarLike | None = None
    for b in bars:
        if b.bar_date <= day:
            chosen = b
        else:
            break
    return chosen


def classify_hold_vs_benchmark(
    *,
    ticker: str,
    name_entry: float,
    name_exit: float,
    name_exit_date: date,
    cspx_entry: float,
    cspx_exit: float,
    band: float = HOLD_BENCHMARK_BAND_PCT,
) -> tuple[HoldKind, float, dict[str, Any]]:
    name_ret = (name_exit - name_entry) / name_entry
    cspx_ret = (cspx_exit - cspx_entry) / cspx_entry
    excess = name_ret - cspx_ret
    evidence = {
        "hold_grading": "cspx_relative",
        "benchmark": HOLD_BENCHMARK_TICKER,
        "band_pct": band,
        "name_return": name_ret,
        "cspx_return": cspx_ret,
        "excess_return": excess,
        "ticker": ticker.upper(),
        "exit_date": name_exit_date.isoformat(),
    }
    if excess < -band:
        kind: HoldKind = "expired_negative"
    elif excess > band:
        kind = "expired_positive"
    else:
        kind = "expired_neutral"
    return kind, excess, evidence


def score_hold_vs_cspx(
    *,
    ticker: str,
    entry_price: float,
    name_bars: Sequence[_BarLike],
    cspx_bars: Sequence[_BarLike] | None,
    event_date: date,
    due_date: date,
    asset_class: str | None = None,
    band: float = HOLD_BENCHMARK_BAND_PCT,
) -> HoldScoreResult:
    """Score a HOLD/WAIT against CSPX; degrade loudly when ineligible."""
    sym = (ticker or "").strip().upper()
    if sym == HOLD_BENCHMARK_TICKER:
        return HoldScoreResult(
            kind="unparseable",
            notes=(
                f"{HOLD_SELF_BENCHMARK_REASON}: refusing to grade "
                f"{HOLD_BENCHMARK_TICKER} against itself"
            ),
        )

    if is_equity_class_for_hold(asset_class) is False:
        return HoldScoreResult(
            kind="unparseable",
            notes=(
                f"{HOLD_NON_EQUITY_REASON}: asset_class="
                f"{asset_class!r} is not equity-class; CSPX is not a "
                f"valid benchmark"
            ),
        )

    if not name_bars:
        return HoldScoreResult(
            kind="unparseable",
            notes=(
                f"{HOLD_INCOMPLETE_BENCHMARK_REASON}: no name bars for "
                f"{sym} in window"
            ),
        )
    if not cspx_bars:
        return HoldScoreResult(
            kind="unparseable",
            notes=(
                f"{HOLD_INCOMPLETE_BENCHMARK_REASON}: no "
                f"{HOLD_BENCHMARK_TICKER} bars covering the window"
            ),
        )

    cspx_sorted = sorted(cspx_bars, key=lambda b: b.bar_date)
    name_sorted = sorted(name_bars, key=lambda b: b.bar_date)

    cspx_entry_bar = _bar_on_or_before(cspx_sorted, event_date)
    cspx_exit_bar = _bar_on_or_before(cspx_sorted, due_date)
    if cspx_entry_bar is None or cspx_exit_bar is None:
        return HoldScoreResult(
            kind="unparseable",
            notes=(
                f"{HOLD_INCOMPLETE_BENCHMARK_REASON}: "
                f"{HOLD_BENCHMARK_TICKER} marks missing for "
                f"entry<={event_date.isoformat()} or "
                f"exit<={due_date.isoformat()}"
            ),
        )

    name_exit_bar = name_sorted[-1]
    if cspx_exit_bar.bar_date < name_exit_bar.bar_date:
        return HoldScoreResult(
            kind="unparseable",
            notes=(
                f"{HOLD_INCOMPLETE_BENCHMARK_REASON}: "
                f"{HOLD_BENCHMARK_TICKER} last bar "
                f"{cspx_exit_bar.bar_date.isoformat()} precedes name exit "
                f"{name_exit_bar.bar_date.isoformat()}"
            ),
        )

    kind, excess, evidence = classify_hold_vs_benchmark(
        ticker=sym,
        name_entry=float(entry_price),
        name_exit=float(name_exit_bar.close),
        name_exit_date=name_exit_bar.bar_date,
        cspx_entry=float(cspx_entry_bar.close),
        cspx_exit=float(cspx_exit_bar.close),
        band=band,
    )
    evidence["cspx_entry_date"] = cspx_entry_bar.bar_date.isoformat()
    evidence["cspx_exit_date"] = cspx_exit_bar.bar_date.isoformat()
    evidence["asset_class"] = asset_class
    return HoldScoreResult(
        kind=kind,
        pnl_pct=excess,
        entry_price_used=float(entry_price),
        exit_price_used=float(name_exit_bar.close),
        exit_trigger_date=name_exit_bar.bar_date,
        notes=(
            f"hold_vs_{HOLD_BENCHMARK_TICKER}: excess={excess:.4f} "
            f"band={band}"
        ),
        evidence=evidence,
    )


def hold_ineligibility_bucket(notes: str | None) -> str | None:
    text = notes or ""
    if HOLD_SELF_BENCHMARK_REASON in text:
        return "excluded_hold_self_benchmark"
    if HOLD_NON_EQUITY_REASON in text:
        return "excluded_hold_non_equity"
    if HOLD_INCOMPLETE_BENCHMARK_REASON in text:
        return "excluded_hold_incomplete_benchmark"
    return None
