# argosy/agents/coherence_arbitrator.py
"""The binding arbitrator for goal/framing tensions. Distinct from fund_manager
(own prompt/schema/telemetry) but embodies its prime-directive authority. Its job
is NOT 'best plan' — it is 'which claim binds under the authority order, and what
invariant must every surface satisfy?'. Two axes: FACTUAL (canonical facts win on
truth) vs POLICY/framing (prime directive > user directives > preference)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class ArbitratorRuling(BaseModel):
    ruling_statement: str = Field(description="The binding answer to the dispute.")
    axis: Literal["factual", "policy"] = Field(description="Authority axis used.")
    basis: Literal["prime_directive", "user_directive", "canonical_fact"] = Field(
        description="The binding basis under the axis."
    )
    rationale: str = Field(description="Why this binds, under the authority order.")
    per_surface_instructions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="[{surface_id, instruction}] — how each surface must state the ruling.",
    )
    coherence_invariant: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Typed invariant(s) the verifier will enforce (kind + fields).",
    )


class CoherenceArbitratorAgent(BaseAgent[ArbitratorRuling]):
    agent_role = "coherence_arbitrator"
    output_model = ArbitratorRuling
    require_citations = False
    schema_retry_attempts = 2

    def build_prompt(
        self, *, dispute_question: str, positions: list[dict],
        canonical_facts: str, prime_directive: str,
        surfaces: list[str] | None = None,
    ) -> tuple[str, str]:
        surfaces = surfaces or []
        surfaces_line = ", ".join(surfaces) if surfaces else "(none registered)"
        system = (
            "You are the Argosy coherence ARBITRATOR. Issue a binding ruling for one "
            "disputed question. First classify the dispute's AUTHORITY AXIS: FACTUAL "
            "(canonical facts win on truth — a directive cannot make a false number "
            "true) vs POLICY/framing (authority order: prime directive > user "
            "directives > panelist preference). Decide WHICH CLAIM BINDS and the exact "
            "INVARIANT every surface must satisfy. You do not design the best plan; you "
            "resolve the contradiction.\n"
            "For a POLICY/framing ruling, emit `coherence_invariant` entries of kind "
            "`required_framing_role` (fields: subject_type, surface, role_field, value) "
            "encoding the binding framing as typed key=value roles, and optionally "
            "`forbidden_claim` (fields: subject_type, surface, pattern) for prose that "
            "must NOT appear. Use ONLY these exact surface ids in every invariant's "
            f"`surface` field: {surfaces_line}. Do NOT invent surface names. Keep `value` "
            "tokens short and machine-comparable (e.g. an age number, or a short snake_case "
            "label)."
        )
        pos = "\n".join(
            f"  - {p.get('role','?')}: {p.get('position','')} (basis={p.get('basis','')})"
            for p in positions
        ) or "  (no panel positions; rule from the prime directive + canonical facts)"
        user = (
            f"PRIME DIRECTIVE:\n{prime_directive}\n\n"
            f"DISPUTED QUESTION:\n{dispute_question}\n\n"
            f"VALID SURFACE IDS (use only these in invariants):\n{surfaces_line}\n\n"
            f"CANONICAL FACTS:\n{canonical_facts}\n\n"
            f"PANELIST POSITIONS:\n{pos}\n"
        )
        return system, user
