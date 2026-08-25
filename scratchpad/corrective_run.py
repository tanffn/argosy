"""Corrective run: hand run 456's findings back to the fleet.

This is the loop the design calls for — full synth -> fleet raises findings
-> amend with those findings as guidance via the LARGE tier -> gates re-run
fresh -> repeat -> promote. Run 456 completed at 10:11 with codex BLOCK and
the FM rejecting; those verdicts are the input here.

FRESH run, NOT a resume of 456. Resuming would replay 456's phase-3 slice
checkpoints, which were authored at 08:11 — before the FI basis was settled
at 08:33 — and that stale-vs-canonical split is precisely codex finding [3].

Anchored on plan 122 (role='draft', produced by run 456) so the chain
accumulates instead of re-anchoring on the July `current`.

Guidance carries ONLY findings the fleet must resolve. Two things that were
blockers in earlier rounds are deliberately ABSENT because they were fixed
in code today and the fleet must not be told to work around them:
  * the NVDA cap divergence (d991f34 / c19bcea) — the resolver now returns
    one value at authoring time and after;
  * the missing fact tokens (4af3f59) — the figures reviewers demanded be
    tokenised now have display entries and will render.

Critically, the old "[B1] the resolver returns 13% on every path" directive
is GONE. It was mine, it was never verified, and it was false: the resolver
returned 12% while the guidance called 12% a banned literal. The FM's
rejection cited it as "a direct user-directive breach (Ariel set [B1])" —
Ariel set no such thing. Do not reintroduce unverified claims as directives.
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

OUT = ROOT / "scratchpad" / "corrective_run.txt"
USER_ID = "ariel"
ANCHOR_PLAN_ID = 123

GUIDANCE = """CORRECTIVE ROUND on plan 123. Resolve the findings below, change nothing else.

[F1] ONE NVDA SALE PROGRAMME — BLOCKER, highest priority.
The draft carries TWO incompatible programmes: sell 8,918 / retain 1,462, and a
quota programme of 3,924 in 2026 plus 5,493 in 2027 (9,417 total) with a 1,523
endpoint. The quota programme is SUPERSEDED and was published in error. Delete
every occurrence of 3,924 / 5,493 / 9,417 / 1,523. Publish exactly ONE
programme, sized from {{fact:concentration.nvda_sell_sh}} and
{{fact:concentration.nvda_target_sh}}.

[F2] THE 560-SHARE DIVERGENCE IS RESOLVED — state it as fact, not risk.
The tax simulation covers 10,940 shares; the snapshot holds 10,380. The
difference is a KNOWN SALE: 560 NVDA shares sold 2026-08-12 for USD 125,325.31
(Schwab Equity Awards transactions, ingested 2026-08-24 into the `lots` table,
27 rows). There is no unexplained divergence. Say so plainly and stop
describing the share universe as unreconciled.

[F3] LOT LEDGER — the Schwab lot rows ARE now imported. Per-lot COST BASIS and
acquisition dates are still absent, so per-lot eligibility claims stay
provisional: keep the ledger refresh as an explicit precondition on any DATED
order, but do not describe the holdings themselves as unknown.

[F4] FI TARGET TRACEABILITY — BLOCKER.
Publish the bridge line by line, each line cited: tracked T12
{{fact:spend.annual_t12_nis}} MINUS the finite mortgage runoff, PLUS the
amortized car cadence, PLUS the late-life healthcare ramp, PLUS the amortized
home-upgrade cadence, PLUS the user-settled adjustment, EQUALS
{{fact:spend.fi_basis_nis}}; the perpetuity is that divided by the 3.0% SWR =
{{fact:retirement.fi_target_nis}}. Also publish
{{fact:spend.annual_t12_donor_check_nis}} as the NON-AUTHORITATIVE
household_budget cross-check, with one sentence on why the tracked figure
governs. Both tokens render now — use them, never literals.

[F5] FIRE BRIDGE — say it once. It is described as both DISTINCT from and
FUNDED BY the finite-liability reserve. Choose ONE funding description and use
it in every horizon. Always publish {{fact:retirement.fire_bridge_nis}} and
{{fact:retirement.fire_bridge_offmandate_nis}} together, never one alone.

[F6] ONE VALUE PER CONCEPT. Every figure in every layer — prose, targets,
evidence facts, assumption ledger — comes from the SAME token. No layer may
publish a different value for a concept another layer already published.

[F7] SECTION COVERAGE. Every skeleton roster section must be expanded; a
section with nothing new states its standing position in one line. Where a
section genuinely has no analyst input this cycle, say so in PLAIN WORDS — do
NOT write the literal phrase "derivation pending", which the artifact-integrity
gate reads as an unrendered token and blocks on.

STANDING — never weakened: two standing glide rules; lowest grant benchmark
first; queue each vest to its trustee-confirmed Section-102 eligibility date;
net proceeds ~67.6% of gross; SGOV is SOLD into IB01 and is never a parking
place; SCHD is HELD and wound down GRADUALLY in annual slices with purchases
and DRIP stopped now, no FUSA/DHSA swap; SGLN remains a standalone gold line;
USD 125,800 deploys as a LUMP SUM at the next LSE open into CSPX 74,700 /
EXUS 37,500 / EIMI 13,600.
NEVER REAPPEAR: 3,924 / 5,493 / 9,417 / 1,523, ILS 1,849,929, ILS 1,274,268,
ILS 209,389, ILS 9,825,302, 59.9%, "park in SGOV".
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_amendment.workers import _large_worker
    from argosy.state.models import DecisionRun, PlanVersion

    anchor = session.get(PlanVersion, ANCHOR_PLAN_ID)
    if anchor is None or anchor.user_id != USER_ID:
        raise RuntimeError(f"anchor plan {ANCHOR_PLAN_ID} missing/cross-tenant")
    if anchor.role == "current":
        raise RuntimeError("refusing to anchor on role='current'")

    run = DecisionRun(
        user_id=USER_ID, ticker="PLAN", tier="large",
        decision_kind="plan_amendment", status="running",
    )
    session.add(run)
    session.commit()

    _newest_before = session.execute(
        sa.select(PlanVersion.id).where(PlanVersion.user_id == USER_ID)
        .order_by(PlanVersion.id.desc())
    ).scalar() or 0
    lines += [
        f"FRESH corrective run {run.id} (not a resume — 456's slices are stale)",
        f"anchor: plan {ANCHOR_PLAN_ID} (role={anchor.role})",
        "guidance: F1..F7 from run 456's codex + reader findings",
        "false [B1] cap directive REMOVED",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")

    _large_worker(
        session=session, user_id=USER_ID, decision_run=run,
        guidance=GUIDANCE, anchor_plan_version_id=ANCHOR_PLAN_ID,
    )

    # `_large_worker` swallows exceptions into status='failed' — re-read.
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
         if (newest and newest[0] > _newest_before) else "NO NEW DRAFT"),
    ]
except Exception:
    lines += ["FAILED", traceback.format_exc()[:5000]]
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
