"""Advisor API route tests — /turn, /gaps, /home-brief, and intake-alias backwards-compat."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from argosy.agents.advisor import AdvisorAgent
from argosy.agents.base import ModelCall
from argosy.api.routes.advisor import (
    classify_mode,
    reset_advisor_agent_factory,
    set_advisor_agent_factory,
)
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow
from argosy.state.models import (
    DailyBrief,
    InvestorEvent,
    KvCacheEntry,
    PensionFundSnapshot,
    User,
    UserContext,
)


def _canned(question: str, **overrides) -> dict:
    base = {
        "stage": "stage_1",
        "question_for_user": question,
        "context_updates": [],
        "stage_complete": False,
        "next_stage": None,
        "confidence": "MEDIUM",
        "cited_sources": [],
        "notes_for_orchestrator": "",
        "mode": "gap_driven",
    }
    base.update(overrides)
    return base


def _factory(canned: dict):
    def _make(user_id: str):
        class _M(AdvisorAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=80,
                    tokens_out=120,
                    model=self.model,
                )

        return _M(user_id=user_id)

    return _make


# ----------------------------------------------------------------------
# classify_mode unit
# ----------------------------------------------------------------------


def test_classify_mode_empty_is_gap_driven() -> None:
    assert classify_mode("") == "gap_driven"
    assert classify_mode("   ") == "gap_driven"


def test_classify_mode_question_is_user_driven() -> None:
    assert classify_mode("What's my tax bracket?") == "user_driven"


def test_classify_mode_statement_is_user_driven() -> None:
    """Even a non-question statement counts as a user-driven turn — the
    advisor should acknowledge + log + ask a related follow-up."""
    assert classify_mode("My salary just went up to 750k NIS.") == "user_driven"


# ----------------------------------------------------------------------
# /api/advisor/turn
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisor_turn_gap_driven_default(
    engine: None, client: AsyncClient
) -> None:
    set_advisor_agent_factory(_factory(_canned("What is your tax residency?")))
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["mode"] == "gap_driven"
        assert "tax residency" in body["question_for_user"].lower()
        assert body["intake_session_id"]
    finally:
        reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_advisor_turn_user_driven_when_question(
    engine: None, client: AsyncClient
) -> None:
    set_advisor_agent_factory(
        _factory(_canned("Israeli tax brackets are…", mode="user_driven"))
    )
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(
                UserContext(
                    user_id="ariel",
                    current_stage="stage_1",
                    identity_yaml="tax_residency: israel\n",
                )
            )
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={
                "user_id": "ariel",
                "last_user_message": "What's my surtax threshold?",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["mode"] == "user_driven"
    finally:
        reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_advisor_turn_persists_agent_report_with_advisor_role(
    engine: None, client: AsyncClient
) -> None:
    set_advisor_agent_factory(_factory(_canned("Q?")))
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200, res.text

        async with db_mod.get_session() as session:
            rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.user_id == "ariel",
                        AgentReportRow.agent_role == "advisor",
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].intake_session_id  # session id stamped
    finally:
        reset_advisor_agent_factory()


@pytest.mark.asyncio
async def test_advisor_turn_applies_context_updates(
    engine: None, client: AsyncClient
) -> None:
    """When the agent emits a context_update, the route must merge it
    into user_context.identity_yaml so the next turn sees it."""
    canned = _canned(
        "Got it.",
        context_updates=[
            {
                "target_section": "identity",
                "yaml_patch": "tax_residency: israel\n",
                "rationale": "User said they live in Tel Aviv.",
            }
        ],
    )
    set_advisor_agent_factory(_factory(canned))
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/advisor/turn",
            json={"user_id": "ariel", "last_user_message": "I live in Tel Aviv."},
        )
        assert res.status_code == 200, res.text

        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == "ariel")
                )
            ).scalar_one()
            assert "israel" in (ctx.identity_yaml or "").lower()
    finally:
        reset_advisor_agent_factory()


# ----------------------------------------------------------------------
# /api/advisor/gaps
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisor_gaps_empty_user_all_missing(
    engine: None, client: AsyncClient
) -> None:
    """A user with no user_context row at all should still yield a
    well-formed GapStatus where every catalog field is missing."""
    res = await client.get("/api/advisor/gaps", params={"user_id": "newbie"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_id"] == "newbie"
    assert body["counts"]["missing"] >= 10
    assert body["counts"]["fresh"] == 0
    assert body["counts"]["stale"] == 0


@pytest.mark.asyncio
async def test_advisor_gaps_marks_answered_fresh(
    engine: None, client: AsyncClient
) -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            UserContext(
                user_id="ariel",
                identity_yaml="tax_residency: israel\nuser_citizenship: [israel]\n",
                current_stage="stage_1",
            )
        )
        await session.commit()

    res = await client.get("/api/advisor/gaps", params={"user_id": "ariel"})
    assert res.status_code == 200, res.text
    body = res.json()
    fresh_paths = {
        item["path"] for item in body["items"] if item["state"] == "fresh"
    }
    assert "identity.tax_residency" in fresh_paths
    assert "identity.user_citizenship" in fresh_paths


@pytest.mark.asyncio
async def test_advisor_gaps_items_carry_freshness_and_label(
    engine: None, client: AsyncClient
) -> None:
    res = await client.get("/api/advisor/gaps", params={"user_id": "freshie"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"], "expected at least one item"
    sample = body["items"][0]
    for k in ("path", "label", "section", "freshness", "priority", "state"):
        assert k in sample


# ----------------------------------------------------------------------
# Backwards-compat: legacy /api/intake/* aliases must keep working.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_alias_status_still_works(
    engine: None, client: AsyncClient
) -> None:
    res = await client.get("/api/intake/status", params={"user_id": "ariel"})
    assert res.status_code == 200
    assert res.json()["current_stage"] == "stage_1"


@pytest.mark.asyncio
async def test_intake_alias_turn_still_works(
    engine: None, client: AsyncClient
) -> None:
    """The legacy /api/intake/turn route now delegates persistence to
    the shared `_persist_turn` helper. It must continue to:
      - resolve current_stage via user_context
      - return TurnResponse (no `mode` field)
      - stamp an agent_reports row with role=intake (NOT advisor)
    """
    from argosy.agents.intake import IntakeAgent
    from argosy.api.routes.intake import (
        reset_intake_agent_factory,
        set_intake_agent_factory,
    )

    def _intake_factory(user_id: str):
        class _M(IntakeAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(
                        {
                            "stage": "stage_1",
                            "question_for_user": "tax residence?",
                            "context_updates": [],
                            "stage_complete": False,
                            "next_stage": None,
                            "confidence": "MEDIUM",
                            "cited_sources": [],
                            "notes_for_orchestrator": "",
                        }
                    ),
                    tokens_in=80,
                    tokens_out=80,
                    model=self.model,
                )

        return _M(user_id=user_id)

    set_intake_agent_factory(_intake_factory)
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/intake/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        # Legacy shape — no `mode` field on the response.
        assert "mode" not in body
        assert body["stage"] == "stage_1"

        # Audit log: agent_role must remain "intake" so cost rollups by
        # role still group the right turns together.
        async with db_mod.get_session() as session:
            rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.user_id == "ariel",
                        AgentReportRow.agent_role == "intake",
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
    finally:
        reset_intake_agent_factory()


# ----------------------------------------------------------------------
# /api/advisor/home-brief
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_brief_empty_user_returns_intake_invite_only(
    engine: None, client: AsyncClient
) -> None:
    """A user with no context, no portfolio, no pension data should yield
    headline + cta + a single 'let's start with intake' gap bullet."""
    res = await client.get("/api/advisor/home-brief", params={"user_id": "newbie"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["headline"]
    assert body["cta"] == {"label": "Talk to advisor", "href": "/advisor"}
    assert body["generated_at"]
    # Without any portfolio or pension data, only the intake-invite gap
    # bullet should appear.
    assert len(body["bullets"]) == 1
    assert body["bullets"][0]["kind"] == "gap"
    assert "intake" in body["bullets"][0]["text"].lower()


@pytest.mark.asyncio
async def test_home_brief_full_user_emits_all_three_bullet_kinds(
    engine: None, client: AsyncClient
) -> None:
    """Gaps + daily brief + pension snapshot → gap, portfolio, signal bullets."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        # Partial intake — one field answered, others still missing so the
        # gap-tracker has something to surface.
        session.add(
            UserContext(
                user_id="ariel",
                current_stage="stage_1",
                identity_yaml="tax_residency: israel\n",
            )
        )
        # Daily brief output → drives the portfolio bullet.
        session.add(
            DailyBrief(
                user_id="ariel",
                run_at=datetime.now(UTC),
                summary_text=(
                    "NVDA up 2.4% overnight; concentration unchanged at 48%."
                ),
                news_report_json="{}",
                macro_report_json="{}",
                concentration_report_json="{}",
                plan_delta_json="{}",
            )
        )
        # Pension snapshot → drives the signal bullet.
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="123",
                fund_name="Altshuler Shaham Pension",
                return_pct_12m=8.2,
                benchmark_return_pct_12m=7.0,
                relative_to_benchmark_pct=1.2,
                snapshot_at=datetime.now(UTC),
            )
        )
        await session.commit()

    res = await client.get("/api/advisor/home-brief", params={"user_id": "ariel"})
    assert res.status_code == 200, res.text
    body = res.json()
    kinds = {b["kind"] for b in body["bullets"]}
    assert "gap" in kinds, body
    assert "portfolio" in kinds, body
    assert "signal" in kinds, body
    # Portfolio bullet should carry the daily-brief summary line.
    portfolio = next(b for b in body["bullets"] if b["kind"] == "portfolio")
    assert "NVDA" in portfolio["text"]
    # Signal bullet should mention the fund + relative-to-benchmark direction.
    signal = next(b for b in body["bullets"] if b["kind"] == "signal")
    assert "Altshuler" in signal["text"]
    assert "above" in signal["text"] or "below" in signal["text"]


@pytest.mark.asyncio
async def test_home_brief_signal_bullet_picks_pension_when_no_investor_events(
    engine: None, client: AsyncClient
) -> None:
    """When investor_events is empty, signal bullet falls back to pension snapshot."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(UserContext(user_id="ariel", current_stage="stage_1"))
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="42",
                fund_name="Migdal Kupat Gemel",
                return_pct_12m=6.5,
                benchmark_return_pct_12m=7.1,
                relative_to_benchmark_pct=-0.6,
                snapshot_at=datetime.now(UTC),
            )
        )
        await session.commit()

    res = await client.get("/api/advisor/home-brief", params={"user_id": "ariel"})
    assert res.status_code == 200, res.text
    body = res.json()
    signal = next((b for b in body["bullets"] if b["kind"] == "signal"), None)
    assert signal is not None, body
    assert "Migdal" in signal["text"]
    assert "below" in signal["text"]


