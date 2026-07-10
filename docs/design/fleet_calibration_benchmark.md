# Fleet Calibration Benchmark — the decontaminated time-machine protocol

**Status:** protocol proven ad-hoc (7/7 on the 2026-07 masked+synthetic runs; a four-time-point
exit-discipline case in progress); benchmark suite NOT yet built. This doc is the spec to build it.
**Purpose:** a repeatable eval that answers "is the fleet's judgment lens working?" after any
change (model upgrade, prompt edit, mandate change) — and catches regressions BEFORE live money.

## 1. The protocol (proven; do not weaken any step)

1. **Case packet** = the analyst-report bundle a real deep-decision run would receive
   (fundamentals + news reports in the exact wire shape `TraderAgent` consumes — see the
   reference harness), constructed from REAL historical filings/press as of a frozen date.
2. **Decontamination (all four, always):**
   a. **Alias** the company (TTCF → "Provida Foods (PVF)"); genericize brands, celebrity
      endorsers, named products, named executives.
   b. **Rescale** every dollar figure by a fixed per-case factor (e.g. ×3.7) — ratios, margins
      and growth rates survive; recognizable absolute numbers don't.
   c. **Relative dates only** ("Q0 = just reported", "FY-1"); no calendar years, no named
      macro events (rewrite "pandemic demand" → "a demand surge from an external shock").
   d. **Contamination check** on output: if any response names the real company or a real-world
      specific not present in the packet, the run is marked CONTAMINATED and doesn't score.
3. **Synthetic control pair** stays in every suite run: one fictional winner-shape + one
   fictional trap-shape (no real-world outcome exists → contamination-proof by construction).
   If the fleet fails the synthetic pair, the lens itself regressed — stop, don't interpret
   the real-case results.
4. **Multi-time-point cases** (the exit test): T1 entry-era packet judged cold; T2..Tn packets
   include position context ("the sleeve HOLDS this from an earlier entry at [rescaled basis]")
   so the verdict space is hold/trim/sell. The sleeve mandate + the exit-discipline rule
   (price alone never exits; falsifiers/milestones do) ride in the constraints, as in production.
5. **Scoring:** each case defines expected verdict CLASSES per time point (not exact sizes).
   A case passes when every time point lands in its expected class. Suite score = cases passed;
   report per-category subtotals (entry / trap / exit / hold-through-drawdown). Persist every
   run's raw outputs + score next to the harness for longitudinal comparison.

## 2. Calibration table — known symbols, ground truth, expected behavior

Categories are the four failure modes that cost real money. Every case is judged BLIND
(the fleet never sees these names).

### Freeze-point rule (how entry-case dates are DEFINED — never eyeballed off the chart)

For every entry case, derive three dated facts from the data, then set the freeze points:
- **T_parabola** — onset of the re-rating, from price history (first sustained leg of the move,
  e.g. trailing-60-trading-day return crossing +50% with follow-through, not a one-day spike).
- **T_signal** — the earliest CITABLE public fact supporting the thesis (a specific filing,
  guidance line, contract award, product datapoint), found in contemporaneous sources.
- **Two freeze points per case:**
  - **F1 = after T_signal, before T_parabola** — the *foresight* test. Graded leniently:
    a PASS is BUY-class, but a reasoned pass-with-falsifiers is only a half-fail (the signal
    was genuinely early).
  - **F2 = immediately after the first CONFIRMING print, inside the early parabola** — the
    *act-on-confirmation* test. Graded strictly: the remaining upside after confirmation was
    still large in every table case, and refusing to buy "because it already moved" is a real
    fleet failure mode (price-anchoring) this benchmark must catch.
If T_signal cannot be found before T_parabola (the move genuinely front-ran public data), the
case is recorded as F2-only — that honesty matters more than a bigger table.

