"""Tests for the fleet self-review detector + runner pack.

Coverage strategy:

  * One positive + one negative test per detector (D1..D10) — synthetic
    DB rows that should trip the detector, and a clean baseline that
    shouldn't.
  * One end-to-end test that exercises the runner: build a tiny DB,
    call ``generate_fleet_self_review``, assert the row landed in
    ``fleet_self_review_reports`` with non-trivial markdown + the
    expected severity counts.
  * One robustness test that proves a detector that raises does NOT
    crash the overall report.

Test DB setup uses an in-tmp_path file-backed SQLite — same shape as
the rest of the test suite (see ``tests/conftest.py``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services import fleet_self_review as fsr
from argosy.services.fleet_self_review import (
    Finding,
    ReviewScope,
    detect_adapter_outcome_failure_swallowed,
    detect_agent_response_truncated,
    detect_analyst_cites_unknown_source,
    detect_codex_disagreed_with_synthesizer,
    detect_consecutive_fm_rejections_same_theme,
    detect_cost_outlier_per_role,
    detect_decision_run_stuck,
    detect_empty_payload_but_confident_output,
    detect_guidance_pipeline_no_op,
    detect_objection_topic_recurrence,
    detect_phase_participants_empty,
    run_all_detectors,
)
from argosy.services.fleet_self_review_runner import (
    compose_markdown,
    generate_fleet_self_review,
)
from argosy.state.models import (
    AgentReport,
    Base,
    DecisionPhase,
    DecisionRun,
    FleetSelfReviewReport,
    User,
)


USER = "ariel"


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB."""
    db_path = tmp_path / "fsr.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _make_run(
    db, *, id_: int, started_offset_min: int = 30, status: str = "completed",
    fund_manager_decision: str | None = "rejected",
    decision_kind: str = "plan_revision",
) -> DecisionRun:
    """Helper: insert a DecisionRun row with predictable timestamps."""
    started = datetime.now(timezone.utc) - timedelta(minutes=started_offset_min)
    finished = (
        None if status == "running"
        else started + timedelta(minutes=20)
    )
    row = DecisionRun(
        id=id_,
        user_id=USER,
        ticker="(plan)",
        tier="T3",
        decision_kind=decision_kind,
        started_at=started,
        finished_at=finished,
        status=status,
        fund_manager_decision=fund_manager_decision,
    )
    db.add(row)
    db.commit()
    return row


def _make_fm_report(db, decision_run_id: int, reasons: list[str]) -> AgentReport:
    payload = json.dumps({"approved": False, "reasons": reasons})
    row = AgentReport(
        user_id=USER,
        agent_role="fund_manager",
        decision_id=f"plan-synth-{decision_run_id}",
        response_text=payload,
        tokens_in=100, tokens_out=100, cost_usd=0.01, model="opus",
    )
    db.add(row)
    db.commit()
    return row


# ----------------------------------------------------------------------
# D1 — guidance_pipeline_no_op
# ----------------------------------------------------------------------


def test_d1_positive_unused_param(tmp_path, sync_session):
    """A phase function declares ``guidance`` but never references it."""
    bad = tmp_path / "bad_orchestrator.py"
    bad.write_text(
        "def _run_phase_3_synthesizer(*, session, user_id, guidance):\n"
        "    return 'output without using guidance'\n",
        encoding="utf-8",
    )
    scope = ReviewScope(user_id=USER, orchestrator_path=bad)
    findings = detect_guidance_pipeline_no_op(sync_session, scope)
    assert len(findings) == 1, findings
    f = findings[0]
    assert f.detector == "D1"
    assert f.severity == "RED"
    assert "_run_phase_3_synthesizer" in f.title
    assert "guidance" in f.title


