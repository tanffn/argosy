"""Tests for the fire-and-forget FM-objection translation precompute.

Background
----------
The first GET /api/plan/draft/objections previously paid ~125 s wall-clock
for N parallel Sonnet translator calls. The fix moves that work into a
fire-and-forget cache warmer that runs at synthesis completion so the
``fm_objection_translations`` table is already populated by the time
the user opens /plan.

Coverage
--------
(a) FM rejection (non-empty reasons) → background task fires and
    persists translation rows.
(b) FM approval → no thread spawned (skipped at scheduling time).
(c) Cache already warm → scheduler early-exits without spawning a
    thread, AND the translator agent is never invoked.
(d) Translator failure → cache rows missing for that slot, but
    ``run_synthesis`` itself completes successfully with no error
    propagated up the stack.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.objection_translator import ObjectionTranslation
from argosy.state.models import (
    AgentReport,
    FMObjectionTranslation,
    PlanVersion,
    User,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session(alembic_engine_at_head):
    """A SQLite session with the schema at HEAD plus a baseline plan.

    Mirrors ``tests/test_plan_synthesis_flow.py::session`` so the
    orchestrator's prerequisites (baseline PlanVersion + User row) are
    in place before ``run_synthesis`` is called.
    """
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(
        PlanVersion(
            user_id="ariel",
            role="baseline",
            version_label="Jacobs v2.0",
            raw_markdown="# Plan",
            distillate_rendered="# Plan distillate\n\nUCITS-first.\n",
        )
    )
    s.commit()
    yield s
    s.close()


def _stub_synthesis_output():
    """Same canonical stub used in test_plan_synthesis_flow."""
    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SynthesisInputs,
    )

    long = HorizonSection(
        horizon="long",
        freshness_expected="annual",
        status="no_change",
        posture="long posture",
    )
    medium = HorizonSection(
        horizon="medium",
        freshness_expected="quarterly",
        status="minor_revision",
        posture="medium posture",
    )
    short = HorizonSection(
        horizon="short",
        freshness_expected="monthly",
        status="major_revision",
        posture="short posture",
    )
    return PlanSynthesisOutput(
        long=long, medium=medium, short=short,
        inputs=SynthesisInputs(),
    )


def _make_fake_translator_report(headline: str, plain_english: str, actions: list[str]):
    """ObjectionTranslator-shaped fake the cache helper introspects.

    Mirrors the helper in ``test_fm_objection_translation_cache.py``.
    """

    class _Report:
        output = ObjectionTranslation(
            headline=headline,
            plain_english=plain_english,
            recommended_actions=actions,
        )

    return _Report()


def _stub_phases_for_synthesis(monkeypatch, *, approved: bool, fm_response_text: str | None):
    """Stub every phase of run_synthesis so the orchestrator runs without
    touching real LLMs. Optionally persists a fund_manager agent_report
    row (matching what _ingest_synthesis_trail would do for a live run)
    so the precompute thread has FM text to parse.
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    monkeypatch.setattr(flow, "_run_phase_1_analysts", lambda **kw: "(analyst reports)")
    monkeypatch.setattr(flow, "_run_phase_2_debates", lambda **kw: "(debate outcomes)")
    monkeypatch.setattr(flow, "_run_phase_3_synthesizer", lambda **kw: _stub_synthesis_output())
    monkeypatch.setattr(flow, "_run_phase_4_risk", lambda **kw: "(risk verdict)")
    monkeypatch.setattr(flow, "_run_phase_5_fund_manager", lambda **kw: approved)
    monkeypatch.setattr(flow, "_assemble_portfolio_summary", lambda **kw: "NVDA 14%")
    monkeypatch.setattr(flow, "_assemble_fills_summary", lambda **kw: "(no fills)")

    # Inject the fund_manager agent_report row right after the
    # orchestrator commits the draft. The orchestrator's
    # _ingest_synthesis_trail call is also mocked to write our canned
    # response_text — this is the row the precompute thread will read.
    if fm_response_text is not None:
        def _fake_ingest_trail(sess, decision_audit_token, _fm_text=fm_response_text):
            # decision_audit_token is e.g. "plan-synth-1".
            sess.add(
                AgentReport(
                    user_id="ariel",
                    agent_role="fund_manager",
                    decision_id=decision_audit_token,
                    response_text=_fm_text,
                    model="claude-opus-4-7",
                )
            )
            sess.commit()
            return 1

        monkeypatch.setattr(flow, "_ingest_synthesis_trail", _fake_ingest_trail)
    else:
        # FM approved + no objections path: no FM row written; the
        # scheduler's early-exit on "no FM report" is exercised. Use a
        # no-op so the real ingest path doesn't run (and look for the
        # JSONL file that doesn't exist in this test fixture).
        monkeypatch.setattr(flow, "_ingest_synthesis_trail", lambda *a, **kw: 0)


