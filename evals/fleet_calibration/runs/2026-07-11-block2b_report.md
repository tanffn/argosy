# Fleet-calibration report — 2026-07-11-block2b.json

| Point | Category | Fleet verdict (conv, size) | In class? | Score | Acted vs benchmark |
|---|---|---|---|---|---|
| amd_2016_f1 | A/F1_lenient | buy (MEDIUM, 1.0 currency) | Y | 1.0 | +5357% vs +5357% |
| boot_2017 | A/entry | hold (MEDIUM, 0.0 shares) | N | 0.0 | +0% vs +1855% |
| cvna_2023_reentry | A/entry | buy (MEDIUM, 1.0 currency) | Y | 1.0 | +649% vs +649% |
| hyln_2021 | B/trap | hold (MEDIUM, 0.0 shares) | Y | 1.0 | +0% vs -86% |
| nlf_synthetic | synthetic/synthetic_winner | buy (HIGH, 1.5 currency) | Y | 1.0 | n/a (synthetic) |

## Category subtotals
- **A**: 2.0/3 points; acting on fleet +2002% vs benchmark +2620% (n=3)
- **B**: 1.0/1 points; acting on fleet +0% vs benchmark -86% (n=1)
- **synthetic**: 1.0/1 points
- **TOTAL**: 4.0/5; acting on the fleet at every frozen point: +1501% avg vs +1944% benchmark (n=4)

## Conviction calibration
- HIGH: 1.0/1 (100%)
- MEDIUM: 3.0/4 (75%)

## Disqualified / not scored
### sdc_2021
- error — disqualified_sanitizer
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
        "evidence": "Scanned the masked candidate_payload (alias 'CBO' / 'ClearBite Oral'). None of the denylisted forbidden_terms appear anywhere in the payload text, notes, or business description. Denylisted strings and the CIK-bearing SEC/GlobeNewswire URLs live only on the sanitizer-side raw_sources/source_manifest, which the Trader does not see. Aliasing of name and ticker is clean.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently verified rescale_factor=1.4 from source_manifest: Q3 raw revenue $138M x1.4 = 193.2 -> masked 193; raw net loss -$89M x1.4 = -124.6 -> masked -125; raw marketing $96M x1.4 = 134.4 -> masked 134. Ratios preserved exactly (revenue_growth_yoy -18.3%, marketing ~70% of revenue). No masked absolute figure equals any raw absolute figure (138/89/96/511/240/630-650/120-140 vs 193/125/134/308/1050/2240/5.67...), so no unscaled figure leaked. Scaling is consistent and provable from the manifest.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "The fy keys and latest_quarter correctly use relative anchors ('FY-2','FY-1','Q3, reported ~1 week ago','as_of: current'). However the annual_income_statement note fields hardcode absolute calendar years: 'GAAP consolidated net loss (FY2019)' and '(FY2020)'. These concrete fiscal-year labels are a temporal anchor that defeats the time-machine masking and lets the Trader pin the era. Concrete date leak.",
        "verdict": "fail"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The raw source references 'digital-ad attribution breakage' (a specific privacy/tracking-policy shock). The masked payload genericizes this to 'digital advertising attribution disrupted industry-wide' and 'digital-ad attribution changes', plus generic 'consumer discretionary soft for lower-income cohorts'. No named vendor, platform version, or dated macro event is disclosed. Genericization is adequate.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [
      "FY2019",
      "FY2020"
    ],
    "safe_to_run": false,
    "summary": "Not safe to run as-is. Three of four dimensions pass: no denylisted alias term reaches the masked payload; the 1.4x absolute-figure rescaling is independently proven from the source_manifest (138->193, 89->125, 96->134 with ratios preserved and no raw figure surviving unscaled); and the ad-attribution macro shock is properly genericized. The failure is on relative_dates: the annual_income_statement note fields hardcode absolute fiscal years 'FY2019' and 'FY2020', a concrete temporal anchor that breaks the time-machine masking. Fix: replace those year labels with the relative FY-2/FY-1 form already used in the fy keys, then re-audit. Company identity and outcome deliberately not inferred."
  },
  "stage": 2,
  "verification": {
    "failures": [
      {
        "check": "relative_dates",
        "verdict": "fail"
      },
      {
        "check": "leaked_terms",
        "verdict": [
          "FY2019",
          "FY2020"
        ]
      },
      {
        "check": "safe_to_run",
        "verdict": false
      }
    ],
    "ok": false
  }
}
```
  - **review:**
```json
{}
```
  - **grading:**
```json
{}
```
### omk_synthetic
- agent_pipeline — grading verification failed
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "synthetic",
    "classification_rationale": "The construction brief self-identifies this as a SYNTHETIC trap-shape ('no real company') modeling meal-kit / D2C 'growth theater'. There is no real underlying issuer, no freeze_date, and an empty source_manifest — so no primary or external sources exist to gather, and none may be fabricated (Rule 4). This places the case in the 'synthetic' category rather than A/B/C/D, which are reserved for real historical freeze points backed by period-accurate primary sources. Within synthetic, the subject describes a declining direct-to-consumer meal-kit shape dressed up as growth ('growth theater') — the canonical trap silhouette (surface-level revenue/subscriber optics masking deteriorating unit economics, churn, and CAC/LTV decay), analogous to the Blue Apron-style D2C fade. That maps to the 'synthetic_trap' grading rather than 'synthetic_winner'. freeze_date is left null because a synthetic construct has no real-world publication timeline to anchor a no-postdating cutoff. sourced_facts is intentionally empty: citing any URL here would be a hallucinated source, since the manifest is empty and the subject is not a real company.",
    "confidence": "HIGH",
    "freeze_date": null,
    "grading": "synthetic_trap",
    "sourced_facts": []
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
        "evidence": "Masked alias 'OMK' is applied consistently across both report objects (report_name 'fundamentals/OMK', 'news/OMK', positions, constraints). The only company name present is the fabricated placeholder 'Orbit Meal Kits (alias OMK)', which is a synthetic label, not a real-issuer identity. No real ticker, CIK, exec name, or unique identifier leaks. forbidden_terms is empty, so there is no denylist term to match against.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Packet is marked synthetic:true with raw_sources:[], source_manifest:[], and rescale_factor:null. There is no frozen underlying figure to rescale and no independent manifest against which scaling could be proven. For a fully fictional synthetic packet the rescaling dimension does not apply; the absolute figures (revenue 890/1180/1260, market_cap 3600, cash 480, price 22.7) are treated as invented constants, and no scaling proof is invented.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "All temporal references are relativized/genericized: fiscal years are 'FY-2','FY-1','FY-0 (just ended)', the quarter is 'Q0 just reported', balance sheet as_of='current', and narrative uses 'Rebranded last year' / 'in two years'. No absolute calendar year, quarter-date, or dated event that could pin the time window is present.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Consumer subscription fatigue; paid-social CAC inflation; growth-stock de-rating still in force' — all generic, undated regime descriptions. No named crisis, dated macro event, specific rate action, or year-anchored episode that would fingerprint the period.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Synthetic candidate packet (case omk_synthetic, synthetic:true). No concrete identity, date, or macro-event leak found. Alias 'OMK' is applied consistently and the only company name is a fabricated placeholder. Dates are fully relativized (FY-2/FY-1/FY-0, Q0, 'last year', 'current') and macro context is generic/undated — both pass. Absolute-figure rescaling is not_applicable: the packet is fully fictional with empty raw_sources/source_manifest and null rescale_factor, so scaling cannot and need not be independently proven (no proof invented). forbidden_terms is empty; no denylist matches. safe_to_run=true. Company is not identified and no eventual outcome is inferred."
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
      "Contribution-after-marketing -55 (FY-0) and -18 (latest_quarter) cited exactly from packet",
      "Revenue growth chain 62/32.6/6.8/4.5 matches FY-2/FY-1/FY-0/latest_quarter",
      "Gross margin 34%->27.5% matches FY-2 and latest_quarter",
      "Net losses -210/-340/-410 and FCF -290 match income statement",
      "PS 2.9x matches valuation.ps_trailing (3600/1260 internally consistent)",
      "Cash 480 / debt 620 and '<2 years burn' match balance_sheet + note",
      "Citations fundamentals/OMK & news/OMK match report_name values verbatim",
      "FALSIFIERS and THE CLOCK reporting requirement satisfied incl. explicit unestimable-horizon statement",
      "output_audit contaminated=false, no calendar years, alias respected",
      "Stage-2 sanitizer safe_to_run=true with verification.ok before consult"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay audit of case omk_synthetic: the consult output is clean and faithful. Every numeric claim in the visible rationale (contribution-after-marketing -55/-18, revenue-growth chain 62/32.6/6.8/4.5, gross margin 34%->27.5%, net losses -210/-340/-410, FCF -290, PS 2.9x, cash 480 vs debt 620, <2yr burn) traces verbatim to packet facts, and cited sources fundamentals/OMK and news/OMK match report_name exactly. The fleet reporting requirement (FALSIFIERS + THE CLOCK with an honest horizon band, including an explicit unestimable-in-constructive-direction statement) is fully met. Output is contamination-free with no calendar years and consistent alias use; the alias/date/macro masking is intentional and correctly handled. Workflow is correct: the stage-2 calibration sanitizer returned safe_to_run=true with passing verification before the consult ran. The written rationale is fully grounded step-by-step in packet data with no unsupported leaps. Investment correctness not graded, per scope.",
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
    "benchmark_return_pct": null,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Expected class is [hold]; the fleet returned action=hold with size 0.0 (positioned=false), squarely in-class. This is a synthetic_trap case — a contribution-negative, decelerating cash-burner — and the correct verdict is do-not-initiate, which the fleet delivered with the required FALSIFIERS + THE CLOCK reporting and an honest unestimable-horizon statement. Because the verdict is a clean in-class hold (no trap buy taken), the benchmark rule's 0.5 partial-credit path — reserved for a small trap buy carrying the killing falsifier or an F1 pass with falsifiers — does not apply; full 1.0 is warranted. The independent replay review (stage 4) found output_clean=true, packet_fidelity=true, workflow_correct=true, no violations, verification.ok=true; per scope I do not reopen that cleanliness/workflow judgment. Acted-on return is 0.0% since no position was opened (size 0.0) and cash was held; benchmark return is null because answer_key.resolution is null, so no realized comparator exists — outcome luck does not affect the class grade regardless. Confidence HIGH: in-class match is unambiguous and the grading rule maps cleanly to 1.0.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": null,
    "expected_benchmark_return_pct": null,
    "expected_in_class": true,
    "mismatches": [
      {
        "actual": 0.0,
        "expected": null,
        "field": "acted_return_pct"
      }
    ],
    "ok": false
  }
}
```