@pytest.mark.asyncio
async def test_home_brief_signal_bullet_prefers_investor_event_over_pension(
    engine: None, client: AsyncClient
) -> None:
    """When both investor events and pension snapshots exist, the most recent
    investor event wins and is surfaced verbatim."""
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(UserContext(user_id="ariel", current_stage="stage_1"))
        session.add(
            PensionFundSnapshot(
                user_id="ariel",
                fund_id="42",
                fund_name="Migdal Kupat Gemel",
                return_pct_12m=6.5,
                benchmark_return_pct_12m=7.1,
                relative_to_benchmark_pct=-0.6,
                snapshot_at=now,
            )
        )
        # Insider buy — most recent.
        session.add(
            InvestorEvent(
                user_id="ariel",
                source="sec_form4",
                ticker="NVDA",
                event_kind="purchase",
                headline="Jensen Huang (officer (CEO)) bought 10,000 NVDA @ $912.34",
                occurred_at=now,
            )
        )
        # An older event so we also confirm "most recent" wins.
        from datetime import timedelta as _td
        session.add(
            InvestorEvent(
                user_id="ariel",
                source="tipranks",
                ticker="AAPL",
                event_kind="analyst_consensus",
                headline="AAPL analyst consensus — Strong Buy avg PT $245.00",
                occurred_at=now - _td(days=3),
            )
        )
        await session.commit()

    res = await client.get("/api/advisor/home-brief", params={"user_id": "ariel"})
    assert res.status_code == 200, res.text
    body = res.json()
    signal = next((b for b in body["bullets"] if b["kind"] == "signal"), None)
    assert signal is not None, body
    # Most-recent investor event wins; pension snapshot is suppressed.
    assert "Jensen Huang" in signal["text"]
    assert "NVDA" in signal["text"]
    # Confirm the pension fallback didn't fire.
    assert "Migdal" not in signal["text"]


