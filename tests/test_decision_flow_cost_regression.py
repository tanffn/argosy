"""Wave A cost-regression smoke (Task 24).

Marked ``llm_eval`` (opt-in via ``-m llm_eval``). Re-runs the same T2 AAPL
scenario captured by ``test_decision_flow_live.py`` for the pre-Wave-A
baseline (``tests/fixtures/cost_baseline_pre_wave_a.json``), then compares
the post-Wave-A run's aggregate output tokens against the baseline.

Why output tokens and not input tokens
--------------------------------------
The pre-Wave-A baseline was captured on the ``claude_code`` backend
(Argosy's default per ``argosy.toml``). That backend reports only
``input_tokens`` / ``output_tokens`` on its ``ResultMessage`` — it does
NOT expose ``cache_read_input_tokens`` or ``cache_creation_input_tokens``,
so ``cache_input_tokens`` and ``cache_creation_tokens`` are always 0 in
the baseline. The baseline's ``total_input_tokens=39`` is therefore not
a usable denominator for a "post should be at least 30% smaller" check
(the input tokens were already under-reported pre-Wave-A — there's
nothing to "reduce"). The baseline's ``regression`` block spells this
out and steers Task 24 to use ``total_output_tokens`` instead.

Output tokens are reported accurately by both backends and remain
sensitive enough to catch the regression we actually care about: that
the post-Wave-A wiring did not somehow cause the model to spew
significantly more output (e.g. a thinking-budget misconfiguration that
leaks reasoning into the response, or a caching mis-wire that breaks
context continuity and forces the model to re-state more).

Backend gating
--------------
This test requires ``ARGOSY_ANTHROPIC__BACKEND=api_key`` plus a reachable
key. Rationale:

  - The new Wave A telemetry fields (cache_input_tokens, thinking_tokens,
    citations_json) are only populated on the api_key backend; running
    this test on claude_code would compare apples (claude_code baseline)
    to apples (claude_code post-run) but would teach us nothing about
    whether the Wave A features are actually saving tokens.
  - The cache-hit-ratio observation only makes sense when
    cache_input_tokens / cache_creation_tokens are non-zero — which only
    happens on api_key.

If the backend is ``claude_code`` or no key is reachable, the test
SKIPs cleanly (it does NOT silently pass).

Assertions
----------
  - PRIMARY (asserted): post-Wave-A total output tokens is within ±30%
    of the baseline. Catches regressions in either direction
    (model now generating more) without being so tight that LLM
    non-determinism trips the test on a normal day.

Observations (printed, NOT asserted)
------------------------------------
  - cache hit ratio = sum(cache_input_tokens) / sum(tokens_in + cache_input_tokens)
    across all post-Wave-A agent reports. Logged so we can eyeball the
    real-world savings without making the test brittle to model-side
    cache-eviction policy changes.
  - per-agent token counts pre vs post (printed as a table).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import AgentReport, ConfidenceBand, _llm_backend_available
from argosy.config import get_settings
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
    FlowConfig,
)
from argosy.decisions.tiers import Tier
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow
from argosy.state.models import User

# Shared helper lifted to conftest in Wave A finalization (Issue 2). Wave A
# telemetry (cache_input_tokens / cache_creation_tokens / thinking_tokens) is
# only populated on the api_key path; running this regression on claude_code
# would compare two claude_code totals and teach us nothing about whether the
# Wave A features are saving tokens.
from tests.conftest import _api_key_backend_available  # noqa: E402


# ---------------------------------------------------------------------------
# Re-use the analyst-report fixture shape from the baseline live test so
# the post-Wave-A run is an apples-to-apples replay of the same scenario.
# ---------------------------------------------------------------------------


def _analyst_reports_for_baseline() -> list[AgentReport]:
    """Mirror of ``tests/test_decision_flow_live.py::_analyst_reports_for_baseline``.

    Kept inline (not imported) because pytest imports test modules lazily
    and the live module has its own ``llm_eval`` markers and live setup
    side-effects we don't want to drag in. Any drift here vs the live
    module would invalidate the apples-to-apples comparison — keep these
    two functions byte-identical in their report bodies.
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
            user_id="ariel_cost_regression",
            model="claude-sonnet-4-6",
            response_text=json.dumps(out.model_dump()),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            prompt_hash="cost-regression-fixture",
            confidence=out.confidence,
            output=out,
        )

    return [_wrap(fundamentals), _wrap(technical), _wrap(sentiment)]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.llm_eval
