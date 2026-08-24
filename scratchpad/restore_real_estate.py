"""Restore real_estate_json onto the head snapshot — reader blocker root cause.

Chain this unblocks (verified end to end on 2026-08-24):

    real_estate_json = []                       (snapshots 35..157)
      -> real_estate_equity_for_snapshot None
      -> legacy stub USD 141,606 is NON-zero, so
         total_net_worth_incl_residence REFUSES (correctly — it will not
         publish a "residence-inclusive" figure that silently dropped the
         residence)
      -> portfolio.total_net_worth_incl_residence_nis = pending
      -> 21 x "[derivation pending]" in the assembled artifact
      -> whole_artifact_reader.leakage_blocked
      -> no phase_55 row -> read_reader_verdict() None -> gate fails closed

Not a parser bug and not a reprice bug: `_parse_real_estate_row` reads all
eight rows of the owner sheet correctly, and `snapshot_refresh` carries
`real_estate` forward (snapshot_refresh.py:616). One bad file ingest on
2026-07-13 (snapshots 35-36) produced an empty list and every reprice since
inherited it.

Values come from the owner sheet — the documented source of truth for this
basis (owner estimates, unaudited). ONE correction is applied on top:
Ariel stated on 2026-08-24 that Atlanta "went to zero... I lost all, nothing
is on my name", so both its Home and Loan legs are zeroed (net equity 0)
rather than republishing the sheet's stale 318,000/219,475. Every other row
is taken verbatim.
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

SHEET = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/"
    "Family Finances Status - 26 Jun.tsv"
)
USER_ID = "ariel"
ZERO_OUT = "atlanta"  # Ariel 2026-08-24: lost entirely, not in his name

from argosy.ingest.tsv import _parse_real_estate_row  # noqa: E402
from argosy.services.net_worth_bases import (  # noqa: E402
    total_net_worth_incl_residence,
)
from argosy.services.plan_numeric_resolver import (  # noqa: E402
    _current_boi_usd_nis,
    _head_snapshot_row,
    _to_float,
)

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

# ---- parse the owner sheet ------------------------------------------------
lines = SHEET.read_text(encoding="utf-8-sig").split("\n")
start = next(
    i for i, ln in enumerate(lines) if "real estate details" in ln.lower()
)
rows: list[dict] = []
for i in range(start + 1, len(lines)):
    raw = lines[i].split("\t")
    if not any((c or "").strip() for c in raw):
        continue
    entry = _parse_real_estate_row(raw, line_no=i + 1)
    if entry is None:
        break  # left the section
    rows.append(entry.model_dump())

print(f"parsed {len(rows)} rows from {SHEET.name}")

zeroed = 0
for r in rows:
    if ZERO_OUT in (r.get("location") or "").lower():
        if r.get("value_local"):
            zeroed += 1
        r["value_local"] = 0.0
for r in rows:
    print(f"   {r['location'][:38]:38} {r['role']:5} "
          f"{r['currency']:3} {r['value_local']:>12,.0f}")
print(f"zeroed {zeroed} Atlanta leg(s) per Ariel 2026-08-24")

# ---- before -------------------------------------------------------------
snap = _head_snapshot_row(session, USER_ID)
fx, _src = _current_boi_usd_nis(session, _to_float(snap.fx_usd_nis) or 0.0)
before, _ = total_net_worth_incl_residence(
    snapshot=snap, fx_usd_nis=fx, session=session, user_id=USER_ID
)
print(f"\nBEFORE: head snapshot {snap.id}, real_estate_json="
      f"{snap.real_estate_json!r}, total NW incl residence = {before}")

# ---- write --------------------------------------------------------------
snap.real_estate_json = json.dumps(rows, default=str)
session.commit()

session.expire_all()
snap = _head_snapshot_row(session, USER_ID)
after, after_usd = total_net_worth_incl_residence(
    snapshot=snap, fx_usd_nis=fx, session=session, user_id=USER_ID
)
print(f"AFTER : total NW incl residence = "
      f"{after:,.0f} NIS (USD {after_usd:,.0f})" if after
      else "AFTER : STILL None — investigate before re-running synthesis")

# ---- the key that was blocking the reader --------------------------------
from argosy.services.plan_numeric_resolver import resolve_plan_numbers  # noqa: E402

v = resolve_plan_numbers(
    session, user_id=USER_ID, decision_run_id=456, include_canonical_ages=True
)
rv = v.get("portfolio.total_net_worth_incl_residence_nis")
print(f"\nresolver key: status={getattr(rv, 'status', None)} "
      f"value={getattr(rv, 'value', None)}")
