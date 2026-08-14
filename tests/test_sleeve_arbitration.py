"""Tests for the sleeve-level arbitration agent and orchestration module.

Tests are pure-seam: no LLM, no network, no live DB (session fixture uses an
isolated tmp SQLite at alembic head via the shared conftest).

Covers:
  1. SleeveArbitrationAgent.build_prompt — structure, source keys, required content.
  2. _check_conservation_invariant — deterministic arithmetic.
  3. detect_redundant_sleeve_clusters — correct grouping by sleeve key.
  4. run_sleeve_arbitration with an injected fake agent — happy path, single-
     instrument skip, agent-error handling, conservation violation.
  5. Supersession: writing arbitration verdicts supersedes the prior per-
     instrument TRIM rows.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import ConfidenceBand
from argosy.agents.sleeve_arbitration_agent import (
    InstrumentDisposition,
    SleeveArbitrationAgent,
    SleeveArbitrationReport,
)
from argosy.services.decision_funnel.sleeve_arbitration import (
    SleeveCluster,
    SleeveArbitrationOutcome,
    _check_conservation_invariant,
    detect_redundant_sleeve_clusters,
    run_sleeve_arbitration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_report(
    *,
    sleeve_key: str = "Equity/Broad Index/Global",
    keep_ticker: str = "FWRA",
    tickers: list[str] | None = None,
    conviction: ConfidenceBand = ConfidenceBand.MEDIUM,
    conservation_assertion: str = "Total sleeve exposure is preserved in the kept vehicle.",
) -> SleeveArbitrationReport:
    """Build a valid SleeveArbitrationReport for testing."""
    tks = tickers or ["FWRA", "ACWD", "MSCI WORLD"]
    dispositions = []
    for tk in tks:
        action = "KEEP" if tk == keep_ticker else "SELL"
        dispositions.append(
            InstrumentDisposition(
                ticker=tk,
                action=action,
                conviction=conviction,
                rationale=f"{tk} rationale",
            )
        )
    return SleeveArbitrationReport(
        sleeve_key=sleeve_key,
        keep_ticker=keep_ticker,
        dispositions=dispositions,
        conservation_assertion=conservation_assertion,
        reasoning_md=f"FWRA is the largest position and is estate-safe (IE UCITS). "
                     f"Consolidating into FWRA preserves global-equity sleeve exposure "
                     f"while eliminating parallel all-world trackers ACWD and MSCI WORLD.",
        falsifiers=[
            "A lower-cost UCITS MSCI All-World vehicle with TER < current FWRA bps enters "
            "the Gemelnet fund platform.",
            "FWRA AUM drops below $1B, triggering closure risk.",
        ],
        revisit_triggers=[
            {"kind": "dated_event", "label": "annual fund review", "date": "2027-08-01"},
        ],
        data_gaps=["TER not supplied for any cluster member — cost comparison unavailable."],
        confidence=conviction,
        cited_sources=[f"sleeve_context/{sleeve_key.replace('/', '_')}"],
    )


@dataclass
class _FakeAgentReport:
    """Minimal AgentReport wrapper for the fake agent."""
    output: SleeveArbitrationReport
    agent_role: str = "sleeve_arbitration"
    prompt_hash: str = "fakehash"
    response_text: str = ""
    tokens_in: int = 100
    tokens_out: int = 200
    cost_usd: float = 0.0
    model: str = "claude-opus-4-8"
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cache_input_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_tokens: int = 0
    citations_json: str | None = None
    sources_json: str | None = None
    run_correlation_id: str = "test-run"
    system_prompt: str = ""
    user_prompt: str = ""


class _FakeAgent:
    """Stand-in for SleeveArbitrationAgent (no LLM)."""

    def __init__(self, report: SleeveArbitrationReport):
        self._report = report

    async def run(self, **_kwargs) -> _FakeAgentReport:
        return _FakeAgentReport(self._report)


class _FailingAgent:
    async def run(self, **_kwargs):
        raise RuntimeError("LLM timeout")


@pytest.fixture
def session(alembic_engine_at_head):
    from argosy.state.models import User
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _seed_verdict(session, *, user_id: str, subject: str, verdict: str,
                  reasoning_md: str = "") -> Any:
    """Insert a settled verdict row and return it."""
    from argosy.services.verdict_registry import write_verdict
    from datetime import date
    v = write_verdict(
        session,
        user_id=user_id,
        subject=subject,
        verdict=verdict,
        conviction="LOW",
        falsifiers=["Some falsifier."],
        revisit_triggers=[{"kind": "dated_event", "label": "review", "date": "2027-01-01"}],
        next_validation=date(2027, 1, 1),
        reasoning_md=reasoning_md,
        settled=True,
    )
    session.commit()
    return v


# ---------------------------------------------------------------------------
# 1. SleeveArbitrationAgent.build_prompt
# ---------------------------------------------------------------------------

class TestSleeveArbitrationAgentBuildPrompt:
    TICKERS = ["FWRA", "ACWD", "MSCI WORLD"]
    SLEEVE_KEY = "Equity/Broad Index/Global"

    def _agent(self) -> SleeveArbitrationAgent:
        return SleeveArbitrationAgent(user_id="ariel")

    def _instruments(self) -> list[dict[str, Any]]:
        return [
            {
                "ticker": "FWRA",
                "asset_class": "Equity",
                "sector": "Broad Index",
                "region": "Global",
                "estate_safe": True,
                "domicile_country": "IE",
                "position_usd_value": 96400.0,
                "position_weight_pct": 2.29,
                "prior_verdict": "TRIM",
                "prior_verdict_reasoning": "Overlaps ACWD.",
                "overlap_instruments": ["ACWD", "MSCI WORLD"],
            },
            {
                "ticker": "ACWD",
                "asset_class": "Equity",
                "sector": "Broad Index",
                "region": "Global",
                "estate_safe": True,
                "domicile_country": "IE",
                "position_usd_value": 83370.0,
                "position_weight_pct": 1.98,
                "prior_verdict": "TRIM",
                "prior_verdict_reasoning": "Overlaps FWRA.",
                "overlap_instruments": ["FWRA", "MSCI WORLD"],
            },
            {
                "ticker": "MSCI WORLD",
                "asset_class": "Equity",
                "sector": "Broad Index",
                "region": "Global",
                "estate_safe": True,
                "domicile_country": "IE",
                "position_usd_value": 36017.0,
                "position_weight_pct": 0.85,
                "prior_verdict": "TRIM",
                "prior_verdict_reasoning": "Overlaps FWRA and ACWD.",
                "overlap_instruments": ["FWRA", "ACWD"],
            },
        ]

    def test_returns_3_tuple(self):
        agent = self._agent()
        result = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        assert len(result) == 3, "build_prompt must return (system, user, sources)"

    def test_sources_contain_sleeve_context_key(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        source_ids = [sid for sid, _ in sources]
        assert "sleeve_context/Equity_Broad Index_Global" in source_ids

    def test_domain_knowledge_source_present_when_supplied(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
            domain_knowledge="Estate tax info.",
        )
        source_ids = [sid for sid, _ in sources]
        assert "domain_knowledge/tax" in source_ids

    def test_domain_knowledge_absent_when_empty(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
            domain_knowledge="",
        )
        source_ids = [sid for sid, _ in sources]
        assert "domain_knowledge/tax" not in source_ids

    def test_system_prompt_mentions_consolidation(self):
        agent = self._agent()
        system, _, _ = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        assert "KEEP" in system and "SELL" in system

    def test_system_prompt_forbids_fabrication(self):
        agent = self._agent()
        system, _, _ = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        assert "fabricate" in system.lower() or "not supplied" in system.lower()

    def test_user_prompt_names_all_tickers(self):
        agent = self._agent()
        _, user, _ = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        for tk in self.TICKERS:
            assert tk in user, f"Expected {tk!r} in user prompt"

    def test_context_block_contains_position_values(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        context_text = "\n".join(text for _, text in sources)
        assert "96400" in context_text  # FWRA USD value

    def test_prior_verdict_reasoning_in_context(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(
            sleeve_key=self.SLEEVE_KEY,
            instruments=self._instruments(),
        )
        context_text = "\n".join(text for _, text in sources)
        assert "Overlaps ACWD" in context_text


# ---------------------------------------------------------------------------
# 2. _check_conservation_invariant (deterministic arithmetic)
# ---------------------------------------------------------------------------

class TestCheckConservationInvariant:
    def _report(self, keep: str = "FWRA",
                actions: dict[str, str] | None = None) -> SleeveArbitrationReport:
        tks = ["FWRA", "ACWD", "MSCI WORLD"]
        acts = actions or {"FWRA": "KEEP", "ACWD": "SELL", "MSCI WORLD": "SELL"}
        dispositions = [
            InstrumentDisposition(ticker=tk, action=acts.get(tk, "SELL"),
                                  conviction=ConfidenceBand.MEDIUM, rationale="r")
            for tk in tks
        ]
        return SleeveArbitrationReport(
            sleeve_key="Equity/Broad Index/Global",
            keep_ticker=keep,
            dispositions=dispositions,
            conservation_assertion="Total sleeve exposure is preserved.",
            reasoning_md="Consolidated.",
            confidence=ConfidenceBand.MEDIUM,
        )

    def test_valid_ruling_passes(self):
        ok, msg = _check_conservation_invariant(self._report())
        assert ok, f"Expected OK, got: {msg}"
        assert "FWRA" in msg

    def test_no_keep_disposition_fails(self):
        ok, msg = _check_conservation_invariant(
            self._report(actions={"FWRA": "SELL", "ACWD": "SELL", "MSCI WORLD": "SELL"})
        )
        assert not ok
        assert "No KEEP" in msg

    def test_multiple_keep_dispositions_fails(self):
        ok, msg = _check_conservation_invariant(
            self._report(actions={"FWRA": "KEEP", "ACWD": "KEEP", "MSCI WORLD": "SELL"})
        )
        assert not ok
        assert "Multiple KEEP" in msg

    def test_keep_ticker_disagreement_fails(self):
        """keep_ticker field disagrees with the KEEP disposition."""
        report = self._report(keep="ACWD",  # declared keep is ACWD
                              actions={"FWRA": "KEEP", "ACWD": "SELL", "MSCI WORLD": "SELL"})
        # keep_ticker=ACWD but the KEEP disposition is FWRA
        ok, msg = _check_conservation_invariant(report)
        assert not ok
        assert "disagrees" in msg

    def test_empty_keep_ticker_still_passes_when_disposition_has_keep(self):
        """keep_ticker='' is allowed; only the disposition is checked."""
        report = self._report(keep="",
                              actions={"FWRA": "KEEP", "ACWD": "SELL", "MSCI WORLD": "SELL"})
        ok, _ = _check_conservation_invariant(report)
        assert ok


# ---------------------------------------------------------------------------
# 3. detect_redundant_sleeve_clusters (deterministic)
# ---------------------------------------------------------------------------

class TestDetectRedundantSleeveClusters:
    def test_two_global_trim_verdicts_form_cluster(self, session):
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="TRIM")
        _seed_verdict(session, user_id="ariel", subject="ACWD", verdict="TRIM")
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.sleeve_key == "Equity/Broad Index/Global"
        assert set(cluster.tickers) == {"FWRA", "ACWD"}

    def test_single_instrument_trim_not_a_cluster(self, session):
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="TRIM")
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        assert clusters == []

    def test_three_global_instruments_single_cluster(self, session):
        for subj in ["FWRA", "ACWD", "MSCI WORLD"]:
            _seed_verdict(session, user_id="ariel", subject=subj, verdict="TRIM")
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        assert len(clusters) == 1
        assert set(clusters[0].tickers) == {"FWRA", "ACWD", "MSCI WORLD"}

    def test_hold_verdict_not_included_in_cluster(self, session):
        """HOLD verdicts are not redundancy-flavoured; they must not trigger arbitration."""
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="HOLD")
        _seed_verdict(session, user_id="ariel", subject="ACWD", verdict="TRIM")
        # Only one TRIM → no cluster
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        assert clusters == []

    def test_different_sleeves_not_merged(self, session):
        """Global broad-index and US broad-index are different sleeves."""
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="TRIM")   # Global
        _seed_verdict(session, user_id="ariel", subject="ACWD", verdict="TRIM")   # Global
        _seed_verdict(session, user_id="ariel", subject="XZEW", verdict="TRIM")   # US
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        # Global cluster: FWRA + ACWD (2 instruments) → qualifies
        # US cluster: XZEW only (1 instrument) → does not qualify
        assert len(clusters) == 1
        assert clusters[0].sleeve_key == "Equity/Broad Index/Global"

    def test_unknown_instrument_skipped(self, session):
        """An instrument not in instrument_reference is skipped (no lookup)."""
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="TRIM")
        _seed_verdict(session, user_id="ariel", subject="UNKN123", verdict="TRIM")
        # UNKN123 not in instrument_reference → no cluster formed with FWRA
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        assert clusters == []  # FWRA alone in Global → no cluster

    def test_sell_verdicts_also_trigger_cluster(self, session):
        """SELL verdicts are redundancy-flavoured (same as TRIM)."""
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="SELL")
        _seed_verdict(session, user_id="ariel", subject="ACWD", verdict="TRIM")
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        assert len(clusters) == 1

    def test_verdict_rows_populated(self, session):
        """The cluster must carry the actual Verdict ORM rows."""
        _seed_verdict(session, user_id="ariel", subject="FWRA", verdict="TRIM")
        _seed_verdict(session, user_id="ariel", subject="ACWD", verdict="TRIM")
        clusters = detect_redundant_sleeve_clusters(session, user_id="ariel")
        cluster = clusters[0]
        assert "FWRA" in cluster.verdict_rows
        assert cluster.verdict_rows["FWRA"].verdict == "TRIM"


# ---------------------------------------------------------------------------
# 4. run_sleeve_arbitration (fake agent, no LLM)
# ---------------------------------------------------------------------------

class TestRunSleeveArbitration:
    """Integration-style tests with an injected fake agent (no LLM).

    DB seams (context builder, verdict writer) are patched so the production
    DB is never read from or written to.
    """

    CLUSTER = SleeveCluster(
        sleeve_key="Equity/Broad Index/Global",
        asset_class="Equity",
        sector="Broad Index",
        region="Global",
        tickers=["FWRA", "ACWD", "MSCI WORLD"],
        verdict_rows={
            "FWRA": MagicMock(id=68, verdict="TRIM", reasoning_md="Overlaps ACWD."),
            "ACWD": MagicMock(id=70, verdict="TRIM", reasoning_md="Overlaps FWRA."),
            "MSCI WORLD": MagicMock(id=75, verdict="TRIM", reasoning_md="Overlaps both."),
        },
    )

    def _run(
        self,
        cluster: SleeveCluster | None = None,
        report: SleeveArbitrationReport | None = None,
        written_ids: dict[str, int] | None = None,
    ) -> SleeveArbitrationOutcome:
        c = cluster or self.CLUSTER
        rep = report or _fake_report(tickers=c.tickers)
        fake_agent = _FakeAgent(rep)
        _written = written_ids if written_ids is not None else {"FWRA": 101, "ACWD": 102, "MSCI WORLD": 103}

        async def _async_run():
            with (
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration._build_cluster_context",
                    return_value=[{"ticker": tk} for tk in c.tickers],
                ),
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration._load_domain_knowledge",
                    return_value="[domain knowledge placeholder]",
                ),
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration._write_arbitration_verdicts",
                    return_value=_written,
                ),
                # Patch the sessionmaker and engine so DB isn't needed.
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration.sa",
                    wraps=sa,
                ),
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration.sessionmaker",
                    return_value=MagicMock(return_value=MagicMock(
                        execute=MagicMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))),
                        close=MagicMock(),
                    )),
                ),
            ):
                return await run_sleeve_arbitration(
                    c,
                    user_id="ariel",
                    _agent_factory=lambda: fake_agent,
                )

        return asyncio.run(_async_run())

    def test_happy_path_status_completed(self):
        outcome = self._run()
        assert outcome.status == "completed", f"Got: {outcome}"

    def test_happy_path_keep_ticker(self):
        outcome = self._run()
        assert outcome.keep_ticker == "FWRA"

    def test_happy_path_written_verdict_ids(self):
        outcome = self._run()
        assert outcome.written_verdict_ids == {"FWRA": 101, "ACWD": 102, "MSCI WORLD": 103}

    def test_happy_path_conservation_ok(self):
        outcome = self._run()
        assert outcome.conservation_ok is True

    def test_single_instrument_cluster_is_skipped(self):
        single = SleeveCluster(
            sleeve_key="Equity/Broad Index/Global",
            asset_class="Equity",
            sector="Broad Index",
            region="Global",
            tickers=["FWRA"],
            verdict_rows={"FWRA": MagicMock(id=1, verdict="TRIM", reasoning_md="")},
        )
        outcome = asyncio.run(
            run_sleeve_arbitration(single, user_id="ariel")
        )
        assert outcome.status == "skipped"

    def test_agent_error_returns_error_status(self):
        async def _async_run():
            with (
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration._build_cluster_context",
                    return_value=[],
                ),
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration._load_domain_knowledge",
                    return_value="",
                ),
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration.sa",
                    wraps=sa,
                ),
                patch(
                    "argosy.services.decision_funnel.sleeve_arbitration.sessionmaker",
                    return_value=MagicMock(return_value=MagicMock(
                        close=MagicMock(),
                    )),
                ),
            ):
                return await run_sleeve_arbitration(
                    self.CLUSTER,
                    user_id="ariel",
                    _agent_factory=lambda: _FailingAgent(),
                )

        outcome = asyncio.run(_async_run())
        assert outcome.status == "error"
        assert "LLM timeout" in outcome.error

    def test_conservation_violation_returns_error(self):
        """A report with no KEEP disposition must trigger conservation error."""
        bad_report = SleeveArbitrationReport(
            sleeve_key="Equity/Broad Index/Global",
            keep_ticker="FWRA",
            dispositions=[
                # All SELL — no KEEP
                InstrumentDisposition(ticker="FWRA", action="SELL",
                                      conviction=ConfidenceBand.LOW, rationale="r"),
                InstrumentDisposition(ticker="ACWD", action="SELL",
                                      conviction=ConfidenceBand.LOW, rationale="r"),
                InstrumentDisposition(ticker="MSCI WORLD", action="SELL",
                                      conviction=ConfidenceBand.LOW, rationale="r"),
            ],
            conservation_assertion="Total sleeve exposure is preserved.",
            reasoning_md="Bad ruling.",
            confidence=ConfidenceBand.LOW,
        )
        outcome = self._run(report=bad_report)
        assert outcome.status == "error"
        assert "Conservation invariant violated" in outcome.error

    def test_report_md_contains_keep_ticker(self):
        outcome = self._run()
        assert "FWRA" in outcome.report_md

    def test_report_md_contains_conservation_statement(self):
        outcome = self._run()
        assert "CONSERVATION" in outcome.report_md or "preserved" in outcome.report_md.lower()


# ---------------------------------------------------------------------------
# 5. Supersession via write_verdict (integration: uses the live alembic DB)
# ---------------------------------------------------------------------------

class TestSupersessionViaRegistry:
    """Verify that writing an arbitration verdict supersedes the prior TRIM row.

    Uses the real write_verdict path against the alembic-head test DB.
    """

    def test_arbitration_verdict_supersedes_prior_trim(self, session):
        """write_verdict for FWRA with HOLD must mark the prior TRIM as superseded."""
        from argosy.services.verdict_registry import write_verdict, get_settled_verdict
        from argosy.state.models import Verdict as VerdictModel
        from datetime import date

        # Seed a TRIM verdict
        prior = write_verdict(
            session,
            user_id="ariel",
            subject="FWRA",
            verdict="TRIM",
            conviction="LOW",
            falsifiers=["Falsifier A."],
            revisit_triggers=[{"kind": "dated_event", "label": "review", "date": "2027-01-01"}],
            next_validation=date(2027, 1, 1),
            reasoning_md="Per-instrument: overlaps ACWD.",
            settled=True,
        )
        session.commit()
        prior_id = prior.id

        # Write the arbitration verdict (HOLD = KEEP in the registry vocabulary)
        new_v = write_verdict(
            session,
            user_id="ariel",
            subject="FWRA",
            verdict="HOLD",
            conviction="MED",
            falsifiers=["Sleeve arbitration falsifier."],
            revisit_triggers=[{"kind": "dated_event", "label": "annual review", "date": "2027-08-01"}],
            next_validation=date(2027, 8, 1),
            reasoning_md=(
                "Sleeve arbitration ruling (Equity/Broad Index/Global): FWRA selected "
                "as the consolidation vehicle."
            ),
            settled=True,
        )
        session.commit()

        # New verdict is settled
        assert new_v.settled is True
        assert new_v.verdict == "HOLD"

        # Old verdict is no longer settled and points to the new one
        session.expire_all()
        old = session.get(VerdictModel, prior_id)
        assert old is not None
        assert old.settled is False
        assert old.superseded_by == new_v.id

        # get_settled_verdict returns the new one
        standing = get_settled_verdict(session, user_id="ariel", subject="FWRA")
        assert standing is not None
        assert standing.id == new_v.id
        assert standing.verdict == "HOLD"
