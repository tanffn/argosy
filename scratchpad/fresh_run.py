"""Fresh corrective run on plan 124 — no donor-379 reuse, Sol's contracts loaded.

Sol's finding: runs 456/461/463 all inherited phases 1-2 from run 379 of
2026-08-15, which predates the entire Section-102 correction. That stale donor
is why old weights and tax-universe assumptions keep contaminating otherwise
clean resolver output. `forces_full_tier=True` is the lever that suppresses the
auto-reuse (`_select_corrective_reuse_run` is only consulted when the flag is
False), so phase 1 analysts run fresh.

Corrections are VALUE PAIRS plus Sol's REQUIRED-STATEMENT contracts. Three
rounds proved narrative instructions do not land and value pairs do; the
required_statement field is the mechanism for findings that have no wrong
number to replace (C6 false-certainty, the after-tax framing).
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

OUT = ROOT / "scratchpad" / "fresh_run.txt"
USER_ID = "ariel"
ANCHOR_PLAN_ID = 124

STANDING = """\
STANDING RULES — no correction weakens these: two standing glide rules; lowest
grant benchmark first; queue each vest to its trustee-confirmed Section-102
eligibility date; net proceeds ~67.6% of gross; SGOV is SOLD into IB01 and is
never a parking place; SCHD is HELD and wound down GRADUALLY in annual slices
with purchases and DRIP stopped now, never swapped into FUSA or DHSA; SGLN
remains a standalone gold line; USD 125,800 deploys as a LUMP SUM at the next
LSE open into CSPX 74,700 / EXUS 37,500 / EIMI 13,600.

NEVER REAPPEAR: 3,924 / 5,493 / 9,417 / 1,523, 62.5%, 59.9%, ILS 1,849,929,
ILS 1,274,268, ILS 209,389, ILS 9,825,302, "park in SGOV".

