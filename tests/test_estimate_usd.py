"""_estimate_usd accounts for cache and thinking pricing (Wave A)."""
from __future__ import annotations

import pytest

from argosy.agents.base import BaseAgent


class _DummyAgent(BaseAgent):
    agent_role = "news"
    output_model = type("Out", (), {})

    def build_prompt(self, **_):  # noqa: D401
        return ("", "")


@pytest.fixture
def agent():
    return _DummyAgent(user_id="ariel", model="claude-sonnet-4-6")


def test_estimate_usd_no_cache_no_thinking(agent):
    # 1000 input + 500 output on Sonnet ($3/M in, $15/M out):
    # cost = 1000*3/1M + 500*15/1M = 0.003 + 0.0075 = 0.0105
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.0105, rel=1e-3)


def test_estimate_usd_with_cache_read(agent):
    # 1000 total input, 800 from cache:
    # uncached_input = 200; cache_read = 800
    # cost = 200*3/1M + 800*3*0.10/1M + 500*15/1M
    #      = 0.0006   + 0.00024            + 0.0075
    #      = 0.00834
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=800, cache_creation_tokens=0, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.00834, rel=1e-3)


def test_estimate_usd_with_cache_creation(agent):
    # 1000 total input, 800 newly cached:
    # uncached_input = 200; cache_write = 800
    # cost = 200*3/1M + 800*3*1.25/1M + 500*15/1M
    #      = 0.0006   + 0.003          + 0.0075
    #      = 0.0111
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=0, cache_creation_tokens=800, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.0111, rel=1e-3)


def test_estimate_usd_with_thinking(agent):
    # Thinking tokens priced as output:
    # cost = 1000*3/1M + (500 + 2000)*15/1M
    #      = 0.003     + 0.0375
    #      = 0.0405
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=0, cache_creation_tokens=0, thinking_tokens=2000,
    )
    assert cost == pytest.approx(0.0405, rel=1e-3)


def test_estimate_usd_combined(agent):
    # 1000 input (500 cache_read, 200 cache_write, 300 uncached), 500 out, 1000 thinking
    # uncached: 300*3/1M = 0.0009
    # read:     500*3*0.10/1M = 0.00015
    # write:    200*3*1.25/1M = 0.00075
    # output+thinking: (500+1000)*15/1M = 0.0225
    # total: 0.02430
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=500, cache_creation_tokens=200, thinking_tokens=1000,
    )
    assert cost == pytest.approx(0.02430, rel=1e-3)


def test_estimate_usd_caps_cached_overflow(agent):
    """Edge case: if upstream telemetry reports cached sums > tokens_in,
    cap proportionally so we never bill more than the true total input.

    tokens_in=1000, cache_read=900, cache_write=300 -> cached_total=1200 > 1000.
    Scale = 1000/1200 = 0.8333; scaled read=750, scaled write=250.
    cost = 0 + 750*3*0.10/1M + 250*3*1.25/1M + 500*15/1M
         = 0.000225      + 0.0009375           + 0.0075
         = 0.0086625
    Without the cap, the buggy path would charge for the full 900+300 plus
    zero uncached -- a ~17% overcharge in this scenario.
    """
    cost = agent._estimate_usd(
        tokens_in=1000, tokens_out=500,
        cache_input_tokens=900, cache_creation_tokens=300, thinking_tokens=0,
    )
    assert cost == pytest.approx(0.0086625, rel=1e-3)


def test_estimate_usd_opus_pricing():
    """Opus 4.7: $5/M in, $25/M out (corrected in Wave A audit; pre-Wave-A
    code carried Opus 4.1 pricing $15/$75)."""
    a = _DummyAgent(user_id="ariel", model="claude-opus-4-7")
    # 1000 in + 500 out: 1000*5/1M + 500*25/1M = 0.005 + 0.0125 = 0.0175
    cost = a._estimate_usd(tokens_in=1000, tokens_out=500)
    assert cost == pytest.approx(0.0175, rel=1e-3)


def test_estimate_usd_haiku_pricing():
    """Haiku 4.5: $1/M in, $5/M out (corrected in Wave A audit; pre-Wave-A
    code carried Haiku 3.5 pricing $0.80/$4)."""
    a = _DummyAgent(user_id="ariel", model="claude-haiku-4-5")
    # 1000 in + 500 out: 1000*1/1M + 500*5/1M = 0.001 + 0.0025 = 0.0035
    cost = a._estimate_usd(tokens_in=1000, tokens_out=500)
    assert cost == pytest.approx(0.0035, rel=1e-3)