### A. Entry lens — should have said BUY (asymmetry recognition)
Freeze dates below are DERIVED per the freeze-point rule; evidence in the appendix.
| Case (real) | Frozen at | Ground truth | PASS = |
|---|---|---|---|
| AMD (turnaround) | **F1 2016-02 · F2 2016-05** | ~50x over 6y | BUY-class at small sleeve size (already in harness, passed masked) |
| AMD (owner case) | **three points: Jan-2023 (post-crash, ~$65 early-Jan base; entry test) · Apr-2025 (deep drawdown −~57% off the Mar-2024 peak, fresh export-control charge, thesis intact; hold-through point) · Mar-2026 (post-first-re-rating consolidation ~$203, ~3x from T1; re-derive test)** | ~9x Jan-23→mid-2026 (~$65→~$581 Jun-2026) | T1 BUY-class; T2 HOLD/ADD-class (category-D behavior mid-case); T3 re-derive-not-trim under the exit-discipline rule — mirrors the client's actual holding; the Apr–Jun-2026 leg vindicates T3 |
| PLTR | 2023 pre-run, post-crash — **F1 2023-03 · F2 2023-06** | ~10x+ in 2y | BUY-class; falsifiers on gov-contract growth |
| NVDA | **early-2023 (the AI-capex moonshot moment: post-chatbot inflection, BEFORE the blowout datacenter guidance) — F1 2023-01 · F2 2023-06** | ~10x from there | BUY-class on the AI-capex thesis before confirmation. ⚠ hardest decontamination in the suite — the story is world-famous; alias + rescale + genericize the AI narrative aggressively ("a new compute-intensive workload class"), and lean on the contamination check + synthetic control |
| NFLX | 2012 post-Qwikster collapse — **F1 2012-06 · F2 2013-02** | ~40x | BUY-class; streaming-transition thesis |
| ANET | 2016 (cloud networking share gains vs CSCO) — **F1 2016-06 · F2 2017-03** | ~15x | BUY-class |

### B. Trap lens — should have said PASS (hype rejection)
| Case | Frozen at | Ground truth | PASS = |
|---|---|---|---|
| GPRO | 2014-2015 peak | -90%+ | PASS/hold-no-position (in harness, passed masked) |
| TTCF | Dec-2020 SPAC hype (T1) | zero by 2023 | PASS *or* small-slot BUY WITH margin/concentration falsifiers recorded (see §3) |
| NKLA | mid-2020 | fraud, ~zero | PASS (no revenue + grand claims = trap-shape) |
| BYND | mid-2019 peak (~$230) | -95% | PASS (valuation vs commoditization) |
| WISH | IPO 2021 | ~zero | PASS (cohort decay visible in the S-1) |
| PTON | late-2021 (~peak, demand normalizing) | -95% | PASS / SELL-class if positioned |

