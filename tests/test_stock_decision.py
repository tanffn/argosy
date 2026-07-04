"""Decision core: StockDecisionAgent prompt shape + verdict semantics + orchestration
(injected agent, no live LLM)."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.agents.stock_decision import (
    StockDecisionAgent,
    StockDecisionOutput,
    decide_stock,
    is_actionable,
)


def test_hold_is_not_actionable_but_trades_are():
    assert not is_actionable("HOLD")
    assert not is_actionable("hold")
    for v in ("BUY", "SELL", "TRIM", "buy"):
        assert is_actionable(v)


def test_prompt_frames_hold_as_first_class_and_forbids_guessing():
    agent = StockDecisionAgent.__new__(StockDecisionAgent)  # no LLM/DB init
    system, user = StockDecisionAgent.build_prompt(
        agent, ticker="RKT", context="held $42k, plan role: exit sleeve",
        bundle={"news": "-27% YTD, housing macro deteriorating"},
    )
    assert "HOLD IS A FIRST-CLASS ANSWER" in system
    assert "do NOT guess" in system
    assert "RKT" in user


def test_prompt_marks_absent_bundle_fields():
    agent = StockDecisionAgent.__new__(StockDecisionAgent)
    _system, user = StockDecisionAgent.build_prompt(
        agent, ticker="SOFI", context="held $36k",
        bundle={"news": "legal headwinds"},  # only news present
    )
    assert "Recent news: legal headwinds" in user
    assert "Fundamentals: (not available)" in user  # absent field named explicitly


def test_decide_stock_uses_injected_agent():
    captured = {}

    class _FakeAgent:
        def run_sync(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output=StockDecisionOutput(
                ticker="RKT", verdict="TRIM", confidence="MED",
                reason="thesis weakening", evidence=["-27% YTD"], data_gaps=["fundamentals"],
            ))

    out = decide_stock(
        "RKT", context="held $42k", bundle={"news": "x"},
        agent_factory=lambda: _FakeAgent(),
    )
    assert out.verdict == "TRIM" and is_actionable(out.verdict)
    assert captured["ticker"] == "RKT" and "news" in captured["bundle"]
