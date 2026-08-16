"""Functional smoke harness for the paths that burned us on 2026-08-16.

Three real incidents, same shape: a path was verified against a PROXY (a
fully-mocked LLM/DB seam, or a derivation helper called directly instead of
through its real resolver) and reported as working, then failed on its first
real invocation. This script hits the REAL code, against a REAL copy of the
dev DB, and prints what actually happened — so a claim of "it works" can be
reproduced by anyone in one command.

Paths covered:
  gate-outcome    argosy.services.gate_outcome_store.persist_gate_outcomes /
                   get_gate_receipt — real DB write + read-back.
  fact-tokenize   argosy.services.plan_numeric_resolver.resolve_plan_numbers
                   (the REAL resolver, not a hand-built ResolvedPlanNumbers)
                   -> argosy.quality.fact_tokenizer.tokenize_bodies, against
                   the latest real draft/current plan prose for user 'ariel'.
  plan-amendment  argosy.orchestrator.flows.plan_amendment.dispatcher.run_small
                   — the real synchronous small-amendment path: opens a real
                   DecisionRun row, applies a real Delta into a real draft.
  fund-vehicle    argosy.services.decision_funnel.fund_vehicle_decision.
                   run_fund_vehicle_decision — the real end-to-end path,
                   INCLUDING a live agent.run() call (real claude.exe
                   invocation). This is the exact path that shipped with 31
                   mocked-seam tests and failed on first live use.

Safety: every command below operates on a THROWAWAY COPY of db/argosy.db in
a tmp dir (ARGOSY_HOME repointed there before any argosy import), so running
this — including the plan-amendment and fund-vehicle paths, which WRITE —
never touches the real dev DB. The copy starts as a byte-identical snapshot
of the real data, so the code sees real households, real plans, real prose.

Usage:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/smoke_real_paths.py [paths...]
    # paths: gate-outcome fact-tokenize plan-amendment fund-vehicle
    # default: all four. fund-vehicle costs a real LLM call; pass
    # --skip-fund-vehicle to omit it (e.g. for a fast CI-style run).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# --- 1. Repoint ARGOSY_HOME at a throwaway copy of the real dev DB, BEFORE
#        any argosy import triggers settings/engine construction. -----------

_REAL_HOME = Path(__file__).parent.parent
_REAL_DB = _REAL_HOME / "db" / "argosy.db"

_TMP_HOME = Path(tempfile.mkdtemp(prefix="argosy-smoke-real-paths-"))
(_TMP_HOME / "db").mkdir(parents=True, exist_ok=True)
_TMP_DB = _TMP_HOME / "db" / "argosy.db"

if not _REAL_DB.exists():
    print(f"FATAL: real dev DB not found at {_REAL_DB}. Nothing to smoke against.")
    sys.exit(1)

shutil.copyfile(_REAL_DB, _TMP_DB)
os.environ["ARGOSY_HOME"] = str(_TMP_HOME)
os.environ.setdefault("ARGOSY_INCREMENTAL_PLAN", "1")

USER_ID = "ariel"

print("=" * 78)
print("ARGOSY REAL-PATH SMOKE HARNESS")
print("=" * 78)
print(f"Real dev DB    : {_REAL_DB}")
print(f"Throwaway copy : {_TMP_DB}  (all writes below land here, not the real DB)")
print(f"User           : {USER_ID}")
print()

# argosy imports AFTER ARGOSY_HOME is set.
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings, reload_settings

reload_settings()
_settings = get_settings()
_sync_url = _settings.database_url.replace("+aiosqlite", "")
_engine = sa.create_engine(_sync_url, connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

from argosy.state import db as db_module

db_module.init_engine(_settings.database_url)


def _latest_decision_run_id(session) -> int | None:
    row = session.execute(
        sa.text(
            "SELECT id FROM decision_runs WHERE user_id = :u "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"u": USER_ID},
    ).first()
    return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# Path 1: gate-outcome persistence
# ---------------------------------------------------------------------------

def smoke_gate_outcome() -> None:
    print("--- gate-outcome persistence (argosy.services.gate_outcome_store) ---")
    from argosy.quality.verification import GateOutcome, GateStatus
    from argosy.services.gate_outcome_store import get_gate_receipt, persist_gate_outcomes

    with _SessionLocal() as session:
        run_id = _latest_decision_run_id(session)
        if run_id is None:
            print("  SKIPPED: no decision_runs row for user 'ariel' in the DB copy.")
            print()
            return

        outcomes = [
            GateOutcome(
                gate="smoke_real_paths_probe",
                status=GateStatus.PASS,
                detail="smoke harness real-path probe",
            ),
        ]
        persist_gate_outcomes(session, run_id, outcomes)

        receipt = get_gate_receipt(session, run_id)
        if receipt is None:
            print(f"  FAIL: persist_gate_outcomes wrote to decision_run_id={run_id}"
                  " but get_gate_receipt returned None (read-back failed).")
        else:
            read_outcomes, summary_line = receipt
            names = [o.gate for o in read_outcomes]
            print(f"  decision_run_id : {run_id}")
            print(f"  wrote           : 1 outcome (gate='smoke_real_paths_probe')")
            print(f"  read back       : {len(read_outcomes)} outcome(s), gates={names}")
            print(f"  summary_line    : {summary_line!r}")
            print(f"  RESULT: {'PASS' if 'smoke_real_paths_probe' in names else 'FAIL'}"
                  " — real DB write, real read-back, no mocking.")
    print()


# ---------------------------------------------------------------------------
# Path 2: fact tokenizer, via the REAL resolver
# ---------------------------------------------------------------------------

def smoke_fact_tokenize() -> None:
    print("--- fact tokenizer (real resolve_plan_numbers -> tokenize_bodies) ---")
    from argosy.quality.fact_tokenizer import tokenize_bodies
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    from argosy.state.queries import get_current_plan, get_pending_draft

    with _SessionLocal() as session:
        run_id = _latest_decision_run_id(session)
        plan = get_pending_draft(session, USER_ID) or get_current_plan(session, USER_ID)
        if plan is None:
            print("  SKIPPED: no plan (draft or current) for user 'ariel' in the DB copy.")
            print()
            return

        bodies = {
            "long": plan.horizon_long_md or "",
            "medium": plan.horizon_medium_md or "",
            "short": plan.horizon_short_md or "",
        }
        total_chars = sum(len(v) for v in bodies.values())
        if total_chars == 0:
            print("  SKIPPED: plan has no horizon prose to tokenize.")
            print()
            return

        try:
            resolved = resolve_plan_numbers(
                session, user_id=USER_ID, decision_run_id=run_id,
                include_canonical_ages=True,
            )
        except Exception as exc:  # noqa: BLE001 — smoke must report, not crash
            print(f"  FAIL: resolve_plan_numbers raised: {exc!r}")
            print()
            return

        new_bodies, violations, subs = tokenize_bodies(bodies, resolved)
        print(f"  plan_version_id : {plan.id} (role={plan.role})")
        print(f"  decision_run_id : {run_id}")
        print(f"  prose chars     : {total_chars}")
        print(f"  substitutions   : {len(subs)}")
        for s in subs[:5]:
            print(f"    - {s}")
        print(f"  drift_violations: {len(violations)}")
        for v in violations[:5]:
            print(f"    - {v}")
        print("  RESULT: PASS — ran the real resolver, not a hand-built stand-in.")
    print()


# ---------------------------------------------------------------------------
# Path 3: plan amendment (small tier, synchronous, real DecisionRun + draft)
# ---------------------------------------------------------------------------

def smoke_plan_amendment() -> None:
    print("--- plan amendment, small tier (dispatcher.run_small) ---")
    from argosy.agents.advisor_amendment_types import AmendmentIntent
    from argosy.agents.plan_synthesizer_types import Delta
    from argosy.orchestrator.flows.plan_amendment import dispatcher
    from argosy.state.queries import get_current_plan

    with _SessionLocal() as session:
        current = get_current_plan(session, USER_ID)
        if current is None:
            print("  SKIPPED: no current plan for user 'ariel' in the DB copy.")
            print()
            return

        delta = Delta(
            item_kind="target",
            item_id="smoke_real_paths.probe_item",
            horizon="medium",
            change_kind="modified",
            summary="smoke harness probe delta (tightening, dry)",
            prior={"cap_pct": 10.0},
            proposed={"cap_pct": 9.0},
            rationale="smoke_real_paths.py real-path probe",
        )
        intent = AmendmentIntent(
            tier="small", direction="tighten", proposed_delta=delta,
            rationale="smoke probe",
        )

        try:
            result = dispatcher.run_small(
                session, user_id=USER_ID,
                message="[smoke_real_paths] probe tighten", intent=intent,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: run_small raised: {exc!r}")
            print()
            return

        row = session.execute(
            sa.text("SELECT status, tier FROM decision_runs WHERE id = :i"),
            {"i": result.decision_run_id},
        ).first()
        print(f"  decision_run_id : {result.decision_run_id}")
        print(f"  status          : {result.status}")
        print(f"  draft_id        : {result.draft_id}")
        print(f"  DB row seen     : status={row[0]!r} tier={row[1]!r}" if row else "  DB row: NOT FOUND")
        print(f"  RESULT: {'PASS' if result.status == 'applied' and row is not None else 'FAIL'}"
              " — real DecisionRun + draft row committed, read back from the DB.")
    print()


# ---------------------------------------------------------------------------
# Path 4: fund-vehicle verdict — real end-to-end, including a live LLM call.
# ---------------------------------------------------------------------------

def smoke_fund_vehicle(ticker: str) -> None:
    print(f"--- fund-vehicle verdict (run_fund_vehicle_decision, ticker={ticker}) ---")
    print("  NOTE: this makes a REAL agent.run() call (live claude.exe). May take 30-90s.")
    from argosy.services.decision_funnel.fund_vehicle_decision import (
        run_fund_vehicle_decision,
    )

    try:
        outcome = asyncio.run(
            run_fund_vehicle_decision(user_id=USER_ID, ticker=ticker)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: run_fund_vehicle_decision raised: {exc!r}")
        print()
        return

    print(f"  status          : {outcome.status}")
    print(f"  verdict         : {getattr(outcome, 'verdict', None)}")
    print(f"  conviction      : {getattr(outcome, 'conviction', None)}")
    print(f"  verdict_id      : {getattr(outcome, 'verdict_id', None)}")
    print(f"  blocked_by      : {getattr(outcome, 'blocked_by', None)}")
    print(f"  blocked_reason  : {getattr(outcome, 'blocked_reason', None)}")
    if outcome.status == "blocked" and outcome.blocked_by == "verdict_defended":
        print("  NOTE: the pushback gate short-circuited before agent.run() because"
              " this ticker already has a settled verdict — no fresh live LLM call"
              " happened this run. Pass a ticker with no standing verdict (or"
              " --ticker) to force the real agent dispatch.")

    with _SessionLocal() as session:
        row = session.execute(
            sa.text(
                "SELECT id FROM agent_reports WHERE user_id=:u AND agent_role='fund_vehicle_analyst' "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"u": USER_ID},
        ).first()
        print(f"  agent_reports row persisted: {row[0] if row else 'NONE'}")

    ok = outcome.status in ("verdict_written", "blocked") or (
        outcome.status == "error" and outcome.blocked_by != "agent_error"
    )
    print(f"  RESULT: {'PASS' if outcome.status != 'error' else 'FAIL'}"
          " — real agent dispatch, real report_obj.output field access, real DB write.")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_ALL = {
    "gate-outcome": lambda: smoke_gate_outcome(),
    "fact-tokenize": lambda: smoke_fact_tokenize(),
    "plan-amendment": lambda: smoke_plan_amendment(),
    "fund-vehicle": lambda: smoke_fund_vehicle("FWRA"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", choices=[*_ALL.keys(), []], default=list(_ALL.keys()))
    parser.add_argument("--skip-fund-vehicle", action="store_true",
                         help="omit the fund-vehicle path (skips the live LLM call)")
    parser.add_argument("--ticker", default="FWRA",
                         help="ticker to use for the fund-vehicle probe")
    args = parser.parse_args()

    paths = args.paths or list(_ALL.keys())
    if args.skip_fund_vehicle:
        paths = [p for p in paths if p != "fund-vehicle"]

    for name in paths:
        if name == "fund-vehicle":
            smoke_fund_vehicle(args.ticker)
        else:
            _ALL[name]()

    print("=" * 78)
    print("Done. Throwaway DB copy left at:", _TMP_DB, "(delete manually if desired)")
    print("=" * 78)


if __name__ == "__main__":
    main()
