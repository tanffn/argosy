"""Production ladder participants — the real LLM seam for the negotiation ladder.

``negotiation_ladder.run_ladder`` drives a bounded A<->B<->arbiter dialogue but
delegates the two LLM judgments to an injected ``LadderParticipants`` protocol
(so the engine stays deterministic + unit-testable). This module wires that seam
to the SAME agents the FM-objection ZigZag uses:

  * peer_round (B, the node OWNER defending the current value) ->
    ``AnalystResponderAgent`` (Sonnet). The change-request is presented to the
    owning analyst as the "objection" to their current derivation. The analyst's
    stance maps to the peer verdict:
      - CONCEDE        -> B_CONCEDES  (the owner agrees A's change is warranted)
      - REBUT / CLARIFY -> UNRESOLVED (the owner defends the current value;
                            whether the rebuttal *lands* is the arbiter's call,
                            never the peer's — mirrors the FM-dialogue split)
  * arbiter (the FM) -> ``FundManagerDialogueVerdictAgent`` (Opus). It reads the
    A<->B turns and classifies:
      - ESCALATE_TO_USER -> GENUINE_DECISION (a risk-appetite / values call only
                            the client can make — surfaced as the one boxed
                            question)
      - everything else  -> EVIDENCE_RESOLVABLE (the FM rules in-fleet; the
                            ruling text is its reasoning)

Hard/derived nodes are never "agreed away" here — the ladder only runs for
NEEDS_LADDER (recipe/policy) change-requests; adjudicate() routes hard-input and
derived-target changes elsewhere (apply / fail-closed). This class makes a REAL
claude.exe call per turn, so it is NEVER constructed in a test path — tests
inject a deterministic double. Production passes an instance to
``run_incremental_cycle(participants=...)``.
"""
from __future__ import annotations

import logging

from argosy.orchestrator.flows.negotiation_ladder import (
    ArbiterClass, LadderTurn, PeerVerdict,
)
from argosy.quality.change_adjudication import ChangeRequest

log = logging.getLogger(__name__)

# Default owning analyst when a node's owner can't be inferred from its key.
# The withdrawal_sequencer owns the SWR / required-real-yield / FI-target
# derivations — the most common ladder subject (a risk-appetite policy change).
_DEFAULT_OWNER_ROLE = "withdrawal_sequencer"

# node-key prefix -> the analyst role that OWNS that derivation. Used to pick the
# perspective the AnalystResponderAgent adopts when defending the current value.
_OWNER_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("retirement.", "withdrawal_sequencer"),
    ("spend.", "household_budget"),
    ("concentration.", "concentration"),
    ("fx.", "fx"),
    ("savings.", "equity_comp"),
)


def _owner_role_for(node_key: str) -> str:
    for prefix, role in _OWNER_BY_PREFIX:
        if node_key.startswith(prefix):
            return role
    return _DEFAULT_OWNER_ROLE


def _proposed_value_text(change: ChangeRequest) -> str:
    val = change.payload.get("value")
    return "(no explicit value)" if val is None else repr(val)


class RealLadderParticipants:
    """LadderParticipants backed by the real analyst-responder + FM-verdict
    agents. Each method makes one live LLM call."""

    def __init__(self, user_id: str, *, owner_role: str | None = None) -> None:
        self.user_id = user_id
        self._owner_role_override = owner_role

    # ---- B: the node owner responds to A's proposed change ----------------- #
    def peer_round(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn], round: int,
    ) -> tuple[PeerVerdict, str]:
        from argosy.agents.analyst_responder import AnalystResponderAgent

        role = self._owner_role_override or _owner_role_for(change.target_node_key)
        topic = f"Proposed change to {change.target_node_key}"
        detail = (
            f"An agent proposes to set {change.target_node_key} to "
            f"{_proposed_value_text(change)}. Rationale: {change.rationale}. "
            "You OWN this derivation. Respond CONCEDE if the change is warranted, "
            "REBUT if the current value is correct and you can defend it with "
            "evidence, or CLARIFY if the proposal misreads your derivation."
        )
        prior_md = "\n".join(f"[{t.speaker.value}/{t.stance.value}] {t.text}" for t in prior_turns)
        agent = AnalystResponderAgent(user_id=self.user_id)
        report = agent.run_sync(
            analyst_role=role,
            objection_topic=topic,
            objection_detail=detail,
            objection_severity="HIGH",
            prior_agent_report_excerpt=prior_md,
            prior_decision_audit_token="",
            prior_agent_report_id=None,
            user_guidance="",
            decision_id="",
        )
        out = getattr(report, "output", report)
        stance = (getattr(out, "stance", "REBUT") or "REBUT").strip().upper()
        reasoning = getattr(out, "reasoning_md", "") or ""
        log.info(
            "ladder.peer_round node=%s role=%s round=%s stance=%s",
            change.target_node_key, role, round, stance,
        )
        if stance == "CONCEDE":
            return PeerVerdict.B_CONCEDES, reasoning
        # REBUT / CLARIFY — the owner defends; let the arbiter weigh it.
        return PeerVerdict.UNRESOLVED, reasoning

    # ---- arbiter: the FM classifies the impasse ---------------------------- #
    def arbiter(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn],
    ) -> tuple[ArbiterClass, str]:
        from argosy.agents.fund_manager_dialogue_verdict import (
            FundManagerDialogueVerdictAgent,
        )

        role = self._owner_role_override or _owner_role_for(change.target_node_key)
        # The last B turn carries the owner's standing defense.
        last_b = next(
            (t for t in reversed(prior_turns) if t.speaker.value == "B"), None
        )
        analyst_reasoning = last_b.text if last_b is not None else ""
        agent = FundManagerDialogueVerdictAgent(user_id=self.user_id)
        report = agent.run_sync(
            objection_topic=f"Proposed change to {change.target_node_key}",
            objection_detail=(
                f"Set {change.target_node_key} to {_proposed_value_text(change)}. "
                f"Rationale: {change.rationale}"
            ),
            objection_severity="HIGH",
            analyst_role=role,
            analyst_stance="REBUT",
            analyst_reasoning_md=analyst_reasoning,
            analyst_suggested_fix="",
            analyst_cited_sources=[],
            user_guidance="",
            decision_id="",
        )
        out = getattr(report, "output", report)
        resolution = (getattr(out, "resolution", "FM_MAINTAINS_OBJECTION") or "").strip().upper()
        reasoning = getattr(out, "reasoning_md", "") or resolution
        log.info(
            "ladder.arbiter node=%s role=%s resolution=%s",
            change.target_node_key, role, resolution,
        )
        if resolution == "ESCALATE_TO_USER":
            return ArbiterClass.GENUINE_DECISION, reasoning
        # FM_ACCEPTS_ANALYST / FM_MAINTAINS_OBJECTION / FM_REVISES_OBJECTION —
        # the FM rules in-fleet; the change either stands or is rejected by the
        # ruling, but it is NOT a client decision.
        return ArbiterClass.EVIDENCE_RESOLVABLE, reasoning


__all__ = ["RealLadderParticipants", "_owner_role_for"]
