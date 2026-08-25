"""Decisive test: is the sim report's ordinary_income built on the 30-day-mean
BENCHMARK or on grant-date FMV?

For an eligible Section-102 lot the settled model says
    ordinary_income = shares x benchmark
so ordinary_income / shares should equal the trustee benchmark exactly.
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

from argosy.services.section_102 import KNOWN_NVDA_BENCHMARKS  # noqa: E402

USER_ID = "ariel"
DB_URL = f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}"
OUT = ROOT / "scratchpad" / "facts_lot_ordinary.txt"

lines: list[str] = []
engine = sa.create_engine(DB_URL)
session = sessionmaker(bind=engine)()

cols = [r[1] for r in session.execute(sa.text("PRAGMA table_info(tax_simulation_lots)"))]
lines.append("tax_simulation_lots columns: " + ", ".join(cols))
lines.append("")

rows = session.execute(sa.text(
    "SELECT * FROM tax_simulation_lots WHERE user_id=:u "
    "ORDER BY simulation_date DESC, id LIMIT 400"
), {"u": USER_ID}).fetchall()
lines.append(f"rows in latest pull: {len(rows)}")
lines.append("")

idx = {c: i for i, c in enumerate(cols)}


def g(r, name):
    i = idx.get(name)
    return r[i] if i is not None else None


latest = None
for r in rows:
    d = g(r, "simulation_date")
    if latest is None:
        latest = d
    if d != latest:
        continue
    sh = g(r, "shares")
    oi = g(r, "ordinary_income_usd")
    grant = str(g(r, "grant_id") or g(r, "grant_number") or "?")
    gdate = g(r, "grant_date")
    elig = g(r, "eligible") if "eligible" in idx else g(r, "holding_period")
    if not sh or oi is None:
        lines.append(f"  grant={grant:<10} shares={sh} ordinary={oi}  <- skipped")
        continue
    per_share = oi / sh
    bench = KNOWN_NVDA_BENCHMARKS.get(grant)
    tag = ""
    if bench:
        diff = (per_share - bench) / bench * 100.0
        tag = f"  benchmark={bench:<9.4f} delta={diff:+.2f}%"
    lines.append(
        f"  grant={grant:<10} gdate={str(gdate):<12} elig={str(elig):<8} "
        f"shares={sh:>8,.0f} ordinary/sh={per_share:>10.4f}{tag}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {OUT}")
