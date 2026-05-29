"""Stage 1 deterministic news extractor — NO LLM.

Sprint commit #13 of the plan/execute/monitor reorg. Pure-Python parser
that normalizes a raw news item (discord msg / RSS item / macro-feed
entry) into the Stage 1 fields of the ``news_signals`` table.

Codex BLOCKER #2 isolation contract
-----------------------------------

``raw_text`` is preserved on the resulting ``ExtractedSignal`` so the
caller can persist it for citation display. It is **NEVER** consumed by
the Stage 2 analyst LLM — only the normalized fields
(``parsed_tickers``, ``event_keywords``, ``sentiment``, ``source_trust``,
``evidence_excerpt``) reach the prompt. Stage 2 lands in commit #14.

Why deterministic
-----------------

Stage 1 is structurally cheap and adversarially robust. Tickers come
from a whitelist (the user's holdings + an S&P 500 fallback) so random
uppercase tokens like ``USD`` / ``GDP`` / ``FOMC`` don't masquerade as
tickers. Event keywords are case-insensitive substring matches.
Sentiment is a naive bag-of-words tie-broken to ``neutral`` — the
analyst LLM applies real judgment downstream on the normalized payload.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# ---------------------------------------------------------------------------
# Whitelists / keyword sets
# ---------------------------------------------------------------------------

# Known-tickers whitelist — the user's holdings + a fallback S&P 500
# subset. The whitelist prevents random uppercase words ("USD" / "GDP"
# / "FOMC" / "AI" / "EU") from being treated as tickers. The production
# set will be assembled from the user's holdings table + an S&P 500
# import (out of scope for this commit — Stage 1 ships with this
# hardcoded fallback).
KNOWN_TICKERS_DEFAULT: frozenset[str] = frozenset({
    # User holdings (per current portfolio TSV)
    "NVDA", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "AAPL", "TSLA",
    "AVGO", "AMD", "TSM", "ASML", "INTC", "QCOM", "MU", "MRVL",
    "VOO", "VTI", "QQQ", "SPY", "IWM", "VXUS", "BND",
    # S&P 500 majors (sampled) — fallback list
    "BRK.B", "JPM", "V", "MA", "JNJ", "PG", "UNH", "HD", "BAC",
    "XOM", "CVX", "WMT", "PFE", "KO", "PEP", "DIS", "NFLX", "CRM",
    "ORCL", "CSCO", "ADBE", "ABT", "TMO", "LIN", "MRK", "ABBV",
    "COST", "MCD", "NKE", "T", "VZ", "BA", "CAT", "GE", "GS",
    "BLK", "MS", "C", "WFC", "AXP", "PYPL", "SQ", "SHOP", "UBER",
    "LYFT", "PLTR", "SNOW", "ZS", "CRWD", "DDOG", "MDB", "NET",
})

# Event keywords — case-insensitive substring match. Each matched
# keyword emits the lowercase canonical form on the signal.
EVENT_KEYWORDS: frozenset[str] = frozenset({
    "rate", "fed", "fomc", "cpi", "earnings", "merger", "m&a",
    "geopolitical", "taiwan", "war", "sanction", "tariff", "ai",
    "regulator", "antitrust", "lawsuit", "guidance", "beat", "miss",
})

# Sentiment lexicons — bag-of-words tie-broken to neutral.
_POSITIVE_TERMS: frozenset[str] = frozenset({
    "beat", "beats", "raise", "raised", "raises", "growth", "growing",
    "upgrade", "upgraded", "upgrades", "surge", "surged", "rally",
    "rallied", "outperform", "outperforms", "strong", "record",
})

_NEGATIVE_TERMS: frozenset[str] = frozenset({
    "miss", "misses", "missed", "cut", "cuts", "downgrade",
    "downgraded", "downgrades", "sanction", "sanctions", "sanctioned",
    "loss", "losses", "plunge", "plunged", "fall", "falls", "weak",
    "warn", "warns", "warning", "lawsuit", "probe",
})

# ---------------------------------------------------------------------------
# Token / regex helpers
# ---------------------------------------------------------------------------

# Ticker token candidates: either ``$NVDA`` / ``$nvda`` (cashtag, any
# case) or a bare ticker-shaped token (2-5 letters, optional ``.B``
# suffix for share-class tickers). We match case-insensitively so
# informal sources writing "nvda" or "Nvda" still surface the symbol;
# the whitelist intersection then drops random words like "the" /
# "but" / "and" that happen to be ≤5 letters.
_TICKER_RE = re.compile(r"\$?\b([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")

# Word-tokenizer for sentiment scoring — splits on non-alphabetic
# boundaries so "beat." / "beat," / "beats" all hit the lexicon.
_WORD_RE = re.compile(r"[A-Za-z&]+")

# Source-trust defaults — macro feeds are official calendars (BLS /
# FOMC) and rank ``high``. RSS / Discord are open inputs and default
# to ``medium``. ``low`` is reserved for explicitly-tagged untrusted
# sources (future surface).
_DEFAULT_TRUST: dict[str, Literal["high", "medium", "low"]] = {
    "macro_feed": "high",
    "rss": "medium",
    "discord": "medium",
}

# DB CHECK constraint: length(evidence_excerpt) <= 280. We trim at
# that boundary verbatim — the cleaned title+description is truncated
# to the first 280 characters.
EVIDENCE_MAX_LEN = 280


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedSignal:
    """Result of Stage 1 extraction on one raw text input.

    All Stage 1 fields are populated; Stage 2 fields (materiality /
    recommended_flag / rationale / analyzed_at) are written by the
    Stage 2 analyst in commit #14.
    """

    source: Literal["discord", "rss", "macro_feed"]
    source_ref: str
    received_at: datetime
    parsed_tickers: list[str]
    event_keywords: list[str]
    sentiment: Literal["positive", "neutral", "negative"]
    source_trust: Literal["high", "medium", "low"]
    evidence_excerpt: str
    raw_text: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract(
    *,
    source: Literal["discord", "rss", "macro_feed"],
    source_ref: str,
    raw_text: str,
    received_at: datetime,
    known_tickers: frozenset[str] | None = None,
) -> ExtractedSignal:
    """Run pure-Python Stage 1 extraction on one raw input.

    Args:
        source: One of ``discord`` / ``rss`` / ``macro_feed``.
        source_ref: Stable per-source ID (channel+msg_id for discord,
            URL for RSS, ``fomc-2026-06-18`` style for macro).
        raw_text: Full text — title + body for RSS, message for discord,
            event description for macro. Stored verbatim on the signal
            but NEVER fed to the Stage 2 LLM prompt.
        received_at: Timezone-aware datetime the item was published /
            observed. For macro events this is the event date itself.
        known_tickers: Override the default ticker whitelist. None →
            ``KNOWN_TICKERS_DEFAULT``.

    Returns:
        A frozen ``ExtractedSignal`` ready for persistence.
    """
    whitelist = known_tickers if known_tickers is not None else KNOWN_TICKERS_DEFAULT
    cleaned = _clean_text(raw_text)

    tickers = _extract_tickers(cleaned, whitelist)
    keywords = _extract_event_keywords(cleaned)
    sentiment = _score_sentiment(cleaned)
    trust = _DEFAULT_TRUST[source]
    excerpt = _make_evidence_excerpt(cleaned)

    return ExtractedSignal(
        source=source,
        source_ref=source_ref,
        received_at=received_at,
        parsed_tickers=tickers,
        event_keywords=keywords,
        sentiment=sentiment,
        source_trust=trust,
        evidence_excerpt=excerpt,
        raw_text=raw_text,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _clean_text(raw: str) -> str:
    """Normalize whitespace so the excerpt + tokenizers see a single
    canonical form. Collapses runs of whitespace (incl. newlines) to a
    single space and strips leading/trailing whitespace."""
    return re.sub(r"\s+", " ", raw).strip()


def _extract_tickers(text: str, whitelist: frozenset[str]) -> list[str]:
    """Match candidate ticker tokens, intersect with the whitelist,
    preserve first-seen order, dedupe.

    Returns canonical uppercase tickers with no ``$`` prefix.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _TICKER_RE.finditer(text):
        candidate = match.group(1).upper()
        if candidate in whitelist and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _extract_event_keywords(text: str) -> list[str]:
    """Case-insensitive substring match for each ``EVENT_KEYWORDS`` entry.
    Returns lowercase canonical form, deduped, in first-seen order."""
    lowered = text.lower()
    out: list[str] = []
    for kw in EVENT_KEYWORDS:
        if kw in lowered and kw not in out:
            out.append(kw)
    # Sort by first appearance in the text for deterministic order
    # (frozenset iteration is hash-randomized by default).
    out.sort(key=lambda k: lowered.find(k))
    return out


def _score_sentiment(text: str) -> Literal["positive", "neutral", "negative"]:
    """Bag-of-words sentiment score. Tie → neutral.

    Tokenizes on alphabetic runs (so ``"beat."``/``"beats"``/``"beat,"``
    all match the lexicon) and counts hits in each polarity. Equal
    counts (including 0/0) resolve to ``neutral``.
    """
    pos = 0
    neg = 0
    for tok in _WORD_RE.findall(text.lower()):
        if tok in _POSITIVE_TERMS:
            pos += 1
        elif tok in _NEGATIVE_TERMS:
            neg += 1
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _make_evidence_excerpt(cleaned: str) -> str:
    """Trim to ≤280 chars verbatim. The DB CHECK constraint
    ``length(evidence_excerpt) <= 280`` enforces the limit; this is
    where it gets honored."""
    if len(cleaned) <= EVIDENCE_MAX_LEN:
        return cleaned
    return cleaned[:EVIDENCE_MAX_LEN]
