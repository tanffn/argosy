# Packet construction guide (for builder agents)

Read `docs/design/fleet_calibration_benchmark.md` (protocol section 1 + appendix) and
`README.md` here first. Reference fixtures: `packets/amd_2016_f2.json` (unpositioned
entry), `packets/ttcf_t2.json` (positioned exit point with the exit rule).

## Non-negotiable protocol

1. **Period-accurate facts.** Every figure comes from filings/press releases/news
   published ON OR BEFORE the freeze date. WebSearch/EDGAR for the real numbers; NEVER
   use a retrospective "why the stock moved" piece as a source. Every load-bearing fact
   gets a `sources[]` entry: `{"fact", "url", "date"}` where `date` = the source's
   PUBLICATION date (not the date you looked it up). The runner refuses to score a point
   with any source dated after the freeze.
2. **Decontamination, all four steps:**
   a. **Alias** the company (use the assigned alias); genericize brands, products,
      executives, celebrity partners, named counterparties ("a leading AI-model
      developer", "two of the largest mass retailers").
   b. **Rescale** every absolute dollar figure by the assigned per-case factor k
      (same k for every point of the same company). Ratios, growth rates, margins,
      and multiples stay EXACT. Prices/market caps may use a separate fixed factor if
      the reference packets for that company already did (NVDA/HLC: financials k=0.23,
      price factor 3.475 — reuse them).
   c. **Relative dates only**: "Q0 = just reported", "FY-1", "~14 months ago". NO
      calendar years anywhere in `analyst_reports` (audited). No named macro events —
      rewrite ("pandemic demand" -> "a demand surge from an external shock",
      "ChatGPT" -> "a consumer generative-AI chatbot", "Ukraine war" -> "a geopolitical
      energy shock").
   d. The runner does the output contamination check; you must fill
      `contamination_terms` with the real company/ticker/product/executive names an
      answer could leak.
3. **Wire shape**: `analyst_reports` = list of exactly two dicts, `agent_role:
   "fundamentals"` and `agent_role: "news"`, following the reference fixtures' field
   conventions (`market_cap_usd_m`, `share_price_usd`, `balance_sheet`,
   `annual_income_statement` (2-3 FYs), `latest_quarter`, `guidance`, `valuation`
   with peer context and price action, `confidence: "HIGH"`; news carries
   `business_description`, `recent_developments` (5-6 bullets, balanced — include the
   bear case), `macro_context`).
4. **Positioned points** (categories C and D): `positioned: true`,
   `constraints_extra: "exit_rule"`, and `positions` text following the ttcf_t2
   pattern — sleeve HOLDS from an earlier entry at the rescaled basis, states
   unrealized P&L, and asks hold/add/trim/exit. The packet's news report must restate
   the ENTRY THESIS and its milestones/falsifiers (see ttcf_t2/t3) so the agent can
   judge falsifiers, not price.
5. **Resolution block**: real (unscaled) freeze price and horizon price from yfinance
   adjusted closes (`.venv/Scripts/python.exe`, yfinance installed);
   `benchmark_return_pct` = buy-and-hold freeze->horizon for entry/hold cases,
   ride-to-terminal for traps/exits. Note the fetch date in `note`.
6. **Balance**: packets must NOT telegraph the answer. Include the genuine
   contemporaneous bear case for winners and the genuine bull case for traps
   (the reference packets show the calibration).
7. After writing fixtures, run
   `.venv/Scripts/python.exe -m pytest -m "not llm_eval" evals/fleet_calibration/test_calibration.py -q`
   and fix any failures.

## Registry (aliases + k), assigned — do not collide

| Company | Alias | k (financials) | Points |
|---|---|---|---|
| PLTR | Meridian Data Systems (MDS) | 3.0 (price factor 3.0 too) | pltr_2023_f1, pltr_2023_f2, pltr_2022_trough |
| NVDA | Helios Compute (HLC) | 0.23 (price factor 3.475, per existing HLC packet) | nvda_2023_f1, nvda_2023_f2 |
| AMD (owner era) | Vireon Semiconductor (VRS) | 1.35 | amd_owner_t1/t2/t3 |
| META | Nexus Social Group (NSG) | 0.44 | meta_2022 |
| NFLX 2022 | StreamCo Media (SMC) | 2.2 | nflx_2022 |
| NFLX 2012 | StreamCo Media (SMC) | 4.1 | nflx_2012_f1, nflx_2012_f2 |
| AMZN | Everport Commerce (EVC) | 1.6 | amzn_2014 |
| NKLA | HydroHaul Motors (HHM) | 0.7 | nkla_2020 |
| BYND | GreenGrain Foods (GGF) | 0.52 | bynd_2019 |
| WISH | DealDart (DDT) | 1.9 | wish_2021 |
| PTON | HomeStride Fitness (HSF) | 0.61 | pton_2021 (trap), pton_2022 (exit) |
| ZM | ConferLink (CFL) | 0.33 | zm_2022 |
| CVNA | AutoPort Online (APO) | 1.15 | cvna_2022, cvna_2023_reentry |
| ANET | LatticeWave Networks (LWN) | 2.9 | anet_2016_f1, anet_2017_f2 |
| AMD (2016 turnaround) | Corvid Microsystems (CVM) | 1.7 (price factor ≈2.84 per F2 convention) | amd_2016_f1, amd_2016_f2 |
| SmileDirectClub | ClearBite Oral (CBO) | 1.4 | sdc_2021 |
| Hyliion | VoltHaul Systems (VHS) | 0.85 | hyln_2021 |
| Boot Barn | Prairie Outfitters (POT) | 2.1 | boot_2017 |

Fresh synthetics (no real company): `nlf_synthetic` (Nexus Lattice Fabric / NLF, winner), `omk_synthetic` (Orbit Meal Kits / OMK, trap). Existing control pair remains `qbt_synthetic` + `srl_synthetic`.

`expected_classes` by grading: entry F1/F2 and synthetic winner -> ["buy"]; trap and
synthetic trap -> ["hold"] (pass = no position); exit -> ["sell"]; hold_drawdown and
rederive -> ["hold", "buy"].
