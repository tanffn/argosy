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


# ---------------------------------------------------------------------------
# 2026-07-08 tracking-state audit FIX 3 — every verdict is a queryable row,
# held_unverified is honest, and the x10 sleeve is the triage FLOOR.
# ---------------------------------------------------------------------------

def _sqlite_session(tmp_path, name="reviews.db"):
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, User

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(User(id="ariel", plan="free"))
    db.commit()
    return db


def test_every_verdict_records_an_audit_row_with_outcome():
    """HOLD, proposed, held_unverified and dedup_skipped ALL leave a row —
    'nothing hidden: reviewed means a queryable row'."""
    verdicts_by_ticker = {
        "HOLDCO": StockDecisionOutput(ticker="HOLDCO", verdict="HOLD", confidence="HIGH", reason="intact"),
        "SELLCO": StockDecisionOutput(ticker="SELLCO", verdict="SELL", confidence="HIGH", reason="broken"),
        "UNVER": StockDecisionOutput(ticker="UNVER", verdict="TRIM", confidence="MED", reason="weak"),
        "DUPCO": StockDecisionOutput(ticker="DUPCO", verdict="SELL", confidence="HIGH", reason="broken"),
    }
    recorded: list[tuple[str, str, dict]] = []

    summary = run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"HOLDCO": 10_000.0, "SELLCO": 20_000.0, "UNVER": 30_000.0, "DUPCO": 40_000.0},
        fetchers={},
        decide=lambda t, *, context, bundle, user_id="ariel": verdicts_by_ticker[t],
        # DUPCO's sink returns None (open peer holds the dedup slot).
        sink=lambda v: None if v.ticker == "DUPCO" else object(),
        verify=lambda v, bundle: v.ticker != "UNVER",
        record=lambda v, **kw: recorded.append((v.ticker, kw["outcome"], kw)),
    )
    outcomes = {t: o for t, o, _ in recorded}
    assert outcomes == {
        "HOLDCO": "hold",
        "SELLCO": "proposed",
        "UNVER": "held_unverified",
        "DUPCO": "dedup_skipped",
    }
    # Audit context rides along.
    kw = next(kw for t, _, kw in recorded if t == "SELLCO")
    assert kw["position_usd"] == 20_000.0
    assert kw["elevated_by_flag"] is False
    assert summary["held_unverified"] == 1
    assert summary["written"] == 1


def test_elevated_flag_is_stamped_on_the_audit_row():
    recorded = []
    run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"TINY": 200.0},
        fetchers={},
        decide=lambda t, *, context, bundle, user_id="ariel": StockDecisionOutput(
            ticker=t, verdict="HOLD", confidence="LOW", reason="x"),
        elevated_flags={"TINY": {
            "kind": "thesis_monitor_weakened", "status": "weakened", "summary": "s",
        }},
        record=lambda v, **kw: recorded.append(kw),
    )
    assert recorded[0]["elevated_by_flag"] is True


def test_x10_sleeve_member_is_reviewed_regardless_of_size():
    """A $4.8k moonshot position (below the $5k gate) is the triage FLOOR —
    reviewed every pass; other small names stay skipped."""
    researched, contexts = [], {}
    run_holdings_review(
        db=None, user_id="ariel", min_position_usd=5_000.0,
        holdings={"TEM": 4_800.0, "TINY": 4_800.0, "BIG": 50_000.0},
        fetchers={},
        decide=_decide_recorder(researched, contexts),
        elevated_flags={},
        always_review={"TEM"},
        record=False,
    )
    assert sorted(researched) == ["BIG", "TEM"]      # TINY still size-skipped
    assert "plan x10-sleeve member (size-floor exempt)" in contexts["TEM"]
    assert "x10-sleeve" not in contexts["BIG"]       # material names unmarked


def test_record_holding_review_writes_queryable_row(tmp_path):
    import json

    from argosy.services.stock_decision import record_holding_review
    from argosy.state.models import HoldingReview

    db = _sqlite_session(tmp_path)
    v = StockDecisionOutput(
        ticker="TEM", verdict="TRIM", confidence="MED",
        reason="read-out risk", evidence=["trial delayed"], data_gaps=["fundamentals"],
    )
    record_holding_review(
        db, "ariel", v, position_usd=4_800.0, elevated_by_flag=True,
        outcome="held_unverified",
    )
    row = db.query(HoldingReview).one()
    assert row.symbol == "TEM"
    assert row.verdict == "TRIM"
    assert row.outcome == "held_unverified"
    assert row.position_usd == 4_800.0
    assert row.elevated_by_flag is True
    ev = json.loads(row.evidence_json)
    assert ev["evidence"] == ["trial delayed"]
    assert ev["data_gaps"] == ["fundamentals"]
    db.close()


def test_default_record_seam_writes_rows_through_db(tmp_path):
    from argosy.state.models import HoldingReview

    db = _sqlite_session(tmp_path, "seam.db")
    run_holdings_review(
        db=db, user_id="ariel", min_position_usd=5_000.0,
        holdings={"BIG": 50_000.0},
        fetchers={},
        decide=lambda t, *, context, bundle, user_id="ariel": StockDecisionOutput(
            ticker=t, verdict="HOLD", confidence="HIGH", reason="intact"),
        elevated_flags={}, always_review=frozenset(),
    )
    rows = db.query(HoldingReview).all()
    assert [(r.symbol, r.outcome) for r in rows] == [("BIG", "hold")]
    db.close()


def test_load_x10_sleeve_symbols_reads_current_plan(tmp_path):
    from argosy.services.stock_decision import load_x10_sleeve_symbols
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )
    from argosy.state.models import PlanVersion

    db = _sqlite_session(tmp_path, "x10.db")
    doc = TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="High-growth / high-potential",
                snapshot_category="Individual Stocks",
                sigma_class="high_growth_basket",
                target_pct=5.0,
                instruments=[
                    AllocationInstrument(symbol="TEM", role="primary", weight_within_class_pct=50.0),
                    AllocationInstrument(symbol="OKLO", role="primary", weight_within_class_pct=50.0),
                ],
            ),
            AllocationClassDoc(
                label="Dividend-quality income",
                snapshot_category="Dividend",
                sigma_class="dividend_quality",
                target_pct=12.0,
                instruments=[
                    AllocationInstrument(symbol="FUSA", role="primary", weight_within_class_pct=100.0),
                ],
            ),
        ],
        glide=[],
    )
    db.add(PlanVersion(
        user_id="ariel", role="current", version_label="v-test",
        target_allocation_json=doc.model_dump_json(),
    ))
    db.commit()
    assert load_x10_sleeve_symbols(db, "ariel") == frozenset({"TEM", "OKLO"})
    db.close()


def test_job_output_summary_surfaces_held_unverified():
    """FIX 3b: the job tick must carry held_unverified into last_output_summary —
    an actionable-but-unconfirmed verdict previously vanished from /api/jobs."""
    import asyncio

    from argosy.services.jobs.holdings_review import HoldingsReviewJob

    job = HoldingsReviewJob(
        user_id="ariel",
        session_factory=lambda: type("S", (), {"close": lambda self: None})(),
        review_fn=lambda session, user_id, min_position_usd: {
            "reviewed": 3, "actionable": 2, "written": 1,
            "held_unverified": 1, "elevated": ["TEM"], "verdicts": [],
        },
    )
    out = asyncio.run(job.tick())
    assert out["held_unverified"] == 1
    assert job.last_output_summary["held_unverified"] == 1
