"""Fund-Manager-rooted agent-tree DAG builder (T0.4 — observability cornerstone).

Pure function that walks ``decision_phases`` + ``agent_reports`` + adapter
outcomes for one ``decision_run_id`` and returns a nested ``AgentTreeResponse``
DTO suitable for rendering the FM-rooted DAG in the UI's observability view.

Topology is hard-coded for synthesis runs (``decision_kind='plan_revision'``,
which is the orchestrator's internal name for the 5-phase synthesis flow).
Future flow kinds (e.g. trade_proposal) will get their own builder.

The builder is deliberately defensive about pre-T0.1 runs:

* ``participants_json`` was empty for every phase prior to commit ``70c481e``
  (T0.1 — landed 2026-05-26). For those old runs the only way to recover
  which agents ran is to fan out by ``agent_reports.decision_id ==
  f"plan-synth-{run_id}"`` and group by ``agent_role``. We do exactly that
  here as the *primary* mechanism — it also works for new runs because
  T0.1 left the ``decision_id`` stamping in place. ``participants_json``
  becomes the preferred source once we extend the builder to expose
  per-phase metadata (out of scope for T0.4).

* ``decision_phases`` for old runs may have a single row with ``kind=
  'plan_synthesis'``; new runs have ``synthesis.phase_1..5``. We accept
  both: phase-1 adapter outcomes are looked up by either kind matching the
  ``synthesis.phase_1`` regex *or* by the older monolithic
  ``plan_synthesis`` kind.

* Phase-1 ``phase_output_json`` only carries ``adapter_outcomes`` for runs
  on/after commit ``cd79755`` (T0.3 — adapters wired to outcome tracker).
  For older runs the adapter list is simply empty, which is fine — the
  agent tree is still useful even without adapter status.

The builder performs NO DB writes, NO HTTP, NO LLM calls. It is safe to
invoke from a synchronous request handler with a short-lived session.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.state.models import AgentReport, DecisionPhase, DecisionRun

# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

NodeStatus = Literal["ok", "degraded", "failed", "skipped"]
AdapterStatus = Literal["ok", "empty", "http_error", "exception"]

# Synthesis decision_kind in the DB. The plan refers to it as "synthesis"
# but the orchestrator stamps "plan_revision". Accept both spellings so a
# future rename doesn't silently break the route. See
# ``argosy/orchestrator/flows/plan_synthesis/orchestrator.py`` for the
# canonical value.
SYNTHESIS_KINDS: frozenset[str] = frozenset({"plan_revision", "synthesis"})

# Match either the new per-phase kind ("synthesis.phase_1") or the older
# monolithic "plan_synthesis" kind that pre-T2.3 runs used.
_PHASE_1_KIND_PATTERN = re.compile(r"^(synthesis\.phase_1|plan_synthesis)$")

# Analyst roles in Phase 1 in stable display order. ``plan_critique`` is
# included because the FM consults it directly as a sibling to the synth
# and risk-facilitator subtrees; older runs (e.g. #23) won't have it and
# the builder degrades to a "skipped" node — that's intentional.
_ANALYST_ROLES: tuple[str, ...] = (
    "concentration",
    "fx",
    "fundamentals",
    "news",
    "sentiment",
    "technical",
    "macro",
    "tax",
    "household_budget",
    "plan_critique",
)

# Mapping from analyst role -> set of adapter names that fed it. Used to
# attach the right adapter outcomes as leaves under each analyst node.
# Only roles with external data dependencies appear here; concentration /
# tax / household_budget / plan_critique are DB-only.
_ROLE_TO_ADAPTERS: dict[str, frozenset[str]] = {
    "news": frozenset({"finnhub_news"}),
    "fundamentals": frozenset({"finnhub"}),
    "technical": frozenset({"yfinance"}),
    "sentiment": frozenset({"tipranks"}),
    "macro": frozenset({"fred"}),
    "fx": frozenset({"boi"}),
}


@dataclass
class AdapterNode:
    adapter_name: str
    target: str | None
    status: AdapterStatus
    latency_ms: int
    payload_size_bytes: int
    http_status_code: int | None
    error_text: str | None


@dataclass
class AgentNode:
    agent_role: str  # e.g. "fund_manager"
    agent_report_id: int | None
    status: NodeStatus
    confidence: str | None  # HIGH / MEDIUM / LOW / None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    side: str | None         # "bull" / "bear" / None
    perspective: str | None  # "aggressive" / "neutral" / "conservative" / None
    response_excerpt: str    # first 500 chars of response_text
    failure_reason: str | None  # set when status == failed or skipped
    children: list["AgentNode"] = field(default_factory=list)
    adapters: list[AdapterNode] = field(default_factory=list)


# T4.4 — recognised non-synthesis kinds that the decisions-replay surface
# routes through ``build_agent_tree`` without crashing. Each is a small,
# single-purpose run that doesn't have the 18-agent topology synthesis
# does; the builder returns ``root=None`` for them and lets the route /
# UI render a kind-appropriate summary instead.
#
# Listed here (not in a separate constant) so the builder is the single
# source of truth — any new kind must show up here AND get a row renderer
# in ui/src/components/agent/DecisionAccordion.tsx.
NON_SYNTHESIS_KINDS: frozenset[str] = frozenset({
    "delta_pushback",     # T4.3 — slim re-debate per PlanDeltaItem
    "daily_brief",        # T4.5 — daily brief generation run
    "trade_proposal",     # legacy per-trade decision flow (Phase 3)
    "plan_amendment_chat",  # Wave 4 chat-driven amendment flow
})


@dataclass
class AgentTreeResponse:
    decision_run_id: int
    decision_kind: str
    status_summary: dict[str, int]
    # ^ e.g. {"agents_ok": 17, "agents_failed": 1, "adapters_ok": 5, "adapters_failed": 2}
    # T4.4 — ``root`` is ``None`` for non-synthesis kinds (delta_pushback,
    # daily_brief, trade_proposal, plan_amendment_chat). The status_summary
    # still carries useful counts in that case (one row per unique
    # agent_role under the run's decision_id). The route returns 200 with
    # an empty-tree payload rather than 404 so the UI can render a
    # kind-appropriate summary instead of an error banner.
    root: AgentNode | None
    # T4.4 — populated when the builder doesn't produce a DAG (i.e. the
    # decision_kind isn't a synthesis kind). The UI surfaces this string
    # in the "no tree available" placeholder. None for synthesis runs.
    unsupported_reason: str | None = None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_agent_tree(db: Session, decision_run_id: int) -> AgentTreeResponse:
    """Build the FM-rooted DAG for one synthesis ``decision_run``.

    Args:
        db: a SQLAlchemy session. Pure read; no flush/commit happens here.
        decision_run_id: the ``decision_runs.id`` to traverse.

    Returns:
        ``AgentTreeResponse`` with the fund_manager node at ``root`` and
        a deduplicated ``status_summary``.

    Raises:
        ValueError: if the run doesn't exist. (Unknown decision_kinds NO
            LONGER raise — see T4.4 note below.)

    T4.4 behaviour change: for any recognised non-synthesis kind (see
    ``NON_SYNTHESIS_KINDS``) the builder returns an ``AgentTreeResponse``
    with ``root=None`` and a populated ``unsupported_reason`` so the
    /decisions/{id} route can serve a 200 with an explanatory payload
    instead of a 404. Truly unknown kinds (not in either
    ``SYNTHESIS_KINDS`` or ``NON_SYNTHESIS_KINDS``) are treated the same
    way — the builder degrades gracefully rather than crashing the
    replay UI for an unrecognised future kind.
    """
    run = db.get(DecisionRun, decision_run_id)
    if run is None:
        raise ValueError(f"decision_run_id={decision_run_id} not found")

    if run.decision_kind not in SYNTHESIS_KINDS:
        # T4.4 — return a populated DTO with root=None for any
        # non-synthesis kind. We still pull the agent_reports for the
        # run so status_summary carries meaningful "how many agents
        # ran" / "how many failed" counts that the UI can show in lieu
        # of a tree.
        decision_id_str_simple = str(decision_run_id)
        # Most non-synthesis flows stamp agent_reports.decision_id as
        # str(decision_run_id) directly; the synthesis flow uses the
        # plan-synth-<id> prefix. Try both so legacy + future-non-
        # synthesis runs both light up.
        non_synth_reports = list(
            db.execute(
                select(AgentReport)
                .where(
                    AgentReport.decision_id.in_([
                        decision_id_str_simple,
                        f"plan-synth-{decision_run_id}",
                    ])
                )
                .order_by(AgentReport.id)
            ).scalars()
        )
        agents_ok = sum(
            1 for r in non_synth_reports
            if (r.confidence or "").upper() != "LOW"
        )
        agents_failed = len(non_synth_reports) - agents_ok
        return AgentTreeResponse(
            decision_run_id=decision_run_id,
            decision_kind=run.decision_kind or "unknown",
            status_summary={
                "agents_ok": agents_ok,
                "agents_failed": agents_failed,
                "adapters_ok": 0,
                "adapters_failed": 0,
            },
            root=None,
            unsupported_reason=(
                f"agent-tree DAG is only built for synthesis runs; "
                f"decision_kind={run.decision_kind!r} is rendered as a "
                f"flat row in /decisions instead"
            ),
        )

    # Pull all agent_reports for this synthesis run, ordered by id so role
    # buckets pop chronologically (risk officers, in particular, must come
    # out aggressive -> neutral -> conservative if they were written that
    # way; the orchestrator does write them in that order today).
    decision_id_str = f"plan-synth-{decision_run_id}"
    reports = list(
        db.execute(
            select(AgentReport)
            .where(AgentReport.decision_id == decision_id_str)
            .order_by(AgentReport.id)
        ).scalars()
    )
    by_role: dict[str, list[AgentReport]] = {}
    for r in reports:
        by_role.setdefault(r.agent_role, []).append(r)

    # Pull phase rows so we can extract Phase-1 adapter outcomes.
    phases = list(
        db.execute(
            select(DecisionPhase)
            .where(DecisionPhase.decision_run_id == decision_run_id)
            .order_by(DecisionPhase.seq)
        ).scalars()
    )
    adapter_outcomes_p1 = _extract_phase_1_adapter_outcomes(phases)

    # Pop helper: each call removes the next available row for that role.
    # Risk officers have 3 rows; pop_one stamps the perspective from the
    # call site (the DB doesn't carry perspective today).
    def pop_one(role: str) -> AgentReport | None:
        candidates = by_role.get(role) or []
        if not candidates:
            return None
        return candidates.pop(0)

    # Build leaves first: Phase-1 analysts. Each analyst gets the adapters
    # that fed it as leaf children of its own ``adapters`` list. The same
    # analyst node object is referenced by multiple parents (bull, bear,
    # plan_synth) — the status_summary walker dedups by id().
    analyst_nodes: dict[str, AgentNode] = {}
    for role in _ANALYST_ROLES:
        r = pop_one(role)
        adapters_for_role = [
            a for a in adapter_outcomes_p1
            if _adapter_feeds_role(a.adapter_name, role)
        ]
        analyst_nodes[role] = _to_node(
            r, role=role, adapters=adapters_for_role,
        )

    # Phase 2: researcher facilitator. Today agent_reports don't carry the
    # horizon, so we render three facilitator subtrees (short / medium /
    # long) by popping the same role three times; each pop falls back to
    # ``None`` (= skipped) once exhausted. Bull and bear are popped per
    # subtree on the same principle. All three subtrees reference the
    # *same* analyst node objects as their grandchildren — the DAG nature
    # is preserved by Python identity, which the summary walker honors.
    analyst_children = [analyst_nodes[r] for r in _ANALYST_ROLES]
    researcher_facilitator_nodes: list[AgentNode] = []
    for _ in range(3):
        bull = _to_node(
            pop_one("bull_researcher"),
            role="bull_researcher",
            side="bull",
            children=analyst_children,
        )
        bear = _to_node(
            pop_one("bear_researcher"),
            role="bear_researcher",
            side="bear",
            children=analyst_children,
        )
        researcher_facilitator_nodes.append(
            _to_node(
                pop_one("researcher_facilitator"),
                role="researcher_facilitator",
                children=[bull, bear],
            )
        )

    # Phase 3: plan synthesizer reads the researcher facilitators AND the
    # Phase-1 analysts directly (the synth ingests phase-1 outputs without
    # going through the bull/bear debate).
    synth_node = _to_node(
        pop_one("plan_synthesizer"),
        role="plan_synthesizer",
        children=[*researcher_facilitator_nodes, *analyst_children],
    )

    # Phase 4: risk facilitator + three risk officers (perspectives stamped
    # at the call site since the DB column doesn't exist).
    risk_facilitator_node = _to_node(
        pop_one("risk_facilitator"),
        role="risk_facilitator",
        children=[
            _to_node(
                pop_one("risk_officer"),
                role="risk_officer",
                perspective="aggressive",
            ),
            _to_node(
                pop_one("risk_officer"),
                role="risk_officer",
                perspective="neutral",
            ),
            _to_node(
                pop_one("risk_officer"),
                role="risk_officer",
                perspective="conservative",
            ),
        ],
    )

    # Phase 5: Fund Manager — root of the DAG. Reads synth + risk
    # facilitator + (separately) the plan_critique analyst.
    fm_node = _to_node(
        pop_one("fund_manager"),
        role="fund_manager",
        children=[
            synth_node,
            risk_facilitator_node,
            analyst_nodes["plan_critique"],
        ],
    )

    return AgentTreeResponse(
        decision_run_id=decision_run_id,
        decision_kind=run.decision_kind,
        status_summary=_summarize(fm_node, adapter_outcomes_p1),
        root=fm_node,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_phase_1_adapter_outcomes(
    phases: list[DecisionPhase],
) -> list[AdapterNode]:
    """Decode the ``adapter_outcomes`` list from Phase 1's ``phase_output_json``.

    Returns an empty list if no Phase-1 row is present, if the row has no
    ``phase_output_json``, if the JSON is malformed, or if the payload
    doesn't carry an ``adapter_outcomes`` key (the case for pre-T0.3 runs).
    The function deliberately doesn't raise — observability must work on
    legacy rows.
    """
    phase_1 = next(
        (p for p in phases if _PHASE_1_KIND_PATTERN.match(p.kind or "")),
        None,
    )
    if phase_1 is None or not phase_1.phase_output_json:
        return []
    try:
        payload = json.loads(phase_1.phase_output_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    raw_outcomes = payload.get("adapter_outcomes") or []
    if not isinstance(raw_outcomes, list):
        return []
    nodes: list[AdapterNode] = []
    for o in raw_outcomes:
        if not isinstance(o, dict):
            continue
        try:
            nodes.append(
                AdapterNode(
                    adapter_name=str(o.get("adapter_name", "")),
                    target=o.get("target"),
                    status=o.get("status", "ok"),
                    latency_ms=int(o.get("latency_ms", 0) or 0),
                    payload_size_bytes=int(
                        o.get("payload_size_bytes", 0) or 0
                    ),
                    http_status_code=o.get("http_status_code"),
                    error_text=o.get("error_text"),
                )
            )
        except (TypeError, ValueError):
            # One bad row shouldn't drop the others.
            continue
    return nodes


def _adapter_feeds_role(adapter_name: str, role: str) -> bool:
    return adapter_name in _ROLE_TO_ADAPTERS.get(role, frozenset())


def _to_node(
    r: AgentReport | None,
    *,
    role: str,
    side: str | None = None,
    perspective: str | None = None,
    children: list[AgentNode] | None = None,
    adapters: list[AdapterNode] | None = None,
) -> AgentNode:
    """Wrap an ``AgentReport`` (or its absence) as an ``AgentNode``.

    Status mapping:
        - report present, confidence == "LOW" => "degraded"
        - report present, otherwise           => "ok"
        - report missing                      => "skipped"

    A truly "failed" status (e.g. the orchestrator marked the run dead)
    isn't reconstructible from agent_reports alone today — when the
    orchestrator gains a per-agent failure column we'll add the mapping
    here. "failed" is reserved in the literal so callers can recognise
    the eventual state without a DTO break.
    """
    if r is None:
        status: NodeStatus = "skipped"
    elif (r.confidence or "").upper() == "LOW":
        status = "degraded"
    else:
        status = "ok"

    return AgentNode(
        agent_role=role,
        agent_report_id=r.id if r else None,
        status=status,
        confidence=r.confidence if r else None,
        model=r.model if r else None,
        tokens_in=r.tokens_in if r else None,
        tokens_out=r.tokens_out if r else None,
        cost_usd=float(r.cost_usd) if r and r.cost_usd is not None else None,
        side=side,
        perspective=perspective,
        response_excerpt=(r.response_text or "")[:500] if r else "",
        failure_reason=None if r else "agent did not run",
        children=children or [],
        adapters=adapters or [],
    )


def _summarize(
    root: AgentNode, adapter_outcomes: list[AdapterNode]
) -> dict[str, int]:
    """Count agents + adapters by status, deduping shared nodes by id().

    Analyst nodes are referenced by multiple parents (bull/bear/synth),
    so a naive recursive count would inflate the totals 3-10×. We walk
    once and skip any node we've already visited.
    """
    agents_ok = 0
    agents_failed = 0
    seen: set[int] = set()

    def walk(n: AgentNode) -> None:
        nonlocal agents_ok, agents_failed
        key = id(n)
        if key in seen:
            return
        seen.add(key)
        if n.status in ("skipped", "failed"):
            agents_failed += 1
        else:
            # "ok" and "degraded" both count as "the agent ran".
            agents_ok += 1
        for c in n.children:
            walk(c)

    walk(root)

    adapters_ok = sum(1 for a in adapter_outcomes if a.status == "ok")
    adapters_failed = sum(
        1 for a in adapter_outcomes if a.status in ("http_error", "exception")
    )
    return {
        "agents_ok": agents_ok,
        "agents_failed": agents_failed,
        "adapters_ok": adapters_ok,
        "adapters_failed": adapters_failed,
    }
