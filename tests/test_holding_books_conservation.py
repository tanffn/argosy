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
    dedupe_positions_by_symbol_location,
    merge_positions_per_account,
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
