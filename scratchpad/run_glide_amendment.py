"""Amend plan 116 with the corrected two-rule NVDA glide.

Replaces the killed full run 436. Phase 3 only, with REAL section freezing:
everything except the six named sections is restored verbatim from plan 116.

Two traps this script exists to avoid, both documented and both previously
paid for:
  * freeze against plan 116, NOT ``prior_current`` — role='current' is plan 92
    from 2026-07-13, so freezing against it would silently revert everything
    93->116 added.
  * unfreeze only UNIQUE section keys. `targets`, `themes`, `actions` and
    `rationale` each appear once per horizon; pairing repeated keys was broken
    until 3648c76 and is still a sharp edge worth not leaning on.
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

OUT = ROOT / "scratchpad" / "run_glide_amendment.txt"
USER_ID = "ariel"
BASELINE_PLAN_ID = 118

#: Sections this pass is allowed to rewrite. Everything else is frozen.
FREEZE_EXCEPT = {
    # NARROW third pass. Plan 118 fixed the contradictions; two figures remain
    # unbound: a stale US-situs literal in `targets` (ILS 9,825,302 against a
    # derived ILS 9,127,060) and the glide tax, which 118 omits entirely.
    "targets",
    "tax_plan",
}

GUIDANCE = """Amend ONLY `targets` and `tax_plan`. Every other section is frozen and
restored verbatim - do not restate or touch them.

TWO FIXES, both about replacing an unsourced number with its resolver token.

1. US-SITUS EXPOSURE. The `targets` section states ILS 9,825,302 as the
   US-situs estate exposure. That literal is STALE - the derived value is
   ILS 9,127,060, a difference of about ILS 698,000. Replace the digits with
   {{fact:concentration.us_situs_estate_exposure_nis}} so it can never drift
   again. Keep the surrounding explanation.

2. THE GLIDE TAX IS MISSING. `tax_plan` explains the 0.30(S-B) + 0.50B
   mechanism correctly but states no total. Add it as
   {{fact:tax.nvda_embedded_cgt_glide_nis}} - the tax on the planned two-year
   sale - and, alongside it, {{fact:tax.nvda_embedded_cgt_nis}} as the
   full-liquidation bound. Label which is which. Never write either as digits.

Change nothing else. Do not restate the glide rules, do not touch the sell
order, do not reopen SCHD or SGOV - those are settled in the frozen sections.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    # ---------------------------------------------------------------------
    # Stub live quotes. critique_reconcile calls refresh_portfolio_snapshot,
    # which reprices all 51 positions against yfinance, and the plan flow
    # invokes it once per reconcile round: measured 3 full passes at ~30 min
    # each (GOOG repriced at 09:39, 10:10, 10:40). That, not the LLM work,
    # is what made run 436 take 4+ hours.
    #
    # Returning None is the module's OWN documented miss path — the previous
    # price is carried forward with a warning, never fabricated (see
    # test_quote_miss_carries_old_value_and_warns). Snapshot 151 is from
    # 2026-08-22 and is what the plan already reasons from, so this changes
    # no figure the amendment depends on. It only stops us paying 30 minutes
    # per round for quotes we do not use.
    # ---------------------------------------------------------------------
    import argosy.services.snapshot_refresh as _sr

    _stub_calls = {"n": 0}

    def _no_live_quote(symbol, *, currency, details):  # noqa: ANN001
        _stub_calls["n"] += 1
        return None

    _sr.default_quote_fn = _no_live_quote

    from argosy.orchestrator.flows.plan_amendment.workers import _medium_worker
    from argosy.state.models import DecisionRun, PlanVersion

    baseline = session.get(PlanVersion, BASELINE_PLAN_ID)
    if baseline is None:
        raise RuntimeError(f"plan {BASELINE_PLAN_ID} not found")
    if baseline.user_id != USER_ID:
        raise RuntimeError("cross-tenant baseline — refusing")
    lines.append(f"freeze baseline: plan {BASELINE_PLAN_ID} role={baseline.role}")
    lines.append(f"unfrozen sections: {sorted(FREEZE_EXCEPT)}")

    run = DecisionRun(
        user_id=USER_ID, ticker="(plan)", tier="medium",
        decision_kind="plan_amendment_chat", status="running",
        started_at=datetime.now(timezone.utc),
        notes_json=json.dumps({
            "message": "corrected two-rule NVDA glide",
            "freeze_except": sorted(FREEZE_EXCEPT),
            "freeze_baseline_plan_id": BASELINE_PLAN_ID,
        }),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    lines.append(f"decision_run={run.id}")
    OUT.write_text("\n".join(lines), encoding="utf-8")

    _medium_worker(
        session=session, user_id=USER_ID, decision_run=run,
        guidance=GUIDANCE,
        freeze_except=FREEZE_EXCEPT,
        freeze_baseline_plan_id=BASELINE_PLAN_ID,
    )
    lines.append("COMPLETED")
    lines.append(f"live-quote calls intercepted: {_stub_calls['n']}")
except Exception:
    lines.append("FAILED")
    lines.append(traceback.format_exc()[:6000])
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