Every figure comes from a {{fact:}} token. If a value has no token, say it is
pending IN PLAIN WORDS — never write the literal string "derivation pending",
which the artifact-integrity gate reads as an unrendered token and blocks on.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_amendment.workers import _large_worker
    from argosy.services.corrective_context import (
        REQUIRED_STATEMENT_CONTRACTS,
        Correction,
        CorrectiveContext,
        _render_block,
    )
    from argosy.state.models import DecisionRun, PlanVersion

    pv = session.get(PlanVersion, ANCHOR_PLAN_ID)
    if pv is None or pv.user_id != USER_ID:
        raise RuntimeError(f"anchor {ANCHOR_PLAN_ID} missing/cross-tenant")
    if pv.role == "current":
        raise RuntimeError("refusing to anchor on role='current'")

    def C(i, topic, canonical, wrong, statement, absent=()):
        return Correction(
            index=i, severity="BLOCKER", topic=topic,
            plan_item_ref="", summary=statement, evidence=list(absent),
            reconcile_status="unresolved",
            canonical_facts=canonical, wrong_values=wrong,
            required_statement=statement,
            source="verdict_feedback", verdict_agent="codex",
            source_run_id=463, source_draft_id=ANCHOR_PLAN_ID,
        )

    corrections = [
        C(1, "[reader] Assumption A13 revives the superseded FI basis",
          [("spend.fi_basis_nis", "300,000")], ["311,584"],
          "The sole binding permanent-equivalent FI spend basis is "
          "{{fact:spend.fi_basis_nis}}. Delete every 311,584 literal, "
          "including assumption A13, and never call it binding."),
        # NOTE: do NOT ban the bare string "12.0". Run 470 did, the skeleton
        # gate could not satisfy it (the anchor's allocation doc legitimately
        # carries 12.0, and 12.0 matches any unrelated 12% figure), the gate
        # failed after retries and the sliced path degraded to the monolith.
        # A wrong_value must be specific enough that removing it is possible.
        C(2, "[reader/gate] Cap published as 12.0 against the settled 13.0",
          [("concentration.nvda_cap_pct", "13.0")],
          ["12.0% cap", "12.0 percent cap", "12% hard cap",
           "cap of 12.0%", "hard ceiling of 12.0"],
          "The governing single-name ceiling is "
          "{{fact:concentration.nvda_cap_pct}} = 13.0%, a SETTLED adjudication "
          "that must not be re-litigated. Render the ceiling through the token "
          "only, never as a typed digit. The glide is sized against the 8% "
          "steering target, not the ceiling."),
        C(3, "[reader] Cash sleeve published at two weights",
          [("allocation.cash_and_t_bills_target_pct", "9.1")], ["10.1"],
          "Publish ONE cash and T-bills weight, from the governing structured "
          "allocation, on every surface including the IPS."),
        C(4, "[reader] RSU tax timing is opposite across surfaces",
          [], [],
          "Section-102 capital track: BOTH the ordinary slice and the capital "
          "slice fall due at SALE, never at vest. The ordinary slice is NOT "
          "settled through payroll at vest. State this identically in the "
          "assumption ledger, the equity-compensation section and every action."),
        C(5, "[reader] The finite-liability reserve is spent twice",
          [], [],
          "The ILS 1,450,000 finite-liability reserve funds education, the "
          "mortgage runoff and weddings. It is NOT also the FIRE bridge. State "
          "the bridge funding once, identically in every horizon, and publish "
          "{{fact:retirement.fire_bridge_nis}} and "
          "{{fact:retirement.fire_bridge_offmandate_nis}} together."),
        C(6, "[gate] Unregistered headline figure",
          [], ["68,403"],
          "Every headline NIS figure must resolve from a {{fact:}} token. "
          "Remove the unregistered 68,403 literal or state it as pending in "
          "plain words."),
    ]

    ctx = CorrectiveContext(
        corrections=corrections, directives=[],
        base_plan_id=ANCHOR_PLAN_ID,
        base_plan_label=pv.version_label or "",
        # Suppresses the donor-379 phase-1/2 reuse Sol identified.
        forces_full_tier=True,
    )
    # Load Sol's C6 + after-tax required-statement contracts.
    for n, contract in enumerate(REQUIRED_STATEMENT_CONTRACTS, start=len(corrections) + 1):
        ctx.corrections.append(Correction(
            index=n, severity="BLOCKER",
            topic=f"[sol-contract] {getattr(contract, 'topic', '') or 'required statement'}",
            plan_item_ref="", summary=contract.required_statement,
            evidence=list(getattr(contract, "must_be_absent", ()) or ()),
            reconcile_status="unresolved",
            canonical_facts=list(getattr(contract, "canonical_facts", ()) or ()),
            wrong_values=list(getattr(contract, "must_be_absent", ()) or ()),
            required_statement=contract.required_statement,
            source="verdict_feedback", verdict_agent="codex",
            source_run_id=463, source_draft_id=ANCHOR_PLAN_ID,
        ))
    ctx.rendered = _render_block(ctx)

    # Reopen run 470: its phases 1-2 are FRESH (donor reuse was suppressed)
    # and checkpointed. Resuming from 3 keeps that work and re-runs only the
    # synthesis the bad correction broke.
    run = session.get(DecisionRun, 470)
    if run is None or run.user_id != USER_ID:
        raise RuntimeError("run 470 missing")
    run.status = "running"
    run.finished_at = None
    session.commit()

    _newest_before = session.execute(
        sa.select(PlanVersion.id).where(PlanVersion.user_id == USER_ID)
        .order_by(PlanVersion.id.desc())
    ).scalar() or 0

    lines += [
        f"FRESH run {run.id} on anchor plan {ANCHOR_PLAN_ID} (role={pv.role})",
        f"corrections: {len(ctx.corrections)} "
        f"({len(corrections)} value-pair + {len(REQUIRED_STATEMENT_CONTRACTS)} sol contracts)",
        "forces_full_tier=True — donor-379 phase-1/2 reuse SUPPRESSED",
        f"rendered guidance: {len(ctx.rendered)} chars",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")

    _large_worker(
        session=session, user_id=USER_ID, decision_run=run,
        guidance=STANDING, anchor_plan_version_id=ANCHOR_PLAN_ID,
        corrective_ctx_override=ctx, resume_from_phase=3,
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
        f"newest plan: {newest[0] if newest else None} ({newest[1] if newest else None})",
        f"role='current' stays: plan {current}",
        ("SUCCEEDED — new draft written"
         if (newest and newest[0] > _newest_before) else "NO NEW DRAFT"),
    ]
except Exception:
    lines += ["FAILED", traceback.format_exc()[:4000]]
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
