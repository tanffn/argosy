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
class CodexFindingNode:
    """One CodexFinding rendered as a sub-row under the codex_second_opinion node.

    Mirrors ``argosy.orchestrator.flows.plan_synthesis.codex_second_opinion.CodexFinding``
    but lives here as a plain dataclass so ``dataclasses.asdict`` can walk
    the whole tree without dragging pydantic into the route serializer.
    Populated only for the ``codex_second_opinion`` node — every other
    AgentNode keeps an empty list (see ``AgentNode.codex_findings``).
    """

    severity: str  # "BLOCKER" / "AMBER" / "YELLOW"
    topic: str
    detail: str
    suggested_fix: str = ""


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
    # Populated only for the codex_second_opinion node — the parsed
    # CodexSecondOpinion.findings list rendered as expandable sub-rows
    # in the UI. Empty list for every other node so the field is always
    # present (consistent JSON shape).
    codex_findings: list[CodexFindingNode] = field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Cost breakdown (per-run observability — sums agent_reports.cost_usd by
# phase + role so the /decisions/[id] page can show "this synthesis cost
# $X total — $Y was the synthesizer, $Z was codex"). Computed AFTER the
# tree is built so it covers every row the run produced, even those the
# tree dedups for topology rendering.
# ---------------------------------------------------------------------------

# Stable phase keys for the breakdown dict. Mirrors the synthesis flow's
# orchestrator phases (1..5 + the 4.5 codex half-step). Kept as a fixed
# vocabulary so the UI can render a deterministic table; phases the run
# didn't enter simply show $0 / 0× rather than being absent.
COST_PHASE_KEYS: tuple[str, ...] = (
    "phase_1",
    "phase_2",
    "phase_3",
    "phase_4",
    "phase_4_5_codex",
    "phase_5",
)

# Role -> phase fallback used when an agent_reports row has no phase_id
# (legacy rows from before migration 0020 / runs that hit the recorder's
# fail-soft path). Mirrors the synthesis topology in
# ``argosy/orchestrator/flows/plan_synthesis/orchestrator.py``.
_ROLE_TO_PHASE_FALLBACK: dict[str, str] = {
    # Phase 1 analysts
    "concentration": "phase_1",
    "fx": "phase_1",
    "fundamentals": "phase_1",
    "news": "phase_1",
    "sentiment": "phase_1",
    "technical": "phase_1",
    "macro": "phase_1",
    "tax": "phase_1",
    "household_budget": "phase_1",
    "plan_critique": "phase_1",
    # Phase 2 debate
    "bull_researcher": "phase_2",
    "bear_researcher": "phase_2",
    "researcher_facilitator": "phase_2",
    # Phase 3 synthesis
    "plan_synthesizer": "phase_3",
    # Phase 4 risk
    "risk_officer": "phase_4",
    "risk_facilitator": "phase_4",
    # Phase 4.5 codex half-step
    "codex_second_opinion": "phase_4_5_codex",
    # Phase 5 FM
    "fund_manager": "phase_5",
}


@dataclass(frozen=True)
class CostBreakdown:
    """Aggregated cost view of one decision_run.

    Computed by walking the ``agent_reports`` rows for the run and
    grouping by phase (via ``phase_id`` -> ``decision_phases.kind`` when
    present, role-based fallback otherwise) and role. NULL ``cost_usd``
    is treated as 0 (some legacy rows have it).

    All cost values are USD floats. ``top_3_agents`` is the three most
    expensive roles in the run sorted descending — when ties occur the
    role name breaks alphabetically (stable for snapshots).
    ``cost_per_phase_table`` is a UI-friendly projection of ``by_phase``
    + per-phase agent counts so the React side doesn't have to recompute.
    """

    total_usd: float
    by_phase: dict[str, float]
    by_role: dict[str, float]
    top_3_agents: list[tuple[str, float]]
    agent_count: int
    cost_per_phase_table: list[dict]


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
    # Per-run cost aggregation (total + by-phase + by-role + top-3). Added
    # so /decisions/[id] can render a "this synthesis cost $X" card
    # without re-querying agent_reports from the UI. Always populated —
    # empty rollup ($0 / 0 agents) when the run has no agent_reports yet.
    cost_breakdown: CostBreakdown = field(
        default_factory=lambda: CostBreakdown(
            total_usd=0.0,
            by_phase={k: 0.0 for k in COST_PHASE_KEYS},
            by_role={},
            top_3_agents=[],
            agent_count=0,
            cost_per_phase_table=[
                {"phase": k, "cost": 0.0, "agent_count": 0}
                for k in COST_PHASE_KEYS
            ],
        )
    )


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
            cost_breakdown=_compute_cost_breakdown(
                reports=non_synth_reports, phases=[],
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

    # Phase 4.5: codex_second_opinion — independent cross-engine reviewer
    # (gpt-5 via the codex-tandem kit) that runs between risk and FM. The
    # FM reads its verdict, so it's a sibling under FM alongside synth,
    # risk_facilitator, and plan_critique. If no codex row exists (older
    # runs predating commit 0bedd9b, or codex was disabled via the env
    # var kill switch), the node renders as "skipped" with a directed
    # failure_reason.
    codex_row = pop_one("codex_second_opinion")
    codex_node = _build_codex_node(codex_row)

    # Phase 5: Fund Manager — root of the DAG. Reads synth + risk
    # facilitator + (separately) the plan_critique analyst + codex's
    # second-opinion verdict.
    fm_node = _to_node(
        pop_one("fund_manager"),
        role="fund_manager",
        children=[
            synth_node,
            risk_facilitator_node,
            analyst_nodes["plan_critique"],
            codex_node,
        ],
    )

    return AgentTreeResponse(
        decision_run_id=decision_run_id,
        decision_kind=run.decision_kind,
        status_summary=_summarize(fm_node, adapter_outcomes_p1),
        root=fm_node,
        cost_breakdown=_compute_cost_breakdown(
            reports=reports, phases=phases,
        ),
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


# Mapping from CodexSecondOpinion.overall_assessment to a confidence-band
# string the UI already styles. Mirrors the (HIGH/MEDIUM/LOW) bands the
# other analyst nodes use so the codex pill looks at-home.
_CODEX_ASSESSMENT_TO_CONFIDENCE: dict[str, str] = {
    "APPROVE": "HIGH",
    "APPROVE_WITH_CONDITIONS": "MEDIUM",
    "BLOCK": "LOW",
}


def _build_codex_node(r: AgentReport | None) -> AgentNode:
    """Render the codex_second_opinion row as an AgentNode under FM.

    Strategy:
      * No row: render as ``skipped`` with a direct failure_reason so the
        UI shows the slot but doesn't pretend codex ran. This happens for
        older synth runs (pre-commit 0bedd9b) and for runs where codex
        was disabled by the env-var kill switch.
      * Row present + ``response_text`` parses as a CodexSecondOpinion:
        confidence-band derived from ``overall_assessment``; findings
        attached to ``codex_findings``; a single-line summary in
        ``response_excerpt`` covering finding count + agreement-with-risk.
      * Row present but ``response_text`` is unparseable JSON: node status
        flips to ``degraded``, ``response_excerpt`` carries the raw text
        (first 500 chars) so the UI still surfaces something useful.

    No exception escapes this helper — the FM-rooted tree must render
    even when codex emits garbage.
    """
    if r is None:
        return AgentNode(
            agent_role="codex_second_opinion",
            agent_report_id=None,
            status="skipped",
            confidence=None,
            model=None,
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
            side=None,
            perspective=None,
            response_excerpt="",
            failure_reason="codex zigzag not run for this synthesis",
            children=[],
            adapters=[],
            codex_findings=[],
        )

    raw_text = r.response_text or ""
    parsed = _parse_codex_response_text(raw_text)
    if parsed is None:
        # Row exists but the verdict body is unparseable. Surface as
        # "degraded" so the UI flags it visually, and carry the raw text
        # so the operator can manually review.
        return AgentNode(
            agent_role="codex_second_opinion",
            agent_report_id=r.id,
            status="degraded",
            confidence=r.confidence,
            model=r.model,
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            cost_usd=(
                float(r.cost_usd) if r.cost_usd is not None else None
            ),
            side=None,
            perspective=None,
            response_excerpt=raw_text[:500],
            failure_reason=(
                "codex verdict JSON unparseable — raw response preserved"
            ),
            children=[],
            adapters=[],
            codex_findings=[],
        )

    # Happy path — parsed verdict. Derive the confidence band from the
    # overall_assessment, build a one-line excerpt that surfaces the
    # finding count + agreement-with-risk + novel-concerns count.
    overall = str(parsed.get("overall_assessment") or "")
    confidence = _CODEX_ASSESSMENT_TO_CONFIDENCE.get(overall)
    findings_raw = parsed.get("findings") or []
    findings: list[CodexFindingNode] = []
    if isinstance(findings_raw, list):
        for f in findings_raw:
            if not isinstance(f, dict):
                continue
            findings.append(
                CodexFindingNode(
                    severity=str(f.get("severity") or ""),
                    topic=str(f.get("topic") or ""),
                    detail=str(f.get("detail") or ""),
                    suggested_fix=str(f.get("suggested_fix") or ""),
                )
            )
    agreement = parsed.get("agreement_with_argosy") or {}
    if not isinstance(agreement, dict):
        agreement = {}
    agrees = agreement.get("agrees_with_risk_verdict")
    novel = agreement.get("novel_concerns_argosy_missed") or []
    novel_count = len(novel) if isinstance(novel, list) else 0
    excerpt_parts: list[str] = []
    if overall:
        excerpt_parts.append(overall)
    excerpt_parts.append(f"{len(findings)} findings")
    if agrees is not None:
        excerpt_parts.append(f"agrees_with_risk={agrees}")
    if novel_count:
        excerpt_parts.append(f"{novel_count} novel concerns")
    if findings:
        topics_preview = ", ".join(
            f.topic for f in findings[:3] if f.topic
        )
        if topics_preview:
            excerpt_parts.append(f"topics: {topics_preview}")
    excerpt = " · ".join(excerpt_parts)[:500]

    return AgentNode(
        agent_role="codex_second_opinion",
        agent_report_id=r.id,
        status="ok",
        confidence=confidence,
        model=r.model,
        tokens_in=r.tokens_in,
        tokens_out=r.tokens_out,
        cost_usd=float(r.cost_usd) if r.cost_usd is not None else None,
        side=None,
        perspective=None,
        response_excerpt=excerpt,
        failure_reason=None,
        children=[],
        adapters=[],
        codex_findings=findings,
    )


def _parse_codex_response_text(text: str) -> dict | None:
    """Strict-then-lenient JSON parse of a codex_second_opinion row.

    Mirrors the parser in
    ``argosy.orchestrator.flows.plan_synthesis.codex_second_opinion._parse_codex_verdict``
    but returns a raw dict (not a pydantic model) so this module avoids a
    dependency on pydantic / on the codex_second_opinion module's import
    graph. Returns ``None`` on any parse failure so the caller can fall
    back to the "degraded" rendering path.
    """
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (TypeError, ValueError):
        pass
    # Lenient: locate first '{' and try raw_decode.
    first_brace = cleaned.find("{")
    if first_brace >= 0:
        try:
            decoder = json.JSONDecoder(strict=False)
            obj, _ = decoder.raw_decode(cleaned[first_brace:])
            if isinstance(obj, dict):
                return obj
        except (TypeError, ValueError):
            pass
    return None


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


def _phase_kind_to_cost_key(kind: str | None) -> str | None:
    """Map a ``decision_phases.kind`` string to a ``COST_PHASE_KEYS`` slot.

    Returns ``None`` for unrecognised kinds (e.g. ``plan_synthesis.verdict``
    — a metadata-only row that doesn't host agent_reports, and the legacy
    monolithic ``plan_synthesis`` kind). The codex half-step uses
    ``phase_n=45`` -> ``synthesis.phase_45`` so we map that to
    ``phase_4_5_codex`` explicitly.
    """
    if not kind:
        return None
    if kind == "synthesis.phase_45":
        return "phase_4_5_codex"
    m = re.match(r"^synthesis\.phase_(\d+)$", kind)
    if not m:
        return None
    n = int(m.group(1))
    if 1 <= n <= 5:
        return f"phase_{n}"
    return None


def _compute_cost_breakdown(
    *,
    reports: list[AgentReport],
    phases: list[DecisionPhase],
) -> CostBreakdown:
    """Aggregate ``agent_reports.cost_usd`` for one decision_run.

    Phase resolution order per row:
      1. ``r.phase_id`` -> ``decision_phases.kind`` -> ``COST_PHASE_KEYS``
         slot (via ``_phase_kind_to_cost_key``). This is the canonical
         source; runs on/after migration 0020 stamp ``phase_id`` for
         every agent_reports row.
      2. Role-based fallback (``_ROLE_TO_PHASE_FALLBACK``) — covers
         pre-0020 rows, runs where the recorder's fail-soft path skipped
         the back-link, and unrecognised phase kinds.
      3. Drop the row from the by_phase histogram (the by_role + total
         still count it) when neither resolves. Should be rare; the UI
         surfaces by_role anyway so the spend is visible.

    NULL ``cost_usd`` is treated as 0 (legacy rows from before cost
    capture or rows where the LLM call failed before the recorder could
    stamp a price). Same for the row count semantics — every row in
    ``reports`` contributes to ``agent_count`` regardless of whether
    cost is known.
    """
    # Index phase_id -> cost_key for O(1) lookup.
    phase_id_to_cost_key: dict[int, str] = {}
    for p in phases:
        key = _phase_kind_to_cost_key(p.kind)
        if key is not None:
            phase_id_to_cost_key[p.id] = key

    by_phase: dict[str, float] = {k: 0.0 for k in COST_PHASE_KEYS}
    phase_agent_counts: dict[str, int] = {k: 0 for k in COST_PHASE_KEYS}
    by_role: dict[str, float] = {}
    total = 0.0
    for r in reports:
        cost = float(r.cost_usd) if r.cost_usd is not None else 0.0
        total += cost
        by_role[r.agent_role] = by_role.get(r.agent_role, 0.0) + cost

        # Resolve phase: phase_id linkage first, role fallback second.
        cost_key: str | None = None
        if r.phase_id is not None:
            cost_key = phase_id_to_cost_key.get(r.phase_id)
        if cost_key is None:
            cost_key = _ROLE_TO_PHASE_FALLBACK.get(r.agent_role)
        if cost_key is not None and cost_key in by_phase:
            by_phase[cost_key] += cost
            phase_agent_counts[cost_key] += 1

    # Top 3 roles by spend, descending. Stable secondary sort on role
    # name so snapshots don't shuffle when two roles tie (e.g. legacy
    # zero-cost rows).
    sorted_roles = sorted(
        by_role.items(), key=lambda kv: (-kv[1], kv[0]),
    )
    top_3 = [(role, cost) for role, cost in sorted_roles[:3]]

    table = [
        {
            "phase": k,
            "cost": by_phase[k],
            "agent_count": phase_agent_counts[k],
        }
        for k in COST_PHASE_KEYS
    ]
    return CostBreakdown(
        total_usd=total,
        by_phase=by_phase,
        by_role=by_role,
        top_3_agents=top_3,
        agent_count=len(reports),
        cost_per_phase_table=table,
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
    # Defensive: the codex_second_opinion row is built once per run and
    # only mounted under FM, so identity dedup (above) is sufficient in
    # the steady state. But if a future refactor accidentally references
    # the codex node under more than one parent without sharing the same
    # Python object, the id()-keyed walker would double-count it. Track
    # the role explicitly so we count codex at most once even across
    # distinct AgentNode instances with the same role.
    codex_role_counted = False

    def walk(n: AgentNode) -> None:
        nonlocal agents_ok, agents_failed, codex_role_counted
        key = id(n)
        if key in seen:
            return
        seen.add(key)
        if n.agent_role == "codex_second_opinion":
            if codex_role_counted:
                # Already counted via a sibling reference; still recurse
                # into children so any unique descendants get counted.
                for c in n.children:
                    walk(c)
                return
            codex_role_counted = True
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
