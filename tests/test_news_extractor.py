"""Stage 1 deterministic extractor tests.

Sprint commit #13 of the plan/execute/monitor reorg. Validates the
pure-Python parser: ticker whitelist, event keywords, sentiment,
source_trust defaults, evidence_excerpt 280-char cap, idempotency.
NO LLM exercised — the whole point of Stage 1 is determinism.
"""
from __future__ import annotations

from datetime import UTC, datetime

from argosy.services.news_extractor import (
    EVIDENCE_MAX_LEN,
    extract,
)

_NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


def test_cashtag_resolves_to_canonical_ticker() -> None:
    """A ``$NVDA`` cashtag should produce parsed_tickers=["NVDA"]."""
    sig = extract(
        source="rss",
        source_ref="https://example.com/n/1",
        raw_text="Nvidia $NVDA crushes Q1 revenue.",
        received_at=_NOW,
    )
    assert "NVDA" in sig.parsed_tickers
    assert sig.parsed_tickers == ["NVDA"]


def test_bare_uppercase_ticker_in_whitelist_is_captured() -> None:
    """A bare ``NVDA`` (no $) in the whitelist should still be tagged."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="NVDA reports strong guidance.",
        received_at=_NOW,
    )
    assert "NVDA" in sig.parsed_tickers


def test_stray_uppercase_not_in_whitelist_is_dropped() -> None:
    """Random uppercase tokens like ``USD`` / ``GDP`` / ``FOMC`` are NOT
    tickers — they must not survive the whitelist filter."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="USD strength weighs on GDP outlook ahead of FOMC.",
        received_at=_NOW,
    )
    for fake in ("USD", "GDP", "FOMC"):
        assert fake not in sig.parsed_tickers, (
            f"{fake!r} should not be a ticker — it's not in the whitelist"
        )


def test_event_keyword_fomc_matches_case_insensitively() -> None:
    """``FOMC`` in mixed case should match the lowercase canonical kw."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="Investors await the FoMc decision next week.",
        received_at=_NOW,
    )
    assert "fomc" in sig.event_keywords


def test_sentiment_positive_when_beat_earnings() -> None:
    """``beat earnings`` triggers positive sentiment via the lexicon."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="NVDA beat earnings expectations with record revenue.",
        received_at=_NOW,
    )
    assert sig.sentiment == "positive"


def test_sentiment_negative_when_missed_guidance() -> None:
    """``missed guidance`` triggers negative sentiment."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="Company missed guidance; downgrade and lawsuit follow.",
        received_at=_NOW,
    )
    assert sig.sentiment == "negative"


def test_sentiment_neutral_when_no_polarity_words() -> None:
    """Tie / zero polarity → neutral."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="The Federal Reserve meets next month to discuss policy.",
        received_at=_NOW,
    )
    assert sig.sentiment == "neutral"


def test_evidence_excerpt_capped_at_280_chars() -> None:
    """The DB CHECK enforces length <= 280; the extractor must honor that.

    Pass a long string; verify the excerpt is exactly 280 chars.
    """
    long_text = "x" * 1000
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text=long_text,
        received_at=_NOW,
    )
    assert len(sig.evidence_excerpt) == EVIDENCE_MAX_LEN == 280


def test_evidence_excerpt_short_text_passes_through() -> None:
    """Short text < 280 chars passes through verbatim (with whitespace
    normalized)."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="Short headline.",
        received_at=_NOW,
    )
    assert sig.evidence_excerpt == "Short headline."


def test_source_trust_high_for_macro_feed() -> None:
    """Macro-feed entries are official calendars → source_trust='high'."""
    sig = extract(
        source="macro_feed",
        source_ref="fomc-2026-06-17",
        raw_text="FOMC rate decision.",
        received_at=_NOW,
    )
    assert sig.source_trust == "high"


def test_source_trust_medium_for_discord_and_rss() -> None:
    """Open inputs (discord / rss) default to source_trust='medium'."""
    for src in ("discord", "rss"):
        sig = extract(
            source=src,  # type: ignore[arg-type]
            source_ref="x",
            raw_text="Some headline.",
            received_at=_NOW,
        )
        assert sig.source_trust == "medium", f"expected medium for {src}"


def test_idempotent_re_extraction() -> None:
    """Same input → same output. The extractor is pure."""
    kwargs = dict(
        source="rss",
        source_ref="https://example.com/n/42",
        raw_text="$NVDA beat earnings; FOMC ahead next week.",
        received_at=_NOW,
    )
    a = extract(**kwargs)  # type: ignore[arg-type]
    b = extract(**kwargs)  # type: ignore[arg-type]
    assert a == b
    # Spot-check shape too, not just equality.
    assert a.parsed_tickers == ["NVDA"]
    assert "fomc" in a.event_keywords
    assert a.sentiment == "positive"


def test_parsed_tickers_dedup_and_uppercase() -> None:
    """Repeated ticker mentions dedupe; $ prefix stripped; case canonical."""
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="$NVDA up. NVDA again. nvda lowercase ignored.",
        received_at=_NOW,
    )
    # nvda lowercase isn't matched by the [A-Z]{1,5} regex — correct.
    assert sig.parsed_tickers == ["NVDA"]


def test_custom_whitelist_overrides_default() -> None:
    """Passing ``known_tickers`` overrides KNOWN_TICKERS_DEFAULT.

    Uses a 5-char ticker (``WDGET``) — the regex caps candidate tokens
    at 5 uppercase letters to match real-world ticker shapes, so the
    custom symbol must respect that bound.
    """
    custom = frozenset({"WDGET"})
    sig = extract(
        source="rss",
        source_ref="x",
        raw_text="WDGET corp announces NVDA partnership.",
        received_at=_NOW,
        known_tickers=custom,
    )
    assert "WDGET" in sig.parsed_tickers
    assert "NVDA" not in sig.parsed_tickers  # not in the custom whitelist
