"""T4.5 daily-brief runner — end-to-end tests with stubs.

Covers:
  - Stubbed agent + adapters → brief generated, persisted, and a
    matching ``decision_runs`` row exists.
  - Adapter failure during input gathering → ``track_adapter_call``
    records ``http_error`` / ``exception`` outcome; brief still
    produced and persisted (graceful degradation).
  - Idempotency: re-running for the same ``brief_date`` updates the
    existing ``daily_briefs`` row instead of creating a duplicate.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
        tests/test_daily_brief_runner.py -v
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.daily_briefer import DailyBrieferAgent, DailyBriefMarkdown
from argosy.services import daily_brief_runner as runner_mod
from argosy.services.daily_brief_runner import generate_daily_brief
from argosy.state import db as db_mod
from argosy.state.models import DailyBrief, DecisionRun, PlanVersion, User


# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------


def _stub_agent(canned: dict | None = None) -> DailyBrieferAgent:
    """Return a DailyBrieferAgent whose ``_call_model`` returns canned JSON.

    The boilerplate ``require_citations`` is already False on this
    agent, so an empty cited_sources list passes validation.
    """
    canned = canned or {
        "top_line": "Quiet overnight; portfolio within plan.",
        "content_md": (
            "## Overnight\n\n"
            "Markets calm. VIX near 15.\n\n"
            "## Holdings\n\n"
            "No material headlines.\n"
        ),
        "confidence": "MEDIUM",
        "cited_sources": ["macro/snapshot"],
    }

    class _Agent(DailyBrieferAgent):
        async def _call_model(self, *, system, user, **kwargs):
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=500,
                tokens_out=400,
                model=self.model,
            )

    return _Agent(user_id="ariel")


async def _seed_user_and_plan() -> None:
    """Seed user 'ariel' + one ``role='current'`` plan."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            PlanVersion(
                user_id="ariel",
                version_label="Plan v1",
                source_path="",
                raw_markdown="# Plan\n\nNVDA target 15%.\n",
                imported_at=datetime(2026, 5, 1, tzinfo=UTC),
                role="current",
            )
        )
        await session.commit()


