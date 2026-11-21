from datetime import UTC, datetime

import pandas as pd
import pytest

from argosy.services.shadow_market import normalize_history
from tests.test_shadow_portfolio import store  # noqa: F401 -- shared SQLite ledger fixture


def test_split_adjusted_close_is_restored_without_adding_dividends_twice():
    frame = pd.DataFrame({"Close": [10, 5], "Dividends": [1, 0], "Stock Splits": [0, 2]},
                         index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
    normalized = normalize_history(frame, instrument="ABC", quote_currency="USD", source="fixture")
    assert normalized["2026-01-02"]["price"] == "20"
    assert normalized["2026-01-02"]["actions"][0]["amount"] == "2"
    assert normalized["2026-01-05"]["price"] == "5"


def test_missing_action_history_is_not_assumed_to_mean_no_actions():
    with pytest.raises(ValueError, match="corporate-action"):
        normalize_history(pd.DataFrame({"Close": [10]}, index=pd.to_datetime(["2026-01-02"])),
                          instrument="ABC", quote_currency="USD", source="fixture")


def test_capital_distributions_are_not_lost():
    frame = pd.DataFrame({"Close": [90], "Dividends": [0], "Stock Splits": [0], "Capital Gains": [10]},
                         index=pd.to_datetime(["2026-01-02"]))
    quote = normalize_history(frame, instrument="ABC", quote_currency="USD", source="fixture")["2026-01-02"]
    assert quote["actions"][0]["amount"] == "10"
    assert quote["actions"][0]["classification"] == "unverified_capital_distribution"


def test_pence_and_pounds_are_not_confused():
    frame = pd.DataFrame({"Close": [250], "Dividends": [0], "Stock Splits": [0]},
                         index=pd.to_datetime(["2026-01-02"]))
    quote = normalize_history(frame, instrument="ABC.L", quote_currency="GBp", source="fixture",
                              calendar_name="XLON")["2026-01-02"]
    assert quote["price"] == "2.50" and quote["currency"] == "GBP"


def test_early_close_comes_from_exchange_calendar():
    frame = pd.DataFrame({"Close": [100], "Dividends": [0], "Stock Splits": [0]},
                         index=pd.to_datetime(["2026-11-27"]))
    quote = normalize_history(frame, instrument="ABC", quote_currency="USD", source="fixture")["2026-11-27"]
    assert datetime.fromisoformat(quote["close_at"]) == datetime(2026, 11, 27, 18, tzinfo=UTC)


def test_partial_session_history_is_not_complete_action_coverage(monkeypatch):
    import argosy.services.shadow_market as provider
    def fetch(identity, start, now):
        days = ["2026-01-02", "2026-01-06"] if identity["instrument"] == "AAA" else ["2026-01-02", "2026-01-05", "2026-01-06"]
        return {day: {"price": "10", "currency": "USD", "as_of": day, "eligible": True,
            "source": "fixture", "action_history_complete": True, "actions": []} for day in days}, None
    monkeypatch.setattr(provider, "fetch_listing", fetch)
    seed = {"activation_date": "2026-01-01", "identities": {
        key: {"instrument": key, "provider": key} for key in ("AAA", "SPY")}, "cash": {}}
    packets, _ = provider.collect(seed, [], now=datetime(2026, 1, 6, 23, tzinfo=UTC))
    packet = next(row for row in packets if row["date"] == "2026-01-05")
    assert "AAA" not in packet["action_coverage"]
    assert "AAA" in packet["unresolved_actions"]


def test_collect_separates_missing_close_from_distribution_and_action_coverage(monkeypatch):
    import argosy.services.shadow_market as provider

    frame = pd.DataFrame({"Close": [101.0, 100.0, float("nan"), 99.0],
                          "Dividends": [0.0, 0.1743, 0.25, float("nan")],
                          "Stock Splits": [0, 0, 0, 0]},
                         index=pd.to_datetime(["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]))
    bars = normalize_history(frame, instrument="AAA.L", quote_currency="USD", source="fixture",
                             calendar_name="XLON")
    monkeypatch.setattr(provider, "fetch_listing", lambda *args: (bars, None))
    seed = {"activation_date": "2026-11-16", "identities": {
        "AAA.L": {"instrument": "AAA.L", "provider": "AAA.L"}}, "cash": {}}
    packets, collection = provider.collect(seed, [], now=datetime(2026, 11, 21, 7, tzinfo=UTC))
    by_date = {packet["date"]: packet for packet in packets}
    intact = by_date["2026-11-18"]
    assert intact["marks"]["AAA.L"]["price"] == "100.0" and not intact["failures"]
    assert intact["actions"][0]["amount"] == "0.1743"
    missing_close = by_date["2026-11-19"]
    assert "AAA.L" not in missing_close["marks"]
    assert missing_close["unpriced_marks"]["AAA.L"]["split_restoration_factor"] == "1"
    assert missing_close["action_coverage"] == ["AAA.L"] and not missing_close["unresolved_actions"]
    assert missing_close["actions"][0]["amount"] == "0.25"
    assert "close" in missing_close["failures"]["AAA.L"]
    missing_dividend = by_date["2026-11-20"]
    assert missing_dividend["marks"]["AAA.L"]["price"] == "99.0"
    assert missing_dividend["unresolved_actions"] == ["AAA.L"] and not missing_dividend["action_coverage"]
    assert "Dividends" in collection["failures"]["AAA.L"]


def test_unknown_split_leaves_only_independently_restorable_prices_eligible():
    frame = pd.DataFrame({"Close": [20, 21, 22], "Stock Splits": [0, float("nan"), 0],
                          "Dividends": [1, 0, 0]},
                         index=pd.to_datetime(["2026-11-18", "2026-11-19", "2026-11-20"]))
    bars = normalize_history(frame, instrument="AAA", quote_currency="USD", source="fixture")
    assert bars["2026-11-18"]["price"] is None
    assert bars["2026-11-18"]["split_restoration_factor"] is None
    assert not bars["2026-11-18"]["action_history_complete"]
    assert not bars["2026-11-19"]["action_history_complete"]
    assert bars["2026-11-20"]["eligible"] and bars["2026-11-20"]["action_history_complete"]


def test_missing_entire_later_session_invalidates_only_earlier_restored_units():
    frame = pd.DataFrame({"Close": [20, 22], "Stock Splits": [0, 0], "Dividends": [0, 0]},
                         index=pd.to_datetime(["2026-11-18", "2026-11-20"]))
    bars = normalize_history(frame, instrument="AAA", quote_currency="USD", source="fixture",
                             latest_missing_session=datetime(2026, 11, 19).date())
    assert bars["2026-11-18"]["split_restoration_factor"] is None
    assert not bars["2026-11-18"]["eligible"] and bars["2026-11-20"]["eligible"]


@pytest.mark.parametrize("corrected_factor", [None, "2"])
def test_recollection_reuses_a_previous_close_only_with_compatible_units(store, corrected_factor):
    import json
    from datetime import timedelta

    from sqlalchemy import select

    from argosy.services.shadow_daily import run_user
    from argosy.services.shadow_market import CONTRACT
    from argosy.state.shadow_models import ShadowEvent
    from tests.test_shadow_portfolio import collected

    def recollect(mode, factor="1"):
        def collector(*args, **kwargs):
            packets, info = collected(*args, **kwargs)
            packet = packets[-1]
            mark = packet["marks"]["AAA"]
            units = {"contract": CONTRACT, "as_of": packet["date"], "currency": "USD",
                     "quote_scale": "1", "split_restoration_factor": factor}
            if mode == "observed":
                mark.update(units)
            else:
                del packet["marks"]["AAA"]
                if mode == "missing_close":
                    packet["unpriced_marks"] = {"AAA": units}
            return packets, info
        return collector

    now = datetime(2026, 1, 6, 23, tzinfo=UTC)
    for tick, (mode, factor) in enumerate([("observed", "1"), ("transport_outage", "1"),
                                           ("missing_close", "1.0"), ("missing_close", corrected_factor)]):
        result = run_user(store, "a", now=now + timedelta(seconds=tick), collector=recollect(mode, factor))
        value = result["report"]["latest"]["books"]["gross"]["unchanged"]
        assert value["coverage_complete"] == (tick < 3)
    with store() as db:
        markets = [json.loads(row.payload_json) for row in db.scalars(select(ShadowEvent).where(
            ShadowEvent.kind == "market").order_by(ShadowEvent.id))]
    revisions = [market for market in markets if market["date"] == "2026-01-06"]
    assert revisions[0]["marks"]["AAA"]["price"] == "12"
    assert "AAA" not in revisions[-1]["marks"]