@pytest.mark.skipif(
    not _llm_backend_available(),
    reason=(
        "No LLM backend reachable: set ANTHROPIC_API_KEY (api_key backend) "
        "or ensure claude.exe is on PATH (claude_code backend)."
    ),
)
@pytest.mark.skipif(
    not _api_key_backend_available(),
    reason=(
        "Cost-regression smoke requires the api_key backend — the "
        "claude_code SDK does not surface cache_read_input_tokens / "
        "cache_creation_input_tokens / thinking_tokens, so the post-Wave-A "
        "telemetry needed for the cache-hit-ratio witness would be empty. "
        "Set ARGOSY_ANTHROPIC__BACKEND=api_key plus ANTHROPIC_API_KEY (or "
        "configured keychain entry) to run."
    ),
)
def test_post_wave_a_output_tokens_within_30pct_of_baseline(
    alembic_engine_at_head,
) -> None:
    """Re-run the T2 AAPL baseline scenario and compare aggregate output
    tokens to the pre-Wave-A capture.

    See module docstring for the rationale on using output-token bounds
    rather than the original plan's input-token reduction assertion.
    """
    # Load the baseline JSON written by Task 2.
    baseline_path = (
        Path(__file__).resolve().parent / "fixtures" / "cost_baseline_pre_wave_a.json"
    )
    assert baseline_path.exists(), (
        f"Baseline not found at {baseline_path}. Run Task 2's live test "
        f"first to capture the pre-Wave-A cost baseline."
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_output = int(baseline["total_output_tokens"])
    baseline_input = int(baseline["total_input_tokens"])
    baseline_per_agent: list[dict] = baseline.get("per_agent", [])

    # Repoint the async engine at the alembic-fixture DB (same trick the
    # other Wave A integration tests use). The fixture set ARGOSY_HOME
    # and reloaded settings, so init_engine() picks up the right URL.
    db_mod.init_engine()

    user_id = "ariel_cost_regression"

    async def _setup_and_run() -> tuple[ApprovedProposal | BlockedProposal, int]:
        # Seed the user row (FK target for agent_reports / decision_runs).
        async with db_mod.get_session() as sess:
            sess.add(User(id=user_id, plan="free"))
            await sess.commit()

        flow = DecisionFlow(
            user_id=user_id,
            config=FlowConfig(
                # Mirror the baseline run's debate-round config so the
                # cascade depth is comparable.
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

    assert isinstance(outcome, (ApprovedProposal, BlockedProposal)), (
        f"Flow returned unexpected type: {type(outcome).__name__}"
    )
    assert decision_run_id > 0, "decision_run_id was not populated"

    # ----------------------------------------------------------------------
    # Pull per-agent telemetry from agent_reports for this decision run.
    # ----------------------------------------------------------------------

    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    with SessionLocal() as s:
        rows = s.execute(
            sa.select(
                AgentReportRow.agent_role,
                AgentReportRow.model,
                AgentReportRow.tokens_in,
                AgentReportRow.tokens_out,
                AgentReportRow.cost_usd,
                AgentReportRow.cache_input_tokens,
                AgentReportRow.cache_creation_tokens,
                AgentReportRow.thinking_tokens,
            )
            .where(AgentReportRow.user_id == user_id)
            .where(AgentReportRow.decision_id == str(decision_run_id))
            .order_by(AgentReportRow.id.asc())
        ).all()

    assert len(rows) > 0, (
        "No agent_reports rows for this decision run — flow short-circuited "
        "before any LLM call. Cannot compare to baseline."
    )

    # Aggregate post-Wave-A telemetry.
    post_output = sum(int(r.tokens_out or 0) for r in rows)
    post_input_raw = sum(int(r.tokens_in or 0) for r in rows)
    post_cache_read = sum(int(r.cache_input_tokens or 0) for r in rows)
    post_cache_creation = sum(int(r.cache_creation_tokens or 0) for r in rows)
    post_thinking = sum(int(r.thinking_tokens or 0) for r in rows)
    post_cost = sum(float(r.cost_usd or 0.0) for r in rows)

    # Cache hit ratio: cache reads / (cache reads + non-cached input tokens).
    # On the api_key backend, tokens_in is the NON-cached input chunk;
    # cache_input_tokens (cache_read_input_tokens) is the cached chunk.
    # A 0.0 ratio means caching is broken; a 0.5+ ratio means we're
    # getting solid reuse on the boilerplate system block.
    total_input_with_cache = post_input_raw + post_cache_read
    cache_hit_ratio = (
        post_cache_read / total_input_with_cache if total_input_with_cache > 0 else 0.0
    )

    # ----------------------------------------------------------------------
    # Print the comparison table (per-agent, pre vs post).
    # ----------------------------------------------------------------------

    # Build a lookup keyed by (role, occurrence_index) so multiple
    # risk_officer rows in the baseline can be matched by position.
    def _index_by_role(items: list) -> dict[str, list]:
        out: dict[str, list] = {}
        for it in items:
            role = it["role"] if isinstance(it, dict) else it.agent_role
            out.setdefault(role, []).append(it)
        return out

    baseline_by_role = _index_by_role(baseline_per_agent)
    post_by_role = _index_by_role([
        {
            "role": r.agent_role,
            "model": r.model,
            "tokens_in": int(r.tokens_in or 0),
            "tokens_out": int(r.tokens_out or 0),
            "cost_usd": float(r.cost_usd or 0.0),
            "cache_input_tokens": int(r.cache_input_tokens or 0),
            "cache_creation_tokens": int(r.cache_creation_tokens or 0),
            "thinking_tokens": int(r.thinking_tokens or 0),
        }
        for r in rows
    ])

    all_roles = sorted(set(baseline_by_role) | set(post_by_role))
    print("\n=== Per-agent token comparison (pre-Wave-A vs post-Wave-A) ===")
    header = (
        f"{'role':<22} {'idx':>3}  "
        f"{'pre_in':>7} {'pre_out':>8}  "
        f"{'post_in':>8} {'post_out':>8}  "
        f"{'cache_r':>7} {'cache_c':>7} {'think':>6}"
    )
    print(header)
    print("-" * len(header))
    for role in all_roles:
        pre_list = baseline_by_role.get(role, [])
        post_list = post_by_role.get(role, [])
        n = max(len(pre_list), len(post_list))
        for i in range(n):
            pre = pre_list[i] if i < len(pre_list) else None
            post = post_list[i] if i < len(post_list) else None
            pre_in = pre["tokens_in"] if pre else 0
            pre_out = pre["tokens_out"] if pre else 0
            post_in = post["tokens_in"] if post else 0
            post_out = post["tokens_out"] if post else 0
            cache_r = post["cache_input_tokens"] if post else 0
            cache_c = post["cache_creation_tokens"] if post else 0
            think = post["thinking_tokens"] if post else 0
            print(
                f"{role:<22} {i:>3}  "
                f"{pre_in:>7} {pre_out:>8}  "
                f"{post_in:>8} {post_out:>8}  "
                f"{cache_r:>7} {cache_c:>7} {think:>6}"
            )

    print("\n=== Rollup ===")
    print(f"  baseline_output      = {baseline_output:>8,}  (primary signal)")
    print(f"  post_output          = {post_output:>8,}")
    output_delta_pct = (
        (post_output - baseline_output) / baseline_output * 100
        if baseline_output > 0
        else float("inf")
    )
    print(
        f"  delta                = {post_output - baseline_output:>+8,}  "
        f"({output_delta_pct:+.1f}% vs baseline)"
    )
    print(f"  baseline_input       = {baseline_input:>8,}  (cache-witness only)")
    print(f"  post_input_raw       = {post_input_raw:>8,}  (uncached chunk)")
    print(f"  post_cache_read      = {post_cache_read:>8,}")
    print(f"  post_cache_creation  = {post_cache_creation:>8,}")
    print(f"  post_thinking_tokens = {post_thinking:>8,}")
    print(f"  cache_hit_ratio      = {cache_hit_ratio:>8.3f}")
    print(f"  post_cost_usd        = ${post_cost:.4f}")

    # ----------------------------------------------------------------------
    # PRIMARY assertion: post output tokens within ±30% of baseline.
    #
    # This is a regression detector, not an improvement detector. We want
    # to catch the case where Wave A wiring went wrong and the model
    # started spewing materially more output (e.g. thinking bleeding into
    # the response, or caching breakage forcing context re-statement). A
    # ±30% band is wide enough to absorb normal LLM non-determinism but
    # tight enough that a 2x explosion in output trips the test.
    # ----------------------------------------------------------------------

    lower_bound = int(baseline_output * 0.70)
    upper_bound = int(baseline_output * 1.30)
    assert lower_bound <= post_output <= upper_bound, (
        f"Post-Wave-A output tokens ({post_output:,}) are outside ±30% of "
        f"baseline ({baseline_output:,}): allowed band "
        f"[{lower_bound:,} .. {upper_bound:,}]. "
        f"Delta {output_delta_pct:+.1f}%. Possible causes: "
        f"(a) thinking budget mis-configured and bleeding into response; "
        f"(b) caching breakage forcing model to re-state context; "
        f"(c) outcome_kind diverged from baseline (cascade depth differs); "
        f"(d) prompt regression after a refactor."
    )

    # ----------------------------------------------------------------------
    # SECONDARY observation (not asserted): cache hit ratio.
    #
    # On the api_key backend, the boilerplate system block is marked
    # cache_control: ephemeral. After the first call in a decision flow,
    # subsequent calls within the 5-min TTL should see meaningful cache
    # reads. A non-trivial ratio (>0.20) is qualitative evidence that
    # caching is wired correctly. We log this rather than assert it
    # because the SDK / model-side cache-eviction policy can change and
    # we don't want this smoke test to break on policy drift.
    # ----------------------------------------------------------------------

    print(
        f"\n[witness] cache_hit_ratio={cache_hit_ratio:.3f} "
        f"({'CACHING ACTIVE' if cache_hit_ratio > 0.20 else 'CACHING WEAK OR DISABLED'})"
    )

    # Diagnostics: capture git SHA + backend so the test output is
    # self-describing in CI logs.
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short=10", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        git_sha = "unknown"
    try:
        backend = get_settings().anthropic.backend
    except Exception:
        backend = "unknown"
    print(
        f"\n[meta] git_sha={git_sha} backend={backend} "
        f"baseline_sha={baseline.get('git_sha')} "
        f"baseline_backend={baseline.get('anthropic_backend')}"
    )
