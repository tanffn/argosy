"""Tests for ``argosy.services.agent_tree_builder.build_agent_tree`` (T0.4).

Two surfaces are exercised:

1. Against the real ``db/argosy.db`` (run #23) — proves the builder copes
   with the pre-T0.1 / pre-T0.3 schema, where ``participants_json`` is
   empty and Phase 1 has no ``adapter_outcomes``. The 18 agent_reports
   rows must still produce a FM-rooted tree.

2. Against a synthetic in-memory SQLite session — fully controlled
   topology, plus an injected ``adapter_outcomes`` list so the status
   summary, role->adapter mapping, and dedup walker all get covered.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.agent_tree_builder import (
    COST_PHASE_KEYS,
    AdapterNode,
    AgentNode,
    AgentTreeResponse,
    CostBreakdown,
    build_agent_tree,
)
from argosy.state.models import (
    AgentReport,
    Base,
    DecisionPhase,
    DecisionRun,
    User,
)


# ---------------------------------------------------------------------------
# Fixture: in-memory session ready for a hand-rolled synthesis run.
# ---------------------------------------------------------------------------


@pytest.fixture
def inmem_session():
    engine = sa.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        sess.add(User(id="ariel", plan="free"))
        sess.commit()
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _seed_synthesis_run(
    sess,
    *,
    user_id: str = "ariel",
    run_id: int | None = None,
    decision_kind: str = "plan_revision",
    include_plan_critique: bool = True,
    adapter_outcomes: list[dict] | None = None,
    risk_officer_count: int = 3,
) -> int:
    """Create a decision_run + a Phase-1 decision_phase + 18 agent_reports.

    ``adapter_outcomes`` (when given) is dumped under
    ``phase_output_json["adapter_outcomes"]`` exactly the way T0.3 writes
    it.

    Returns the new ``decision_runs.id``.
    """
    now = datetime.now(timezone.utc)
    run = DecisionRun(
        user_id=user_id,
        ticker="(plan)",
        tier=None,
        decision_kind=decision_kind,
        status="completed",
        started_at=now,
        finished_at=now,
    )
    if run_id is not None:
        run.id = run_id
    sess.add(run)
    sess.commit()
    sess.refresh(run)
    rid = run.id

    decision_id_str = f"plan-synth-{rid}"

    def mk(role: str, confidence: str | None = "MEDIUM",
           model: str = "claude-sonnet-4-6",
           response: str = "ok") -> AgentReport:
        ar = AgentReport(
            user_id=user_id,
            agent_role=role,
            decision_id=decision_id_str,
            response_text=response,
            confidence=confidence,
            model=model,
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )
        sess.add(ar)
        return ar

    analyst_roles = [
        "concentration", "fx", "fundamentals", "news",
        "sentiment", "technical", "macro", "tax",
        "household_budget",
    ]
    if include_plan_critique:
        analyst_roles.append("plan_critique")
    for role in analyst_roles:
        mk(role)
    mk("bull_researcher", model="claude-opus-4-7")
    mk("bear_researcher", model="claude-opus-4-7")
    mk("researcher_facilitator")
    mk("plan_synthesizer", confidence=None, model="claude-opus-4-7")
    for _ in range(risk_officer_count):
        mk("risk_officer")
    mk("risk_facilitator", confidence="LOW")
    mk("fund_manager", confidence=None, model="claude-opus-4-7")
    sess.commit()

    phase_output: dict = {"phase": 1}
    if adapter_outcomes is not None:
        phase_output["adapter_outcomes"] = adapter_outcomes
    phase = DecisionPhase(
        decision_run_id=rid,
        user_id=user_id,
        seq=1,
        kind="synthesis.phase_1",
        started_at=now,
        finished_at=now,
        participants_json="[]",
        phase_output_json=json.dumps(phase_output),
    )
    sess.add(phase)
    sess.commit()
    return rid


# ---------------------------------------------------------------------------
# 1) Real DB — run #23.
# ---------------------------------------------------------------------------


def _real_db_session():
    db_path = Path(os.environ.get("ARGOSY_HOME", ".")) / "db" / "argosy.db"
    if not db_path.exists():
        # Try the project-relative default.
        alt = Path("D:/Projects/financial-advisor/db/argosy.db")
        if alt.exists():
            db_path = alt
    if not db_path.exists():
        return None
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return SessionLocal()


def test_build_agent_tree_for_existing_run_23() -> None:
    sess = _real_db_session()
    if sess is None:
        pytest.skip("real db/argosy.db not available; skipping live-DB check")
    try:
        tree = build_agent_tree(sess, decision_run_id=23)
    finally:
        sess.close()

    # FM at root.
    assert isinstance(tree, AgentTreeResponse)
    assert tree.root.agent_role == "fund_manager"
    assert tree.decision_kind == "plan_revision"
    assert tree.decision_run_id == 23

    # FM's children: synth, risk_facilitator, plan_critique (legacy),
    # plus codex_second_opinion (rendered as "skipped" for runs predating
    # the codex ZigZag in 0bedd9b).
    child_roles = {c.agent_role for c in tree.root.children}
    assert {
        "plan_synthesizer",
        "risk_facilitator",
        "plan_critique",
        "codex_second_opinion",
    } <= child_roles

    # The synth has the three researcher facilitators + 10 analysts as children.
    synth = next(c for c in tree.root.children if c.agent_role == "plan_synthesizer")
    facilitator_children = [
        c for c in synth.children if c.agent_role == "researcher_facilitator"
    ]
    assert len(facilitator_children) == 3
    # Run #23 only has one bull + bear + facilitator row, so two of the three
    # facilitator subtrees will be "skipped" — that's fine and expected for
    # legacy data.

    # Risk facilitator must have three risk-officer children, perspectives
    # stamped in aggressive/neutral/conservative order.
    rf = next(c for c in tree.root.children if c.agent_role == "risk_facilitator")
    assert [c.perspective for c in rf.children] == [
        "aggressive", "neutral", "conservative",
    ]

    # Dedup summary — at least the FM ran, and there should be more
    # "agents_ok" than the 18 raw reports because analysts appear under
    # multiple parents BUT the dedup walker should keep the unique count
    # to roughly the topology size (FM + synth + risk_fac + 3 risk officers
    # + 3 fac + 1 bull + 1 bear + 10 analysts = ~21). Two of the three
    # facilitator subtrees have skipped bull/bear; the analyst nodes are
    # shared so they only count once.
    summary = tree.status_summary
    assert summary["agents_ok"] >= 1
    # Sanity: dedup actually happened — without it the count would balloon
    # past 50.
    assert summary["agents_ok"] + summary["agents_failed"] < 50


# ---------------------------------------------------------------------------
# 2) Synthetic in-memory run.
# ---------------------------------------------------------------------------


def test_build_agent_tree_happy_path_with_adapters(inmem_session) -> None:
    outcomes = [
        {"adapter_name": "finnhub_news", "target": "NVDA", "status": "ok",
         "latency_ms": 120, "payload_size_bytes": 9001,
         "http_status_code": 200, "error_text": None},
        {"adapter_name": "yfinance", "target": "NVDA", "status": "ok",
         "latency_ms": 80, "payload_size_bytes": 500,
         "http_status_code": None, "error_text": None},
        {"adapter_name": "sec_13f", "target": "NVDA", "status": "http_error",
         "latency_ms": 1500, "payload_size_bytes": 0,
         "http_status_code": 404, "error_text": "Not Found"},
    ]
    rid = _seed_synthesis_run(
        inmem_session, adapter_outcomes=outcomes,
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    # Topology sanity.
    assert tree.root.agent_role == "fund_manager"
    assert tree.decision_kind == "plan_revision"
    fm_children_roles = [c.agent_role for c in tree.root.children]
    assert fm_children_roles == [
        "plan_synthesizer",
        "risk_facilitator",
        "plan_critique",
        "codex_second_opinion",
        "whole_artifact_reader",
    ]

    # Three researcher_facilitator subtrees under the synth.
    synth = tree.root.children[0]
    rf_subtrees = [c for c in synth.children
                   if c.agent_role == "researcher_facilitator"]
    assert len(rf_subtrees) == 3
    for rf in rf_subtrees:
        kids = [c.agent_role for c in rf.children]
        assert kids == ["bull_researcher", "bear_researcher"]

    # The synth also has the 10 analysts as direct children (after the
    # facilitators).
    analyst_children = [
        c for c in synth.children if c.agent_role not in {
            "researcher_facilitator",
        }
    ]
    analyst_roles = {c.agent_role for c in analyst_children}
    assert analyst_roles == {
        "concentration", "fx", "fundamentals", "news", "sentiment",
        "technical", "macro", "tax", "household_budget", "plan_critique",
    }

    # The "news" analyst should have the finnhub_news adapter attached.
    news = next(c for c in analyst_children if c.agent_role == "news")
    assert [a.adapter_name for a in news.adapters] == ["finnhub_news"]
    # The "technical" analyst should have yfinance attached.
    tech = next(c for c in analyst_children if c.agent_role == "technical")
    assert [a.adapter_name for a in tech.adapters] == ["yfinance"]
    # "sec_13f" doesn't map to any role in the current table — it stays
    # only in the run-level summary (failed).
    for c in analyst_children:
        assert "sec_13f" not in [a.adapter_name for a in c.adapters]

    # DAG sharing: bull's analyst children must be the SAME Python objects
    # as the synth's analyst children (so the UI can recognise a shared
    # node).
    bull = rf_subtrees[0].children[0]
    assert bull.agent_role == "bull_researcher"
    assert any(c is news for c in bull.children)

    # Risk officers — exactly three, perspectives stamped left to right.
    rf_node = tree.root.children[1]
    assert [c.perspective for c in rf_node.children] == [
        "aggressive", "neutral", "conservative",
    ]
    # All three officers were seeded -> all "ok".
    assert {c.status for c in rf_node.children} == {"ok"}

    # Risk facilitator had confidence=LOW -> degraded.
    assert rf_node.status == "degraded"

    # Status summary: dedup count must equal the count of unique node
    # identities. Topology = FM (1) + synth (1) + risk_fac (1) + 3 risk
    # officers (3) + 3 researcher_facilitators (3) + 3 bulls (3) + 3 bears
    # (3) + 10 analyst leaves (10, shared) + codex_second_opinion (1,
    # skipped since not seeded) + whole_artifact_reader (1, skipped since
    # not seeded) = 27 unique. Two of those 27 are
    # degraded/ok (synth + plan_synthesizer's None confidence) — both
    # count as "ok" in the summary; codex's + reader's "skipped" count as
    # "agents_skipped" (split out from "agents_failed" so the user-facing
    # banner doesn't conflate didn't-run with errored-out).
    summary = tree.status_summary
    assert (
        summary["agents_ok"]
        + summary["agents_failed"]
        + summary["agents_skipped"]
        == 27
    )
    # Skipped nodes in this seed: only one bull + one bear + one
    # researcher_facilitator row are inserted, but the builder expects
    # three facilitator subtrees (long/medium/short). So 2 facilitators +
    # 2 bulls + 2 bears render as "skipped", plus codex_second_opinion
    # (no codex row seeded) + whole_artifact_reader (no reader row seeded)
    # = 8 unique skipped nodes. None of these
    # should bleed into "agents_failed" — that's the whole point of
    # splitting skipped vs failed.
    assert summary["agents_skipped"] == 8
    # No agent_reports row in this seed has a confidence that maps to
    # "failed", so the failed count is exactly 0.
    assert summary["agents_failed"] == 0
    # Two adapters reported ok, one http_error.
    assert summary["adapters_ok"] == 2
    assert summary["adapters_failed"] == 1


def test_adapter_unavailable_classification() -> None:
    """Auth/tier blocks, Cloudflare challenges, and structural no-coverage
    are classified 'unavailable' (not a failure); transient errors, plain
    404s, and config bugs ('series does not exist') stay 'failed'."""
    from argosy.services.agent_tree_builder import (
        AdapterNode,
        _adapter_is_unavailable,
        _summarize,
    )

    def node(name, status, code=None, err=None):
        return AdapterNode(
            adapter_name=name, target="X", status=status, latency_ms=0,
            payload_size_bytes=0, http_status_code=code, error_text=err,
        )

    # Unavailable (known, non-actionable):
    assert _adapter_is_unavailable(node("tipranks", "http_error", 403,
        "<!DOCTYPE html>Just a moment</html>"))
    assert _adapter_is_unavailable(node("finnhub_social", "exception", None,
        "FinnhubAPIException(status_code: 403):"))
    assert _adapter_is_unavailable(node("finnhub_financials", "exception", None,
        "MissingDataSourceError: finnhub: empty metrics for CSPX"))
    assert _adapter_is_unavailable(node("yfinance", "exception", None,
        "MissingDataSourceError: yfinance returned no history for ACWD"))

    # Real failures (actionable) — must NOT be reclassified:
    assert not _adapter_is_unavailable(node("sec_13f", "http_error", 404,
        "Not Found"))
    assert not _adapter_is_unavailable(node("fred", "exception", None,
        "ValueError: Bad Request. The series does not exist."))
    assert not _adapter_is_unavailable(node("x", "http_error", 503,
        "Service Unavailable"))
    assert not _adapter_is_unavailable(node("ok_one", "ok"))

    # _summarize splits the buckets and keeps the real failure visible.
    root = AgentNode(
        agent_role="fund_manager", agent_report_id=1, status="ok",
        confidence=None, model=None, tokens_in=0, tokens_out=0,
        cost_usd=0.0, side=None, perspective=None, response_excerpt="",
        failure_reason=None, children=[],
    )
    outcomes = [
        node("ok_one", "ok"),
        node("tipranks", "http_error", 403, "Just a moment"),
        node("finnhub_social", "exception", None, "(status_code: 403)"),
        node("yf", "exception", None, "MissingDataSourceError: no history"),
        node("fred", "exception", None, "ValueError: series does not exist"),
    ]
    s = _summarize(root, outcomes)
    assert s["adapters_ok"] == 1
    assert s["adapters_unavailable"] == 3
    assert s["adapters_failed"] == 1  # only the fred config bug


def test_build_agent_tree_skipped_nodes_for_old_run(inmem_session) -> None:
    """When researcher rows are missing, the facilitator subtree's bull
    and bear come back as ``skipped`` and propagate ``failure_reason``."""
    rid = _seed_synthesis_run(
        inmem_session,
        adapter_outcomes=None,
        include_plan_critique=False,
        risk_officer_count=0,
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    # plan_critique was not seeded -> the FM's third child is a "skipped"
    # placeholder.
    plan_critique = tree.root.children[2]
    assert plan_critique.agent_role == "plan_critique"
    assert plan_critique.status == "skipped"
    assert plan_critique.failure_reason == "agent did not run"
    assert plan_critique.agent_report_id is None

    # codex_second_opinion was not seeded -> FM's fourth child is the
    # codex placeholder with its directed failure_reason.
    codex_node = tree.root.children[3]
    assert codex_node.agent_role == "codex_second_opinion"
    assert codex_node.status == "skipped"
    assert codex_node.failure_reason == "codex zigzag not run for this synthesis"
    assert codex_node.agent_report_id is None

    # All three risk officers were skipped, but their perspectives still
    # render so the UI can show empty slots.
    rf = tree.root.children[1]
    perspectives = [c.perspective for c in rf.children]
    assert perspectives == ["aggressive", "neutral", "conservative"]
    assert all(c.status == "skipped" for c in rf.children)

    # No adapter_outcomes -> both adapter counts are zero.
    summary = tree.status_summary
    assert summary["adapters_ok"] == 0
    assert summary["adapters_failed"] == 0


def test_build_agent_tree_returns_root_none_for_non_synthesis_kind(
    inmem_session,
) -> None:
    """T4.4: non-synthesis kinds (delta_pushback, daily_brief,
    trade_proposal, plan_amendment_chat) used to raise ValueError; now
    the builder returns root=None + unsupported_reason so the /decisions/{id}
    route can serve 200 with an explanatory payload.
    """
    rid = _seed_synthesis_run(
        inmem_session, decision_kind="trade_proposal",
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    assert tree.root is None
    assert tree.decision_kind == "trade_proposal"
    assert tree.unsupported_reason is not None
    assert "trade_proposal" in tree.unsupported_reason
    # status_summary is still meaningful — analyst rows we seeded count.
    summary = tree.status_summary
    assert summary["agents_ok"] + summary["agents_failed"] > 0
    assert summary["adapters_ok"] == 0
    assert summary["adapters_failed"] == 0


def test_build_agent_tree_returns_root_none_for_delta_pushback(
    inmem_session,
) -> None:
    """T4.4: delta_pushback kind (new in T4.3, ships later) is handled
    gracefully even though the synthesis pipeline doesn't produce one yet."""
    rid = _seed_synthesis_run(
        inmem_session, decision_kind="delta_pushback",
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    assert tree.root is None
    assert tree.decision_kind == "delta_pushback"
    assert tree.unsupported_reason is not None


def test_build_agent_tree_returns_root_none_for_daily_brief(
    inmem_session,
) -> None:
    """T4.4: daily_brief kind (new in T4.5, ships later) is handled gracefully."""
    rid = _seed_synthesis_run(
        inmem_session, decision_kind="daily_brief",
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    assert tree.root is None
    assert tree.decision_kind == "daily_brief"
    assert tree.unsupported_reason is not None


def test_build_agent_tree_rejects_missing_run(inmem_session) -> None:
    with pytest.raises(ValueError, match="not found"):
        build_agent_tree(inmem_session, decision_run_id=999_999)


def test_build_agent_tree_tolerates_malformed_phase_output(inmem_session) -> None:
    """If ``phase_output_json`` isn't valid JSON or doesn't carry
    ``adapter_outcomes``, the builder must still produce a tree with an
    empty adapter list, not crash."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)

    # Overwrite phase_output_json with garbage.
    phase = inmem_session.query(DecisionPhase).filter_by(
        decision_run_id=rid, seq=1
    ).first()
    phase.phase_output_json = "{not valid json"
    inmem_session.commit()

    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    assert tree.root.agent_role == "fund_manager"
    assert tree.status_summary["adapters_ok"] == 0
    assert tree.status_summary["adapters_failed"] == 0


def test_build_agent_tree_dag_dedup_is_correct(inmem_session) -> None:
    """The same analyst node is reachable through (bull, bear, synth) on
    each of the three facilitator subtrees plus directly through synth.
    The summary walker must visit each analyst exactly once."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    # Walk and count every (id(node)) reachable from root.
    counts: dict[int, int] = {}

    def walk(n: AgentNode) -> None:
        counts[id(n)] = counts.get(id(n), 0) + 1
        for c in n.children:
            walk(c)
    walk(tree.root)

    # Any analyst node should be reached via:
    #   - synth direct child         (+1)
    #   - 3 bull subtrees × child    (+3)
    #   - 3 bear subtrees × child    (+3)
    # = 7 raw visits. plan_critique is additionally a child of the FM
    # itself, so its raw visit count is 8.
    synth = tree.root.children[0]
    news = next(c for c in synth.children if c.agent_role == "news")
    assert counts[id(news)] == 7

    plan_critique = next(
        c for c in synth.children if c.agent_role == "plan_critique"
    )
    # plan_critique also reachable from FM directly.
    assert counts[id(plan_critique)] == 8

    # ...but the deduped summary count includes plan_critique just once.
    # Unique node identities = 27 (see happy path test) — 25 plus the
    # codex_second_opinion node + whole_artifact_reader node (both skipped
    # since the seed doesn't include their agent_reports rows). "skipped"
    # is now tracked separately from "failed", so the total is
    # ok + failed + skipped.
    assert (
        tree.status_summary["agents_ok"]
        + tree.status_summary["agents_failed"]
        + tree.status_summary["agents_skipped"]
        == 27
    )


# ---------------------------------------------------------------------------
# Codex ZigZag — second-opinion node under FM (shipped after commit 0bedd9b).
# ---------------------------------------------------------------------------


def _seed_codex_row(
    sess,
    *,
    decision_run_id: int,
    user_id: str = "ariel",
    response_text: str | None = None,
) -> AgentReport:
    """Append a codex_second_opinion agent_reports row to an existing run."""
    decision_id_str = f"plan-synth-{decision_run_id}"
    if response_text is None:
        response_text = json.dumps({
            "overall_assessment": "APPROVE_WITH_CONDITIONS",
            "findings": [
                {
                    "severity": "AMBER",
                    "topic": "Concentration drift",
                    "detail": "NVDA exceeds 30% of portfolio NAV.",
                    "suggested_fix": "Trim 5% over the next 30 days.",
                    "cited_synthesizer_paragraphs": ["..."],
                },
                {
                    "severity": "YELLOW",
                    "topic": "Tax treatment ambiguity",
                    "detail": "Schwab RSU vest cost basis is unconfirmed.",
                    "suggested_fix": "Reconcile against last 1099-B.",
                    "cited_synthesizer_paragraphs": [],
                },
            ],
            "agreement_with_argosy": {
                "agrees_with_risk_verdict": "partial",
                "novel_concerns_argosy_missed": [
                    "FX-hedged class is undersized vs declared target.",
                ],
            },
            "user_directive_respected": True,
        })
    ar = AgentReport(
        user_id=user_id,
        agent_role="codex_second_opinion",
        decision_id=decision_id_str,
        response_text=response_text,
        confidence=None,
        model="gpt-5-codex",
        tokens_in=0,
        tokens_out=4321,
        cost_usd=0.0,
    )
    sess.add(ar)
    sess.commit()
    sess.refresh(ar)
    return ar


def test_codex_second_opinion_node_present_when_row_exists(
    inmem_session,
) -> None:
    """When a codex_second_opinion agent_reports row exists for a synth
    run, the FM-rooted tree carries a fourth child node whose parsed
    findings + assessment are surfaced for the UI to render."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    _seed_codex_row(inmem_session, decision_run_id=rid)

    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    fm_children_roles = [c.agent_role for c in tree.root.children]
    assert fm_children_roles == [
        "plan_synthesizer",
        "risk_facilitator",
        "plan_critique",
        "codex_second_opinion",
        "whole_artifact_reader",
    ]
    codex = tree.root.children[3]
    assert codex.status == "ok"
    # APPROVE_WITH_CONDITIONS -> MEDIUM band.
    assert codex.confidence == "MEDIUM"
    assert codex.model == "gpt-5-codex"
    assert codex.agent_report_id is not None
    # Parsed findings are surfaced as a typed list on the node.
    assert len(codex.codex_findings) == 2
    severities = [f.severity for f in codex.codex_findings]
    assert severities == ["AMBER", "YELLOW"]
    topics = [f.topic for f in codex.codex_findings]
    assert "Concentration drift" in topics
    # The excerpt summarises the verdict for the always-visible row.
    assert "APPROVE_WITH_CONDITIONS" in codex.response_excerpt
    assert "2 findings" in codex.response_excerpt
    assert "agrees_with_risk=partial" in codex.response_excerpt
    assert "1 novel concerns" in codex.response_excerpt
    # Codex must NOT have any children/adapters of its own — it's a
    # cross-engine leaf.
    assert codex.children == []
    assert codex.adapters == []


def test_codex_second_opinion_node_skipped_when_no_row(inmem_session) -> None:
    """Older synth runs (pre-commit 0bedd9b) have no codex_second_opinion
    agent_reports row. The tree must still render the codex slot with a
    ``skipped`` status and a directed failure_reason (not the generic
    ``agent did not run`` message used by other roles) so the UI can
    explain why the codex panel is empty."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    codex = tree.root.children[3]
    assert codex.agent_role == "codex_second_opinion"
    assert codex.status == "skipped"
    assert codex.failure_reason == "codex zigzag not run for this synthesis"
    assert codex.agent_report_id is None
    assert codex.codex_findings == []
    # Confidence/model are None for a skipped codex (no row to read from).
    assert codex.confidence is None
    assert codex.model is None


def test_codex_second_opinion_node_unparseable_falls_back_gracefully(
    inmem_session,
) -> None:
    """A codex row whose response_text isn't valid JSON should NOT crash
    the builder. The node renders with ``status='degraded'`` and the raw
    text surfaces in ``response_excerpt`` so the operator can manually
    review."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    _seed_codex_row(
        inmem_session,
        decision_run_id=rid,
        response_text=(
            "I cannot return JSON today. The market is closed and "
            "compliance flagged my prompt as ambiguous."
        ),
    )

    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    codex = tree.root.children[3]
    assert codex.agent_role == "codex_second_opinion"
    assert codex.status == "degraded"
    assert codex.agent_report_id is not None
    # No findings could be extracted from the garbage payload.
    assert codex.codex_findings == []
    # The raw text is preserved verbatim (truncated to 500 chars) so the
    # operator can read what codex actually said.
    assert "I cannot return JSON today" in codex.response_excerpt
    assert codex.failure_reason is not None
    assert "unparseable" in codex.failure_reason


# ---------------------------------------------------------------------------
# Cost breakdown — per-run aggregation surfaced under the agent tree.
# ---------------------------------------------------------------------------


def _seed_cost_run(
    sess,
    *,
    user_id: str = "ariel",
    rows: list[tuple[str, str, float | None]],
) -> int:
    """Create a synthesis decision_run with one row per ``(role, phase_kind, cost)``.

    The phase_kind drives which DecisionPhase the row's ``phase_id``
    points at. Empty/None phase_kind means "no phase_id" — the
    aggregator must fall back to the role heuristic.
    """
    now = datetime.now(timezone.utc)
    run = DecisionRun(
        user_id=user_id,
        ticker="(plan)",
        tier=None,
        decision_kind="plan_revision",
        status="completed",
        started_at=now,
        finished_at=now,
    )
    sess.add(run)
    sess.commit()
    sess.refresh(run)
    rid = run.id

    # Materialise the distinct phase kinds the seed needs.
    phase_kinds_used = sorted({pk for _, pk, _ in rows if pk})
    phase_ids: dict[str, int] = {}
    for i, pk in enumerate(phase_kinds_used, start=1):
        ph = DecisionPhase(
            decision_run_id=rid,
            user_id=user_id,
            seq=i,
            kind=pk,
            started_at=now,
            finished_at=now,
            participants_json="[]",
            phase_output_json=json.dumps({"phase": i}),
        )
        sess.add(ph)
    sess.commit()
    for pk in phase_kinds_used:
        ph = (
            sess.query(DecisionPhase)
            .filter_by(decision_run_id=rid, kind=pk)
            .first()
        )
        phase_ids[pk] = ph.id

    decision_id_str = f"plan-synth-{rid}"
    for role, pk, cost in rows:
        ar = AgentReport(
            user_id=user_id,
            agent_role=role,
            decision_id=decision_id_str,
            response_text="ok",
            confidence="MEDIUM",
            model="claude-sonnet-4-6",
            tokens_in=10,
            tokens_out=20,
            cost_usd=cost if cost is not None else 0,
            phase_id=phase_ids.get(pk) if pk else None,
        )
        sess.add(ar)
    sess.commit()
    return rid


def test_cost_breakdown_aggregates_correctly(inmem_session) -> None:
    """Seed 5 agent_reports with known costs across 3 phases (+ codex
    half-step) and assert ``by_phase`` + ``by_role`` + ``top_3_agents``
    + ``cost_per_phase_table`` come back right.

    Costs (USD):
      news                phase_1                $0.10
      bull_researcher     phase_2                $0.20
      bear_researcher     phase_2                $0.30
      plan_synthesizer    phase_3                $1.50  <- top
      codex_second_opinion phase_4_5_codex       $0.40
    """
    rid = _seed_cost_run(
        inmem_session,
        rows=[
            ("news", "synthesis.phase_1", 0.10),
            ("bull_researcher", "synthesis.phase_2", 0.20),
            ("bear_researcher", "synthesis.phase_2", 0.30),
            ("plan_synthesizer", "synthesis.phase_3", 1.50),
            ("codex_second_opinion", "synthesis.phase_45", 0.40),
        ],
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    cb = tree.cost_breakdown
    assert isinstance(cb, CostBreakdown)

    # Total
    assert cb.total_usd == pytest.approx(2.50)
    assert cb.agent_count == 5

    # By phase — only the slots we populated have non-zero cost; the
    # untouched slots (phase_4, phase_5) come back as 0.0 (fixed-key
    # vocabulary).
    assert set(cb.by_phase.keys()) == set(COST_PHASE_KEYS)
    assert cb.by_phase["phase_1"] == pytest.approx(0.10)
    assert cb.by_phase["phase_2"] == pytest.approx(0.50)
    assert cb.by_phase["phase_3"] == pytest.approx(1.50)
    assert cb.by_phase["phase_4_5_codex"] == pytest.approx(0.40)
    assert cb.by_phase["phase_4"] == 0.0
    assert cb.by_phase["phase_5"] == 0.0

    # By role — every role in the seed appears with its exact spend.
    assert cb.by_role["news"] == pytest.approx(0.10)
    assert cb.by_role["bull_researcher"] == pytest.approx(0.20)
    assert cb.by_role["bear_researcher"] == pytest.approx(0.30)
    assert cb.by_role["plan_synthesizer"] == pytest.approx(1.50)
    assert cb.by_role["codex_second_opinion"] == pytest.approx(0.40)

    # Top 3 — synth (1.50), codex (0.40), bear (0.30).
    roles_in_top = [r for r, _ in cb.top_3_agents]
    assert roles_in_top == ["plan_synthesizer", "codex_second_opinion", "bear_researcher"]
    # Costs match the per-role sums.
    assert dict(cb.top_3_agents)["plan_synthesizer"] == pytest.approx(1.50)

    # cost_per_phase_table mirrors by_phase + adds agent counts.
    table_by_phase = {r["phase"]: r for r in cb.cost_per_phase_table}
    assert table_by_phase["phase_1"]["agent_count"] == 1
    assert table_by_phase["phase_2"]["agent_count"] == 2
    assert table_by_phase["phase_3"]["agent_count"] == 1
    assert table_by_phase["phase_4_5_codex"]["agent_count"] == 1
    assert table_by_phase["phase_4"]["agent_count"] == 0
    assert table_by_phase["phase_5"]["agent_count"] == 0
    # Stable phase order in the table (UI rendering depends on it).
    assert [r["phase"] for r in cb.cost_per_phase_table] == list(COST_PHASE_KEYS)


def test_cost_breakdown_role_fallback_when_phase_id_missing(
    inmem_session,
) -> None:
    """Rows without a phase_id linkage still land in the right phase
    bucket via the role-based fallback. Mirrors pre-0020 legacy runs."""
    rid = _seed_cost_run(
        inmem_session,
        rows=[
            ("fund_manager", "", 0.50),       # no phase_id -> role fallback -> phase_5
            ("plan_synthesizer", "", 1.00),   # no phase_id -> role fallback -> phase_3
            ("news", "", 0.05),               # no phase_id -> role fallback -> phase_1
        ],
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    cb = tree.cost_breakdown
    assert cb.total_usd == pytest.approx(1.55)
    assert cb.by_phase["phase_1"] == pytest.approx(0.05)
    assert cb.by_phase["phase_3"] == pytest.approx(1.00)
    assert cb.by_phase["phase_5"] == pytest.approx(0.50)


def test_cost_breakdown_null_cost_treated_as_zero(inmem_session) -> None:
    """Legacy rows with NULL ``cost_usd`` (pre-cost-capture, or a future
    migration that relaxes the NOT NULL constraint) must sum as 0
    rather than crash. We exercise the branch by calling the private
    aggregator directly with a synthetic AgentReport whose ``cost_usd``
    is set to None — the production column is currently NOT NULL so we
    can't insert one via the ORM, but the float-coercion path needs to
    stay defensive for the SQLAlchemy edge case (e.g. when the row was
    materialised from a JOIN that returned None)."""
    from argosy.services.agent_tree_builder import _compute_cost_breakdown

    fake_rows = [
        AgentReport(
            user_id="ariel",
            agent_role="news",
            decision_id="plan-synth-1",
            response_text="",
            cost_usd=None,  # type: ignore[arg-type]
            phase_id=None,
        ),
        AgentReport(
            user_id="ariel",
            agent_role="plan_synthesizer",
            decision_id="plan-synth-1",
            response_text="",
            cost_usd=1.00,
            phase_id=None,
        ),
    ]
    cb = _compute_cost_breakdown(reports=fake_rows, phases=[])
    assert cb.total_usd == pytest.approx(1.00)
    assert cb.agent_count == 2
    # The NULL row falls under phase_1 (role fallback) with 0 cost; the
    # synth row falls under phase_3 with $1.
    assert cb.by_phase["phase_1"] == 0.0
    assert cb.by_phase["phase_3"] == pytest.approx(1.00)
    # The NULL-cost row still surfaces in by_role with $0 so the user
    # can see that the agent ran.
    assert cb.by_role["news"] == 0.0
    assert cb.by_role["plan_synthesizer"] == pytest.approx(1.00)


def test_cost_breakdown_empty_when_no_reports(inmem_session) -> None:
    """A run with zero agent_reports still produces a valid CostBreakdown
    DTO — empty histogram, zero total. The UI hides the card via the
    ``agent_count > 0`` guard, but the field must be present."""
    now = datetime.now(timezone.utc)
    run = DecisionRun(
        user_id="ariel",
        ticker="(plan)",
        tier=None,
        decision_kind="plan_revision",
        status="completed",
        started_at=now,
        finished_at=now,
    )
    inmem_session.add(run)
    inmem_session.commit()
    inmem_session.refresh(run)

    tree = build_agent_tree(inmem_session, decision_run_id=run.id)
    cb = tree.cost_breakdown
    assert cb.total_usd == 0.0
    assert cb.agent_count == 0
    assert cb.top_3_agents == []
    assert all(v == 0.0 for v in cb.by_phase.values())


# ---------------------------------------------------------------------------
# Whole-artifact reader (phase 5.5) — mirrors the codex_second_opinion wiring.
# ---------------------------------------------------------------------------


def _seed_reader_row(
    sess,
    *,
    decision_run_id: int,
    user_id: str = "ariel",
    response_text: str | None = None,
    cost_usd: float = 0.0,
    with_phase: bool = False,
) -> AgentReport:
    """Append a whole_artifact_reader agent_reports row to an existing run.

    When ``with_phase`` is set, a ``synthesis.phase_55`` DecisionPhase is
    created (mirroring the orchestrator's ``phase_n=55`` recorder) and the
    row is back-linked via ``phase_id`` so the kind-based cost mapping path
    is exercised; otherwise the row has no ``phase_id`` and the role-based
    fallback path is exercised.
    """
    decision_id_str = f"plan-synth-{decision_run_id}"
    if response_text is None:
        response_text = json.dumps({
            "overall_assessment": "BLOCK",
            "findings": [
                {
                    "kind": "contradiction",
                    "severity": "BLOCKER",
                    "detail": "Policy says FI 8-10% but the target shows 21%.",
                    "surfaces_cited": [
                        "Policy: FI 8-10%",
                        "Target: FI 21%",
                    ],
                },
                {
                    "kind": "stale",
                    "severity": "AMBER",
                    "detail": "Retirement date references a prior plan year.",
                    "surfaces_cited": [],
                },
            ],
        })
    phase_id = None
    if with_phase:
        now = datetime.now(timezone.utc)
        ph = DecisionPhase(
            decision_run_id=decision_run_id,
            user_id=user_id,
            seq=99,
            kind="synthesis.phase_55",
            started_at=now,
            finished_at=now,
            participants_json="[]",
            phase_output_json=json.dumps({"phase": 55}),
        )
        sess.add(ph)
        sess.commit()
        sess.refresh(ph)
        phase_id = ph.id
    ar = AgentReport(
        user_id=user_id,
        agent_role="whole_artifact_reader",
        decision_id=decision_id_str,
        response_text=response_text,
        confidence=None,
        model="gpt-5-codex",
        tokens_in=0,
        tokens_out=2222,
        cost_usd=cost_usd,
        phase_id=phase_id,
    )
    sess.add(ar)
    sess.commit()
    sess.refresh(ar)
    return ar


def test_phase_55_kind_maps_to_reader_cost_key(inmem_session) -> None:
    """A row stamped with the reader phase (``synthesis.phase_55``, written
    by the orchestrator's ``phase_n=55`` recorder) lands in the
    ``phase_5_5_reader`` cost bucket — mirroring codex's
    ``synthesis.phase_45`` -> ``phase_4_5_codex`` mapping."""
    from argosy.services.agent_tree_builder import _phase_kind_to_cost_key

    assert _phase_kind_to_cost_key("synthesis.phase_55") == "phase_5_5_reader"

    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    _seed_reader_row(
        inmem_session, decision_run_id=rid, cost_usd=0.30, with_phase=True
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    cb = tree.cost_breakdown
    assert "phase_5_5_reader" in cb.by_phase
    assert cb.by_phase["phase_5_5_reader"] == pytest.approx(0.30)


def test_whole_artifact_reader_role_fallback_to_reader_phase(
    inmem_session,
) -> None:
    """A reader row with no ``phase_id`` (legacy / fail-soft recorder path)
    still lands in ``phase_5_5_reader`` via the role-based fallback."""
    from argosy.services.agent_tree_builder import _ROLE_TO_PHASE_FALLBACK

    assert (
        _ROLE_TO_PHASE_FALLBACK["whole_artifact_reader"] == "phase_5_5_reader"
    )

    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    _seed_reader_row(
        inmem_session, decision_run_id=rid, cost_usd=0.20, with_phase=False
    )
    tree = build_agent_tree(inmem_session, decision_run_id=rid)
    cb = tree.cost_breakdown
    assert cb.by_phase["phase_5_5_reader"] == pytest.approx(0.20)


def test_reader_cost_phase_key_present_in_vocabulary() -> None:
    """The reader phase is part of the fixed cost-phase vocabulary so the
    UI renders a deterministic table slot (after phase_5)."""
    assert "phase_5_5_reader" in COST_PHASE_KEYS
    assert COST_PHASE_KEYS.index("phase_5_5_reader") > COST_PHASE_KEYS.index(
        "phase_5"
    )


def test_whole_artifact_reader_node_present_when_row_exists(
    inmem_session,
) -> None:
    """When a whole_artifact_reader row exists, the FM-rooted tree carries a
    reader node whose parsed overall_assessment + coherence findings are
    surfaced for the UI to render (mirrors codex_second_opinion)."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    _seed_reader_row(inmem_session, decision_run_id=rid)

    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    fm_children_roles = [c.agent_role for c in tree.root.children]
    assert "whole_artifact_reader" in fm_children_roles
    reader = next(
        c for c in tree.root.children
        if c.agent_role == "whole_artifact_reader"
    )
    assert reader.status == "ok"
    # BLOCK -> LOW confidence band.
    assert reader.confidence == "LOW"
    assert reader.agent_report_id is not None
    assert len(reader.coherence_findings) == 2
    severities = [f.severity for f in reader.coherence_findings]
    assert severities == ["BLOCKER", "AMBER"]
    kinds = [f.kind for f in reader.coherence_findings]
    assert "contradiction" in kinds
    # surfaces_cited carried through.
    assert reader.coherence_findings[0].surfaces_cited == [
        "Policy: FI 8-10%",
        "Target: FI 21%",
    ]
    assert "BLOCK" in reader.response_excerpt
    # Leaf node — no children/adapters of its own.
    assert reader.children == []
    assert reader.adapters == []


def test_whole_artifact_reader_node_skipped_when_no_row(
    inmem_session,
) -> None:
    """A synth run with no reader row still renders the reader slot with a
    ``skipped`` status and a directed failure_reason."""
    rid = _seed_synthesis_run(inmem_session, adapter_outcomes=None)
    tree = build_agent_tree(inmem_session, decision_run_id=rid)

    reader = next(
        c for c in tree.root.children
        if c.agent_role == "whole_artifact_reader"
    )
    assert reader.status == "skipped"
    assert reader.agent_report_id is None
    assert reader.coherence_findings == []
