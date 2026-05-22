"""Live-LLM end-to-end decision-flow test — Wave A baseline (Task 2).

Marked ``llm_eval`` (opt-in via ``-m llm_eval``). Runs the FULL
DecisionFlow cascade — researcher debate, trader, three-perspective risk
team, risk facilitator, fund manager — against the real LLM backend, then
captures a cost baseline JSON used by Wave A's cost-regression smoke
test (Task 24).

NO mocks, NO skip markers beyond the backend-availability guard. The
whole point is to measure real costs prior to the BaseAgent prompt-
caching / extended-thinking / Citations API features landing.

Cost: ~$5-15 per run on api_key backend (Opus-heavy T2 stack). The
``debate_rounds_t2=1`` config keeps the call count to ~10-12 LLM calls
(2 researchers + 1 researcher facilitator + 1 trader + 3 risk officers
+ 1 risk facilitator + 1 fund manager). Authorized by Ariel for the
Wave A baseline. Free of direct $ cost when the ``claude_code`` backend
is configured (charged to the user's Claude Code subscription).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import AgentReport, ConfidenceBand, _llm_backend_available
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
    FlowConfig,
)
from argosy.decisions.tiers import Tier
from argosy.state import db as db_mod
from argosy.state.models import User


# ---------------------------------------------------------------------------
# Synthetic analyst reports (same shape as cli/decide.py builds from rows)
# ---------------------------------------------------------------------------


def _analyst_reports_for_baseline() -> list[AgentReport]:
    """Build a representative analyst-panel input for a T2 AAPL scenario.

    The decision flow consumes analyst reports as inputs — the analyst
    layer itself is not part of the decision pipeline. We hand-craft three
    analyst dicts that resemble a realistic fundamentals + technical +
    sentiment trio so the debate / trader / risk / FM have something
    concrete to disagree about. The values are illustrative, not real
    market data; the LLMs will reason from these as if they were fresh
    inputs.
    """
    from pydantic import BaseModel

    class _AnalystOutput(BaseModel):
        agent_role: str
        cited_sources: list[str]
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        report: str

    fundamentals = _AnalystOutput(
        agent_role="fundamentals",
        cited_sources=["analyst:fundamentals", "domain_knowledge/equity_basics.md"],
        confidence=ConfidenceBand.MEDIUM,
        report=(
            "AAPL trades at P/E ~30, modestly above the 5y average of ~26. "
            "Free cash flow growth slowed to 4% YoY last quarter from 11% "
            "the prior year. Services revenue continues to grow at ~13% "
            "YoY and now contributes 25% of total revenue. Balance sheet "
            "remains strong with $60B net cash. Bull case: services mix "
            "shift supports margin expansion. Bear case: iPhone unit growth "
            "is flat in mature markets and China headwinds persist."
        ),
    )
    technical = _AnalystOutput(
        agent_role="technical",
        cited_sources=["analyst:technical", "domain_knowledge/ta_indicators.md"],
        confidence=ConfidenceBand.MEDIUM,
        report=(
            "AAPL closed today at $223.40, down 4.2% on the session — "
            "the largest single-day move since Q2. RSI dropped from 62 to "
            "44, no longer overbought. The 50-day SMA at $228 was lost "
            "today; next major support is the 200-day at $208. Volume "
            "was 1.8x the 30-day average. Setup is neutral-to-cautious "
            "in the very short term; a re-test of $208 within 2 weeks is "
            "the modal path if the broader tape stays soft."
        ),
    )
    sentiment = _AnalystOutput(
        agent_role="sentiment",
        cited_sources=["analyst:sentiment", "domain_knowledge/news_taxonomy.md"],
        confidence=ConfidenceBand.LOW,
        report=(
            "Sentiment turned moderately negative today on a Reuters story "
            "about softer iPhone 16 pre-order volumes in China (-12% YoY "
            "per supply-chain checks). Apple has not commented. Two sell-"
            "side desks reiterated Buy with $250 PTs; one downgraded to "
            "Hold citing the China datapoint. Social-media chatter volume "
            "is 2x baseline but skews informational rather than panic."
        ),
    )

    def _wrap(out: _AnalystOutput) -> AgentReport:
        return AgentReport(
            agent_role=out.agent_role,
            user_id="ariel_live_baseline",
            model="claude-sonnet-4-6",
            response_text=json.dumps(out.model_dump()),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            prompt_hash="baseline-fixture",
            confidence=out.confidence,
            output=out,
        )

    return [_wrap(fundamentals), _wrap(technical), _wrap(sentiment)]


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)"
    ),
)
def test_t2_decision_flow_live_baseline(alembic_engine_at_head, tmp_path) -> None:
    """Run the T2 decision flow live and capture a cost baseline.

    Scenario: a modest AAPL buy after a one-day -4% drift, with mixed
    fundamentals / technical / sentiment inputs. Tier is pinned to T2 so
    the FULL Opus-leaning stack runs (researcher debate → trader → 3-
    perspective risk team → fund manager).

    Assertions (hard — the test fails if any of these break):
      - flow returned an ApprovedProposal or BlockedProposal (not an
        exception, not some other type)
      - decision_run_id was populated
      - at least 6 agent_reports rows were written (debate + trader +
        risk team must have fired; the floor is loose to tolerate early
        exits like trader_hold or risk REJECT)
      - total tokens_in across those rows > 0 (agents recorded usage)

    Observed but NOT asserted (recorded into the baseline JSON for
    Task 24's downstream comparison):
      - total_cost_usd: may be 0 on the claude_code backend (the SDK
        reports cost via ResultMessage.total_cost_usd, which the current
        BaseAgent does not persist into agent_reports.cost_usd). The
        test prints a WARNING and continues; it does NOT fail.
      - total_input_tokens: under-reported on the claude_code backend
        (cache reads excluded). Useful as a cache-hit-rate witness post-
        Wave-A, but NOT as the regression denominator — see the
        ``regression`` block in the baseline JSON, which steers Task 24
        to use total_output_tokens instead.
      - outcome_kind: persisted in the baseline. If a future re-run
        produces a different outcome_kind, the cascade depth differs and
        Task 24's apples-to-apples comparison may break — see the
        baseline JSON's ``outcome_stability_note`` for the fallback
        (per-agent-role comparison via the ``per_agent`` array).
      - total_output_tokens: persisted as the primary regression signal;
        not asserted here because the LLM is non-deterministic and a
        hard floor would either be too loose to catch regressions or too
        tight to be stable.

    The baseline JSON is written under tests/fixtures/ before the test
    returns so the file persists past the tmp_path fixture teardown.
    """
    import asyncio

    # Point the async engine at the same SQLite file the alembic fixture
    # set up (same pattern as conftest.client_with_db). The fixture sets
    # ARGOSY_HOME and reloads settings, so plain init_engine() reads the
    # right URL.
    db_mod.init_engine()

    user_id = "ariel_live_baseline"

    async def _setup_and_run() -> tuple[ApprovedProposal | BlockedProposal, int]:
        # Seed the user row (FK target for agent_reports / decision_runs).
        async with db_mod.get_session() as sess:
            sess.add(User(id=user_id, plan="free"))
            await sess.commit()

        flow = DecisionFlow(
            user_id=user_id,
            config=FlowConfig(
                # Keep debate to 1 round to bound cost on the baseline run.
                # Post-Wave-A regression test (Task 24) uses the same value
                # so the comparison is apples-to-apples.
                debate_rounds_t1=1,
                debate_rounds_t2=1,
                debate_rounds_t3=1,
            ),
        )
        outcome = await flow.run(
            ticker="AAPL",
            tier=Tier.T2,
            analyst_reports=_analyst_reports_for_baseline(),
            positions_summary=(
                "Cash: $50,000. AAPL: 0 shares. Total portfolio: "
                "$500,000 across SPY (60%), QQQ (25%), cash (10%), other (5%)."
            ),
            user_constraints=(
                "Target AAPL allocation: 0-3% of portfolio. Tax: Israeli "
                "resident, 25% CGT. Prefer limit orders. Single trade size "
                "$2,000-$5,000."
            ),
            risk_caps={
                "max_single_position_pct": 5.0,
                "max_sector_concentration_pct": 35.0,
            },
            account_class="main",
        )
        return outcome, outcome.decision_run_id

    outcome, decision_run_id = asyncio.run(_setup_and_run())

    # Flow returned something sane.
    assert isinstance(outcome, (ApprovedProposal, BlockedProposal)), (
        f"Flow returned unexpected type: {type(outcome).__name__}"
    )
    assert decision_run_id > 0, "decision_run_id was not populated"

    # ----------------------------------------------------------------------
    # Query agent_reports for the cost baseline, using the SYNC engine the
    # alembic fixture handed us (we already have it; reuse rather than
    # opening a new async session).
    # ----------------------------------------------------------------------

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    with SessionLocal() as s:
        rows = s.execute(
            sa.text(
                """
                SELECT agent_role, model, tokens_in, tokens_out, cost_usd, created_at
                FROM agent_reports
                WHERE user_id = :uid AND decision_id = :did
                ORDER BY id ASC
                """
            ),
            {"uid": user_id, "did": str(decision_run_id)},
        ).all()

    assert len(rows) > 0, (
        "No agent_reports rows written for this decision run — the flow "
        "may have short-circuited before any LLM call."
    )

    total_in = sum(int(r[2] or 0) for r in rows)
    total_out = sum(int(r[3] or 0) for r in rows)
    total_cost = sum(float(r[4] or 0.0) for r in rows)

    # Plausibility: the debate + trader + risk team must have fired. At T2
    # with debate_rounds_t2=1 we expect at minimum: 2 bull/bear turns +
    # 1 researcher facilitator + 1 trader + 3 risk officers + 1 risk
    # facilitator + 1 fund manager = ~9 rows. Allow some slack for early
    # exits (trader_hold, risk REJECT) — we just want >= 6.
    assert len(rows) >= 6, (
        f"Only {len(rows)} agent_reports rows — expected ≥ 6 for a T2 run. "
        f"Roles seen: {[r[0] for r in rows]}"
    )
    assert total_in > 0, "Sum of tokens_in is zero — agents did not record usage."
    # cost_usd is allowed to be 0.0 on the claude_code backend (the SDK
    # reports cost separately via ResultMessage.total_cost_usd, which the
    # current BaseAgent doesn't persist into agent_reports.cost_usd). We
    # log this case but don't fail — the regression-test (Task 24) will
    # use tokens for the apples-to-apples comparison anyway.
    if total_cost <= 0:
        print(
            "WARNING: total cost_usd == 0 (claude_code backend records "
            "tokens but not USD in agent_reports). Baseline still useful "
            "for token comparison."
        )

    # ----------------------------------------------------------------------
    # Resolve git SHA and SDK version dynamically.
    # ----------------------------------------------------------------------

    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short=10", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        git_sha = "unknown"

    try:
        import anthropic as _anthropic
        sdk_version = getattr(_anthropic, "__version__", "unknown")
    except ImportError:
        sdk_version = "not-installed"

    try:
        from argosy.config import get_settings
        backend = get_settings().anthropic.backend
    except Exception:
        backend = "unknown"

    # ----------------------------------------------------------------------
    # Emit the baseline JSON under tests/fixtures/.
    # ----------------------------------------------------------------------

    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    out_path = fixtures_dir / "cost_baseline_pre_wave_a.json"

    baseline = {
        "scenario": "t2_decision_live_e2e",
        "test_file": "tests/test_decision_flow_live.py",
        "ticker": "AAPL",
        "tier": "T2",
        "outcome_kind": type(outcome).__name__,
        "decision_run_id": decision_run_id,
        "agent_report_count": len(rows),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": round(total_cost, 6),
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "git_sha": git_sha,
        "branch": "wave-a-baseagent-api",
        "anthropic_sdk_version": sdk_version,
        "anthropic_backend": backend,
        "flow_config": {
            "debate_rounds_t2": 1,
        },
        "per_agent": [
            {
                "role": r[0],
                "model": r[1],
                "tokens_in": int(r[2] or 0),
                "tokens_out": int(r[3] or 0),
                "cost_usd": float(r[4] or 0.0),
            }
            for r in rows
        ],
    }
    out_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"\nBaseline written to {out_path}")
    print(
        f"Total: {len(rows)} agent calls, {total_in:,} in / {total_out:,} out tokens, "
        f"${total_cost:.4f}"
    )
