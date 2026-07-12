# Fleet-calibration report — 2026-07-12-fable-ab.json

| Point | Category | Fleet verdict (conv, size) | In class? | Score | Acted vs benchmark |
|---|---|---|---|---|---|
| amd_2016_f1 | A/F1_lenient | buy (MEDIUM, 1.0 currency) | Y | 1.0 | +5357% vs +5357% |
| boot_2017 | A/entry | buy (MEDIUM, 1.0 currency) | Y | 1.0 | +1855% vs +1855% |
| cvna_2023_reentry | A/entry | buy (MEDIUM, 1.0 currency) | Y | 1.0 | +649% vs +649% |
| hyln_2021 | B/trap | hold (MEDIUM, 0.0 shares) | Y | 1.0 | +0% vs -86% |
| sdc_2021 | B/trap | hold (HIGH, 0.0 shares) | Y | 1.0 | +0% vs -100% |
| ttcf_t2 | C/exit | hold (MEDIUM, 0.0 shares) | N | 0.0 | -100% vs -100% |

## Category subtotals
- **A**: 3.0/3 points; acting on fleet +2620% vs benchmark +2620% (n=3)
- **B**: 2.0/2 points; acting on fleet +0% vs benchmark -93% (n=2)
- **C**: 0.0/1 points; acting on fleet -100% vs benchmark -100% (n=1)
- **TOTAL**: 5.0/6; acting on the fleet at every frozen point: +1293% avg vs +1262% benchmark (n=6)

## Conviction calibration
- HIGH: 1.0/1 (100%)
- MEDIUM: 4.0/5 (80%)


## Horizon calibration (§2b clock band)
- not_applicable: 6
- amd_2016_f1: not_applicable (stated=12-18 mo (12-18 months); actual_months=None)
- boot_2017: not_applicable (stated=24-48 mo (2-4 years); actual_months=None)
- cvna_2023_reentry: not_applicable (stated=2-3 mo (2-3 months); actual_months=None)
- hyln_2021: not_applicable (stated=3-5 mo (3-5 months); actual_months=None)
- sdc_2021: not_applicable (stated=24-36 mo (Honest re-rating horizon if a turnaround thesis ever emerged: 2-3 years); actual_months=None)
- ttcf_t2: not_applicable (stated=12-24 mo (12-24 months); actual_months=None)

## Per-point detail (section 2b three-column + clock)
### amd_2016_f1 — real: AMD @ 2016-02-29 ($2.14 adj); F1 per spec appendix (post FY2015 10-K, before T_parabola / THATIC)
- **Fleet reasoning:** buy (MEDIUM) — Buy a small, deliberately-sized stake (~1% of portfolio, the low end of the high-potential sleeve) as an asymmetric turnaround bet — the market is pricing this company as if its comeback product fails, so success is not in the price.
  - falsifiers: FALSIFIERS (any one kills the thesis): (a) the clean-sheet core slips beyond the stated 12-18 month launch window, or independent launch benchmarks show materially less than the claimed ~40% IPC uplift; (b) loss or material renegotiation of either console semi-custom contract, breaking the revenue floor; (c) liquidity
  - clock: THE CLOCK: next validation point is the next quarterly report (~3 months out) — watch cash burn and console-floor stability, not headline growth; the decisive test is the new-core launch in 12-18 months; honest re-rating horizon is 2-4 years, since even a successful launch needs one or two product generations to win se
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=4/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "A",
    "classification_rationale": "Real (non-synthetic) single-name entry decision at a clean freeze point → Category A. At 2016-02-29 the packet is a genuine contested turnaround: the bear case (FY2015 revenue $3,991M −27.5% YoY, −$660M net loss, −$412M stockholders' equity deficit, $785M liquidity against $2.26B debt and $160M interest expense) and the bull catalyst (clean-sheet Zen +40% IPC + SMT roadmap from the 2015-05-06 Financial Analyst Day; incumbent process-node cadence slip disclosed mid-2015) are BOTH fully disclosed by primary sources before the freeze. That makes it a well-posed, resolvable entry case rather than a value trap (B), a hold-through-drawdown (C), or a constructed scenario (synthetic). Grading = F1_lenient (matches case_id 'amd_2016_f1'): the test is the directional entry thesis on a multi-year turnaround, not precise entry price/date, so timing is graded leniently. Freeze date 2016-02-29 chosen because it is the first clean point after the FY2015 10-K (2016-02-18) with the full financial-distress-vs-catalyst evidence set on the table and no source postdating it. Classification and freeze point were selected without reference to the eventual outcome; latest source (2016-02-18) precedes the freeze.",
    "confidence": "MEDIUM",
    "freeze_date": "2016-02-29",
    "grading": "F1_lenient",
    "sourced_facts": [
      {
        "fact": "AMD FY2015 revenue $3,991M (-27.5% YoY), net loss -$660M; gross margin depressed by inventory write-downs; console semi-custom annuity at scale.",
        "publication_date": "2016-02-18",
        "url": "https://www.sec.gov/Archives/edgar/data/2488/000000248816000047/amd-123115x10k.htm"
      },
      {
        "fact": "FY2015 year-end: cash & cash equivalents + marketable securities $785M; total debt $2.26B; stockholders' equity deficit -$412M; FY2015 interest expense $160M.",
        "publication_date": "2016-02-18",
        "url": "https://www.sec.gov/Archives/edgar/data/2488/000000248816000047/amd-123115x10k.htm"
      },
      {
        "fact": "2015 Financial Analyst Day: clean-sheet Zen core roadmap claiming up to +40% IPC plus SMT; first competitive CPU attempt in a decade.",
        "publication_date": "2015-05-06",
        "url": "https://ir.amd.com/news-events/press-releases/detail/611/amd-outlines-sharpened-focus-at-2015-financial-analyst-day"
      },
      {
        "fact": "Incumbent process-node cadence slip disclosed mid-2015 (next node volume pushed; an intermediate 14nm refresh added), signaling a competitor stumble ahead of Zen.",
        "publication_date": "2015-07-16",
        "url": "https://www.anandtech.com/show/9447/intel-10nm-and-kaby-lake"
      }
    ]
  },
  "stage": 1,
  "verification": {
    "mismatches": [],
    "ok": true
  }
}
```
  - **sanitizer:**
```json
{
  "agent_role": "calibration_sanitizer",
  "model": "claude-opus-4-8",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "No forbidden term appears verbatim in the masked candidate_payload. Consoles rendered generically as 'BOTH major game consoles'; CEO as 'respected chip-engineer CEO' (not named); rival as 'dominant CPU incumbent'; new core as 'clean-sheet CPU core' (not 'Zen core'/'Ryzen'/'EPYC'). Company shown as 'Corvid Microsystems (alias CVM)'. No AMD/Intel/Lisa Su/Radeon/PlayStation/Xbox/GlobalFoundries/THATIC leak.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Factor 1.7 independently confirmed from source_manifest: revenue 3991*1.7=6784.7~6785; net loss 660*1.7=1122; cash 785*1.7=1334.5; total debt 2260*1.7=3842; equity deficit 412*1.7=700.4~-700; interest 160*1.7=272. Ratios (rev growth -27.5%) preserved unscaled and consistent. Note: share_price 6.1 and market_cap 2898 are absent from the manifest so cannot be independently proven, but every manifest-backed absolute scales cleanly by 1.7 and no raw absolute dollar figure leaks through unscaled.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload dates are all relative: 'FY-1/FY-2/FY-3', as_of 'current', '~2 weeks ago', CEO took over '~two years ago', '12-18 months from launch', incumbent slip 'in the prior year'. No absolute calendar years (2015/2016) from raw_sources leak into the masked payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Macro rendered generically: 'PC market shrinking mid-single-digits', 'server demand growing with cloud buildouts', 'semi cycle soft after an inventory correction'. The incumbent node event is genericized to 'process-node cadence slip' with the specific node number (10nm/14nm from raw) stripped.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "All four protocol dimensions pass. Non-synthetic packet (AMD-2016 masked as 'CVM'/'Corvid Microsystems'). No forbidden term leaks verbatim; consoles, CEO, and rival incumbent are properly genericized. Absolute-figure rescaling of 1.7 is independently proven from source_manifest across revenue, net loss, cash, total debt, equity deficit, and interest expense (share price and market cap are not in the manifest and thus not independently provable, but no raw absolute leaks through). Dates are fully relativized (FY-1/FY-2/FY-3, ~weeks/years ago) and macro events are genericized (node number stripped). safe_to_run=true."
  },
  "stage": 2,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **review:**