# ----------------------------------------------------------------------
# Test 1: happy path — stubbed agent + adapters, brief persisted.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_daily_brief_happy_path(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With stubbed adapters + agent, the brief lands as one row plus
    one ``decision_runs`` row pointing back."""
    await _seed_user_and_plan()

    # Stub adapter gathering so we don't touch the network. We don't
    # need adapter outcomes here (covered in test 2) — just empty
    # payloads so the agent gets a minimal-but-valid input bundle.
    async def _stub_macro() -> dict[str, float]:
        return {"vix": 15.2, "ust_10y": 4.18}

    async def _stub_news(tickers):
        return {"NVDA": [{"headline": "NVDA quiet", "summary": "."}]}

    def _stub_tsv() -> tuple[list[str], str]:
        return (["NVDA"], "  NVDA     qty=100  value=80000  acct=fidelity")

    monkeypatch.setattr(runner_mod, "_gather_macro_snapshot", _stub_macro)
    monkeypatch.setattr(runner_mod, "_gather_news", _stub_news)
    monkeypatch.setattr(runner_mod, "_load_portfolio_snapshot", _stub_tsv)

    agent = _stub_agent()
    target = date(2026, 5, 26)

    async with db_mod.get_session() as session:
        row = await generate_daily_brief(
            "ariel", session, brief_date=target, agent=agent
        )

    # Row populated.
    assert row.brief_date == target
    assert "Overnight" in row.content_md
    assert row.summary_text.startswith("Quiet overnight")
    assert row.decision_run_id is not None

    # Exactly one DailyBrief + one DecisionRun.
    async with db_mod.get_session() as session:
        briefs = (await session.execute(select(DailyBrief))).scalars().all()
        runs = (await session.execute(select(DecisionRun))).scalars().all()
    assert len(briefs) == 1
    assert len(runs) == 1
    dr = runs[0]
    assert dr.decision_kind == "daily_brief"
    assert dr.status == "completed"
    # notes_json carries the brief_date per T4.4 UI label expectation.
    notes = json.loads(dr.notes_json)
    assert notes["brief_date"] == target.isoformat()
    # Back-pointer matches.
    assert briefs[0].decision_run_id == dr.id


# ----------------------------------------------------------------------
# Test 2: graceful degradation — adapter raises, brief still produced.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_daily_brief_adapter_failure_graceful(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the macro adapter raises mid-gather, the brief still lands.

    Verifies:
      - Adapter outcomes tracked via ``track_adapter_call`` carry the
        ``http_error`` / ``exception`` status.
      - The agent still gets called (with empty macro_snapshot in this
        case) and the brief is persisted.
      - The brief mentions "no overnight data" in the canned payload
        so the home page doesn't render a blank card.
    """
    await _seed_user_and_plan()

    # Force the macro adapter to raise; the runner's per-series
    # exception capture must convert that to an adapter outcome and
    # carry on. We also stub news to be empty and TSV to be empty so
    # the agent sees a truly degraded input set.
    from argosy.services import adapter_outcomes

    async def _failing_get_series(self, series, **kwargs):
        raise RuntimeError(f"network down for {series}")

    # Patch the FredAdapter's get_series to fail. Each per-series call
    # in _gather_macro_snapshot is wrapped in track_adapter_call, so
    # the four series produce four ``exception`` outcomes.
    from argosy.adapters.data.fred_adapter import FredAdapter

    monkeypatch.setattr(FredAdapter, "get_series", _failing_get_series)

    async def _empty_news(tickers):
        return {}

    def _empty_tsv():
        return ([], "")

    monkeypatch.setattr(runner_mod, "_gather_news", _empty_news)
    monkeypatch.setattr(runner_mod, "_load_portfolio_snapshot", _empty_tsv)

    # Reset the outcomes contextvar so we can observe what the runner
    # captures during this call.
    adapter_outcomes.reset_outcomes()

    # Canned agent output reflects degraded state.
    agent = _stub_agent(
        canned={
            "top_line": "No overnight data available.",
            "content_md": (
                "No overnight data available — FRED, news and "
                "portfolio inputs all empty. Returning a placeholder."
            ),
            "confidence": "LOW",
            "cited_sources": [],
        }
    )

    target = date(2026, 5, 27)
    async with db_mod.get_session() as session:
        row = await generate_daily_brief(
            "ariel", session, brief_date=target, agent=agent
        )

    # Brief landed despite adapter failures.
    assert "No overnight data" in row.content_md
    assert row.brief_date == target

    # Adapter outcomes were recorded — at least one ``exception`` /
    # ``http_error`` outcome per failed series. We don't assert exact
    # series_count because the failure path is the same for all four;
    # we DO require that the captured set is non-empty and all are
    # non-ok.
    outcomes = adapter_outcomes.collect_outcomes()
    assert outcomes, "expected at least one adapter outcome to be recorded"
    fred_outcomes = [o for o in outcomes if o.adapter_name == "fred"]
    assert fred_outcomes, "expected FRED outcomes to be recorded"
    # Every FRED outcome must be a non-ok status (the adapter raised).
    assert all(o.status != "ok" for o in fred_outcomes), [
        o.status for o in fred_outcomes
    ]

    # And the decision_runs row is still marked completed (graceful
    # degradation: adapter failure is NOT a runner failure).
    async with db_mod.get_session() as session:
        runs = (await session.execute(select(DecisionRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "completed"


# ----------------------------------------------------------------------
# Test 3: idempotency — same brief_date re-run updates, doesn't dup.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_daily_brief_idempotent_on_same_date(
    engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive calls for the same ``brief_date`` produce ONE
    ``daily_briefs`` row (updated in place), not two.

    Each call still creates its own ``decision_runs`` row — the
    timeline of "we ran this twice" is honest, but the user-facing
    artifact stays single."""
    await _seed_user_and_plan()

    async def _stub_macro():
        return {}

    async def _stub_news(tickers):
        return {}

    def _stub_tsv():
        return ([], "")

    monkeypatch.setattr(runner_mod, "_gather_macro_snapshot", _stub_macro)
    monkeypatch.setattr(runner_mod, "_gather_news", _stub_news)
    monkeypatch.setattr(runner_mod, "_load_portfolio_snapshot", _stub_tsv)

    target = date(2026, 5, 28)

    # First run: produce a v1 brief.
    agent_v1 = _stub_agent(
        canned={
            "top_line": "v1 brief",
            "content_md": "v1 body",
            "confidence": "MEDIUM",
            "cited_sources": [],
        }
    )
    async with db_mod.get_session() as session:
        row_v1 = await generate_daily_brief(
            "ariel", session, brief_date=target, agent=agent_v1
        )
    first_id = row_v1.id

    # Second run: produce a v2 brief on the SAME date.
    agent_v2 = _stub_agent(
        canned={
            "top_line": "v2 brief",
            "content_md": "v2 body",
            "confidence": "MEDIUM",
            "cited_sources": [],
        }
    )
    async with db_mod.get_session() as session:
        row_v2 = await generate_daily_brief(
            "ariel", session, brief_date=target, agent=agent_v2
        )
    # Same DailyBrief row was updated, not a new one.
    assert row_v2.id == first_id
    assert row_v2.content_md == "v2 body"
    assert row_v2.summary_text == "v2 brief"

    # But TWO decision_runs exist — every fire produces its own.
    async with db_mod.get_session() as session:
        briefs = (await session.execute(select(DailyBrief))).scalars().all()
        runs = (await session.execute(select(DecisionRun))).scalars().all()
    assert len(briefs) == 1, [b.id for b in briefs]
    assert len(runs) == 2, [r.id for r in runs]
    # The brief points at the LATEST decision_run (v2).
    assert briefs[0].decision_run_id == max(r.id for r in runs)


# ----------------------------------------------------------------------
# Test 4: scheduler gate — disabled under pytest.
# ----------------------------------------------------------------------


def test_is_enabled_for_runtime_off_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_enabled_for_runtime`` must be False whenever
    ``PYTEST_CURRENT_TEST`` is set, regardless of the env-var opt-in.

    This is the test-isolation guard: even if a CI environment exports
    ``ARGOSY_DAILY_BRIEF_ENABLED=1`` globally, the scheduler must NOT
    fire under pytest."""
    monkeypatch.setenv("ARGOSY_DAILY_BRIEF_ENABLED", "1")
    # PYTEST_CURRENT_TEST is set by pytest itself for every test.
    assert runner_mod.is_enabled_for_runtime() is False


def test_is_enabled_for_runtime_respects_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside pytest context the env var alone determines on/off."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ARGOSY_DAILY_BRIEF_ENABLED", raising=False)
    assert runner_mod.is_enabled_for_runtime() is False
    monkeypatch.setenv("ARGOSY_DAILY_BRIEF_ENABLED", "1")
    assert runner_mod.is_enabled_for_runtime() is True
    monkeypatch.setenv("ARGOSY_DAILY_BRIEF_ENABLED", "0")
    assert runner_mod.is_enabled_for_runtime() is False
