"""Full synthesis ANCHORED ON PLAN 119, via _large_worker.

This is the corrected version of the run Ariel caught. Three things are
different from the aborted run 447:

1. ANCHOR. `anchor_plan_version_id=119` means the synthesizer starts from the
   corrected draft instead of `role='current'` — which is plan 92 of 13 July,
   so run 447 was rebuilding from July and discarding drafts 93..119. Plan 92
   REMAINS current; nothing is promoted.
2. ENTRY POINT. Goes through `_large_worker`, not a bare `run_synthesis` call,
   so the amendment DecisionRun is reused and chat-turn -> amendment -> draft
   stays one audit chain.
3. GATES. Being a full run, it writes `synthesis.phase_45` (codex) and
   `synthesis.phase_55` (reader), so the resulting draft can actually clear
   promote_gate — which no medium amendment can.

Live quotes are NOT stubbed: this is the authoritative run and its numbers
should be freshly priced. The per-finding repricing that made earlier runs take
hours was fixed in 2d760db.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

OUT = ROOT / "scratchpad" / "run_anchored_synthesis.txt"
USER_ID = "ariel"
ANCHOR_PLAN_ID = 121

GUIDANCE = """Anchor on the current draft and PRESERVE its substance. This round clears
FOUR codex BLOCKERS and nothing else. Do not re-derive the Section-102 content,
the SGOV disposal, the SCHD hold or the cash plan - they are correct.

[B1] THE CAP IS PUBLISHED AS BOTH 12% AND 13%. The resolver now returns 13% on
every path from the settled TargetAllocationDoc. A 12% literal still survives in
long.targets. DELETE every hard-coded cap digit. The cap is ONLY ever
{{fact:concentration.nvda_cap_pct}} and the steering target is ONLY ever
{{fact:concentration.nvda_target_pct}}. If you cannot express it as a token, do
not state it.

[B2] THE NVDA WEIGHT IS WRONG. The draft repeats 59.9%. The canonical value is
{{fact:concentration.nvda_current_pct}} = 56.8%, reproducible as raw NVDA
2,228,793.61 over a tradeable book of 3,925,157.61 excluding direct real estate
and cash. Delete every 59.9% literal and bind the token.

[B3] THE FIRE BRIDGE CONTRADICTS ITSELF. The medium FIRE Bridge section says
both bridges are liquid drawdowns DISTINCT from the ILS 1.45M finite-liability
reserve, while another surface has them FUNDED BY it. Pick one and say it once.
The two bridge figures are {{fact:retirement.fire_bridge_nis}} (mandate case)
and {{fact:retirement.fire_bridge_offmandate_nis}} (off-mandate) - always both,
never one alone.

[B4] THE NVDA ORDER IS ASSERTED WITHOUT ITS SIZE. The short horizon admits
holdings are provisional, the Schwab lot CSV is missing, and there is no fixed
order size until a GO/NO-GO refresh. Do NOT issue a dated sale with an
unresolved quantity. Write the action as: refresh the trustee lot ledger FIRST,
then size the tranche from {{fact:concentration.nvda_sell_sh}}, with the refresh
as an explicit precondition. State the sizing input as pending if it is pending
- never assert an exact program on inputs you admit are unresolved.

[B5 - AMBER, fix if cheap] The lot-ordering rationale reads as economically
inconsistent. Explain the mechanism once: tax per share is 0.30(S-B) + 0.50B, so
a LOWER grant benchmark B means LESS total tax even though it means MORE
conventional capital gain. Both statements are true because the ordinary slice
is taxed at 50% and the capital slice at 30%.

EVERY figure must be a {{fact:}} token. Unbound headline digits are what caused
these blockers. If a value has no token, state it as pending rather than typing
a number.

STANDING CONSTRAINTS - unchanged, do not weaken:
Two standing rules for the NVDA glide; sell lowest grant benchmark first; queue
each vest to its trustee-confirmed Section-102 eligibility date; net proceeds
~67.6% of gross; SGOV is SOLD into IBTA and is never a parking place; SCHD is
HELD with no FUSA/DHSA swap; USD 125,800 deploys as a LUMP SUM on the next LSE
session into CSPX 74,700 / EXUS 37,500 / EIMI 13,600. Household income
components must sum: {{fact:income.primary_net_annual_nis}} +
{{fact:income.secondary_net_annual_nis}} +
{{fact:income.other_recurring_annual_nis}} =
{{fact:income.household_net_annual_nis}}. Stale literals that must never
reappear: 3,924 / 5,493 / 9,417, ILS 1,849,929, ILS 1,274,268, ILS 209,389,
ILS 9,825,302, 59.9%, a 12% cap.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_amendment.workers import _large_worker
    from argosy.state.models import DecisionRun, PlanVersion

    anchor = session.get(PlanVersion, ANCHOR_PLAN_ID)
    if anchor is None or anchor.user_id != USER_ID:
        raise RuntimeError(f"anchor plan {ANCHOR_PLAN_ID} missing or cross-tenant")
    cur = session.execute(
        sa.select(PlanVersion).where(
            PlanVersion.user_id == USER_ID, PlanVersion.role == "current")
    ).scalars().first()
    lines.append(f"anchor: plan {anchor.id} (role={anchor.role})")
    lines.append(f"current stays: plan {cur.id} (role={cur.role})" if cur else "no current")

    run = DecisionRun(
        user_id=USER_ID, ticker="(plan)", tier="large",
        decision_kind="plan_amendment_chat", status="running",
        started_at=datetime.now(timezone.utc),
        notes_json=json.dumps({
            "message": "corrected two-rule NVDA glide — full run for verdicts",
            "anchor_plan_version_id": ANCHOR_PLAN_ID,
        }),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    lines.append(f"decision_run={run.id}")
    OUT.write_text("\n".join(lines), encoding="utf-8")

    _large_worker(
        session=session, user_id=USER_ID, decision_run=run,
        guidance=GUIDANCE, anchor_plan_version_id=ANCHOR_PLAN_ID,
    )

    # _large_worker CATCHES its own exceptions and stamps the DecisionRun,
    # so returning normally does NOT mean the run succeeded. Run 452 wrote
    # "COMPLETED" here while the run had actually died on 14 rewriter
    # invariant violations and produced no draft. Report the OUTCOME, not
    # the fact that the call returned.
    session.expire_all()
    fresh = session.get(DecisionRun, run.id)
    status = getattr(fresh, "status", None)
    newest = session.execute(
        sa.select(PlanVersion.id, PlanVersion.role)
        .where(PlanVersion.user_id == USER_ID)
        .order_by(PlanVersion.id.desc())
    ).first()
    wrote_draft = bool(newest and newest[0] > ANCHOR_PLAN_ID)
    lines.append(f"run status: {status!r}")
    lines.append(f"newest plan: {newest[0] if newest else None} ({newest[1] if newest else None})")
    if status == "completed" and wrote_draft:
        lines.append("SUCCEEDED — new draft written")
    else:
        lines.append(
            "FAILED — status=%r, new draft written=%s. Check the log for "
            "plan_amendment.large.failed / rewriter_structural_violations."
            % (status, wrote_draft)
        )
except Exception:
    lines.append("FAILED")
    lines.append(traceback.format_exc()[:6000])
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
