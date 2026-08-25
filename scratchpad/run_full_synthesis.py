"""Full 15-phase synthesis — the ONLY path that produces a promotable plan.

Why this and not another amendment: promote_gate requires five authorities, two
of which (codex phase_45, reader phase_55) are written only by a full run, and
run_codex_second_opinion needs Phase 1/2/4 artifacts an amendment never
produces. Three amendment passes produced good CONTENT (plans 117->118->119)
that no authority could ever clear. This run generates the verdicts for real.

Why it should not take hours this time: runs 436 and 437 died at 4h+ and 1h36m
because critique_reconcile repriced all 51 positions once per FINDING rather
than once per round (fixed in 2d760db). Live quotes are NOT stubbed here — this
is the authoritative run and its numbers should be freshly priced.

The guidance carries everything settled today so the synthesizer does not
re-derive it wrongly: the two standing rules, the canonical resolver keys, the
SGOV disposal, the SCHD hold, and the corrected household income.
"""
from __future__ import annotations

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

OUT = ROOT / "scratchpad" / "run_full_synthesis.txt"
USER_ID = "ariel"

GUIDANCE = """\
The NVDA glide is TWO STANDING RULES, not a terminating schedule. Ariel
confirmed the 2-year horizon on 2026-08-22. Plan 119 already carries this
content correctly - preserve its substance; this run exists to produce the
review verdicts that an amendment cannot.

RULE 1 - Sell {{fact:concentration.nvda_sell_sh}} of the vested NVDA shares over
two years, LOWEST GRANT BENCHMARK FIRST, retaining
{{fact:concentration.nvda_target_sh}}. Section-102 tax per share is
0.30(S-B) + 0.50B, so a HIGHER benchmark costs MORE total tax - it swaps
30%-taxed capital income for 50%-taxed ordinary income. Sell order by benchmark:
213000, 182406, 246477, the 2023 ESPP, then 289172/289173.

RULE 2 - Queue each new vest to its TRUSTEE-CONFIRMED Section-102 eligibility
date (ordinarily 24 months from grant/deposit with the trustee). Never sell on
arrival: that reclassifies the shares wholly to ordinary income at 50%.

ANY text asserting the old terminating schedule - 3,924 in 2026 plus 5,493 in
2027, or a 9,417 total - is SUPERSEDED and must not appear anywhere.

BIND EVERY FIGURE to a {{fact:<key>}} token. Never write these as digits:
  {{fact:concentration.nvda_sell_sh}} {{fact:concentration.nvda_target_sh}}
  {{fact:concentration.nvda_cap_pct}} {{fact:concentration.nvda_current_pct}}
  {{fact:tax.nvda_embedded_cgt_glide_nis}} {{fact:tax.nvda_embedded_cgt_nis}}
  {{fact:concentration.us_situs_estate_exposure_nis}}
  {{fact:retirement.fi_margin_net_of_realization_glide_nis}}
  {{fact:portfolio.net_worth_nis}} {{fact:income.household_net_annual_nis}}
  {{fact:income.primary_net_annual_nis}} {{fact:income.secondary_net_annual_nis}}
  {{fact:income.other_recurring_annual_nis}}
Stale literals that must NEVER reappear: ILS 1,849,929 and ILS 1,274,268 for the
glide tax, ILS 209,389 for the FI margin, ILS 9,825,302 for US-situs.

NET CASH - net proceeds are approximately 67.6% of gross, not 73%. Size
deployment off actual settled proceeds, never gross times a rounded factor.

SGOV IS BEING SOLD into IBTA (iShares USD Treasury 1-3yr UCITS) - it is a
US-domiciled wrapper. Never instruct buying or parking proceeds in SGOV.

SCHD - HOLD. Do NOT instruct a swap into FUSA or DHSA. Reviewed 2026-08-23:
DHSA returned 9.64% annualised since inception against SCHD's ~13.19%, so the
performance give-up exceeds the contingent estate saving. Stop purchases and
DRIP, redirect new flow to the UCITS core.

CASH DEPLOYMENT - Ariel is deploying USD 125.8k as a LUMP SUM on the next LSE
session into CSPX 74.7k / EXUS 37.5k / EIMI 13.6k. Not dollar-cost-averaged.

ESTATE - the estate section must carry REASONING, not just a documents
checklist: why NVDA and US-domiciled ETFs are US-situs; why Irish UCITS removes
wrapper-level situs; that the USD 60,000 exemption is PER DECEDENT; and that the
glide plus UCITS migration is the operative remedy.

HOUSEHOLD INCOME is a resolved fact and the components sum exactly:
primary + secondary + other = household.
"""

lines: list[str] = []
engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

try:
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import run_synthesis

    lines.append(f"started {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("full 15-phase run — produces codex/reader/FM verdicts")
    OUT.write_text("\n".join(lines), encoding="utf-8")

    result = run_synthesis(
        session, user_id=USER_ID, trigger="check_in", guidance=GUIDANCE)
    lines.append("COMPLETED")
    lines.append(repr(result)[:2000])
except Exception:
    lines.append("FAILED")
    lines.append(traceback.format_exc()[:6000])
finally:
    OUT.write_text("\n".join(lines), encoding="utf-8")

print("done -> " + str(OUT))
