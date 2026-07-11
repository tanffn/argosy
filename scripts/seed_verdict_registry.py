"""Seed the settled-verdict registry from known deep-decision runs.

Item B acceptance seeds (handover §5d/§5e):
  ORCL  run 198 — HOLD/wait, revisit at $110-115 or FCF+/debt-stable
  SOFI  run 186 — HOLD
  BMY   run 187 — HOLD
  OPEN  run 188 — HOLD
  VOR   proposal 16 post-expiry — HOLD (LOW), re-eval later
  OKLO / RKLB / ASTS — HOLD (discovery verdicts, runs ≥199)

Idempotent: re-running refreshes the same source_decision_run_id rows.
Does NOT call live LLMs. Usage:

  .venv/Scripts/python.exe scripts/seed_verdict_registry.py
  .venv/Scripts/python.exe scripts/seed_verdict_registry.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# Ensure project root on path when run as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy.orm import sessionmaker

from argosy.services.verdict_registry import write_verdict
from argosy.state import db as db_mod
from argosy.state.models import User

USER_ID = "ariel"

# Reference shapes from handover §5d/§5e — prose triggers typed for the checker.
SEEDS: list[dict] = [
    {
        "subject": "ORCL",
        "verdict": "WAIT",
        "conviction": "HIGH",
        "source_decision_run_id": 198,
        "falsifiers": [
            "FCF turns sustainably positive with debt stable",
            "OpenAI backlog concentration materially eases",
        ],
        "revisit_triggers": [
            {"kind": "price_below", "price": 115.0},
            {"kind": "price_below", "price": 110.0},
            {
                "kind": "metric_condition",
                "metric": "fcf_ttm",
                "op": ">",
                "value": 0,
                "label": "FCF positive with debt stable",
            },
        ],
        "next_validation": date(2026, 10, 1),
        "reasoning_md": (
            "ORCL HOLD/wait (run 198): fair value ~$150 vs ~$140.6 (7% buffer, "
            "leverage-discounted); TTM FCF ≈ −$24.5B; D/E ~3.0; OpenAI backlog "
            "concentration. Revisit at price ~$110–115 or FCF+/debt-stable. DEFENDED."
        ),
    },
    {
        "subject": "SOFI",
        "verdict": "HOLD",
        "conviction": "MED",
        "source_decision_run_id": 186,
        "falsifiers": ["thesis-breaking credit deterioration"],
        "revisit_triggers": [],
        "reasoning_md": "SOFI HOLD (run 186) — settled deep-decision.",
    },
    {
        "subject": "BMY",
        "verdict": "HOLD",
        "conviction": "MED",
        "source_decision_run_id": 187,
        "falsifiers": ["pipeline setback that invalidates the hold thesis"],
        "revisit_triggers": [],
        "reasoning_md": "BMY HOLD (run 187) — settled deep-decision.",
    },
    {
        "subject": "OPEN",
        "verdict": "HOLD",
        "conviction": "MED",
        "source_decision_run_id": 188,
        "falsifiers": ["balance-sheet stress forcing dilution"],
        "revisit_triggers": [],
        "reasoning_md": "OPEN HOLD (run 188) — settled deep-decision.",
    },
    {
        "subject": "VOR",
        "verdict": "HOLD",
        "conviction": "LOW",
        "source_decision_run_id": None,  # proposal 16 expired; no run id required
        "falsifiers": ["readout that clears the killing falsifier"],
        "revisit_triggers": [
            {"kind": "dated_event", "date": "2026-10-01", "label": "VOR re-eval window"},
        ],
        "reasoning_md": (
            "VOR HOLD (proposal 16 post-expiry 2026-07-13): expiry ≠ verdict; "
            "re-eval later if falsifiers stay quiet. DEFENDED."
        ),
    },
    {
        "subject": "OKLO",
        "verdict": "HOLD",
        "conviction": "MED",
        "source_decision_run_id": 199,
        "falsifiers": ["first criticality failure"],
        "revisit_triggers": [
            {
                "kind": "dated_event",
                "date": "2026-07-31",
                "label": "July-2026 first criticality",
            },
        ],
        "reasoning_md": (
            "OKLO HOLD (discovery, run ≥199): clock is July-2026 first "
            "criticality. Monitor-only per Ariel. DEFENDED."
        ),
    },
    {
        "subject": "RKLB",
        "verdict": "HOLD",
        "conviction": "MED",
        "source_decision_run_id": 200,
        "falsifiers": ["make-or-break launch failure"],
        "revisit_triggers": [],
        "reasoning_md": (
            "RKLB HOLD (discovery): two make-or-break events outstanding. "
            "Monitor-only. DEFENDED."
        ),
    },
    {
        "subject": "ASTS",
        "verdict": "HOLD",
        "conviction": "MED",
        "source_decision_run_id": 201,
        "falsifiers": ["next launch / data-integrity failure"],
        "revisit_triggers": [],
        "reasoning_md": (
            "ASTS HOLD (discovery): next launch/data-integrity checkpoint. "
            "Monitor-only. DEFENDED."
        ),
    },
]


def _resolve_run_id(sess, run_id: int | None) -> int | None:
    """Drop source_decision_run_id when the DecisionRun row is not present yet.

    Discovery verdicts (OKLO/RKLB/ASTS) may seed before their runs land;
    FK would otherwise reject the insert.
    """
    if run_id is None:
        return None
    from argosy.state.models import DecisionRun

    if sess.get(DecisionRun, run_id) is None:
        print(
            f"WARN: decision_runs.id={run_id} missing — seeding without run FK",
            file=sys.stderr,
        )
        return None
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-id", default=USER_ID)
    args = parser.parse_args()

    import sqlalchemy as sa

    url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        if sess.get(User, args.user_id) is None:
            print(f"ERROR: user {args.user_id!r} not found", file=sys.stderr)
            return 2
        written = []
        for seed in SEEDS:
            run_id = _resolve_run_id(sess, seed.get("source_decision_run_id"))
            if args.dry_run:
                print(
                    f"DRY-RUN would write {seed['subject']} {seed['verdict']} "
                    f"run_id={run_id}"
                )
                continue
            row = write_verdict(
                sess,
                user_id=args.user_id,
                subject=seed["subject"],
                verdict=seed["verdict"],
                conviction=seed["conviction"],
                falsifiers=seed.get("falsifiers"),
                revisit_triggers=seed.get("revisit_triggers"),
                next_validation=seed.get("next_validation"),
                source_decision_run_id=run_id,
                reasoning_md=seed.get("reasoning_md") or "",
                settled=True,
            )
            written.append({
                "id": row.id,
                "subject": row.subject,
                "verdict": row.verdict,
                "source_decision_run_id": row.source_decision_run_id,
            })
        if not args.dry_run:
            sess.commit()
        print(json.dumps({"ok": True, "dry_run": args.dry_run, "written": written}, indent=2))
        return 0
    finally:
        sess.close()


if __name__ == "__main__":
    raise SystemExit(main())