# ---------------------------------------------------------------------------
# (a) Happy path — FM rejection with non-empty objections
# ---------------------------------------------------------------------------


def test_fm_rejection_warms_translation_cache(session, monkeypatch):
    """When FM rejects with N reasons, the post-synthesis precompute
    thread parses + translates them, populating
    ``fm_objection_translations`` so the route hits cache on first load.
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    fm_response = json.dumps(
        {
            "approved": False,
            "reasons": [
                "TIME-CRITICAL — missed Section 102 deadline",
                "MISSING STOP — no drawdown trigger on speculative bucket",
            ],
            "cited_sources": ["docs/design/SDD.md#§6.11"],
        }
    )

    _stub_phases_for_synthesis(
        monkeypatch, approved=False, fm_response_text=fm_response,
    )

    translator_calls: list[str] = []

    async def _fake_run(self, *, topic, detail, severity, cited_sources=None):
        translator_calls.append(topic)
        return _make_fake_translator_report(
            headline=f"H::{topic}",
            plain_english=f"PE::{topic}",
            actions=[f"A::{topic}"],
        )

    # Run synthesis. The orchestrator schedules the precompute on a
    # daemon thread; we join it via threading.enumerate() so the
    # test deterministically waits for the warmer to finish before
    # asserting on rows.
    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
        _join_precompute_threads()

    assert out.draft_id is not None

    # Translator was invoked once per objection (parallel batch).
    assert sorted(translator_calls) == [
        "MISSING STOP", "TIME-CRITICAL",
    ]

    # Two rows persisted, one per objection, attached to the draft.
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == out.draft_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    by_idx = {r.objection_index: r for r in rows}
    assert by_idx[0].headline == "H::TIME-CRITICAL"
    assert by_idx[1].plain_english == "PE::MISSING STOP"


# ---------------------------------------------------------------------------
# (b) FM approved → no precompute, no thread spawned
# ---------------------------------------------------------------------------


def test_fm_approval_skips_precompute(session, monkeypatch):
    """When FM approves, the orchestrator MUST NOT spawn the precompute
    thread (saves a fork + cache hit on the empty objections list).
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    _stub_phases_for_synthesis(monkeypatch, approved=True, fm_response_text=None)

    schedule_calls: list[int] = []

    def _spy_schedule(**kw):
        schedule_calls.append(kw.get("plan_version_id", -1))

    monkeypatch.setattr(
        flow, "_schedule_fm_objection_translation_precompute", _spy_schedule,
    )

    translator_calls: list[str] = []

    async def _fake_run(self, **kw):
        translator_calls.append(kw.get("topic", ""))
        return _make_fake_translator_report("h", "pe", [])

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
        _join_precompute_threads()

    assert out.draft_id is not None
    # The scheduler must never have been called — FM approved.
    assert schedule_calls == []
    # Translator must never have been invoked.
    assert translator_calls == []
    # No cache rows persisted.
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == out.draft_id,
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ---------------------------------------------------------------------------
# (c) Cache already warm → no new translator calls
# ---------------------------------------------------------------------------