## Per-point detail (section 2b three-column + clock)
### amd_2016_f1 — real: AMD @ 2016-02-29 ($2.14 adj); F1 per spec appendix (post FY2015 10-K, before T_parabola / THATIC)
- **Fleet reasoning:** buy (MEDIUM) — A high-risk, high-reward turnaround that fits the high-potential sleeve — buy a small starter position sized at the low end of the sleeve budget.
  - falsifiers: FALSIFIERS that would kill the thesis: (1) the new CPU core slips well past ~18 months or launches materially below its claimed performance / fails to win share; (2) a covenant breach or forced, heavily dilutive emergency capital raise before the product ships; (3) loss or non-renewal of the console chip contracts — th
  - clock: THE CLOCK: interim quarterly prints over the next 12-18 months are burn/stabilization checkpoints; the hard catalyst is the clean-sheet CPU launch in ~12-18 months. Honest re-rating horizon: 2-3 years, and it is genuinely hard to pin more tightly than that — that imprecision is itself a reason conviction is MEDIUM, not
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
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
        "evidence": "Company masked as 'Corvid Microsystems (alias CVM)'. Full scan of candidate_payload finds no forbidden_terms leaked: product named 'clean-sheet CPU core' (not Zen/Ryzen), rival called 'dominant CPU incumbent' (not Intel), consoles as 'BOTH major game consoles' (not PlayStation/Xbox). No real identity or CEO name surfaced.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently verified from source_manifest at factor 1.7: revenue 3991*1.7=6784.7~6785; net loss 660*1.7=1122; cash 785*1.7=1334.5; total debt 2260*1.7=3842; equity deficit -412*1.7=-700; interest exp 160*1.7=272. All six manifest-backed absolutes scale consistently; ratios/margins/growth preserved exact.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload expresses time relatively ('FY-1/FY-2/FY-3', '~2 weeks ago', '~two years ago', 'prior year', '12-18 months', as_of:'current'). None of the raw_sources absolute dates (2015-05-06, 2015-07-16, 2016-02-18) appear in the masked text.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Raw incumbent process-node slip (Intel, 14nm, mid-2015) rendered generically as 'dominant CPU incumbent showed a process-node cadence slip in the prior year'. Macro context ('PC market shrinking mid-single-digits; server demand growing with cloud buildouts; semi cycle soft') carries no identifying node/vendor detail.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Non-synthetic packet (synthetic:false), so all four checks apply. No forbidden denylist term leaks in the masked payload. Rescaling at factor 1.7 is independently provable from source_manifest across all six manifest-backed absolutes (revenue, net loss, cash, debt, equity deficit, interest expense). Relative dates and macro-event genericization both clean. Safe to run.\""
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
      "Revenue '$6,785M ... fell 27.5%' matches packet fundamentals revenue_usd_m:6785, revenue_growth_pct:-27.5",
      "Loss progression '-$141M, then -$685M, then -$1,122M' matches FY-3/FY-2/FY-1 net_income_usd_m",
      "Balance-sheet claims (debt $3,842M, cash $1,334.5M, equity -$700M, interest $272M) match balance_sheet block exactly",
      "P/S 0.43 and EV/S 0.80 match valuation.ps_trailing:0.43 and ev_s_trailing:0.8",
      "GM 27% depressed by write-downs / structural ~32% matches fundamentals note",
      "FALSIFIERS and THE CLOCK (12-18mo CPU launch, 2-3yr honest horizon) satisfy the constraints_rendered reporting requirement",
      "output_audit.contaminated=false and sanitizer safe_to_run=true with leaked_terms=[] corroborate clean masked output"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Independent replay audit of case amd_2016_f1 (alias CVM). OUTPUT CLEAN: response_raw is well-formed JSON; output_audit shows contaminated=false with no calendar years, and the masked identity holds (no AMD/Intel/Zen/PlayStation/Xbox leakage) — consistent with the stage-2 sanitizer passing all four masking checks (alias, factor-1.7 rescaling, relative dates, macro genericization). PACKET FIDELITY: every numeric claim in the rationale matches the packet exactly — revenue 6785/-27.5%, prior 3.9%, loss ladder -141/-685/-1122, debt 3842, cash 1334.5, equity -700, interest 272, P/S 0.43, EV/S 0.80, GM 27%/~32%, 40% IPC, 12-18mo launch. WORKFLOW CORRECT: sanitizer→generation ordering intact; sizing (~1% of a 1-2%/name, ≤5% sleeve), stock/market (no leverage/options), and both mandated reporting elements — FALSIFIERS (4) and THE CLOCK (CPU launch next validation point, honest 2-3yr band with explicit imprecision) — are present. GROUNDING: rationale is strongly and specifically tied to packet facts. Minor gap: the 25% Israeli CGT figure in tax_estimate is a regulatory rate stated without the domain_knowledge citation Rule 1 requires. I grade only the written rationale and do not assess investment correctness or the expected verdict.",
    "violations": [
      "tax_estimate cites 'Israeli CGT @25%' as a regulatory rate without the required domain_knowledge/... path citation (Rule 1)"
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
    "rationale": "Case amd_2016_f1 (Category A, genuine winner). expected_classes=[\"buy\"]; fleet action=\"buy\" → in-class verdict. Under the primary rule (\"1.0 for an in-class verdict\"), this scores full credit. The recorded rule is F1_LENIENT: on this deliberately scary setup (revenue −27.5% to $6,785M, three widening losses −141/−685/−1,122, negative equity −$700M, debt $3,842M vs cash $1,334.5M), the doctrine-ideal answer IS a falsifier-gated, small starter (~1% of a 1–2%/name high-potential sleeve) at MEDIUM conviction — exactly what the fleet delivered, with four explicit falsifiers and THE CLOCK (12–18mo CPU catalyst, honest 2–3yr re-rating band). The 0.5 partial-credit path is reserved for weaker near-misses (a strict-F1 hedged pass, or a small trap buy saved only by the killing falsifier); it does not apply to a clean, complete in-class buy on a genuine winner under the lenient rule. Per instruction, outcome luck does not change the class grade, but the acted-on return follows from the buy-and-hold to horizon: (116.78 − 2.14)/2.14 = 5357%, matching benchmark_return_pct=5357.0. I do not reopen the reviewer's cleanliness/workflow judgment (output_clean, packet_fidelity, workflow_correct all true; the noted uncited 25% CGT rate is a Rule-1 nit that does not bear on the class grade). Confidence HIGH: unambiguous in-class match plus live resolution data.",
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
- notes: Case amd_2016_f1 (Category A, genuine winner). expected_classes=["buy"]; fleet action="buy" → in-class verdict. Under the primary rule ("1.0 for an in-class verdict"), this scores full credit. The recorded rule is F1_LENIENT: on this deliberately scary setup (revenue −27.5% to $6,785M, three widening losses −141/−685/−1,122, negative equity −$700M, debt $3,842M vs cash $1,334.5M), the doctrine-ideal answer IS a falsifier-gated, small starter (~1% of a 1–2%/name high-potential sleeve) at MEDIUM conviction — exactly what the fleet delivered, with four explicit falsifiers and THE CLOCK (12–18mo CPU catalyst, honest 2–3yr re-rating band). The 0.5 partial-credit path is reserved for weaker near-misses (a strict-F1 hedged pass, or a small trap buy saved only by the killing falsifier); it does not apply to a clean, complete in-class buy on a genuine winner under the lenient rule. Per instruction, outcome luck does not change the class grade, but the acted-on return follows from the buy-and-hold to horizon: (116.78 − 2.14)/2.14 = 5357%, matching benchmark_return_pct=5357.0. I do not reopen the reviewer's cleanliness/workflow judgment (output_clean, packet_fidelity, workflow_correct all true; the noted uncited 25% CGT rate is a Rule-1 nit that does not bear on the class grade). Confidence HIGH: unambiguous in-class match plus live resolution data.

### boot_2017 — real: Boot Barn (BOOT) @ 2017-06-02 ($6.30); obscure western/work retailer during the store-based-retail-decline narrative; ~20x by mid-2024
- **Fleet reasoning:** hold (MEDIUM) — Do not initiate a position — this is a heavily indebted, no-growth specialty retailer that does not clear the bar for the high-potential growth sleeve.
  - falsifiers: FALSIFIERS that would kill (or rescue) the contrarian thesis: (a) same-store sales turn durably positive for 2+ consecutive quarters, proving real pricing power and moat; (b) meaningful debt paydown that de-risks the balance sheet; (c) margins recover back toward prior-year levels. Conversely, worsening same-store sale
  - clock: THE CLOCK: next validation point is the upcoming Q1 earnings print (~3 months out), where flat-guide credibility gets its first test. Honest re-rating horizon: hard to estimate — a debt-heavy retail turnaround with no stated re-acceleration and no catalyst could take 2-3 years or never re-rate at all, and that genuine
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
        "evidence": "candidate_payload uses masked name 'Prairie Outfitters (alias POT)' / ticker POT. None of the forbidden_terms (Boot Barn, BOOT, Sheplers, Conroy, Country Outfitter, Wood's Boots) appear anywhere in the masked payload. The real SEC CIK URL and the 'boot-barn' URL exist only in raw_sources/source_manifest (reference material), not in the Trader-facing candidate_payload.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Factor 2.1 independently confirmed from source_manifest: FY-1 rev 1323 = $629.8M x2.1 (1322.6); FY-2 rev 1195 = implied FY2016 $568.9M x2.1 (1194.7); Q4 rev 342 = $163M x2.1 (342.3); GAAP NI 30 = $14.2M x2.1 (29.8); adj NI 31 = $14.9M x2.1 (31.3). No raw absolute figure (629.8/163/14.2/14.9/0.53/0.55) leaks into the masked payload; ratios (10.7% growth, +0.3%/-0.9% SSS, -20% adj EPS) are preserved exactly. market_cap 357, share_price 13.23, cash 32, debt 462 are not present in the manifest so cannot be individually proven, but they are internally consistent and are not raw leaks.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload uses only relative period labels: 'FY-2', 'FY-1 (just reported)', as_of 'current', 'Q4 / FY just reported', 'next-year guide is flat SSS'. No absolute year (2017) or calendar date (e.g. 2017-04-01/2017-06-02) from raw_sources appears in the masked payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Raw source detail 'low commodities prices in key markets + soft general retail' is genericized in the payload to 'energy/ag end-markets uneven' and 'broad de-rating of store-based specialty retail'. No named or dated identifiable macro event is exposed; macro_context is a generic sector-narrative description.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "All four protocol dimensions pass. No forbidden term, raw dollar figure, absolute date, or identifiable macro event leaks into the Trader-facing candidate_payload. Rescale factor 2.1 was independently verified from the source_manifest against five figures (FY-1/FY-2 revenue, Q4 revenue, GAAP NI, adjusted NI), all matching within rounding; preserved ratios are exact. A few balance-sheet/price figures (market cap, share price, cash, debt) are not in the manifest and thus not individually provable, but they are not raw leaks and are internally consistent. synthetic=false so all checks are applicable. safe_to_run=true."
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
      "'~220 physical stores' matches packet balance_sheet note 'store base ~220 doors across ~30 states'",
      "'sales rose 10.7%' matches revenue_growth_pct 10.7; SSS +0.3% matches same_store_sales_pct 0.3; latest-quarter SSS -0.9% matches latest_quarter.same_store_sales_pct -0.9",
      "'adjusted EPS fell about 20%' matches FY-1 note 'adj EPS down ~20% YoY despite sales growth'",
      "Net margin ~2.3% correctly derived from net_income_usd_m 30 / revenue_usd_m 1323 (=2.27%)",
      "Debt/cash/market-cap claims ($462M / $32M / $357M) match total_debt_usd_m 462, cash_and_equivalents_usd_m 32, market_cap_usd_m 357",
      "Valuation claims P/S 0.27 and P/E 11.9 match ps_trailing 0.27 and pe_trailing 11.9",
      "FALSIFIERS and THE CLOCK sections present in Recommendation per reporting requirement; unestimable re-rating horizon explicitly disclosed as required",
      "cited_sources ['fundamentals/POT','news/POT'] match report_name fields verbatim in packet",
      "Masked/scaled figures acknowledged per provenance note and not treated as a data-quality defect",
      "Sanitizer stage-2 checks (alias, rescaling, relative_dates, macro_genericization) all pass; output_audit contaminated=false with no calendar years or forbidden terms"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay is clean and faithful. Every numeric and structural claim in the visible rationale traces exactly to packet fields (growth 10.7%, SSS +0.3%/-0.9%, adj EPS -20%, debt 462/cash 32/mktcap 357, P/S 0.27, P/E 11.9), and the net-margin figure (~2.3%) is correctly derived from net_income 30 / revenue 1323. The mandated FALSIFIERS and THE CLOCK (Q1 print ~3 months; honestly-unestimable re-rating horizon) are both recorded, confidence band (MEDIUM) is reported, and the masking provenance is handled per instructions rather than flagged as a defect. cited_sources match the packet report_names verbatim; no domain_knowledge cite is required since no tax/regulatory rate is claimed (no transaction). Sanitizer passed all four checks with safe_to_run=true, and output_audit shows no contamination or calendar-year leakage. Action=hold with size 0 and all-zero expected_impact is internally consistent with a do-not-initiate verdict. Rationale is fully grounded (score 4). Investment correctness was not assessed and no hidden chain-of-thought was inspected.",
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
    "benchmark_return_pct": 1855.0,
    "confidence": "HIGH",
    "in_expected_class": false,
    "rationale": "Expected class = [\"buy\"]; fleet verdict = action \"hold\", size 0.0 shares — an explicit do-not-initiate. That is not in the expected class, so in_expected_class=false. Score is 0.0: the benchmark's only partial-credit provisions are (a) an F1 pass carrying falsifiers and (b) a *small trap buy* with the killing falsifier stated. Neither applies here — this is the opposite shape (a full decline of a buy-expected name, positioned=false), not a hedged/small entry, so no 0.5 warrant exists. The rationale did carry falsifiers (SSS turning durably positive, debt paydown, margin recovery) and THE CLOCK, but falsifiers only rescue a small trap buy or an F1 case; they do not upgrade a zero-size decline on a buy-expected case. Acted-on return = 0.0% because no position was taken (size 0, positioned=false); the benchmark resolution shows the name ran from $6.30 at freeze to $123.15 at the 7y (2024-06-03) horizon, a +1855% miss. Per the rule, outcome luck does not change the class grade — the decline would be off-class regardless of the eventual print, and the review agent's clean/faithful workflow judgment (replay grounded, sanitizer/output_audit pass) is left untouched and does not offset the class miss.",
    "score": 0.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 0.0,
    "expected_benchmark_return_pct": 1855,
    "expected_in_class": false,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 0.0 (OUT of class); acted +0% vs benchmark +1855%; source=calibration_grader
- notes: Expected class = ["buy"]; fleet verdict = action "hold", size 0.0 shares — an explicit do-not-initiate. That is not in the expected class, so in_expected_class=false. Score is 0.0: the benchmark's only partial-credit provisions are (a) an F1 pass carrying falsifiers and (b) a *small trap buy* with the killing falsifier stated. Neither applies here — this is the opposite shape (a full decline of a buy-expected name, positioned=false), not a hedged/small entry, so no 0.5 warrant exists. The rationale did carry falsifiers (SSS turning durably positive, debt paydown, margin recovery) and THE CLOCK, but falsifiers only rescue a small trap buy or an F1 case; they do not upgrade a zero-size decline on a buy-expected case. Acted-on return = 0.0% because no position was taken (size 0, positioned=false); the benchmark resolution shows the name ran from $6.30 at freeze to $123.15 at the 7y (2024-06-03) horizon, a +1855% miss. Per the rule, outcome luck does not change the class grade — the decline would be off-class regardless of the eventual print, and the review agent's clean/faithful workflow judgment (replay grounded, sanitizer/output_audit pass) is left untouched and does not offset the class miss.

### cvna_2023_reentry — real: CVNA @ 2023-08-01 ($49.11 as-traded); re-entry after 2023-07-19 debt exchange + record GPU/Adj EBITDA print
- **Fleet reasoning:** buy (MEDIUM) — Buy a small starter position in the high-potential sleeve — this is a genuine turnaround where the survival risk has just been removed, sized so it can hurt but not damage the plan.
  - falsifiers: FALSIFIERS that would kill the thesis: (1) adjusted EBITDA turns negative again or GPU rolls back over; (2) a large forced dilutive equity raise; (3) any covenant breach or a new maturity wall re-emerging on the debt; (4) revenue fails to stabilize and re-accelerate over the next 12-18 months. THE CLOCK: next validatio
  - clock: THE CLOCK: next validation point is the next quarterly earnings print in roughly 3 months — management has guided to another positive adjusted EBITDA quarter, so watch whether GPU holds and units stop falling. Expected re-rating horizon is an honest 2-3 years, the time needed to prove balance-sheet repair and volume re
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
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
        "evidence": "Masked payload consistently uses 'APO' / 'AutoPort Online' with no forbidden target term (Carvana, CVNA, Garcia, ADESA, vending machine, Apollo, Ernie) appearing anywhere in candidate_payload. Real carvana.com URLs / SEC CIK 1690820 appear only in raw_sources+source_manifest (verifier inputs), not in the Trader-facing payload. Auction network, founder, and product-format identifiers are all genericized. (Assigned alias 'APO' coincidentally matches Apollo's real ticker, but does not disclose the target company.)",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Factor 1.15 independently confirmed from source_manifest across all verifiable dollar figures: revenue 2968->3413 (x1.150), cash 677->779 (x1.151), total GPU 6520->7498 (x1.150), Adj EBITDA 155->178 (x1.149), GAAP loss 105->121 (x1.150), debt reduction 1200->1380 (x1.150), cash-interest cut 430->494.5 (x1.150). Ratios/growth rates (-24% rev, -35% units, -94% GPU) preserved unscaled. Total_debt 5980, market_cap 10652, share_price 56.48 and FY annual lines have no manifest counterpart but are internally consistent.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload uses relative/anchored dates only: 'Q2, reported ~2 weeks ago', as_of 'current', 'Two years ago', 'a year ago', and FY-1/FY-2 labels. No absolute 2023-07-19 date and no specific 2025/2027 maturity years leak; maturities are genericized to 'near-term maturity wall' / 'survival cliff removed'.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Pandemic-era used-vehicle price spike is genericized as 'used-vehicle prices off the shock-era peak' and 'prior cycle peak/high'; credit tightening framed generically. No named macro event (COVID/pandemic/specific year) appears.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Real (non-synthetic) masked packet cleared. No forbidden target terms leak into the Trader-facing candidate_payload; the real company name/URLs are confined to the verifier-only raw_sources/source_manifest. Rescaling factor 1.15 is independently proven from the manifest across every verifiable dollar figure (revenue, cash, GPU, EBITDA, net loss, debt reduction, interest cut) with ratios preserved. Dates are properly relativized (FY-1/FY-2, '~2 weeks ago', 'current') and the pandemic macro spike is genericized to 'shock-era peak'. All four checks pass; safe_to_run=true. One benign observation: the assigned alias 'APO' collides with Apollo's real ticker but does not disclose the target company.\"}"
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
      "Every dollar/ratio in the rationale maps exactly to the packet: GPU 7498, Adj EBITDA 178, GAAP loss -121, total debt 5980, cash 779, P/S 0.68, EV/S 1.01, rev -24% YoY, P/E null.",
      "Units -35% and debt-cut >$1,380M / interest-cut >$494M/yr are supported by the news report and balance_sheet note in the packet.",
      "output_audit reports contaminated=false, empty calendar_years_in_output, and no suspect terms; rationale uses only relative dates ('~2 weeks ago','~3 months','2-3 years','12-18 months') and the alias APO/AutoPort.",
      "Reporting requirement satisfied: Recommendation section contains explicit FALSIFIERS (4 checkable conditions) and THE CLOCK (next earnings ~3 months, 2-3yr re-rating band with stated unestimability).",
      "Sizing (~1% = low end of the 1-2% sleeve band, market order, GTC, stock, no options/leverage) conforms to the rendered constraints.",
      "cited_sources restricted to the two supplied reports fundamentals/APO and news/APO, both present in the packet.",
      "Grounding gap: tax_estimate asserts Israeli capital-gains tax 25% without a domain_knowledge citation and the rate is not present in the packet."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Replay is clean and faithful. All numeric claims in the visible rationale reconcile exactly to the persisted packet (GPU 7498, Adj EBITDA 178, GAAP loss -121, debt 5980, cash 779, P/S 0.68, EV/S 1.01, rev -24%, units -35%, debt/interest cuts). No contamination or leaked target terms; only relative dates used, consistent with the passing stage-2 sanitizer. Workflow is correct: sanitizer cleared safe_to_run, and the trader emitted a schema-valid buy at the low end of the 1-2% sleeve band with the mandated FALSIFIERS and THE CLOCK present in the Recommendation section. Rationale is well grounded with one minor gap: the 25% Israeli capital-gains rate in tax_estimate is a regulatory figure lacking a domain_knowledge citation and is not in the packet. Score 3 (grounded with a minor citation gap). I did not assess investment correctness and make no claim about hidden reasoning.",
    "violations": [
      "tax_estimate cites a 25% Israeli capital-gains rate (a regulatory rate) with no domain_knowledge/... citation; the figure is absent from the packet and uncited, a Rule 1 gap."
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
    "acted_return_pct": 648.9,
    "benchmark_return_pct": 648.9,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Category A entry case cvna_2023_reentry. Expected class = {buy}; the fleet verdict was action=\"buy\" (a ~1% starter in the high-potential sleeve), which is squarely in-class, so the grading rule assigns 1.0. No partial-credit condition applies: this is not a bare F1-with-falsifiers pass nor a trap buy with a killing falsifier — it is a direct in-class buy on a genuine survival-risk-abated re-entry, and the rationale carries proper FALSIFIERS and THE CLOCK. Outcome luck does not affect the class grade. Acted-on return: the buy captured the full move from price_at_freeze_real 49.11 to price_at_horizon_real 367.78 over the 2y horizon = (367.78−49.11)/49.11 = 648.9%, equal to the benchmark_return_pct of 648.9. The review agent's cleanliness/workflow judgment (output_clean, packet_fidelity, workflow_correct, grounded score 3 with the noted minor uncited 25% Israeli CGT gap) is not reopened here and does not change the class grade.",
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
- notes: Category A entry case cvna_2023_reentry. Expected class = {buy}; the fleet verdict was action="buy" (a ~1% starter in the high-potential sleeve), which is squarely in-class, so the grading rule assigns 1.0. No partial-credit condition applies: this is not a bare F1-with-falsifiers pass nor a trap buy with a killing falsifier — it is a direct in-class buy on a genuine survival-risk-abated re-entry, and the rationale carries proper FALSIFIERS and THE CLOCK. Outcome luck does not affect the class grade. Acted-on return: the buy captured the full move from price_at_freeze_real 49.11 to price_at_horizon_real 367.78 over the 2y horizon = (367.78−49.11)/49.11 = 648.9%, equal to the benchmark_return_pct of 648.9. The review agent's cleanliness/workflow judgment (output_clean, packet_fidelity, workflow_correct, grounded score 3 with the noted minor uncited 25% Israeli CGT gap) is not reopened here and does not change the class grade.

### hyln_2021 — real: Hyliion (HYLN) @ 2021-06-01 ($10.61); obscure Class-8 e-powertrain blank-check-merger winner-shape with $0 revenue; -86% by mid-2024
- **Fleet reasoning:** hold (MEDIUM) — Do not initiate a position — VoltHaul Systems (VHS) is a pre-revenue story stock whose price already assumes years of successful execution that hasn't started, so there is no buffer protecting you if it stumbles.
  - falsifiers: FALSIFIERS (what would change the call to a small starter buy, or confirm the pass): (a) BUY-SIDE — actual recognized revenue in H2 at or near guidance, plus a successful year-end demo of the flagship long-haul architecture, plus a binding OEM or fleet order; hitting these would justify a 1-2% sleeve entry even at a hi
  - clock: THE CLOCK: next validation point is the H2 first-Hybrid-revenue print (this calendar year) and the year-end flagship demos — that is when the story first gets tested against reality. Honest re-rating horizon to a real long-hold thesis: 2-3 years for commercialization to prove out; this is inherently hard to time becaus
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
        "evidence": "No forbidden term appears in the masked candidate_payload. Alias 'VHS'/'VoltHaul Systems' used throughout. Identifying specifics genericized: flagship product -> 'flagship long-haul architecture'; SPAC sponsor -> 'blank-check merger'; OEM/chassis partner -> 'chassis deliveries for demos'; founder left unnamed as 'charismatic founder-CEO'. Forbidden list (Hyliion, HYLN, Hypertruck, Healy, Tortoise Acquisition, Peterbilt) all absent from the masked text.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "rescale_factor 0.85 independently confirmed from source_manifest/raw_sources: net loss 16.6*0.85=14.1->-14; R&D 9.3*0.85=7.9->8; SG&A 7.4*0.85=6.3->6; cash 335*0.85=284.75->285; investments 297*0.85=252.45->252. All manifest-backed figures consistent. (market_cap 1530, share_price 9.02, FY-1 net_income -60 have no manifest counterpart, so not independently checkable, but no contradiction and the proven factor holds across all verifiable lines.)",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Masked payload expresses timing relatively: as_of 'current'; 'Q1, 10-Q filed ~2 weeks ago'; 'first revenue guided H2/back half of the year'; 'year-end demos'; 'went public via a blank-check merger months ago'. No absolute 2021 calendar dates from raw_sources leak into the masked payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The period-specific SPAC warrant-accounting restatement wave is rendered generically as 'blank-check warrant accounting (industry-wide issue)' with 'SPAC' replaced by 'blank-check' and the raw '~80 vehicles' count and 2021 dates suppressed. Freight-cycle/ESG-decarb framing is generic and undated.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Masked payload passes all four checks. No forbidden/denylist term leaks; product, sponsor, OEM, and founder identifiers are genericized. Absolute-figure rescaling (0.85) is independently proven from the source_manifest across five figures (net loss, R&D, SG&A, cash, investments); market cap, share price, and FY-1 net income lack manifest counterparts but show no inconsistency. Dates are relative throughout, and the era-specific blank-check warrant restatement is genericized without dates or counts. Safe to run."
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
      "Cash+investments: rationale '$285M cash plus $252M in marketable investments (roughly $537M total)' = packet balance_sheet cash_and_equivalents_usd_m 285 + marketable_investments_usd_m 252 = 537",
      "Loss run-rate: 'roughly $14M-per-quarter loss' = packet latest_quarter.gaap_net_income_usd_m -14",
      "Valuation: 'values the whole company at about $1,530M' and EV '~$1B' = packet market_cap_usd_m 1530 minus ~537 = ~993",
      "Zero-revenue/uncomputable multiples: P/E, P/S, EV/S null = packet valuation fields all null; annual/quarter revenue_usd_m 0",
      "Deployments-not-sales: 'installed ten hybrid units' = news recent_developments 'Ten Hybrid units installed ... deployments, not sold-and-recognized volume'",
      "Clean balance sheet: 'against zero debt' = packet total_debt_usd_m 0",
      "Restatement caveat = fundamentals note 'warrant-accounting restatement in flight' and news 'Restating prior-year blank-check warrant accounting'",
      "FALSIFIERS/CLOCK grounded in news guidance 'first Hybrid revenue in the back half of the year and year-end demos of the flagship long-haul architecture'",
      "Cited source ids fundamentals/VHS and news/VHS appear verbatim as packet report_name values"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay passes all audited dimensions. Output is clean: no forbidden/identifying terms (Hyliion, HYLN, Hypertruck, Healy, Tortoise, Peterbilt) leak; alias VHS/VoltHaul used throughout; SPAC is a generic category expansion, not an identifier; no absolute calendar year appears (timing kept relative), matching output_audit.contaminated=false. Packet fidelity holds: cash 285 + investments 252 = ~537M, ~$14M/qtr loss, market cap 1530, derived EV ~$1B, zero debt, ten hybrid units, and null P/E-P/S-EV/S all trace exactly to packet fields; cited_sources fundamentals/VHS and news/VHS match report_name values verbatim. Constraint fidelity: the fleet reporting requirement is met with explicit FALSIFIERS and THE CLOCK (next validation = H2 revenue print + year-end demos; 2-3yr horizon explicitly flagged as hard-to-time, lowering conviction); sleeve sizing (~1-2%) preserved; provenance/masking honored with no unmasking attempt. Workflow is coherent: action=hold/size 0 aligns with a do-not-initiate verdict and expected_impact of no transaction; MEDIUM confidence is defensible. Visible rationale is fully grounded step-by-step in packet facts with no unsupported claims. Investment correctness was not assessed, per mandate.",
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
    "rationale": "Case hyln_2021 (category B, grading=trap). Expected class = [\"hold\"]; fleet verdict = action \"hold\", size 0.0 (do-not-initiate). That is squarely in the expected class, so the benchmark rule assigns a full in-class pass = 1.0. This is not a partial-credit situation: no trap buy was taken (positioned=false), so the 0.5 lanes (F1 pass with falsifiers, or a small trap buy with the killing falsifier) do not apply — the verdict fully declines the position. The rationale correctly identifies the pre-revenue SPAC story-stock as full-narrative-price with no earnings buffer, and includes explicit FALSIFIERS and THE CLOCK, but those only strengthen an already in-class hold; they do not downgrade it. Acted-on return: fleet took no position (size 0), so acted return = 0.0%. Benchmark buy-and-hold return from resolution: price_at_freeze_real 10.61 → price_at_horizon_real 1.48 = -86% over the 3y horizon (yfinance closes fetched 2026-07-11). The fleet avoided an 86% drawdown; outcome luck does not affect the class grade, but here the class grade is independently correct. Per mandate I do not reopen the review agent's cleanliness/workflow judgment (review: output_clean, packet_fidelity, workflow_correct all true, no violations). Confidence HIGH: resolution is live primary data fetched today and the grade is deterministic against the recorded rule.",
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
- notes: Case hyln_2021 (category B, grading=trap). Expected class = ["hold"]; fleet verdict = action "hold", size 0.0 (do-not-initiate). That is squarely in the expected class, so the benchmark rule assigns a full in-class pass = 1.0. This is not a partial-credit situation: no trap buy was taken (positioned=false), so the 0.5 lanes (F1 pass with falsifiers, or a small trap buy with the killing falsifier) do not apply — the verdict fully declines the position. The rationale correctly identifies the pre-revenue SPAC story-stock as full-narrative-price with no earnings buffer, and includes explicit FALSIFIERS and THE CLOCK, but those only strengthen an already in-class hold; they do not downgrade it. Acted-on return: fleet took no position (size 0), so acted return = 0.0%. Benchmark buy-and-hold return from resolution: price_at_freeze_real 10.61 → price_at_horizon_real 1.48 = -86% over the 3y horizon (yfinance closes fetched 2026-07-11). The fleet avoided an 86% drawdown; outcome luck does not affect the class grade, but here the class grade is independently correct. Per mandate I do not reopen the review agent's cleanliness/workflow judgment (review: output_clean, packet_fidelity, workflow_correct all true, no violations). Confidence HIGH: resolution is live primary data fetched today and the grade is deterministic against the recorded rule.

### nlf_synthetic — real: SYNTHETIC winner-shape (no real company) — obscure industrial-software niche
- **Fleet reasoning:** buy (HIGH) — Buy a starter position in the high-potential sleeve — this is a quietly compounding, under-covered software business trading at a reasonable price with a real buffer.
  - falsifiers: FALSIFIERS that would kill the thesis: (1) net revenue retention falling below ~105% for two straight quarters — the switching-cost/expansion engine breaking; (2) gross margin reversing its climb or stalling well below the mid-70s; (3) revenue growth decelerating toward the low teens without a margin/cash-flow offset;
  - clock: THE CLOCK: the next validation point is the upcoming quarterly earnings print (roughly one quarter out, ~3 months) — watch retention, margin, and whether profitability holds. The full re-rating horizon is honestly a long band: 2-4 years, because this is a slow-sales-cycle, low-coverage name that re-rates only as boredo
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
- **Five-stage structured receipts:**
  - **classifier_data_sourcing:**
```json
{
  "agent_role": "calibration_classifier_sourcing",
  "model": "claude-opus-4-8",
  "output": {
    "category": "synthetic",
    "classification_rationale": "Case is explicitly synthetic: case_id 'nlf_synthetic', freeze_date null, empty source_manifest, and subject 'SYNTHETIC winner-shape (no real company) — obscure industrial-software niche'. No real issuer exists, so there is no historical calendar to anchor and no primary/market sources to gather; per the rules I do not fabricate any. Category=synthetic because it is a constructed case rather than a real-issuer freeze point (A/B/C/D). Grading=synthetic_winner because the subject specifies a 'winner-shape', not a trap. Freeze_date remains null since a synthetic packet has no real-world evidence stream to time-box, and no date may be invented. Outcome was not used to select category or grading; classification derives solely from the case metadata.",
    "confidence": "HIGH",
    "freeze_date": null,
    "grading": "synthetic_winner",
    "sourced_facts": []
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
        "evidence": "Alias 'NLF' is applied consistently throughout the payload (report_name 'fundamentals/NLF', 'news/NLF', positions 'No existing position in NLF'). The expanded label 'Nexus Lattice Fabric (alias NLF)' is itself a fabricated placeholder, not a real issuer. forbidden_terms is empty and no real company name, ticker, or identifying proper noun leaks. Business description is deliberately generic (industrial-maintenance scheduling / spare-parts forecasting software) with no de-anonymizing specifics.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Packet is flagged synthetic:true with raw_sources:[], source_manifest:[], and rescale_factor:null. There is no frozen source packet against which a scaling constant could be independently derived, and a fully fictional packet has no real historical figures to rescale. Absolute figures (revenue 420/540/720, market_cap 2800, share_price 18.4, cash 410) are internally consistent (e.g. 2800/18.4 ≈ 152M implied shares) but internal consistency is not proof of scaling, and the constant cannot be independently proven — so the check does not apply rather than being asserted as pass.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "All time references are relative/genericized: fiscal years as 'FY-3','FY-2','FY-1', quarter as 'Q0 just reported', and 'as_of':'current'. No absolute calendar years, month/day stamps, or datable anchors appear in the candidate payload, so no date leak.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Macro context is generic: 'Industrial capex steady; software multiples compressed vs prior cycle' and 'niche compounder the market has not re-rated'. No named, datable macro event (no specific rate-hike cycle, pandemic, election, or war) that would pin the packet to a period.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Fully fictional synthetic packet (synthetic:true, empty raw_sources/source_manifest, rescale_factor null, forbidden_terms empty). Aliasing is consistent with no real-identity leak; relative dates and macro references are properly genericized. The absolute-figure rescaling dimension is not_applicable because no frozen manifest exists to independently derive or verify a scaling constant — internal figure consistency was noted but is not treated as proof. No concrete leaks found, so safe_to_run=true. Company identity and eventual outcome are not inferred."
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
      "Revenue growth 22%/28.6%/33.3% and Q0 +31% YoY match annual_income_statement and latest_quarter.revenue_growth_yoy_pct.",
      "NRR 122% and '>120% for a third straight quarter' match latest_quarter.net_revenue_retention_pct and its note.",
      "Gross margin 71→73→74.5% and net income -35/-12/+28 and FCF +55 match annual_income_statement.",
      "Cash ~410 vs debt 90 (net cash) matches balance_sheet.",
      "P/S 3.9, EV/S 3.4, P/E 55, ~40% below prior overshoot match valuation block.",
      "~8% SBC and bear/bull framing match news/NLF recent_developments.",
      "1.5% sizing within constraints_rendered '~1-2% per position' and '~5% sleeve'; uncorrelated-with-glided-tech claim matches positions text.",
      "Falsifiers and THE CLOCK (next earnings ~3 months; 2-4y re-rating band flagged as hard to pin) satisfy the rendered REPORTING REQUIREMENT.",
      "output_audit.contaminated=false, calendar_years_in_output=[] confirm no date/identity leak; sanitizer safe_to_run=true, verification.ok=true."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Replay audit of case nlf_synthetic. OUTPUT CLEAN: output_audit shows contaminated=false with no calendar years/suspect terms; the rationale uses only relative anchors (FY-3/FY-2/FY-1, Q0, '~3 months', '2-4 years') and the alias NLF consistently, with no real-issuer leak. Sanitizer (stage 2) returned safe_to_run=true with verification ok. PACKET FIDELITY: every packet-derived number in the rationale reconciles exactly to the source packet — revenue growth 22/28.6/33.3%, Q0 +31% YoY, NRR 122% (>120% third straight quarter), gross margin 71→73→74.5%, net income -35/-12/+28, FCF +55, cash 410 / debt 90, P/S 3.9, EV/S 3.4, P/E 55, ~40% off prior overshoot, and ~8% SBC. WORKFLOW CORRECT: sizing (1.5%) sits inside the rendered 1-2%/position and ~5% sleeve constraints; the fleet reporting requirement is satisfied with 5 checkable falsifiers and a THE CLOCK section (next earnings ~3 months, honest 2-4y re-rating band explicitly flagged as hard to pin, which correctly lowers conviction rather than being omitted). GROUNDING: rationale is grounded step-by-step with two minor gaps — an uncited 'comparable software names 6-10x sales' peer figure, and a '25% real CGT' tax rate asserted without the Rule-1-required domain_knowledge citation (sources block is empty). These do not overturn the recommendation but are citation-discipline gaps, so grounded score = 3. I do not grade investment correctness or the expected outcome, and I claim no access to hidden reasoning — only the written rationale was assessed.",
    "violations": [
      "Uncited peer-comparable numeric claim: 'comparable software names often trade at 6-10x sales' has no packet or external-source citation (Rule 1).",
      "Tax-rate claim '25% real CGT' stated without a domain_knowledge/... citation as required by Rule 1; the supplied sources block is empty."
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
    "acted_return_pct": null,
    "benchmark_return_pct": null,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Fleet action \"buy\" matches expected_classes [\"buy\"] exactly, so under the synthetic_winner rule this is an in-class verdict scoring 1.0. No partial-credit condition applies: this is a clean in-class hit, not an F1-pass-with-falsifiers or a trap-buy-with-killing-falsifier case. The independent replay review (stage 4) found output_clean=true, packet_fidelity=true, workflow_correct=true, with grounding reconciling all packet numbers; I do not reopen that cleanliness/workflow judgment. The two flagged citation-discipline gaps (uncited \"6-10x sales\" peer comp and an uncited \"25% real CGT\" rate) were judged by the reviewer not to overturn the recommendation and do not affect the class grade. Acted-on return is not computable because answer_key.resolution is null (positioned=false), so return fields are left null; outcome luck does not affect the class grade in any case.",
    "score": 1.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": null,
    "expected_benchmark_return_pct": null,
    "expected_in_class": true,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 1.0 (in class); acted n/a; source=calibration_grader
- notes: Fleet action "buy" matches expected_classes ["buy"] exactly, so under the synthetic_winner rule this is an in-class verdict scoring 1.0. No partial-credit condition applies: this is a clean in-class hit, not an F1-pass-with-falsifiers or a trap-buy-with-killing-falsifier case. The independent replay review (stage 4) found output_clean=true, packet_fidelity=true, workflow_correct=true, with grounding reconciling all packet numbers; I do not reopen that cleanliness/workflow judgment. The two flagged citation-discipline gaps (uncited "6-10x sales" peer comp and an uncited "25% real CGT" rate) were judged by the reviewer not to overturn the recommendation and do not affect the class grade. Acted-on return is not computable because answer_key.resolution is null (positioned=false), so return fields are left null; outcome luck does not affect the class grade in any case.