@pytest.mark.asyncio
async def test_home_brief_signal_bullet_omitted_when_no_events_or_pension(
    engine: None, client: AsyncClient
) -> None:
    """No investor events + no pension snapshots → no signal bullet at all."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(
            UserContext(
                user_id="ariel",
                current_stage="stage_1",
                identity_yaml="tax_residency: israel\n",
            )
        )
        # Daily brief so the response isn't dominated by the intake invite.
        session.add(
            DailyBrief(
                user_id="ariel",
                run_at=datetime.now(UTC),
                summary_text="Quiet day across the watchlist.",
                news_report_json="{}",
                macro_report_json="{}",
                concentration_report_json="{}",
                plan_delta_json="{}",
            )
        )
        await session.commit()

    res = await client.get("/api/advisor/home-brief", params={"user_id": "ariel"})
    assert res.status_code == 200, res.text
    body = res.json()
    kinds = {b["kind"] for b in body["bullets"]}
    assert "signal" not in kinds, body


@pytest.mark.asyncio
async def test_home_brief_signal_bullet_cross_user_isolation(
    engine: None, client: AsyncClient
) -> None:
    """An investor event for user A must not leak into user B's home brief."""
    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(User(id="dana"))
        session.add(UserContext(user_id="ariel", current_stage="stage_1"))
        session.add(UserContext(user_id="dana", current_stage="stage_1"))
        # Event belongs ONLY to ariel.
        session.add(
            InvestorEvent(
                user_id="ariel",
                source="sec_form4",
                ticker="NVDA",
                event_kind="purchase",
                headline="Insider X bought 5,000 NVDA",
                occurred_at=now,
            )
        )
        # Dana has a pension snapshot to make the fallback path observable.
        session.add(
            PensionFundSnapshot(
                user_id="dana",
                fund_id="99",
                fund_name="Harel Pension",
                return_pct_12m=5.0,
                benchmark_return_pct_12m=6.0,
                relative_to_benchmark_pct=-1.0,
                snapshot_at=now,
            )
        )
        await session.commit()

    # Dana — must see her pension snapshot, NOT ariel's investor event.
    res_dana = await client.get(
        "/api/advisor/home-brief", params={"user_id": "dana"}
    )
    assert res_dana.status_code == 200
    body_dana = res_dana.json()
    signal_dana = next(
        (b for b in body_dana["bullets"] if b["kind"] == "signal"), None
    )
    assert signal_dana is not None
    assert "Harel" in signal_dana["text"]
    assert "NVDA" not in signal_dana["text"], (
        "ariel's investor event leaked to dana"
    )

    # Ariel — must see his own investor event.
    res_ariel = await client.get(
        "/api/advisor/home-brief", params={"user_id": "ariel"}
    )
    assert res_ariel.status_code == 200
    body_ariel = res_ariel.json()
    signal_ariel = next(
        (b for b in body_ariel["bullets"] if b["kind"] == "signal"), None
    )
    assert signal_ariel is not None
    assert "NVDA" in signal_ariel["text"]


