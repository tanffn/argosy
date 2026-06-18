"""Capstone — ONE runnable incremental plan cycle (Layer 5).

`run_incremental_cycle` takes the current plan + a set of change-requests (from
the user OR agents), updates only the blast radius, re-renders the canonical
surfaces, re-verifies coherence, and ends CLOSED (promotable) or with a FINITE
set of real client questions. It COMPOSES the already-built, already-tested
modules — it adds NO new derivation math:

  * argosy.quality.graph_collections   — holdings COLLECTION + set-edge wiring
  * argosy.quality.live_surfaces        — canonical single-node surfaces
  * argosy.quality.surface_rendering    — recheck_coherence over the artifact
  * argosy.quality.change_adjudication  — ownership-/authority-aware routing
  * argosy.orchestrator.flows.negotiation_ladder — bounded A<->B<->arbiter ladder
  * argosy.quality.publish_gate         — fail-closed promotability
  * argosy.state.graph_store            — persistence + propagation_events

The whole flow is gated behind ``ARGOSY_INCREMENTAL_PLAN`` and does NOT touch
from-scratch synthesis. There are NO real claude.exe / LLM calls in this module:
the ladder's LLM seam is the injected ``participants`` (a deterministic fake in
tests; the real analyst/FM agents in production).

### build_base_graph node-key reconciliation
The three builders use different node-key namespaces. ``build_base_graph``
produces ONE graph and points each canonical subject at the node that actually
exists in it, via ``register_canonical_surfaces(graph, subject_node_map=...)``:

  subject_type            canonical default key            actual node used
  ----------------------  -------------------------------  ------------------------------------
  fi_capital_sufficiency  retirement.fi_margin_signed_nis  retirement.fi_margin_signed_nis  (resolver, same key)
  retirement_age_headline retirement.earliest_safe_age     retirement.earliest_safe_age     (resolver, same key)
  us_situs_estate         estate.us_situs_exposure_nis     concentration.us_situs_estate_nis (collections)
  net_worth_liquid        net_worth.liquid_nis             portfolio.liquid_net_worth_nis   (resolver, liquid basis)
  net_worth_investable    net_worth.investable_nis         portfolio.net_worth_nis          (resolver, investable basis)

The canonical scalar DERIVED values (fi_margin, earliest_safe_age, liquid net
worth, investable net worth) are SOURCED from the resolver manifest
(resolve_plan_numbers, the authoritative single source) and seeded as the node
value — we do NOT create a second independent derivation of the same number. The
resolver exposes TWO DISTINCT net-worth bases — ``portfolio.liquid_net_worth_nis``
(EXCLUDES real estate; the FI sufficiency basis) and ``portfolio.net_worth_nis``
(INCLUDES foreign real estate; reconciliation only) — and they map to DIFFERENT
nodes so the investable surface never renders the liquid figure. The holdings
COLLECTION (build_holdings_graph) supplies the us-situs estate figure + per-symbol
breakdown + the set-edge membership wiring, so adding/removing a position
re-derives the estate exposure.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.change_adjudication import (
    ChangeKind, ChangeRequest, Disposition, OwnershipMap, adjudicate,
)
from argosy.orchestrator.flows.negotiation_ladder import (
    LadderParticipants, TerminalState, run_ladder,
)
from argosy.quality.graph_collections import (
    BREAKDOWN_KEY, FX_KEY, HOLDINGS_KEY, NET_WORTH_KEY, NVDA_PCT_KEY,
    US_SITUS_KEY, build_holdings_graph,
)
from argosy.quality.live_surfaces import (
    EARLIEST_SAFE_AGE_NODE, FI_MARGIN_NODE, canonical_surface_concepts,
    register_canonical_surfaces,
)
from argosy.quality.publish_gate import OpenFlag, can_publish_plan
from argosy.quality.surface_rendering import (
    register_surface_concepts, recheck_coherence,
)

log = logging.getLogger(__name__)

FLAG_ENV = "ARGOSY_INCREMENTAL_PLAN"

# The resolver exposes TWO DISTINCT net-worth bases (verified on live data:
# liquid 11,687,926 EXCLUDES real estate — the FI basis; investable 11,954,153
# INCLUDES foreign real estate — reconciliation only). They must map to DIFFERENT
# nodes or the investable surface would render the liquid figure and recreate the
# cross-surface contradiction this whole design exists to kill.
LIQUID_NW_KEY = "portfolio.liquid_net_worth_nis"
INVESTABLE_NW_KEY = "portfolio.net_worth_nis"

# subject_type -> the node key actually present in the base graph (overrides the
# live_surfaces CANONICAL_SUBJECT_NODE defaults where the namespaces differ).
SUBJECT_NODE_MAP: dict[str, str] = {
    "us_situs_estate": US_SITUS_KEY,
    "net_worth_liquid": LIQUID_NW_KEY,
    "net_worth_investable": INVESTABLE_NW_KEY,
}

# Canonical scalar nodes seeded from the authoritative resolver manifest.
_RESOLVER_SCALAR_KEYS = (
    FI_MARGIN_NODE, EARLIEST_SAFE_AGE_NODE, LIQUID_NW_KEY, INVESTABLE_NW_KEY,
)


class IncrementalPlanDisabled(RuntimeError):
    """Raised when run_incremental_cycle is called with the feature flag off."""


def _flag_on() -> bool:
    """Read ARGOSY_INCREMENTAL_PLAN via settings, falling back to os.environ.
    Truthy values: 1/true/yes/on (case-insensitive)."""
    val: str | None = None
    try:
        from argosy.config import get_settings

        val = getattr(get_settings(), FLAG_ENV.lower(), None)
    except Exception:  # noqa: BLE001 — config optional / absent attr
        val = None
    if val is None:
        val = os.environ.get(FLAG_ENV)
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class CycleResult:
    """The outcome of one incremental cycle."""

    closed: bool
    real_questions: list[dict] = field(default_factory=list)
    open_flags: list[str] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)
    replay_ref: str | None = None
    promotable: bool = False
    graph: DerivationGraph | None = None


@dataclass
class AppliedChange:
    """The disposition of one change-request after adjudication + (maybe) ladder."""

    cr: ChangeRequest
    disposition: str
    applied: bool
    dirtied_input: str | None = None
    real_question: dict | None = None
    note: str = ""


# --------------------------------------------------------------------------- #
# Task 1 — build_base_graph                                                   #
# --------------------------------------------------------------------------- #

def _snapshot_positions_fx(session, user_id: str) -> tuple[list[dict], float]:
    """Read the latest portfolio snapshot's positions + fx (read-only)."""
    from sqlalchemy import select

    from argosy.state.models import PortfolioSnapshotRow

    snap = session.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(PortfolioSnapshotRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snap is None:
        return [], 0.0
    try:
        positions = json.loads(snap.positions_json or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        positions = []
    # Mark to the BOI CURRENT representative rate (snapshot fx is only the
    # fallback) — the SAME "one FX per book" convention the resolver uses for
    # net worth + US-situs. Seeding the collection fx node from the snapshot's
    # stored rate instead would make the us-situs surface diverge from the
    # authoritative resolver/codex figure (the FX-basis bug this design kills).
    snap_fx = float(snap.fx_usd_nis or 0.0)
    try:
        from argosy.services.plan_numeric_resolver import _current_boi_usd_nis

        fx, _src = _current_boi_usd_nis(session, snap_fx)
    except Exception:  # noqa: BLE001 — BOI unavailable -> snapshot fallback
        fx = snap_fx
    return positions, float(fx or 0.0)


def _resolver_scalars(session, user_id: str, decision_run_id: int) -> dict[str, float]:
    """Source the canonical scalar values from the authoritative resolver
    manifest. Only resolved (non-pending) keys are returned; a pending key is
    left out so its node stays a fail-closed seed (caller decides)."""
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

    resolved = resolve_plan_numbers(
        session, user_id=user_id, decision_run_id=decision_run_id,
        include_canonical_ages=True,
    )
    out: dict[str, float] = {}
    for key in _RESOLVER_SCALAR_KEYS:
        rv = resolved.get(key)
        if rv.status == "resolved" and rv.value is not None:
            out[key] = float(rv.value)
    return out


def build_base_graph(
    session,
    user_id: str,
    *,
    decision_run_id: int,
    resolver_values: dict[str, float] | None = None,
) -> DerivationGraph:
    """Hydrate the current plan into ONE closed derivation graph.

    Composition:
      * holdings COLLECTION + set-edge derived figures (build_holdings_graph)
        from the latest snapshot — gives the us-situs estate figure, the
        per-symbol breakdown, net worth, and the NVDA pct, all wired to the
        ``holdings`` membership node.
      * the canonical SCALAR derived values (FI margin, earliest_safe_age,
        liquid net worth) SOURCED from the resolver manifest and seeded as
        INPUT nodes (the resolver is the authoritative single source — we do
        not re-derive these here).
      * the canonical SURFACE nodes (register_canonical_surfaces), pointed at
        the node keys actually present via SUBJECT_NODE_MAP.

    ``resolver_values`` injects the canonical scalars (the resolver is heavy +
    re-entrant); when absent they are read from resolve_plan_numbers. Returns a
    recomputed, closed graph.
    """
    positions, fx = _snapshot_positions_fx(session, user_id)
    graph = build_holdings_graph(positions, fx)

    scalars = (
        dict(resolver_values) if resolver_values is not None
        else _resolver_scalars(session, user_id, decision_run_id)
    )
    # Seed the canonical scalar nodes as INPUTs carrying the resolver value.
    # register_canonical_surfaces assumes these DERIVED-equivalent nodes already
    # exist — ensure each canonical subject's node is present first.
    for key in _RESOLVER_SCALAR_KEYS:
        value = scalars.get(key, 0.0)
        if key not in graph.keys():
            graph.add_node(Node(key=key, kind=NodeKind.INPUT, value=value))
        else:
            graph.get(key).value = value

    register_canonical_surfaces(graph, subject_node_map=SUBJECT_NODE_MAP)
    graph.recompute()
    return graph


# --------------------------------------------------------------------------- #
# Task 2 — change-request -> adjudicate -> ladder -> apply                    #
# --------------------------------------------------------------------------- #

def _ownership(graph: DerivationGraph, recipe_node_keys: set[str] | None) -> OwnershipMap:
    return OwnershipMap(graph, recipe_node_keys=set(recipe_node_keys or ()))


def _apply_change(
    graph: DerivationGraph,
    cr: ChangeRequest,
    participants: LadderParticipants,
    *,
    owners: OwnershipMap,
) -> AppliedChange:
    """Adjudicate one change-request; route NEEDS_LADDER through the negotiation
    ladder; apply only an accepted input/recipe-resolved change. A
    genuine-decision escalation becomes a real client question (NOT applied)."""
    outcome = adjudicate(cr, owners)

    if outcome.disposition is Disposition.REJECTED:
        return AppliedChange(cr, "rejected", applied=False, note=outcome.reason)

    if outcome.disposition is Disposition.NEEDS_AUDIT:
        # An audited, contestable input edit. For the cycle we apply it (it is
        # recorded as a change-request); the audit trail is the persisted CR.
        return _apply_input(graph, cr, note="needs_audit (applied + recorded)")

    if outcome.disposition is Disposition.ACCEPTED:
        return _apply_input(graph, cr)

    # NEEDS_LADDER — recipe/policy change is negotiated, not commanded.
    result = run_ladder(cr, participants)
    if result.terminal_state in (
        TerminalState.B_CONCEDED, TerminalState.ARBITER_RULED,
    ):
        # The change/ruling stands; apply if it resolves to an input value.
        if cr.kind in (ChangeKind.SET_INPUT, ChangeKind.SET_RECIPE) and "value" in cr.payload:
            target = cr.target_node_key
            node = graph.get(target) if target in graph.keys() else None
            if node is not None and node.kind is NodeKind.INPUT:
                return _apply_input(graph, cr, note=f"ladder:{result.terminal_state.value}")
        return AppliedChange(
            cr, "ladder_resolved", applied=False,
            note=f"{result.terminal_state.value} (no in-graph input to set)",
        )
    if result.terminal_state is TerminalState.A_CONCEDED:
        return AppliedChange(cr, "withdrawn", applied=False,
                             note="A_conceded (rebuttal landed)")
    # ESCALATED_TO_USER — a certified real client question.
    return AppliedChange(
        cr, "escalated_to_user", applied=False,
        real_question={
            "target_node_key": cr.target_node_key,
            "question": result.user_question or "",
            "proposed_value": cr.payload.get("value"),
            "author": f"{cr.author.kind.value}:{cr.author.role}",
        },
        note="escalated_to_user",
    )


def _apply_input(graph: DerivationGraph, cr: ChangeRequest, *, note: str = "") -> AppliedChange:
    """Apply an accepted input change to the graph (set_input)."""
    target = cr.target_node_key
    if target not in graph.keys():
        return AppliedChange(cr, "skipped", applied=False,
                             note=f"target {target} not in graph")
    node = graph.get(target)
    if node.kind is not NodeKind.INPUT:
        return AppliedChange(cr, "skipped", applied=False,
                             note=f"{target} is {node.kind.value}, not an INPUT")
    if "value" not in cr.payload:
        return AppliedChange(cr, "skipped", applied=False, note="no value in payload")
    graph.set_input(target, cr.payload["value"])
    return AppliedChange(cr, "applied", applied=True, dirtied_input=target, note=note)


# --------------------------------------------------------------------------- #
# Task 3/4 — propagate, re-render, recheck coherence, publish gate, persist    #
# --------------------------------------------------------------------------- #

def run_incremental_cycle(
    session,
    *,
    user_id: str,
    decision_run_id: int,
    change_requests: list[ChangeRequest] | None = None,
    participants: LadderParticipants | None = None,
    persist: bool = True,
    authorities: dict[str, object] | None = None,
    recipe_node_keys: set[str] | None = None,
    resolver_values: dict[str, float] | None = None,
) -> CycleResult:
    """Run ONE incremental plan cycle (gated behind ARGOSY_INCREMENTAL_PLAN).

    1. build_base_graph -> one closed graph.
    2. for each CR: adjudicate -> (ladder) -> apply; collect real questions +
       open flags (rejections/escalations); accumulate dirtied inputs.
    3. recompute() (blast radius only) re-renders the affected DERIVED+SURFACE
       nodes deterministically.
    4. recheck_coherence over the WHOLE artifact -> open_flags.
    5. closed = is_closed() and not open_flags and not real_questions.
    6. publish gate (can_publish_plan) -> promotable (fail-closed).
    7. if persist: save_graph + one propagation_events row (replay_ref).
    """
    if not _flag_on():
        raise IncrementalPlanDisabled(
            f"{FLAG_ENV} is off; from-scratch synthesis is untouched"
        )

    change_requests = list(change_requests or [])
    graph = build_base_graph(
        session, user_id, decision_run_id=decision_run_id,
        resolver_values=resolver_values,
    )

    # Bind the coherence recheck to the canonical surface->concept map (every
    # surface of a subject reads the same node, so coherence sees identical
    # values — a basis-flip is impossible by construction).
    register_surface_concepts(canonical_surface_concepts())

    # A recipe/policy change-request may target a policy node that is not a
    # materialized derived/collection node in the base graph (it is a policy
    # seed, e.g. the SWR assumption). Seed any declared recipe node that is
    # absent as a valueless INPUT so adjudicate() can classify it RECIPE and
    # route it through the ladder (it is never applied as a graph value here).
    for key in set(recipe_node_keys or ()):
        if key not in graph.keys():
            graph.add_node(Node(key=key, kind=NodeKind.INPUT, value=None))

    owners = _ownership(graph, recipe_node_keys)
    real_questions: list[dict] = []
    open_flags: list[str] = []
    for cr in change_requests:
        applied = _apply_change(graph, cr, participants, owners=owners)
        if applied.real_question is not None:
            real_questions.append(applied.real_question)
        if applied.disposition == "rejected":
            open_flags.append(f"rejected:{cr.target_node_key} ({applied.note})")

    recomputed = graph.recompute()

    violations = recheck_coherence(graph)
    for v in violations:
        open_flags.append(f"coherence:{getattr(v, 'concept', v)!r}")

    closed = graph.is_closed() and not open_flags and not real_questions

    # Publish gate (fail-closed): an open coherence flag OR any open real
    # question blocks promotion even with all authorities clear.
    publish_flags: list[OpenFlag] = [
        OpenFlag(node_key="cross_surface", kind="coherence") for _ in violations
    ]
    publish_flags += [
        OpenFlag(node_key=q["target_node_key"], kind="hard") for q in real_questions
    ]
    if authorities is not None:
        decision = can_publish_plan(authorities=authorities, open_flags=publish_flags)
        promotable = decision.can_promote
    else:
        # No authority set supplied -> promotability is unknown; fail-closed.
        promotable = False

    replay_ref: str | None = None
    if persist:
        replay_ref = _persist(session, user_id, graph, recomputed)

    return CycleResult(
        closed=closed,
        real_questions=real_questions,
        open_flags=open_flags,
        recomputed=recomputed,
        replay_ref=replay_ref,
        promotable=promotable,
        graph=graph,
    )


def _persist(
    session, user_id: str, graph: DerivationGraph, recomputed: list[str],
) -> str:
    """Save the graph to the latest plan_version's plan_nodes/edges and emit ONE
    propagation_events row for the cycle. Returns a replay_ref of the form
    ``plan:<plan_id>:cycle:<cycle_id>``."""
    from sqlalchemy import select

    from argosy.state.graph_store import save_graph
    from argosy.state.models import PlanVersion, PropagationEvent

    pv = session.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id)
        .where(PlanVersion.role.in_(("draft", "current")))
        .order_by(PlanVersion.id.desc())
    ).scalars().first()
    if pv is None:
        return ""
    plan_id = pv.id
    cycle_id = uuid.uuid4().hex[:16]

    save_graph(session, plan_id, graph)

    rerendered = [
        k for k in recomputed if graph.get(k).kind is NodeKind.SURFACE
    ]
    event = PropagationEvent(
        plan_id=plan_id,
        cycle_id=cycle_id,
        trigger_node_key="incremental_cycle",
        invalidated_node_keys_json=json.dumps(sorted(recomputed)),
        recomputed_json=json.dumps(
            {k: {"new": graph.get(k).value} for k in recomputed}, default=str
        ),
        rerendered_surfaces_json=json.dumps(sorted(rerendered)),
        verification_verdicts_json=json.dumps({}),
    )
    session.add(event)
    session.commit()
    return f"plan:{plan_id}:cycle:{cycle_id}"


__all__ = [
    "FLAG_ENV",
    "SUBJECT_NODE_MAP",
    "IncrementalPlanDisabled",
    "CycleResult",
    "AppliedChange",
    "build_base_graph",
    "run_incremental_cycle",
]
