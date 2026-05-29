"""Tests for plain_english_labels.py — static sanitizer for the four
config-key leak surfaces flagged by the user on 2026-05-29.
"""
from __future__ import annotations

from argosy.services.plain_english_labels import (
    friendly_agent_role,
    friendly_source_label,
    friendly_source_labels,
)


class TestFriendlyAgentRole:
    def test_known_role_maps(self):
        assert friendly_agent_role("fundamentals_analyst") == "fundamentals"
        assert friendly_agent_role("sentiment_analyst") == "sentiment"
        assert friendly_agent_role("plan_synthesizer") == "plan synthesizer"
        assert friendly_agent_role("bull_researcher") == "bull case"

    def test_unknown_role_falls_back(self):
        # _analyst suffix stripped + underscores -> spaces.
        assert friendly_agent_role("custom_unknown_analyst") == "custom unknown"

    def test_none_defaults_to_analyst(self):
        assert friendly_agent_role(None) == "analyst"
        assert friendly_agent_role("") == "analyst"


class TestFriendlySourceLabel:
    def test_indicators_prefix(self):
        assert friendly_source_label("indicators/NVDA") == "NVDA technical indicators"

    def test_dated_fundamentals(self):
        assert (
            friendly_source_label("fundamentals:NVDA:2026-05-29")
            == "NVDA fundamentals (2026-05-29)"
        )

    def test_agent_report_id(self):
        assert friendly_source_label("agent_report:12345") == "agent report #12345"

    def test_fx_pair(self):
        assert friendly_source_label("fx/USD/NIS") == "FX USD NIS"

    def test_unrecognized_returns_verbatim(self):
        # Caller decides whether to render raw.
        assert friendly_source_label("some-random-id") == "some-random-id"

    def test_empty_returns_empty(self):
        assert friendly_source_label("") == ""


class TestFriendlySourceLabels:
    def test_dedups_and_caps(self):
        out = friendly_source_labels(
            ["indicators/NVDA", "indicators/NVDA", "fx/USD/NIS"],
            max_count=6,
        )
        assert out == ["NVDA technical indicators", "FX USD NIS"]

    def test_cap_respected(self):
        many = [f"indicators/T{i}" for i in range(10)]
        out = friendly_source_labels(many, max_count=3)
        assert len(out) == 3
