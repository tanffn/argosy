"""The deploy decision team: blind reviewer prompt + objection reconciliation +
fail-open. No live LLM — review_fn injected."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.agents.deployment_reviewer import (
    DeploymentReviewerAgent,
    DeploymentReviewOutput,
    ReviewObjection,
)
from argosy.services.deploy_decision_team import run_deploy_decision_team


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
    }


def test_reviewer_prompt_is_blind_and_lensed():
    agent = DeploymentReviewerAgent.__new__(DeploymentReviewerAgent)
    system, user = DeploymentReviewerAgent.build_prompt(
        agent, lens="concentration", packet=_packet(),
        buys=[{"symbol": "R1GR", "amount_usd": 18000, "sleeve": "US growth"}],
    )
    assert "YOUR LENS is concentration" in system
    assert "you have NOT seen its reasoning" in system   # blind
    assert "BUY R1GR $18,000" in user
    assert "reason withheld" in user                      # no author rationale leaks in


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


def test_team_is_fail_open_when_a_reviewer_dies():
    def _review(lens, packet, buys, *, user_id="ariel"):
        if lens == "diversification":
            raise RuntimeError("claude.exe timeout")
        return DeploymentReviewOutput(lens=lens, objections=[])

    decision = run_deploy_decision_team(
        _packet(), _proposal(_buy("EXUS", 26000)),
        lenses=("concentration", "diversification", "prudence"),
        review_fn=_review,
    )
    # the dead reviewer is skipped, the trade is NOT blocked, and degradation is flagged
    assert decision.reviewers_ran == 2 and decision.reviewers_expected == 3
    assert decision.degraded is True
    assert decision.all_clear and [b.symbol for b in decision.approved] == ["EXUS"]


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
        # Second write same symbol (today's run, NEW amount + objections) →
        # dedup collision REFRESHES the open row IN PLACE — the inbox must
        # never show yesterday's stale amount for a buy that no longer exists.
        decision2 = TeamDecision(flagged=[{
            "symbol": "R1GR", "amount_usd": 13000.0,
            "objections": [{"lens": "diversification",
                            "concern": "fake diversifier — mega-cap clone",
                            "severity": "warn"}],
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
    assert row[2] == "info"                            # worst objection now warn
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


def test_supersede_cleared_flags_closes_rereviewed_symbols(alembic_engine_at_head):
    """A flag the team re-reviewed and CLEARED must disappear from the client's
    checklist; flags for symbols NOT reviewed this run stay open."""
    from sqlalchemy.orm import Session

    from argosy.services.deploy_decision_team import (
        TeamDecision,
        supersede_cleared_flags,
        write_team_flag_proposals,
    )

    def _flag(sym, amt):
        return {"symbol": sym, "amount_usd": amt,
                "objections": [{"lens": "prudence", "concern": "x", "severity": "warn"}]}

    with Session(alembic_engine_at_head) as s:
        # Yesterday: SPMV and XOLD flagged.
        write_team_flag_proposals(s, "ariel", TeamDecision(flagged=[_flag("SPMV", 20000), _flag("XOLD", 9000)]))
        # Today: SPMV re-reviewed and cleared (approved); XOLD not in the proposal.
        today = TeamDecision(flagged=[_flag("CSPX", 42000)])
        write_team_flag_proposals(s, "ariel", today)
        n = supersede_cleared_flags(s, "ariel", today, reviewed_symbols={"SPMV", "CSPX", "EXUS"})
        assert n == 1
        rows = dict(s.execute(__import__("sqlalchemy").text(
            "SELECT dedup_key, status FROM action_proposals WHERE kind='deploy_team_flag'"
        )).fetchall())
        assert rows["deploy_team_flag:ariel:SPMV"] == "superseded"   # cleared this run
        assert rows["deploy_team_flag:ariel:XOLD"] == "open"          # not re-reviewed
        assert rows["deploy_team_flag:ariel:CSPX"] == "open"          # still flagged


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
