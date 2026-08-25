"""Full 15-phase synthesis carrying the corrected two-rule NVDA glide.

Durable side-effects happen inside run_synthesis; this wrapper writes its
outcome to a file BEFORE printing (cp1252 console has killed runs before).
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

OUT = ROOT / "scratchpad" / "run_glide_synthesis.txt"
USER_ID = "ariel"

GUIDANCE = """\
STRATEGY CHANGE — the NVDA glide is now TWO STANDING RULES, not a terminating
schedule. Ariel confirmed the 2-year horizon on 2026-08-22 after being shown the
corrected tax figure.

RULE 1 — Sell 8,857 of the 10,380 vested shares over two years, LOWEST GRANT
BENCHMARK FIRST, retaining the ~1,523 highest-benchmark shares. Under Section 102
tax per share is 0.30(S-B) + 0.50B, so a higher benchmark costs MORE total tax: it
swaps 30%-taxed capital income for 50%-taxed ordinary income. The sell order is
213000 (18.11) then 182406 (18.32) then 246477 (31.97) then the 2023 ESPP (37.50)
then 289172/289173 (87.49). The retained high-benchmark set nearly coincides with
the shares not yet Section-102 eligible.

RULE 2 — Queue each NEW vest to its own Section-102 eligibility date, which is 24
months from ITS grant date. Never "sell on arrival": doing so would dump shares
inside their 24-month clocks and reclassify them WHOLLY to ordinary income at 50%.

NET CASH IS 68% OF GROSS, not 73%. The 2026 actuals read 73% only because the
ordinary slice had not yet been taken. Size all deployment off 68%.

TAX FIGURE — the 2-year glide totals ILS 1,849,929, NOT the ILS 1,274,268 quoted in
earlier drafts. The premium for 2 years over 7 is only ILS 68,543 (0.9% of the
position) because the ordinary slice is TIMING-INVARIANT: shares x benchmark is
fixed when the grant is priced, so pacing sales across more tax years cannot shrink
it. Only the capital slice's 2% surtax band responds to pacing. Bind every figure to
a {{fact:<key>}} token; do not write digits that will drift.

REDEPLOYMENT — proceeds go into UCITS (Irish-domiciled) instruments, NOT
US-domiciled ones. US-situs assets are currently 73.9% of the book against a USD 60k
non-resident estate exemption taxed up to 40%. Redeploying ~USD 1.9M of NVDA
proceeds into US-domiciled funds would preserve a seven-figure estate exposure at no
benefit; UCITS eliminates it at no cost, since the CGT is being paid either way.
This instruction must appear explicitly in the plan, not be left to the deploy path.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine)()

try:
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import run_synthesis

    lines.append("launching run_synthesis(trigger='check_in')")
    OUT.write_text("\n".join(lines), encoding="utf-8")

    result = run_synthesis(
        session, user_id=USER_ID, trigger="check_in", guidance=GUIDANCE)

    lines.append("COMPLETED")
    lines.append(repr(result)[:3000])
except Exception:
    lines.append("FAILED")
    lines.append(traceback.format_exc()[:6000])
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
