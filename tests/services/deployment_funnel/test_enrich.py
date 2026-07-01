from argosy.services.deployment_funnel.enrich import (
    build_history_features,
    news_sentiment_for,
)


class _StubProvider:
    def __init__(self, q, hi, z):
        self._q, self._hi, self._z = q, hi, z

    def quote(self, s):
        return self._q

    def history_high(self, s):
        return self._hi

    def zscore(self, s):
        return self._z


def test_history_features_computes_pct_below_ath():
    hf = build_history_features("SGLD", _StubProvider(368.0, 372.0, 1.9))
    assert hf.pct_below_ath == round((372.0 - 368.0) / 372.0 * 100, 2)
    assert hf.stale is False


def test_missing_quote_marks_stale():
    hf = build_history_features("SGLD", _StubProvider(None, 372.0, None))
    assert hf.stale is True


def test_news_sentiment_absent_returns_none():
    assert news_sentiment_for("SGLD", {}) is None


def test_news_sentiment_present():
    assert news_sentiment_for("NVDA", {"NVDA": "positive"}) == "positive"
