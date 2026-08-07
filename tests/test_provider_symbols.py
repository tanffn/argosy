"""Class-share ticker normalization per provider (BRK/B fix)."""
from __future__ import annotations

import pytest

from argosy.adapters.data.symbols import to_finnhub_symbol, to_yahoo_symbol


@pytest.mark.parametrize(
    "raw,yahoo,finnhub",
    [
        ("BRK/B", "BRK-B", "BRK.B"),
        ("BRK.B", "BRK-B", "BRK.B"),  # already-dot form -> yahoo dash, finnhub unchanged
        ("GOOG", "GOOG", "GOOG"),      # plain ticker unchanged
        ("goog", "GOOG", "goog"),      # yahoo uppercases; finnhub leaves case
        ("  AMD ", "AMD", "AMD"),      # whitespace stripped
    ],
)
def test_provider_normalization(raw, yahoo, finnhub):
    assert to_yahoo_symbol(raw) == yahoo
    assert to_finnhub_symbol(raw) == finnhub


def test_hebrew_and_dash_tickers_untouched():
    # TA-200 trackers: dash + double-quote, no latin slash -> no-op on the
    # slash replace (yahoo upper() is a no-op on Hebrew).
    for t in ['ת"א-200', 'מחקה ת"א-200']:
        assert to_finnhub_symbol(t) == t.strip()
        assert "/" not in to_yahoo_symbol(t)


def test_empty_symbol_safe():
    assert to_yahoo_symbol("") == ""
    assert to_finnhub_symbol("") == ""
