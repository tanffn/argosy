"""Confidence helpers on the canonical CurrentBook accessor.

The DB-integration path (load_current_book selecting the head + real-today
book) is exercised by the resolver/dashboard tests that now go through it;
here we pin the confidence contract that every money surface relies on.
"""
from __future__ import annotations

from argosy.services.current_book import HIGH, MEDIUM, CurrentBook
from argosy.services.holding_books import TotalBookResult, UnmanagedLoadResult


def _cb(stale: tuple[str, ...] = (), *, degraded: bool = False) -> CurrentBook:
    return CurrentBook(
        snapshot=object(),
        result=TotalBookResult(
            total=[], managed=[],
            load=UnmanagedLoadResult(rows=[], ok=True),
            degraded=degraded, degrade_reason=None, stale_marks=stale,
        ),
        snapshot_id=1, snapshot_date=None, fx_usd_nis=3.0, fx_usd_eur=None,
    )


def test_book_confidence_high_only_when_fully_fresh():
    assert _cb().book_confidence() == HIGH
    # ANY soft-stale mark degrades a multi-symbol / denominator figure
    assert _cb(stale=("AAPL@broker",)).book_confidence() == MEDIUM


def test_symbol_confidence_scopes_to_the_symbol():
    b = _cb(stale=("AAPL@broker",))
    assert b.symbol_confidence("NVDA") == HIGH   # NVDA's own mark is fresh
    assert b.symbol_confidence("aapl") == MEDIUM  # case-insensitive


def test_stale_note_empty_when_fresh_and_present_when_stale():
    assert _cb().stale_note() == ""
    note = _cb(stale=("AAPL@x",)).stale_note()
    assert "STALE MARK" in note and "AAPL@x" in note


def test_empty_book_is_not_degraded_and_flags_empty():
    empty = CurrentBook(
        snapshot=None,
        result=TotalBookResult(
            total=[], managed=[],
            load=UnmanagedLoadResult(rows=[], ok=True), degraded=False,
        ),
        snapshot_id=None, snapshot_date=None, fx_usd_nis=None, fx_usd_eur=None,
    )
    assert empty.is_empty is True
    assert empty.degraded is False
    assert empty.book_confidence() == HIGH  # vacuously; callers gate on is_empty
