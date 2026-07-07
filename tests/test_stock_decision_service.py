"""Orchestration: bundle assembly (best-effort), tiered holdings review, and the
HOLD-stays-silent filter. No live LLM/network — fetchers + decide injected."""
from __future__ import annotations

from argosy.agents.stock_decision import StockDecisionOutput
from argosy.services.stock_decision import (
    actionable_verdicts,
    decide_holdings,
    load_elevated_thesis_flags,
    research_bundle,
    run_holdings_review,
    verify_verdict,
    write_stock_decision_proposal,
)


def test_research_bundle_is_best_effort():
    def ok(t): return f"news for {t}"
    def empty(t): return None
    def boom(t): raise RuntimeError("source down")

    b = research_bundle("RKT", fetchers={"news": ok, "fundamentals": empty, "sentiment": boom})
    assert b == {"news": "news for RKT"}  # empty + failing sources simply absent


def test_triage_skips_immaterial_names():
    researched = []

    def _decide(ticker, *, context, bundle, user_id="ariel"):
        researched.append(ticker)
        return StockDecisionOutput(ticker=ticker, verdict="HOLD", confidence="LOW", reason="x")

    decide_holdings(
        {"BIG": 100_000.0, "TINY": 200.0},
        fetchers={},
        context_of=lambda t, u: f"{t} ${u:,.0f}",
        triage=lambda t, usd: usd >= 1_000.0,   # only research material positions
        decide=_decide,
    )
    assert researched == ["BIG"]  # TINY skipped by the tiering gate


def test_hold_stays_silent_only_actionable_surface():
    verdicts_by_ticker = {
        "RKT": StockDecisionOutput(ticker="RKT", verdict="TRIM", confidence="MED", reason="weakening"),
        "CSPX": StockDecisionOutput(ticker="CSPX", verdict="HOLD", confidence="HIGH", reason="intact"),
    }

    def _decide(ticker, *, context, bundle, user_id="ariel"):
        return verdicts_by_ticker[ticker]

    verdicts = decide_holdings(
        {"RKT": 42_000.0, "CSPX": 156_000.0},
        fetchers={"news": lambda t: f"headline {t}"},
        context_of=lambda t, u: f"{t}",
        decide=_decide,
    )
    assert len(verdicts) == 2                      # both decided (audit trail)
    surfaced = actionable_verdicts(verdicts)
    assert [v.ticker for v in surfaced] == ["RKT"]  # only the TRIM surfaces; HOLD silent


def test_run_holdings_review_writes_only_actionable(monkeypatch):
    verdicts_by_ticker = {
        "RKT": StockDecisionOutput(ticker="RKT", verdict="SELL", confidence="HIGH", reason="thesis broken"),
        "CSPX": StockDecisionOutput(ticker="CSPX", verdict="HOLD", confidence="HIGH", reason="intact"),
        "TINY": StockDecisionOutput(ticker="TINY", verdict="SELL", confidence="LOW", reason="n/a"),
    }
    written = []

    def _decide(ticker, *, context, bundle, user_id="ariel"):
        return verdicts_by_ticker[ticker]

    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"RKT": 42_000.0, "CSPX": 156_000.0, "TINY": 200.0},
        fetchers={"news": lambda t: "headline"},
        decide=_decide,
        sink=lambda v: written.append(v.ticker) or object(),
    )
    # TINY skipped by triage; RKT (SELL) written; CSPX (HOLD) silent.
    assert summary["reviewed"] == 2
    assert summary["actionable"] == 1
    assert written == ["RKT"]


def test_verify_verdict_blind_rederivation_gate():
    sell = StockDecisionOutput(ticker="RKT", verdict="SELL", confidence="HIGH", reason="broken")
    bundle = {"news": "x"}

    # Re-derivation confirms a reduce -> passes.
    def _confirm(ticker, *, context, bundle, user_id="ariel"):
        return StockDecisionOutput(ticker=ticker, verdict="TRIM", confidence="MED", reason="agrees reduce")
    assert verify_verdict(sell, bundle=bundle, decide=_confirm) is True

    # Re-derivation diverges to HOLD -> fails (fail-closed; the trade won't surface).
    def _diverge(ticker, *, context, bundle, user_id="ariel"):
        return StockDecisionOutput(ticker=ticker, verdict="HOLD", confidence="HIGH", reason="intact")
    assert verify_verdict(sell, bundle=bundle, decide=_diverge) is False

    # HOLD never reaches the gate.
    hold = StockDecisionOutput(ticker="CSPX", verdict="HOLD", confidence="HIGH", reason="ok")
    assert verify_verdict(hold, bundle=bundle, decide=_diverge) is True


def test_run_holdings_review_holds_unverified_trades():
    # RKT decides SELL, but the blind re-derivation diverges -> not surfaced.
    def _decide(ticker, *, context, bundle, user_id="ariel"):
        return StockDecisionOutput(ticker=ticker, verdict="SELL", confidence="HIGH", reason="first pass")

    written = []
    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"RKT": 42_000.0},
        fetchers={"news": lambda t: "headline"},
        decide=_decide,
        sink=lambda v: written.append(v.ticker) or object(),
        verify=lambda v, bundle: False,   # re-derivation refuses to confirm
    )
    assert summary["actionable"] == 1
    assert summary["written"] == 0
    assert summary["held_unverified"] == 1
    assert written == []


# ---------------------------------------------------------------------------
# Thesis-flag elevation: a weakened/broken thesis bypasses the size triage.
# ---------------------------------------------------------------------------

