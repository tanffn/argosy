# Plan — encode arbiter ruling DIRECTION in the negotiation ladder

## Problem (gap 2, surfaced by the live SWR run 2026-06-18)
`negotiation_ladder.run_ladder` collapses both directions of an
`EVIDENCE_RESOLVABLE` arbiter ruling to `TerminalState.ARBITER_RULED`, and
`incremental_plan._apply_change` APPLIES the change on `ARBITER_RULED`
regardless of whether the FM ruled **for** or **against** it. The live SWR run
proved the bug: the FM ruled to REJECT the 3.5% raise (FM_ACCEPTS_ANALYST =
the owner's rebuttal lands), yet the apply path would have set 3.5%.

## Fix (typed, minimal blast radius)
1. **negotiation_ladder.py**
   - Add `TerminalState.ARBITER_REJECTED` (arbiter ruled AGAINST the change).
   - `run_ladder`: tolerantly unpack the arbiter return — `(ArbiterClass, str)`
     (legacy doubles: treated as APPLY for back-compat) OR
     `(ArbiterClass, str, applies: bool)`. For `EVIDENCE_RESOLVABLE`:
     `applies` → `ARBITER_RULED`; not `applies` → `ARBITER_REJECTED`.
   - `GENUINE_DECISION` path unchanged (`ESCALATED_TO_USER`).
2. **incremental_plan.py** `_apply_change`: `ARBITER_RULED` → apply (as now);
   `ARBITER_REJECTED` → NOT applied (like `A_CONCEDED`), recorded.
3. **ladder_participants.py** `arbiter`: return the 3-tuple. Direction from the
   FM resolution (objection_detail = the proposed change; owner stance = REBUT):
   - `FM_MAINTAINS_OBJECTION` → applies=True (FM stands by the proposed change).
   - `FM_ACCEPTS_ANALYST` / `FM_REVISES_OBJECTION` → applies=False (owner's
     defense wins / revised+open → keep current value).
   - `ESCALATE_TO_USER` → GENUINE_DECISION.
   Update the docstring: gap (2) FIXED.
4. **Tests**: negotiation_ladder ARBITER_REJECTED case; ladder_participants
   arbiter mapping asserts (class, applies); incremental_plan reject-not-applied.

## Verify
- Targeted: test_negotiation_ladder, test_ladder_participants, test_incremental_plan,
  test_adjudication_e2e, test_change_request_store.
- Re-run the live SWR ladder → expect TERMINAL STATE = arbiter_rejected (FM held 3.0%).
