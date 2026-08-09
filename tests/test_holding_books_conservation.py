"""Post-restore conservation fixes in holding_books.py.

Finding 3: two blank-symbol lots in the same location must not collide on the
dedupe key (a real Leumi cash lot, $5,446.93, was silently dropped).
Finding 2: a carried quantity's observation date must survive a merge/reprice
(re-dating it to "today" every self-refresh permanently disarmed the 90-day
quantity-staleness guard).
"""
from __future__ import annotations

from datetime import date

from argosy.services.holding_books import (
    TotalBookResult,
    UnmanagedLoadResult,
    dedupe_positions_by_symbol_location,
    merge_positions_per_account,
    merge_total_book_positions,
)


def test_finding3_blank_symbol_lots_not_collapsed():
    pos = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash", "usd_value_k": 17.66},
        {"symbol": "", "location": "Leumi", "asset_type": "Cash", "usd_value_k": 5.45},
        {"symbol": "NVDA", "location": "schwab", "usd_value_k": 100.0},
        {"symbol": "NVDA", "location": "schwab", "usd_value_k": 100.0},  # true dup
    ]
    out = dedupe_positions_by_symbol_location(pos)
    blanks = [p for p in out if not (p.get("symbol") or "").strip()]
    nvda = [p for p in out if p.get("symbol") == "NVDA"]
    # both distinct cash lots survive — no silent money drop
    assert len(blanks) == 2
    assert sum(p["usd_value_k"] for p in blanks) == 17.66 + 5.45
    # named-symbol dedupe still fires (no $200k phantom NVDA double-count)
    assert len(nvda) == 1


def test_finding2_carried_observed_date_preserved_but_fresh_row_stamped():
    # incoming = a self-refresh snapshot dated 2026-08-08. NVDA is a carried
    # holding whose true quantity date is 2026-07-13; CSPX is a fresh feed row
    # with no observation date of its own.
    incoming = [
        {"symbol": "NVDA", "location": "schwab", "shares": 10940,
         "observed_as_of": "2026-07-13", "usd_value_k": 2450.0},
        {"symbol": "CSPX", "location": "Leumi", "usd_value_k": 50.0},
    ]
    res = merge_positions_per_account(
        prior_positions=[],
        incoming_positions=incoming,
        incoming_snapshot_date=date(2026, 8, 8),
    )
    by_sym = {p.get("symbol"): p for p in res.positions}
    # carried quantity keeps its true observation date (guard stays armed)
    assert str(by_sym["NVDA"]["observed_as_of"]).startswith("2026-07-13")
    # ...but the mark date is legitimately today (it was just repriced)
    assert str(by_sym["NVDA"]["valued_as_of"]).startswith("2026-08-08")
    # a fresh feed row (no carried date) takes the feed date
    assert str(by_sym["CSPX"]["observed_as_of"]).startswith("2026-08-08")


def test_finding2_no_carried_date_falls_back_to_feed_date():
    incoming = [{"symbol": "AMD", "location": "Leumi", "usd_value_k": 25.0}]
    res = merge_positions_per_account(
        prior_positions=[],
        incoming_positions=incoming,
        incoming_snapshot_date=date(2026, 8, 8),
    )
    p = res.positions[0]
    assert str(p["observed_as_of"]).startswith("2026-08-08")
    assert str(p["valued_as_of"]).startswith("2026-08-08")


# --- Sol BLOCK re-review fixes (2026-08-09) --------------------------------


def test_block3_stale_mark_valued_date_not_laundered_to_feed_date():
    # snapshot_refresh emits a KNOWN-stale row (quote miss) with its true older
    # valued_as_of + mark_stale=True. The per-account merge must NOT overwrite
    # that mark date to the incoming feed date — doing so laundered a stale
    # mark into a fresh one (Sol BLOCK-3).
    incoming = [
        {"symbol": "NVDA", "location": "schwab", "shares": 10940,
         "observed_as_of": "2026-07-13", "valued_as_of": "2026-07-13",
         "mark_stale": True, "usd_value_k": 2450.0},
    ]
    res = merge_positions_per_account(
        prior_positions=[],
        incoming_positions=incoming,
        incoming_snapshot_date=date(2026, 8, 8),
    )
    nvda = {p.get("symbol"): p for p in res.positions}["NVDA"]
    assert str(nvda["valued_as_of"]).startswith("2026-07-13")  # NOT laundered
    assert nvda.get("mark_stale") is True
    # a NON-stale fresh feed row still takes the feed date (no regression)
    fresh = merge_positions_per_account(
        prior_positions=[],
        incoming_positions=[{"symbol": "CSPX", "location": "Leumi",
                             "usd_value_k": 50.0}],
        incoming_snapshot_date=date(2026, 8, 8),
    ).positions[0]
    assert str(fresh["valued_as_of"]).startswith("2026-08-08")


def test_block3_carryonly_unchanged_balance_keeps_mark_date():
    # A carry-only cash balance (NOT flagged mark_stale) that was never
    # re-observed must keep its OWN mark date across a refresh — not be
    # re-stamped "fresh" to the feed date every day (Sol round-3 #2: the
    # carry-only persistence path, distinct from the priceable-miss path).
    incoming = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "usd_value_k": 10.0, "valued_as_of": "2026-08-01", "raw_line": 5},
    ]
    res = merge_positions_per_account(
        prior_positions=[],
        incoming_positions=incoming,
        incoming_snapshot_date=date(2026, 8, 9),
    )
    p = res.positions[0]
    assert str(p["valued_as_of"]).startswith("2026-08-01")  # NOT laundered to 08-09


