"""Measured facts: what the resolver ACTUALLY publishes for the glide tax today.

Writes to a file BEFORE printing (cp1252 console has killed three runs).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

USER_ID = "ariel"
DB_URL = f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}"
OUT = ROOT / "scratchpad" / "facts_glide_tax.txt"

KEYS = [
    "tax.nvda_embedded_cgt_glide_nis",
    "tax.nvda_embedded_cgt_glide_dated_nis",
    "tax.nvda_embedded_cgt_nis",
    "tax.retention_at_vest_pct",
    "tax.retention_capital_track_pct",
    "concentration.nvda_sell_sh",
    "concentration.nvda_target_sh",
    "concentration.nvda_eligible_now_sh",
    "concentration.nvda_eligible_by_glide_horizon_sh",
    "concentration.nvda_cap_pct",
    "concentration.nvda_current_pct",
    "retirement.fi_margin_net_of_realization_glide_nis",
    "retirement.fi_margin_net_of_realization_glide_dated_nis",
]

lines: list[str] = []


def emit(s: str = "") -> None:
    lines.append(s)


engine = sa.create_engine(DB_URL)
Session = sessionmaker(bind=engine)
session = Session()

# Most recent plan-revision decision run.
row = session.execute(sa.text(
    "SELECT id, decision_kind, status, started_at FROM decision_runs "
    "WHERE user_id=:u AND ticker='(plan)' ORDER BY id DESC LIMIT 5"
), {"u": USER_ID}).fetchall()
emit("recent plan decision_runs (id, kind, status, started):")
for r in row:
    emit(f"  {r[0]}  {r[1]}  {r[2]}  {r[3]}")
emit()

run_id = row[0][0] if row else None
emit(f"resolving against decision_run_id={run_id}")
emit()

from argosy.services.plan_numeric_resolver import resolve_plan_numbers  # noqa: E402

res = resolve_plan_numbers(
    session, user_id=USER_ID, decision_run_id=run_id, include_canonical_ages=False)
values = res.values if hasattr(res, "values") else res

emit("RESOLVED GLIDE / TAX KEYS")
for k in KEYS:
    rv = values.get(k)
    if rv is None:
        emit(f"  {k:<58} <absent>")
        continue
    v = rv.value
    shown = f"{v:,.4f}" if isinstance(v, float) and abs(v) < 10 else (
        f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))
    emit(f"  {k:<58} {rv.status:<9} {shown}")
    if rv.status == "resolved" and getattr(rv, "formula", None):
        emit(f"      formula: {rv.formula}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {OUT}")
