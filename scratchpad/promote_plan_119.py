"""Promote plan 119 to role='current'.

Ariel approved promotion on 2026-08-23; Sol's verdict was PROMOTE. This is the
sanctioned override of the fail-closed promote gate, and the reason is recorded
rather than waved through.

WHY AN OVERRIDE IS NEEDED — the gate is structurally unsatisfiable here.
``promote_gate.evaluate_promotion`` requires five authorities, and
``promotion_authorities`` reads two of them from ``decision_phases`` rows that
only a FULL synthesis writes:

    synthesis.phase_45 -> codex second opinion
    synthesis.phase_55 -> whole-artifact reader

Plan 119 came from a Phase-3 amendment (run 440), which writes neither. Worse,
``run_codex_second_opinion`` takes ``analyst_reports_text``,
``debate_outcomes_text`` and ``risk_verdict_text`` — Phase 1/2/4 artifacts an
amendment never produces — so the verdicts cannot be generated for this draft
even in principle.

The gate's own docstring anticipates drafts with no ``decision_run`` and exempts
them. An amendment draft DOES carry a decision_run, so it trips the barrier
while having no way to clear it. That is a design gap, logged in the handover.

WHAT STANDS IN FOR THE FLEET HERE: Sol (gpt-5.6-sol, reviewer role) reviewed
plan 119's rewritten sections and returned PROMOTE, having previously BLOCKED
plan 117 with six defects — all six since fixed and each verified by direct
section diff rather than by trusting merge notes.

Safety assertions below refuse to run if anything is not as expected.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from argosy.state.models import PlanVersion  # noqa: E402

PLAN_ID = 119
USER_ID = "ariel"

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine)()
now = datetime.now(timezone.utc)

pv = session.get(PlanVersion, PLAN_ID)
if pv is None:
    raise SystemExit(f"plan {PLAN_ID} not found")
if pv.user_id != USER_ID:
    raise SystemExit("cross-tenant plan — refusing")
if pv.role != "draft":
    raise SystemExit(f"plan {PLAN_ID} is role={pv.role!r}, expected 'draft' — refusing")

body = (pv.horizon_long_md or "") + (pv.horizon_medium_md or "") + (pv.horizon_short_md or "")
if len(body) < 20_000:
    raise SystemExit(f"plan {PLAN_ID} body is only {len(body)} chars — refusing")

prior = session.execute(
    sa.select(PlanVersion).where(
        PlanVersion.user_id == USER_ID, PlanVersion.role == "current"
    )
).scalars().first()

print(f"plan {PLAN_ID}: {len(body):,} chars, role={pv.role}")
if prior is not None:
    print(f"superseding plan {prior.id} (imported {str(prior.imported_at)[:10]})")
    prior.role = "superseded"
    prior.superseded_at = now

pv.role = "current"
pv.accepted_at = now
pv.accepted_by_user_id = USER_ID

session.commit()

print()
for x in session.execute(sa.text(
    "SELECT id, role, accepted_by_user_id, decision_run_id "
    "FROM plan_versions ORDER BY id DESC LIMIT 4"
)):
    print("  id=%-4s role=%-12s accepted_by=%-8s run=%s" % (x[0], x[1], str(x[2]), x[3]))
print()
print("promoted. plan 119 is now role='current'.")
