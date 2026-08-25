"""Dry-run the self-heal repair on a CLONE of the DB. Live data untouched.

`repair_plan_version` has no dry_run flag - it always applies. So the only safe
way to observe its behaviour on plan 125 (which is role='current') is to clone
the database, point the session at the clone, and inspect what it did there.

Sol's own build failed (exit=1, 436k tokens, no report), so nothing here is
taken on trust: this reports the actual before/after violation counts, every
handler receipt, and every refusal, read back from the clone.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

LIVE = ROOT / "db" / "argosy.db"
CLONE = ROOT / "scratchpad" / "repair_dryrun.db"

if CLONE.exists():
    CLONE.unlink()
shutil.copy2(LIVE, CLONE)
print(f"cloned {LIVE.name} -> {CLONE.name} ({CLONE.stat().st_size/1e6:.0f} MB)")

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from argosy.services.plan_repair import (  # noqa: E402
    PlanRepairRefused,
    repair_plan_version,
)

engine = sa.create_engine(f"sqlite:///{CLONE.as_posix()}")
# The feature migration is intentionally uncommitted, so the live DB (and its
# byte-for-byte clone) does not have the audit table yet.  Create only that new
# table on the disposable clone; never migrate or repair the live DB here.
from argosy.state.models import PlanRepairAttempt  # noqa: E402

PlanRepairAttempt.__table__.create(engine, checkfirst=True)
session = sessionmaker(bind=engine, expire_on_commit=False)()


def _cleanup_clone() -> None:
    session.close()
    engine.dispose()
    CLONE.unlink(missing_ok=True)


atexit.register(_cleanup_clone)

PLAN_ID = 125

print()
print("=== GUARD CHECK: refuse a current plan without allow_current ===")
try:
    repair_plan_version(session, plan_version_id=PLAN_ID, trigger="manual")
    print("  !! NOT REFUSED - the current-plan guard did not fire")
except PlanRepairRefused as exc:
    print(f"  refused as designed: {exc}")
except Exception as exc:  # noqa: BLE001
    print(f"  raised {type(exc).__name__}: {str(exc)[:160]}")

print()
print("=== REPAIR (allow_current=True) on the CLONE ===")
try:
    res = repair_plan_version(
        session, plan_version_id=PLAN_ID, trigger="manual",
        allow_current=True, include_bounded=True,
    )
except Exception as exc:  # noqa: BLE001
    import traceback
    print(f"  RAISED {type(exc).__name__}: {str(exc)[:300]}")
    traceback.print_exc(limit=6)
    raise SystemExit(1) from exc

print(f"  status              : {res.status}")
print(f"  base sha256         : {res.base_artifact_sha256[:24]}...")
print(f"  result sha256       : {getattr(res, 'result_artifact_sha256', '?')[:24]}...")
for attr in ("leakage_before", "leakage_after"):
    if hasattr(res, attr):
        print(f"  {attr:20}: {getattr(res, attr)}")

print()
print("  --- handler receipts ---")
for h in getattr(res, "handlers", []) or []:
    print(f"    {getattr(h,'handler','?'):28} candidates={getattr(h,'candidate_count',0):3} "
          f"applied={getattr(h,'applied_count',0):3} reverted={getattr(h,'reverted',False)} "
          f"gate={getattr(h,'gate_before',0):3}->{getattr(h,'gate_after',0):3} "
          f"delta={getattr(h,'gate_delta',0):+3}")

print()
print("  --- refusals (must include the 8,918 vs 3,378 conflict) ---")
refusals = getattr(res, "refusals", []) or []
if not refusals:
    print("    NONE - suspicious; the ceiling conflict should not be auto-repairable")
for r in refusals:
    print(f"    * {getattr(r,'finding','?')}: {getattr(r,'reason','?')}")

print()
print("=== VIOLATIONS on the clone, before vs after ===")
for label, js in (("before", getattr(res, "deterministic_before", None)),
                  ("after", getattr(res, "deterministic_after", None))):
    print(f"  {label}: {js}")

print()
print("=== LIVE DB UNTOUCHED? ===")
live_eng = sa.create_engine(f"sqlite:///file:{LIVE.as_posix()}?mode=ro&uri=true")
with live_eng.connect() as c:
    cur = c.execute(sa.text(
        "SELECT id FROM plan_versions WHERE user_id='ariel' AND role='current'"
    )).scalar()
    n = c.execute(sa.text("SELECT COUNT(*) FROM plan_versions")).scalar()
print(f"  live current plan: {cur} | live plan_versions rows: {n}")
