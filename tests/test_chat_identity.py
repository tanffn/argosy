from datetime import UTC, datetime

from argosy.services.chat_advisor.contracts import Principal
from argosy.services.chat_advisor.identity import lookup_identity
from argosy.services.chat_advisor.retrieval import RetrievalService


def test_identity_is_generic_exact_ticker_or_name_with_dated_primary_source(monkeypatch):
    monkeypatch.setattr("argosy.services.chat_advisor.identity.issuer_directory", lambda: (
        datetime(2026, 9, 19, tzinfo=UTC),
        [{"ticker": "AAA", "title": "Alpha Exploration Inc", "cik_str": 123},
         {"ticker": "BBB", "title": "Beta Software Corp", "cik_str": 456}],
    ))
    for query, expected in (("aaa", "AAA"), ("Beta Software", "BBB")):
        result = lookup_identity([query])
        assert result["total_matches"] == 1
        assert result["matches"][0]["ticker"] == expected
        assert result["verified_at"].startswith("2026-09-19")
    assert lookup_identity(["unknown"])["matches"] == []


def test_identity_failure_is_uncertainty_not_distinct_entities(monkeypatch):
    def fail(*args):
        raise TimeoutError("provider unavailable")
    monkeypatch.setattr("argosy.services.chat_advisor.identity.lookup_identity", fail)
    reader = RetrievalService(session_factory=lambda: None)
    result = reader.read(Principal("u", "g", "c", "d"), "identity", query="AAA")
    assert result.data["verified"] is False
    assert "do not assert the names are the same or different" in result.warnings[0]
    assert result.citations == []
