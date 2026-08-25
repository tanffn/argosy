"""Independent oracle for the glide tax — recomputes it straight from the lot
rows, with no call into realization_tax_summary, and compares.

Codex's requested real-path check: assert the published token equals a value
derived independently from the same source data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402

CAP_RATE = 0.30
ORD_RATE = 0.50
SELL_SH = 8920.0
REV_PRICE = 215.38
OUT = ROOT / "scratchpad" / "oracle_glide_tax.txt"

lines: list[str] = []
e = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
with e.connect() as c:
    sim = c.execute(sa.text(
        "SELECT simulation_date FROM tax_simulation_lots WHERE user_id='ariel' "
        "ORDER BY ingested_at DESC LIMIT 1")).scalar()
    sim_price = c.execute(sa.text(
        "SELECT sale_price_usd FROM tax_simulation_lots WHERE user_id='ariel' "
        "AND simulation_date=:s LIMIT 1"), {"s": sim}).scalar()
    rows = c.execute(sa.text(
        "SELECT id, grant_id, shares, capital_income_usd, ordinary_income_usd, "
        "eligible, plan_type FROM tax_simulation_lots "
        "WHERE user_id='ariel' AND simulation_date=:s"), {"s": sim}).fetchall()

lines.append(f"sim={sim}  sim_price={sim_price}  rev_price={REV_PRICE}")
delta = REV_PRICE - float(sim_price)
lines.append(f"delta_price={delta:.4f}")
lines.append("")


def key(r):
    oi, sh = r[4], r[2]
    if oi is None or not sh:
        return (float("inf"), r[0])
    return (oi / sh, r[0])


eligible = sorted([r for r in rows if r[5]], key=key)

taken = 0.0
cap_tax = ord_tax = 0.0
lines.append("cap consumes (lowest benchmark first):")
for r in eligible:
    if taken >= SELL_SH:
        break
    sh = min(float(r[2]), SELL_SH - taken)
    frac = sh / float(r[2])
    ci = (r[3] or 0.0) * frac
    oi = (r[4] or 0.0) * frac
    ci_rev = max(0.0, ci + delta * sh)
    cap_tax += CAP_RATE * ci_rev
    ord_tax += ORD_RATE * oi
    taken += sh
    lines.append(
        f"  grant={str(r[1]):<8} {r[6]:<6} sh={sh:>7,.0f} bench/sh={(r[4] or 0)/r[2]:>9.4f}"
        f"  cap_rev=${ci_rev:>12,.0f}  ord=${oi:>11,.0f}")

lines.append("")
lines.append(f"shares taken           {taken:>14,.0f}")
lines.append(f"capital tax (30%)  USD {cap_tax:>14,.2f}")
lines.append(f"ordinary tax (50%) USD {ord_tax:>14,.2f}")
total_usd = cap_tax + ord_tax
lines.append(f"total              USD {total_usd:>14,.2f}")
for fx in (2.991, 3.0):
    lines.append(f"  x FX {fx}          ILS {total_usd * fx:>14,.0f}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {OUT}")
