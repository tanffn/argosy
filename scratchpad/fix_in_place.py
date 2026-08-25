"""Fix draft 124 IN PLACE. No regeneration.

Ariel: "no after, you fix in place and we close it... we run in place."

He is right that a regeneration re-types every digit and re-rolls the same
contradictions. Every blocker across nine rounds has been one concept published
as two typed digits. These are surgical edits to the persisted draft, each one
replacing a stale literal with the settled value — the same wrong->canonical
pairing that has been the only correction type that ever lands.

Every edit is printed before and after. Nothing is promoted here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

PLAN_ID = 124
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

from argosy.state.models import PlanVersion  # noqa: E402

pv = session.get(PlanVersion, PLAN_ID)
if pv is None:
    raise SystemExit(f"plan {PLAN_ID} not found")
if pv.role == "current":
    raise SystemExit("refusing to edit role='current'")

BODY_COLS = [
    "horizon_long_md", "horizon_medium_md", "horizon_short_md",
    "horizon_long_md_audit", "horizon_medium_md_audit",
    "horizon_short_md_audit", "sections_json", "narrative_json",
]

# (wrong, canonical, why) — each is a settled value replacing a stale literal.
TEXT_FIXES = [
    ("₪311,584", "₪300,000",
     "assumption ledger A13 revived the superseded FI basis and called it "
     "BINDING; the settled basis is NIS 300,000 (goals_yaml, Ariel 2026-08-24)"),
    ("311,584", "300,000", "same, unprefixed occurrences"),
]

report: list[str] = []

# ---- 1. structured allocation doc: cap 12.0 -> 13.0 ----------------------
doc = json.loads(pv.target_allocation_json or "{}")
before_cap = doc.get("nvda_cap_pct")
if before_cap is not None and float(before_cap) != 13.0:
    doc["nvda_cap_pct"] = 13.0
    pv.target_allocation_json = json.dumps(doc)
    report.append(
        f"target_allocation_doc.nvda_cap_pct: {before_cap} -> 13.0  "
        "(settled adjudication, proposal 63 — must not be re-litigated)"
    )
else:
    report.append(f"target_allocation_doc.nvda_cap_pct already {before_cap}")

# ---- 2. body text: stale literals -> settled values ----------------------
for col in BODY_COLS:
    val = getattr(pv, col, None)
    if not val:
        continue
    new = val
    for wrong, right, _why in TEXT_FIXES:
        new = new.replace(wrong, right)
    if new != val:
        n = sum(val.count(w) for w, _r, _ in TEXT_FIXES)
        setattr(pv, col, new)
        report.append(f"{col}: {n} stale literal(s) replaced")

session.commit()

# ---- 3. verify on the real path -----------------------------------------
session.expire_all()
pv = session.get(PlanVersion, PLAN_ID)
body = " ".join(getattr(pv, c, "") or "" for c in BODY_COLS)
doc = json.loads(pv.target_allocation_json or "{}")

print("=== EDITS ===")
for line in report:
    print("  " + line)
print()
print("=== VERIFY ===")
print(f"  doc nvda_cap_pct        : {doc.get('nvda_cap_pct')}")
for pat in ("311,584", "300,000", "10,000,000", "68,403"):
    print(f"  {pat:12} occurrences : {body.count(pat)}")
print(f"  role                    : {pv.role}  (must not be 'current')")
