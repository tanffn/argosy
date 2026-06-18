"""The bounded negotiation ladder (Layer 2).

Generalizes argosy/orchestrator/flows/fm_objection_dialogue.py
(converge_fm_objections) from the fixed FM<->analyst pair to ANY author pair:

  1. A files "change X because Y" against a node B owns.
  2. B may REBUT the rationale Y itself (not just the value). The burden is on
     A to defend Y; "A said so" never wins.
  3. Bounded peer rounds A<->B, n = MAX_PEER_ROUNDS (3).
  4. Unresolved -> escalate to the ARBITER (FM), which CLASSIFIES the conflict:
     resolvable-by-evidence -> it rules + applies (stays in the fleet);
     genuine judgment call    -> escalate up.
  5. Escalate to the USER -- last rung, ONLY for a certified real decision,
     surfaced as a single boxed choice.

Every step is recorded as a typed LadderTurn; the ladder ends in exactly one
typed TerminalState. The peer/arbiter step functions are INJECTED (the
LadderParticipants protocol) so the engine is deterministic + unit-testable;
production wires them to the same agents fm_objection_dialogue uses.

Pure orchestration — no DB writes here; the caller persists the returned turns
+ terminal state via change_request_store (Task 9/10). No direct LLM calls in
this module (that is the converge_fm_objections gotcha: a real claude.exe call
in the synthesis-flow path hangs the tests). Participants are the LLM seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from argosy.quality.change_adjudication import ChangeRequest


MAX_PEER_ROUNDS = 3  # spec: bounded peer rounds n = 3


class Speaker(str, Enum):
    A = "A"
    B = "B"
    ARBITER = "arbiter"
    USER = "user"


class Stance(str, Enum):
    PROPOSE = "propose"
    REBUT = "rebut"
    CONCEDE = "concede"
    RULE = "rule"
    CLASSIFY = "classify"
    ASK = "ask"
    ANSWER = "answer"


class TerminalState(str, Enum):
    A_CONCEDED = "A_conceded"
    B_CONCEDED = "B_conceded"
    ARBITER_RULED = "arbiter_ruled"
    ESCALATED_TO_USER = "escalated_to_user"
    SUPERSEDED = "superseded"


@dataclass
class LadderTurn:
    round: int
    speaker: Speaker
    stance: Stance
    text: str
    cited_nodes: list[str] = field(default_factory=list)


class PeerVerdict(str, Enum):
    """Outcome of one peer (B) reply to A's standing proposal/defense."""
    B_CONCEDES = "b_concedes"      # B accepts A's change
    A_CONCEDES = "a_concedes"      # B's rebuttal lands; A withdraws
    UNRESOLVED = "unresolved"      # neither side yields this round


class ArbiterClass(str, Enum):
    EVIDENCE_RESOLVABLE = "evidence_resolvable"  # arbiter rules + applies
    GENUINE_DECISION = "genuine_decision"        # escalate to the user


class LadderParticipants(Protocol):
    """The LLM seam. Production wires these to the analyst-responder /
    FM-verdict agents; tests inject a deterministic double."""

    def peer_round(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn], round: int,
    ) -> tuple[PeerVerdict, str]:
        ...

    def arbiter(
        self, *, change: ChangeRequest, prior_turns: list[LadderTurn],
    ) -> tuple[ArbiterClass, str]:
        ...


@dataclass
class LadderResult:
    terminal_state: TerminalState
    turns: list[LadderTurn]
    arbiter_class: ArbiterClass | None = None
    user_question: str | None = None  # the boxed choice, when escalated_to_user


def run_ladder(change: ChangeRequest, participants: LadderParticipants) -> LadderResult:
    """Drive the bounded ladder. Records every turn; ends in one TerminalState.

    A opens with PROPOSE ("change X because Y"). Then up to MAX_PEER_ROUNDS
    peer rounds: B may CONCEDE (-> B_conceded), B's rebuttal may land and A
    CONCEDE (-> A_conceded), or the round is UNRESOLVED and we continue.
    Unresolved after n=3 escalates to the arbiter (FM), which CLASSIFIES the
    conflict: evidence-resolvable -> it rules; genuine decision -> the user
    is asked a single boxed choice.
    """
    turns: list[LadderTurn] = [
        LadderTurn(
            round=0, speaker=Speaker.A, stance=Stance.PROPOSE,
            text=f"change {change.target_node_key} because {change.rationale}",
            cited_nodes=[change.target_node_key],
        )
    ]

    for rnd in range(1, MAX_PEER_ROUNDS + 1):
        verdict, text = participants.peer_round(
            change=change, prior_turns=turns, round=rnd,
        )
        if verdict is PeerVerdict.B_CONCEDES:
            turns.append(LadderTurn(rnd, Speaker.B, Stance.CONCEDE, text,
                                    [change.target_node_key]))
            return LadderResult(TerminalState.B_CONCEDED, turns)
        if verdict is PeerVerdict.A_CONCEDES:
            turns.append(LadderTurn(rnd, Speaker.B, Stance.REBUT, text,
                                    [change.target_node_key]))
            turns.append(LadderTurn(rnd, Speaker.A, Stance.CONCEDE,
                                    "rebuttal accepted; withdrawing",
                                    [change.target_node_key]))
            return LadderResult(TerminalState.A_CONCEDED, turns)
        # UNRESOLVED — record B's rebuttal and continue.
        turns.append(LadderTurn(rnd, Speaker.B, Stance.REBUT, text,
                                [change.target_node_key]))

    # Unresolved after n=3 — escalate to the arbiter (FM).
    arbiter_class, ruling = participants.arbiter(change=change, prior_turns=turns)
    turns.append(LadderTurn(
        round=MAX_PEER_ROUNDS + 1, speaker=Speaker.ARBITER, stance=Stance.CLASSIFY,
        text=f"classification: {arbiter_class.value}",
        cited_nodes=[change.target_node_key],
    ))
    if arbiter_class is ArbiterClass.EVIDENCE_RESOLVABLE:
        turns.append(LadderTurn(
            round=MAX_PEER_ROUNDS + 1, speaker=Speaker.ARBITER, stance=Stance.RULE,
            text=ruling, cited_nodes=[change.target_node_key],
        ))
        return LadderResult(TerminalState.ARBITER_RULED, turns,
                            arbiter_class=arbiter_class)

    # Genuine decision — escalate to the user as a single boxed choice.
    question = (
        f"Decision needed on {change.target_node_key}: {ruling} "
        f"(proposed: {change.payload.get('value')!r}). How would you like to proceed?"
    )
    turns.append(LadderTurn(
        round=MAX_PEER_ROUNDS + 2, speaker=Speaker.USER, stance=Stance.ASK,
        text=question, cited_nodes=[change.target_node_key],
    ))
    return LadderResult(TerminalState.ESCALATED_TO_USER, turns,
                        arbiter_class=arbiter_class, user_question=question)


__all__ = [
    "MAX_PEER_ROUNDS", "Speaker", "Stance", "TerminalState",
    "PeerVerdict", "ArbiterClass",
    "LadderTurn", "LadderParticipants", "LadderResult", "run_ladder",
]