def _decide_recorder(researched, contexts=None):
    def _decide(ticker, *, context, bundle, user_id="ariel"):
        researched.append(ticker)
        if contexts is not None:
            contexts[ticker] = context
        return StockDecisionOutput(ticker=ticker, verdict="HOLD", confidence="LOW", reason="x")
    return _decide


def test_elevated_small_position_gets_deep_pass():
    """A weakened thesis elevates TINY past the size gate; the flag's evidence
    rides in the agent's context as labelled input, and the summary is auditable."""
    researched, contexts = [], {}
    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"BIG": 100_000.0, "TINY": 200.0},
        fetchers={},
        decide=_decide_recorder(researched, contexts),
        elevated_flags={"TINY": {
            "kind": "thesis_monitor_weakened", "status": "weakened",
            "summary": "guidance cut; core product losing share",
        }},
    )
    assert sorted(researched) == ["BIG", "TINY"]   # TINY deep-passed despite size
    assert summary["elevated"] == ["TINY"]
    assert "thesis_monitor status: weakened — guidance cut" in contexts["TINY"]
    assert "thesis_monitor" not in contexts["BIG"]  # non-elevated context unchanged


def test_no_flags_size_triage_unchanged():
    researched = []
    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"BIG": 100_000.0, "TINY": 200.0},
        fetchers={},
        decide=_decide_recorder(researched),
        elevated_flags={},
    )
    assert researched == ["BIG"]                   # TINY still skipped by size
    assert summary["elevated"] == []


def test_broken_thesis_also_elevates():
    researched, contexts = [], {}
    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"TINY": 200.0},
        fetchers={},
        decide=_decide_recorder(researched, contexts),
        elevated_flags={"TINY": {
            "kind": "thesis_monitor_broken", "status": "broken",
            "summary": "chapter-11 filing",
        }},
    )
    assert researched == ["TINY"]
    assert summary["elevated"] == ["TINY"]
    assert "thesis_monitor status: broken — chapter-11 filing" in contexts["TINY"]


def test_flag_for_unheld_ticker_is_ignored():
    researched = []
    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"BIG": 100_000.0},
        fetchers={},
        decide=_decide_recorder(researched),
        elevated_flags={"GHOST": {
            "kind": "thesis_monitor_broken", "status": "broken", "summary": "gone",
        }},
    )
    assert researched == ["BIG"]                   # GHOST not held -> nothing to review
    assert summary["elevated"] == []


def test_load_elevated_thesis_flags_reads_active_unexpired_only(tmp_path):
    """Loader keys ACTIVE, unexpired thesis_monitor_* flags by payload ticker and
    ignores expired / acknowledged / other-kind rows; 'broken' wins over 'weakened'."""
    import json
    from datetime import datetime, timedelta, timezone

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, MonitorFlag, User

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'flags.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)
    db.add(User(id="ariel", plan="free"))

    def _flag(kind, ticker, status, *, expires=None, acked=None, row_status="active",
              rationale="thesis evidence"):
        return MonitorFlag(
            user_id="ariel", kind=kind, severity="warning",
            payload=json.dumps({
                "ticker": ticker, "thesis_status": status, "rationale_md": rationale,
            }),
            surfaced_at=now - timedelta(days=1),
            expires_at=expires, acknowledged_at=acked,
            dedup_key=f"v1|thesis_monitor|ariel|{ticker}|{status}|{kind}",
            status=row_status,
        )

    db.add_all([
        _flag("thesis_monitor_weakened", "RKT", "weakened",
              expires=now + timedelta(days=5), rationale="housing macro deteriorating"),
        _flag("thesis_monitor_broken", "RKT", "broken",
              expires=now + timedelta(days=5)),                       # broken wins
        _flag("thesis_monitor_weakened", "OLD", "weakened",
              expires=now - timedelta(days=1)),                       # expired -> out
        _flag("thesis_monitor_broken", "ACK", "broken",
              expires=now + timedelta(days=5), acked=now),            # acked -> out
        _flag("thesis_monitor_weakened", "SUP", "weakened",
              expires=now + timedelta(days=5), row_status="superseded"),  # inactive -> out
        MonitorFlag(
            user_id="ariel", kind="alpha_report_caution", severity="warning",
            payload=json.dumps({"caution": "NVDA looks stretched"}),
            surfaced_at=now, dedup_key="v1|alpha|x", status="active",
        ),                                                            # other kind -> out
    ])
    db.commit()

    flags = load_elevated_thesis_flags(db, "ariel", now=now)
    db.close()
    assert set(flags) == {"RKT"}
    assert flags["RKT"]["kind"] == "thesis_monitor_broken"
    assert flags["RKT"]["status"] == "broken"
    assert flags["RKT"]["summary"] == "thesis evidence"


def test_write_stock_decision_proposal_builds_actionproposal():
    captured = {}

    class _FakeDb:
        def add(self, row): captured["row"] = row
        def commit(self): pass
        def rollback(self): pass

    v = StockDecisionOutput(
        ticker="RKT", verdict="TRIM", confidence="MED",
        reason="housing macro deteriorating", evidence=["-27% YTD"], data_gaps=["fundamentals"],
    )
    row = write_stock_decision_proposal(_FakeDb(), "ariel", v)
    assert row is captured["row"]
    assert row.kind == "stock_decision"
    assert row.severity == "warning"                         # TRIM -> warning
    assert row.dedup_key == "stock_decision:ariel:RKT"
    assert "Trim RKT" in row.summary
    assert "-27% YTD" in row.rationale_md and "fundamentals" in row.rationale_md