```json
{
  "agent_role": "calibration_reviewer",
  "model": "claude-opus-4-8",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "-27.5% revenue decline in rationale matches packet FY-1 revenue_growth_pct: -27.5",
      "$1.12B loss matches net_income_usd_m: -1122; $3.84B debt matches total_debt_usd_m: 3842; $1.33B cash matches cash_and_equivalents_usd_m: 1334.5",
      "Negative equity ($-700M) matches stockholders_equity_usd_m: -700; $272M interest matches annual_interest_expense_usd_m: 272",
      "0.43x P/S and 0.8x EV/S match valuation.ps_trailing: 0.43 and ev_s_trailing: 0.8",
      "GM 27% depressed / structural ~32% matches fundamentals note; ~40% IPC, 12-18mo launch, <1% server share, console floor all present in news/CVM recent_developments/business_description",
      "~1% sizing sits within constraints_rendered sleeve caps (5% total, 1-2% per position); stock-only honors 'No leverage, no options'",
      "FALSIFIERS (a-d) and THE CLOCK (next quarter ~3mo / launch 12-18mo / 2-4y re-rating band) present inside Recommendation, satisfying the REPORTING REQUIREMENT",
      "cited_sources fundamentals/CVM and news/CVM appear verbatim as report_name values in the packet analyst_reports",
      "output_audit contaminated=false with empty calendar_years/contamination_hits/suspect_terms; sanitizer output safe_to_run=true, leaked_terms=[]"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay audited against the persisted packet only. OUTPUT CLEAN: rationale contains no forbidden identifiers, no absolute calendar years, and all dates are relativized; output_audit shows contaminated=false with empty hit/suspect/year lists, and the Stage-2 sanitizer passed all four protocol checks (safe_to_run=true, leaked_terms=[]). PACKET FIDELITY: every hard numeric claim reconciles exactly to the packet — rev -27.5%, net loss -1122, debt 3842, cash 1334.5, equity -700, interest 272, P/S 0.43, EV/S 0.8, GM 27%→~32% structural — and all qualitative claims (40% IPC, 12-18mo launch, <1% server share, console floor, engineer-CEO, incumbent node slip) trace to news/CVM. CONSTRAINTS respected: ~1% sizing within the 1-2%/5% sleeve bands, stock-only (no leverage/options), masking/provenance handled correctly, confidence band reported (MEDIUM), and the mandated FALSIFIERS + THE CLOCK are both present inside the Recommendation section. Tax is honestly deferred with no fabricated rate (Rule 4). Both cited source ids appear verbatim in the packet — no hallucinated citations. WORKFLOW CORRECT. Grounding scored 4: fully grounded on packet facts with one minor non-blocking gap — the hedged general-market comparators ('healthy chip designers trade at several times these multiples', 're-rate several-fold') are uncited qualitative context, not packet-specific numeric claims. Investment correctness not graded; no hidden chain-of-thought claimed.",
    "violations": [
      "Minor (non-blocking): comparative valuation context 'healthy chip designers trade at several times these multiples' and 're-rate several-fold' are uncited general-market assertions rather than packet-grounded figures"
    ],
    "workflow_correct": true
  },
  "stage": 4,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **grading:**
```json
{
  "agent_role": "calibration_grader",
  "model": "claude-opus-4-8",
  "output": {
    "acted_return_pct": 5357.0,
    "benchmark_return_pct": 5357.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case amd_2016_f1 (category A, positioned=false) has expected_classes=[\"buy\"] under the F1_lenient rule. The fleet verdict action is \"buy\", a direct in-class match, so the base grade is 1.0. The 0.5 partial-credit paths do not apply here: this is a genuine winner (not a trap), so neither the 'small trap buy with the killing falsifier' nor the 'F1 falsifier-only pass' condition is triggered. The F1 reporting requirement is independently satisfied — the recommendation contains the mandated FALSIFIERS (a–d: core slip/IPC miss, console-contract loss, liquidity break, incumbent cadence restore) and THE CLOCK (next-quarter ~3mo checkpoint, 12–18mo launch, 2–4y re-rating band). The Stage-4 calibration reviewer graded output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=4/5 with only a minor non-blocking uncited-comparator note and no violations; per instruction I do not reopen that cleanliness/workflow judgment. Returns: benchmark = (116.78−2.14)/2.14 = 5357.0%, matching the supplied resolution; because the action was a buy, the acted-on return equals the full move (5357.0%). Outcome luck does not affect the class grade, but the resolution confirms the correct call. Confidence HIGH: deterministic in-class match plus a clean independent replay.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 5357.0,
    "expected_benchmark_return_pct": 5357.0,
    "expected_in_class": true,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 1.0 (in class); acted +5357% vs benchmark +5357%; source=calibration_grader
- notes: Case amd_2016_f1 (category A, positioned=false) has expected_classes=["buy"] under the F1_lenient rule. The fleet verdict action is "buy", a direct in-class match, so the base grade is 1.0. The 0.5 partial-credit paths do not apply here: this is a genuine winner (not a trap), so neither the 'small trap buy with the killing falsifier' nor the 'F1 falsifier-only pass' condition is triggered. The F1 reporting requirement is independently satisfied — the recommendation contains the mandated FALSIFIERS (a–d: core slip/IPC miss, console-contract loss, liquidity break, incumbent cadence restore) and THE CLOCK (next-quarter ~3mo checkpoint, 12–18mo launch, 2–4y re-rating band). The Stage-4 calibration reviewer graded output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=4/5 with only a minor non-blocking uncited-comparator note and no violations; per instruction I do not reopen that cleanliness/workflow judgment. Returns: benchmark = (116.78−2.14)/2.14 = 5357.0%, matching the supplied resolution; because the action was a buy, the acted-on return equals the full move (5357.0%). Outcome luck does not affect the class grade, but the resolution confirms the correct call. Confidence HIGH: deterministic in-class match plus a clean independent replay.

### boot_2017 — real: Boot Barn (BOOT) @ 2017-06-02 ($6.30); obscure western/work retailer during the store-based-retail-decline narrative; ~20x by mid-2024
- **Fleet reasoning:** buy (MEDIUM) — Buy a starter position at ~1% of the portfolio — the low end of the high-potential sleeve — as a contrarian bet that a durable niche retailer is being priced as a dying one.
  - falsifiers: FALSIFIERS — (1) existing-store sales come in NEGATIVE for two consecutive quarters, i.e. below the flat guide (demand-durability claim dies); (2) gross margin or exclusive-brand mix declines year-over-year, or the company shifts to discount-driven promotional selling (the pricing-power / full-price-discipline pillar d
  - clock: THE CLOCK — next validation point: the next quarterly earnings print, estimated ~3 months out (~October 2026), which tests the flat same-store-sales guide and the margin trajectory; expected re-rating horizon: an honest band of 2-4 years, since the market will demand several consecutive quarters of margin and same-stor
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=4/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "A",
    "classification_rationale": "Frozen reconstruction burn: frozen_taxonomy supplied, so category/grading/freeze_date are emitted verbatim (A / entry / 2017-06-02) and taxonomy is not re-litigated. The case is a trap-shaped entry (Category A hard-foresight winner) where the contemporaneous 2017-06-02 print reads soft: FY2017 adjusted NI/EPS fell ~20% YoY, Q4 consolidated SSS was -0.9%, Q4 sales (~$163M) missed the street, the stock sold off ~15% on the print, and the FY2018/Q1 guide was flat-to-slightly-positive SSS with roughly break-even Q1 EPS — explicitly NOT a re-acceleration print. All load-bearing earnings facts are verified against the primary SEC 8-K exhibit (press release dated 2017-06-01) and corroborated by live search (Q4 SSS -0.9%, net sales +9.1% to $163.0M; FY SSS +0.3%, +10.7% to $629.8M; EPS shortfall driven by soft retail, unanticipated opex, and sheplers.com platform transition). The print-day selloff magnitude and management's commodity/soft-retail commentary come from the contemporaneous OCBJ secondary source dated 2017-06-02. No source postdates the freeze date, no retrospective 'why it moved' articles were used, and the packet is not sanitized — the negatives (miss, deceleration, soft guide, selloff) are preserved as the setup. Confidence MEDIUM: earnings facts are primary-source and contemporaneously corroborated, but WebFetch to the SEC exhibit was permission-blocked (verified via search instead) and the stock-reaction magnitude rests on a single secondary source; recommend the domain-refresh agent confirm the OCBJ selloff figure and the SEC exhibit text directly.</classification_rationale>\n<parameter name=\"confidence\">MEDIUM",
    "confidence": "MEDIUM",
    "freeze_date": "2017-06-02",
    "grading": "entry",
    "sourced_facts": [
      {
        "fact": "Boot Barn FY2017 (fiscal year ended April 1, 2017): net sales $629.8M (+10.7%); consolidated same-store sales +0.3%; GAAP net income $14.2M / $0.53 EPS; adjusted net income $14.9M / $0.55 EPS (down ~20% vs FY2016 adjusted). Primary SEC 8-K earnings exhibit, press release dated 2017-06-01.",
        "publication_date": "2017-06-01",
        "url": "https://www.sec.gov/Archives/edgar/data/1610250/000110465917036899/a17-14427_1ex99d1.htm"
      },
      {
        "fact": "Q4 FY2017: consolidated same-store sales -0.9%; net sales +9.1% to $163.0M, missing street estimates; EPS shortfall attributed to lower-than-expected retail store sales, unanticipated operating expenses, and sheplers.com e-commerce platform-transition disruption. Print-day stock sold off ~15%; management cited low commodities prices in key markets and soft general retail.",
        "publication_date": "2017-06-02",
        "url": "https://www.ocbj.com/retail/apparel-retail/boot-barn-stock-dips-q4-results/"
      },
      {
        "fact": "FY2018 outlook issued with results: flat to slightly positive consolidated same-store sales; Q1 FY2018 guide roughly break-even EPS on flat SSS — not a re-acceleration print. Primary SEC 8-K earnings exhibit, press release dated 2017-06-01.",
        "publication_date": "2017-06-01",
        "url": "https://www.sec.gov/Archives/edgar/data/1610250/000110465917036899/a17-14427_1ex99d1.htm"
      }
    ]
  },
  "stage": 1,
  "verification": {
    "mismatches": [],
    "ok": true
  }
}
```
  - **sanitizer:**
```json
{
  "agent_role": "calibration_sanitizer",
  "model": "claude-opus-4-8",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "No forbidden_terms (Boot Barn, BOOT, Sheplers, Conroy, Country Outfitter, Wood's Boots) appear verbatim anywhere in candidate_payload. Company is masked as 'Prairie Outfitters (alias POT)'. Category descriptors (western/work footwear, cowboy boots, flame-resistant workwear, ~220 doors/~30 states) are generic sector language, not denylisted brand names.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Factor 2.1 independently reproduced from source_manifest: FY revenue 629.8*2.1=1322.6~=1323; Q4 revenue 163*2.1=342.3~=342; GAAP NI 14.2*2.1=29.8~=30; adj NI 14.9*2.1=31.3~=31. All masked absolutes differ from their raw values (no un-scaled leak), and multiples/growth (rev +10.7%, adj EPS -20%, SSS +0.3/-0.9%) match raw exactly.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Masked payload uses only relative/generic anchors ('current', 'FY-1 (just reported)', 'FY-2', 'next year', 'Q4/FY just reported'). The raw 2017-06 publication dates are not surfaced; no absolute year appears in the candidate_payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Raw source's specific driver ('low commodities prices in key markets + soft general retail') is genericized to 'Broad de-rating of store-based specialty retail; soft apparel traffic; energy/ag end-markets uneven' with no dated or named macro event that would timestamp/identify the vintage.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Non-synthetic packet (case boot_2017); all four checks applicable and passing. No forbidden term leaks in the masked payload. Absolute-figure rescaling independently verified from source_manifest at factor 2.1 (FY revenue 629.8->1323, Q4 163->342, NI 14.2->30, adj NI 14.9->31), with ratios/growth preserved and no raw absolute leaking through. Dates are genericized to relative FY labels with no 2017 anchor; macro context genericized to a sector de-rating narrative with no dated event. No company identification or outcome inference included. safe_to_run=true; leaked_terms empty."
  },
  "stage": 2,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **review:**
```json
{
  "agent_role": "calibration_reviewer",
  "model": "claude-opus-4-8",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Rationale '10.7% last year' = packet revenue_growth_pct: 10.7 (fundamentals/POT)",
      "Rationale 'adjusted EPS FELL ~20%' = packet note 'adj EPS down ~20% YoY' (fundamentals/POT)",
      "Rationale '+0.3% year / -0.9% latest quarter' = same_store_sales_pct 0.3 (FY-1) and -0.9 (latest_quarter)",
      "Rationale '$462M debt, $32M cash, $357M market value' = total_debt_usd_m 462, cash_and_equivalents_usd_m 32, market_cap_usd_m 357",
      "Rationale 'P/E 11.9 / 0.27x sales' = valuation.pe_trailing 11.9 and ps_trailing 0.27",
      "Rationale '~220 stores' = balance_sheet note 'store base ~220 doors across ~30 states'",
      "FALSIFIERS and THE CLOCK both present in Recommendation section per reporting requirement",
      "cited_sources ['fundamentals/POT','news/POT'] match packet report_name values verbatim"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay for case boot_2017 (masked candidate 'POT'). OUTPUT CLEAN: no forbidden brand terms or 2017-vintage leaks; the sole calendar year (2026) is the agent's operating present under system date 2026-07-12, which is expected behavior for a masked-vintage replay and matches output_audit contaminated=false — not a contamination. The agent honored the masking instruction (did not attempt de-anonymization, did not treat masking as a data defect). PACKET FIDELITY: every numeric claim in the rationale traces exactly to packet facts — revenue growth 10.7%, adj EPS -20%, SSS +0.3%/-0.9%, debt $462M / cash $32M / market cap $357M, P/E 11.9, P/S 0.27, ~220 stores. Cited only fundamentals/POT and news/POT, both present verbatim. Sizing (1.0% at low end of the 1-2% per-position sleeve band) matches the rendered constraints. Correctly declined to cite an unsupplied tax rate instead of fabricating one. WORKFLOW: the required Recommendation-section elements (FALSIFIERS with checkable conditions; THE CLOCK with next validation point ~next print and an honest 2-4y re-rating band) are both present. GROUNDING: rationale is fully grounded in packet facts for all factual assertions; the only soft gap is the upside scenario referencing 'historical retail norms' not in the payload, but it is clearly framed as hypothetical judgment, not fact. Investment correctness and expected verdict were not graded, per scope.",
    "violations": [],
    "workflow_correct": true
  },
  "stage": 4,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **grading:**
```json
{
  "agent_role": "calibration_grader",
  "model": "claude-opus-4-8",
  "output": {
    "acted_return_pct": 1855.0,
    "benchmark_return_pct": 1855.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case boot_2017 (Category A, entry grading). Expected_classes=[\"buy\"]; the fleet verdict action is \"buy\" (a ~1% starter position in the high-potential sleeve). That is an exact in-class match, so under the recorded entry rule (1.0 for an in-class verdict) the class grade is 1.0. No partial-credit condition applies: this is not an F1-with-falsifiers or a trap-buy-with-killing-falsifier case — it is a clean in-class buy. The independent replay review (stage 4) found output_clean, packet_fidelity, and workflow_correct all true with no violations, and both required Recommendation elements (FALSIFIERS and THE CLOCK) present; per scope I do not reopen that cleanliness/workflow judgment. Acted-on return: the buy participates fully in the resolved move from price_at_freeze_real 6.30 to price_at_horizon_real 123.15 over the 7y horizon = (123.15-6.30)/6.30 = 1854.8% ≈ 1855%, matching the recorded benchmark_return_pct of 1855. Outcome luck (the large realized gain) does not alter the class grade — the score reflects that the verdict fell in the expected class, which it did regardless of the favorable resolution. Confidence HIGH: verdict class and resolution values are both fully specified in the supplied input.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 1855,
    "expected_benchmark_return_pct": 1855,
    "expected_in_class": true,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 1.0 (in class); acted +1855% vs benchmark +1855%; source=calibration_grader
- notes: Case boot_2017 (Category A, entry grading). Expected_classes=["buy"]; the fleet verdict action is "buy" (a ~1% starter position in the high-potential sleeve). That is an exact in-class match, so under the recorded entry rule (1.0 for an in-class verdict) the class grade is 1.0. No partial-credit condition applies: this is not an F1-with-falsifiers or a trap-buy-with-killing-falsifier case — it is a clean in-class buy. The independent replay review (stage 4) found output_clean, packet_fidelity, and workflow_correct all true with no violations, and both required Recommendation elements (FALSIFIERS and THE CLOCK) present; per scope I do not reopen that cleanliness/workflow judgment. Acted-on return: the buy participates fully in the resolved move from price_at_freeze_real 6.30 to price_at_horizon_real 123.15 over the 7y horizon = (123.15-6.30)/6.30 = 1854.8% ≈ 1855%, matching the recorded benchmark_return_pct of 1855. Outcome luck (the large realized gain) does not alter the class grade — the score reflects that the verdict fell in the expected class, which it did regardless of the favorable resolution. Confidence HIGH: verdict class and resolution values are both fully specified in the supplied input.

### cvna_2023_reentry — real: CVNA @ 2023-08-01 ($49.11 as-traded); re-entry after 2023-07-19 debt exchange + record GPU/Adj EBITDA print
- **Fleet reasoning:** buy (MEDIUM) — Buy a small, high-potential-sleeve position (~1% of portfolio, the cautious end of the 1-2% band) in this online used-vehicle retailer — a classic asymmetric turnaround where the survival risk that crushed the stock has just been removed, but the market price still reflects much of the old fear.
  - falsifiers: FALSIFIERS — any of these kills the thesis: (1) Adjusted EBITDA turns negative again or gross profit per unit falls materially from this quarter's record in either of the next two quarterly reports; (2) the noteholder exchange fails to close on the announced >90%-support terms; (3) a large, dilutive emergency equity ra
  - clock: THE CLOCK — next validation point: the next quarterly earnings print, estimated ~2-3 months out (management has guided another positive Adjusted EBITDA quarter; a miss is an early falsifier). Full re-rating horizon: honestly 2-3 years — the thesis needs the exchange to close, two-plus quarters of sustained per-unit pro
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=4/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "A",
    "classification_rationale": "Genuine historical freeze point (real ticker CVNA, real SEC/IR filings), so not 'synthetic'. The case_id 'cvna_2023_reentry' and the packet describe a fallen-angel/distressed name at a turnaround inflection: Q2-2023 posted first-ever record total GPU ($6,520, +94% YoY) and record positive Adjusted EBITDA (+$155M) alongside a debt-restructuring support agreement that cuts near-term maturities, cash interest, and total debt — but with still-negative GAAP net income (-$105M), declining volumes/revenue (units -35%, revenue -24% YoY), thin cash ($677M) and heavy leverage. That structure is a two-sided NEW/RE-ENTRY decision (bull turnaround thesis vs. falling-knife/leverage-trap risk), not an exit, hold-through-drawdown, or thesis re-derivation of an existing position — hence Category A, grading 'entry'. Classification chosen on setup structure only; the eventual post-freeze price path was not used. Packet left un-sanitized (both the record-metrics bull case and the loss/leverage/coercive-exchange bear case retained). All four manifest sources are primary (SEC EDGAR + Carvana investor relations), published 2023-07-19, which is <= the 2023-08-01 freeze date, so no source postdates the freeze. Confidence held at MEDIUM: I could not independently re-fetch the live pages, and the load-bearing freeze-date market price/valuation for an entry decision is absent from the manifest.",
    "confidence": "MEDIUM",
    "freeze_date": "2023-08-01",
    "grading": "entry",
    "sourced_facts": [
      {
        "fact": "Q2-2023: retail units sold 76,530 (-35% YoY); total revenue $2.968B (-24% YoY); total GPU $6,520 (+94% YoY, company record); Adjusted EBITDA +$155M (company record positive); GAAP net loss -$105M.",
        "publication_date": "2023-07-19",
        "url": "https://www.sec.gov/Archives/edgar/data/1690820/000169082023000218/ex99_1q22023.htm"
      },
      {
        "fact": "Transaction support agreement with holders of >90% of senior unsecured notes: eliminates >83% of 2025/2027 unsecured note maturities, reduces cash interest expense by >$430M/yr for two years, and cuts total debt by >$1.2B; new notes secured by operating and auction-network assets.",
        "publication_date": "2023-07-19",
        "url": "https://investors.carvana.com/news-releases/2023/07-19-2023-113111747"
      },
      {
        "fact": "Cash, cash equivalents and restricted cash of $677M as of 2023-06-30.",
        "publication_date": "2023-07-19",
        "url": "https://www.sec.gov/Archives/edgar/data/1690820/000169082023000219/cvna-20230630.htm"
      },
      {
        "fact": "Management guides to another quarter of positive Adjusted EBITDA and frames the strategy as profitability-first, then return-to-growth.",
        "publication_date": "2023-07-19",
        "url": "https://investors.carvana.com/news-releases/2023/07-19-2023-113026425"
      }
    ]
  },
  "stage": 1,
  "verification": {
    "mismatches": [],
    "ok": true
  }
}
```
  - **sanitizer:**
```json
{
  "agent_role": "calibration_sanitizer",
  "model": "claude-opus-4-8",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Masked payload consistently uses ticker 'APO' and 'AutoPort Online (alias APO)'. Full scan of candidate_payload (fundamentals, news, constraints, positions) contains none of the forbidden real-identity terms (Carvana/CVNA/Garcia/ADESA/vending machine/Apollo/Ernie). The acquired auction business is described only as a 'physical wholesale-auction network', founder referred to generically as 'founder-controlled'.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently verified from source_manifest/raw_sources at declared factor 1.15: revenue 2968->3413 (x1.150), total GPU 6520->7498 (x1.150), Adj EBITDA 155->178 (x1.148), GAAP loss 105->121 (x1.152), cash 677->779 (x1.151), debt reduction >1200->>1380 (x1.15), cash interest >430/yr->>494/yr (x1.149). All absolute USD figures scaled consistently; no raw absolute value leaks through. Ratios/growth rates (-24% rev, -35% units, 94% GPU) preserved. Note: retail units 76,530->75,840 are a non-dollar count, perturbed rather than scaled, and do not expose the raw figure.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Raw source date 2023-07-19 is expressed relatively in the masked payload ('Q2, reported ~2 weeks ago', as_of 'current', 'Two years ago', 'a year ago'), and fiscal years use relative FY-1/FY-2 labels. The explicit '2025/2027' maturity years from raw_sources are genericized to 'near-term maturity wall'/'removes the near-term maturity wall'. No absolute calendar year leaks.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The pandemic-era used-vehicle price spike is genericized in macro_context to 'Used-vehicle prices off the shock-era peak' and 'physical wholesale-auction network acquired at the prior cycle peak' — no named macro event (COVID/pandemic/specific year) appears.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Masked payload passes all four protocol checks. No forbidden_terms leak into the candidate the Trader would see; company/ticker/auction-network/founder are all aliased or genericized. Absolute-figure rescaling independently proven from the frozen source_manifest at factor 1.15 across every USD figure (revenue, GPU, EBITDA, net loss, cash, debt reduction, cash interest), with ratios and growth rates preserved. Source publication date 2023-07-19 and 2025/2027 maturity years are relativized/genericized; the pandemic macro spike is abstracted to 'shock-era peak'. Real identity not inferred and outcome not disclosed. safe_to_run=true, HIGH confidence.\""
  },
  "stage": 2,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **review:**
```json
{
  "agent_role": "calibration_reviewer",
  "model": "claude-opus-4-8",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Relative-time-only rationale ('two-week-old', '~2-3 months out', '2-3 years', '18 months out') consistent with output_audit.calendar_years_in_output=[] and contaminated=false",
      "No forbidden identity terms in output; firm described generically as 'online used-vehicle retailer' with 'physical wholesale-auction network'",
      "129% growth -> packet FY-2 revenue_growth_pct 129.4; $3.3B loss -> FY-1 net_income_usd_m -3328",
      "Latest_quarter matches: -35% units, GPU 7498, Adj EBITDA 178, revenue -24%, GAAP net income -121",
      "Valuation matches: ps_trailing 0.68, ev_s_trailing 1.01, price_action ~-90% off peak",
      "Post-exchange debt ~$4.6B derived from total_debt 5980 minus >$1380M cut; cash 779 from balance_sheet",
      ">90% noteholders, >$1.38B debt, >$494M/yr interest relief all present in news recent_developments/balance_sheet note",
      "Sizing ~1% within stated 1-2% per-position band, under ~5% sleeve cap per constraints_rendered",
      "FALSIFIERS (5 conditions) and THE CLOCK (next print ~2-3 months; re-rating 2-3 years) present per REPORTING REQUIREMENT",
      "cited_sources 'fundamentals/APO' and 'news/APO' match packet report_name fields verbatim"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "The persisted trader output is clean, faithful, and workflow-correct. Cleanliness: rationale uses only relative time anchors (no calendar years leak; matches output_audit contaminated=false), and no forbidden identity terms appear — the firm is described generically as an online used-vehicle retailer. Absolute dollars quoted are the scaled/masked values (explicitly flagged as masked), so no raw figure is exposed. Packet fidelity: spot-checked numerics all tie to packet fields — 129.4% FY-2 growth, -$3328M FY-1 loss, -35% units, GPU 7498, Adj EBITDA +178, -24% revenue, GAAP -$121M, 0.68x P/S, 1.01x EV/S, ~-90% off peak, cash $779M, and derived post-exchange debt ~$4.6B (5980-1380). Constraint fidelity: sizing ~1% within the 1-2% per-position band and under the ~5% sleeve cap; FALSIFIERS and THE CLOCK (next print ~2-3 months, re-rating 2-3 years) both reported as required; masked-name protocol respected; no leverage/options. Workflow: buy/market/GTC, size 1.0 currency=1%, MEDIUM confidence justified by masked payload plus absent adversarial debate; cited_sources (fundamentals/APO, news/APO) are verbatim packet report_names. Visible rationale is fully grounded: every numeric claim maps to a packet field or a transparent derivation, and forward-looking bands are labeled as reasoning rather than asserted fact. Sanitizer stage-2 record (factor-1.15 rescale, alias/date/macro genericization) passed independently. Not graded: investment correctness or expected outcome.",
    "violations": [],
    "workflow_correct": true
  },
  "stage": 4,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **grading:**
```json
{
  "agent_role": "calibration_grader",
  "model": "claude-opus-4-8",
  "output": {
    "acted_return_pct": 648.9,
    "benchmark_return_pct": 648.9,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case cvna_2023_reentry (category A, entry grade) has expected_classes=[\"buy\"]; the fleet verdict was action=\"buy\" (small ~1% high-potential-sleeve position), which is squarely in-class → 1.0 per the rule (in-class = 1.0). No partial-credit condition applies: this is a clean buy matching the expected class, not an F1 pass-with-falsifiers or a trap buy needing the killing falsifier, so the 0.5 tier is not invoked. The independent replay review (stage 4) rated the output clean, packet-faithful, and workflow-correct with no violations; per instructions I do not reopen that cleanliness/workflow judgment. Acted-on return: the fleet bought, so it captured the benchmark path — freeze 49.11 → horizon 367.78 = (367.78−49.11)/49.11 = +648.9%, equal to the recorded benchmark_return_pct of 648.9%. Outcome luck (the large realized gain) does not alter the class grade; the score reflects correct classification, which is independently 1.0.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 648.9,
    "expected_benchmark_return_pct": 648.9,
    "expected_in_class": true,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 1.0 (in class); acted +649% vs benchmark +649%; source=calibration_grader
- notes: Case cvna_2023_reentry (category A, entry grade) has expected_classes=["buy"]; the fleet verdict was action="buy" (small ~1% high-potential-sleeve position), which is squarely in-class → 1.0 per the rule (in-class = 1.0). No partial-credit condition applies: this is a clean buy matching the expected class, not an F1 pass-with-falsifiers or a trap buy needing the killing falsifier, so the 0.5 tier is not invoked. The independent replay review (stage 4) rated the output clean, packet-faithful, and workflow-correct with no violations; per instructions I do not reopen that cleanliness/workflow judgment. Acted-on return: the fleet bought, so it captured the benchmark path — freeze 49.11 → horizon 367.78 = (367.78−49.11)/49.11 = +648.9%, equal to the recorded benchmark_return_pct of 648.9%. Outcome luck (the large realized gain) does not alter the class grade; the score reflects correct classification, which is independently 1.0.

### hyln_2021 — real: Hyliion (HYLN) @ 2021-06-01 ($10.61); obscure Class-8 e-powertrain blank-check-merger winner-shape with $0 revenue; -86% by mid-2024
- **Fleet reasoning:** hold (MEDIUM) — Do not initiate a position in VHS — the company has zero sales, an unproven product, and a price that already assumes years of successful execution; the evidence needed to justify owning it arrives within the next two quarters, so wait for it.
  - falsifiers: FALSIFIERS (conditions that would flip this to a buy-review): (1) first recognized revenue in H2 from genuine third-party fleet customers — not demo installs — at unit economics consistent with guided gross margins; (2) the year-end flagship long-haul architecture demo converting into binding, dated fleet purchase orde
  - clock: THE CLOCK: next validation point is the H2 revenue print — the Q3 quarterly filing, estimated 3-5 months out — followed by the year-end flagship demos; the honest re-rating horizon is 6-18 months, because commercial proof (or failure) will emerge across the next two to four quarters, not immediately. **Sources:** fund
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=4/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "B",
    "classification_rationale": "Real, verifiable ticker (HYLN) with a genuine SEC/filing trail, so this is not a synthetic packet. At the 2021-06-01 freeze the evidence presents a classic trap structure: an attractive surface narrative (large ~$632M cash + short/long-term investments war chest, an exciting hybrid/EV powertrain story, guidance for 'first Hybrid revenue in H2,' Hypertruck ERX demo units 'toward year-end,' and ten powertrains installed in Q1) layered over absent fundamentals ($0 revenue, -$16.6M net loss, cash burn from $9.3M R&D and $7.4M SG&A, plus a 2020 restatement for blank-check warrant accounting and repeatedly-deferred product/revenue milestones). No existing position is specified, so this is an entry decision whose disciplined, period-accurate answer is 'avoid' — the packet is engineered to lure a story/cash-rich buy while the falsifiable fundamentals do not support it. Grading = trap was selected purely from the pre-freeze setup; HYLN's later decline was deliberately NOT used to pick the category or freeze point. All three cited facts predate the freeze (2021-05-11, 2021-05-17, 2021-05-19); no source postdates 2021-06-01. Category letter B (real-ticker hype/story-trap archetype) is inferred from the taxonomy structure — hence MEDIUM confidence on the letter — while the trap grading and the underlying facts are primary-sourced and high-confidence. Exact share price at freeze was not in the manifest and could not be re-sourced under current tool permissions, but it is not load-bearing for the archetype; a domain-refresh pass could add it for packet completeness.",
    "confidence": "MEDIUM",
    "freeze_date": "2021-06-01",
    "grading": "trap",
    "sourced_facts": [
      {
        "fact": "Q1-2021: $0 revenue; net loss -$16.6M; R&D $9.3M; SG&A $7.4M; cash and cash equivalents $335M plus short- and long-term investments (~$297M combined)",
        "publication_date": "2021-05-17",
        "url": "https://www.sec.gov/Archives/edgar/data/1759631/000162828021010423/hyln-20210331.htm"
      },
      {
        "fact": "Business update: expects first Hybrid revenue in H2; Hypertruck ERX demo units targeted for fleets toward year-end; ten Hybrid powertrains installed in Q1 (pre-revenue deployments)",
        "publication_date": "2021-05-11",
        "url": "https://investors.hyliion.com/news/news-details/2021/Hyliion-Reports-First-Quarter-2021-Business-Updates/default.aspx"
      },
      {
        "fact": "Company restating 2020 financials for blank-check warrant accounting (same class of restatement affecting ~80 blank-check vehicles); delayed detailed financial commentary until amended filings",
        "publication_date": "2021-05-19",
        "url": "https://www.ttnews.com/articles/hyliion-q1-results-lack-revenue-product-development-accelerates"
      }
    ]
  },
  "stage": 1,
  "verification": {
    "mismatches": [],
    "ok": true
  }
}
```
  - **sanitizer:**
```json
{
  "agent_role": "calibration_sanitizer",
  "model": "claude-opus-4-8",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "No forbidden term appears in the masked candidate_payload. Company rendered as 'VoltHaul Systems (alias VHS)'; 'Hypertruck' → 'flagship long-haul architecture'; founder name ('Healy') → 'Charismatic founder-CEO'; SPAC sponsor ('Tortoise Acquisition') → 'blank-check merger'; OEM ('Peterbilt') → generic 'OEM dependency'. Ticker HYLN → VHS. Forbidden URLs/names appear only in raw_sources/source_manifest (sanitizer reference), not in the payload the Trader sees.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "rescale_factor 0.85 independently confirmed from raw_sources across five figures: net loss -16.6*0.85=-14.11→-14; R&D 9.3*0.85=7.905→8; SG&A 7.4*0.85=6.29→6; cash 335*0.85=284.75→285; investments ~297*0.85=252.45→252. All consistent; no payload figure equals an unscaled raw value. Market cap (1530), share price (9.02), and FY-1 annual (-60) are absent from the manifest and thus not independently verifiable, but none leaks a raw absolute value.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload uses only relative/genericized dates ('10-Q filed ~2 weeks ago', 'Went public ... months ago', 'first Hybrid revenue in the back half of the year', 'year-end demos', as_of='current'). The absolute 2021-05 dates exist only in raw_sources/source_manifest, not in the candidate payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Raw source's specific SPAC warrant-accounting restatement wave ('~80 blank-check vehicles') is genericized in the payload to 'industry-wide issue' and 'blank-check complex starting to re-rate as warrant accounting and lockups hit'; the specific ~80 count and 2021 timing are dropped. Freight-cycle and ESG/decarbonization framing kept generic (undated).",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Real (non-synthetic) hyln_2021 packet. All four protocol dimensions pass: no forbidden term (Hyliion/HYLN/Hypertruck/Healy/Tortoise Acquisition/Peterbilt) leaks into the masked payload; the 0.85 rescale factor is independently proven from raw_sources across five figures (net loss, R&D, SG&A, cash, investments) with no unscaled value present; dates are relative/genericized; and the SPAC warrant-restatement macro event is genericized to an industry-wide issue. Market cap, share price, and FY-1 annual figures are not in the manifest so cannot be independently verified, but they leak no raw absolute value. Safe to run."
  },
  "stage": 2,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **review:**
```json
{
  "agent_role": "calibration_reviewer",
  "model": "claude-opus-4-8",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Cash+investments 285+252=537 matches rationale '$537M in cash and investments'",
      "Enterprise-ish value 1530-537=993 matches 'roughly $990M that buyers are paying for the business'",
      "Implied shares 1530/9.02≈169.6M; net cash/share 537/169.6≈$3.17 matches '~$3 per scaled share equivalent'",
      "FY-1 net_income_usd_m -60 and latest_quarter gaap_net_income_usd_m -14 match 'lost $60M last fiscal year and $14M last quarter'",
      "total_debt_usd_m 0 matches 'zero debt'; ten Hybrid units and H2 revenue guidance match news/VHS recent_developments",
      "cited_sources ['fundamentals/VHS','news/VHS'] match packet report_names verbatim",
      "FALSIFIERS and THE CLOCK explicitly present in Recommendation section per constraints_rendered reporting requirement",
      "output_audit: contaminated=false, calendar_years_in_output empty, no suspect terms"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay audit of hyln_2021 (alias VHS) passes on all four dimensions. Output cleanliness: output_audit confirms no calendar-year or alias contamination; rationale uses only relative dates (H2, this year, next two quarters, 3-5 months out) and no forbidden terms leak. Packet fidelity: all numeric claims reconcile to packet facts — cash+investments 285+252=537; business value 1530-537≈990; net cash/share = 537/(1530/9.02≈169.6M)≈$3; FY-1 loss -60, latest-quarter -14, zero debt, ten hybrid installs, H2 revenue guidance, year-end demos all match. Constraint fidelity: the fleet REPORTING REQUIREMENT is fully satisfied — explicit FALSIFIERS (flip and kill conditions) and THE CLOCK (Q3 H2 revenue print ~3-5 months, honest 6-18 month re-rating band) are recorded in the Recommendation section; masking treated as intentional; sleeve sizing acknowledged; MEDIUM confidence justified by single-payload provenance. Cited sources match packet report_names exactly. Workflow correct: hold / no-initiate, size 0, cash_delta $0, no tax event — internally consistent. Rationale is fully grounded with correct derived arithmetic; no unsupported claims. Investment correctness of the hold verdict was not graded, per scope.",
    "violations": [],
    "workflow_correct": true
  },
  "stage": 4,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **grading:**
```json
{
  "agent_role": "calibration_grader",
  "model": "claude-opus-4-8",
  "output": {
    "acted_return_pct": 0.0,
    "benchmark_return_pct": -86.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case hyln_2021 is a category-B trap (aliased VHS): a winner-shaped blank-check-merger narrative that never commercialized, resolving -86% from 10.61 to 1.48 over the 3y horizon. Expected_classes = [\"hold\"]; the fleet verdict is \"hold\" (Do not initiate, size 0.0), which lands squarely in the expected class, so in_expected_class = true. The benchmark rule reserves 0.5 for partial-credit shapes such as an F1 pass with falsifiers or a small trap BUY that carries the killing falsifier; this verdict is a clean no-initiate hold, not a partial trap purchase, so no discount applies and the score is 1.0. The independent replay review (stage 4) passed all four dimensions — output_clean, packet_fidelity, workflow_correct, reasoning_grounded_score 4, zero violations — and I do not reopen that cleanliness/workflow judgment. Acted-on return is computed from the recorded action: size 0.0 / not positioned means no capital was exposed, so acted_return_pct = 0.0% versus the benchmark_return_pct of -86%. The fleet correctly avoided the loss; per the rule, outcome luck does not alter the in-class grade, but here the in-class verdict and the favorable avoidance coincide.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 0.0,
    "expected_benchmark_return_pct": -86,
    "expected_in_class": true,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 1.0 (in class); acted +0% vs benchmark -86%; source=calibration_grader
- notes: Case hyln_2021 is a category-B trap (aliased VHS): a winner-shaped blank-check-merger narrative that never commercialized, resolving -86% from 10.61 to 1.48 over the 3y horizon. Expected_classes = ["hold"]; the fleet verdict is "hold" (Do not initiate, size 0.0), which lands squarely in the expected class, so in_expected_class = true. The benchmark rule reserves 0.5 for partial-credit shapes such as an F1 pass with falsifiers or a small trap BUY that carries the killing falsifier; this verdict is a clean no-initiate hold, not a partial trap purchase, so no discount applies and the score is 1.0. The independent replay review (stage 4) passed all four dimensions — output_clean, packet_fidelity, workflow_correct, reasoning_grounded_score 4, zero violations — and I do not reopen that cleanliness/workflow judgment. Acted-on return is computed from the recorded action: size 0.0 / not positioned means no capital was exposed, so acted_return_pct = 0.0% versus the benchmark_return_pct of -86%. The fleet correctly avoided the loss; per the rule, outcome luck does not alter the in-class grade, but here the in-class verdict and the favorable avoidance coincide.

### sdc_2021 — real: SmileDirectClub (SDC) @ 2021-11-15 ($4.05); obscure DTC-aligner trap after Q3 demand cliff; Chapter 11 2023-09
- **Fleet reasoning:** hold (HIGH) — Do not initiate a position — this is a shrinking, loss-making business with broken customer economics, not an asymmetric growth candidate.
  - falsifiers: FALSIFIERS (conditions that would force a fresh look): (1) two consecutive quarters of positive year-over-year revenue growth achieved with marketing spend below ~40% of revenue — proof the acquisition math works without buying growth; (2) a demonstrated GAAP or free-cash-flow breakeven quarter without one-off gains; (
  - clock: THE CLOCK: the next validation point is the Q4 earnings print, estimated ~3 months out (management just guided that quarter down, so expectations are low — a miss even against lowered guidance would confirm the demand cliff). Honest re-rating horizon if a turnaround thesis ever emerged: 2-3 years minimum, since fixing
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=4/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "B",
    "classification_rationale": "SmileDirectClub at the 2021-11-15 freeze is a real single-name equity (not synthetic) sitting at a genuine historical inflection: the 2021-11-08 Q3 print and guidance cut. Pre-freeze evidence describes a former high-growth IPO name whose thesis is actively breaking — revenue contracting 18.3% YoY to $137.7M, FY guide slashed to $630-650M from a prior $750-800M, Q4 guided $120-140M vs ~$202M street, marketing/selling ~70% of revenue funding widening losses (-$89M Q3, -$240M YTD), and management attributing the miss to demand-side macro pressure on its core demographic plus ad-attribution breakage. The post-earnings crash creates an optically cheap 'buy-the-dip' entry, but the fundamentals signal broken unit economics and structural demand erosion. This is the classic value-trap / falling-knife setup, so the correct grading key is 'trap' (avoid the tempting entry). Classification and freeze selection rely only on the contemporaneous 2021-11-08 disclosures, not on the eventual 2023 bankruptcy outcome or any retrospective post-mortem. The freeze date sits ~5 trading days after the earnings/guidance cut, giving the market time to digest the disclosed information while staying strictly pre-outcome.",
    "confidence": "MEDIUM",
    "freeze_date": "2021-11-15",
    "grading": "trap",
    "sourced_facts": [
      {
        "fact": "Q3-2021 total revenue $137.7M, down 18.3% YoY from $168.5M; net loss -$89M; marketing and selling ~$96M (~70% of revenue); initial aligner shipments ~70k, with the decline driven by lower aligner shipments amid negative macro factors on the core demographic.",
        "publication_date": "2021-11-08",
        "url": "https://www.sec.gov/Archives/edgar/data/1775625/000177562521000075/sdcearningsrelease-09302021.htm"
      },
      {
        "fact": "FY2021 revenue guidance cut to $630-650M (from prior $750-800M); Q4-2021 guided to $120-140M versus consensus of ~$202M; management cited macro headwinds to the core demographic plus digital-ad attribution breakage.",
        "publication_date": "2021-11-08",
        "url": "https://www.globenewswire.com/news-release/2021/11/08/2329688/0/en/SmileDirectClub-Reports-Third-Quarter-2021-Financial-Results.html"
      },
      {
        "fact": "YTD nine-month revenue ~$511M (+8.3%) masks sequential and YoY quarterly contraction; cumulative YTD net loss approximately -$240M.",
        "publication_date": "2021-11-08",
        "url": "https://www.sec.gov/Archives/edgar/data/1775625/000177562521000075/sdcearningsrelease-09302021.htm"
      }
    ]
  },
  "stage": 1,
  "verification": {
    "mismatches": [],
    "ok": true
  }
}
```
  - **sanitizer:**
```json
{
  "agent_role": "calibration_sanitizer",
  "model": "claude-opus-4-8",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "No forbidden term (SmileDirectClub, Smile Direct, SDC, SDCCQ, Katzman, Invisalign, Align Technology, SmileShop) appears verbatim in candidate_payload. Genericizations hold: 'retail kiosks' for SmileShop, 'Founder-family controls the vote' for named founder, 'chairside incumbent' not named. case_id 'sdc_2021' is metadata outside the payload.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently verified factor 1.4 from source_manifest: Q3 rev $138M*1.4=193.2->193; net loss -$89M*1.4=124.6->-125; marketing $96M*1.4=134.4->134. Ratios (-18.3% YoY, ~70% mktg/rev) preserved. No masked absolute equals its raw value. Market cap/cash/debt/price not in manifest, so those specific values are not independently provable, but the scaling constant is confirmed on all overlapping figures.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload uses relative framing ('current', 'FY-2'/'FY-1', 'reported ~1 week ago', 'IPO-era peak'); no absolute source year (2021) or dated event leaks despite raw source date 2021-11-08.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Macro context genericized to 'digital advertising attribution disrupted industry-wide' and 'consumer discretionary soft for lower-income cohorts'; the datable ad-attribution event (raw: 'digital-ad attribution breakage') is not named or pinned to a specific platform/date.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Masked payload for alias CBO is clean across all four dimensions. No forbidden term leaks verbatim; genericizations for retail footprint, founder, and incumbent are intact. Rescale constant 1.4 is independently confirmed from the source_manifest on three overlapping absolute figures (Q3 revenue 138->193, net loss 89->125, marketing 96->134) with ratios preserved and no un-rescaled raw value leaking. Dates are relative-only, and the macro ad-attribution event is genericized without pinning. Non-synthetic packet, all checks applicable. Safe to run."
  },
  "stage": 2,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **review:**
```json
{
  "agent_role": "calibration_reviewer",
  "model": "claude-opus-4-8",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Numeric claims match packet: 71% gross margin, marketing 134/193 (~70%), FY-1 -12.5%, Q3 -18.3% YoY, net loss -125, cash 308 vs debt 1050, derived ~740 gap = 1050-308, P/S 2.5, -80% price action.",
      "Qualitative claims sourced from news report: never-GAAP-profitable, founder-family vote control, incumbent competitive response, regulatory/clinical pushback, platform-optionality framing.",
      "Constraint reporting requirement satisfied: Recommendation section includes explicit FALSIFIERS list and THE CLOCK (next validation = Q4 print ~3 months out; honest 2-3 year re-rating horizon band).",
      "Citations fundamentals/CBO and news/CBO match packet report_name fields verbatim; output_audit shows contaminated=false with no calendar years."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay audit of case sdc_2021 (alias CBO), pass on all four review dimensions. OUTPUT CLEAN: no forbidden identity terms leak in output_full; genericizations intact; output_audit confirms zero calendar years and contaminated=false, consistent with my independent read (rationale uses relative-only temporal framing). PACKET FIDELITY: every numeric claim traces exactly to packet facts — 71% gross margin, ~70% marketing/revenue (134/193), FY-1 -12.5% and Q3 -18.3% revenue, -$125M net loss, $308M cash vs $1,050M debt, ~$740M gap (1050-308=742), 2.5x P/S, -80% price action, founder vote control, never-GAAP-profitable, incumbent/regulatory risks. Only derived figure ($740M) is valid arithmetic on packet values; no fabricated numbers. Citations fundamentals/CBO and news/CBO match report_name fields verbatim; no domain_knowledge citation needed since there is no transaction/tax claim. WORKFLOW CORRECT: action=hold with size 0 and coherent expected_impact; the mandated Recommendation section contains both FALSIFIERS (three checkable conditions) and THE CLOCK (Q4 print ~3 months out plus an honest 2-3 year re-rating band). REASONING GROUNDED (4): visible rationale is fully anchored to packet facts step by step, with only a self-defined ~40% falsifier threshold appropriately framed as an analytical condition rather than a packet datum. I graded only the written rationale, not investment correctness, and did not access any hidden reasoning.",
    "violations": [],
    "workflow_correct": true
  },
  "stage": 4,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **grading:**
```json
{
  "agent_role": "calibration_grader",
  "model": "claude-opus-4-8",
  "output": {
    "acted_return_pct": 0.0,
    "benchmark_return_pct": -100.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case sdc_2021 is a category-B trap. Expected class is [\"hold\"] (do-not-initiate / pass). The fleet verdict was action=hold with size 0.0 shares — squarely in-class, so full 1.0 credit. The 0.5 partial-credit path (a small trap buy that nonetheless names the killing falsifier) does not apply here because no position was taken; this is a clean pass, not a mitigated buy. Acted-on return: with size 0 the fleet initiated no exposure, so the acted return is 0.0% versus the benchmark ride-to-zero of -100% (SDCCQ $4.05 at freeze → $0.00 at Chapter 11 liquidation, per resolution fetched 2026-07-11) — the trap was fully avoided. The rationale correctly diagnosed broken unit economics (71% gross margin consumed by ~70% marketing/revenue), shrinking demand (-12.5% FY, -18.3% Q3), solvency overhang ($308M cash vs $1,050M debt), and anchored fair value to liquidation math, with explicit falsifiers and a clock — consistent with the passing Stage-4 replay review, which I do not reopen. Outcome luck does not affect the class grade, but the ride-to-zero resolution confirms the pass was substantively right. Confidence HIGH: in-class match is unambiguous and the resolution is a primary benchmark value.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 0.0,
    "expected_benchmark_return_pct": -100,
    "expected_in_class": true,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 1.0 (in class); acted +0% vs benchmark -100%; source=calibration_grader
- notes: Case sdc_2021 is a category-B trap. Expected class is ["hold"] (do-not-initiate / pass). The fleet verdict was action=hold with size 0.0 shares — squarely in-class, so full 1.0 credit. The 0.5 partial-credit path (a small trap buy that nonetheless names the killing falsifier) does not apply here because no position was taken; this is a clean pass, not a mitigated buy. Acted-on return: with size 0 the fleet initiated no exposure, so the acted return is 0.0% versus the benchmark ride-to-zero of -100% (SDCCQ $4.05 at freeze → $0.00 at Chapter 11 liquidation, per resolution fetched 2026-07-11) — the trap was fully avoided. The rationale correctly diagnosed broken unit economics (71% gross margin consumed by ~70% marketing/revenue), shrinking demand (-12.5% FY, -18.3% Q3), solvency overhang ($308M cash vs $1,050M debt), and anchored fair value to liquidation math, with explicit falsifiers and a clock — consistent with the passing Stage-4 replay review, which I do not reopen. Outcome luck does not affect the class grade, but the ride-to-zero resolution confirms the pass was substantively right. Confidence HIGH: in-class match is unambiguous and the resolution is a primary benchmark value.

### ttcf_t2 — real: TTCF @ 2021-08-16 (~$17.50); Q2-21 miss + guidance cut
- **Fleet reasoning:** hold (MEDIUM) — Keep the existing slot as-is — do not add, do not exit. The thesis is bruised, not broken, and the sleeve's own rules say a falling price alone is never a reason to sell.
  - falsifiers: falsifier fires or the sleeve breaches its cap; neither has happened. **Data quality:** Figures are intentionally masked and scaled (ratios and growth rates are exact per the provenance note). The bull/bear debate outcome arrived empty, and the consensus revenue estimate is second-hand within the analyst report, so th
  - clock: THE CLOCK — next validation point: the Q3 earnings print, estimated ~3 months out (around October 2026), which also dates the 12,000-store distribution milestone; that print tests falsifiers 1, 3 and 4 directly. Expected re-rating horizon if margins inflect as branded mix rises: honestly 12-24 months — margin proof nee
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "C",
    "classification_rationale": "Frozen reconstruction burn: frozen_taxonomy present, so category=C / grading=exit / freeze_date=2021-08-16 are emitted verbatim without re-litigation. The packet describes a positioned name (TTCF) whose falsifiers fired at the 2021-08-12 Q2 print, four calendar days before the 2021-08-16 freeze — supporting an exit (SELL) call rather than a hold. The load-bearing exit evidence: first-ever consensus miss, gross-margin FY guide CUT to 16-22% (from the 20-25% guided at Q1 on 2021-05-12), and adjusted-EBITDA FY guide flipped to negative $14-17M (from positive $2-4M), i.e., unit economics structurally deteriorating despite still-rapid top-line growth. The distribution wins (Target nationwide six products; Kroger ~1,800 stores / 12 SKUs) and the ~$37M cash plant acquisitions are contemporaneous bull-facing facts included to keep the packet un-sanitized: the contemporaneous print still looks like a growth story, but the margin/EBITDA guide-down are the fired falsifiers that justify the exit. All four sources predate the freeze date; none is a retrospective 'why it moved' article. Corroboration: web search confirmed the GM guide cut 16-22% from 20-25%, the negative adj-EBITDA guide, and the Kroger 1,800-store / 12-SKU detail; primary-source pages could not be fetched directly (WebFetch unavailable), so confidence is MEDIUM rather than HIGH.",
    "confidence": "MEDIUM",
    "freeze_date": "2021-08-16",
    "grading": "exit",
    "sourced_facts": [
      {
        "fact": "Q2-2021 results: revenue $50.7M (+45.9% YoY), gross margin 15.7%, GAAP net loss -$53.2M including a one-time non-cash $46.0M deferred-tax valuation allowance; first consensus miss. FY gross-margin guidance CUT to 16-22% from the 20-25% guided at Q1 (2021-05-12 PR); adjusted-EBITDA FY guidance flipped to negative $14-17M from positive $2-4M. These fired falsifiers (margin/EBITDA guide-down) are the load-bearing exit trigger.",
        "publication_date": "2021-08-12",
        "url": "https://www.globenewswire.com/news-release/2021/08/12/2280137/0/en/Tattooed-Chef-Reports-Second-Quarter-2021-Financial-Results.html"
      },
      {
        "fact": "National retail distribution wins (contemporaneous bull evidence, not sanitized out): nationwide launch at a top mass retailer (Target, six products, per 2021-03-10 PR) and expansion into ~1,800 stores at the largest US grocer (Kroger, 12 SKUs rolled out nationally). Corroborated by contemporaneous trade press.",
        "publication_date": "2021-07-12",
        "url": "https://www.globenewswire.com/news-release/2021/07/12/2261160/0/en/Tattooed-Chef-to-Launch-in-Kroger-Stores-Nationwide.html"
      },
      {
        "fact": "Cash acquisitions of food/tortilla plants (New Mexico Food Distributors, Inc. + Karsten Tortilla Factory, LLC; ~$37M total) for added capacity and a southwestern product line; both closed 2021-05-14, completion announced 2021-05-18. Same PR detailed the expense reclassifications affecting reported gross margin.",
        "publication_date": "2021-05-18",
        "url": "https://www.globenewswire.com/news-release/2021/05/18/2231349/0/en/Tattooed-Chef-Completes-Acquisition-of-New-Mexico-Food-Distributors-Inc-and-Karsten-Tortilla-Factory-LLC-Reclassifies-Certain-Expenses.html"
      },
      {
        "fact": "FY2020 final results: revenue $148.5M (+75% YoY), gross margin 14.6% after reclassifications (reclass detail in the 2021-05-18 PR). Establishes the pre-freeze baseline of rapid growth on thin, deteriorating margins.",
        "publication_date": "2021-03-19",
        "url": "https://www.sec.gov/Archives/edgar/data/1741231/000121390021016721/f10k2020_tattooedchef.htm"
      }
    ]
  },
  "stage": 1,
  "verification": {
    "mismatches": [],
    "ok": true
  }
}
```
  - **sanitizer:**
```json
{
  "agent_role": "calibration_sanitizer",
  "model": "claude-opus-4-8",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "candidate_payload uses alias 'PVF'/'Provida Foods (alias PVF)' throughout. Scanned all payload fields against forbidden_terms: none of 'Tattooed Chef', 'TTCF', 'Ittella', 'Galletti', 'Forum Merger', 'Costco', 'Walmart', 'Sam's Club', 'Club Foods', 'Kroger', 'Karsten', 'New Mexico Food' appear in the masked payload. Named retailers/competitors are genericized ('one of the largest mass-market retailers', 'largest grocery chain', 'warehouse-club private-label', 'category's pure-play leader'). Forbidden strings exist only in raw_sources URLs/text (my ground truth), not in what the Trader sees. Note: 'tortilla/southwestern product line' is descriptive product-category language, not a forbidden term.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently verified rescale_factor 3.7 from source_manifest/raw_sources: FY rev $148.5M*3.7=549.45->549; Q2 rev $50.7M*3.7=187.6->188; net loss -$53.2M*3.7=-196.8->-197; DTA allowance $46.0M*3.7=170.2->~170; acquisitions ~$37M*3.7=136.9->~137; EBITDA guide -14/-17*3.7=-51.8/-62.9->-52/-63 and +2/4*3.7=7.4/14.8->+7/+15. All absolute figures present in the manifest scale consistently at 3.7; ratios/margins/growth left exact and unscaled; no raw un-rescaled absolute ($50.7/$53.2/$148.5/$46/$37) leaked into payload. Caveat: market_cap 5300, share_price 64.75, cash 519, debt 10, entry 74.95 are not present in the manifest and cannot be independently proven, but the provable subset is fully consistent and no true absolute leaked.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload uses only relative temporal anchors: 'FY-1 (the entry-year)', as_of 'current', 'Q0 = Q2 of the current FY, reported ~4 days ago', 'by the end of next quarter', 'for several quarters', 'Entered three quarters ago'. No absolute calendar dates appear in candidate_payload; the 2021-xx-xx dates exist only in raw_sources/source_manifest. '~4 days ago' is consistent with the manifest event date 2021-08-12 under a masked current date.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context genericized: 'Reopening economy', 'freight and food-input inflation squeezing every food manufacturer', 'speculative listing-vehicle cohort broadly deflating' (SPAC cohort genericized). No named or dated macro events (no COVID/year references, no named indices or programs). Competitor de-rating framed generically as 'category's pure-play leader has repeatedly missed'.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Non-synthetic historical packet (case ttcf_t2). All four protocol dimensions pass. No forbidden_terms leak into the masked candidate_payload (retailers/competitors genericized; forbidden strings confined to raw_sources ground truth). Absolute-figure rescaling independently proven at factor 3.7 from the source_manifest for every figure present there (FY/Q2 revenue, net loss, DTA allowance, acquisitions, EBITDA guidance), with ratios/margins/growth correctly left exact; no un-rescaled raw absolute leaked. Market cap, share price, cash and entry price are absent from the manifest and thus not independently provable, but the provable subset is fully consistent. Dates are all relative; macro events genericized. Safe to run."
  },
  "stage": 2,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **review:**
```json
{
  "agent_role": "calibration_reviewer",
  "model": "claude-opus-4-8",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "output_audit reports contaminated=false, suspect_terms=[], calendar_years_in_output=['2026']",
      "All output numerics match packet: 45.9% Q growth, +~59% FY guide, GM 20-25%→16-22%, EBITDA +7/+15→-52/-63, loss -197 with ~170 non-cash DTA, ex-item op loss ~-27, top-2 71%, cash 519/debt 10, P/S 6.0, entry 74.95→64.75=-14%, ~0.86% sizing",
      "Recommendation section contains required FALSIFIERS (5 conditions) and THE CLOCK (Q3 print ~3mo out + 12-24mo re-rating band)",
      "Exit-discipline constraint satisfied: HOLD with slot kept; no price-appreciation trim, no falsifier fired, no cap breach",
      "cited_sources = ['fundamentals/PVF','news/PVF'] — the packet report_names; raw source URLs correctly not cited"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Replay audit of case ttcf_t2 (masked PVF/Provida Foods). OUTPUT CLEAN: no forbidden identifiers leak into the Trader-visible output — retailers/competitors are genericized and real names (and the raw source URLs) are never cited; only calendar year 2026 appears, consistent with the masked present, not the 2021 freeze date. PACKET FIDELITY: every numeric claim reconciles to the packet — 45.9% Q growth, +~59% FY guide, GM cut 20-25%→16-22%, adj-EBITDA +7/+15→-52/-63, GAAP loss -197 with non-cash ~170 DTA and ex-item op loss ~-27, top-2 at 71%, cash 519 vs debt 10, ~30% higher share count, P/S 6.0, entry 74.95→64.75 = -14%, ~1%→~0.86% sizing, ~5% cap. WORKFLOW CORRECT: exit-discipline honored (position is down, not up; no falsifier fired, no cap breach → sanctioned HOLD keeping the slot); Recommendation section records both FALSIFIERS (5 checkable conditions) and THE CLOCK (Q3 print ~3mo out + honest 12-24mo re-rating band); MEDIUM confidence justified by empty bull/bear debate and second-hand consensus estimate. GROUNDING = 3: nearly all reasoning ties to packet figures, but two statements are uncited — an external food-sector multiple comparison and an Israeli CGT-offset tax claim lacking a domain_knowledge citation (Rule 1). Minor gaps only; no fabrication. I do not assess investment correctness and make no claim to hidden chain-of-thought.",
    "violations": [
      "Uncited tax/regulatory claim: 'the -14% unrealized loss remains available for future Israeli CGT offset' lacks a domain_knowledge citation (Rule 1) — minor, conditional",
      "Uncited external market claim: 'Established food businesses typically trade at a small fraction of that sales multiple' has no source citation — minor comparative statement"
    ],
    "workflow_correct": true
  },
  "stage": 4,
  "verification": {
    "failures": [],
    "ok": true
  }
}
```
  - **grading:**
```json
{
  "agent_role": "calibration_grader",
  "model": "claude-opus-4-8",
  "output": {
    "acted_return_pct": -100.0,
    "benchmark_return_pct": -100.0,
    "confidence": "HIGH",
    "in_expected_class": false,
    "rationale": "Case ttcf_t2 (category C, masked TTCF) is a ride-to-zero value trap: expected_classes=['sell'], grading rule='exit'. The benchmark resolution is terminal Chapter 11 (price_at_freeze_real 17.5 → price_at_horizon_real 0.0), benchmark_return_pct = -100%. The fleet verdict was HOLD (size 0.0 shares — no add, no exit), which keeps the existing slot alive through the horizon. HOLD is not in the expected 'sell' class, so in_expected_class=false. Because the position was retained rather than exited, the acted-on outcome equals the ride-to-zero path: acted_return_pct = -100%, matching the benchmark. Partial credit (0.5) is warranted only where the benchmark rule explicitly allows it — an F1 pass with falsifiers or a small trap buy carrying the killing falsifier. This verdict is neither: it is a full HOLD of an existing position in an exit-required case, and none of its five forward falsifiers is the killing condition that would have forced the exit before equity zeroed. The sleeve's 'never exit on price' discipline is exactly what produced the ride to zero here, so no partial-credit path applies → 0.0. Per instructions, outcome luck does not alter the class grade, but here the class itself (hold vs. required sell) is off, independent of the -100% resolution. The independent replay review's cleanliness/workflow judgment (output_clean, packet_fidelity, workflow_correct, grounding=3) is not reopened; it does not bear on investment-class correctness. Confidence HIGH: expected class, grading rule, and terminal resolution are all unambiguous.",
    "score": 0.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": -100,
    "expected_benchmark_return_pct": -100,
    "expected_in_class": false,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 0.0 (OUT of class); acted -100% vs benchmark -100%; source=calibration_grader
- notes: Case ttcf_t2 (category C, masked TTCF) is a ride-to-zero value trap: expected_classes=['sell'], grading rule='exit'. The benchmark resolution is terminal Chapter 11 (price_at_freeze_real 17.5 → price_at_horizon_real 0.0), benchmark_return_pct = -100%. The fleet verdict was HOLD (size 0.0 shares — no add, no exit), which keeps the existing slot alive through the horizon. HOLD is not in the expected 'sell' class, so in_expected_class=false. Because the position was retained rather than exited, the acted-on outcome equals the ride-to-zero path: acted_return_pct = -100%, matching the benchmark. Partial credit (0.5) is warranted only where the benchmark rule explicitly allows it — an F1 pass with falsifiers or a small trap buy carrying the killing falsifier. This verdict is neither: it is a full HOLD of an existing position in an exit-required case, and none of its five forward falsifiers is the killing condition that would have forced the exit before equity zeroed. The sleeve's 'never exit on price' discipline is exactly what produced the ride to zero here, so no partial-credit path applies → 0.0. Per instructions, outcome luck does not alter the class grade, but here the class itself (hold vs. required sell) is off, independent of the -100% resolution. The independent replay review's cleanliness/workflow judgment (output_clean, packet_fidelity, workflow_correct, grounding=3) is not reopened; it does not bear on investment-class correctness. Confidence HIGH: expected class, grading rule, and terminal resolution are all unambiguous.
