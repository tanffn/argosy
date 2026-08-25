"""Resume run 456 from phase 3 — reuse the skeleton and five good slices.

Run 456 died at 07:57:50 on 2026-08-24 after the monolith fallback burned
four 900s SDK timeouts. The fallback was never the real failure: the
``sections_long`` slice returned successfully while omitting all 11 of its
roster entries, assembly refused it, and the sliced path degraded to the
monolith (``plan_synthesis.sliced_degraded_to_monolith``, 06:57:24).

Two commits make this resume worth doing:

* ``dc384b4`` validates roster coverage INSIDE the slice retry envelope,
  and discards a resumed checkpoint that fails the same check — so the
  bad ``sections_long`` row is re-run instead of replayed forever.
* ``76fbad3`` gives ``_large_worker`` a ``resume_from_phase`` passthrough,
  so this goes through the worker (audit chain + amendment events +
  cancellation re-check) rather than a bare ``run_synthesis``.

Expected shape: skeleton and five slices resume from checkpoint, only
``sections_long`` re-runs, assembly proceeds to risk / codex / FM /
reader. Nothing here promotes anything — plan 92 stays ``role='current'``
and the promote gate remains the only path to a new current.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

OUT = ROOT / "scratchpad" / "resume_456.txt"
USER_ID = "ariel"
RUN_ID = 456
RESUME_FROM_PHASE = 3
ANCHOR_PLAN_ID = 121

GUIDANCE = """\
Clear the codex BLOCKERS and change nothing else. The Section-102 content, the
SGOV disposal, the SCHD position and the cash plan are settled.

[B1] The cap is published as both 12% and 13%. The resolver now returns 13% on
every path from the settled TargetAllocationDoc. DELETE every hard-coded cap
digit. The cap is ONLY {{fact:concentration.nvda_cap_pct}}; the steering target
is ONLY {{fact:concentration.nvda_target_pct}}.

[B2] The NVDA weight is wrong. The draft repeats 59.9%; the canonical value is
{{fact:concentration.nvda_current_pct}} = 56.8%. Delete every 59.9% literal.

[B3] The FIRE bridge contradicts itself - described as both DISTINCT from and
FUNDED BY the ILS 1.45M finite-liability reserve. Say it once. Always publish
both {{fact:retirement.fire_bridge_nis}} and
{{fact:retirement.fire_bridge_offmandate_nis}}, never one alone.

[B4] The NVDA order is asserted without its size while the draft admits holdings
are provisional and the lot CSV is missing. Write it as: refresh the trustee lot
ledger FIRST, then size from {{fact:concentration.nvda_sell_sh}}, with the
refresh an explicit precondition. Never a dated order with an unresolved size.

[B5] Explain the lot-ordering mechanism once: tax per share is 0.30(S-B) +
0.50B, so a LOWER benchmark B means LESS total tax despite MORE conventional
capital gain - the ordinary slice is taxed at 50%, the capital slice at 30%.

Every figure must be a {{fact:}} token. If a value has no token, state it as
pending rather than typing a number.

SECTION COVERAGE - non-negotiable. Every section named in the skeleton roster
must be expanded. Emitting a subset is a failure, not a shortening: the long
horizon owns client_goals, net_worth, capital_sufficiency, concentration, ips,
estate, monte_carlo, withdrawal, insurance, healthcare and life_events, and all
eleven must appear. If a section has nothing new to say, write the one-line
standing position - never omit the entry.

STANDING - do not weaken: two standing glide rules; lowest grant benchmark
first; queue each vest to its trustee-confirmed Section-102 eligibility date;
net proceeds ~67.6% of gross; SGOV is SOLD into IB01 and is never a parking
place; SCHD is HELD and wound down GRADUALLY in annual slices with purchases and
DRIP stopped now, with no FUSA/DHSA swap; USD 125,800 deploys as a LUMP SUM at
the next LSE open into CSPX 74,700 / EXUS 37,500 / EIMI 13,600.
Never reappear: 3,924 / 5,493 / 9,417, ILS 1,849,929, ILS 1,274,268,
ILS 209,389, ILS 9,825,302, 59.9%, a 12% cap.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_amendment.workers import _large_worker
    from argosy.state.models import DecisionRun, PlanVersion

    run = session.get(DecisionRun, RUN_ID)
    if run is None or run.user_id != USER_ID:
        raise RuntimeError(f"run {RUN_ID} missing or cross-tenant")

    anchor = session.get(PlanVersion, ANCHOR_PLAN_ID)
    if anchor is None or anchor.user_id != USER_ID:
        raise RuntimeError(f"anchor plan {ANCHOR_PLAN_ID} missing/cross-tenant")
    if anchor.role == "current":
        raise RuntimeError("refusing to anchor on role='current'")

    phases = [
        r[0] for r in session.execute(sa.text(
            "SELECT kind FROM decision_phases WHERE decision_run_id=:i "
            "ORDER BY id"
        ), {"i": RUN_ID})
    ]
    slices = [p for p in phases if p.startswith("synthesis.phase_3.slice.")]
    if "synthesis.phase_3.skeleton" not in phases:
        raise RuntimeError("no skeleton checkpoint — nothing to resume")
    lines += [
        f"resuming run {RUN_ID} from phase {RESUME_FROM_PHASE}",
        f"anchor: plan {ANCHOR_PLAN_ID} (role={anchor.role})",
        f"checkpoints: skeleton + {len(slices)} slices",
        "expect: sections_long DISCARDED as incomplete and re-run (dc384b4)",
    ]

    # Reopen the row so the flow does not refuse a terminal status.
    run.status = "running"
    run.finished_at = None
    session.commit()
    OUT.write_text("\n".join(lines), encoding="utf-8")

    _large_worker(
        session=session, user_id=USER_ID, decision_run=run,
        guidance=GUIDANCE,
        anchor_plan_version_id=ANCHOR_PLAN_ID,
        resume_from_phase=RESUME_FROM_PHASE,
    )

    # `_large_worker` swallows exceptions into status='failed', so never
    # trust "it returned" as success — re-read the row and look for a draft.
    session.expire_all()
    fresh = session.get(DecisionRun, RUN_ID)
    newest = session.execute(
        sa.select(PlanVersion.id, PlanVersion.role)
        .where(PlanVersion.user_id == USER_ID)
        .order_by(PlanVersion.id.desc())
    ).first()
    current = session.execute(
        sa.select(PlanVersion.id).where(
            PlanVersion.user_id == USER_ID, PlanVersion.role == "current"
        )
    ).scalar()
    wrote = bool(newest and newest[0] > ANCHOR_PLAN_ID)
    lines += [
        f"run status: {getattr(fresh, 'status', None)!r}",
        f"newest plan: {newest[0] if newest else None} "
        f"({newest[1] if newest else None})",
        f"role='current' stays: plan {current}",
        "SUCCEEDED — new draft written" if wrote else "NO NEW DRAFT",
    ]
except Exception:
    lines += ["FAILED", traceback.format_exc()[:5000]]
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
