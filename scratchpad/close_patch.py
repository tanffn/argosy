"""Deterministic patch of draft 125 — Sol's four estate defects. No LLM.

Sol's finding: the draft publishes a stale US-situs subtotal ($3,029.1k) and a
derived tax estimate (~$1.19M) against canonical $3,051.5k / ILS 9,127,060 —
the difference being AVUV, REET and VHT, whose domicile IS US-situs and IS
already included; only the snapshot marker needs backfilling.

Impact: changes no trade, no allocation, no NVDA share count, no Section-102
calculation and no FI decision. But it would contaminate the counsel brief and
misstates the displayed tax estimate by roughly $9k. So it is struck
mechanically rather than silently blessed.

The replacement publishes ONLY the token. A derived dollar estimate typed as
digits is exactly the failure class that cost this week.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

PLAN_ID = 125
COLS = [
    "horizon_long_md", "horizon_medium_md", "horizon_short_md",
    "horizon_long_md_audit", "horizon_medium_md_audit",
    "horizon_short_md_audit", "sections_json", "narrative_json",
]

OLD = (
    "Estate: $3,029.1k of US-situs assets marked in the snapshot against a "
    "$60,000 exemption at 40% is roughly $1.19M of tax, published in shekels "
    "as {{fact:concentration.us_situs_estate_exposure_nis}}"
)
NEW = (
    "Estate: US-situs assets against a $60,000 per-decedent exemption taxed up "
    "to 40%, published in shekels as "
    "{{fact:concentration.us_situs_estate_exposure_nis}} (AVUV, REET and VHT "
    "are US-domiciled and already included; only the snapshot situs marker "
    "needs backfilling)"
)

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

from argosy.state.models import PlanVersion  # noqa: E402

pv = session.get(PlanVersion, PLAN_ID)
if pv is None:
    raise SystemExit(f"plan {PLAN_ID} not found")
if pv.role == "current":
    raise SystemExit("refusing to edit role='current'")

hits = 0
for col in COLS:
    val = getattr(pv, col, None)
    if not val or OLD not in val:
        continue
    setattr(pv, col, val.replace(OLD, NEW))
    n = val.count(OLD)
    hits += n
    print(f"  {col}: {n} replacement(s)")

if hits == 0:
    print("  NO MATCH — the exact sentence was not found; nothing changed")
else:
    session.commit()

session.expire_all()
pv = session.get(PlanVersion, PLAN_ID)
body = " ".join(getattr(pv, c, "") or "" for c in COLS)

print()
print("=== VERIFY (patched bytes) ===")
for pat, label in [
    (r"3,029\.1|3029\.1|3\.0291", "stale estate subtotal"),
    (r"1\.19M|1\.19 ?M", "stale estate tax estimate"),
]:
    n = len(re.findall(pat, body))
    print(f"  {label:28} {'CLEAN' if n == 0 else f'PRESENT x{n}'}")
tok = body.count("{{fact:concentration.us_situs_estate_exposure_nis}}")
print(f"  {'estate token present':28} x{tok}")
print(f"  {'[derivation pending]':28} x{body.count('[derivation pending]')}")
print(f"  {'{{fact:}} tokens':28} {body.count('{{fact:')}")
print(f"  {'role':28} {pv.role}")
