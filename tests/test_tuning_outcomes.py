from datetime import date

import pytest

from argosy.services.recommendation_history import compare_window
from evals.fleet_calibration import tuning_outcomes as outcomes


def row(price, spy_price=100):
    result = compare_window(event_at="2026-01-01", days=30,
        subject={date(2026, 1, 2): 100, date(2026, 1, 31): price},
        benchmark={date(2026, 1, 2): 100, date(2026, 1, 31): spy_price}, as_of=date(2026, 2, 1))
    return {"partition": "development", "original_policy_hash": "original", "original_go": False,
            "outcome": result}


def test_inclusive_thresholds_resist_rounding_but_not_meaningful_near_misses():
    assert outcomes.summarize([row(120)])[0]["rose20pct_and_beat_spy"] == 1
    assert outcomes.summarize([row(80)])[0]["fell20pct"] == 1
    assert outcomes.summarize([row(119.99999)])[0]["rose20pct_and_beat_spy"] == 0
    assert outcomes.summarize([row(80.00001)])[0]["fell20pct"] == 0
    assert outcomes.summarize([row(120, 120)])[0]["rose20pct_and_beat_spy"] == 0


def test_cache_requires_explicit_requested_range_and_usd():
    sample = {"status": "available", "currency": "USD", "bars": {"2026-01-05": 100}}
    assert not outcomes.cache_covers(sample, date(2026, 1, 1), date(2026, 2, 1))
    sample.update(requested_start="2026-01-04", requested_end="2026-02-01")
    assert not outcomes.cache_covers(sample, date(2026, 1, 1), date(2026, 2, 1))
    sample["requested_start"] = "2025-12-01"
    assert outcomes.cache_covers(sample, date(2026, 1, 1), date(2026, 2, 1))
    sample["currency"] = "ILS"
    assert not outcomes.cache_covers(sample, date(2026, 1, 1), date(2026, 2, 1))


def test_invalid_or_unverified_audit_is_rejected_before_fetch():
    snapshot = {"audit_screen_ids": [1]}
    audit = {"kind": "screen_reasoning_audit", "status": "completed", "snapshot_sha256": "same",
             "selected_report_ids": [1], "batches": [{"status": "completed", "coverage_verified": True}]}
    outcomes.require_completed_audit(snapshot, "same", audit)
    with pytest.raises(ValueError, match="no prices fetched"):
        outcomes.require_completed_audit(snapshot, "different", audit)
    audit["batches"][0]["coverage_verified"] = False
    with pytest.raises(ValueError, match="no prices fetched"):
        outcomes.require_completed_audit(snapshot, "same", audit)
