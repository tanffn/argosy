"""Tests for GET /api/decisions/{id}/replay (Wave D)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argosy.agents.researcher_facilitator import DebateOutcome
from argosy.agents.base import ConfidenceBand
from argosy.services.negotiation_recorder import record_negotiation_phase
from argosy.state.models import (
    AgentReport,
    DecisionPhase,
    DecisionRun,
    User,
    UserFile,
)


@pytest.fixture
def _seed(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        if sess.get(User, "bob") is None:
            sess.add(User(id="bob", plan="free"))
        sess.commit()
    finally:
        sess.close()


def _seed_run_with_phase(
    client_with_db, *, user_id: str = "ariel",
) -> tuple[int, int]:
    """Insert a decision_run + one decision_phase + an agent_report.
    Returns (run_id, phase_id).
    """
    sess = client_with_db.app.state.session_factory()
    try:
        run = DecisionRun(
            user_id=user_id, ticker="AAPL", tier="T2",
            decision_kind="trade_proposal", status="completed",
            started_at=datetime.now(timezone.utc),
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id

        ar = AgentReport(
            user_id=user_id, agent_role="bull_researcher",
            decision_id=str(run_id), response_text="Bull case",
            confidence="HIGH", model="opus",
            tokens_in=100, tokens_out=200, cost_usd=0.05,
        )
        sess.add(ar)
        sess.commit()
        sess.refresh(ar)
        ar_id = ar.id

        phase = DecisionPhase(
            decision_run_id=run_id, user_id=user_id, seq=1,
            kind="researcher_debate",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            participants_json=json.dumps([{
                "agent_role": "bull_researcher",
                "agent_report_id": ar_id,
                "side": "bull",
                "round": 1,
                "confidence": "HIGH",
                "model": "opus",
            }]),
            verdict_json=json.dumps({
                "winning_side": "bull",
                "synthesis": "Bull thesis prevails on valuation.",
                "cited_evidence": [],
                "rounds_run": 1,
                "confidence": "HIGH",
                "cited_sources": ["docs/x.md"],
            }),
            verdict_kind="DebateOutcome",
            tldr_md="## Debate verdict\n\n- Winning side: `bull`\n",
            bundle_dir=None,
        )
        sess.add(phase)
        sess.commit()
        sess.refresh(phase)
        phase_id = phase.id
    finally:
        sess.close()
    return run_id, phase_id


def test_replay_returns_404_for_unknown_run(client_with_db, _seed):
    r = client_with_db.get("/api/decisions/9999/replay?user_id=ariel")
    assert r.status_code == 404


def test_replay_returns_404_for_other_user(client_with_db, _seed):
    run_id, _ = _seed_run_with_phase(client_with_db, user_id="ariel")
    r = client_with_db.get(f"/api/decisions/{run_id}/replay?user_id=bob")
    assert r.status_code == 404


def test_replay_returns_full_payload(client_with_db, _seed):
    run_id, phase_id = _seed_run_with_phase(client_with_db)
    r = client_with_db.get(f"/api/decisions/{run_id}/replay?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["decision_run"]["id"] == run_id
    assert body["decision_run"]["ticker"] == "AAPL"
    assert body["decision_run"]["tier"] == "T2"

    assert len(body["phases"]) == 1
    phase = body["phases"][0]
    assert phase["id"] == phase_id
    assert phase["kind"] == "researcher_debate"
    assert phase["verdict_kind"] == "DebateOutcome"
    assert phase["verdict"]["winning_side"] == "bull"
    assert "Winning side" in phase["tldr_md"]
    assert phase["transcript_md_url"].endswith(
        f"/decisions/{run_id}/phases/{phase_id}/transcript"
    )

    assert len(phase["participants"]) == 1
    p = phase["participants"][0]
    assert p["agent_role"] == "bull_researcher"
    assert p["side"] == "bull"
    assert p["round"] == 1
    assert p["confidence"] == "HIGH"
    assert p["model"] == "opus"
    assert p["tokens_in"] == 100
    assert p["cost_usd"] == 0.05


def test_replay_includes_user_files_inputs(client_with_db, _seed):
    run_id, _ = _seed_run_with_phase(client_with_db)

    sess = client_with_db.app.state.session_factory()
    try:
        f = UserFile(
            user_id="ariel", sha256="a" * 64, original_name="brief.png",
            sanitized_name="brief.png", mime_type="image/png", kind="image",
            size_bytes=10, storage_path="/tmp/brief.png", source="chat_attachment",
            decision_run_id=run_id,
        )
        sess.add(f)
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get(f"/api/decisions/{run_id}/replay?user_id=ariel")
    body = r.json()
    files = body["inputs"]["user_files"]
    assert len(files) == 1
    assert files[0]["original_name"] == "brief.png"
    assert files[0]["kind"] == "image"


def test_replay_phases_ordered_by_seq(client_with_db, _seed):
    """Multiple phases for one run come back in seq order."""
    sess = client_with_db.app.state.session_factory()
    try:
        run = DecisionRun(
            user_id="ariel", ticker="MSFT", tier="T3",
            decision_kind="trade_proposal", status="completed",
            started_at=datetime.now(timezone.utc),
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
        for seq, kind in enumerate(["analysts", "researcher_debate", "trader"], start=1):
            sess.add(DecisionPhase(
                decision_run_id=run_id, user_id="ariel", seq=seq, kind=kind,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                participants_json="[]",
            ))
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get(f"/api/decisions/{run_id}/replay?user_id=ariel")
    body = r.json()
    kinds = [p["kind"] for p in body["phases"]]
    assert kinds == ["analysts", "researcher_debate", "trader"]
    seqs = [p["seq"] for p in body["phases"]]
    assert seqs == [1, 2, 3]


def test_phase_transcript_endpoint_streams_when_present(
    client_with_db, _seed, tmp_path, monkeypatch,
):
    """The transcript.md stream returns the on-disk file when bundle_dir is set."""
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    # Create a real bundle on disk via the recorder.
    sess = client_with_db.app.state.session_factory()
    try:
        run = DecisionRun(
            user_id="ariel", ticker="GOOG", tier="T1",
            decision_kind="trade_proposal", status="completed",
            started_at=datetime.now(timezone.utc),
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
    finally:
        sess.close()

    v = DebateOutcome(
        winning_side="bull", synthesis="ok", cited_evidence=[], rounds_run=1,
        confidence=ConfidenceBand.MEDIUM, cited_sources=["docs/x.md"],
    )
    phase_id = asyncio.run(record_negotiation_phase(
        user_id="ariel", decision_run_id=run_id, kind="researcher_debate",
        started_at=datetime.now(timezone.utc),
        agent_report_ids=[], verdict=v,
    ))

    r = client_with_db.get(
        f"/api/decisions/{run_id}/phases/{phase_id}/transcript?user_id=ariel"
    )
    assert r.status_code == 200
    assert "Transcript" in r.text or "transcript" in r.text.lower()


def test_phase_transcript_404_for_other_user(client_with_db, _seed):
    """Wrong user gets 404 (don't leak existence)."""
    sess = client_with_db.app.state.session_factory()
    try:
        run = DecisionRun(
            user_id="ariel", ticker="GOOG", tier="T1",
            decision_kind="trade_proposal", status="completed",
            started_at=datetime.now(timezone.utc),
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        run_id = run.id
        phase = DecisionPhase(
            decision_run_id=run_id, user_id="ariel", seq=1, kind="trader",
            started_at=datetime.now(timezone.utc),
            participants_json="[]",
            bundle_dir="/tmp/nonexistent",
        )
        sess.add(phase)
        sess.commit()
        sess.refresh(phase)
        phase_id = phase.id
    finally:
        sess.close()

    r = client_with_db.get(
        f"/api/decisions/{run_id}/phases/{phase_id}/transcript?user_id=bob"
    )
    assert r.status_code == 404