def test_d1_negative_param_actually_used(tmp_path, sync_session):
    """Same signature but ``guidance`` IS referenced — no finding."""
    good = tmp_path / "good_orchestrator.py"
    good.write_text(
        "def _run_phase_3_synthesizer(*, session, user_id, guidance):\n"
        "    print(guidance)\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    scope = ReviewScope(user_id=USER, orchestrator_path=good)
    findings = detect_guidance_pipeline_no_op(sync_session, scope)
    assert findings == []


# ----------------------------------------------------------------------
# D2 — consecutive_fm_rejections_same_theme
# ----------------------------------------------------------------------


def test_d2_positive_three_overlapping_rejections(sync_session):
    """3 latest plan_revision runs all rejected with full topic overlap."""
    # Three runs, each FM-rejected with the same topic.
    same_reasons = [
        "Cross-horizon coherence failure NVDA share-count arithmetic.",
        "Section 102 tax sequencing timing risk.",
    ]
    for n in (1, 2, 3):
        _make_run(sync_session, id_=n, started_offset_min=10 * n)
        _make_fm_report(sync_session, n, same_reasons)
    scope = ReviewScope(user_id=USER)
    findings = detect_consecutive_fm_rejections_same_theme(sync_session, scope)
    assert len(findings) == 1
    assert findings[0].detector == "D2"
    assert findings[0].severity == "RED"


def test_d2_negative_distinct_topics(sync_session):
    """Three rejected runs but with totally different topics — no finding."""
    distinct = [
        ["Cash buffer below floor."],
        ["NVDA share-count arithmetic."],
        ["Tax-loss harvesting deferred."],
    ]
    for n in (1, 2, 3):
        _make_run(sync_session, id_=n, started_offset_min=10 * n)
        _make_fm_report(sync_session, n, distinct[n - 1])
    scope = ReviewScope(user_id=USER)
    findings = detect_consecutive_fm_rejections_same_theme(sync_session, scope)
    assert findings == []


# ----------------------------------------------------------------------
# D3 — adapter_outcome_failure_swallowed
# ----------------------------------------------------------------------


def _add_phase_1_with_outcomes(
    db, decision_run_id: int, outcomes: list[dict],
) -> None:
    """Insert a synthesis.phase_1 row carrying the given adapter outcomes."""
    payload = {
        "analyst_reports_text": "(analyst reports)",
        "adapter_outcomes": outcomes,
    }
    row = DecisionPhase(
        decision_run_id=decision_run_id,
        user_id=USER,
        seq=1,
        kind="synthesis.phase_1",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        finished_at=datetime.now(timezone.utc),
        participants_json="[]",
        phase_output_json=json.dumps(payload),
    )
    db.add(row)
    db.commit()


def test_d3_positive_failed_adapter_high_confidence(sync_session):
    """Finnhub failed but news analyst returned HIGH confidence."""
    _make_run(sync_session, id_=1, started_offset_min=5)
    _add_phase_1_with_outcomes(sync_session, 1, [
        {
            "adapter_name": "finnhub", "target": "AAPL",
            "status": "http_error", "http_status_code": 500,
            "error_text": "boom",
        },
    ])
    sync_session.add(AgentReport(
        user_id=USER, agent_role="news", decision_id="plan-synth-1",
        response_text="long content...", tokens_in=10, tokens_out=10,
        cost_usd=0.01, model="opus", confidence="HIGH",
    ))
    sync_session.commit()
    findings = detect_adapter_outcome_failure_swallowed(
        sync_session, ReviewScope(user_id=USER),
    )
    assert any(f.detector == "D3" for f in findings)


def test_d3_negative_analyst_low_when_adapter_fails(sync_session):
    """Same setup but the analyst correctly downgraded to LOW."""
    _make_run(sync_session, id_=1, started_offset_min=5)
    _add_phase_1_with_outcomes(sync_session, 1, [
        {"adapter_name": "finnhub", "target": "AAPL",
         "status": "http_error", "http_status_code": 500},
    ])
    sync_session.add(AgentReport(
        user_id=USER, agent_role="news", decision_id="plan-synth-1",
        response_text="short caveat", tokens_in=10, tokens_out=10,
        cost_usd=0.01, model="opus", confidence="LOW",
    ))
    sync_session.commit()
    findings = detect_adapter_outcome_failure_swallowed(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D4 — analyst_cites_unknown_source
# ----------------------------------------------------------------------


def test_d4_positive_unknown_source_cited(sync_session):
    _make_run(sync_session, id_=1, started_offset_min=5)
    sync_session.add(AgentReport(
        user_id=USER, agent_role="macro", decision_id="plan-synth-1",
        response_text=(
            "Per macro/FRED/DCOILWTICO the WTI is at $80. "
            "Also macro/FRED/SOMETHING_ELSE shows a delta."
        ),
        sources_json=json.dumps([
            {"id": "macro/FRED/DCOILWTICO", "title": "WTI"}
        ]),
        tokens_in=10, tokens_out=10, cost_usd=0.01, model="opus",
        confidence="MEDIUM",
    ))
    sync_session.commit()
    findings = detect_analyst_cites_unknown_source(
        sync_session, ReviewScope(user_id=USER),
    )
    assert len(findings) == 1
    assert "macro/FRED/SOMETHING_ELSE" in (
        findings[0].evidence.get("unknown_sources") or []
    )


def test_d4_negative_all_citations_known(sync_session):
    _make_run(sync_session, id_=1, started_offset_min=5)
    sync_session.add(AgentReport(
        user_id=USER, agent_role="macro", decision_id="plan-synth-1",
        response_text="Per macro/FRED/DCOILWTICO the WTI is at $80.",
        sources_json=json.dumps([{"id": "macro/FRED/DCOILWTICO"}]),
        tokens_in=10, tokens_out=10, cost_usd=0.01, model="opus",
        confidence="MEDIUM",
    ))
    sync_session.commit()
    findings = detect_analyst_cites_unknown_source(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D5 — empty_payload_but_confident_output
# ----------------------------------------------------------------------


def test_d5_positive_empty_adapter_high_confidence_long_response(sync_session):
    _make_run(sync_session, id_=1, started_offset_min=5)
    _add_phase_1_with_outcomes(sync_session, 1, [
        {"adapter_name": "finnhub", "target": "AAPL", "status": "empty"},
        {"adapter_name": "finnhub", "target": "MSFT", "status": "empty"},
    ])
    long_response = "X" * 2500
    sync_session.add(AgentReport(
        user_id=USER, agent_role="news", decision_id="plan-synth-1",
        response_text=long_response, tokens_in=10, tokens_out=10,
        cost_usd=0.01, model="opus", confidence="HIGH",
    ))
    sync_session.commit()
    findings = detect_empty_payload_but_confident_output(
        sync_session, ReviewScope(user_id=USER),
    )
    assert len(findings) == 1
    assert findings[0].detector == "D5"


def test_d5_negative_low_confidence_when_empty(sync_session):
    _make_run(sync_session, id_=1, started_offset_min=5)
    _add_phase_1_with_outcomes(sync_session, 1, [
        {"adapter_name": "finnhub", "target": "AAPL", "status": "empty"},
    ])
    sync_session.add(AgentReport(
        user_id=USER, agent_role="news", decision_id="plan-synth-1",
        response_text="short", tokens_in=10, tokens_out=10,
        cost_usd=0.01, model="opus", confidence="LOW",
    ))
    sync_session.commit()
    findings = detect_empty_payload_but_confident_output(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D6 — cost_outlier_per_role
# ----------------------------------------------------------------------


def test_d6_positive_cost_outlier(sync_session):
    # 5 rows of 'fundamentals' at $0.05 + 1 at $0.50 → 10x median → trips.
    for _ in range(5):
        sync_session.add(AgentReport(
            user_id=USER, agent_role="fundamentals", decision_id="d",
            response_text="x", tokens_in=10, tokens_out=10,
            cost_usd=0.05, model="opus",
        ))
    sync_session.add(AgentReport(
        user_id=USER, agent_role="fundamentals", decision_id="d",
        response_text="x", tokens_in=10, tokens_out=10,
        cost_usd=0.50, model="opus",
    ))
    sync_session.commit()
    findings = detect_cost_outlier_per_role(
        sync_session, ReviewScope(user_id=USER),
    )
    assert any(f.detector == "D6" for f in findings)


def test_d6_negative_no_outlier_in_band(sync_session):
    for cost in (0.05, 0.06, 0.07, 0.08, 0.05, 0.07):
        sync_session.add(AgentReport(
            user_id=USER, agent_role="fundamentals", decision_id="d",
            response_text="x", tokens_in=10, tokens_out=10,
            cost_usd=cost, model="opus",
        ))
    sync_session.commit()
    findings = detect_cost_outlier_per_role(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D7 — decision_run_stuck
# ----------------------------------------------------------------------


def test_d7_positive_old_running_row(sync_session):
    _make_run(
        sync_session, id_=1, started_offset_min=240,
        status="running", fund_manager_decision=None,
    )
    findings = detect_decision_run_stuck(
        sync_session, ReviewScope(user_id=USER),
    )
    assert len(findings) == 1
    assert findings[0].detector == "D7"


def test_d7_negative_recent_running_ok(sync_session):
    _make_run(
        sync_session, id_=1, started_offset_min=10,
        status="running", fund_manager_decision=None,
    )
    findings = detect_decision_run_stuck(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D8 — phase_participants_empty
# ----------------------------------------------------------------------


def test_d8_positive_empty_participants(sync_session):
    _make_run(sync_session, id_=1, started_offset_min=10)
    sync_session.add(DecisionPhase(
        decision_run_id=1, user_id=USER, seq=1,
        kind="synthesis.phase_3",
        started_at=datetime.now(timezone.utc),
        participants_json="[]",
    ))
    sync_session.commit()
    findings = detect_phase_participants_empty(
        sync_session, ReviewScope(user_id=USER),
    )
    assert any(f.detector == "D8" for f in findings)


def test_d8_negative_populated_participants(sync_session):
    _make_run(sync_session, id_=1, started_offset_min=10)
    sync_session.add(DecisionPhase(
        decision_run_id=1, user_id=USER, seq=1,
        kind="synthesis.phase_3",
        started_at=datetime.now(timezone.utc),
        participants_json='[{"agent_role":"news","agent_report_id":1}]',
    ))
    sync_session.commit()
    findings = detect_phase_participants_empty(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D9 — objection_topic_recurrence
# ----------------------------------------------------------------------


def test_d9_positive_recurring_topic(sync_session):
    for n in (1, 2, 3):
        _make_run(sync_session, id_=n, started_offset_min=10 * n)
        _make_fm_report(
            sync_session, n,
            [
                "NVDA share-count arithmetic is wrong again",
                f"Some other concern unique to run {n}",
            ],
        )
    findings = detect_objection_topic_recurrence(
        sync_session, ReviewScope(user_id=USER),
    )
    nvda = [f for f in findings if "nvda" in f.evidence.get("topic", "")]
    assert nvda, f"expected NVDA recurrence in {findings}"


def test_d9_negative_unique_topics(sync_session):
    distinct = [
        ["Cash buffer below floor"],
        ["Tax-loss harvesting deferred"],
        ["Bond ladder mismatch"],
    ]
    for n in (1, 2, 3):
        _make_run(sync_session, id_=n, started_offset_min=10 * n)
        _make_fm_report(sync_session, n, distinct[n - 1])
    findings = detect_objection_topic_recurrence(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D10 — agent_response_truncated
# ----------------------------------------------------------------------


def test_d10_positive_response_ends_with_comma(sync_session):
    sync_session.add(AgentReport(
        user_id=USER, agent_role="news", decision_id="d",
        response_text=(
            "X" * 250 + ", \"reasons\": ["
        ),
        tokens_in=10, tokens_out=10, cost_usd=0.01, model="opus",
    ))
    sync_session.commit()
    findings = detect_agent_response_truncated(
        sync_session, ReviewScope(user_id=USER),
    )
    assert any(f.detector == "D10" for f in findings)


def test_d10_negative_clean_terminator(sync_session):
    sync_session.add(AgentReport(
        user_id=USER, agent_role="news", decision_id="d",
        response_text="X" * 250 + ".",
        tokens_in=10, tokens_out=10, cost_usd=0.01, model="opus",
    ))
    sync_session.commit()
    findings = detect_agent_response_truncated(
        sync_session, ReviewScope(user_id=USER),
    )
    assert findings == []


# ----------------------------------------------------------------------
# D11 — codex_disagreed_with_synthesizer
# ----------------------------------------------------------------------


def _make_codex_report(
    db, decision_run_id: int, verdict: dict,
) -> AgentReport:
    """Helper: insert a codex_second_opinion agent_report row.

    Mirrors what ``run_codex_second_opinion`` persists — the row's
    ``response_text`` is the JSON form of a ``CodexSecondOpinion``.
    """
    row = AgentReport(
        user_id=USER,
        agent_role="codex_second_opinion",
        decision_id=f"plan-synth-{decision_run_id}",
        response_text=json.dumps(verdict),
        tokens_in=0, tokens_out=500, cost_usd=0.0, model="gpt-5-codex",
    )
    db.add(row)
    db.commit()
    return row


def test_d11_fires_red_when_codex_blocks(sync_session):
    """Codex assessed BLOCK → RED finding."""
    _make_run(sync_session, id_=42, started_offset_min=10)
    _make_codex_report(sync_session, 42, {
        "overall_assessment": "BLOCK",
        "findings": [
            {"severity": "BLOCKER", "topic": "Cash buffer collapse",
             "detail": "The plan drains the buffer below floor.",
             "suggested_fix": "Restore the buffer.",
             "cited_synthesizer_paragraphs": []},
            {"severity": "BLOCKER", "topic": "Tax sequencing",
             "detail": "Section 102 sequencing is wrong.",
             "suggested_fix": "Defer.",
             "cited_synthesizer_paragraphs": []},
        ],
        "agreement_with_argosy": {
            "agrees_with_risk_verdict": False,
            "novel_concerns_argosy_missed": [],
        },
        "user_directive_respected": True,
    })
    findings = detect_codex_disagreed_with_synthesizer(
        sync_session, ReviewScope(user_id=USER, decision_run_id=42),
    )
    red = [f for f in findings if f.severity == "RED"]
    assert len(red) == 1
    assert red[0].detector == "D11"
    assert "BLOCKED" in red[0].title
    topics = red[0].evidence.get("blocker_topics") or []
    assert "Cash buffer collapse" in topics
    assert "Tax sequencing" in topics


def test_d11_fires_amber_when_user_directive_ignored(sync_session):
    """Codex flags user_directive_respected=False → AMBER."""
    _make_run(sync_session, id_=43, started_offset_min=10)
    _make_codex_report(sync_session, 43, {
        "overall_assessment": "APPROVE_WITH_CONDITIONS",
        "findings": [],
        "agreement_with_argosy": {
            "agrees_with_risk_verdict": "partial",
            "novel_concerns_argosy_missed": [],
        },
        "user_directive_respected": False,
    })
    findings = detect_codex_disagreed_with_synthesizer(
        sync_session, ReviewScope(user_id=USER, decision_run_id=43),
    )
    amber = [f for f in findings if f.severity == "AMBER"]
    assert len(amber) == 1
    assert amber[0].detector == "D11"
    assert "user directive" in amber[0].title.lower()
    assert amber[0].evidence.get("user_directive_respected") is False


def test_d11_fires_yellow_when_codex_finds_novel_concerns(sync_session):
    """Codex returns 2 novel concerns → 1 YELLOW with both in detail."""
    _make_run(sync_session, id_=44, started_offset_min=10)
    novel = [
        "Concentration risk in semis sector not addressed by analysts.",
        "RSU vesting schedule overlaps with tax-loss harvest window.",
    ]
    _make_codex_report(sync_session, 44, {
        "overall_assessment": "APPROVE_WITH_CONDITIONS",
        "findings": [],
        "agreement_with_argosy": {
            "agrees_with_risk_verdict": "partial",
            "novel_concerns_argosy_missed": novel,
        },
        "user_directive_respected": True,
    })
    findings = detect_codex_disagreed_with_synthesizer(
        sync_session, ReviewScope(user_id=USER, decision_run_id=44),
    )
    yellow = [f for f in findings if f.severity == "YELLOW"]
    assert len(yellow) == 1
    assert yellow[0].detector == "D11"
    listed = yellow[0].evidence.get("novel_concerns_argosy_missed") or []
    assert novel[0] in listed
    assert novel[1] in listed


def test_d11_no_finding_when_codex_skipped(sync_session):
    """No codex row at all → empty list (not an error)."""
    _make_run(sync_session, id_=45, started_offset_min=10)
    # Intentionally do NOT insert a codex_second_opinion row.
    findings = detect_codex_disagreed_with_synthesizer(
        sync_session, ReviewScope(user_id=USER, decision_run_id=45),
    )
    assert findings == []


def test_d11_handles_unparseable_codex_response(sync_session):
    """Codex row has garbage text → graceful empty + log (no crash)."""
    _make_run(sync_session, id_=46, started_offset_min=10)
    # Insert a row whose response_text isn't recoverable JSON.
    sync_session.add(AgentReport(
        user_id=USER,
        agent_role="codex_second_opinion",
        decision_id="plan-synth-46",
        response_text="completely unparseable garbage with no braces at all",
        tokens_in=0, tokens_out=100, cost_usd=0.0, model="gpt-5-codex",
    ))
    sync_session.commit()
    findings = detect_codex_disagreed_with_synthesizer(
        sync_session, ReviewScope(user_id=USER, decision_run_id=46),
    )
    assert findings == []


def test_d11_no_finding_when_no_run_id_in_scope(sync_session):
    """Daily sweep (decision_run_id=None) → empty list."""
    findings = detect_codex_disagreed_with_synthesizer(
        sync_session, ReviewScope(user_id=USER, decision_run_id=None),
    )
    assert findings == []


# ----------------------------------------------------------------------
# Robustness — a detector that raises must not crash the report.
# ----------------------------------------------------------------------


def test_run_all_detectors_isolates_a_crashing_detector(sync_session, monkeypatch):
    """If one detector raises, others still run and stats reports the failure."""

    def _boom(db, scope):
        raise RuntimeError("synthetic boom")

    # Replace one detector with the bomb.
    original = list(fsr.ALL_DETECTORS)
    patched = list(original)
    patched[5] = ("D6", "cost_outlier_per_role", _boom)
    monkeypatch.setattr(fsr, "ALL_DETECTORS", tuple(patched))

    findings, stats = run_all_detectors(
        sync_session, ReviewScope(user_id=USER),
    )
    assert isinstance(findings, list)  # didn't raise
    d6 = next(s for s in stats if s["detector"] == "D6")
    assert d6["ok"] is False
    assert "synthetic boom" in (d6["error"] or "")


# ----------------------------------------------------------------------
# End-to-end runner — generate_fleet_self_review writes a row.
# ----------------------------------------------------------------------


def test_generate_fleet_self_review_persists_row(sync_session, tmp_path):
    """Tiny DB, fire the runner, assert a row landed with non-empty
    markdown + correct severity counts."""
    # Seed D7-tripping data: an old running decision_run.
    _make_run(
        sync_session, id_=99, started_offset_min=300,
        status="running", fund_manager_decision=None,
    )

    # Point D1 at a known-good orchestrator (so D1 doesn't trip in this
    # test) by passing a clean file via the scope.  generate_fleet_self_review
    # doesn't expose scope.orchestrator_path so we just rely on the real
    # orchestrator file (which we KNOW is clean post-f8faaca).
    row = generate_fleet_self_review(
        sync_session,
        user_id=USER,
        scope_kind="manual",
        decision_run_id=None,
    )
    assert isinstance(row, FleetSelfReviewReport)
    assert row.id is not None
    assert row.scope_kind == "manual"
    # D7 must have tripped on the 5h-old running row.
    sev = json.loads(row.severity_summary_json)
    assert sev["RED"] >= 1
    # Findings list contains a D7.
    findings = json.loads(row.findings_json)
    assert any(f["detector"] == "D7" for f in findings)
    # Markdown is non-trivial.
    assert "Fleet self-review" in row.content_md
    assert "Detector run-stats" in row.content_md


def test_compose_markdown_groups_by_severity_and_dedups_stats(sync_session):
    """Pure render test — feed two synthetic Findings + stats."""
    findings = [
        Finding(id="D1:x", detector="D1", severity="RED",
                category="architecture", title="Critical bug"),
        Finding(id="D6:y", detector="D6", severity="YELLOW",
                category="cost", title="Cost outlier"),
    ]
    stats = [
        {"detector": "D1", "name": "n1", "ok": True, "count": 1, "error": None},
        {"detector": "D2", "name": "n2", "ok": False, "count": 0,
         "error": "boom"},
    ]
    md = compose_markdown(
        findings, stats,
        scope=ReviewScope(user_id=USER, decision_run_id=42),
        scope_kind="post_synthesis",
    )
    assert "Critical bug" in md
    assert "Cost outlier" in md
    assert "decision_run #42" in md
    # Stats table includes the failing detector.
    assert "boom" in md
    # RED section appears before YELLOW.
    assert md.index("## RED findings") < md.index("## YELLOW findings")
