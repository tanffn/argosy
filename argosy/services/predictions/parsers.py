"""Tiny regex-based parsers for prediction writer adapters.

Per spec §1.4 the Discord channel format Ariel watches uses informal
conventions like ``BUY $NVDA → $180 stop $135 by Fri``. The writer
adapter for Discord (``writers.write_discord_prediction``) needs a way
to decide "does this message body contain a parseable alpha call?" so
that the per-source ``actionable-only`` gate (spec §3 anti-collision
contract) skips chatter without a direction + ticker.

Scope intentionally tiny: regex only. If the live extractor later turns
out to need an LLM-assisted parser, that ships as
``discord_call_parser.py`` per spec §7.3 — a follow-on, not in commit #3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


# Direction keywords map to the predictions.direction CHECK enum.
# BUY / LONG / ADD => long; SELL / SHORT / TRIM => short. Case-insensitive
# at the regex level.
_DIRECTION_LONG: tuple[str, ...] = ("BUY", "LONG", "ADD", "BULL", "BULLISH")
_DIRECTION_SHORT: tuple[str, ...] = ("SELL", "SHORT", "TRIM", "BEAR", "BEARISH")

# Alpha-call regex — captures (direction_kw, optional $, ticker). Ticker
# is 1-5 uppercase letters per US-equity convention; word boundaries on
# both sides so "BUY SELLING" doesn't match (SELLING isn't a ticker).
_ALPHA_CALL_RE = re.compile(
    r"\b("
    + "|".join(_DIRECTION_LONG + _DIRECTION_SHORT)
    + r")\s+\$?([A-Z]{1,5})\b",
    re.IGNORECASE,
)

# Price-level patterns. Tolerant of optional $, optional decimals.
# Targets: "→ $180", "target $180", "tgt 180", "pt $180". Stops:
# "stop $135", "sl 135", "stop-loss 135".
_TARGET_RE = re.compile(
    r"(?:→|->|=>|\btarget\b|\btgt\b|\bpt\b|\bprice\s*target\b)"
    r"\s*\$?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(?:stop(?:-?loss)?|sl)\b\s*\$?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AlphaCall:
    """A parsed alpha call extracted from free-text.

    Empty / unparseable inputs return ``None`` from
    ``extract_alpha_call_from_text``; callers MUST check.
    """

    ticker: str
    direction: Literal["long", "short"]
    target_price: float | None = None
    stop_price: float | None = None


def extract_alpha_call_from_text(text: str) -> AlphaCall | None:
    """Best-effort alpha-call extractor for Discord free-text messages.

    Returns ``None`` when the text has no recognisable (direction, ticker)
    pair. Used by ``write_discord_prediction`` to GATE the prediction
    write so chatter / off-topic messages don't land in the ledger as
    polluting ``unparseable`` rows (spec §3 — gate on actionable case).

    The regex is intentionally permissive on direction keywords + strict
    on the ticker shape (1-5 uppercase). Multi-call messages (e.g.
    "BUY NVDA, SELL AMD") return only the FIRST call — a follow-on
    parser can handle multi-leg when the data shows it matters.

    Args:
      text: the raw message body. Typically <500 chars for Discord.

    Returns:
      ``AlphaCall`` with ``ticker`` upper-cased and ``direction`` in
      {long, short}. ``target_price`` / ``stop_price`` populated when
      the corresponding regex matches; ``None`` otherwise.
    """
    if not text or not isinstance(text, str):
        return None

    match = _ALPHA_CALL_RE.search(text)
    if match is None:
        return None

    direction_kw = match.group(1).upper()
    ticker = match.group(2).upper()
    if direction_kw in _DIRECTION_LONG:
        direction: Literal["long", "short"] = "long"
    elif direction_kw in _DIRECTION_SHORT:
        direction = "short"
    else:  # pragma: no cover — regex is closed-set
        return None

    target_price: float | None = None
    stop_price: float | None = None

    tgt_match = _TARGET_RE.search(text)
    if tgt_match is not None:
        try:
            target_price = float(tgt_match.group(1))
        except ValueError:  # pragma: no cover — regex guarantees digits
            target_price = None

    stop_match = _STOP_RE.search(text)
    if stop_match is not None:
        try:
            stop_price = float(stop_match.group(1))
        except ValueError:  # pragma: no cover — regex guarantees digits
            stop_price = None

    return AlphaCall(
        ticker=ticker,
        direction=direction,
        target_price=target_price,
        stop_price=stop_price,
    )


__all__ = ["AlphaCall", "extract_alpha_call_from_text"]
