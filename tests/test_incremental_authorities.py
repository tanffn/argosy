"""Tests for incremental_authorities — composing the scoped authority set for an
edit-in-place plan. Pure: the LLM seam is a fake; no DB/LLM."""
from __future__ import annotations

from argosy.quality.incremental_authorities import compute_incremental_authorities
from argosy.quality.publish_gate import OpenFlag, can_publish_plan


class _FakeAgents:
    def __init__(self, codex, reader, fm):
        self._codex, self._reader, self._fm = codex, reader, fm
        self.seen_changed = None

    def codex_rederive(self, *, plan_fields, changed_node_keys):
        self.seen_changed = changed_node_keys
        return self._codex

    def reader_review(self, *, plan_fields):
        return self._reader

    def fund_manager_review(self, *, plan_fields):
        return self._fm


def test_all_clear_composes_promotable_authorities():
    agents = _FakeAgents(codex="APPROVE", reader="APPROVE", fm="approved")
    auth = compute_incremental_authorities(
        agents=agents, plan_fields={"sections_json": "[]"},
        changed_node_keys=["retirement.fi_margin_signed_nis"],
        deterministic_gate_clear=True, rederivation_clear=True,
    )
    assert auth["rederivation"] == "APPROVE"
    assert auth["deterministic_gate"] is True
    assert agents.seen_changed == ["retirement.fi_margin_signed_nis"]
    decision = can_publish_plan(authorities=auth, open_flags=[])
    assert decision.can_promote is True


def test_fm_rejected_blocks():
    agents = _FakeAgents(codex="APPROVE", reader="APPROVE", fm="rejected")
    auth = compute_incremental_authorities(
        agents=agents, plan_fields={}, changed_node_keys=[],
        deterministic_gate_clear=True, rederivation_clear=True,
    )
    decision = can_publish_plan(authorities=auth, open_flags=[])
    assert decision.can_promote is False
    assert any("fund_manager" in b for b in decision.blocking_authorities)


def test_none_llm_verdict_fails_closed():
    # A missing reader verdict must block (never silently cleared).
    agents = _FakeAgents(codex="APPROVE", reader=None, fm="approved")
    auth = compute_incremental_authorities(
        agents=agents, plan_fields={}, changed_node_keys=[],
        deterministic_gate_clear=True, rederivation_clear=True,
    )
    assert auth["whole_artifact_reader"] is None
    decision = can_publish_plan(authorities=auth, open_flags=[])
    assert decision.can_promote is False


def test_rederivation_and_gate_deterministic_block():
    agents = _FakeAgents(codex="APPROVE", reader="APPROVE", fm="approved")
    auth = compute_incremental_authorities(
        agents=agents, plan_fields={}, changed_node_keys=[],
        deterministic_gate_clear=False, rederivation_clear=False,
    )
    assert auth["rederivation"] == "BLOCK"
    assert auth["deterministic_gate"] is False
    decision = can_publish_plan(authorities=auth, open_flags=[])
    assert decision.can_promote is False