@pytest.mark.asyncio
async def test_home_brief_caches_within_ttl(
    engine: None, client: AsyncClient
) -> None:
    """Second call within 30 minutes returns the cached payload — no
    recompute. We verify by mutating the underlying DailyBrief between
    calls and asserting the cached `summary_text` is still served."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(UserContext(user_id="ariel", current_stage="stage_1"))
        session.add(
            DailyBrief(
                user_id="ariel",
                run_at=datetime.now(UTC),
                summary_text="FIRST",
                news_report_json="{}",
                macro_report_json="{}",
                concentration_report_json="{}",
                plan_delta_json="{}",
            )
        )
        await session.commit()

    res1 = await client.get("/api/advisor/home-brief", params={"user_id": "ariel"})
    assert res1.status_code == 200
    body1 = res1.json()
    portfolio1 = next(
        (b for b in body1["bullets"] if b["kind"] == "portfolio"), None
    )
    assert portfolio1 is not None
    assert "FIRST" in portfolio1["text"]

    # Cache row should now exist.
    async with db_mod.get_session() as session:
        cached = (
            await session.execute(
                select(KvCacheEntry).where(
                    KvCacheEntry.provider == "advisor_home_brief",
                    KvCacheEntry.key == "user:ariel",
                )
            )
        ).scalar_one_or_none()
        assert cached is not None

        # Mutate the daily brief — a non-cached call would now read SECOND.
        db_row = (
            await session.execute(
                select(DailyBrief).where(DailyBrief.user_id == "ariel")
            )
        ).scalar_one()
        db_row.summary_text = "SECOND"
        await session.commit()

    res2 = await client.get("/api/advisor/home-brief", params={"user_id": "ariel"})
    assert res2.status_code == 200
    body2 = res2.json()
    portfolio2 = next(
        (b for b in body2["bullets"] if b["kind"] == "portfolio"), None
    )
    # Cache hit: still serves FIRST.
    assert portfolio2 is not None
    assert "FIRST" in portfolio2["text"]
    assert "SECOND" not in portfolio2["text"]