def test_cache_already_warm_skips_translator(session, monkeypatch):
    """If ``fm_objection_translations`` rows already exist for the draft
    (e.g. an earlier on-demand route call warmed it), the scheduler
    must early-exit BEFORE spawning the thread so the translator is
    never invoked.
    """
    from argosy.orchestrator.flows import plan_synthesis as flow

    fm_response = json.dumps(
        {
            "approved": False,
            "reasons": ["TOPIC_A — detail A"],
            "cited_sources": [],
        }
    )

    # Inject a fund_manager AgentReport pre-emptively so when the
    # orchestrator's ``_ingest_synthesis_trail`` is monkeypatched, we
    # can ALSO seed a pre-existing cache row on the freshly-created
    # draft. Wrap the ingest function to add a cache row alongside the
    # AgentReport so the scheduler's "already cached" guard trips.
    original_stub = _stub_phases_for_synthesis
    original_stub(monkeypatch, approved=False, fm_response_text=fm_response)

    original_ingest = flow._ingest_synthesis_trail

    def _ingest_then_seed_cache(sess, decision_audit_token):
        # Run the test's existing fake ingest (writes the FM row).
        original_ingest(sess, decision_audit_token)
        # Look up the freshly-committed draft and seed a cache row on
        # it so the scheduler sees an already-warm cache.
        draft = (
            sess.execute(
                sa.select(PlanVersion)
                .where(PlanVersion.user_id == "ariel", PlanVersion.role == "draft")
                .order_by(PlanVersion.id.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        if draft is not None:
            sess.add(
                FMObjectionTranslation(
                    plan_version_id=draft.id,
                    objection_index=0,
                    topic_hash="0" * 64,
                    headline="(pre-warmed)",
                    plain_english="(pre-warmed)",
                    recommended_actions_json="[]",
                )
            )
            sess.commit()
        return 1

    monkeypatch.setattr(flow, "_ingest_synthesis_trail", _ingest_then_seed_cache)

    translator_calls: list[str] = []

    async def _fake_run(self, **kw):
        translator_calls.append(kw.get("topic", ""))
        return _make_fake_translator_report("h", "pe", [])

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_fake_run,
    ):
        out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
        _join_precompute_threads()

    assert out.draft_id is not None
    # The scheduler must have early-exited; translator never invoked.
    assert translator_calls == []
    # The pre-warmed row is still the only row.
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == out.draft_id,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].headline == "(pre-warmed)"


# ---------------------------------------------------------------------------
# (d) Translator failure → cache rows missing, synthesis succeeds
# ---------------------------------------------------------------------------


def test_translator_failure_does_not_break_synthesis(session, monkeypatch):
    """When the translator agent raises for every objection, the
    precompute thread MUST log + swallow the error. ``run_synthesis``
    returns its normal SynthesisResult and no error propagates.

    The cache table is empty for the failing slots — the on-demand
    route path takes over as the fallback.
    """
    from argosy.agents.errors import AgentRunError
    from argosy.orchestrator.flows import plan_synthesis as flow

    fm_response = json.dumps(
        {
            "approved": False,
            "reasons": [
                "BAD_TOPIC_A — detail one",
                "BAD_TOPIC_B — detail two",
            ],
            "cited_sources": [],
        }
    )

    _stub_phases_for_synthesis(
        monkeypatch, approved=False, fm_response_text=fm_response,
    )

    translator_calls: list[str] = []

    async def _always_fails(self, *, topic, detail, severity, cited_sources=None):
        translator_calls.append(topic)
        raise AgentRunError(f"simulated translator failure for {topic}")

    with patch(
        "argosy.agents.objection_translator.ObjectionTranslatorAgent.run",
        new=_always_fails,
    ):
        # The synthesis call itself must NOT raise; translator failures
        # in the background thread are best-effort.
        out = flow.run_synthesis(session, user_id="ariel", trigger="scheduled")
        _join_precompute_threads()

    assert out.draft_id is not None

    # Translator was attempted for both slots.
    assert sorted(translator_calls) == ["BAD_TOPIC_A", "BAD_TOPIC_B"]

    # No cache rows persisted (the helper drops partial rows for
    # failed translations).
    rows = (
        session.execute(
            sa.select(FMObjectionTranslation).where(
                FMObjectionTranslation.plan_version_id == out.draft_id,
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_precompute_threads(timeout: float = 30.0) -> None:
    """Block until every fm-objection-precompute daemon thread finishes.

    The scheduler names threads ``fm-objection-precompute-<decision_run_id>``
    so we filter to those. ``timeout`` is the per-thread join timeout —
    a stubbed translator returns instantly, so well under a second is
    the expected wall-clock.
    """
    import threading

    for t in threading.enumerate():
        if t is threading.current_thread():
            continue
        if (t.name or "").startswith("fm-objection-precompute-"):
            t.join(timeout=timeout)
            assert not t.is_alive(), (
                f"precompute thread {t.name!r} did not finish within {timeout}s"
            )
