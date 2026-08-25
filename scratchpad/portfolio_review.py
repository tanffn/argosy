"""Current portfolio review — straight from the latest snapshot."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402

OUT = ROOT / "scratchpad" / "portfolio_review.txt"
lines: list[str] = []
e = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")

with e.connect() as c:
    row = c.execute(sa.text(
        "SELECT id, snapshot_date, imported_at, fx_usd_nis, fx_usd_eur, "
        "positions_json, allocations_json, totals_json, real_estate_json, pensions_json "
        "FROM portfolio_snapshots WHERE user_id='ariel' ORDER BY id DESC LIMIT 1"
    )).fetchone()

sid, sdate, imported, fx, fxe = row[0], row[1], row[2], row[3], row[4]
lines.append(f"SNAPSHOT {sid}   date={sdate}   imported={imported}")
lines.append(f"FX  USD/NIS={fx}   USD/EUR={fxe}")
lines.append("")


def j(x):
    if not x:
        return None
    return json.loads(x) if isinstance(x, str) else x


totals = j(row[7])
lines.append("TOTALS")
lines.append(json.dumps(totals, indent=2, ensure_ascii=False)[:1500])
lines.append("")

pos = j(row[5]) or []
if isinstance(pos, dict):
    pos = pos.get("positions") or list(pos.values())

rows = []
for p in pos:
    if not isinstance(p, dict):
        continue
    sym = p.get("symbol") or p.get("ticker") or p.get("name") or "?"
    usd = p.get("usd_value_k") or p.get("usd_value") or p.get("value_usd") or 0
    qty = p.get("quantity") or p.get("shares") or ""
    acct = p.get("account") or p.get("source") or ""
    cls = p.get("asset_class") or p.get("sigma_class") or p.get("category") or ""
    try:
        usd = float(usd)
    except Exception:
        usd = 0.0
    rows.append((sym, usd, qty, acct, cls))

tot = sum(r[1] for r in rows) or 1.0
rows.sort(key=lambda r: -r[1])
lines.append(f"POSITIONS  ({len(rows)} rows, total usd_k={tot:,.1f})")
lines.append(f"  {'symbol':<14}{'usd_k':>12}{'wt%':>8}  {'qty':>10}  {'account':<18}{'class'}")
for sym, usd, qty, acct, cls in rows:
    if usd <= 0:
        continue
    lines.append(f"  {str(sym):<14}{usd:>12,.1f}{usd/tot*100:>7.1f}%  {str(qty):>10}  "
                 f"{str(acct)[:17]:<18}{str(cls)[:24]}")

lines.append("")
alloc = j(row[6])
if alloc:
    lines.append("ALLOCATIONS")
    lines.append(json.dumps(alloc, indent=2, ensure_ascii=False)[:2000])

re_ = j(row[8])
if re_:
    lines.append("")
    lines.append("REAL ESTATE")
    lines.append(json.dumps(re_, indent=2, ensure_ascii=False)[:1200])

pen = j(row[9])
if pen:
    lines.append("")
    lines.append("PENSIONS")
    lines.append(json.dumps(pen, indent=2, ensure_ascii=False)[:1200])

OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {OUT}")
