"""RESUME run 452 from phase 4 — skip the ~40 min of phase-3 slice work.

Why resume rather than relaunch: five background runs have been killed
externally today, the last within seconds of starting. A full run needs ~90
minutes; a resume from phase 4 needs perhaps 30. Completed phases persist in
``decision_phases``, so resuming converts a kill from "lose everything" into
"lose the current phase". Each attempt then makes forward progress.

Run 452 has 8 phases persisted through the consolidated ``synthesis.phase_3``.
It died AFTER that, in the language rewriter, on 14 ``section_id`` invariant
violations — the self-referential join now fixed in
``_force_preserve_structured_fields``. So phase 3's output is sound and only the
post-phase-3 path needs to re-run.

``existing_decision_run_id=452`` keeps the audit lineage on the original row, so
calling ``run_synthesis`` directly here does NOT lose the chain the
``_large_worker`` rule exists to protect — the worker simply has no
``resume_from_phase`` parameter to pass through.
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

OUT = ROOT / "scratchpad" / "resume_synthesis.txt"
USER_ID = "ariel"
RESUME_RUN_ID = 452
RESUME_FROM_PHASE = 4
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

STANDING - do not weaken: two standing glide rules; lowest grant benchmark
first; queue each vest to its trustee-confirmed Section-102 eligibility date;
net proceeds ~67.6% of gross; SGOV is SOLD into IB01 and is never a parking
place; SCHD position unchanged with no FUSA/DHSA swap; USD 125,800 deploys as a
LUMP SUM at the next LSE open into CSPX 74,700 / EXUS 37,500 / EIMI 13,600.
Never reappear: 3,924 / 5,493 / 9,417, ILS 1,849,929, ILS 1,274,268,
ILS 209,389, ILS 9,825,302, 59.9%, a 12% cap.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import run_synthesis
    from argosy.state.models import DecisionRun, PlanVersion

    run = session.get(DecisionRun, RESUME_RUN_ID)
    if run is None or run.user_id != USER_ID:
        raise RuntimeError(f"run {RESUME_RUN_ID} missing or cross-tenant")
    phases = [
        r[0] for r in session.execute(sa.text(
            "SELECT kind FROM decision_phases WHERE decision_run_id=:i ORDER BY id"
        ), {"i": RESUME_RUN_ID})
    ]
    if "synthesis.phase_3" not in phases:
        raise RuntimeError(
            f"run {RESUME_RUN_ID} has no consolidated phase_3 — cannot resume from 4"
        )
    lines.append(f"resuming run {RESUME_RUN_ID} from phase {RESUME_FROM_PHASE}")
    lines.append(f"phases already persisted: {len(phases)}")
    lines.append(f"anchor: plan {ANCHOR_PLAN_ID}")

    # Reopen the row so the flow does not refuse a terminal status.
    run.status = "running"
    run.finished_at = None
    session.commit()
    OUT.write_text("\n".join(lines), encoding="utf-8")

    run_synthesis(
        session, user_id=USER_ID, trigger="check_in", guidance=GUIDANCE,
        existing_decision_run_id=RESUME_RUN_ID,
        resume_from_phase=RESUME_FROM_PHASE,
        anchor_plan_version_id=ANCHOR_PLAN_ID,
    )

    session.expire_all()
    fresh = session.get(DecisionRun, RESUME_RUN_ID)
    newest = session.execute(
        sa.select(PlanVersion.id, PlanVersion.role)
        .where(PlanVersion.user_id == USER_ID)
        .order_by(PlanVersion.id.desc())
    ).first()
    wrote = bool(newest and newest[0] > ANCHOR_PLAN_ID)
    lines.append(f"run status: {getattr(fresh, 'status', None)!r}")
    lines.append(f"newest plan: {newest[0] if newest else None} ({newest[1] if newest else None})")
    lines.append("SUCCEEDED — new draft written" if wrote else "NO NEW DRAFT")
except Exception:
    lines.append("FAILED")
    lines.append(traceback.format_exc()[:5000])
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
