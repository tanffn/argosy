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

### A. Entry lens — should have said BUY (asymmetry recognition)
| Case (real) | Frozen at | Ground truth | PASS = |
|---|---|---|---|
| AMD (turnaround) | 2015-2016 | ~50x over 6y | BUY-class at small sleeve size (already in harness, passed masked) |
| AMD (owner case) | **three points: Jan-2023 (post-crash ~$65) · Apr-2025 (mid-cycle chop, thesis intact) · Mar-2026 (post-run)** | ~9x Jan-23→2026 | T1 BUY-class; T2 HOLD/ADD-class (category-D behavior mid-case); T3 re-derive-not-trim under the exit-discipline rule — mirrors the client's actual holding |
| PLTR | 2023 pre-GAAP-profit, post-crash | ~10x+ in 2y | BUY-class; falsifiers on gov-contract growth |
| NVDA | **early-2023 (the AI-capex moonshot moment: post-chatbot inflection, BEFORE the blowout datacenter guidance)** | ~10x from there | BUY-class on the AI-capex thesis before confirmation. ⚠ hardest decontamination in the suite — the story is world-famous; alias + rescale + genericize the AI narrative aggressively ("a new compute-intensive workload class"), and lean on the contamination check + synthetic control |
| NFLX | 2012 post-Qwikster collapse | ~40x | BUY-class; streaming-transition thesis |
| ANET | 2016 (cloud networking share gains vs CSCO) | ~15x | BUY-class |

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
| TTCF | T2 Aug-2021 (GM collapse, revenue still +45%) · T3 May-2022 · T4 Mar-2023 | zero | SELL-class by T2 or T3 at latest — the "growth real, business broken" separation (run in progress) |
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
