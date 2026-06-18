"""Real-participant ladder seam (EXPERIMENTAL — not wired to any live path yet).

``negotiation_ladder.run_ladder`` drives a bounded A<->B<->arbiter dialogue but
delegates the two LLM judgments to an injected ``LadderParticipants`` protocol
(so the engine stays deterministic + unit-testable). This module wires that seam
to the SAME agents the FM-objection ZigZag uses:

  * peer_round (B, the node OWNER) -> ``AnalystResponderAgent`` (Sonnet). The
    owner is asked to defend the current value against A's proposed change.
  * arbiter (the FM) -> ``FundManagerDialogueVerdictAgent`` (Opus). It reads the
    A<->B turns and classifies:
      - ESCALATE_TO_USER -> GENUINE_DECISION (a risk-appetite / values call only
                            the client can make)
      - everything else  -> EVIDENCE_RESOLVABLE (the FM rules in-fleet)

KNOWN SEMANTIC GAPS surfaced by the live SWR run (2026-06-18) — why peer_round
NEVER returns B_CONCEDES and this stays experimental:

  1. ``AnalystResponderAgent``'s CONCEDE/REBUT stance is framed relative to an FM
     *objection* (CONCEDE = "the objector is right"). In the ladder's "owner
     responds to a proposed change" frame that label INVERTS: in the live run the
     analyst returned CONCEDE while its reasoning *rejected* the proposed SWR
     raise as "anchor-shopping" and *defended* the current value. Trusting the
     label would auto-apply a change the owner actually rejected. So peer_round
     treats the reused agent's reply as DEFENSE ONLY (always UNRESOLVED) and lets
     the FM arbiter rule — a purpose-built owner agent (ACCEPT/REJECT/UNRESOLVED
     relative to the *change*) is the proper fix.
  2. ``run_ladder`` collapses both directions of an EVIDENCE_RESOLVABLE ruling to
     ``ARBITER_RULED``, and ``incremental_plan._apply_change`` APPLIES the change
     on ARBITER_RULED regardless of whether the FM ruled FOR or AGAINST it. Until
     the terminal state encodes ruling direction, an arbiter "keep current value"
     ruling must not drive an apply. (Tracked as the next follow-on.)

Makes a REAL claude.exe call per turn, so it is NEVER constructed in a test path
— tests inject a deterministic double + assert the mapping with fakes.
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
        try:
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
        except Exception as exc:  # noqa: BLE001 — an unresponsive owner is unresolved
            log.warning("ladder.peer_round node=%s role=%s round=%s err=%s",
                        change.target_node_key, role, round, exc)
            return PeerVerdict.UNRESOLVED, f"owner response unavailable ({exc})"
        out = getattr(report, "output", report)
        stance = (getattr(out, "stance", "REBUT") or "REBUT").strip().upper()
        reasoning = getattr(out, "reasoning_md", "") or ""
        log.info(
            "ladder.peer_round node=%s role=%s round=%s stance=%s",
            change.target_node_key, role, round, stance,
        )
        # Gap (1): the reused agent's CONCEDE label inverts in this frame, so it
        # is NOT trusted to mean "accept A's change". The owner's reply is
        # captured as DEFENSE and the FM arbiter rules. Always UNRESOLVED.
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
            return ArbiterClass.GENUINE_DECISION, reasoning
        # FM_ACCEPTS_ANALYST / FM_MAINTAINS_OBJECTION / FM_REVISES_OBJECTION —
        # the FM rules in-fleet; the change either stands or is rejected by the
        # ruling, but it is NOT a client decision.
        return ArbiterClass.EVIDENCE_RESOLVABLE, reasoning


__all__ = ["RealLadderParticipants", "_owner_role_for"]
