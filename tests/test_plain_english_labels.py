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

    def test_dated_fundamentals_slash(self):
        # Real producer shape (codex zigzag (c) #B1).
        assert (
            friendly_source_label("fundamentals/NVDA/2026-05-29")
            == "NVDA fundamentals (2026-05-29)"
        )

    def test_dated_fundamentals_colon_legacy(self):
        # Legacy colon shape -- still accepted.
        assert (
            friendly_source_label("fundamentals:NVDA:2026-05-29")
            == "NVDA fundamentals (2026-05-29)"
        )

    def test_dated_news_slash(self):
        assert (
            friendly_source_label("news/AMD/2026-05-15")
            == "AMD news (2026-05-15)"
        )

    def test_dated_technical_slash(self):
        assert (
            friendly_source_label("technical/QQQM/2026-04-30")
            == "QQQM technical (2026-04-30)"
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


class TestPythonTsParity:
    """Codex zigzag (c)#I5 (2026-05-29): the Python + TS mirror modules
    can silently diverge. This test loads the TS file as text and
    asserts the prefix list + dated-kind labels match the Python side.
    Add new entries to BOTH files when a new namespace ships.
    """

    def test_source_prefix_set_in_sync(self):
        import re
        from pathlib import Path
        from argosy.services.plain_english_labels import (
            _SOURCE_PREFIX_LABELS,
        )

        ts_path = (
            Path(__file__).resolve().parent.parent
            / "ui" / "src" / "lib" / "plain-english-labels.ts"
        )
        ts_text = ts_path.read_text(encoding="utf-8")
        # Extract the list of prefix strings from the TS source.
        # Lines look like: ["indicators/", (rest) => `${rest} technical indicators`],
        ts_prefixes = set(re.findall(r'\["([^"]+)",', ts_text))
        py_prefixes = {p for p, _ in _SOURCE_PREFIX_LABELS}

        only_python = py_prefixes - ts_prefixes
        only_ts = ts_prefixes - py_prefixes
        # Filter TS-side false positives (other string-array literals).
        only_ts = {x for x in only_ts if "/" in x or ":" in x}

        assert not only_python, (
            f"Source-ID prefixes in Python but missing from TS mirror "
            f"({ts_path}): {sorted(only_python)}. Add matching entries "
            f"to ui/src/lib/plain-english-labels.ts."
        )
        assert not only_ts, (
            f"Source-ID prefixes in TS but missing from Python "
            f"(argosy/services/plain_english_labels.py): {sorted(only_ts)}. "
            f"Add matching entries to plain_english_labels.py."
        )

    def test_dated_kind_label_in_sync(self):
        import re
        from pathlib import Path
        from argosy.services.plain_english_labels import _DATED_KIND_LABEL

        ts_path = (
            Path(__file__).resolve().parent.parent
            / "ui" / "src" / "lib" / "plain-english-labels.ts"
        )
        ts_text = ts_path.read_text(encoding="utf-8")
        # Extract DATED_KIND_LABEL block: matches `key: "value",` lines.
        block_match = re.search(
            r"DATED_KIND_LABEL[^{]*\{([^}]+)\}", ts_text, re.DOTALL,
        )
        assert block_match, "DATED_KIND_LABEL not found in TS mirror"
        ts_keys = set(re.findall(r"(\w+):\s*\"", block_match.group(1)))
        py_keys = set(_DATED_KIND_LABEL.keys())

        only_python = py_keys - ts_keys
        only_ts = ts_keys - py_keys

        assert not only_python, (
            f"Dated-kind labels in Python but missing from TS: "
            f"{sorted(only_python)}"
        )
        assert not only_ts, (
            f"Dated-kind labels in TS but missing from Python: "
            f"{sorted(only_ts)}"
        )