### C. Exit discipline — held from earlier, should have SOLD on fired falsifiers
| Case | Time points | Ground truth | PASS = |
|---|---|---|---|
| TTCF | T2 Aug-2021 (GM collapse, revenue still +45%) · T3 May-2022 · T4 Mar-2023 | zero | SELL-class by T2 or T3 at latest — the "growth real, business broken" separation. **RUN 2026-07-10 (artifacts tmp/fleet_timemachine/ttcf_*): T1 correct PASS on the hype (0% vs -100%); T2 HOLD = the one miss (saw the margin falsifier "bent, not fired"; cost 60% of remaining value); T3 SELL-ALL HIGH via fired falsifiers under the no-price-exit rule (salvaged 35% of basis vs ride-to-zero); T4 DISQUALIFIED — packet used 10-K figures filed after the freeze (the temporal-integrity audit's first catch).** Convention candidate from the T2 gap: a guidance FLIP on the profitability milestone counts as falsifier-FIRED, not bent (would have exited at -14%) — adopt into the exit doctrine after one more case confirms it. |
| PTON | 2022 (inventory writedowns, demand cliff) | -95% from entry era | SELL-class on the demand-falsifier |
| ZM | 2021-2022 (growth decel from 300%+ to teens, churn) | -85% | SELL/TRIM-class when the growth thesis breaks |
| CVNA | mid-2022 (debt spiral era) | -98% THEN 50x recovery | SELL-class in 2022 is a PASS (the information available said survival risk); the 2023 recovery does NOT retroactively fail the case — score on decision quality, not outcome luck. Optional T-point at the 2023 restructuring = re-entry-worthy signal |

### D. Hold-through-drawdown — price crashed, thesis intact: should have HELD (the PLTR-scar test)
| Case | Frozen at | Ground truth | PASS = |
|---|---|---|---|
| PLTR | 2022 trough (~$6-8, -80% from top; gov revenue still growing, FCF turning) | 10x+ from there | HOLD/ADD-class — selling here on price = the exact client scar |
| META | Nov-2022 (~$90; core FCF intact, capex fear) | ~7x | HOLD/ADD-class |
| NFLX | mid-2022 (~$170 post-subscriber-loss panic) | ~4x | HOLD-class (ads/paid-sharing optionality) |
| AMZN | 2014 (loss headlines, AWS not yet broken out) | ~10x | HOLD/ADD-class |

Category D is the counterweight to C: a fleet that passes C by selling everything that falls
fails D. Calibration = passing BOTH, which is only possible by judging falsifiers, not price.

## 2b. Per-case report format (owner-specified, 2026-07-10 — every suite run reports this table)

One row per symbol per time point, three columns, none optional:

| Column | Content |
|---|---|
| **Fleet reasoning** | Verdict (buy/sell/hold/trim) + conviction + SIZE ("buy a lot or a starter slot") + the decisive reasoning line + the falsifiers it recorded + **the clock (owner-specified 2026-07-10): the NEXT VALIDATION POINT (the dated or estimated event where the thesis gets tested — an earnings print, launch, readout, contract renewal) and the expected re-rating horizon with an honest band ("3-6 months" vs "2-3 years"). Hard to estimate ≠ optional: an unestimable horizon is itself reportable and lowers conviction.** Scoring later checks horizon calibration: did re-ratings land inside the stated bands? |
| **Reasoning-integrity audit** | Did it cheat? TWO checks: (a) **packet audit** — every fact in the case packet must come from a source DATED ON OR BEFORE the freeze date (a "why the stock moved" piece from a week later is future knowledge; we are in the future relative to the test — the packet builder must cite each source's date, and the audit re-verifies); (b) **output audit** — the fleet's reasoning must not use any fact absent from the packet (real names, later events, outcome knowledge). Either failure = the point doesn't score. |
| **Agent score** | Was the reasoning correct → "if we acted on this verdict we would have gained/lost **NN%**" measured at the case's resolution horizon (and to the terminal outcome for trap/exit cases), vs the benchmark path (buy-and-hold / ride-to-zero) |

The suite summary aggregates the third column per category: "acting on the fleet at every
frozen point would have returned X% vs Y% benchmark" — the number that says whether the fleet
earns live-money trust.

## 3. Scoring judgment calls (recorded so they're not re-litigated)

- A trap-case BUY is a *graded* fail: full fail if bought without the killing falsifier
  recorded; half-credit if bought small WITH the falsifier that later fired (the protocol's
  point is the falsifier discipline, and category-C behavior then decides the money outcome).
- Outcome luck never overrides decision quality (CVNA rule above).
- MEDIUM-conviction verdicts in the right class count as passes; conviction calibration is
  reported separately (are HIGHs righter than MEDs? they should be).

## 4. Implementation notes (for the building agent)

- Reference harness: `tmp/fleet_timemachine/` (`run_game.py` invocation pattern +
  `run_synthetic.py` + `packets_sanitized.json` shape). LIFT it into a committed home:
  `evals/fleet_calibration/` — packets as JSON fixtures, runner script, scorecard persisted as
  `evals/fleet_calibration/runs/<date>.json`. Mark the pytest entry `-m llm_eval` (real LLM
  calls; excluded from the default suite).
- Packet construction is the labor: each case needs period-accurate figures from filings
  (WebSearch/EDGAR), then the four-step sanitization. Build order: lift the 4 existing cases
  (AMD, GPRO, QBT, SRL) + TTCF's four points, then one case per category per session.
- Cadence: run the suite on every fleet-affecting change (model swap, trader/mandate prompt
  edit) and quarterly otherwise; the scorecard lands in the jobs/transparency surface like any
  other calibration artifact ("benchmark N/M, last run <date>").
- Rot guard: cases FROZEN once written (packet content never edited except factual-error fixes,
  logged). New models may have later knowledge cutoffs — the alias+rescale+relative-dates
  protocol is what keeps old cases valid; the synthetic pair is the tripwire if it ever stops
  sufficing (add fresh synthetics, they're cheap).

## Appendix: freeze-point derivations (2026-07-10; yfinance adjusted closes + contemporaneous sources)

T_parabola rule as implemented: first trading day the trailing-60-trading-day return ≥ +50%
with follow-through (r60d stays > +30% for the next 20 sessions AND price 60 sessions later ≥
price at the cross). All closes split-adjusted.

### NVDA (AI-capex era)
- **T_parabola = 2023-03-16** (r60d +54%). Monthly closes: 2022-10 13.48 · 2022-12 14.60 ·
  2023-01 19.52 · 2023-02 23.19 · 2023-03 27.75 · 2023-05 37.80 · 2023-07 46.69.
- **T_signal = 2022-11-30** — ChatGPT public launch (the product datapoint that created the
  AI-capex thesis; 1M users in 5 days). Corroborated by the Q3-FY23 print 2022-11-16 (Data
  Center $3.83B +31% YoY while total revenue fell 17% — sec.gov/Archives/edgar/data/1045810/000104581022000163/q3fy23pr.htm)
  and Microsoft's ~$10B OpenAI investment 2023-01-23 (cnbc.com/2023/01/23/microsoft-announces-multibillion-dollar-investment-in-chatgpt-maker-openai.html).
- **F1 2023-01** (signal ~2 months old, price pre-parabola). **F2 2023-06** — immediately after
  the first confirming print inside the parabola: Q1-FY24 on 2023-05-24, record DC $4.28B +
  the $11.0B guide (sec.gov/Archives/edgar/data/1045810/000104581023000087/q1fy24pr.htm);
  remaining upside from ~$42 was still >3x.

### AMD (owner case — three hand-picked points, verified)
- Two re-rating legs, both derived: **first parabola onset 2024-01-16** (r60d +55%; monthly:
  2023-10 98.50 → 2023-12 147.41 → 2024-02 192.53) and a **second leg from 2025-07/10**
  (crossings 2025-07-02 r60d +62%; 2025-09 161.79 → 2025-10 256.12 on the OpenAI deal).
- **T1 Jan-2023 (entry test)** — post-crash base: 2022-12 close $64.77, 2023-01 close $75.15
  (the "~$65" is the late-Dec/early-Jan level; verified). Dated fact: Lisa Su's CES keynote
  2023-01-04/05 previewed Instinct MI300 (146B-transistor DC AI APU) — the DC-AI thesis was
  citable (forbes.com/sites/tiriasresearch/2023/01/23/amds-lisa-su-goes-extra-time-with-announcements-during-ces-2023-keynote/).
- **T2 Apr-2025 (hold-through point — relabeled from "mid-cycle chop")** — 2025-04 close
  $97.35 ≈ −57% from the Mar-2024 peak (~$227), i.e. a DEEP drawdown with fresh bad news: the
  2025-04-16 8-K disclosing up to ~$800M MI308 export-control charges
  (ir.amd.com/financial-information/sec-filings/content/0000002488-25-000039/amd-20250415.htm);
  roadmap/DC-AI thesis intact. Category-D behavior is the expected class here.
- **T3 Mar-2026 (post-first-re-rating consolidation, re-derive test — relabeled from
  "post-run")** — 2026-03 close $203.43 (~3.1x T1), AFTER the 2025-10-06 OpenAI 6GW
  Instinct partnership + 160M-share warrant
  (amd.com/en/newsroom/press-releases/2025-10-6-amd-and-openai-announce-strategic-partnership-to-d.html)
  but BEFORE the Apr–Jun-2026 leg (2026-04 354.49 · 2026-06 580.91 ≈ 9x the ~$65 base).
  Re-derive-not-trim at T3 was subsequently vindicated; ground truth "~9x" confirmed.

### PLTR (2023)
- **T_parabola = 2023-05-23** (r60d +57%; ignited by the 2023-05-08 Q1 print). Monthly closes:
  2022-12 6.42 · 2023-02 7.84 · 2023-04 7.75 · 2023-05 14.71 · 2023-07 19.84.
- **T_signal = 2023-02-13** — first GAAP-profitable quarter announced (Q4-2022 GAAP net income
  $31M, EPS $0.01; sec.gov/Archives/edgar/data/1321655/000132165523000005/a2022q4ex991pressrelease.htm;
  cnbc.com/2023/02/13/palantir-q4-earnings-2022.html).
- **F1 2023-03** (post-profit-print, price still ~$8). **F2 2023-06** — immediately after the
  confirming 2023-05-08 print (2nd GAAP-profitable quarter + AIP demand) that ignited the
  parabola; at ~$15 the remaining upside was still ~8x.

### ANET (2016)
- **Honest deviation: the strict +50%/60d rule NEVER fires in 2015-2018** — the 2016 re-rating
  was a slow grind (monthly closes, split-adj: 2016-01 3.75 → 2016-12 6.05, +61% without a
  60-day burst). Relaxed crossing: r60d ≥ +30% with follow-through on **2017-03-09**, ignited
  by the Q4-2016 print (2017-02-16); **T_parabola ≈ 2017-02/03** (2017-01 5.88 → 2017-03 8.27
  → 2017-05 9.21 → 2017-11 14.57).
- **T_signal = FY2015 Form 10-K (filed 2016-03)** — Microsoft, via World Wide Technology, was
  12.0% of FY2015 revenue (>10% in 2013/2014/2015 too), customers included six of the largest
  cloud providers — the cloud-titan share-gain thesis in a filing
  (sec.gov/Archives/edgar/data/1596532/000159653216000244/anet20151231-10k.htm).
- **F1 2016-06** (signal on file, price mid-grind ~$64 pre-split). **F2 2017-03** — immediately
  after the Q4-2016 confirming print, early in the only leg that qualifies as a parabola. Note
  F2 falls outside the nominal "2016" label; kept because the rule outranks the label.

### NFLX (2012)
- **T_parabola = 2012-12-04** (r60d +53%; same day as the Disney exclusive-output deal
  announcement — engadget.com/2012-12-04-netflix-strikes-licensing-agreement-with-disney-will-be-exclusi.html).
  Monthly closes (split-adj): 2012-09 0.78 · 2012-10 1.13 · 2012-12 1.32 · 2013-01 2.36 ·
  2013-04 3.09.
- **T_signal = 2012-04-23** — Q1-2012 shareholder letter: +1.74M US streaming net adds, the
  first clean post-Qwikster recovery print
  (sec.gov/Archives/edgar/data/0001065280/000144530512001133/nflx-033112xex991.htm).
- **F1 2012-06** (recovery print on record, stock near post-collapse lows). **F2 2013-02** —
  immediately after the first confirming print inside the parabola: Q4-2012 on 2013-01-23,
  +2.05M US streaming adds (sec.gov/Archives/edgar/data/0001065280/000106528013000005/nflx-0123112xex991.htm).

### AMD (2015-16 turnaround — light verification; case already passed)
- **T_parabola = 2016-04-22** (r60d +87%), the first trading day after the 2016-04-21 Q1-2016
  print + THATIC China x86 JV ($293M licensing;
  forbes.com/sites/patrickmoorhead/2016/04/21/amd-forms-china-x86-server-soc-jv-and-gets-293-million-to-start/).
  Monthly closes: 2015-09 1.72 · 2016-02 2.14 · 2016-04 3.55 · 2016-06 5.14 · 2016-08 7.40.
- **T_signal = 2015-05-06** — Financial Analyst Day: Zen roadmap with up to +40% IPC + SMT
  (ir.amd.com/news-events/press-releases/detail/611/amd-outlines-sharpened-focus-at-2015-financial-analyst-day).
- **F1 2016-02** (signal 9 months old, price ~$2.14 near the bottom). **F2 2016-05** —
  immediately after the igniting 2016-04-21 print; at ~$3.6-4.6 the remaining upside was still
  >10x.
