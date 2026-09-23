"""The deploy decision team: blind reviewer prompt + objection reconciliation +
fail-open. No live LLM — review_fn injected."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.deployment_reviewer import (
    DeploymentReviewerAgent,
    DeploymentReviewOutput,
    ReviewObjection,
)
from argosy.services.deploy_decision_team import (
    build_review_resolution,
    run_deploy_decision_team,
)


def _buy(sym, amt, sleeve=""):
    return SimpleNamespace(symbol=sym, amount_usd=amt, sleeve=sleeve)


def _proposal(*buys):
    return SimpleNamespace(buys=list(buys))


def _packet():
    return {
        "nvda": {"pct": 58.0, "lookthrough_usd": 2_300_000, "book_usd": 3_990_000, "cap_pct": 13.0},
        "instrument_facts": [{"symbol": "R1GR", "us_weight": 1.0}, {"symbol": "EXUS", "us_weight": 0.0}],
        "plan_menu": [{"sleeve": "US growth", "target_pct": 11.0, "current_pct": 4.0}],
        "holdings": {"NVDA": 2_296_000.0},
        "tax_lot_coverage": {
            "TSLA": {
                "available": False,
                "meaning": "cost basis missing; after-tax proceeds cannot be verified",
            }
        },
    }


@pytest.mark.parametrize("lens", ["sizing", "candidate_selection", "diversification"])
def test_reviewer_replacements_respect_whole_position_sizing(lens):
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    system, _ = agent.build_prompt(lens=lens, packet=_packet(), buys=[])
    assert "feasibility of your OWN suggested replacement" in system
    assert "current holdings plus the proposed addition" in system
    assert "post-tax portfolio total" in system
    assert "sub-1% position needs" in system
    assert "genuine evidence-backed >=5x convexity case" in system
    assert "not a demand to fund every sleeve now" in system
    if lens == "sizing":
        assert "EVERY proposed buy, not only moonshots" in system


def test_reviewer_prompt_is_blind_and_lensed():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    system, user = DeploymentReviewerAgent.build_prompt(
        agent, lens="concentration", packet=_packet(),
        buys=[{"symbol": "R1GR", "amount_usd": 18000, "sleeve": "US growth"}],
    )
    assert "YOUR LENS is concentration" in system
    assert "you have NOT seen its reasoning" in system   # blind
    assert "constituent presence" in system
    assert "before/after portfolio exposure" in system
    assert "direction-of-change arithmetic" in system
    assert "DILUTES that concentration" in system
    assert "6.5%-NVDA world ETF materially" in system
    assert "PLAN MENU is authoritative" in system
    assert "replacing 0%-NVDA cash" in system
    assert "never a veto merely" in system
    assert "BUY R1GR $18,000" in user
    assert "reason withheld" in user                      # no author rationale leaks in
    assert "cost basis missing" in user
    assert "do not demand a fabricated executable sale" in system


def test_candidate_reviewer_judges_zero_one_or_split_without_false_floor():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    packet = dict(_packet())
    packet["discovery_candidates"] = [
        {
            "ticker": ticker,
            "rank": rank,
            "score": 77.0,
            "fresh_as_of": "2026-08-28T00:00:00+00:00",
            "fleet": {
                "verdict": "BUY",
                "conviction": "MED",
                "thesis_md": "Evidence-backed 5x convexity with a dated catalyst.",
            },
        }
        for ticker, rank in (("GLUE", 1), ("QURE", 2))
    ]
    system, user = DeploymentReviewerAgent.build_prompt(
        agent, lens="candidate_selection", packet=packet, buys=[]
    )
    blob = system.lower()
    assert "none, one, or multiple" in blob
    assert "sub-1% position is not disqualified" in blob
    assert "prefer a split" in blob
    assert "position-size floor" in blob
    assert "issuer funding" in blob
    assert "never investor deployable cash" in blob
    assert "do not originate a new sale solely" in blob
    assert "new deployable cash: $0" in user
    assert "GLUE" in user and "QURE" in user


def test_review_team_sees_blind_sells_and_verified_funding_without_rationale():
    captured = {}

    def review(lens, packet, buys, **_kwargs):
        captured.update(packet)
        return DeploymentReviewOutput(lens=lens, objections=[])

    proposal = SimpleNamespace(
        buys=[_buy("ABCL", 8_000, "moonshot")],
        sells=[SimpleNamespace(symbol="TSLA", amount_usd=10_000, reason="secret")],
        candidate_comparisons=[SimpleNamespace(
            ticker="ANNX",
            selection="NOT_SELECTED",
            recommended_position_usd=0,
            why="secret comparative rationale",
        )],
        cash_to_deploy=8_000,
        cash_to_reserve=0,
    )
    packet = {**_packet(), "deployable_usd": 0.0}
    decision = run_deploy_decision_team(
        packet, proposal, lenses=("candidate_selection",), review_fn=review
    )
    assert decision.all_clear
    assert captured["proposed_sells"] == [
        {"symbol": "TSLA", "amount_usd": 10_000.0}
    ]
    assert captured["review_funding"]["verified_net_sell_proceeds_usd"] == 8_000.0
    assert captured["author_candidate_dispositions"] == [{
        "ticker": "ANNX",
        "selection": "NOT_SELECTED",
        "recommended_position_usd": 0,
    }]
    assert "secret" not in str(captured)


def test_team_carries_unique_plan_sleeve_across_blind_review_seam():
    captured_buys = []

    def review(lens, packet, buys, **_kwargs):
        captured_buys.extend(buys)
        return DeploymentReviewOutput(lens=lens, objections=[])

    packet = {
        **_packet(),
        "plan_menu": [
            {"sleeve": "US broad-market core", "tickers": ["CSPX"]},
            {"sleeve": "Dividend-quality income", "tickers": ["FUSA"]},
        ],
    }
    proposal = _proposal(_buy("CSPX", 150_000), _buy("FUSA", 29_000))

    decision = run_deploy_decision_team(
        packet,
        proposal,
        lenses=("diversification",),
        review_fn=review,
    )

    assert decision.all_clear
    assert [row["sleeve"] for row in captured_buys] == [
        "US broad-market core",
        "Dividend-quality income",
    ]
    assert [buy.sleeve for buy in proposal.buys] == [
        "US broad-market core",
        "Dividend-quality income",
    ]


def test_reviewer_sees_candidate_disposition_receipt_but_not_reasoning():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    packet = dict(_packet())
    packet["author_candidate_dispositions"] = [{
        "ticker": "ABCL",
        "selection": "NOT_SELECTED",
        "recommended_position_usd": 0,
    }]
    system, user = DeploymentReviewerAgent.build_prompt(
        agent,
        lens="candidate_selection",
        packet=packet,
        buys=[],
    )

    assert "AUTHOR CANDIDATE DISPOSITIONS" in user
    assert "ABCL: NOT_SELECTED" in user
    assert "reason withheld" in user
    assert "Tax-lot availability is not verified net sale proceeds" in system


@pytest.mark.parametrize("policy_status,failures", [
    ("blocked", ["tax evidence expired"]), ("actionable", []),
])
def test_every_blind_lens_gets_raw_sell_execution_constraints(policy_status, failures):
    packet = {**_packet(), "allow_sells": True,
              "staged_sell_policies": {"XYZ": {"status": policy_status, "failures": failures,
                  "maximum_current_tranche_usd": 1234.56, "execute_no_later_than": "2026-09-20"}},
              "user_constraints": "No full liquidation."}
    captured = []
    def review(lens, packet, buys, **kwargs):
        agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
        system, user = agent.build_prompt(lens=lens, packet=packet, buys=buys)
        captured.append((lens, system, user))
        return DeploymentReviewOutput(lens=lens, objections=[])
    proposal = SimpleNamespace(buys=[], sells=[], rationale="secret author explanation")
    result = run_deploy_decision_team(packet, proposal, review_fn=review)
    assert result.all_clear and not result.degraded
    assert len(captured) >= 3
    for _, system, user in captured:
        assert json.dumps(packet["staged_sell_policies"]) in user
        assert '"allow_sells": true' in user and "No full liquidation." in user
        assert "secret author explanation" not in user
        assert "temporarily unexecutable tranche can coexist" in system


def test_sizing_reviewer_rederives_probability_aware_amount_blind():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    system, user = DeploymentReviewerAgent.build_prompt(
        agent,
        lens="sizing",
        packet=_packet(),
        buys=[{"symbol": "GLUE", "amount_usd": 26_000, "sleeve": "moonshot"}],
    )
    blob = system.lower()
    assert "economic-wipeout probability" in blob
    assert "probability-weighted payoff" in blob
    assert "10x possibility is not conviction" in blob
    assert "small existing tracking positions are not sizing precedents" in blob
    assert "changes_amount" in blob
    assert "recommended_amount_usd" in blob
    assert "BUY GLUE $26,000" in user


def test_reviewer_schema_normalizes_plain_language_severity():
    warning = ReviewObjection(
        ticker="IWQU", concern="worth surfacing", severity="warning"
    )
    blocker = ReviewObjection(
        ticker="R1GR", concern="unsound", severity="blocking"
    )
    assert warning.severity == "warn"
    assert blocker.severity == "block"


@pytest.mark.real_seam
def test_real_reviewer_agent_dispatch_parses_objection(monkeypatch):
    """Exercise BaseAgent.run; only the external model call is replaced."""
    payload = {
        "lens": "concentration",
        "objections": [{
            "ticker": "R1GR",
            "concern": "The before/after exposure arithmetic does not improve the book.",
            "severity": "block",
        }],
        "overall_note": "One material objection.",
    }

    async def fake_call(self, *, system, user, **kwargs):
        assert "reason withheld" in user and "not seen its reasoning" in system.lower()
        return ModelCall(
            text=json.dumps(payload), tokens_in=10, tokens_out=10, model="test-model"
        )

    monkeypatch.setattr(DeploymentReviewerAgent, "_call_model", fake_call)
    report = DeploymentReviewerAgent(user_id="ariel").run_sync(
        lens="concentration",
        packet=_packet(),
        buys=[{"symbol": "R1GR", "amount_usd": 18_000, "sleeve": "US growth"}],
    )
    assert isinstance(report.output, DeploymentReviewOutput)
    assert report.output.objections[0].severity == "block"


def test_team_flags_objected_buys_and_approves_the_rest():
    # Concentration reviewer refutes R1GR (NVDA-heavy); nothing objects to EXUS.
    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "concentration":
            return DeploymentReviewOutput(lens=lens, objections=[
                ReviewObjection(ticker="R1GR", concern="~14% NVDA — not a diversifier", severity="block"),
            ])
        return DeploymentReviewOutput(lens=lens, objections=[])

    decision = run_deploy_decision_team(
        _packet(), _proposal(_buy("R1GR", 18000, "US growth"), _buy("EXUS", 26000, "Intl")),
        lenses=("concentration", "diversification", "prudence"),
        review_fn=_review,
    )
    assert [b.symbol for b in decision.approved] == ["EXUS"]
    assert len(decision.flagged) == 1 and decision.flagged[0]["symbol"] == "R1GR"
    assert decision.flagged[0]["objections"][0]["lens"] == "concentration"
    assert not decision.all_clear
    assert decision.reviewers_ran == 3 and not decision.degraded


def test_team_records_degradation_when_a_reviewer_dies():
    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "diversification":
            raise RuntimeError("claude.exe timeout")
        return DeploymentReviewOutput(lens=lens, objections=[])

    decision = run_deploy_decision_team(
        _packet(), _proposal(_buy("EXUS", 26000)),
        lenses=("concentration", "diversification", "prudence"),
        review_fn=_review,
    )
    # The team result captures fewer eyes; the money-path caller stops on degraded.
    assert decision.reviewers_ran == 2 and decision.reviewers_expected == 3
    assert decision.degraded is True
    assert decision.all_clear and [b.symbol for b in decision.approved] == ["EXUS"]


def test_warn_only_buy_remains_approved_but_auditable():
    proposal = _proposal(_buy("IWQU", 22000))

    def _review(lens, packet, buys, **kwargs):
        objections = []
        if lens == "diversification":
            objections = [ReviewObjection(
                ticker="IWQU", concern="compare a role-equivalent fund", severity="warn"
            )]
        return DeploymentReviewOutput(lens=lens, objections=objections)

    decision = run_deploy_decision_team({}, proposal, review_fn=_review)
    assert [b.symbol for b in decision.approved] == ["IWQU"]
    assert decision.all_clear
    assert decision.flagged[0]["objections"][0]["severity"] == "warn"


def test_glue_warn_that_changes_amount_forces_reconciliation():
    """Regression: the live fleet said $8-10k, not $26k, but `warn` shipped."""
    proposal = _proposal(_buy("GLUE", 26_000, "moonshot"))

    def _review(lens, packet, buys, **kwargs):
        objections = []
        if lens == "sizing":
            objections = [ReviewObjection(
                ticker="GLUE",
                concern="A starter preserves convexity with less capital at risk.",
                severity="warn",
                impact="changes_amount",
                recommended_amount_usd=10_000,
            )]
        return DeploymentReviewOutput(lens=lens, objections=objections)

    decision = run_deploy_decision_team({}, proposal, review_fn=_review)
    assert not decision.all_clear
    assert decision.approved == []
    objection = decision.material_flagged[0]["objections"][0]
    assert objection["impact"] == "changes_amount"
    assert objection["recommended_amount_usd"] == 10_000


def test_omitted_candidate_objection_is_not_orphaned():
    """A reviewer can require adding a finalist absent from proposal.buys."""
    proposal = _proposal(_buy("CSPX", 40_000, "US core"))

    def _review(lens, packet, buys, **kwargs):
        objections = []
        if lens == "candidate_selection":
            objections = [ReviewObjection(
                ticker="ABCL",
                concern="The omitted finalist deserves a funded slot.",
                severity="block",
                impact="adds_omitted_candidate",
                recommended_amount_usd=12_000,
            )]
        return DeploymentReviewOutput(lens=lens, objections=objections)

    decision = run_deploy_decision_team({}, proposal, review_fn=_review)
    assert not decision.all_clear
    assert [b.symbol for b in decision.approved] == ["CSPX"]
    flagged = decision.material_flagged[0]
    assert flagged["symbol"] == "ABCL"
    assert flagged["amount_usd"] == 0
    assert flagged["proposed"] is False


def test_review_resolution_preserves_dissent_and_the_re_review_outcome():
    first = run_deploy_decision_team(
        {},
        _proposal(_buy("GLUE", 26_000, "moonshot")),
        lenses=("sizing",),
        review_fn=lambda lens, packet, buys, **kwargs: DeploymentReviewOutput(
            lens=lens,
            objections=[ReviewObjection(
                ticker="GLUE",
                concern="The probability distribution warrants a smaller starter.",
                severity="warn",
                impact="changes_amount",
                recommended_amount_usd=10_000,
            )],
        ),
    )
    second = run_deploy_decision_team(
        {},
        _proposal(_buy("GLUE", 10_000, "moonshot")),
        lenses=("sizing",),
        review_fn=lambda lens, packet, buys, **kwargs: DeploymentReviewOutput(
            lens=lens, objections=[]
        ),
    )

    resolution = build_review_resolution([first, second])

    assert resolution is not None
    assert resolution.one_voice is True
    assert resolution.rounds == 2
    assert resolution.objections[0].status == "resolved_by_re_review"
    assert resolution.objections[0].proposed_amount_usd == 26_000
    assert resolution.objections[0].recommended_amount_usd == 10_000


def test_team_decision_maps_to_dto():
    # The route maps a TeamDecision -> TeamReviewDTO; lock that shape.
    from argosy.services.contracts import TeamFlaggedBuyDTO, TeamObjectionDTO, TeamReviewDTO

    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "concentration":
            return DeploymentReviewOutput(lens=lens, objections=[
                ReviewObjection(ticker="R1GR", concern="14% NVDA", severity="block")])
        return DeploymentReviewOutput(lens=lens, objections=[])

    d = run_deploy_decision_team(
        _packet(), _proposal(_buy("R1GR", 18000, "growth"), _buy("EXUS", 26000, "intl")),
        review_fn=_review,
    )
    dto = TeamReviewDTO(
        reviewers_ran=d.reviewers_ran, reviewers_expected=d.reviewers_expected,
        degraded=d.degraded, approved=[b.symbol for b in d.approved],
        flagged=[TeamFlaggedBuyDTO(symbol=f["symbol"], amount_usd=f["amount_usd"],
                 objections=[TeamObjectionDTO(**o) for o in f["objections"]]) for f in d.flagged],
    )
    assert dto.approved == ["EXUS"]
    assert dto.flagged[0].symbol == "R1GR"
    assert dto.flagged[0].objections[0].severity == "block"


def test_write_team_flag_proposals_builds_inbox_rows():
    from argosy.services.deploy_decision_team import (
        TeamDecision,
        write_team_flag_proposals,
    )

    captured = []

    class _FakeDb:
        def add(self, row): captured.append(row)
        def commit(self): pass
        def rollback(self): pass

    decision = TeamDecision(flagged=[{
        "symbol": "R1GR", "amount_usd": 18000.0,
        "objections": [
            {"lens": "concentration", "concern": "~14% NVDA — not a diversifier", "severity": "block"},
            {"lens": "prudence", "concern": "adds to an extended sleeve", "severity": "warn"},
        ],
    }])
    n = write_team_flag_proposals(_FakeDb(), "ariel", decision)
    assert n == 1
    row = captured[0]
    assert row.kind == "deploy_team_flag"
    assert row.dedup_key == "deploy_team_flag:ariel:R1GR"
    assert row.severity == "warning"                 # worst objection is block
    assert "R1GR" in row.summary and "$18,000" in row.summary
    assert "~14% NVDA" in row.rationale_md and "NOT executed" in row.rationale_md
    assert row.status == "open"


def test_write_team_flag_proposals_survives_the_real_schema(alembic_engine_at_head):
    """REAL-DB write test — the class of test the fake-db version can't be.
    The original fake-db test was green while every live insert died on
    ck_action_proposals_kind (kind='deploy_team_flag' wasn't in the 0055
    CHECK enum; the sink swallowed the IntegrityError as a presumed dedup
    collision). This test writes through the real migrated schema, so a
    constraint regression fails loudly instead of silently killing the sink."""
    from sqlalchemy.orm import Session

    from argosy.services.deploy_decision_team import (
        TeamDecision,
        write_team_flag_proposals,
    )

    decision = TeamDecision(flagged=[{
        "symbol": "R1GR", "amount_usd": 16000.0,
        "objections": [{"lens": "concentration", "concern": "~14% NVDA", "severity": "block"}],
    }])
    with Session(alembic_engine_at_head) as s:
        assert write_team_flag_proposals(s, "ariel", decision) == 1
        first = s.execute(
            __import__("sqlalchemy").text(
                "SELECT id, summary FROM action_proposals "
                "WHERE dedup_key = 'deploy_team_flag:ariel:R1GR'"
            )
        ).fetchone()
        assert first is not None and "$16,000" in first[1]
        # Second blocking write same symbol (today's run, NEW amount + objections) →
        # dedup collision REFRESHES the open row IN PLACE — the inbox must
        # never show yesterday's stale amount for a buy that no longer exists.
        decision2 = TeamDecision(flagged=[{
            "symbol": "R1GR", "amount_usd": 13000.0,
            "objections": [{"lens": "diversification",
                            "concern": "fake diversifier — mega-cap clone",
                            "severity": "block"}],
        }])
        assert write_team_flag_proposals(s, "ariel", decision2) == 1
        rows = s.execute(
            __import__("sqlalchemy").text(
                "SELECT id, kind, severity, status, summary, rationale_md "
                "FROM action_proposals "
                "WHERE dedup_key = 'deploy_team_flag:ariel:R1GR'"
            )
        ).fetchall()
    assert len(rows) == 1                              # still ONE row (same slot)
    row = rows[0]
    assert row[0] == first[0]                          # row id kept
    assert row[1] == "deploy_team_flag" and row[3] == "open"
    assert row[2] == "warning"                         # stop-level objection
    assert "$13,000" in row[4] and "$16,000" not in row[4]   # amount refreshed
    assert "fake diversifier" in row[5]                # objections refreshed


def test_write_stock_decision_proposal_survives_the_real_schema(alembic_engine_at_head):
    """Same real-schema regression net for the holdings-review sink
    (kind='stock_decision' had the identical silent CHECK failure)."""
    from sqlalchemy.orm import Session

    from argosy.agents.stock_decision import StockDecisionOutput
    from argosy.services.stock_decision.service import write_stock_decision_proposal

    v = StockDecisionOutput(ticker="RKT", verdict="TRIM", confidence="MED", reason="probe")
    with Session(alembic_engine_at_head) as s:
        row = write_stock_decision_proposal(s, "ariel", v)
        assert row is not None and row.id is not None


def test_supersede_cleared_flags_keeps_only_current_blocks(alembic_engine_at_head):
    """The inbox mirrors current blocks, not stale or advisory objections."""
    from sqlalchemy.orm import Session

    from argosy.services.deploy_decision_team import (
        TeamDecision,
        supersede_cleared_flags,
        write_team_flag_proposals,
    )

    def _flag(sym, amt, severity="block"):
        return {"symbol": sym, "amount_usd": amt,
                "objections": [{"lens": "prudence", "concern": "x", "severity": severity}]}

    with Session(alembic_engine_at_head) as s:
        # Yesterday: SPMV and XOLD had stop-level flags.
        write_team_flag_proposals(s, "ariel", TeamDecision(flagged=[_flag("SPMV", 20000), _flag("XOLD", 9000)]))
        # Today: CSPX has only an advisory note; SPMV cleared and XOLD was dropped.
        today = TeamDecision(flagged=[_flag("CSPX", 42000, "warn")])
        write_team_flag_proposals(s, "ariel", today)
        n = supersede_cleared_flags(s, "ariel", today, reviewed_symbols={"SPMV", "CSPX", "EXUS"})
        assert n == 2
        rows = dict(s.execute(__import__("sqlalchemy").text(
            "SELECT dedup_key, status FROM action_proposals WHERE kind='deploy_team_flag'"
        )).fetchall())
        assert rows["deploy_team_flag:ariel:SPMV"] == "superseded"   # cleared this run
        assert rows["deploy_team_flag:ariel:XOLD"] == "superseded"    # dropped from final run
        assert "deploy_team_flag:ariel:CSPX" not in rows               # advisory only


def test_warn_only_team_flag_does_not_create_inbox_action():
    from argosy.services.deploy_decision_team import TeamDecision, write_team_flag_proposals

    class _ExplodingDb:
        def add(self, row): raise AssertionError("advisory must not become an action")

    warn = TeamDecision(flagged=[{
        "symbol": "IWQU", "amount_usd": 22000,
        "objections": [{"lens": "diversification", "concern": "note", "severity": "warn"}],
    }])
    assert write_team_flag_proposals(_ExplodingDb(), "ariel", warn) == 0


def test_write_team_flag_proposals_nothing_flagged_is_a_noop():
    from argosy.services.deploy_decision_team import (
        TeamDecision,
        write_team_flag_proposals,
    )

    class _ExplodingDb:
        def add(self, row): raise AssertionError("no rows expected")

    assert write_team_flag_proposals(_ExplodingDb(), "ariel", TeamDecision()) == 0


def test_team_enriches_facts_with_nvda_lookthrough():
    captured = {}

    def _review(lens, packet, buys, *, user_id="ariel"):
        captured["facts"] = packet.get("instrument_facts")
        return DeploymentReviewOutput(lens=lens, objections=[])

    run_deploy_decision_team(_packet(), _proposal(_buy("R1GR", 18000), _buy("MELI", 6000)),
                             lenses=("concentration",), review_fn=_review)
    r1gr = next(f for f in captured["facts"] if f["symbol"] == "R1GR")
    assert r1gr["nvda_weight"] > 0.1  # raw NVDA ground truth handed to the reviewer
    # Situs FACTS travel with each instrument, so reviewers re-derive estate
    # exposure from incorporation/domicile — never from the author's claims.
    assert r1gr["us_situs"] is False                    # Irish UCITS, estate-safe
    meli = next(f for f in captured["facts"] if f["symbol"] == "MELI")
    assert meli["us_situs"] is True                     # Delaware-inc = US-situs
    assert meli["domicile"] == "US"
    assert meli["us_weight"] == 0.0   # geographic weight ≠ situs — both facts present


def test_prudence_brief_and_prompt_carry_situs_facts():
    """The prudence reviewer must re-derive US-situs from the provided
    incorporation/domicile facts — never accept the author's claimed weights —
    and the prompt must actually render those facts."""
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    packet = dict(_packet())
    packet["instrument_facts"] = [
        {"symbol": "MELI", "us_weight": 0.0, "us_situs": True, "domicile": "US"},
        {"symbol": "EXUS", "us_weight": 0.0, "us_situs": False,
         "domicile": "non-US (UCITS/IL)"},
    ]
    system, user = DeploymentReviewerAgent.build_prompt(
        agent, lens="prudence", packet=packet,
        buys=[{"symbol": "MELI", "amount_usd": 6000, "sleeve": "high-growth"}],
    )
    assert "never from" in system and "claimed weights" in system
    assert "US-SITUS (estate-exposed)" in user          # MELI's situs fact rendered
    assert "non-US-situs" in user                       # EXUS's too


def test_recovery_review_prompt_keeps_prior_portfolio_objection():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    packet = dict(_packet())
    packet["recovery_review_objections"] = [{"symbol": "PORTFOLIO", "objections": [
        {"concern": "Core funding depends on an unresolved sale", "impact": "rejects_trade"},
    ]}]
    system, user = agent.build_prompt(lens="prudence", packet=packet, buys=[])
    assert "Core funding depends on an unresolved sale" in user
    assert "prior portfolio-wide dependency cannot disappear" in system
