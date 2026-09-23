"""Both callers must populate the same complete canonical cache value."""
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from argosy.api.routes import retirement
from argosy.services import derived_cache, home_greeting
from argosy.services.retirement import retirement_plan
from argosy.services.retirement.scenario_mc import FeasibleAgeResult


@pytest.mark.parametrize("home_first", [True, False])
def test_shared_cache_is_complete_in_both_page_orders(monkeypatch, home_first):
    result = FeasibleAgeResult(48, .92, .90, 50, 60, 67, 44, 100000, {"source": "canonical"})
    calls = []

    def compute(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setenv("ARGOSY_DERIVED_CACHE", "1")
    monkeypatch.setattr(retirement_plan, "canonical_feasible_dual_track", compute)
    monkeypatch.setattr(retirement, "canonical_feasible_dual_track", compute)
    monkeypatch.setattr(derived_cache, "version_tuple", lambda *args: ("same-user-plan-snapshot",))
    derived_cache.clear()
    try:
        if home_first:
            home_greeting._fi_line(None, "ariel", now=datetime(2026, 9, 11, tzinfo=UTC))
        payload = retirement.get_feasible_age("ariel", db=None)
        home_greeting._fi_line(None, "ariel", now=datetime(2026, 9, 11, tzinfo=UTC))
        assert payload == asdict(result)
        assert len(calls) == 1
    finally:
        derived_cache.clear()