def test_block5_same_source_row_collapses_distinct_lots_survive():
    # SAME raw_line = the same parsed source row appearing twice -> collapse
    # (no double-count). A distinct source lot (different raw_line) survives.
    pos = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 12},
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 12},  # same row
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 17.66, "raw_line": 13},
    ]
    out = dedupe_positions_by_symbol_location(pos)
    blanks = [p for p in out if not (p.get("symbol") or "").strip()]
    assert len(blanks) == 2
    assert sorted(p["usd_value_k"] for p in blanks) == [5.45, 17.66]


def test_block5_distinct_identical_blank_lots_both_survive_not_dropped():
    # Two DISTINCT source lines with identical content -> BOTH survive; dedupe
    # must never silently drop one (the original $5,446.93-drop failure mode,
    # and the content-key regression Sol round-2 #3 caught).
    pos = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 20},
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 21},
    ]
    out = dedupe_positions_by_symbol_location(pos)
    blanks = [p for p in out if not (p.get("symbol") or "").strip()]
    assert len(blanks) == 2
    assert sum(p["usd_value_k"] for p in blanks) == 10.90


def test_block5_ambiguous_identical_blank_lots_fail_loud():
    import pytest

    from argosy.services.holding_books import books_consistency_check_positions

    ambiguous = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 20},
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 21},
    ]
    # identical NONZERO content, distinct rows -> can't tell dup from real -> loud
    with pytest.raises(AssertionError):
        books_consistency_check_positions(ambiguous)

    # distinct values never trip it (the real live-book Leumi cash case)
    ok = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 20},
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 17.66, "raw_line": 21},
    ]
    books_consistency_check_positions(ok)  # no raise


def test_block1_soft_stale_priceable_records_stale_mark_not_degraded():
    # NVDA priceable, mark 8 days old (soft tier: MARK_STALE_DAYS<age<=HARD),
    # live reprice MISSES (quote_fn returns None) → publish last close flagged,
    # do NOT degrade the book, but RECORD the symbol on stale_marks so a
    # HIGH-confidence consumer downgrades (Sol BLOCK-1).
    stale: list[str] = []
    pos = [{
        "symbol": "NVDA", "location": "schwab", "shares": 100,
        "currency": "USD", "asset_type": "Stock",
        "valued_as_of": "2026-08-01", "observed_as_of": "2026-07-13",
        "current_price": 100.0, "usd_value_k": 10.0,
    }]
    out = merge_total_book_positions(
        pos, today=date(2026, 8, 9),
        quote_fn=lambda *a, **k: None,  # deterministic reprice miss
        stale_marks=stale,
    )
    nvda = [p for p in out if p.get("symbol") == "NVDA"][0]
    assert nvda.get("mark_stale") is True
    assert nvda.get("usd_value_k") == 10.0  # last close retained (graceful)
    assert any(m.startswith("NVDA@") for m in stale)


def test_block5r5_distinct_accounts_same_rawline_both_survive():
    # Two DIFFERENT accounts can carry the same source-line ordinal after
    # per-account/history merges; keying blanks on raw_line ALONE collapsed a
    # Leumi and a UBS lot and dropped one (Sol round-5 #1). Location is in the
    # key, so both survive.
    pos = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.0, "raw_line": 12},
        {"symbol": "", "location": "UBS", "asset_type": "Cash",
         "currency": "USD", "usd_value_k": 7.0, "raw_line": 12},
    ]
    out = dedupe_positions_by_symbol_location(pos)
    blanks = [p for p in out if not (p.get("symbol") or "").strip()]
    assert len(blanks) == 2
    assert sum(p["usd_value_k"] for p in blanks) == 12.0


def test_block4r7_zero_rawline_identical_blanks_fail_loud():
    # raw_line=0 is the model's MISSING default — dedupe treats 0 as falsy
    # (per-occurrence, both survive), so the integrity check must treat 0 as
    # missing too and FAIL LOUD, else two identical raw_line=0 cash rows would
    # publish double (Sol round-7 #4).
    import pytest

    from argosy.services.holding_books import books_consistency_check_positions

    pos = [
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 0},
        {"symbol": "", "location": "Leumi", "asset_type": "Cash",
         "currency": "NIS", "usd_value_k": 5.45, "raw_line": 0},
    ]
    with pytest.raises(AssertionError):
        books_consistency_check_positions(pos)


def test_block4r5_hard_stale_cash_recorded_in_stale_marks():
    # Hard-stale cash (can't be repriced) is KEPT but must be flagged on
    # stale_marks so consumers downgrade — it previously bypassed both
    # degradation and the confidence downgrade (Sol round-5 #4).
    stale: list[str] = []
    pos = [{
        "symbol": "", "location": "Leumi", "asset_type": "Cash",
        "currency": "NIS", "usd_value_k": 100.0, "valued_as_of": "2026-07-01",
    }]
    out = merge_total_book_positions(
        pos, today=date(2026, 8, 9), stale_marks=stale,  # 39 days -> HARD
    )
    c = out[0]
    assert c.get("mark_stale") is True
    assert c.get("usd_value_k") == 100.0  # cash balance retained
    assert any("@leumi" in m for m in stale)  # but recorded for downgrade


def test_block1_total_book_result_is_mark_stale_helper():
    r = TotalBookResult(
        total=[], managed=[],
        load=UnmanagedLoadResult(rows=[], ok=True),
        degraded=False, stale_marks=("NVDA@schwab",),
    )
    assert r.is_mark_stale("nvda") is True
    assert r.is_mark_stale("AAPL") is False
