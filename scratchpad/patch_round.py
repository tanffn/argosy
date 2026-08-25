"""Bounded PATCH round on plan 122 — classify first, launch only on PATCH.

Six corrective rounds re-authored the whole plan because the auto-built
corrective context is (a) every open finding and (b) critique-sourced, so it
carries no wrong_values. An unaddressable correction forces FULL outright, and
a dozen findings blow past MAX_IMPLICATED_GROUPS (2 of 4 slice groups) anyway.

This hands in a BOUNDED set whose wrong values were each verified to occur in
plan 122's rendered surfaces, classifies it, and refuses to launch unless the
verdict is PATCH.

Excluded on purpose: the stray-cap correction — "13.0" survives in plan
122's persisted target_allocation_json (a render-only global surface),
which forces FULL. It needs no patch: the constant is already 0.12, so the
next run builds a doc without it. And the FIRE-bridge correction. Its literals span long +
medium + sections (3 of 4 groups), so the classifier correctly declines to
treat it as a local edit. It gets its own round once its tokens render
(retirement.fire_bridge_fi_age_estimate_nis was registered in 4af3f59).
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

OUT = ROOT / "scratchpad" / "patch_round.txt"
USER_ID = "ariel"
ANCHOR_PLAN_ID = 123

STANDING = """STANDING RULES — never weakened by any correction:
Two standing glide rules; lowest grant benchmark first; queue each vest to its
trustee-confirmed Section-102 eligibility date; net proceeds ~67.6% of gross;
SGOV is SOLD into IB01 and is never a parking place; SCHD is HELD and wound
down GRADUALLY in annual slices with purchases and DRIP stopped now, and never
swapped into FUSA or DHSA; SGLN remains a standalone gold line; USD 125,800
deploys as a LUMP SUM at the next LSE open into CSPX 74,700 / EXUS 37,500 /
EIMI 13,600.

NEVER REAPPEAR: 3,924 / 5,493 / 9,417 / 1,523, ILS 1,849,929, ILS 1,274,268,
ILS 209,389, ILS 9,825,302, 59.9%, "park in SGOV".

Every figure must come from a {{fact:}} token. If a value has no token, state
it as pending rather than typing a digit.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_amendment.workers import _large_worker
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _load_patch_base_output,
    )
    from argosy.quality.patch_reachability import classify_patch_reachability
    from argosy.services.corrective_context import (
        Correction,
        CorrectiveContext,
    )
    from argosy.state.models import DecisionRun, PlanVersion

    pv = session.get(PlanVersion, ANCHOR_PLAN_ID)
    if pv is None or pv.user_id != USER_ID:
        raise RuntimeError(f"anchor {ANCHOR_PLAN_ID} missing/cross-tenant")
    if pv.role == "current":
        raise RuntimeError("refusing to anchor on role='current'")
    prior = _load_patch_base_output(pv)
    if prior is None:
        raise RuntimeError("anchor has no structured artifact")

    def C(i, topic, canonical, wrong, statement):
        return Correction(
            index=i, severity="BLOCKER", topic=topic,
            plan_item_ref="",            # empty: an UNRESOLVABLE ref forces
            summary=statement,           # FULL; the no-ref form falls through
            evidence=[],                 # to occurrence matching instead
            reconcile_status="unresolved",
            canonical_facts=canonical, wrong_values=wrong,
            required_statement=statement,
            source="verdict_feedback", verdict_agent="codex",
            source_run_id=456, source_draft_id=ANCHOR_PLAN_ID,
        )

    corrections = [
        C(1, "[C2] Two incompatible NVDA sale programmes in one document",
          [("concentration.nvda_sell_sh", "8,918"),
           ("concentration.nvda_target_sh", "1,462")],
          ["3,924", "5,493", "9,417", "1,523"],
          "ONE sale programme only: sell {{fact:concentration.nvda_sell_sh}} "
          "and retain {{fact:concentration.nvda_target_sh}}, both from their "
          "tokens. The 3,924 / 5,493 / 9,417 quota programme and its 1,523 "
          "endpoint are SUPERSEDED and must not appear anywhere."),
    ]

    ctx = CorrectiveContext(
        corrections=corrections, directives=[],
        base_plan_id=ANCHOR_PLAN_ID,
        base_plan_label=pv.version_label or "",
        forces_full_tier=False,
    )
    from argosy.services.corrective_context import _render_block
    ctx.rendered = _render_block(ctx)

    rendered_surfaces = {
        "long": pv.horizon_long_md or "",
        "medium": pv.horizon_medium_md or "",
        "short": pv.horizon_short_md or "",
    }

    reach = classify_patch_reachability(
        corrections=[c.to_payload() for c in corrections],
        directives=[], prior=prior,
        rendered_surfaces=rendered_surfaces,
        global_surfaces={
            "target_allocation_json": pv.target_allocation_json or "",
        },
    )
    lines += [
        f"classifier verdict: {reach.verdict}",
        f"reason: {reach.reason}",
    ]
    for d in reach.decisions:
        lines.append(
            f"   [{d.index}] {d.scope:5} groups={list(d.implicated_groups)}"
        )
    OUT.write_text("\n".join(lines), encoding="utf-8")

    if reach.verdict != "PATCH":
        lines.append("REFUSING TO LAUNCH — not a bounded patch")
        raise SystemExit(0)

    run = DecisionRun(
        user_id=USER_ID, ticker="PLAN", tier="large",
        decision_kind="plan_amendment", status="running",
    )
    session.add(run)
    session.commit()
    lines.append(f"launching PATCH round as run {run.id} on plan {ANCHOR_PLAN_ID}")
    OUT.write_text("\n".join(lines), encoding="utf-8")

    _large_worker(
        session=session, user_id=USER_ID, decision_run=run,
        guidance=STANDING, anchor_plan_version_id=ANCHOR_PLAN_ID,
        corrective_ctx_override=ctx,
    )

    session.expire_all()
    fresh = session.get(DecisionRun, run.id)
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
    lines += [
        f"run status: {getattr(fresh, 'status', None)!r}",
        f"fm: {getattr(fresh, 'fund_manager_decision', None)!r}",
        f"newest plan: {newest[0] if newest else None} "
        f"({newest[1] if newest else None})",
        f"role='current' stays: plan {current}",
        ("SUCCEEDED — new draft written"
         if (newest and newest[0] > ANCHOR_PLAN_ID) else "NO NEW DRAFT"),
    ]
except SystemExit:
    pass
except Exception:
    lines += ["FAILED", traceback.format_exc()[:4000]]
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
