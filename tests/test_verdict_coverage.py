"""All-holdings verdict-coverage tests.

Every seam (now / book_loader / decide_fn) is injected — no network, no LLM,
no live DB (the ``session`` fixture is an isolated tmp SQLite at alembic head).
The book is fed via a fake ``book_loader`` so the report/escalation logic is
exercised without building a full portfolio snapshot.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.services.verdict_coverage import (
    enumerate_held_symbols,
    ensure_coverage,
    holdings_coverage_report,
)
from argosy.state.models import ActionProposal, User, Verdict


def _markers(session, symbol=None):
    q = session.query(ActionProposal).filter(
        ActionProposal.dedup_key.like("holdings_coverage_checked:%"),
        ActionProposal.status == "open",
    )
    rows = q.all()
    if symbol is not None:
        rows = [r for r in rows if r.dedup_key.endswith(f":{symbol.upper()}")]
    return rows

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


# Set by the ``session`` fixture so the fake decide seam can persist a verdict on
# a SEPARATE connection (same engine) — mirroring the fleet, which commits its
# verdict on its own session before ``asyncio.run`` returns. This is what lets the
# ground-truth fresh-verdict-row check (DEFECT B) be exercised cross-connection.
_TEST_SESSIONMAKER = None


@pytest.fixture
def session(alembic_engine_at_head):
    global _TEST_SESSIONMAKER
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    _TEST_SESSIONMAKER = SessionLocal
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()
    _TEST_SESSIONMAKER = None


# --- helpers ---------------------------------------------------------------
def _row(symbol, usd_k, *, asset_type="", details=""):
    return {
        "symbol": symbol,
        "usd_value_k": usd_k,
        "asset_type": asset_type,
        "details": details,
    }


def _fake_book(rows):
    """A book_loader stand-in — returns an object with a ``.total`` list."""
    return lambda _session, _user_id: SimpleNamespace(total=list(rows))


def _write_verdict(session, subject, *, age_days):
    """Insert a settled verdict directly so its age is deterministic (avoids the
    onupdate=utcnow that write_verdict would stamp)."""
    dt = NOW - timedelta(days=age_days)
    v = Verdict(
        user_id="ariel",
        subject=subject.upper(),
        verdict="HOLD",
        conviction="HIGH",
        settled=True,
        created_at=dt,
        updated_at=dt,
    )
    session.add(v)
    session.commit()
    return v.id


def _outcome_persists_verdict(outcome) -> bool:
    """Model PRODUCTION: which outcome envelopes actually WRITE a settled verdict
    row. ``approved`` (flow.py:805), ``trader_hold`` (flow.py:488) and
    ``us_situs_floor`` (flow.py:805 before the floor blocks) DO; ``verdict_defended``
    (re-affirm, no new row) and every error / no-verdict block do NOT."""
    if isinstance(outcome, dict):
        status, blocked_by = outcome.get("status"), outcome.get("blocked_by")
    else:
        status, blocked_by = getattr(outcome, "status", None), getattr(outcome, "blocked_by", None)
    status, blocked_by = str(status or "").lower(), str(blocked_by or "").lower()
    if status == "approved":
        return True
    if status == "blocked" and blocked_by in {"trader_hold", "us_situs_floor"}:
        return True
    return False


class _RecordingDecide:
    """Fake escalation seam — records calls, never touches the fleet, and (like
    the real fleet) COMMITS a settled verdict on a SEPARATE connection when the
    outcome is one prod would persist.

    ``fail_on`` raises; ``returns`` maps a subject to a structured outcome dict
    (e.g. {"status": "error"} or a defended block); default is the production
    completing envelope for a fresh re-verdict — status="approved". ``no_write``
    names subjects whose verdict write is SWALLOWED (simulating
    ``_record_settled_verdict`` catching a registry-write failure) — the outcome
    envelope still returns "complete" but NO row is persisted, which the
    ground-truth check must treat as retry, never covered."""

    def __init__(self, fail_on=(), returns=None, no_write=()):
        self.calls = []
        self._fail_on = set(fail_on)
        self._returns = dict(returns or {})
        self._no_write = set(no_write)

    def __call__(self, *, user_id, subject, cited_new_facts, reason):
        self.calls.append({"subject": subject, "cited": list(cited_new_facts), "reason": reason})
        if subject in self._fail_on:
            raise RuntimeError(f"boom {subject}")
        if subject in self._returns:
            outcome = self._returns[subject]
        else:
            outcome = {"status": "approved", "subject": subject}
        if subject not in self._no_write and _outcome_persists_verdict(outcome):
            self._commit_settled_verdict(user_id, subject)
        return outcome

    @staticmethod
    def _commit_settled_verdict(user_id, subject):
        """Persist + COMMIT a settled verdict on its own connection (supersedes
        any prior settled row so the partial-unique index is honored)."""
        from argosy.services.verdict_registry import write_verdict

        assert _TEST_SESSIONMAKER is not None, "session fixture must set the sessionmaker"
        ws = _TEST_SESSIONMAKER()
        try:
            write_verdict(
                ws, user_id=user_id, subject=subject.upper(),
                verdict="HOLD", conviction="HIGH", settled=True,
            )
            ws.commit()
        finally:
            ws.close()


# ---------------------------------------------------------------------------
# 1) Enumeration — ALL held symbols incl. ETFs; cash + real estate excluded.
# ---------------------------------------------------------------------------
def test_enumerate_includes_etfs_excludes_cash_and_real_estate():
    rows = [
        _row("NVDA", 2500.0),                       # stock
        _row("CSPX", 900.0),                        # etf
        _row("O", 60.0),                            # reit
        _row("-", 120.0, asset_type="cash"),        # cash sentinel (symbol-less)
        _row("", 69.0, asset_type="real estate"),   # physical real estate (symbol-less)
    ]
    held = enumerate_held_symbols(rows)
    syms = {h.symbol for h in held}
    assert syms == {"NVDA", "CSPX", "O"}
    by = {h.symbol: h for h in held}
    assert by["NVDA"].structure == "stock"
    assert by["CSPX"].structure == "etf"
    assert by["O"].structure == "reit"
    # Largest holding first.
    assert held[0].symbol == "NVDA"


def test_enumerate_keeps_symbol_bearing_securities_despite_raw_asset_type():
    """FIX #1: listed REIT/property-ETF rows (O, IWDP) carry a raw
    asset_type of 'Real Estate' on the live book but ARE priceable securities —
    they must be enumerated and get the A2 note. Only SYMBOL-LESS rows are
    physical real estate / cash. A bond ETF mislabelled 'Cash' likewise stays."""
    rows = [
        _row("O", 18.7, asset_type="Real Estate"),      # listed REIT
        _row("IWDP", 34.4, asset_type="Real Estate"),   # listed property ETF
        _row("IBTA", 50.0, asset_type="Cash"),          # bond ETF mislabelled Cash
        _row("-", 100.0, asset_type="Cash"),            # physical/deployable cash
        _row("", 69.0, asset_type="Real Estate"),       # physical property
    ]
    held = enumerate_held_symbols(rows)
    by = {h.symbol: h for h in held}
    assert set(by) == {"O", "IWDP", "IBTA"}
    assert by["O"].structure == "reit"
    assert by["IWDP"].structure == "etf"
    assert by["IBTA"].structure == "etf"


def test_enumerate_sums_duplicate_symbols():
    held = enumerate_held_symbols([_row("CSPX", 100.0), _row("cspx", 50.0)])
    assert len(held) == 1
    assert held[0].symbol == "CSPX"
    assert held[0].usd_value_k == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# 2) Classification — covered / stale / uncovered by verdict age.
# ---------------------------------------------------------------------------
def test_report_classifies_covered_stale_uncovered(session):
    _write_verdict(session, "NVDA", age_days=10)    # fresh -> covered
    _write_verdict(session, "CSPX", age_days=200)   # old   -> stale
    # EXUS has NO verdict -> uncovered
    loader = _fake_book([_row("NVDA", 2500.0), _row("CSPX", 900.0), _row("EXUS", 300.0)])

    rep = holdings_coverage_report(session, "ariel", now=NOW, max_age_days=90, book_loader=loader)

    by = {i.symbol: i for i in rep.items}
    assert by["NVDA"].coverage_status == "covered"
    assert by["NVDA"].verdict_age_days == pytest.approx(10.0)
    assert by["CSPX"].coverage_status == "stale"
    assert by["EXUS"].coverage_status == "uncovered"
    assert by["EXUS"].verdict_id is None and by["EXUS"].verdict_age_days is None
    assert rep.totals == {"held": 3, "covered": 1, "stale": 1, "uncovered": 1}


def test_report_is_read_only(session):
    loader = _fake_book([_row("EXUS", 300.0), _row("NVDA", 2500.0)])
    before = session.query(Verdict).count()
    holdings_coverage_report(session, "ariel", now=NOW, book_loader=loader)
    # No verdict rows written, no other side effect.
    assert session.query(Verdict).count() == before == 0


def test_etf_a2_metadata_limit_recorded_not_faked(session):
    loader = _fake_book([_row("CSPX", 900.0), _row("NVDA", 2500.0)])
    rep = holdings_coverage_report(session, "ariel", now=NOW, book_loader=loader)
    by = {i.symbol: i for i in rep.items}
    # ETF vehicle -> A2 limitation recorded honestly.
    assert by["CSPX"].a2_metadata_limited is True
    assert by["CSPX"].a2_note and "data-blocked" in by["CSPX"].a2_note
    assert "NOT a fabricated" in by["CSPX"].a2_note
    # Single stock -> no A2 vehicle limitation.
    assert by["NVDA"].a2_metadata_limited is False
    assert by["NVDA"].a2_note is None


# ---------------------------------------------------------------------------
# 3) ensure_coverage — escalate uncovered+stale only, capped, most-overdue
#    first, idempotent, best-effort; fully-covered = no-op.
# ---------------------------------------------------------------------------
def test_ensure_coverage_escalates_only_uncovered_and_stale(session):
    _write_verdict(session, "NVDA", age_days=10)    # covered -> NOT escalated
    _write_verdict(session, "CSPX", age_days=200)   # stale   -> escalated
    loader = _fake_book([_row("NVDA", 2500.0), _row("CSPX", 900.0), _row("EXUS", 300.0)])
    decide = _RecordingDecide()

    summary = ensure_coverage(
        session, "ariel", now=NOW, max_age_days=90, limit=10,
        decide_fn=decide, book_loader=loader,
    )

    fired = {c["subject"] for c in decide.calls}
    assert fired == {"CSPX", "EXUS"}  # covered NVDA skipped
    assert summary["escalated"] == 2
    assert summary["covered"] == 1 and summary["stale"] == 1 and summary["uncovered"] == 1


def test_ensure_coverage_respects_limit_most_overdue_first(session):
    # Two stale (different ages) + one uncovered. limit=2 -> uncovered first,
    # then the OLDEST stale; the newer stale waits for next run.
    _write_verdict(session, "CSPX", age_days=120)   # stale, newer
    _write_verdict(session, "EXUS", age_days=400)   # stale, oldest
    loader = _fake_book([
        _row("NVDA", 2500.0),   # uncovered
        _row("CSPX", 900.0),
        _row("EXUS", 300.0),
    ])
    decide = _RecordingDecide()

    summary = ensure_coverage(
        session, "ariel", now=NOW, max_age_days=90, limit=2,
        decide_fn=decide, book_loader=loader,
    )

    fired = [c["subject"] for c in decide.calls]
    assert summary["escalated"] == 2
    assert fired[0] == "NVDA"       # uncovered is most overdue
    assert fired[1] == "EXUS"       # oldest stale next
    assert "CSPX" not in fired      # capped out; next run


def test_ensure_coverage_idempotent_within_run(session):
    # Same symbol appearing twice in the book is fired at most once.
    loader = _fake_book([_row("EXUS", 300.0), _row("exus", 100.0)])
    decide = _RecordingDecide()
    ensure_coverage(session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader)
    assert [c["subject"] for c in decide.calls] == ["EXUS"]


def test_ensure_coverage_best_effort_one_failure_does_not_abort(session):
    loader = _fake_book([_row("NVDA", 2500.0), _row("CSPX", 900.0)])
    decide = _RecordingDecide(fail_on={"NVDA"})

    summary = ensure_coverage(
        session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader,
    )
    # NVDA raised, CSPX still fired.
    assert {c["subject"] for c in decide.calls} == {"NVDA", "CSPX"}
    assert summary["escalated"] == 1
    assert any("NVDA" in e for e in summary["errors"])


def test_ensure_coverage_fully_covered_is_noop(session):
    _write_verdict(session, "NVDA", age_days=5)
    _write_verdict(session, "CSPX", age_days=5)
    loader = _fake_book([_row("NVDA", 2500.0), _row("CSPX", 900.0)])
    decide = _RecordingDecide()

    summary = ensure_coverage(
        session, "ariel", now=NOW, max_age_days=90, limit=10,
        decide_fn=decide, book_loader=loader,
    )
    assert decide.calls == []
    assert summary["escalated"] == 0 and summary["candidates"] == 0


def test_ensure_coverage_threads_a2_note_into_escalation(session):
    loader = _fake_book([_row("CSPX", 900.0)])   # etf, uncovered
    decide = _RecordingDecide()
    ensure_coverage(session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader)
    [call] = decide.calls
    assert call["subject"] == "CSPX"
    # The honest A2 limitation is carried as a cited fact, never a fake verdict.
    assert any("data-blocked" in c for c in call["cited"])


def test_real_estate_etf_and_reit_get_a2_note(session):
    """FIX #1: O (REIT) + IWDP (property ETF) carry raw asset_type 'Real Estate'
    but ARE securities — they enumerate AND receive the honest A2 note."""
    loader = _fake_book([
        _row("O", 18.7, asset_type="Real Estate"),
        _row("IWDP", 34.4, asset_type="Real Estate"),
    ])
    rep = holdings_coverage_report(session, "ariel", now=NOW, book_loader=loader)
    by = {i.symbol: i for i in rep.items}
    assert set(by) == {"O", "IWDP"}
    for sym in ("O", "IWDP"):
        assert by[sym].coverage_status == "uncovered"
        assert by[sym].a2_metadata_limited is True
        assert by[sym].a2_note and "NOT a fabricated" in by[sym].a2_note


# ---------------------------------------------------------------------------
# FIX #2 — defended-stale converges via a cooldown marker (no re-fire loop).
# ---------------------------------------------------------------------------
def test_defended_stale_writes_cooldown_and_converges(session):
    # Three STALE names of decreasing age; the fleet DEFENDS each (re-affirms the
    # standing verdict). With limit=1, successive sweeps must rotate through ALL
    # THREE instead of re-firing the oldest every time.
    _write_verdict(session, "OLD1", age_days=400)
    _write_verdict(session, "OLD2", age_days=300)
    _write_verdict(session, "WAIT", age_days=200)
    loader = _fake_book([_row("OLD1", 300.0), _row("OLD2", 200.0), _row("WAIT", 100.0)])
    defended = {"status": "blocked", "blocked_by": "verdict_defended"}
    decide = _RecordingDecide(returns={"OLD1": defended, "OLD2": defended, "WAIT": defended})

    fired = []
    for _ in range(3):
        summary = ensure_coverage(
            session, "ariel", now=NOW, max_age_days=90, limit=1,
            decide_fn=decide, book_loader=loader,
        )
        session.commit()
        # Each defended sweep escalates exactly one (never zero -> no dead loop).
        assert summary["escalated"] == 1 and summary["failed"] == 0
        fired.append(decide.calls[-1]["subject"])

    # Oldest-first, each fired ONCE — coverage converged across the whole book.
    assert fired == ["OLD1", "OLD2", "WAIT"]
    # A ``checked`` cooldown marker now suppresses each defended name.
    assert {m.dedup_key for m in _markers(session)} == {
        "holdings_coverage_checked:OLD1",
        "holdings_coverage_checked:OLD2",
        "holdings_coverage_checked:WAIT",
    }
    # A 4th sweep is a no-op — every name is in cooldown ("covered again").
    summary = ensure_coverage(
        session, "ariel", now=NOW, max_age_days=90, limit=1,
        decide_fn=decide, book_loader=loader,
    )
    assert summary["escalated"] == 0 and summary["candidates"] == 0
    assert summary["covered"] == 3 and summary["stale"] == 0


# ---------------------------------------------------------------------------
# FIX #3 — structured fleet FAILURE is not counted as covered + does not starve.
# ---------------------------------------------------------------------------
def test_structured_error_outcome_is_failed_not_escalated_and_rotates(session):
    # BIG (largest) is uncovered and the fleet returns a structured error; S1/S2
    # are uncovered too. limit=1. BIG must NOT be marked covered, and its short
    # retry cooldown must let S1 then S2 get slots next sweeps (no starvation).
    loader = _fake_book([_row("BIG", 900.0), _row("S1", 200.0), _row("S2", 100.0)])
    decide = _RecordingDecide(returns={"BIG": {"status": "error"}})

    s1 = ensure_coverage(session, "ariel", now=NOW, limit=1, decide_fn=decide, book_loader=loader)
    session.commit()
    assert decide.calls[-1]["subject"] == "BIG"
    assert s1["escalated"] == 0 and s1["failed"] == 1
    assert any("BIG" in e for e in s1["errors"])
    # BIG got a retry marker (not permanently covered).
    [big_marker] = _markers(session, "BIG")
    import json
    assert json.loads(big_marker.suggested_payload)["state"] == "retry"

    # Next sweeps rotate to S1, then S2 — the failing BIG no longer hogs the slot.
    ensure_coverage(session, "ariel", now=NOW, limit=1, decide_fn=decide, book_loader=loader)
    session.commit()
    ensure_coverage(session, "ariel", now=NOW, limit=1, decide_fn=decide, book_loader=loader)
    session.commit()
    fired = [c["subject"] for c in decide.calls]
    assert fired == ["BIG", "S1", "S2"]


# ---------------------------------------------------------------------------
# DEFECT A — cooldown markers are INTERNAL and never surface to the user.
# ---------------------------------------------------------------------------
def test_is_coverage_marker_dedup_key_matches_both_prefixes():
    from argosy.services.verdict_coverage import is_coverage_marker_dedup_key

    assert is_coverage_marker_dedup_key("holdings_coverage_checked:NVDA")
    assert is_coverage_marker_dedup_key("holdings_coverage_retry:NVDA")
    assert not is_coverage_marker_dedup_key("flagsig:NVDA")
    assert not is_coverage_marker_dedup_key("allocate:something")
    assert not is_coverage_marker_dedup_key(None)
    assert not is_coverage_marker_dedup_key("")


def test_coverage_markers_never_surface_in_user_proposal_list(session):
    """DEFECT A — the sweep writes cooldown markers as open action_proposals for
    the dedup mechanism, but they are internal bookkeeping and must NEVER appear
    in the user-facing proposal list / inbox / home greeting (all read via
    list_open_action_proposals)."""
    from argosy.services.action_proposals import list_open_action_proposals

    # Drive one COMPLETING (checked) marker + one FAILING (retry) marker.
    loader = _fake_book([_row("AAA", 300.0), _row("BBB", 200.0)])
    decide = _RecordingDecide(returns={"BBB": {"status": "error"}})
    ensure_coverage(session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader)
    session.commit()

    # Both markers exist in the raw table (checked + retry share the prefix)...
    raw = _markers(session)
    assert {m.dedup_key for m in raw} == {
        "holdings_coverage_checked:AAA",
        "holdings_coverage_checked:BBB",
    }
    # ...but NONE of them surface to the user — the list is empty of coverage
    # chatter (here it is empty entirely, since only markers exist).
    visible = list_open_action_proposals(session, "ariel")
    assert all(
        not (p.dedup_key or "").startswith("holdings_coverage_") for p in visible
    )
    assert visible == []


def test_real_proposals_still_surface_alongside_markers(session):
    """DEFECT A regression guard — the marker exclusion must NOT drop genuine
    user-facing proposals."""
    from argosy.services.action_proposals import list_open_action_proposals

    session.add(
        ActionProposal(
            user_id="ariel",
            summary="real decision",
            rationale_md="please decide",
            suggested_payload="{}",
            severity="warning",
            surfaced_at=NOW,
            expires_at=NOW + timedelta(days=7),
            status="open",
            kind="rebalance",
            dedup_key="rebalance:core",
            execution_state="proposed",
        )
    )
    session.commit()
    loader = _fake_book([_row("AAA", 300.0)])
    ensure_coverage(session, "ariel", now=NOW, limit=10, decide_fn=_RecordingDecide(), book_loader=loader)
    session.commit()

    visible = list_open_action_proposals(session, "ariel")
    assert [p.dedup_key for p in visible] == ["rebalance:core"]


def _add_proposal(session, *, dedup_key, summary, kind="note_only", severity="info", surfaced_at):
    session.add(
        ActionProposal(
            user_id="ariel",
            summary=summary,
            rationale_md=summary,
            suggested_payload="{}",
            severity=severity,
            surfaced_at=surfaced_at,
            expires_at=surfaced_at + timedelta(days=30),
            status="open",
            kind=kind,
            dedup_key=dedup_key,
            execution_state="proposed",
        )
    )


def test_digest_excludes_markers_before_limit_and_count(session):
    """DECISIVE (Defect A) — the email digest applies LIMIT 5 + an open-count.
    5 coverage markers (surfaced NEWER than the one real proposal) must NOT crowd
    the real proposal out of the body, and must NOT inflate open_proposal_count /
    has_any_activity. Exclusion is pushed into the SQL BEFORE the limit + count."""
    from argosy.services.email_digest import build_weekly_digest

    # 5 markers (3 checked + 2 retry), all surfaced NOW (newest -> would occupy
    # every LIMIT-5 slot without the fix).
    for i in range(3):
        _add_proposal(
            session, dedup_key=f"holdings_coverage_checked:SYM{i}",
            summary=f"coverage checked SYM{i}", surfaced_at=NOW,
        )
    for i in range(2):
        _add_proposal(
            session, dedup_key=f"holdings_coverage_retry:BAD{i}",
            summary=f"coverage retry BAD{i}", surfaced_at=NOW,
        )
    # One REAL proposal, surfaced OLDER so without the fix it would be dropped.
    _add_proposal(
        session, dedup_key="rebalance:core", summary="real rebalance decision",
        kind="rebalance", severity="warning", surfaced_at=NOW - timedelta(hours=1),
    )
    session.commit()

    digest = build_weekly_digest(session, "ariel", now=NOW)

    # The real proposal is visible; no marker leaked into the body.
    assert len(digest.open_proposals) == 1
    assert digest.open_proposals[0].summary == "real rebalance decision"
    # Count reflects ONLY the real proposal (markers excluded before the count).
    assert digest.summary.open_proposal_count == 1
    # has_any_activity is driven by the real proposal, not marker chatter.
    assert digest.has_any_activity is True


def test_digest_markers_only_is_no_activity(session):
    """DECISIVE (Defect A) — a book with ONLY coverage markers open must read as
    NO activity (open_count 0, has_any_activity False) — markers alone never
    trigger an 'activity' digest email."""
    from argosy.services.email_digest import build_weekly_digest

    for i in range(5):
        _add_proposal(
            session, dedup_key=f"holdings_coverage_checked:SYM{i}",
            summary=f"coverage checked SYM{i}", surfaced_at=NOW,
        )
    session.commit()

    digest = build_weekly_digest(session, "ariel", now=NOW)
    assert digest.open_proposals == []
    assert digest.summary.open_proposal_count == 0
    assert digest.has_any_activity is False


def test_digest_like_exclusion_does_not_treat_underscore_as_wildcard(session):
    """LOW-SEV nit — '_' is a single-char LIKE wildcard, so an unescaped
    'holdings_coverage_checked:%' would also exclude a legit key like
    'holdingsXcoverageYchecked:...'. The exclusion escapes '_', so this legit
    proposal MUST remain visible + counted."""
    from argosy.services.email_digest import build_weekly_digest

    _add_proposal(
        session, dedup_key="holdingsXcoverageYchecked:LEGIT",
        summary="legit not a marker", kind="rebalance", severity="warning",
        surfaced_at=NOW,
    )
    session.commit()
    digest = build_weekly_digest(session, "ariel", now=NOW)
    assert [p.summary for p in digest.open_proposals] == ["legit not a marker"]
    assert digest.summary.open_proposal_count == 1
    assert digest.has_any_activity is True


# ---------------------------------------------------------------------------
# DEFECT B — completeness is FAIL-CLOSED (allowlist). Unknown/None/{}/infra/
# blocked_* -> retry (never covered); only fresh re-verdict or verdict_defended.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "outcome",
    [
        None,
        {},
        {"status": "unknown_weirdness"},
        {"status": "reevaluated"},  # TEST-ONLY legacy status: no longer completing
        {"status": "infra-block"},
        {"status": "blocked", "blocked_by": "infra-block"},
        {"status": "blocked", "blocked_by": "open_error"},
        {"status": "blocked", "blocked_by": "analysts_error"},
        {"status": "blocked", "blocked_by": "flow_error"},
        {"status": "blocked"},  # blocked with NO blocked_by
        # Real funnel blocked_by values that DO NOT settle a verdict (could not
        # decide) — must be retry, never covered (verified flow.py:505-714).
        {"status": "blocked", "blocked_by": "trader_insufficient_data"},
        {"status": "blocked", "blocked_by": "risk_team"},
        {"status": "blocked", "blocked_by": "fund_manager"},
        {"status": "blocked", "blocked_by": "plan_critique_red"},
        {"status": "blocked", "blocked_by": "sleeve_fit_invalid"},
        {"status": "error"},
        {"status": "quorum_failed"},
        # NOTE: us_situs_floor is NOT here — in prod it WRITES a settled verdict
        # before the floor blocks, so ground-truth makes it covered (tested below).
    ],
)
def test_fail_closed_non_completing_routes_to_retry_not_covered(session, outcome):
    import json

    loader = _fake_book([_row("EXUS", 300.0)])
    decide = _RecordingDecide(returns={"EXUS": outcome})
    summary = ensure_coverage(
        session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader
    )
    session.commit()
    assert summary["escalated"] == 0 and summary["failed"] == 1
    assert summary["covered"] == 0  # NEVER faked to covered
    [m] = _markers(session, "EXUS")
    assert json.loads(m.suggested_payload)["state"] == "retry"


@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "approved"},
        {"status": "blocked", "blocked_by": "trader_hold"},
        {"status": "blocked", "blocked_by": "verdict_defended"},
    ],
)
def test_fail_closed_completing_outcomes_mark_checked(session, outcome):
    import json

    loader = _fake_book([_row("EXUS", 300.0)])
    decide = _RecordingDecide(returns={"EXUS": outcome})
    summary = ensure_coverage(
        session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader
    )
    session.commit()
    assert summary["escalated"] == 1 and summary["failed"] == 0
    [m] = _markers(session, "EXUS")
    assert json.loads(m.suggested_payload)["state"] == "checked"


def test_trader_hold_that_writes_verdict_is_covered_with_row(session):
    """DECISIVE (Defect B, ground truth) — a real DeepDecisionOutcome trader_hold
    whose decide_fn ACTUALLY writes+commits a settled verdict → covered/checked,
    AND a settled verdict row now exists. Not status-mapping: the row is proof."""
    import json

    from argosy.services.decision_funnel.deep_decision import DeepDecisionOutcome

    hold = DeepDecisionOutcome(
        ticker="NVDA", status="blocked", decision_run_id=7,
        blocked_reason="Trader returned HOLD: durable thesis intact",
        blocked_by="trader_hold",
    )
    loader = _fake_book([_row("NVDA", 2500.0)])  # uncovered, largest
    decide = _RecordingDecide(returns={"NVDA": hold})  # writes+commits a verdict
    summary = ensure_coverage(
        session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader
    )
    session.commit()
    assert summary["escalated"] == 1 and summary["failed"] == 0
    # GROUND TRUTH: a settled verdict row exists (written on a separate conn).
    assert session.query(Verdict).filter_by(subject="NVDA", settled=True).count() == 1
    assert json.loads(_markers(session, "NVDA")[0].suggested_payload)["state"] == "checked"


def test_trader_hold_with_swallowed_write_is_retry_not_covered(session):
    """DECISIVE (Defect B, Sol's reproduction) — a trader_hold envelope whose
    verdict WRITE was swallowed (``_record_settled_verdict`` catches the failure at
    flow.py:847 yet the outcome still returns) leaves NO fresh row. The status
    allowlist would have FALSE-COVERED it for 90 days; ground truth marks it
    RETRY, and NO verdict row exists."""
    import json

    from argosy.services.decision_funnel.deep_decision import DeepDecisionOutcome

    hold = DeepDecisionOutcome(
        ticker="NVDA", status="blocked", decision_run_id=7,
        blocked_reason="hold", blocked_by="trader_hold",
    )
    loader = _fake_book([_row("NVDA", 2500.0)])
    decide = _RecordingDecide(returns={"NVDA": hold}, no_write={"NVDA"})  # write swallowed
    summary = ensure_coverage(
        session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader
    )
    session.commit()
    assert summary["escalated"] == 0 and summary["failed"] == 1
    assert session.query(Verdict).filter_by(subject="NVDA").count() == 0  # nothing settled
    assert json.loads(_markers(session, "NVDA")[0].suggested_payload)["state"] == "retry"


def test_us_situs_floor_wrote_verdict_is_covered(session):
    """DECISIVE (Defect B) — us_situs_floor WRITES a settled actionable verdict
    (flow.py:805) BEFORE the deterministic floor blocks the proposal
    (deep_decision.py:378) and never retracts it. Ground truth sees the fresh row
    → covered (the old status-allowlist wrongly retried it)."""
    import json

    loader = _fake_book([_row("SOFI", 40.0)])
    decide = _RecordingDecide(returns={"SOFI": {"status": "blocked", "blocked_by": "us_situs_floor"}})
    summary = ensure_coverage(
        session, "ariel", now=NOW, limit=10, decide_fn=decide, book_loader=loader
    )
    session.commit()
    assert summary["escalated"] == 1 and summary["failed"] == 0
    assert session.query(Verdict).filter_by(subject="SOFI", settled=True).count() == 1
    assert json.loads(_markers(session, "SOFI")[0].suggested_payload)["state"] == "checked"


def test_verdict_defended_is_covered_without_new_row(session):
    """DECISIVE (Defect B) — the re-affirm path writes NO new verdict row, so it is
    covered via the explicit ``verdict_defended`` status special-case (case (a)),
    NOT via a fresh-row check. Verify no row was added."""
    import json

    _write_verdict(session, "OLD", age_days=200)  # existing stale standing verdict
    before = session.query(Verdict).count()
    loader = _fake_book([_row("OLD", 300.0)])
    decide = _RecordingDecide(returns={"OLD": {"status": "blocked", "blocked_by": "verdict_defended"}})
    summary = ensure_coverage(
        session, "ariel", now=NOW, max_age_days=90, limit=10,
        decide_fn=decide, book_loader=loader,
    )
    session.commit()
    assert summary["escalated"] == 1 and summary["failed"] == 0
    assert session.query(Verdict).count() == before  # no new row written
    assert json.loads(_markers(session, "OLD")[0].suggested_payload)["state"] == "checked"


def test_ground_truth_sees_verdict_committed_on_separate_connection(session, alembic_engine_at_head):
    """DECISIVE (Defect B, visibility) — the fleet commits its verdict on a
    PHYSICALLY DISTINCT connection; ``_coverage_attempt_completed`` must end the
    coverage read-txn and SEE that committed row. Uses a NullPool writer engine so
    the write genuinely happens on a different DBAPI connection (a pooled second
    Session could hand back the SAME physical connection and make this vacuous).
    A missing row → False (retry), never a false-cover."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as _Session
    from sqlalchemy.pool import NullPool

    from argosy.services.verdict_coverage import (
        _coverage_attempt_completed,
        _settled_verdict_identity,
    )
    from argosy.services.verdict_registry import write_verdict

    # Capture pre-identity, then RELEASE the coverage read-txn (mirrors
    # ensure_coverage committing before decide_fn) so the foreign write doesn't hit
    # SQLite's single-writer lock.
    pre_id, pre_upd = _settled_verdict_identity(session, user_id="ariel", subject="EXUS")
    assert (pre_id, pre_upd) == (None, None)
    cov_raw = session.connection().connection.dbapi_connection
    session.commit()

    # Writer on a SEPARATE engine w/ NullPool → guaranteed distinct raw connection.
    writer_engine = create_engine(str(alembic_engine_at_head.url), poolclass=NullPool)
    try:
        with _Session(writer_engine) as ws:
            write_raw = ws.connection().connection.dbapi_connection
            assert write_raw is not cov_raw  # physically distinct connections
            write_verdict(ws, user_id="ariel", subject="EXUS", verdict="HOLD",
                          conviction="HIGH", settled=True)
            ws.commit()
    finally:
        writer_engine.dispose()

    # The coverage session must re-read cross-connection and find the committed row.
    assert _coverage_attempt_completed(
        session, user_id="ariel", subject="EXUS",
        pre_id=pre_id, pre_updated_at=pre_upd, outcome={"status": "approved"},
    ) is True
    # A subject with NO committed verdict → False (safe retry direction).
    pre_id2, pre_upd2 = _settled_verdict_identity(session, user_id="ariel", subject="NONE")
    assert _coverage_attempt_completed(
        session, user_id="ariel", subject="NONE",
        pre_id=pre_id2, pre_updated_at=pre_upd2, outcome={"status": "approved"},
    ) is False


def test_in_place_refresh_same_run_id_is_covered(session):
    """DECISIVE (Defect B, ISSUE 1) — a re-verdict via write_verdict's IN-PLACE
    branch (same source_decision_run_id → UPDATE the existing row, SAME id, only
    bumped updated_at, verdict_registry.py:167-194) must be COVERED. Keying on id
    alone (old code) missed this and would endless-retry the holding."""
    import json
    import time

    from argosy.services.verdict_registry import write_verdict

    # Pre-seed a STALE settled verdict with a specific source_decision_run_id (so it
    # is a candidate), on a separate connection.
    _seed_run_id = 4242
    ws0 = _TEST_SESSIONMAKER()
    try:
        ws0.add(
            Verdict(
                user_id="ariel", subject="EXUS", verdict="HOLD", conviction="MED",
                settled=True, source_decision_run_id=_seed_run_id,
                created_at=NOW - timedelta(days=200), updated_at=NOW - timedelta(days=200),
            )
        )
        ws0.commit()
    finally:
        ws0.close()

    # decide_fn refreshes the SAME (subject, run) row in place → same id, bumped
    # updated_at. Distinct decide seam (not _RecordingDecide) to hit this path.
    def _refresh_in_place(*, user_id, subject, cited_new_facts, reason):
        time.sleep(0.01)  # ensure updated_at strictly advances (sub-second clock)
        wsr = _TEST_SESSIONMAKER()
        try:
            write_verdict(
                wsr, user_id=user_id, subject=subject.upper(), verdict="HOLD",
                conviction="HIGH", settled=True, source_decision_run_id=_seed_run_id,
            )
            wsr.commit()
        finally:
            wsr.close()
        return {"status": "blocked", "blocked_by": "trader_hold"}

    loader = _fake_book([_row("EXUS", 300.0)])  # stale (age 200) → candidate
    summary = ensure_coverage(
        session, "ariel", now=NOW, max_age_days=90, limit=10,
        decide_fn=_refresh_in_place, book_loader=loader,
    )
    session.commit()

    # The in-place refresh IS a completed re-verdict → covered/checked, not retry.
    assert summary["escalated"] == 1 and summary["failed"] == 0
    [m] = _markers(session, "EXUS")
    assert json.loads(m.suggested_payload)["state"] == "checked"
    # Still exactly one settled row (in-place UPDATE, not a new insert).
    assert session.query(Verdict).filter_by(subject="EXUS", settled=True).count() == 1


# ---------------------------------------------------------------------------
# DEFECT C — a session close() that raises must never escape tick().
# ---------------------------------------------------------------------------
def test_tick_does_not_escape_on_session_close_failure(monkeypatch):
    import asyncio

    from argosy.orchestrator.loops import holdings_coverage_sweep as hcs

    class _BoomCloseSession:
        def __init__(self):
            self.close_attempted = False

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            self.close_attempted = True
            raise RuntimeError("close boom")

    sess = _BoomCloseSession()
    # Neutralize the actual coverage work — this test targets the finally/close
    # guard, not the escalation logic.
    monkeypatch.setattr(hcs, "ensure_coverage", lambda *a, **k: {"held": 0, "escalated": 0})

    loop = hcs.HoldingsCoverageSweepLoop(
        enabled=True,
        user_id="ariel",
        session_factory=lambda: sess,
        now_fn=lambda: NOW,
    )
    summary = asyncio.run(loop.tick())  # MUST NOT raise
    assert sess.close_attempted is True
    assert isinstance(summary, dict)


def test_tick_does_not_escape_on_raising_clock():
    """DECISIVE (Defect C) — a clock (now_fn) that raises must NOT escape tick().
    The clock is resolved INSIDE the guarded body; previously it was called
    before the guard and propagated out."""
    import asyncio

    from argosy.orchestrator.loops import holdings_coverage_sweep as hcs

    def _boom_clock():
        raise RuntimeError("clock boom")

    def _unreachable_factory():
        raise AssertionError("session_factory must not be reached — clock failed first")

    loop = hcs.HoldingsCoverageSweepLoop(
        enabled=True,
        user_id="ariel",
        session_factory=_unreachable_factory,
        now_fn=_boom_clock,
    )
    summary = asyncio.run(loop.tick())  # MUST NOT raise
    assert isinstance(summary, dict) and "error" in summary
    assert "clock boom" in summary["error"]


# ---------------------------------------------------------------------------
# DEFECT D — cadence is DAILY so RETRY_COOLDOWN_DAYS=2 is coherent.
# ---------------------------------------------------------------------------
def test_cron_is_daily_for_retry_cadence_coherence():
    from argosy.orchestrator.loops.holdings_coverage_sweep import (
        _DEFAULT_CRON,
        holdings_coverage_sweep_metadata,
    )

    assert _DEFAULT_CRON == "0 8 * * *"  # daily, not weekly "0 8 * * 0"
    md = holdings_coverage_sweep_metadata()
    assert md.schedule_cron == "0 8 * * *"
    assert "daily" in md.schedule_human.lower()
    assert "weekly" not in md.schedule_human.lower()
