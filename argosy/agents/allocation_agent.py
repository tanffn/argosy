"""AllocationAgent (Slice 1b) — a THIN Opus ranker/sequencer/explainer over the
deterministic 1a candidates.

The engine (1a) owns every number — amounts, instruments, tax. This agent only:
  * orders the candidates into now / this_quarter / later and a sequence,
  * attaches a deployment-PACE recommendation (lump vs tranched) to BUY tasks,
    informed by the market-context snapshot (the one bounded place sentiment
    enters the core — pace ≠ destination; it never changes amounts/instruments),
  * writes a one-line rationale per task.

It references candidates by INDEX and invents no numbers; the result is then run
through :func:`reconcile_or_raise` (identity + uniqueness + 1:1 coverage), so a
task that wraps an invented/duplicated/dropped candidate fails loud.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from argosy.agents.base import BaseAgent
from argosy.services.contracts import AllocationCandidate, ExecutableTask
from argosy.services.executable_tasks import reconcile_or_raise


class AllocationTaskSpec(BaseModel):
    candidate_index: int
    # Literal so an out-of-vocabulary value from the model fails schema
    # validation (codex 1b #3) rather than producing a malformed task.
    horizon: Literal["now", "this_quarter", "later"]
    pace: Literal["lump", "tranched"]
    pace_rationale: str = ""
    rationale: str = ""


class AllocationOrdering(BaseModel):
    tasks: list[AllocationTaskSpec]


def _render_candidate(i: int, c: AllocationCandidate) -> str:
    legs = "; ".join(
        f"{l.side} ${l.notional_usd:,.0f} {l.symbol} ({l.funding_source})"
        for l in c.legs
    )
    return (f"[{i}] kind={c.kind} horizon_hint={c.horizon} legs=[{legs}]"
            + (f" tax_nis={c.est_tax_nis}" if c.est_tax_nis else "")
            + (" surtax_split_suggested" if c.surtax_split_suggested else ""))


class AllocationAgent(BaseAgent[AllocationOrdering]):
    """Order / group / pace / explain the deterministic allocation candidates."""

    agent_role = "allocation_agent"   # not in the role tables -> Opus fallback
    output_model = AllocationOrdering
    require_citations = False          # cites are carried from 1a, not re-sourced

    def build_prompt(self, *, candidates, verdicts, market_context):
        rendered = "\n".join(_render_candidate(i, c) for i, c in enumerate(candidates))
        system = (
            "You are Argosy's allocation sequencer. You are given a set of "
            "DETERMINISTIC, already-priced allocation candidates (amounts, "
            "instruments and tax are FIXED — you must not change, add, or remove "
            "any number or instrument). Your only job is to:\n"
            "  1. Order the candidates into a sensible execution sequence and "
            "assign each a horizon: 'now', 'this_quarter', or 'later'. Prefer "
            "SELL/SWAP (funding) before the BUYs they fund.\n"
            "  2. Attach a deployment pace to each task: 'lump' (deploy at once) "
            "or 'tranched' (average in), guided by the market-context snapshot "
            "(elevated volatility / risk-off favours 'tranched'; otherwise "
            "lump-sum usually wins). Pace is the ONLY place market sentiment "
            "enters — it never changes amounts or instruments.\n"
            "  3. Write a one-line rationale per task.\n\n"
            "HARD RULES:\n"
            "  - Reference each candidate by its integer index exactly as given.\n"
            "  - Cover EVERY candidate EXACTLY ONCE — no dropping, no duplicating, "
            "no inventing new candidates or indices.\n"
            "  - Invent no numbers; the amounts are the engine's, not yours."
        )
        user = (
            "CANDIDATES (index-prefixed; amounts are fixed):\n"
            f"{rendered or '(none)'}\n\n"
            f"PER-POSITION VERDICTS: {json.dumps(verdicts, default=str)}\n"
            f"MARKET CONTEXT (volatility / sentiment / FX): "
            f"{json.dumps(market_context, default=str)}\n\n"
            "Return a JSON object {\"tasks\": [{\"candidate_index\": int, "
            "\"horizon\": \"now|this_quarter|later\", \"pace\": \"lump|tranched\", "
            "\"pace_rationale\": str, \"rationale\": str}, ...]} covering every "
            "candidate index exactly once."
        )
        return system, user


def order_and_explain(candidates, verdicts, market_context, *,
                      user_id: str = "ariel") -> list[ExecutableTask]:
    """Run the agent over 1a's candidates and return reconciled ExecutableTask[].

    Raises ``ValueError`` if the agent references an out-of-range index or the
    resulting task set does not cover the candidate set 1:1 (fail loud — the
    agent is not allowed to invent, drop, or duplicate)."""
    if not candidates:
        return []
    agent = AllocationAgent(user_id=user_id)
    report = agent.run_sync(candidates=candidates, verdicts=verdicts,
                            market_context=market_context)
    ordering: AllocationOrdering = report.output
    tasks: list[ExecutableTask] = []
    used: list[int] = []
    for seq, spec in enumerate(ordering.tasks, start=1):
        idx = spec.candidate_index
        if idx < 0 or idx >= len(candidates):
            raise ValueError(
                f"allocation agent referenced out-of-range candidate_index {idx} "
                f"(have {len(candidates)} candidates)")
        used.append(idx)
        cand = candidates[idx]
        tasks.append(ExecutableTask(
            seq=seq, candidate=cand, horizon=spec.horizon, pace=spec.pace,
            pace_rationale=spec.pace_rationale, rationale=spec.rationale,
            cites=cand.cites))
    # Coverage on INDICES, not fingerprints: two distinct candidates may share a
    # fingerprint (identical legs), so a duplicated index that drops another
    # candidate would slip past the fingerprint-count gate (codex 1b #1). Require
    # each index used exactly once.
    if sorted(used) != list(range(len(candidates))):
        raise ValueError(
            f"allocation agent must wrap each candidate index exactly once; "
            f"got indices {sorted(used)} for {len(candidates)} candidates")
    reconcile_or_raise(tasks, candidates)
    return tasks


__all__ = ["AllocationAgent", "AllocationOrdering", "AllocationTaskSpec",
           "order_and_explain"]
