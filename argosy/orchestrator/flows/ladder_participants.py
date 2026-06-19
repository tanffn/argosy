"""Real-participant ladder seam — the production LLM wiring for the negotiation
ladder (M2 of the generator swap).

``negotiation_ladder.run_ladder`` drives a bounded A<->B<->arbiter dialogue but
delegates the two LLM judgments to an injected ``LadderParticipants`` protocol
(so the engine stays deterministic + unit-testable). This module wires that seam:

  * peer_round (B, the node OWNER) -> ``PlanNodeOwnerAgent`` (Opus). A
    PURPOSE-BUILT agent that decides RELATIVE TO THE CHANGE — ACCEPT_CHANGE /
    REJECT_CHANGE / UNRESOLVED — grounded in the node's current value +
    derivation (gap-1 fix). Mapping is DIRECT, no inversion:
      - ACCEPT_CHANGE -> B_CONCEDES (apply A's change).
      - REJECT_CHANGE / UNRESOLVED -> UNRESOLVED (the arbiter rules direction).
  * arbiter (the FM) -> ``FundManagerDialogueVerdictAgent`` (Opus). Returns the
    ruling DIRECTION as a third tuple element (``applies``):
      - ESCALATE_TO_USER     -> GENUINE_DECISION (a client-only values call)
      - FM_MAINTAINS_OBJECTION -> EVIDENCE_RESOLVABLE, applies=True (apply)
      - FM_ACCEPTS_ANALYST / FM_REVISES_OBJECTION -> EVIDENCE_RESOLVABLE,
        applies=False (keep current) -> ARBITER_REJECTED, NOT applied.

History: the live SWR run (2026-06-18) exposed two gaps now BOTH FIXED — (1) the
reused FM-objection agent's CONCEDE/REBUT inverted in the ladder frame (replaced
here by the purpose-built owner agent), and (2) ARBITER_RULED ignored ruling
direction (now ARBITER_RULED vs ARBITER_REJECTED).

Makes a REAL claude.exe call per turn, so it is NEVER constructed in a test path
— tests inject a deterministic double + assert the mapping with fakes. The graph
is bound (``self.graph``) by ``run_incremental_cycle`` after build so the owner
agent is grounded in the node's live value + derivation.
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

    def __init__(
        self, user_id: str, *, owner_role: str | None = None, graph=None,
    ) -> None:
        self.user_id = user_id
        self._owner_role_override = owner_role  # retained for the arbiter framing
        # The derivation graph (set by run_incremental_cycle after build) so the
        # owner agent can be grounded in the node's current value + derivation.
        self.graph = graph

    def _node_context(self, node_key: str) -> tuple[str, str]:
        """(current_value, derivation_md) for the node, from the graph. Falls back
        to '(unavailable)' when no graph is bound."""
        g = self.graph
        if g is None or node_key not in set(g.keys()):
            return "(unavailable)", "(derivation unavailable — no graph bound)"
        node = g.get(node_key)
        current = repr(node.value)
        inbound = {k: repr(g.get(k).value) for k in node.inputs if k in set(g.keys())}
        if inbound:
            deriv = (
                f"computed via {node.compute_version or node.kind.value} from "
                f"inbound values: {inbound}"
            )
        else:
            deriv = f"{node.kind.value} (no inbound edges)"
        return current, deriv

    # ---- B: the node owner decides on A's proposed change ------------------ #
    def peer_round(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn], round: int,
    ) -> tuple[PeerVerdict, str]:
        from argosy.agents.plan_node_owner import PlanNodeOwnerAgent

        node_key = change.target_node_key
        current_value, derivation_md = self._node_context(node_key)
        prior_md = "\n".join(
            f"[{t.speaker.value}/{t.stance.value}] {t.text}" for t in prior_turns
        )
        agent = PlanNodeOwnerAgent(user_id=self.user_id)
        try:
            report = agent.run_sync(
                node_key=node_key,
                current_value=current_value,
                derivation_md=derivation_md,
                proposed_value=_proposed_value_text(change),
                rationale=change.rationale,
                prior_turns_md=prior_md,
                decision_id="",
            )
        except Exception as exc:  # noqa: BLE001 — an unresponsive owner is unresolved
            log.warning("ladder.peer_round node=%s round=%s err=%s",
                        node_key, round, exc)
            return PeerVerdict.UNRESOLVED, f"owner response unavailable ({exc})"
        out = getattr(report, "output", report)
        stance = (getattr(out, "stance", "UNRESOLVED") or "UNRESOLVED").strip().upper()
        reasoning = getattr(out, "reasoning_md", "") or ""
        log.info("ladder.peer_round node=%s round=%s stance=%s", node_key, round, stance)
        # Purpose-built owner stance maps DIRECTLY (no inversion):
        #   ACCEPT_CHANGE -> B concedes (apply A's change).
        #   REJECT_CHANGE / UNRESOLVED -> unresolved; the arbiter rules direction
        #   (gap-2: ARBITER_RULED applies, ARBITER_REJECTED keeps current value).
        if stance == "ACCEPT_CHANGE":
            return PeerVerdict.B_CONCEDES, reasoning
        return PeerVerdict.UNRESOLVED, f"[owner stance={stance}] {reasoning}"

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
        # Ground the verdict in the derivation node itself: the ladder has no
        # prior agent_report lineage to cite (unlike the FM-objection flow), and
        # the FM agent requires citations. The node + its inputs ARE the evidence.
        node_citation = f"derivation_node:{change.target_node_key}"
        agent = FundManagerDialogueVerdictAgent(user_id=self.user_id)
        try:
            report = agent.run_sync(
                objection_topic=f"Proposed change to {change.target_node_key}",
                objection_detail=(
                    f"Set {change.target_node_key} to {_proposed_value_text(change)}. "
                    f"Rationale: {change.rationale}. Ground your verdict in the "
                    f"derivation node {node_citation} (cite it)."
                ),
                objection_severity="HIGH",
                analyst_role=role,
                analyst_stance="REBUT",
                analyst_reasoning_md=analyst_reasoning,
                analyst_suggested_fix="",
                analyst_cited_sources=[node_citation],
                user_guidance="",
                decision_id="",
            )
        except Exception as exc:  # noqa: BLE001
            # The fleet could not produce a ruling — fail SAFE to a client
            # decision rather than silently applying a contested change. Preserves
            # the ladder guarantee: end CLOSED or with a real client question.
            log.warning("ladder.arbiter node=%s role=%s err=%s -> escalate_to_user",
                        change.target_node_key, role, exc)
            return ArbiterClass.GENUINE_DECISION, (
                f"the fleet could not adjudicate this change in-house ({exc}); "
                "surfacing as a client decision"
            )
        out = getattr(report, "output", report)
        resolution = (getattr(out, "resolution", "FM_MAINTAINS_OBJECTION") or "").strip().upper()
        reasoning = getattr(out, "reasoning_md", "") or resolution
        log.info(
            "ladder.arbiter node=%s role=%s resolution=%s",
            change.target_node_key, role, resolution,
        )
        if resolution == "ESCALATE_TO_USER":
            return ArbiterClass.GENUINE_DECISION, reasoning, False
        # The FM rules in-fleet. Direction (objection_detail = the PROPOSED change,
        # owner stance = REBUT defending the current value):
        #   FM_MAINTAINS_OBJECTION -> FM stands by the proposed change  -> APPLY.
        #   FM_ACCEPTS_ANALYST     -> the owner's rebuttal wins          -> REJECT.
        #   FM_REVISES_OBJECTION   -> revised + still open               -> REJECT (keep current).
        applies = resolution == "FM_MAINTAINS_OBJECTION"
        return ArbiterClass.EVIDENCE_RESOLVABLE, reasoning, applies


__all__ = ["RealLadderParticipants", "_owner_role_for"]
