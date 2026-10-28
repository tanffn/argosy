from datetime import UTC, datetime

import pandas as pd
import pytest

from argosy.services.shadow_market import normalize_history


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
            "source": "fixture", "actions": []} for day in days}, None
    monkeypatch.setattr(provider, "fetch_listing", fetch)
    seed = {"activation_date": "2026-01-01", "identities": {
        key: {"instrument": key, "provider": key} for key in ("AAA", "SPY")}, "cash": {}}
    packets, _ = provider.collect(seed, [], now=datetime(2026, 1, 6, 23, tzinfo=UTC))
    packet = next(row for row in packets if row["date"] == "2026-01-05")
    assert "AAA" not in packet["action_coverage"]
    assert "AAA" in packet["unresolved_actions"]
