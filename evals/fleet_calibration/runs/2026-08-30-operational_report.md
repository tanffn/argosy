# Fleet-calibration report — 2026-08-30-operational.json

| Point | Category | Fleet verdict (conv, size) | In class? | Score | Acted vs benchmark |
|---|---|---|---|---|---|
| cvna_2023_reentry | A/entry | hold (MEDIUM, 0.0 shares) | N | 0.0 | +0% vs +649% |
| hyln_2021 | B/trap | hold (MEDIUM, 0.0 shares) | Y | 1.0 | +0% vs -86% |
| sdc_2021 | B/trap | hold (HIGH, 0.0 shares) | Y | 1.0 | +0% vs -100% |
| ttcf_t2 | C/exit | hold (MEDIUM, 0.0 shares) | N | 0.0 | -100% vs -100% |
| nlf_synthetic | synthetic/synthetic_winner | buy (MEDIUM, 1.5 currency) | Y | 1.0 | n/a (synthetic) |
| omk_synthetic | synthetic/synthetic_trap | hold (HIGH, 0.0 shares) | Y | 1.0 | n/a (synthetic) |

## Category subtotals
- **A**: 0.0/1 points; acting on fleet +0% vs benchmark +649% (n=1)
- **B**: 2.0/2 points; acting on fleet +0% vs benchmark -93% (n=2)
- **C**: 0.0/1 points; acting on fleet -100% vs benchmark -100% (n=1)
- **synthetic**: 2.0/2 points
- **TOTAL**: 4.0/6; acting on the fleet at every frozen point: -25% avg vs +91% benchmark (n=4)

## Conviction calibration
- HIGH: 2.0/2 (100%)
- MEDIUM: 2.0/4 (50%)

## Disqualified / not scored
### amd_2016_f1
- agent_pipeline — review failed: packet_fidelity
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Full-string scan of candidate_payload (alias, analyst_reports, constraints_rendered, positions) against all 12 denylist entries returned zero verbatim or case-insensitive substring hits. Issuer appears only as the masked entity 'Corvid Microsystems (alias CVM)'. Near-miss checked and cleared: 'internal benchmarks' is not a denylisted-vendor substring. Peers/partners are referred to only as 'dominant incumbents', 'the dominant CPU incumbent', 'BOTH major game consoles', and 'console refreshes' — no named party, no product/brand name, no foundry or JV name. Denylist terms occur ONLY in raw_sources/source_manifest, which are sanitizer-side inputs and must not be forwarded to the Trader; confirm the pipeline ships candidate_payload alone.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently reconstructed from source_manifest only (not from analyst facts), dividing each masked figure by rescale_factor 1.7: revenue 6785/1.7=3991.2 vs manifest 3,991; net loss -1122/1.7=-660.0 vs manifest -660; cash 1334.5/1.7=785.0 vs manifest 785; total debt 3842/1.7=2260.0 vs manifest 2.26B; equity deficit -700/1.7=-411.8 vs manifest -412; interest expense 272/1.7=160.0 vs manifest 160. Six manifest-anchored figures reconcile to the single stated constant within rounding — scaling is proven, not assumed. Ratios preserved exactly as claimed: revenue growth 6785/9360-1=-27.5%, prior-year 9360/9008-1=+3.9%, P/S 2898/6785=0.427≈0.43, EV/S (2898+3842-1334.5)/6785=0.797≈0.80. CAVEAT: market_cap_usd_m 2898 and share_price_usd 6.1 have NO manifest counterpart, so their scaling cannot be independently proven; they are internally consistent with the scaled financials but the implied share count (2898/6.1≈475M) is not corroborated by any supplied source. Treat those two fields as unproven-but-consistent rather than verified.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No calendar year, month, or absolute date appears anywhere in candidate_payload. Fiscal periods are relative-labelled ('FY-3', 'FY-2', 'FY-1 (latest full year, just filed)'), as_of is 'current', and all temporal anchors are offsets: '~2 weeks ago', '~two years ago', '~12-18 months from launching', 'in the prior year', '~-50% over ~2 years', 'a decade'. The absolute dates present in raw_sources/source_manifest (publication_date fields) are sanitizer-side only and do not appear in the masked payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context is stated only in unnamed, undated aggregate terms ('PC market shrinking mid-single-digits; server demand growing with cloud buildouts; semi cycle soft after an inventory correction') — no named cycle, index, or dated event. The competitor-process item is genericized to 'The dominant CPU incumbent showed a process-node cadence slip in the prior year'; the specific node geometry present in the underlying manifest entry was correctly stripped and the incumbent is unnamed. ADVISORY (not a leak, no term disclosed): the conjunction of '#2 in BOTH CPU and discrete GPU', 'semi-custom silicon in BOTH major consoles', and 'clean-sheet core, ~40% IPC' is structurally identifying to a domain-literate reader. Protocol treats only concrete term leakage as disqualifying, so this does not block the run, but the packet owner may wish to soften one of the three.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audit of candidate_payload for case_id amd_2016_f1 (synthetic=false, so rescaling is in scope and was tested). All four protocol dimensions pass and zero forbidden terms leak into the masked payload. Aliasing: clean — the issuer, both incumbents, both console platforms, the foundry/JV and all product/executive names are genericized; denylist terms survive only in the sanitizer-side raw_sources/source_manifest, which must not be forwarded. Rescaling: independently proven from source_manifest arithmetic alone at the stated constant 1.7 across six anchored figures (revenue, net loss, cash, total debt, equity deficit, interest expense), with all ratios, growth rates and multiples internally consistent to rounding; the two unanchored fields (market cap, share price) are consistent but uncorroborated by any supplied source, which is the sole reason confidence is MEDIUM rather than HIGH. Relative dates: clean — no absolute date anywhere in the payload, every anchor is an offset. Macro genericization: clean — the sector backdrop and the competitor cadence slip are stated without named parties, dates, or node geometry. safe_to_run=true. Two non-blocking recommendations for the packet owner: (1) confirm the pipeline ships candidate_payload only, since the manifest carries raw identifiers; (2) the co-occurrence of dual-market #2 positioning, dual-console semi-custom, and the clean-sheet-core claim is structurally identifying even with every term masked — consider softening one leg if de-identification strength matters more than fidelity. No verdict on the candidate's merits, issuer identity, or eventual outcome is offered or implied."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "MEDIUM",
    "grounding_evidence": [
      "Packet fundamentals/CVM balance_sheet: total_debt_usd_m 3842, cash_and_equivalents_usd_m 1334.5, stockholders_equity_usd_m -700, annual_interest_expense_usd_m 272 — reproduced in rationale_summary as '$3.8B of debt against $1.3B of cash, negative shareholder equity, $272M of annual interest'.",
      "Packet valuation.ps_trailing 0.43 — supports rationale claim 'It trades below one times sales'; no other cheapness metric is asserted.",
      "Packet FY-1 note 'GM depressed by inventory write-downs; structural GM nearer ~32%' with gross_margin_pct 27 — both figures used correctly in the structural-margin falsifier (threshold <30% for two consecutive quarters, on the deteriorating side of ~32%).",
      "Packet news/CVM: 'Still ~12-18 months from launching a CLEAN-SHEET CPU core; management claims ~40% IPC uplift in internal benchmarks' — quoted faithfully, but mis-derived into '2027-08-31' as the OUTER edge from as-of 2026-08-30 (that is the 12-month edge).",
      "Packet fundamentals/CVM latest_quarter note 'console annuity is the floor' and news 'Kept alive by semi-custom silicon powering BOTH major game consoles under long-term contracts' — grounds the rationale's 'console chip contracts give it a revenue floor'.",
      "Packet positions: 'Budget for a new high-potential-sleeve position: ~1-2% of portfolio' — output sizes at 1.0%, bottom of band, with stated justification 'chosen low because of the equity deficit and going-concern-adjacent leverage'.",
      "replay.output_audit: contaminated=false, contamination_hits=[], suspect_terms=[], calendar_years_in_output=['2026','2027'] — no freeze-era (2016/2017) or real-issuer leakage in the output.",
      "Cash trigger arithmetic verified: 1334.5 x 0.6 = 800.7, consistent with the output's claim of '~40% erosion' at a $800M threshold, and the threshold is not already breached at entry.",
      "sanitizer.output check 'absolute_figure_rescaling': 'market_cap_usd_m 2898 and share_price_usd 6.1 have NO manifest counterpart ... Treat those two fields as unproven-but-consistent rather than verified' — the P/S leg of the thesis rests on these; not charged against the Trader, who had no visibility into stage 2."
    ],
    "output_clean": true,
    "packet_fidelity": false,
    "reasoning_grounded_score": 3,
    "summary": "REPLAY AUDIT — case amd_2016_f1 (freeze 2016-02-29), Trader output reviewed against the persisted packet only.\n\nOUTPUT CLEANLINESS — PASS. The final artifact is a single well-formed JSON object matching the decision schema; `response_raw` parses identically to `output_full` with no prose, headings, or fences. No de-anonymization: the issuer appears only as \"CVM\"/masked descriptors; no real issuer, incumbent, console platform, product-family, or foundry name appears. No era leakage — the only calendar years present are 2026 and 2027 (the replay's operating \"now\" and a forward checkpoint), consistent with `output_audit.contaminated=false, contamination_hits=[]`. No post-freeze outcome knowledge is asserted anywhere: the new core is consistently described as unproven (\"no confirming turnaround print\", \"internally claimed ~40% uplift\", \"verify ... INDEPENDENT IPC benchmarks\"). `cited_sources` uses exactly the two report_names that appear verbatim in the packet (\"fundamentals/CVM\", \"news/CVM\").\n\nPACKET FIDELITY — FAIL on one decision-critical derivation, otherwise exact. Every quoted figure reconciles to the packet: debt $3,842M→\"$3.8B\"; cash $1,334.5M→\"$1.3B\" and used verbatim as the $1,334.5M baseline; interest expense $272M; negative stockholders' equity (-$700M); reported GM 27% vs the packet's own \"structural GM nearer ~32%\"; revenue $6,785M; P/S 0.43→\"below one times sales\". Sizing at 1.0% sits at the bottom of the packet's \"~1-2% of portfolio\" budget and inside the sleeve cap; instrument=stock, no options, no leverage, honoring the constraints. The $800M cash trigger is correctly declared in masked-scaled units and is ~40% below the $1,334.5M baseline as claimed (1334.5 × 0.6 ≈ 800.7), and is not already true at entry — good trigger hygiene.\n\nTHE DEFECT: the dated trigger asserts \"management guides 12-18 months, i.e. an outer edge of ~2027-08-31\". From the stated as-of of 2026-08-30, 2027-08-31 is the TWELVE-month (near) edge; the outer edge of an 12-18 month window is ~2028-02-29. The falsifier therefore converts the packet's 18-month tolerance into a 12-month hard deadline and would fire BUY->EXIT on a launch that lands squarely inside management's guided window. The packet fact is quoted faithfully but mis-derived into the actionable artifact, so I cannot certify exact fidelity.\n\nSecondary fidelity notes (minor): (a) `size_units=\"currency\"` with `size_shares_or_currency=1.0` while the prose insists the figure is PERCENT of portfolio — the typed field contradicts the narrative and a downstream executor reading the typed field alone would size 1.0 currency unit; the model flagged the ambiguity but still emitted a mismatched enum rather than leaving it uncomputable. (b) Falsifier prose attributes provenance to strings (\"company segment disclosure\", \"management's discussion\", \"the quarterly and annual filings\") that are not packet source_ids; only two source_ids exist. These read as descriptions of where a FUTURE check would be made rather than as fabricated citations, and the machine-readable `cited_sources` array is clean, so I treat this as sloppy provenance language, not a hallucinated citation.\n\nWORKFLOW — PASS. Both mandated reporting elements are present and discharged in the structured fields. FALSIFIERS: four, each specific and checkable, each with an explicit BUY->EXIT transition, each with a stated reason for its threshold and its persistence requirement (single reading for a cash level; two consecutive quarters for margin, because \"a single quarter's mix can swing margin\"). THE CLOCK: the next validation point is named (clean-sheet core shipping, 12-18 month window, checkpoint trigger emitted as a dated_event) and the re-rating horizon is given as an honest band (\"2-4 years\" in the rationale). The fourth falsifier is deliberately left qualitative with an explicit justification for why no typed trigger was emitted (\"segment-level, duration-based and part-event, so a point-trigger on consolidated revenue would mis-fire\") — that is the correct call given the packet supplies no segment split, and it is disclosed rather than silently fudged. MEDIUM confidence is defensible against a packet describing a roadmap story with no confirming print. Expected_impact correctly declines to state a currency notional and states no tax on entry with no rate claimed.\n\nREASONING GROUNDING — 3/4. Grading only the written rationale, which I can read; I make no claim about hidden reasoning. Each load-bearing step traces to a packet fact: the \"revenue floor\" rests on the packet's console-annuity language and the fundamentals note \"console annuity is the floor\"; the cheapness leg rests on the supplied 0.43 P/S; the downsizing to the band's bottom is explicitly justified by the equity deficit and leverage, both in the packet; the margin falsifier is built on the packet's own write-down caveat and the ~32% structural figure rather than on the headline 27%. Gaps that cost the fourth point: the 12-vs-18-month edge is asserted rather than computed and is wrong; the segment-revenue falsifier reasons about a line item the packet never quantifies (self-flagged, but still an extrapolation); and the P/S leg leans on market_cap/share_price, the two fields the stage-2 sanitizer explicitly logged as \"unproven-but-consistent\" with no manifest counterpart — the Trader neither knew nor could know that, so it is not charged as an error, but it does mean the cheapness claim is the least-anchored leg of the thesis.\n\nNo opinion is offered on whether buying this candidate was correct; that is outside my remit.",
    "violations": [
      "DATE DERIVATION ERROR (decision-critical): falsifier #3 states 'management guides 12-18 months, i.e. an outer edge of ~2027-08-31' and the dated_event trigger fires BUY->EXIT on 2027-08-31. From the output's own as-of of 2026-08-30 that date is the 12-month (near) edge; the outer edge of a 12-18 month window is ~2028-02-29. The trigger would exit a position on a launch that lands inside the packet's guided window.",
      "TYPED-FIELD MISMATCH: size_units='currency' with size_shares_or_currency=1.0, while expected_impact states the number is 'PERCENT OF PORTFOLIO, not a currency notional'. The machine-readable field contradicts the prose; a downstream executor reading the enum would size 1.0 currency unit.",
      "PROVENANCE LANGUAGE OUTSIDE THE SOURCE SET (minor): falsifier prose attributes facts to 'company segment disclosure', \"management's discussion\", and 'the quarterly and annual filings' — none of which are packet source_ids. Only 'fundamentals/CVM' and 'news/CVM' exist. The cited_sources array itself is clean, so this is loose wording rather than a fabricated citation.",
      "UNQUANTIFIED LINE ITEM (minor): falsifier #4 sets a '>25% YoY for two consecutive quarters' condition on semi-custom/console SEGMENT revenue, which the packet never breaks out; only consolidated revenue of $6,785M is supplied. Self-disclosed as qualitative, which mitigates but does not remove the extrapolation."
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": 5357.0,
    "benchmark_return_pct": 5357.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case amd_2016_f1 (category A, grading rule F1_lenient, positioned=false). VERDICT CLASS: the fleet emitted action=\"buy\" (grading_input.fleet_result.action), and expected_classes is exactly [\"buy\"] (grading_input.answer_key.expected_classes) — a direct in-class match, so score = 1.0 per the rule \"use 1.0 for an in-class verdict\". The 0.5 partial-credit branch does not apply: it is reserved for verdicts OUTSIDE the expected class that the benchmark rule rescues (an F1 pass carrying falsifiers, or a small trap buy carrying the killing falsifier). This is not a trap case (expected class is buy) and no pass/abstain was issued, so no downgrade path is triggered. RETURN: acted_return_pct = benchmark_return_pct = 5357.0% because the acted class matches the benchmark class and the position was a long entry held over the stated horizon; the resolution supplies price_at_freeze_real 2.14 (2016-02-29 close, post FY2015 10-K filed 2016-02-18) and price_at_horizon_real 116.78 at the 6y (2022-02-01) horizon, i.e. 116.78/2.14 - 1 = +5357% (grading_input.answer_key.resolution, yfinance adjusted closes retrieved 2026-07-11 — single secondary vendor source, but the freeze/horizon prices are internally consistent with the stated benchmark_return_pct). Sizing was 1.0 at the bottom of the sanctioned high-potential sleeve band, which affects portfolio impact but not the class grade. DEFECTS NOTED, NOT CHARGED: the stage-4 calibration_reviewer returned packet_fidelity=false on a decision-critical date derivation (falsifier #3 treats 2027-08-31 as the outer edge of a 12-18 month window measured from 2026-08-30, when that is the 12-month near edge; the outer edge is ~2028-02-29), plus a typed-field mismatch (size_units=\"currency\" against prose asserting percent-of-portfolio) and two minor provenance/extrapolation flags. Per my remit I do not reopen the reviewer's cleanliness/workflow judgment (output_clean=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true), and the F1_lenient rule keys the class grade to the emitted action, not to falsifier hygiene — these defects are recorded for the audit trail and would matter to trigger-execution quality, but they do not move the class score. OUTCOME LUCK: the extraordinary +5357% realized return is reported as resolution data only and plays no part in the grade; the same \"buy\" would have scored 1.0 against a losing resolution. CONFIDENCE HIGH: class comparison and return arithmetic are both fully determined by the supplied answer_key and fleet_result fields, with no missing facts requiring domain-refresh investigation.",
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
### boot_2017
- agent_pipeline — review failed: workflow_correct
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Scanned every string in candidate_payload (alias, positions, constraints_rendered, both analyst reports, business_description, recent_developments, balance-sheet note). No denylisted company name, ticker, acquired-subsidiary name, banner name, or founder/executive surname appears verbatim, hyphenated, spaced, or case-folded. The company is referred to only by the two-word neutral alias plus the alias code. No URLs, CIK numbers, or filing identifiers from raw_sources/source_manifest bled into the payload — the payload contains zero citations. Only near-collision: the generic product noun for the footwear category (used twice in the business description) shares a stem with one denylisted ticker string; this is unavoidable category vocabulary and is not a standalone identifier. Separately, case_id contains a category stem plus a year and must be withheld from the Trader — it is harness metadata, not part of candidate_payload.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven from source_manifest + masked payload only, no external lookup. Declared constant 2.1. Manifest item 1 (regulatory filing exhibit): full-year net sales x2.1 = 1322.6 vs payload revenue_usd_m 1323; GAAP net income x2.1 = 29.8 vs payload net_income_usd_m 30; adjusted net income x2.1 = 31.3 vs payload adjusted_net_income_usd_m 31. Manifest item 2 (secondary press): Q4 sales x2.1 = 342.3 vs payload latest_quarter revenue_usd_m 342. All four within rounding tolerance, and no manifest dollar figure appears unscaled in the payload. Consistency cross-checks internal to the payload: 357/30 = 11.9 = stated pe_trailing; 357/1323 = 0.270 = stated ps_trailing; (357+462-32)/1323 = 0.595 = stated ev_s_trailing; 1323/1195 = 1.107 = stated revenue_growth_pct 10.7, confirming the FY-2 revenue line carries the same constant. Ratios, margins and growth rates are unchanged by a uniform scalar, as the provenance note claims. Caveat recorded: cash, total debt, share price and the two quarterly EPS figures have no manifest counterpart, so their scaling is consistent but not independently provable; none of them matches a plausible unscaled original, so no leak is indicated.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "All temporal anchors are relative: fiscal periods are labelled 'FY-2' and 'FY-1 (just reported)', the quarter is 'Q4 / FY just reported', as_of is 'current', and guidance is described as 'next-year guide' / 'guide for next year is flat SSS'. No calendar year, month name, ISO date, fiscal-year-end date, or year-bearing quarter label appears anywhere in candidate_payload. The dated material (2017-06-01 / 2017-06-02 publication dates in raw_sources and source_manifest) is confined to the harness-side blocks and does not appear in the payload the Trader receives.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Broad de-rating of store-based specialty retail; soft apparel traffic; energy/ag end-markets uneven' — a described condition with no named episode, no year, no index or index level, and no policy/rate event. The business_description and bear case refer to competitors only generically ('large ecommerce platforms winning', 'e-comm share gains by giants', 'malls dying'); no mega-cap competitor is named. The commodity linkage from manifest item 2 is genericized to 'energy/ag end-markets' rather than reproducing the source's specific commodity-price framing. No macro reference narrows the packet to a datable window.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audited the masked payload against all four protocol dimensions. (1) ALIAS: no denylisted name, ticker, subsidiary, or person-name appears verbatim or in near-form anywhere in candidate_payload; the payload uses only the neutral alias and a generic two-word descriptor. The only collision is the generic product-category noun used in the business description (western/work footwear), which is category vocabulary rather than the denylisted ticker string, and cannot be removed without destroying the thesis. Note for the harness (not a payload defect): the case_id field itself carries a category word plus a four-digit year, so case_id must not be forwarded to the Trader. (2) RESCALING: independently proven from source_manifest only. Manifest item 1 gives full-year net sales, GAAP net income and adjusted net income; manifest item 2 gives Q4 sales. Applying the declared constant 2.1: sales -> 1322.6 vs payload 1323; GAAP NI -> 29.8 vs payload 30; adj NI -> 31.3 vs payload 31; Q4 sales -> 342.3 vs payload 342. All four land within rounding, and no raw dollar figure from the manifest survives unscaled anywhere in the payload. Internal consistency also holds independently: market cap / net income = 357/30 = 11.9 matches the stated trailing P/E; market cap / revenue = 357/1323 = 0.270 matches the stated P/S; (357 + 462 - 32)/1323 = 0.595 matches the stated EV/S; and 1323/1195 = 1.107 matches the stated 10.7% growth, so the prior-year revenue line is consistent with the same constant. Balance-sheet items (cash, total debt) and the quarterly EPS figures have no manifest counterpart, so their scaling is asserted, not proven — but they are ratio-consistent with the scaled figures and none of them equals a plausible unscaled original. (3) RELATIVE DATES: every temporal reference is relative — FY-2, FY-1 (just reported), Q4 / FY just reported, as_of \"current\", next-year guide. No calendar year, month, quarter-with-year, or fiscal-year-end date appears. (4) MACRO GENERICIZATION: the macro block describes a de-rating of store-based specialty retail, soft apparel traffic, and uneven commodity end-markets without naming any year, index level, named market episode, or named mega-cap competitor (large e-commerce players are referred to only in the generic). RESIDUAL RISK (flagged, not disqualifying): the payload retains an unscaled physical-footprint count (~220 doors, ~30 states) alongside a narrow category description; footprint counts are not covered by the dollar-rescaling rule, but the combination is a moderately strong fingerprint for a determined searcher. Recommend the harness consider bucketing or scaling store counts in future packets. Verdict: no concrete leak, scaling independently proven on all manifest-backed figures, dates and macro clean — safe_to_run = true."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Packet fundamentals/POT revenue_growth_pct=10.7 -> rationale 'sales grew 10.7%'",
      "Packet same_store_sales_pct=0.3 (FY-1) and latest_quarter.same_store_sales_pct=-0.9 -> rationale 'same-store sales were roughly flat, not collapsing' and falsifier baseline '-0.9% for the quarter and +0.3% for the full FY-1'",
      "Packet valuation.ps_trailing=0.27 and pe_trailing=11.9 -> 'about a quarter of one year's sales and under 12 times profit'",
      "Packet note 'adj EPS down ~20% YoY despite sales growth' -> 'adjusted earnings fell about 20% despite the growth'",
      "Packet balance_sheet total_debt_usd_m=462, cash_and_equivalents_usd_m=32 -> '$462M of debt against $32M of cash' and derived net debt '$430M = $462M total debt less $32M cash'",
      "Packet net_income_usd_m=30 on revenue_usd_m=1323 -> falsifier baseline 'net margin ... 2.3% = $30M on $1,323M' (30/1323 = 2.27%)",
      "Packet FY-2 net_income_usd_m=21 vs FY-1 30 -> data-limitation note 'GAAP net income rose from $21M to $30M while adjusted earnings per share fell about 20% — these cannot be reconciled'",
      "Packet news/POT 'rising exclusive-brand mix' and 'full-price selling discipline' -> structural falsifier on markdown-driven selling and declining exclusive-brand penetration",
      "Packet report_name values 'fundamentals/POT' and 'news/POT' appear verbatim in cited_sources; no other ids cited",
      "constraints_rendered '~1-2% per position' and sleeve cap '~5%' -> concentration_delta 'POT 0% -> ~1.0% ... sleeve stays well inside its ~5% cap'",
      "replay.output_audit: contaminated=false, calendar_years_in_output=[], contamination_hits=[], suspect_terms=[]",
      "constraints_rendered: 'THE CLOCK — the NEXT VALIDATION POINT ... and the expected re-rating horizon as an honest band' — no corresponding band or unestimable declaration exists anywhere in output_full"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Output is clean and packet-faithful but fails one explicit constraint. CLEANLINESS: the visible output contains no real issuer name, ticker, calendar year, or leaked URL; it uses only the alias POT and relative fiscal labels, matching the replay's own output_audit (contaminated=false, calendar_years_in_output=[], zero contamination_hits). The dated harness-side sources (2017-06-01/2017-06-02 filing exhibit and trade press) never surface in output_full or response_raw. FIDELITY: every numeric claim maps to a packet field — 10.7% revenue growth, +0.3% FY-1 SSS and -0.9% Q4 SSS, 0.27 P/S, 11.9 P/E, ~20% adjusted-EPS decline, $462M debt vs $32M cash, and the two correctly derived figures ($430M net debt = 462-32; 2.3% net margin = 30/1323). cited_sources are verbatim packet report_name strings ('fundamentals/POT', 'news/POT'); no fabricated ids. The tax line correctly refuses to assert a rate because no domain_knowledge file was supplied — consistent with the citation rule rather than a gap. Sizing (~1.0% of NAV, inside the 1-2%/position and ~5% sleeve caps), stock-only, no leverage and no options all honour constraints_rendered. WORKFLOW: the FALSIFIERS half of the reporting requirement is satisfied in depth (four checkable falsifiers, each with named source, as-of baseline, forward-only threshold and BUY->EXIT transition, plus an honest data-limitation note). THE CLOCK half is not satisfied: there is no dated or estimated next validation point (both revisit_triggers carry date=null; the only gesture is 'the next two same-store-sales prints' in the rationale, undated and unestimated), and no re-rating horizon band appears anywhere — nor the required explicit statement that the horizon is unestimable. The constraint states this is not optional, so workflow_correct=false. Secondary defects: size_units='currency' carries a percent-of-NAV value of 1.0 (self-flagged in expected_impact, which mitigates but does not cure the field-semantics mismatch), and the second trigger attaches a HOLD->ADD transition with an underived 9.00 price to a BUY-action packet. GROUNDING (visible rationale only; no claim of access to hidden reasoning): the written chain is step-by-step traceable — bear narrative from news/POT, 'roughly flat, not collapsing' from the two SSS figures, cheapness from the stated multiples, the cost-outgrowing-sales risk from revenue +10.7% against adj EPS -20%, and the leverage caution from 462/32. The GAAP-vs-adjusted irreconcilability ($21M->$30M net income against a ~20% adj-EPS decline) is a genuine packet-derived observation and correctly drives MEDIUM rather than HIGH. Minor gaps keep this at 3: the 9.00 add-on trigger and the $500M net-debt threshold's '~$70M' headroom framing are asserted without a stated derivation method, and 'paid well to be wrong' is a qualitative leap beyond the cited multiples.",
    "violations": [
      "CLOCK requirement unmet: no re-rating horizon band reported (e.g. '3-6 months' / '2-3 years') and no explicit statement that the horizon is unestimable, despite constraints_rendered stating 'A hard-to-estimate horizon is NOT optional to report'.",
      "NEXT VALIDATION POINT not dated or estimated: both revisit_triggers have date=null; the only forward anchor is the undated prose phrase 'the next two same-store-sales prints are the first real test'.",
      "Field-semantics mismatch: size_units='currency' with size_shares_or_currency=1.0 actually denotes percent-of-NAV, disclosed in expected_impact ('not a dollar notional... Do not read it as $1') rather than typed correctly.",
      "Trigger-state inconsistency: decision_transition 'HOLD->ADD' is attached to a packet whose action is 'buy', and the 9.00 price_below threshold (vs share_price_usd 13.23) is stated without any derivation."
    ],
    "workflow_correct": false
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": 1855.0,
    "benchmark_return_pct": 1855.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case boot_2017 (Category A, entry grading, positioned=false). Expected classes = [\"buy\"]; fleet verdict action = \"buy\" → in-class. Per the recorded grading rule, an in-class verdict scores 1.0; the 0.5 partial-credit carve-outs (F1 pass with falsifiers; small trap buy carrying the killing falsifier) do not apply here — this is not a trap case, so no reduction is warranted. The calibration reviewer's workflow finding (workflow_correct=false: missing CLOCK re-rating horizon band and undated next-validation point, size_units field-semantics mismatch, HOLD->ADD trigger on a buy packet) is recorded and left undisturbed per instruction; it does not alter the verdict-class grade, and the reviewer independently confirmed output_clean=true, packet_fidelity=true, contamination-free replay audit. Returns (from answer_key.resolution, grading input): freeze price $6.30 → horizon price $123.15 over 7y (2024-06-03) = +1855% benchmark return [source: answer_key.resolution, boot_2017; horizon close fetched 2026-07-11 via yfinance]. Because the action was a long entry from an unpositioned base, acted-on return equals the benchmark return at +1855% on the ~1.0% NAV starter position; outcome luck is noted only for the record and does not affect the class grade. Confidence HIGH: verdict-vs-expected-class comparison and the resolution arithmetic are fully determined by the supplied inputs, with no missing facts requiring domain-refresh escalation.",
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


## Horizon calibration (§2b clock band)
- unestimable_stated: 1
- not_applicable: 3
- no_band: 2
- cvna_2023_reentry: no_band (stated=none; actual_months=None)
- hyln_2021: no_band (stated=none; actual_months=None)
- sdc_2021: unestimable_stated (stated=unestimable; actual_months=None)
- ttcf_t2: not_applicable (stated=24-36 mo (re-rating horizon 2-3 years); actual_months=None)
- nlf_synthetic: not_applicable (stated=24-36 mo (2-3 years); actual_months=None)
- omk_synthetic: not_applicable (stated=24-36 mo (2-3 years); actual_months=None)

## Per-point detail (section 2b three-column + clock)
### cvna_2023_reentry — real: CVNA @ 2023-08-01 ($49.11 as-traded); re-entry after 2023-07-19 debt exchange + record GPU/Adj EBITDA print
- **Fleet reasoning:** hold (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Full-string scan of candidate_payload (analyst_reports, constraints_rendered, positions) finds zero verbatim or near-verbatim occurrences of any forbidden_terms entry. Identity-bearing specifics are genericized instead of named: the founder is rendered as 'Founder-controlled', the acquired auction business as 'a physical wholesale-auction network acquired at the prior cycle peak', and the retail format detail is absent entirely. candidate_payload contains no URLs or citation strings, so no source hostname from source_manifest is exposed to the Trader. RESIDUAL RISK (documented, non-blocking, not a concrete leak of the subject): the 3-letter alias is a character-prefix of one denylisted term and coincides with the real ticker of an unrelated listed issuer; it points away from, not at, the masked subject, but a less collision-prone alias is recommended for future packets.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven solely by dividing each masked dollar figure in candidate_payload by its source_manifest counterpart; declared rescale_factor = 1.15. source_manifest[0]: revenue 3413 -> quotient 1.1499; total GPU 7498 -> 1.1500; Adjusted EBITDA 178 -> 1.1484 (rounding); GAAP net loss -121 -> 1.1524 (rounding). source_manifest[1]: debt reduction >1380 -> 1.1500; cash-interest saving >494/yr -> 1.1488 (rounding). source_manifest[2]: cash_and_equivalents_usd_m 779 -> 1.1507 (rounding). All 7 manifest-covered dollar figures land on 1.15 within integer-rounding tolerance; unscaled ratios are preserved exactly (revenue_growth_yoy_pct -24 and units -35% match source_manifest[0] verbatim, as the convention requires). NOT INDEPENDENTLY PROVABLE (no manifest counterpart): market_cap_usd_m 10652, share_price_usd 56.48, total_debt_usd_m 5980, and both annual_income_statement rows. These are internally consistent rather than proven: 15645/14736 = 1.0617 reproduces the stated 6.2% growth; (10652 + 5980 - 779)/15645 = 1.013 approximates the stated ev_s_trailing 1.01; 10652/15645 = 0.681 reproduces ps_trailing 0.68. ANOMALY: retail_units 75840 vs source_manifest[0] 76,530 gives quotient 0.9910 — neither parity nor 1.15. Unit counts fall outside the payload's declared dollar-only scaling convention, so this is an undeclared perturbation; it is immaterial to every disclosed ratio (no gross-profit figure is published that GPU x units could contradict), but it leaves the count within 1% of the manifest value, a weak searchability vector worth tightening.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "candidate_payload contains no absolute date, year, or quarter-year token. Period references are exclusively relative: annual_income_statement labelled 'FY-2'/'FY-1'; as_of 'current'; latest_quarter.which 'Q2, reported ~2 weeks ago'; business_description 'Two years ago the firm looked like a survival risk'; recent_developments 'the information set that said survival risk a year ago'; valuation 'the prior cycle high'; interest relief 'for two years'. The explicit maturity years carried in source_manifest[1] are correctly suppressed and rendered as 'the near-term maturity wall' / 'extend maturities', so the manifest's strongest date anchor does not reach the Trader.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Used-vehicle prices off the shock-era peak; auto retail demand soft; credit tightening still in the background' — no named macro event, no named central bank or policy action, no dated cycle. Corroborating genericizations elsewhere: business_description 'acquired at the prior cycle peak'; valuation.price_action '~-90% from the prior cycle high'; fundamentals FY-1 note 'debt-spiral year' with no external cause named. The macro backdrop is described only by direction and state, which is what a Trader needs for the decision and is not sufficient to date the packet.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audited the exact candidate_payload against the four protocol dimensions; no fetching performed, rescaling proven only from source_manifest arithmetic. (1) ALIAS — no denylisted term appears verbatim or in near-verbatim form anywhere in candidate_payload; identifying specifics are genericized (\"founder-controlled\", \"physical wholesale-auction network acquired at the prior cycle peak\"). The payload also carries no URLs at all, so no manifest hostnames reach the Trader. Residual (non-blocking) risk: the 3-letter alias is a prefix of one denylisted term and collides with a real, unrelated listed ticker — recommend a less collision-prone alias in future packets; it does not identify the subject. (2) RESCALING — independently verified at 1.15 for every masked dollar figure with a manifest counterpart (7/7, all within rounding). Market cap, share price, total debt, and both FY income-statement rows have no manifest counterpart and are therefore not independently provable; they are internally consistent (FY-1/FY-2 revenue quotient reproduces the stated 6.2% growth; EV/S and P/S reproduce from the stated cap, debt, cash and FY-1 revenue). One anomaly: retail_units is a non-dollar count outside the declared scaling convention and sits ~0.9% below the manifest value rather than at parity or ×1.15 — immaterial to any stated ratio (gross profit is not disclosed in the payload) but undeclared, and it leaves the count within 1% of the true figure, a weak searchability vector. (3) RELATIVE DATES — payload is fully relative (\"FY-1\", \"FY-2\", \"as_of: current\", \"Q2, reported ~2 weeks ago\", \"two years ago\", \"a year ago\"); no absolute year appears, and the manifest's explicit maturity years are correctly genericized to \"near-term maturity wall\". (4) MACRO GENERICIZATION — macro framing uses \"shock-era peak\", \"prior cycle peak/high\", \"credit tightening still in the background\"; no named macro event, policy body, or dated cycle. Verdict: safe_to_run = true, with two documented residual risks (alias-string collision; near-true unit count). Separately noted for the packet owner, not scored here: the business description is granular enough that a well-informed reader could recognize the issuer independent of the alias — inherent to this masking design, not a protocol violation. Confidence MEDIUM: rescaling is proven for the manifest-covered figures but five absolute figures are manifest-uncovered and can only be shown internally consistent, not independently scaled."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Packet fundamentals/APO latest_quarter: adjusted_ebitda_usd_m 178, gaap_net_income_usd_m -121, total_gpu_usd 7498, retail_units 75840, revenue_growth_yoy_pct -24 — all four reproduced verbatim in output falsifiers and triggers.",
      "Packet balance_sheet: total_debt_usd_m 5980, cash_and_equivalents_usd_m 779 → output '~$5.2B of net debt' (5980-779=5201). Verified.",
      "Packet market_cap_usd_m 10652 + share_price_usd 56.48 → output '~22x enterprise-value-to-annualized-adjusted-EBITDA': (10652+5980-779)/(178*4)=22.3x. Verified.",
      "Output price trigger '$40 ... ~18x vs ~22x at the current $56.48': 10652*(40/56.48)=7544; (7544+5980-779)/712=17.9x. Verified against packet inputs.",
      "Packet news/APO: 'slash near-term cash interest by >$494M/yr for two years' → output 'The interest relief only runs two years' and the '2-4 years' horizon rationale.",
      "Packet news/APO: 'units fell ~35% YoY' → output falsifier baseline 'units down ~35% YoY'.",
      "constraints_rendered REPORTING REQUIREMENT (falsifiers + NEXT VALIDATION POINT + honest horizon band) → satisfied by the falsifiers array and the dated_event trigger labelled 'ESTIMATED next validation point ... not confirmed ... Expected re-rating horizon: 2-4 years'.",
      "constraints_rendered 'No leverage, no options' → output instrument 'stock', order_type 'market', size 0, no derivative construct.",
      "sanitizer.output.safe_to_run = true and verification.ok = true, stage 2 — packet cleared before the trader consulted it.",
      "replay.output_audit: contaminated=false, contamination_hits=[], suspect_terms=[], calendar_years_in_output=['2026'] — consistent with my own scan of response_raw.",
      "Divergence: packet says 'first strongly positive Adjusted EBITDA quarter'; output rationale_summary says 'first solidly positive operating cash profit' — while output falsifier 5 states it 'cannot verify how much of the $178M survives interest and capital expenditure'."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "REPLAY AUDIT — case cvna_2023_reentry (masked packet \"APO\"), stage-2 sanitizer PASS (safe_to_run=true, verification.ok=true), trader verdict HOLD / size 0.\n\n(1) OUTPUT CLEANLINESS — PASS. `response_raw` is exactly one JSON object: no prose, headings, or code fences before or after. No real issuer name, ticker, founder name, or source hostname appears anywhere in the output; the trader never touched the real 2023 `sources` URLs (which were not in its packet) and never cited them. The only calendar tokens are 2026 (\"2026-08-30\", \"2026-11-10\", \"~Nov 2026\"), which are the run-environment current date and a forward estimate — not leakage of the 2023 freeze date; this matches replay.output_audit (contaminated=false, contamination_hits=[], suspect_terms=[]).\n\n(2) PACKET FIDELITY — PASS on every numeric. Verbatim matches: share price $56.48; total debt $5,980M; cash $779M; Adj EBITDA +$178M; GAAP net income -$121M; total GPU $7,498; retail units 75,840; units -35% YoY; revenue -24% YoY; \"two years\" of cash-interest relief. Derived figures reproduce exactly from packet inputs: net debt 5980-779 = $5,201M ≈ \"$5.2B\"; EV = 10,652+5,980-779 = 15,853, annualized Adj EBITDA = 178x4 = 712, EV/EBITDA = 22.3x ≈ \"~22x\"; at $40 the implied cap is 10,652x(40/56.48) = 7,544, EV 12,745, = 17.9x ≈ the stated \"~18x\". No figure was invented and none contradicts the packet. Constraint fidelity holds: stock only (no leverage, no options), no position initiated, ~1-2% sleeve budget explicitly preserved, Israeli-tax line correctly says \"no disposal, no capital-gains event\". Two minor, non-numeric nits logged in violations.\n\n(3) WORKFLOW — PASS. Sanitizer ran first (stage 2) and cleared the packet before the trader consulted it; the trader evaluated strictly on the masked payload and did not attempt de-anonymization or treat the alias as a data defect, as the provenance note requires. The fleet REPORTING REQUIREMENT is satisfied in both limbs: FALSIFIERS are checkable, each carries a source, an as-of baseline, and an explicit forward-only proof that the threshold is not already met (units shrinking vs. required growth; debt $5,980M vs. $5,000M; Adj EBITDA +$178M vs. <$0; GPU $7,498 vs. <$6,000). THE CLOCK is recorded with a next validation point (~Nov 2026 Q3 print), openly labelled ESTIMATED and \"not confirmed\", plus an honest re-rating band (\"2-4 years\") tied to a stated mechanism (two-year interest-relief window + one deleveraging cycle). Emitted revisit_triggers are internally consistent with the falsifier text: the two compound/multi-quarter falsifiers are declared QUALITATIVE and correctly emit no point-trigger, and none of the three emitted triggers is already armed at baseline. Confidence MEDIUM is declared and justified by a named data gap (no FCF or post-exchange cash-interest schedule), which is the correct band under the fleet rule rather than HIGH.\n\n(4) REASONING GROUNDING (visible rationale only; I make no claim about hidden reasoning) — 3/4. Every load-bearing step in the written rationale traces to a packet fact and every derived multiple reconciles arithmetically, and the output volunteers its own limitation (GAAP still -$121M, Adj EBITDA unbridged to cash). One genuine gap: the rationale_summary describes the quarter as \"its first solidly positive operating cash profit\", whereas the packet states \"first strongly positive Adjusted EBITDA quarter\" — Adjusted EBITDA is not cash, and the output's own fifth falsifier concedes it cannot verify how much of the $178M survives interest and capex. Grounded with a minor self-contradicting gloss, hence 3 rather than 4.\n\nI graded only cleanliness, fidelity, workflow, and grounding; I express no view on whether HOLD was the right investment call.",
    "violations": [
      "MINOR — citation hygiene: cited_sources includes 'positions_snapshot (no existing APO position; high-potential sleeve budget ~1-2% of portfolio)'. The packet's field is named 'positions', and 'positions_snapshot' appears nowhere verbatim in the supplied inputs. The content of the gloss is accurate to the packet, so this is a constructed-id nit rather than a hallucinated fact, but under the verbatim-source-id rule the id should have been copied exactly. The other two ids ('fundamentals/APO', 'news/APO') match packet report_name exactly.",
      "MINOR — imprecise gloss in the user-facing summary: 'posted its first solidly positive operating cash profit' restates the packet's Adjusted EBITDA as cash profit. Adjusted EBITDA is explicitly non-GAAP and unbridged to cash, as the output's own fifth falsifier concedes; GAAP net income remains -$121M. Disclosed elsewhere, but the summary sentence itself overstates the packet.",
      "MINOR — label consistency: falsifier 4 is tagged 'HOLD->SELL' although no position exists to sell. Falsifier 3 defines the convention ('confirms permanent avoid, drop from the candidate list') and falsifier 4 relies on it implicitly without restating; harmless but under-specified."
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": 0.0,
    "benchmark_return_pct": 648.9,
    "confidence": "HIGH",
    "in_expected_class": false,
    "rationale": "ENTRY-GRADED CASE cvna_2023_reentry (category A). Expected class = [\"buy\"] (answer_key.expected_classes). Fleet verdict = \"hold\", size 0.0 shares, positioned=false (fleet_result.action/size, top-level positioned). A hold at zero size is not an entry, so the verdict falls outside the expected class: in_expected_class=false.\n\nSCORE = 0.0. The recorded benchmark rule grants 1.0 only for an in-class verdict and 0.5 only where explicitly warranted — an F1 (avoid-class) pass carrying falsifiers, or a small trap buy that names the killing falsifier. Neither applies: this is an entry-graded buy case, the fleet took no position at all, and no partial-credit clause covers a no-entry on a buy expectation. The trader's falsifier discipline and dated next-validation point are quality features that the reviewer credited under workflow, but under this grading rule they do not convert a missed entry into partial credit.\n\nACTED RETURN: size 0.0 with no position opened ⇒ zero capital deployed ⇒ acted_return_pct = 0.0. BENCHMARK RETURN = 648.9% over the 2y horizon to 2025-08-01, from price_at_freeze_real 49.11 to price_at_horizon_real 367.78 (answer_key.resolution; as-traded reconstruction ×5 to reverse the May-2026 forward 5:1 split adjustment, fetched 2026-07-11). Shortfall vs. benchmark = 648.9 pp of forgone return, but per the grading contract outcome luck does not change the class grade — the 0.0 rests on the verdict being out of class, not on the size of the subsequent move.\n\nREVIEW NOT REOPENED: the calibration_reviewer's findings (output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, three MINOR violations, confidence HIGH; review.verification.ok=true) are accepted as recorded and are orthogonal to the class grade. The reviewer itself expressly declined to opine on whether HOLD was the right call, which is the judgment scored here.\n\nCONFIDENCE HIGH: every input needed for the grade (expected_classes, action, size, positioned, resolution prices and horizon) is present verbatim in the supplied grading input; the return arithmetic is reproducible from price_at_freeze_real and price_at_horizon_real, and no external or stale data was required. No fact was missing, so no domain-refresh referral is warranted.",
    "score": 0.0
  },
  "stage": 5,
  "verification": {
    "expected_acted_return_pct": 0.0,
    "expected_benchmark_return_pct": 648.9,
    "expected_in_class": false,
    "mismatches": [],
    "ok": true
  }
}
```
- **Agent score:** 0.0 (OUT of class); acted +0% vs benchmark +649%; source=calibration_grader
- notes: ENTRY-GRADED CASE cvna_2023_reentry (category A). Expected class = ["buy"] (answer_key.expected_classes). Fleet verdict = "hold", size 0.0 shares, positioned=false (fleet_result.action/size, top-level positioned). A hold at zero size is not an entry, so the verdict falls outside the expected class: in_expected_class=false.

SCORE = 0.0. The recorded benchmark rule grants 1.0 only for an in-class verdict and 0.5 only where explicitly warranted — an F1 (avoid-class) pass carrying falsifiers, or a small trap buy that names the killing falsifier. Neither applies: this is an entry-graded buy case, the fleet took no position at all, and no partial-credit clause covers a no-entry on a buy expectation. The trader's falsifier discipline and dated next-validation point are quality features that the reviewer credited under workflow, but under this grading rule they do not convert a missed entry into partial credit.

ACTED RETURN: size 0.0 with no position opened ⇒ zero capital deployed ⇒ acted_return_pct = 0.0. BENCHMARK RETURN = 648.9% over the 2y horizon to 2025-08-01, from price_at_freeze_real 49.11 to price_at_horizon_real 367.78 (answer_key.resolution; as-traded reconstruction ×5 to reverse the May-2026 forward 5:1 split adjustment, fetched 2026-07-11). Shortfall vs. benchmark = 648.9 pp of forgone return, but per the grading contract outcome luck does not change the class grade — the 0.0 rests on the verdict being out of class, not on the size of the subsequent move.

REVIEW NOT REOPENED: the calibration_reviewer's findings (output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, three MINOR violations, confidence HIGH; review.verification.ok=true) are accepted as recorded and are orthogonal to the class grade. The reviewer itself expressly declined to opine on whether HOLD was the right call, which is the judgment scored here.

CONFIDENCE HIGH: every input needed for the grade (expected_classes, action, size, positioned, resolution prices and horizon) is present verbatim in the supplied grading input; the return arithmetic is reproducible from price_at_freeze_real and price_at_horizon_real, and no external or stale data was required. No fact was missing, so no domain-refresh referral is warranted.

### hyln_2021 — real: Hyliion (HYLN) @ 2021-06-01 ($10.61); obscure Class-8 e-powertrain blank-check-merger winner-shape with $0 revenue; -86% by mid-2024
- **Fleet reasoning:** hold (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Full-string scan of candidate_payload (alias, analyst_reports, constraints_rendered, positions) returns zero occurrences of any of the 6 forbidden_terms or their morphological variants. Entity is consistently 'VoltHaul Systems (alias VHS)'. Sensitive referents are genericized rather than dropped: flagship product -> 'range-extender electric architecture (natural-gas generator + battery)' and 'flagship long-haul architecture'; OEM chassis partner -> 'chassis deliveries for demos'; sponsor/SPAC vehicle -> 'blank-check merger months ago'; CEO -> 'charismatic founder-CEO'. The retained token 'Hybrid' is a generic powertrain word and is not on the denylist. No URL, CIK, exchange ticker or filing accession number appears in the payload (the real URLs exist only in raw_sources/source_manifest, which are sanitizer-side and not forwarded). Residual, non-blocking: the descriptor set (Class-8 heavy trucks + gas-generator range extender + recent blank-check listing + zero revenue) is a narrow enough fingerprint that expert re-identification is plausible; flagged, not failed, since no concrete identifier is present.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven independently from source_manifest only. Manifest fact dated 2021-05-17 (https://www.sec.gov/Archives/edgar/data/1759631/000162828021010423/hyln-20210331.htm) gives: net loss -16.6, R&D 9.3, SG&A 7.4, cash 335, investments ~297 (USD m). Applying the declared constant 0.85 with round-to-integer: 16.6*0.85=14.11 -> payload -14 MATCH; 9.3*0.85=7.905 -> payload 8 MATCH; 7.4*0.85=6.29 -> payload 6 MATCH; 335*0.85=284.75 -> payload 285 MATCH; 297*0.85=252.45 -> payload 252 MATCH. Revenue 0 and total_debt 0 are scale-invariant and consistent. 5/5 manifest-covered figures confirm a single uniform 0.85 constant; no manifest figure appears in the payload at its raw 1.0x value. NOT PROVEN (outside manifest coverage, flagged rather than asserted): market_cap_usd_m 1530, share_price_usd 9.02, and annual_income_statement FY-1 net_income_usd_m -60 have no manifest counterpart, so their scaling is neither confirmable nor refutable from the frozen packet; internal consistency holds (1530/9.02 implies ~169.6m shares, and a uniform constant leaves implied share count invariant, so no contradiction is detectable). Recommend the domain-refresh/packet owner extend source_manifest to cover market cap, share price and the prior fiscal year.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No calendar year, month name, ISO date or year-qualified fiscal period appears anywhere in candidate_payload. All temporal anchors are relative or period-relative: 'FY-1', as_of 'current', 'Q1, 10-Q filed ~2 weeks ago', 'in the latest quarter', 'Went public via a blank-check merger months ago', 'guides first Hybrid revenue in the back half of the year', 'year-end demos', 'prior-year ... warrant accounting'. The bare 'Q1' carries no year and is bounded by the relative filing phrase, so it does not resolve to an absolute date. The reporting requirement in constraints_rendered asks for horizons as bands ('3-6 months', '2-3 years'), which is date-relative by construction.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The restatement event is stated only as 'Restating prior-year blank-check warrant accounting (industry-wide issue); delayed some financial commentary' and, in macro_context, as 'blank-check complex starting to re-rate as warrant accounting and lockups hit'. Relative to the manifest fact dated 2021-05-19 (https://www.ttnews.com/articles/hyliion-q1-results-lack-revenue-product-development-accelerates), the payload strips the identifying specifics: no regulator named, no statement date, and the '~80 affected vehicles' count is dropped — that count is a datable fingerprint and its removal is the correct call. 'SPAC' is consistently substituted with the era-neutral 'blank-check'. Remaining macro claims ('freight cycle firm', 'ESG/decarbonization capital abundant') are directional regime descriptions with no dated policy, index level, rate or program anchor.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Clean on the denylist and on relative dates; rescaling is independently confirmed at 0.85 for every figure the manifest actually covers, with three payload figures falling outside manifest coverage and therefore unproven. No concrete leak found, so safe_to_run=true. (1) ALIAS: none of the six denylisted tokens appear anywhere in candidate_payload; the flagship product is rendered as \"range-extender electric architecture (natural-gas generator + battery)\" / \"flagship long-haul architecture\", the OEM partner as \"chassis deliveries for demos\", the sponsor vehicle as \"blank-check merger\", and the CEO as \"charismatic founder-CEO\". The payload carries no URLs, no CIK, no real ticker. RESIDUAL RISK (not a concrete leak, so not a fail): the business description is narrow enough — Class-8 electrified powertrain, hybrid add-on plus gas-generator range extender, recent blank-check listing, pre-revenue — that a sector-literate reader could plausibly re-identify the issuer from the combination alone; if the benchmark wants strong anonymity rather than token-level masking, the description should be blurred further. (2) RESCALING: proven at 0.85 for the five manifest-backed line items (see check evidence). Three payload figures — market_cap_usd_m 1530, share_price_usd 9.02, and the FY-1 net_income_usd_m -60 — have no counterpart in source_manifest, so their scaling cannot be proven or disproven from the frozen packet; nothing in the payload appears at an un-scaled 1.0x against any manifest fact. I recommend the packet owner extend source_manifest to cover market cap, share price and the prior fiscal year before this case is used for scoring, since an un-rescaled share price would itself be a fingerprint. (3) DATES: fully relativized. (4) MACRO: the warrant-accounting restatement is genericized to an unnamed \"industry-wide issue\" with no regulator, no date and no affected-vehicle count. Confidence MEDIUM: the token, date and macro dimensions are decisively verifiable from the payload text, but rescaling coverage is partial by construction of the manifest."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Packet balance_sheet: cash_and_equivalents_usd_m 285 + marketable_investments_usd_m 252 = 537 — output states 'the baseline is $537M — $285M cash plus $252M marketable investments' (arithmetic verified)",
      "Packet balance_sheet.total_debt_usd_m = 0 — output states 'over half a billion in cash and no debt' and 'zero debt'",
      "Packet market_cap_usd_m = 1530 — output states 'the market already values it near $1.5 billion'",
      "Packet latest_quarter.gaap_net_income_usd_m = -14 — output states 'roughly $14M of quarterly GAAP net loss'",
      "Packet latest_quarter.revenue_usd_m = 0 and annual FY-1 revenue_usd_m = 0 — output states 'recognized revenue ... is exactly $0.0M' and 'The company has zero revenue'",
      "Packet share_price_usd = 9.02 — output states 'raises equity below the current $9.02 share price'",
      "Packet latest_quarter.which = 'Q1, 10-Q filed ~2 weeks ago' — output states 'as-of the latest 10-Q filed ~2 weeks ago'",
      "Packet news/VHS: 'Ten Hybrid units installed in the latest quarter — deployments, not sold-and-recognized volume' — output states 'ten Hybrid units installed as deployments only' and 'I'd revisit when they book actual sales, not installations'",
      "Packet news/VHS: 'Management guides first Hybrid revenue in the back half of the year and year-end demos of the flagship long-haul architecture' — output's NEXT VALIDATION POINT tests exactly these two items",
      "Packet news/VHS + fundamentals note: 'Restating prior-year blank-check warrant accounting (industry-wide issue)' / 'warrant-accounting restatement in flight' — output states 'prior-year accounting is still being restated' and requires the restatement 'filed complete with no additional material weakness'",
      "Packet valuation: ev_s_trailing, pe_trailing, ps_trailing all null — supports output claim of 'no way to judge whether that price leaves any safety buffer'",
      "Packet fundamentals note: 'post-listing cash pile funds ~multi-year burn at current rate' — supports output claim 'it has years to try' and 'the multi-year runway with no debt'",
      "constraints_rendered sleeve rule '~1-2% per position' — output concentration_delta and the HOLD->ADD '~1% starter position' both stay inside the stated sleeve; 'No leverage, no options' respected via instrument 'stock'",
      "Packet report_name fields 'fundamentals/VHS' and 'news/VHS' appear verbatim as the only cited_sources; sanitizer-side URLs were not cited",
      "constraints_rendered reporting requirement (FALSIFIERS + NEXT VALIDATION POINT + honest horizon band, with explicit statement if unestimable) — satisfied by four falsifiers plus the 2026-12-31 trigger carrying '2-4 years' and an explicit unestimability admission",
      "UNSUPPORTED (minor): the $350M runway floor in falsifier 4 has no derivation from the packet's burn rate; declared as a forward-only judgment threshold rather than a packet fact",
      "UNSUPPORTED (minor): the '2-4 years to any commercial-scale proof' band is asserted without packet-derived basis, though the agent explicitly flags the horizon as genuinely unestimable"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "REPLAY AUDIT — case hyln_2021 (masked packet \"VHS\"), verdict: passes cleanliness, packet fidelity, and workflow checks; visible rationale is grounded with minor unsupported quantitative choices.\n\n(1) OUTPUT CLEANLINESS — PASS. The response text contains no denylisted or de-masking content: the issuer is referred to only as \"VHS\"/\"the company\", the flagship product only as \"the range-extender long-haul architecture\", the listing vehicle only as \"blank-check\". No real ticker, company name, product name, CIK, URL or filing accession number appears. Nothing the sanitizer stripped is reintroduced (no regulator name, no \"~80 affected vehicles\" count, no SPAC token). The agent did not attempt to identify the real issuer, as the provenance note required. The only calendar year emitted is 2026 (matching the replay harness's ambient today, 2026-08-30), not the 2021 freeze era — so no leakage of the true timeline and no anachronistic market data; this is consistent with the persisted output_audit (contaminated=false, contamination_hits=[]).\n\n(2) PACKET FIDELITY — PASS. Every numeric claim traces verbatim to the packet: \"near $1.5 billion\" ← market_cap_usd_m 1530; \"$537M — $285M cash plus $252M marketable investments\" ← balance_sheet 285 + 252 (arithmetic correct); \"no debt\" ← total_debt_usd_m 0; \"roughly $14M of quarterly GAAP net loss\" ← gaap_net_income_usd_m -14; \"$9.02 share price\" ← share_price_usd 9.02; \"exactly $0.0M revenue\" ← revenue_usd_m 0; \"ten Hybrid units installed as deployments only\" ← news report; \"10-Q filed ~2 weeks ago\" ← latest_quarter.which; \"restatement is still in flight\" ← fundamentals note + news item; \"~1-2% of portfolio\" sleeve ← constraints_rendered/positions. The claim that valuation offers \"no way to judge whether that price leaves any safety buffer\" is supported by valuation.pe_trailing / ps_trailing / ev_s_trailing all being null against zero sales. No figure is invented, inflated, or de-scaled. cited_sources [\"fundamentals/VHS\",\"news/VHS\"] match the packet report_name fields verbatim; the sanitizer-side real URLs were correctly NOT cited.\n\n(3) WORKFLOW CORRECTNESS — PASS. The reporting requirement is fully discharged: FALSIFIERS are recorded (four, each with a named baseline, a source class, a breach transition, and an explicit note where a condition is deliberately left qualitative); THE CLOCK is recorded as a dated NEXT VALIDATION POINT (2026-12-31, tied to the guided H2 revenue and the year-end flagship demo) with an honest re-rating band (\"2-4 years\"), plus the explicitly required acknowledgement that the horizon is genuinely unestimable and that this itself lowers conviction. Portfolio constraints respected: instrument \"stock\", no leverage, no options; size 0 with cash_delta $0 and concentration_delta stating the sleeve stays unused; tax_estimate correctly states no disposal event. Schema is well-formed and response_raw parses to output_full without drift. A confidence band (MEDIUM) is reported per fleet rule 3.\n\n(4) REASONING GROUNDING (visible rationale only; no claim of access to hidden reasoning) — 3/4. Each step of the written rationale is anchored to a packet fact: zero revenue → no valuation anchor; $1.53bn cap against $0 sales → price prices an unbuilt commercial product; $537M and zero debt against ~$14M/quarter loss → \"years to try\" (arithmetically ~38 quarters, and corroborated by the packet's own \"funds ~multi-year burn\" note); \"installations, not sales\" → directly mirrors the news line distinguishing deployments from recognized volume; restatement-in-flight → cited as an unresolved books risk. Minor gaps preventing a 4: the $350M runway threshold is an arbitrary round number with no derivation from the packet's burn rate, and the \"2-4 years to commercial-scale proof\" band is asserted rather than derived (though the agent flags its own unestimability, which partially mitigates).\n\nMINOR, NON-BLOCKING OBSERVATIONS (recorded, not scored as failures): (a) \"hold\" and the \"HOLD->SELL / terminate candidacy\" transitions are semantically loose given the packet states there is no existing position — the output self-corrects by defining SELL as \"drop from the watchlist\", so intent is unambiguous; (b) falsifiers timestamp baselines \"as-of 2026-08-30\", an absolute date derived from ambient today rather than from the deliberately date-relativized packet — acceptable because the requirement explicitly asks for a dated validation point, and the date does not leak the freeze era.\n\nI did not assess whether hold/pass was the correct investment call; that is outside this review's scope.",
    "violations": [
      "MINOR (non-blocking): action 'hold' and the 'HOLD->SELL' breach transitions are semantically loose given the packet states 'No existing position in VHS'; the output disambiguates by defining SELL as 'terminate candidacy and drop from the watchlist', and size 0 / cash_delta $0 / 'no position initiated' keep the instruction unambiguous.",
      "MINOR (non-blocking): falsifier baselines are timestamped 'as-of 2026-08-30' and the validation point dated 2026-12-31 — absolute dates derived from the harness's ambient today rather than from the deliberately date-relativized packet. No freeze-era (2021) leakage results, and the constraint explicitly required a dated or estimated validation point."
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": 0.0,
    "benchmark_return_pct": -86.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case hyln_2021 (category B, grading rule \"trap\"; expected_classes = [\"hold\"]). The fleet returned action=\"hold\" with size=0.0 shares and no position initiated (positioned=false), which is verbatim in the expected class — full credit 1.0 under the benchmark rule (1.0 for an in-class verdict). The 0.5 partial-credit path does not apply: it is reserved for an F1 pass carrying falsifiers or a small trap buy that names the killing falsifier, and neither is triggered here because the verdict was not a buy of any size and required no partial-credit rescue. No penalty is applied for the reviewer's two MINOR non-blocking observations (loose HOLD->SELL semantics with no existing position; ambient-date-derived falsifier timestamps), and the review agent's cleanliness/packet-fidelity/workflow PASS judgments and reasoning_grounded_score of 3 are accepted as final and not reopened. Acted-on return: with size 0.0 and cash_delta $0, no capital was exposed, so acted_return_pct = 0.0% against a benchmark_return_pct of -86% over the 3y horizon to 2024-05-31 (price_at_freeze_real 10.61 -> price_at_horizon_real 1.48, per the supplied answer_key.resolution, yfinance closes fetched 2026-07-11) — the trap was avoided, i.e. 86 points of loss not taken. Per instruction, outcome luck does not alter the class grade: the score would be 1.0 for the in-class \"hold\" regardless of how the ticker resolved. Confidence HIGH: the grade rests on a direct string match between fleet_result.action and answer_key.expected_classes plus arithmetic on the supplied resolution block — all primary inputs, none stale or inferred.",
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
- notes: Case hyln_2021 (category B, grading rule "trap"; expected_classes = ["hold"]). The fleet returned action="hold" with size=0.0 shares and no position initiated (positioned=false), which is verbatim in the expected class — full credit 1.0 under the benchmark rule (1.0 for an in-class verdict). The 0.5 partial-credit path does not apply: it is reserved for an F1 pass carrying falsifiers or a small trap buy that names the killing falsifier, and neither is triggered here because the verdict was not a buy of any size and required no partial-credit rescue. No penalty is applied for the reviewer's two MINOR non-blocking observations (loose HOLD->SELL semantics with no existing position; ambient-date-derived falsifier timestamps), and the review agent's cleanliness/packet-fidelity/workflow PASS judgments and reasoning_grounded_score of 3 are accepted as final and not reopened. Acted-on return: with size 0.0 and cash_delta $0, no capital was exposed, so acted_return_pct = 0.0% against a benchmark_return_pct of -86% over the 3y horizon to 2024-05-31 (price_at_freeze_real 10.61 -> price_at_horizon_real 1.48, per the supplied answer_key.resolution, yfinance closes fetched 2026-07-11) — the trap was avoided, i.e. 86 points of loss not taken. Per instruction, outcome luck does not alter the class grade: the score would be 1.0 for the in-class "hold" regardless of how the ticker resolved. Confidence HIGH: the grade rests on a direct string match between fleet_result.action and answer_key.expected_classes plus arithmetic on the supplied resolution block — all primary inputs, none stale or inferred.

### sdc_2021 — real: SmileDirectClub (SDC) @ 2021-11-15 ($4.05); obscure DTC-aligner trap after Q3 demand cliff; Chapter 11 2023-09
- **Fleet reasoning:** hold (HIGH) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Full-string scan of candidate_payload (alias, analyst_reports, constraints_rendered, positions) against all 8 forbidden_terms returned zero verbatim or case-variant matches. Payload consistently uses the masked name 'ClearBite Oral (alias CBO)' and genericizes entity-specific nouns: the incumbent competitor appears only as 'the chairside incumbent' (no vendor/product name), the physical retail footprint only as 'retail kiosks' (no branded store name), and control persons only as 'Founder-family controls the vote' (no surname). Caveat, not a fail: the identifying URLs in raw_sources/source_manifest contain the issuer name in their path slugs — these live OUTSIDE candidate_payload and must never be forwarded to the Trader or echoed into downstream artifacts.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven independently from source_manifest arithmetic at the declared rescale_factor 1.4, using only manifest-covered figures. source_manifest[0] (issuer Q3 earnings release, publication_date 2021-11-08): revenue $138M x 1.4 = 193.2 vs payload latest_quarter.revenue_usd_m 193 (rounding delta 0.2); net loss -$89M x 1.4 = -124.6 vs payload gaap_net_income_usd_m -125 (delta 0.4); marketing and selling $96M x 1.4 = 134.4 vs payload marketing_and_selling_usd_m 134 (delta 0.4). All three within +/-0.5 integer rounding, same factor, no figure left at raw scale. Scale-invariant fields are correctly preserved unscaled: revenue_growth_yoy_pct -18.3 matches manifest[0] exactly; marketing-to-revenue ~70% matches manifest[0]. Internal consistency of the annual series holds: 919/1051 = 0.8744 = the stated -12.5% FY-1 growth. UNVERIFIABLE SUBSET (flagged, not proven): market_cap_usd_m 2240, share_price_usd 5.67, cash_and_equivalents_usd_m 308, total_debt_usd_m 1050 and both annual revenue lines have no counterpart fact in source_manifest, so their scaling is asserted but not independently provable from the frozen packet; they are at least mutually consistent with a single-constant transform. Two residual observations for the packet owner: (a) ps_trailing 2.5 implies TTM sales 896 while ev_s_trailing 3.2 against EV = 2240 + 1050 - 308 = 2982 implies 932 — a ~4% mismatch, likely a net-debt-definition or rounding artifact, not a masking failure; (b) market_cap / share_price = ~395 (million shares) is invariant under dollar rescaling, so the implied share count survives masking as a re-identification vector — consider perturbing price and market cap by different constants in future packets.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No absolute calendar dates, years, quarters-with-year, or month names appear anywhere in candidate_payload. Every temporal reference is relative or era-neutral: as_of 'current'; latest_quarter.which 'Q3, reported ~1 week ago'; fiscal years labelled 'FY-2' and 'FY-1'; price history as 'stock -80%+ from IPO-era peak'. The manifest's 2021-11-08 publication date and the FY2021 guidance framing in manifest[1] do not appear in the payload. constraints_rendered contains only relative horizons ('5+ year', '3-6 months', '2-3 years') supplied as reporting-format examples, which carry no dating information.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The two macro drivers carried from source_manifest[1] ('macro headwinds to core demographic plus digital-ad attribution breakage') are rendered in the payload without any named platform, OS release, policy, or dated event: macro_context reads 'Consumer discretionary soft for lower-income cohorts; digital advertising attribution disrupted industry-wide', and recent_developments repeats it as 'macro pressure on its core demographic plus digital-ad attribution changes'. No vendor name, framework name, version number, pandemic reference, central-bank action, or year-anchored macro label appears, so the era cannot be pinned from the macro text alone.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Cleared to run. Zero forbidden terms leak into candidate_payload; competitor, retail-format and control-person references are all genericized. Dollar rescaling is independently proven at the declared 1.4x from source_manifest arithmetic on the three manifest-covered figures (193, -125, 134 vs 138, -89, 96 raw), with scale-invariant ratios correctly left untouched. All temporal references are relative (FY-1/FY-2, 'reported ~1 week ago', 'IPO-era peak') and the macro drivers are stated without any named platform or dated event. Confidence is MEDIUM rather than HIGH because the balance-sheet, market-cap, share-price and annual-revenue figures have no counterpart in the frozen manifest, so their scaling is asserted but not provable here — I did not infer proof from the classifier's narrative. Two non-blocking hygiene notes for the packet owner: the manifest/raw_sources URLs embed the issuer name in their path slugs and must not travel with the payload, and market_cap divided by share_price yields an unmasked implied share count (~395M), which survives a single-constant dollar transform as a re-identification vector. The ps_trailing 2.5 vs ev_s_trailing 3.2 pair is internally off by ~4% against the stated cash and debt — a definitional or rounding artifact worth correcting, not a masking defect. No opinion offered on issuer identity or eventual outcome."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "MEDIUM",
    "grounding_evidence": [
      "Output: 'revenue fell 18% last quarter' -> packet analyst_reports[0].latest_quarter.revenue_growth_yoy_pct = -18.3. MATCH.",
      "Output falsifier 1: 'currently $134M on $193M revenue = 69.4%' -> packet marketing_and_selling_usd_m = 134, revenue_usd_m = 193; 134/193 = 69.43%. ARITHMETIC VERIFIED.",
      "Output: 'marketing consumed about 70% of every sales dollar' -> packet latest_quarter.note: 'marketing spend ≈ 70% of quarterly revenue; CAC not working; guide cut hard'. MATCH.",
      "Output falsifier 3: 'GAAP quarterly net loss ... currently -$125M' -> packet latest_quarter.gaap_net_income_usd_m = -125. MATCH.",
      "Output: 'Cash of $308M sits against $1,050M of debt' -> packet balance_sheet.cash_and_equivalents_usd_m = 308, total_debt_usd_m = 1050. MATCH.",
      "Output: 'the balance sheet is funding losses rather than building anything durable' -> packet news recent_developments: 'a balance sheet that funds losses rather than a moat'. GROUNDED PARAPHRASE.",
      "Output: 'each new customer costs more than it brings back' -> packet news recent_developments: 'CAC/LTV math visibly broken'. GROUNDED PARAPHRASE.",
      "Output cited_sources ['fundamentals/CBO', 'news/CBO'] -> packet analyst_reports[0].report_name = 'fundamentals/CBO', analyst_reports[1].report_name = 'news/CBO'. VERBATIM, NO INVENTED IDS.",
      "Constraint 'REPORTING REQUIREMENT ... (2) THE CLOCK ... if it is genuinely unestimable, say so explicitly — that itself lowers conviction' -> output: 'when — or whether — a turnaround shows up is genuinely unestimable, and that alone lowers conviction'. REQUIREMENT SATISFIED.",
      "Constraint 'No leverage, no options' and '~1-2% per position' -> output instrument='stock', size_shares_or_currency=0.0, concentration_delta='No change; high-potential sleeve remains unallocated by this decision.' COMPLIANT.",
      "Sanitizer flagged ps_trailing 2.5 vs ev_s_trailing 3.2 as internally inconsistent (~4%); output never cites either multiple and states 'Shrinkage, not valuation, is the reason for the pass'. CONTAMINATED FIELD CORRECTLY AVOIDED.",
      "UNSUPPORTED: output 'Next real test is the Q4 print in roughly three months' -> packet contains only latest_quarter.which = 'Q3, reported ~1 week ago'; no forward event date exists anywhere in the packet. NO PACKET BASIS.",
      "ERA ARTIFACT: output revisit_triggers[0].date = '2026-12-01' vs case freeze_date '2021-11-15'; output_audit.calendar_years_in_output = ['2026'] with contaminated=false and empty contamination_hits."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "CLEAN / FAITHFUL / WORKFLOW-CORRECT, with minor dating gaps. (1) Cleanliness: output uses only the masked alias 'CBO', contains no issuer name, no competitor/control-person identifiers, and no post-freeze outcome knowledge; output_audit records contaminated=false with an empty suspect_terms list. The single calendar year present (2026) comes from the revisit_trigger date 2026-12-01, which is anchored to the reviewing agent's system clock, not to any issuer-specific hindsight — it leaks nothing about the masked company, so I do not score it as contamination, but I flag it as a replay-harness era artifact (freeze_date is 2021-11-15). (2) Packet fidelity: every numeric claim reconciles verbatim to the packet — '-18%' vs revenue_growth_yoy_pct -18.3; 'about 70% of every sales dollar' vs the packet note 'marketing spend ≈ 70% of quarterly revenue' and the agent's own arithmetic $134M/$193M = 69.4% (exact); '-$125M' vs gaap_net_income_usd_m -125; '$308M ... $1,050M of debt' vs cash_and_equivalents_usd_m 308 and total_debt_usd_m 1050. cited_sources ['fundamentals/CBO','news/CBO'] appear verbatim as report_name values in analyst_reports; no source id was invented, abbreviated, or paraphrased. Qualitative claims are grounded too: 'each new customer costs more than it brings back' maps to 'CAC/LTV math visibly broken' and 'CAC not working'; 'balance sheet is funding losses rather than building anything durable' maps to the bear note 'a balance sheet that funds losses rather than a moat'. Creditably, the agent did NOT rely on ps_trailing/ev_s_trailing — the one internally inconsistent pair the sanitizer flagged (~4% mismatch) — and stated explicitly that 'Shrinkage, not valuation, is the reason for the pass'. (3) Workflow: response_raw is exactly one JSON object with no prose or fences; schema fields are complete and type-consistent; constraint compliance holds (instrument=stock, no leverage/options, size 0.0, high-potential sleeve left unallocated, cash_delta $0). Both halves of the REPORTING REQUIREMENT are satisfied: four falsifiers, each with a stated baseline, a checkable threshold, and a HOLD->ADD transition — one of which is honestly relabelled as an accelerant that hardens rather than disproves the pass — plus THE CLOCK naming the Q4/full-year print as the next validation point and explicitly declaring the re-rating horizon unestimable, which the rendered constraint expressly allows and links to lowered conviction. (4) Visible rationale grounding scored 3, not 4: all quantitative steps trace to packet facts, but one inferential step is unsupported — the packet states only 'Q3, reported ~1 week ago' and contains no forward calendar, so 'Next real test is the Q4 print in roughly three months' is an assumption, and it understates a typical Q3-report-to-Q4-print gap (~3.5-4 months). A secondary tension: the record declares confidence HIGH while the rationale simultaneously says the unestimable horizon 'lowers conviction'; these are separable (data quality vs thesis conviction) and the packet's two analyst reports both self-report HIGH, so HIGH is defensible, but the record does not reconcile the two. I express no view on whether the pass was the right investment call.",
    "violations": [
      "Unsupported temporal inference: rationale_summary asserts 'Next real test is the Q4 print in roughly three months'; the packet supplies no forward calendar (only latest_quarter.which = 'Q3, reported ~1 week ago'), and a Q3-report-to-Q4-print gap is typically ~3.5-4 months, so the estimate is both ungrounded and slightly early.",
      "Era artifact (non-leaking): revisit_triggers[0].date = '2026-12-01' is ~5 years past freeze_date 2021-11-15, anchored to the reviewing agent's system clock. It reveals nothing about the masked issuer, but it breaks the packet's deliberate relative-date discipline (sanitizer check 'relative_dates' passed precisely because the payload contained no absolute dates).",
      "Unreconciled confidence tension: record sets confidence 'HIGH' while rationale_summary states the unestimable turnaround timing 'lowers conviction'; the distinction between data confidence and thesis conviction is never made explicit.",
      "Minor semantic stretch (not a fidelity error): action 'hold' is used for a name where positions states 'No existing position in CBO'; size 0.0 and the rationale's 'do not initiate a position' disambiguate the intent, but 'hold' nominally presupposes an existing holding."
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": 0.0,
    "benchmark_return_pct": -100.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Trap case sdc_2021 (category B); answer_key.expected_classes = [\"hold\"]. The fleet returned action=\"hold\" with size=0.0 shares and an explicit \"Pass on CBO — do not initiate a position,\" which is a clean no-position verdict and lands squarely in the expected class, so the grading rule's full-credit branch applies (1.0). The 0.5 partial-credit branch is not triggered: this is neither an F1-pass-with-falsifiers case nor a small trap buy with the killing falsifier — no capital was committed at all. Acted-on return computed from the recorded action and answer_key.resolution: size 0.0 at price_at_freeze_real $4.05 (answer_key.resolution) means zero exposure through the ride-to-zero horizon, so acted_return_pct = 0.0%; the unavoided benchmark was -100% (answer_key.resolution.benchmark_return_pct = -100, horizon \"ride-to-zero (Ch.11 2023-09)\", Yahoo SDCCQ close 2021-11-15 = $4.05 retrieved 2026-07-11, terminal value $0.00). The avoided loss is outcome information only and does not raise the class grade above in-class; equally, the reviewer's noted defects (ungrounded \"Q4 print in roughly three months,\" the 2026-12-01 era-artifact revisit date, the unreconciled HIGH-confidence tension, and the semantic use of \"hold\" where no position existed) are grounding/hygiene findings that the review agent already adjudicated as clean/faithful/workflow-correct (output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3) — I do not reopen that judgment, and none of them change the action class. Grading confidence is HIGH: the verdict-to-class mapping is unambiguous and the resolution figures are primary and fully supplied in the grading input.",
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
- notes: Trap case sdc_2021 (category B); answer_key.expected_classes = ["hold"]. The fleet returned action="hold" with size=0.0 shares and an explicit "Pass on CBO — do not initiate a position," which is a clean no-position verdict and lands squarely in the expected class, so the grading rule's full-credit branch applies (1.0). The 0.5 partial-credit branch is not triggered: this is neither an F1-pass-with-falsifiers case nor a small trap buy with the killing falsifier — no capital was committed at all. Acted-on return computed from the recorded action and answer_key.resolution: size 0.0 at price_at_freeze_real $4.05 (answer_key.resolution) means zero exposure through the ride-to-zero horizon, so acted_return_pct = 0.0%; the unavoided benchmark was -100% (answer_key.resolution.benchmark_return_pct = -100, horizon "ride-to-zero (Ch.11 2023-09)", Yahoo SDCCQ close 2021-11-15 = $4.05 retrieved 2026-07-11, terminal value $0.00). The avoided loss is outcome information only and does not raise the class grade above in-class; equally, the reviewer's noted defects (ungrounded "Q4 print in roughly three months," the 2026-12-01 era-artifact revisit date, the unreconciled HIGH-confidence tension, and the semantic use of "hold" where no position existed) are grounding/hygiene findings that the review agent already adjudicated as clean/faithful/workflow-correct (output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3) — I do not reopen that judgment, and none of them change the action class. Grading confidence is HIGH: the verdict-to-class mapping is unambiguous and the resolution figures are primary and fully supplied in the grading input.

### ttcf_t2 — real: TTCF @ 2021-08-16 (~$17.50); Q2-21 miss + guidance cut
- **Fleet reasoning:** hold (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Full-text scan of candidate_payload against all 12 forbidden_terms returned zero verbatim or near-verbatim hits. Issuer appears only as 'Provida Foods (alias PVF)' and report_names 'fundamentals/PVF', 'news/PVF'. Sponsor vehicle -> 'the listing structure' / 'speculative listing-vehicle cohort'. Club retailer -> 'warehouse-club private-label volume' and 'the club channel'. Mass retailer -> 'one of the largest mass-market retailers'. Grocer -> '~1,800 stores of the largest grocery chain'. Acquisition targets -> 'two tortilla/food-plant acquisitions' and 'southwestern/tortilla products'. Sector leader -> 'the category's pure-play leader'. Note: forbidden_terms were treated strictly as a denylist for scanning; none were introduced into this output as facts.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Uniform factor 3.7 proven from source_manifest only. 148.5*3.7=549.45~549 and GM 14.6% unscaled [sec.gov/Archives/edgar/data/1741231/000121390021016721/f10k2020_tattooedchef.htm, 2021-03-19]. 50.7*3.7=187.59~188; -53.2*3.7=-196.84~-197; 46.0*3.7=170.2~170; guide -14/-17*3.7=-51.8/-62.9 = '-52 to -63'; prior +2/+4*3.7=+7.4/+14.8 = '+7 to +15'; GM guide 16-22 from 20-25 and growth +45.9% carried unscaled as required [globenewswire.com/news-release/2021/08/12/2280137/..., 2021-08-12]. ~37*3.7=136.9~137 for the two cash plant deals [globenewswire.com/news-release/2021/05/18/2231349/..., 2021-05-18]. Six independent manifest-anchored figures all reconcile at 3.7 with rounding <=0.5. UNPROVEN SUBSET (no manifest counterpart, asserted not proven): market_cap 5300, share_price 64.75, entry 74.95, cash 519, debt 10, FY guide 870-895, consensus ~200 — these are only shown internally consistent (5300/880=6.02 vs stated P/S 6.0; 549*1.59=873 vs '+~59%'). Verdict is pass on the provable set; the unprovable set is flagged rather than proven by invention.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No absolute date, month, or calendar year from the frozen packet (2021-03-19, 2021-05-18, 2021-07-12, 2021-08-12) survives into candidate_payload. All temporal anchors are relative and self-referential: 'FY-1 (the entry-year)', 'as_of: current', 'Q0 = Q2 of the current FY, reported ~4 days ago', 'Entered three quarters ago', 'in over 12,000 retail stores by the end of next quarter', 'run heavy for several quarters'. Fiscal-quarter labels (Q2 of the current FY) carry no year and do not de-anonymize the period.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Reopening economy; freight and food-input inflation squeezing every food manufacturer; speculative listing-vehicle cohort broadly deflating' — the pandemic reopening, the blank-check-listing cohort, and the sector de-rating are all described functionally with no named event, no named vehicle, no named peer, and no date. Warrant dilution is attributed to 'the listing structure' rather than a named sponsor. Peer weakness is 'the category's pure-play leader has repeatedly missed estimates'. Residual non-blocking risk noted for the packet owner: the operational counts '~1,800 stores' and 'over 12,000 retail stores' are preserved verbatim from source and are distinctive; they are not forbidden terms and do not fail this check, but banding them would harden future packets.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "AUDIT VERDICT: safe_to_run = TRUE. No forbidden term appears verbatim or in near-verbatim form anywhere in the candidate payload; all issuer, sponsor-vehicle, retailer, and acquisition-target names are rendered as descriptive generics (\"Provida Foods (alias PVF)\", \"warehouse-club private-label volume\", \"one of the largest mass-market retailers\", \"largest grocery chain\", \"two tortilla/food-plant acquisitions\", \"the listing structure\", \"the category's pure-play leader\"). ABSOLUTE-FIGURE RESCALING is independently proven at a uniform factor of 3.7 against the source_manifest alone, without reference to any classifier-supplied facts: FY-1 revenue 148.5 x 3.7 = 549.45 -> payload 549 [https://www.sec.gov/Archives/edgar/data/1741231/000121390021016721/f10k2020_tattooedchef.htm, retrieved-at 2021-03-19]; latest-quarter revenue 50.7 x 3.7 = 187.6 -> payload 188; GAAP net loss -53.2 x 3.7 = -196.8 -> payload -197; non-cash valuation allowance 46.0 x 3.7 = 170.2 -> payload ~170; adj-EBITDA guide -14 to -17 x 3.7 = -51.8 to -62.9 -> payload \"-52 to -63\" and +2 to +4 x 3.7 = +7.4 to +14.8 -> payload \"+7 to +15\" [https://www.globenewswire.com/news-release/2021/08/12/2280137/0/en/Tattooed-Chef-Reports-Second-Quarter-2021-Financial-Results.html, retrieved-at 2021-08-12]; acquisition cash outlay ~37 x 3.7 = 136.9 -> payload ~137 [https://www.globenewswire.com/news-release/2021/05/18/2231349/0/en/Tattooed-Chef-Completes-Acquisition-of-New-Mexico-Food-Distributors-Inc-and-Karsten-Tortilla-Factory-LLC-Reclassifies-Certain-Expenses.html, retrieved-at 2021-05-18]. All ratios are correctly left unscaled (GM 14.6%, 15.7%, growth +75%, +45.9%), as the provenance note promises. CAVEAT (drives the MEDIUM band): market_cap 5300, share_price 64.75, entry 74.95, cash 519, and debt 10 have NO counterpart in the source_manifest, so their scaling is not independently provable; they are only shown to be internally consistent (5300 / 880 midpoint guide = 6.02, matching the stated ps_on_fy_guide of 6.0, and 549 x 1.59 = 873, matching the \"+~59%\" growth attached to the 870-895 guide). I did not manufacture proof for that subset. RELATIVE DATES: clean — every temporal reference is relative (\"FY-1 (the entry-year)\", \"as_of: current\", \"reported ~4 days ago\", \"Entered three quarters ago\", \"by the end of next quarter\", \"for several quarters\"); no calendar year, month, or ISO date from the raw packet survives into the payload. MACRO GENERICIZATION: the pandemic-reopening backdrop, the blank-check/listing-vehicle cohort de-rating, and the sector leader's serial misses are all described functionally with no proper nouns and no dated macro event. RESIDUAL, NON-BLOCKING IDENTIFIABILITY RISK for the packet owner's awareness (not a protocol failure and not a forbidden term): the payload preserves two verbatim non-dollar operational figures from the sources — the \"~1,800 stores\" grocery-chain door count and the \"over 12,000 retail stores\" target — which are distinctive enough that a determined reader with a search index could narrow the candidate set; consider fuzzing store counts to bands in future packets. I did not attempt to identify the issuer and make no statement about the eventual outcome of the position."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Packet balance_sheet: 'cash_and_equivalents_usd_m: 519', 'total_debt_usd_m: 10' -> output rationale '$519M cash against $10M debt' (exact match).",
      "Packet balance_sheet.share_count_note: 'share count is ~30% higher than at entry (warrant exercises/exchanges from the listing structure)' -> output '~30% more shares mean each one owns less of the business' (exact match).",
      "Packet valuation.ps_on_fy_guide: 6.0 -> output 'At six times sales there's no buffer, so we don't add' (exact match).",
      "Packet news/PVF: 'branded products will be in over 12,000 retail stores by the end of next quarter' and '~1,800 stores of the largest grocery chain' -> output falsifier #3 and rationale 'heading past 12,000 stores' (exact match).",
      "Packet guidance: 'fy_revenue_usd_m: 870-895 maintained (+~59%)' and 'adj_ebitda_usd_m: CUT to -52 to -63, from +7 to +15' -> output falsifier #2 and rationale 'the year flipped to a loss' (exact match).",
      "Packet customer_concentration: 'split_pct: [39, 32]', 'top_2_customers_pct_of_revenue: 71' -> output falsifier #2 mechanism '(39% and 32% of revenue, 71% combined)' (exact match).",
      "Packet positions: 'entry at $74.95/share (~1% of portfolio at entry...)' and 'Current price $64.75 -> unrealized P&L -14%' -> output concentration_delta 'roughly 0.9% of portfolio (entry ~1%, now -14%)'; 1% x 0.86 = 0.86% ~ 0.9%, recomputed and consistent.",
      "Independent recomputation of the ADD trigger: 43/64.75 x $5,300M market_cap = ~$3,519M; divided by the $880M midpoint of the maintained $870-895M guide = 4.0x P/S — matches the output's claim 'from ~6.0x toward ~4.0x' exactly.",
      "Constraints EXIT DISCIPLINE: 'PRICE APPRECIATION ALONE IS NEVER AN EXIT TRIGGER... The only sanctioned trims: (a) ... thesis falsifier fires, or (b) the sleeve breaches its plan cap' -> output takes action 'hold', size 0, fires no price-based trim, and states the cap is not breached; the lone price trigger is HOLD->ADD marked 'ARMS A REVIEW ONLY'.",
      "Constraints REPORTING REQUIREMENT: '(1) FALSIFIERS ... (2) THE CLOCK — the NEXT VALIDATION POINT ... and the expected re-rating horizon as an honest band' -> output supplies four falsifiers, names the Q3 print as the next validation point (flagged ESTIMATED), and states 're-rating horizon 2-3 years'.",
      "Packet latest_quarter.net_loss_note: 'includes a one-time NON-CASH ~170 deferred-tax valuation allowance ... ex-item operating loss ~-27' — cited by the output under the GROSS MARGIN falsifier, whereas the packet attaches it to the net loss; basis for the deduction from 4 to 3 on grounding.",
      "cited_sources values 'fundamentals/PVF' and 'news/PVF' appear verbatim as packet analyst_reports[].report_name — no hallucinated or constructed source ids."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "REPLAY AUDIT — case ttcf_t2 (alias PVF), freeze 2021-08-16. VERDICT: output clean, packet-faithful, workflow-compliant; visible rationale grounded with one minor mis-attachment.\n\n(1) OUTPUT CLEANLINESS — PASS. `response_raw` is exactly one JSON object with no prose, headings, or code fences before or after it. No real-issuer identifiers, no source-manifest names (the manifest's real names appear only in the reviewer-side `sources` block, never in the output), and no forbidden-term leakage. `cited_sources` contains only \"fundamentals/PVF\" and \"news/PVF\", which appear verbatim as `report_name` values in the packet — no invented, abbreviated, or constructed source ids. Nulls are used correctly for inapplicable fields (limit_price, stop_price, and per-trigger unused keys). The only calendar year in the output is 2026 (`revisit_triggers[0].date = \"2026-11-30\"`), which the harness's own `output_audit` scored `contaminated: false` — it is anchored to the agent's system date (2026-08-30), not to the frozen 2021 source dates, so no post-freeze/lookahead fact leaked. NOTE (non-blocking): the packet is deliberately relative-dated (\"reported ~4 days ago\"), so emitting an absolute ISO date couples the output to the harness clock rather than to packet time; the agent mitigated this by labelling it \"ESTIMATED ... exact date not disclosed in inputs\".\n\n(2) PACKET FIDELITY — PASS. Every numeric in the output traces to the packet, and the derived figures recompute:\n• $519M cash / $10M debt, ~$137M on two plant acquisitions, ~30% higher share count — all verbatim from `balance_sheet`.\n• 15.7% latest-quarter GM vs 14.6% FY-1; ~$170M non-cash deferred-tax valuation allowance; ex-item operating loss ~-$27M — verbatim from `latest_quarter` / `annual_income_statement`.\n• FY guide \"$870-895M maintained (+~59%)\"; adj-EBITDA \"CUT to -52 to -63\"; GM guide cut — verbatim from `guidance`.\n• Top-2 concentration 39% + 32% = 71% — verbatim from `customer_concentration`.\n• \"six times sales\" = `ps_on_fy_guide: 6.0`; entry $74.95 → $64.75 = -14% — matches `positions`.\n• 12,000+ doors, ~1,800 grocery stores, nationwide mass-retailer launch — verbatim from `news/PVF`.\n• DERIVED, VERIFIED: $43 price trigger → 43/64.75 × $5,300M = ~$3,519M cap; /$880M midpoint guide = 4.0x P/S, exactly as claimed. Slot 1% × 0.86 = 0.86% ≈ \"roughly 0.9%\", as claimed. $250M cash floor vs \"roughly four times\" the -$52M to -$63M guided loss = 4.0x–4.8x — loose but honestly hedged with \"roughly\".\nNo figure in the output is absent from the packet; nothing fabricated.\n\n(3) WORKFLOW CORRECTNESS — PASS. The client EXIT DISCIPLINE is respected: action is `hold` with `size_shares_or_currency: 0`; there is no price-triggered trim anywhere; the single price trigger is HOLD->ADD and states explicitly \"This ARMS A REVIEW ONLY; a lower price does not break the thesis.\" The sleeve-cap branch is correctly evaluated and correctly not fired (\"inside the ~1-2% per-position and ~5% sleeve caps, so no mechanical rebalance is owed\"). The REPORTING REQUIREMENT is fully met: four checkable falsifiers, each with a decision transition; a named NEXT VALIDATION POINT (Q3 print) with the estimation honestly flagged as estimated rather than silently invented; and an explicit re-rating horizon band (\"2-3 years\"). Two falsifiers are deliberately left qualitative with a stated reason (two-quarter persistence cannot be a single-reading metric; a bare 12,000-door numeric trigger would mis-fire immediately) — that is a defensible, self-documented choice, not an omission. Confidence band reported (MEDIUM); conservative versus the packet's HIGH-confidence analyst reports, which is acceptable given masked/scaled inputs.\n\n(4) REASONING GROUNDEDNESS (visible rationale only; I make no claim of access to hidden reasoning) — 3/4. The written rationale is a step-by-step ledger against packet facts: what landed (doors, balance sheet) versus what moved against the thesis (GM guide cut, EBITDA flip to loss, ~30% dilution), then the valuation ceiling (6.0x sales) as the stated reason not to add. Each link is traceable. Two minor gaps: (a) falsifier #1 attaches the \"~$170M deferred-tax valuation allowance / ex-item operating loss ~-$27M\" adjustment to the GROSS MARGIN reading, but the packet attaches that adjustment to the GAAP net loss — GM of 15.7% is unaffected by the allowance, so the framing is imprecise even though every underlying number is packet-correct; (b) \"funds years of planned losses\" is an unstated inference ($519M ÷ ~$57M ≈ 9 years) — directionally sound but not shown. Neither is fabrication; both are phrasing/derivation gaps, hence 3 rather than 4.\n\nI did not attempt to identify the masked issuer and I express no view on whether hold was the correct investment call — only on cleanliness, fidelity, workflow, and grounding. Reviewer confidence: HIGH (complete packet, replay, and output supplied; all arithmetic independently recomputed).",
    "violations": [
      "MINOR (non-blocking, grounding): falsifier #1 frames the ~$170M non-cash deferred-tax valuation allowance and the ~-$27M ex-item operating loss as qualifying the GROSS MARGIN reading of 15.7%; the packet attaches that adjustment to the GAAP net loss (latest_quarter.net_loss_note), and gross margin is unaffected by it. Underlying figures are packet-correct; the attachment is not.",
      "MINOR (non-blocking, precision): the $250M cash falsifier is described as 'roughly four times the current-year guided adj-EBITDA loss of -$52M to -$63M'; the true multiple spans 4.0x-4.8x. Hedged with 'roughly', so not a fabricated figure.",
      "MINOR (non-blocking, temporal coupling): revisit_triggers[0].date = '2026-11-30' is an absolute ISO date derived from the agent's system clock, while the packet is deliberately relative-dated ('reported ~4 days ago'). No post-freeze fact leaked (harness output_audit: contaminated=false) and the agent labelled it 'ESTIMATED ... exact date not disclosed in inputs', but absolute dating of a relative-dated packet is a fragility worth noting.",
      "MINOR (non-blocking, citation granularity): rule 1 asks that every numeric claim be cited; rationale_summary carries figures ($519M, $10M, 12,000 stores, six times sales, ~30% dilution) under a report-level cited_sources array rather than per-claim attribution. Falsifiers do carry inline source-and-as-of attributions, so provenance is recoverable."
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": -100.0,
    "benchmark_return_pct": -100.0,
    "confidence": "HIGH",
    "in_expected_class": false,
    "rationale": "Case ttcf_t2, category C, grading rule \"exit\"; the answer key admits exactly one expected class: \"sell\" (answer_key.expected_classes). The fleet returned action \"hold\" with size 0.0 on an already-held position (positioned: true), which is not the expected class, so in_expected_class = false.\n\nPartial credit is not available here. The benchmark grants 0.5 only in the two enumerated situations — an F1-class pass that carries the falsifiers, or a small trap BUY accompanied by the killing falsifier. Neither applies: this is an exit-graded case with a single sanctioned class, and the position was already on the book, so a \"hold\" is an affirmative decision to remain exposed rather than a sized entry that could earn a reduced-damage discount. The agent's four falsifiers (gross-margin persistence, adj-EBITDA/customer concentration, door count, $250M cash floor) were forward-armed triggers, none of which fired at the freeze, so no killing-falsifier credit is warranted either. Score = 0.0.\n\nReturns: the resolution is a ride-to-zero — price_at_freeze_real 17.5 → price_at_horizon_real 0.0, benchmark_return_pct -100 (answer_key.resolution, horizon \"terminal (Chapter 11 2023-07; equity ~zero)\"). Because the acted decision was hold with size 0.0 on an existing position, the acted-on exposure is identical to the benchmark path: acted_return_pct = -100.\n\nOutcome luck is explicitly excluded from the class grade, and it does not change this one — the grade rests on the class mismatch (hold vs. sell), not on the terminal outcome. I am also not reopening the reviewer's stage-4 findings: the replay review judged the output clean, packet-faithful and workflow-correct with reasoning_grounded_score 3/4 and only MINOR non-blocking violations. Those findings stand and are orthogonal to calibration scoring — a well-formed, well-cited, discipline-respecting answer in the wrong class still scores 0.0 on class.\n\nConfidence HIGH: the expected class, the acted action/size, and the resolution figures were all supplied directly in the grading input, and the reviewer's verification returned ok with no failures, so no external or stale data was needed for this determination.",
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
- notes: Case ttcf_t2, category C, grading rule "exit"; the answer key admits exactly one expected class: "sell" (answer_key.expected_classes). The fleet returned action "hold" with size 0.0 on an already-held position (positioned: true), which is not the expected class, so in_expected_class = false.

Partial credit is not available here. The benchmark grants 0.5 only in the two enumerated situations — an F1-class pass that carries the falsifiers, or a small trap BUY accompanied by the killing falsifier. Neither applies: this is an exit-graded case with a single sanctioned class, and the position was already on the book, so a "hold" is an affirmative decision to remain exposed rather than a sized entry that could earn a reduced-damage discount. The agent's four falsifiers (gross-margin persistence, adj-EBITDA/customer concentration, door count, $250M cash floor) were forward-armed triggers, none of which fired at the freeze, so no killing-falsifier credit is warranted either. Score = 0.0.

Returns: the resolution is a ride-to-zero — price_at_freeze_real 17.5 → price_at_horizon_real 0.0, benchmark_return_pct -100 (answer_key.resolution, horizon "terminal (Chapter 11 2023-07; equity ~zero)"). Because the acted decision was hold with size 0.0 on an existing position, the acted-on exposure is identical to the benchmark path: acted_return_pct = -100.

Outcome luck is explicitly excluded from the class grade, and it does not change this one — the grade rests on the class mismatch (hold vs. sell), not on the terminal outcome. I am also not reopening the reviewer's stage-4 findings: the replay review judged the output clean, packet-faithful and workflow-correct with reasoning_grounded_score 3/4 and only MINOR non-blocking violations. Those findings stand and are orthogonal to calibration scoring — a well-formed, well-cited, discipline-respecting answer in the wrong class still scores 0.0 on class.

Confidence HIGH: the expected class, the acted action/size, and the resolution figures were all supplied directly in the grading input, and the reviewer's verification returned ok with no failures, so no external or stale data was needed for this determination.

### nlf_synthetic — real: SYNTHETIC winner-shape (no real company) — obscure industrial-software niche
- **Fleet reasoning:** buy (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "\"NLF\" used consistently in top-level alias, candidate_payload.alias, report_name \"fundamentals/NLF\" and \"news/NLF\", and positions (\"No existing position in NLF\"). Sole expansion is \"Nexus Lattice Fabric (alias NLF)\" in the fundamentals company field — a fabricated placeholder declared fictional by the constraints_rendered provenance note (\"The company, alias, events, and absolute dollar figures were invented for this test; there is no real company\"), and always bound to the alias rather than replacing it. No ticker, exchange, listing venue, person name, geography, or named customer present; counterparties are sector-only (\"regional chemical and pulp customers; neither is a household name\"). forbidden_terms is empty, so no denylist token could be matched; none was introduced.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "synthetic=true and rescale_factor=null: figures have no real-world originals, so rescaling is not_applicable by protocol. Independent proof would also be impossible — raw_sources=[] and source_manifest=[] — and I did not fall back on any classifier-agent facts. Internal-consistency substitute performed: 420→540→720 reproduces stated growth 28.6% and 33.3%; 2800 mcap / 18.4 price ⇒ ~152M shares; ps_trailing 3.9 ⇒ ~718 ≈ 720 FY-1 revenue; ev_s_trailing 3.4 vs computed EV (2800+90−410=2480)/720 = 3.44. Caveat: pe_trailing 55 implies TTM NI ≈ 51, above FY-1 NI 28 and near Q0 annualized 56 — coherent only across the described profitability inflection. Non-blocking.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "All temporal anchors are offset-relative: fy labels \"FY-3\"/\"FY-2\"/\"FY-1\", latest_quarter.which \"Q0 just reported\", as_of \"current\", \"a third straight quarter\", plus generic horizon bands \"3-6 months\"/\"2-3 years\" in the reporting requirement. Zero calendar years, months, dated quarters, or absolute dates anywhere in the payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Only macro reference is macro_context: \"Industrial capex steady; software multiples compressed vs prior cycle.\" Supporting market color is likewise era-agnostic: \"quiet grind higher off a forgotten ordinary listing; -40% from a prior overshoot, now consolidating\". No named crisis, central-bank action, pandemic, election, index level, or dated market episode that could anchor the Trader to a real period.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audit of case_id `nlf_synthetic` (alias NLF), declared synthetic=true with empty forbidden_terms, empty raw_sources, empty source_manifest, and rescale_factor=null. VERDICT: safe_to_run=true; no concrete leaks found. (1) ALIAS: the token \"NLF\" is used consistently across both analyst reports, report_name fields, positions, and the top-level alias; the only expansion, \"Nexus Lattice Fabric (alias NLF)\", is a fabricated placeholder name explicitly declared fictional in the constraints_rendered provenance note, and it is always bound to the alias rather than substituted for it. No ticker, exchange, listing venue, founder/executive name, headquarters, or customer name appears — customers are described only by sector (\"regional chemical and pulp customers; neither is a household name\"). (2) ABSOLUTE-FIGURE RESCALING: not_applicable per protocol — synthetic=true, so the USD figures have no real-world originals and rescale_factor is null. Independent proof is in any case impossible here because raw_sources and source_manifest are both empty; I did not substitute any classifier-supplied facts for that missing manifest. Substituting the required internal-consistency test, the figure set is largely self-coherent: revenue 420→540→720 reproduces the stated 28.6% and 33.3% growth; market cap 2800 / price 18.4 implies ~152M shares; ps_trailing 3.9 recovers ~718 ≈ FY-1 revenue 720; ev_s_trailing 3.4 reconciles exactly with EV = 2800 + 90 debt − 410 cash = 2480 over 720 (3.44). One soft caveat, not a leak and not a blocker: pe_trailing 55 implies TTM net income ≈ 51, which sits above FY-1's 28 and just below Q0's annualized 56 — reconcilable only if the trailing four quarters straddle the profitability inflection the report itself describes. Flagging for the Trader's awareness; the multiple should be read as inflection-distorted, not as a clean trailing figure. (3) RELATIVE DATES: fully compliant — all time references are offset-relative (FY-3, FY-2, FY-1, \"Q0 just reported\", as_of \"current\", \"a third straight quarter\") and the requested horizon bands are generic (\"3-6 months\", \"2-3 years\"). No calendar year, month, quarter label, or absolute date appears anywhere in the payload. (4) MACRO EVENT GENERICIZATION: compliant — the sole macro line is \"Industrial capex steady; software multiples compressed vs prior cycle,\" and the valuation note references \"a prior overshoot\" and \"a forgotten ordinary listing.\" All are era-agnostic; no named crisis, rate-cycle event, pandemic, election, index level, or dated market episode is present that would let the Trader anchor to a real period. No forbidden terms were available to leak (denylist is empty) and I added none. Per instruction I make no attempt to identify a company and offer no view on the eventual outcome. Confidence HIGH: the determination rests entirely on the frozen payload text supplied in full, and requires no external or time-sensitive data."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "CLEANLINESS: replay.response_raw is exactly one JSON object with no prose, headings, or code fences before/after; every schema field (ticker, action, size, instrument, order_type, time_in_force, rationale_summary, expected_impact{concentration_delta,cash_delta,tax_estimate}, confidence, cited_sources, falsifiers, revisit_triggers) is populated and parses to replay.output_full without drift.",
      "CLEANLINESS: replay.output_audit reports contaminated=false, contamination_hits=[], suspect_terms=[]. The single calendar token '2026' is the estimated dated_event '2026-11-30', which is consistent with the session date (2026-08-30) and is explicitly self-labelled '(estimated; Q0 just reported, so roughly one quarter out)' — it anchors to the run date, not to any era-identifying packet fact, and the packet itself remains offset-relative (FY-3/FY-2/FY-1, 'Q0 just reported').",
      "PACKET FIDELITY (numerics traced): '~31%' = latest_quarter.revenue_growth_yoy_pct 31; 'gross margin ~74%' = FY-1 gross_margin_pct 74.5; 'existing customers spending 22% more' = net_revenue_retention_pct 122; 'first sustained profits and positive cash flow' = FY-1 net_income_usd_m 28 and free_cash_flow_usd_m 55 plus Q0 gaap_net_income_usd_m 14; 'net cash on the balance sheet' = cash_and_equivalents_usd_m 410 vs total_debt_usd_m 90; FY-1 growth +33.3% = revenue_growth_pct 33.3; 'stock-based comp ~8% of revenue' = news/NLF recent_developments.",
      "PACKET FIDELITY (derived figure disclosed as derived): the cash-generation falsifier's 'implied trailing net income near $51M' is arithmetic on packet values pe_trailing 55 and market_cap_usd_m 2800 (2800/55 = 50.9) and is labelled 'implied', not asserted as a reported figure.",
      "PACKET FIDELITY (no fabrication where data absent): expected_impact.cash_delta states the portfolio dollar total was not supplied and therefore expresses size as percent-of-portfolio rather than a notional or share count; tax_estimate declines to state any rate, noting no domain_knowledge tax file was supplied — consistent with the citation rule and with the constraint packet, which gives no portfolio total and no tax file.",
      "CONSTRAINT FIDELITY: sizing 1.5% sits at the mid-point of the stated '~1-2% per position' sleeve rule and is stated to keep the high-potential sleeve inside its '~5% cap'; instrument='stock', order_type='market', limit_price=null, stop_price=null honours 'no leverage, no options'; the 2-3 year re-rating band and 'BUY->EXIT' transitions are consistent with the 5+ year intended hold.",
      "CONSTRAINT FIDELITY (reporting requirement): both mandated elements are present inside the recommendation payload — (1) FALSIFIERS: four checkable conditions (NRR <105% single quarter; revenue growth <+18% YoY for two consecutive quarters WHILE margin stops expanding; gross margin <70% for two consecutive quarters; TTM FCF <$0M), each with baseline, as-of, source report and decision transition, and each verified to sit away from the current actual so none is already-true; (2) THE CLOCK: next validation point given as the estimated next quarterly print with an explicit '2-3 years' re-rating band AND an explicit statement that the horizon is genuinely uncertain because the name is unfollowed with no visible catalyst calendar — the constraint's requirement that an unestimable horizon be named as such, and tied to the MEDIUM (not HIGH) conviction.",
      "CITATIONS: cited_sources ['fundamentals/NLF','news/NLF'] match the packet report_name fields verbatim; no invented, abbreviated, or paraphrased source ids appear. The top-level sources array is empty, so no external/market-data URL claim was available or made.",
      "WORKFLOW: stage-2 sanitizer ran first and returned safe_to_run=true, leaked_terms=[], verification.ok=true with failures=[]; the trader stage then executed against the sanitized packet. The trader made no attempt to identify a real company, honouring the provenance note ('FULLY FICTIONAL benchmark control ... do not attempt to identify a real company'). Machine-checkable falsifiers are mirrored as metric_condition revisit_triggers (net_revenue_retention_pct <105, ttm_free_cash_flow_usd_m <0); the two persistence-based falsifiers are explicitly declared '(qualitative, persistence required — no point trigger)' rather than silently omitted.",
      "GROUNDING GAP 1 (minor, non-blocking): rationale_summary says the name 'trades near 3.4x sales'. 3.4 is packet ev_s_trailing (enterprise-value/sales); the price-to-sales figure in the same packet block is ps_trailing 3.9. Both numbers exist in the packet and the EV multiple is the more conservative-of-the-two framing given net cash, but the label 'x sales' attached to the EV figure in a plain-language summary is imprecise.",
      "GROUNDING GAP 2 (minor, non-blocking): the sanitizer's flagged internal tension — pe_trailing 55 implies TTM net income ~51 against FY-1 net_income_usd_m 28 — is used by the trader (as the $51M implied figure) but never surfaced as a caveat, even though the same falsifier text argues the profitability inflection is 'the newest and thinnest part of this thesis'. The inflection framing is present; the explicit reconciliation is not.",
      "SCOPE: this review grades cleanliness, packet/constraint fidelity, workflow order and grounding of the VISIBLE written rationale only. No hidden chain-of-thought was available or inspected, and no view is expressed on whether the buy decision was correct."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Replay of case_id `nlf_synthetic` (alias NLF) audited against the persisted packet only. VERDICT: output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3 (grounded with minor gaps). CLEANLINESS: response_raw is a single well-formed JSON object with no surrounding prose and parses to output_full without drift; output_audit reports contaminated=false with no hits or suspect terms. The only calendar token, the estimated dated_event 2026-11-30, tracks the session date (2026-08-30) rather than any packet fact, is self-labelled '(estimated; Q0 just reported, so roughly one quarter out)', and does not re-anchor the offset-relative packet — non-blocking. PACKET FIDELITY: every numeric in the rationale and falsifiers traces to a supplied field — 31% YoY growth, 74.5% gross margin, 122% NRR, FY-1 net income 28 / FCF +55, cash 410 vs debt 90, FY-1 growth 33.3%, SBC ~8% of revenue; the one derived number (implied TTM net income ~$51M from pe_trailing 55 on a $2,800M market cap) is arithmetically correct and labelled 'implied'. Where the packet is silent the output declines to fabricate: no portfolio notional or share count (positions gave no dollar total) and no tax rate (no domain_knowledge file supplied, stated explicitly). cited_sources match report_name values verbatim; no invented ids. CONSTRAINT FIDELITY: 1.5% sizing is the mid-point of the '~1-2% per position' rule and stays inside the ~5% sleeve cap; stock/market with null limit and stop honours 'no leverage, no options'; the mandated FALSIFIERS and THE CLOCK are both present inside the recommendation, with four checkable conditions carrying baselines, as-of anchors, sources and decision transitions (all verified not already-true), plus a next validation point, an honest '2-3 years' band, and an explicit statement that the horizon is genuinely uncertain — which the output ties to its MEDIUM rather than HIGH conviction, exactly as the constraint demands. WORKFLOW: stage-2 sanitizer ran first (safe_to_run=true, leaked_terms=[], verification ok), the trader ran on the sanitized packet, made no attempt to identify a real company per the provenance note, mirrored the two machine-checkable falsifiers as metric_condition triggers, and openly declared the two persistence-based ones as qualitative with no point trigger rather than dropping them. GROUNDING: the written rationale is stepwise traceable — thesis (expansion inside existing plants), valuation (multiple vs growth), and risk (small TAM, ERP bundling) each map to named packet lines, and each falsifier states its baseline and source. Two minor gaps hold the score at 3 rather than 4: (a) the summary calls 3.4x a 'sales' multiple when 3.4 is the packet's EV/sales and 3.9 is its price/sales; (b) the sanitizer-flagged tension between pe_trailing 55 (implied TTM NI ~51) and FY-1 net income 28 is used but never reconciled aloud, despite the same passage calling the profitability inflection the thinnest part of the thesis. Neither gap contradicts a packet fact or a constraint, so no violation is recorded. Confidence HIGH: the packet, constraints, sanitizer record and full raw response were supplied verbatim, making this a deterministic textual audit requiring no external or time-sensitive data. No hidden reasoning was accessed and no view is offered on investment correctness.",
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": null,
    "benchmark_return_pct": null,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Class match is deterministic: fleet_result.action = \"buy\" (grading input, fleet_result.action) and answer_key.expected_classes = [\"buy\"] (grading input, answer_key.expected_classes), so the verdict is in-class. Grading rule recorded for this case is \"synthetic_winner\" (grading input, grading), and the benchmark rule reserves 0.5 only for explicitly enumerated partial-credit situations (an F1 pass carrying falsifiers, or a small trap buy that names the killing falsifier); neither applies to a straight in-class winner, so the full 1.0 is awarded rather than a partial. positioned=false (grading input, positioned) means no pre-existing exposure discounts the call. Returns are not computable: answer_key.resolution is null (grading input, answer_key.resolution) — this synthetic control ships no price path — so acted_return_pct and benchmark_return_pct are reported null rather than estimated; per the no-fabrication rule no figure is invented, and outcome luck would not have altered the class grade in any event. The stage-4 replay review is accepted as-is and not reopened: output_clean=true, packet_fidelity=true, workflow_correct=true, violations=[], reasoning_grounded_score=3 (review.output). Its two flagged grounding gaps (3.4x labelled \"sales\" when 3.4 is the packet EV/sales versus ps_trailing 3.9; the unreconciled pe_trailing 55 vs FY-1 net income 28 tension) were recorded by the reviewer as minor and non-blocking with no violation, and under the recorded rule cleanliness/grounding nuances of this kind do not reduce an in-class score. The fleet's own MEDIUM confidence (fleet_result.confidence) reflects thesis uncertainty on a fictional issuer and likewise does not affect class scoring. Confidence HIGH: every input needed for the class comparison — action, expected_classes, grading rule, positioned flag, and the completed review — was supplied verbatim, making this a closed deterministic check requiring no external or time-sensitive data.",
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
- notes: Class match is deterministic: fleet_result.action = "buy" (grading input, fleet_result.action) and answer_key.expected_classes = ["buy"] (grading input, answer_key.expected_classes), so the verdict is in-class. Grading rule recorded for this case is "synthetic_winner" (grading input, grading), and the benchmark rule reserves 0.5 only for explicitly enumerated partial-credit situations (an F1 pass carrying falsifiers, or a small trap buy that names the killing falsifier); neither applies to a straight in-class winner, so the full 1.0 is awarded rather than a partial. positioned=false (grading input, positioned) means no pre-existing exposure discounts the call. Returns are not computable: answer_key.resolution is null (grading input, answer_key.resolution) — this synthetic control ships no price path — so acted_return_pct and benchmark_return_pct are reported null rather than estimated; per the no-fabrication rule no figure is invented, and outcome luck would not have altered the class grade in any event. The stage-4 replay review is accepted as-is and not reopened: output_clean=true, packet_fidelity=true, workflow_correct=true, violations=[], reasoning_grounded_score=3 (review.output). Its two flagged grounding gaps (3.4x labelled "sales" when 3.4 is the packet EV/sales versus ps_trailing 3.9; the unreconciled pe_trailing 55 vs FY-1 net income 28 tension) were recorded by the reviewer as minor and non-blocking with no violation, and under the recorded rule cleanliness/grounding nuances of this kind do not reduce an in-class score. The fleet's own MEDIUM confidence (fleet_result.confidence) reflects thesis uncertainty on a fictional issuer and likewise does not affect class scoring. Confidence HIGH: every input needed for the class comparison — action, expected_classes, grading rule, positioned flag, and the completed review — was supplied verbatim, making this a closed deterministic check requiring no external or time-sensitive data.

### omk_synthetic — real: SYNTHETIC trap-shape (no real company) — meal-kit / D2C growth theater
- **Fleet reasoning:** hold (HIGH) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2027']
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=3/4
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Alias 'OMK' used consistently in alias field, report_name 'fundamentals/OMK' and 'news/OMK', and positions ('No existing position in OMK'). Only expanded name is the invented 'Orbit Meal Kits (alias OMK)' in analyst_reports[0].company, permissible under synthetic=true. No ticker, exchange, executive name, HQ/geography identifier, or listing venue present. forbidden_terms is empty, so no denylist entries could be or were matched; none were introduced into this output.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "synthetic=true, rescale_factor=null, raw_sources=[] and source_manifest=[] — no independently constructed manifest exists, and by construction no real-world originals exist to rescale. Substituted internal-consistency check, computed only from payload fields, and it holds: 1180/890=+32.6% and 1260/1180=+6.8% match stated growth; 3600/1260=2.86~=ps 2.9; EV 3600+620-480=3740, /1260=2.97~=ev_s 3.0; pe_trailing null is correct against losses -210/-340/-410; GM 34.0->28.2 = 580bps ~= news '~600 bps'; Q0 rev 310 at +4.5% YoY implies ~297 prior-year quarter, coherent with FY-0 1260 (~315/qtr); cash 480 vs FY-1 FCF -290 supports '<2 years of burn'. Sole nit: news says '+63% to +7%' vs table 62%/6.8% — rounding drift only, not a contradiction.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "All temporal anchors are relative: 'FY-2', 'FY-1', 'FY-0 (just ended)', 'Q0 just reported', as_of 'current', 'Rebranded last year', 'in two years', '+40% YTD', '-65% from listing-era high'. No calendar year, month, absolute quarter label, or explicit date string appears anywhere in candidate_payload. The '3-6 months' / '2-3 years' strings in constraints_rendered are illustrative horizon-band examples in the reporting instructions, not datestamps.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context = 'Consumer subscription fatigue; paid-social CAC inflation; growth-stock de-rating still in force' — standing conditions only, with no named macro event, no policy body or rate-cycle reference, no crisis label, and no date anchor. Supporting narrative ('discounting and shipping inflation', 'CAC treadmill') is likewise generic sector mechanics. 'listing-era high' refers to the IPO period without dating or naming it.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audit of case_id `omk_synthetic` (synthetic=true, forbidden_terms=[], raw_sources=[], source_manifest=[], rescale_factor=null). No concrete leak found; safe_to_run=true.\n\nALIAS: \"OMK\" is applied consistently across `alias`, `report_name` (\"fundamentals/OMK\", \"news/OMK\"), and `positions` (\"No existing position in OMK\"). The only expanded name is \"Orbit Meal Kits (alias OMK)\" in `analyst_reports[0].company`, which is an invented label under the declared synthetic condition, not a real-world identity. No tickers, exchanges, executive names, city/HQ identifiers, or listing venues appear. forbidden_terms is empty, so there is no denylist to match against and none of its contents were introduced.\n\nABSOLUTE-FIGURE RESCALING: not_applicable per protocol — synthetic=true, rescale_factor=null, and both raw_sources and source_manifest are empty, so there is no independently constructed manifest from which scaling could be proven, and by construction no real originals exist. Substituted internal-consistency verification, all arithmetic recomputed from payload fields only: FY-1 rev 1180 / FY-2 rev 890 = +32.6% (matches stated revenue_growth_pct 32.6); FY-0 1260 / FY-1 1180 = +6.8% (matches 6.8); ps_trailing 3600 / 1260 = 2.86 ≈ stated 2.9; EV = 3600 + 620 debt − 480 cash = 3740, /1260 = 2.97 ≈ stated ev_s_trailing 3.0; pe_trailing correctly null against consecutive GAAP losses (−210 / −340 / −410); gross margin 34.0 → 28.2 = 580 bps, consistent with the news report's \"~600 bps\"; latest-quarter rev 310 at +4.5% YoY implies a ~297 prior-year quarter, coherent with FY-0 1260 (~315/qtr average) and with the stated sequential deceleration; cash 480 against FY-1 FCF −290 and widening net loss supports the \"<2 years of burn\" note. One immaterial rounding nit, flagged but not disqualifying: the news report says growth \"collapsed from +63% to +7%\" while the fundamentals table states 62% and 6.8% — rounding drift, not a contradiction.\n\nRELATIVE DATES: fully relativized throughout — \"FY-2\", \"FY-1\", \"FY-0 (just ended)\", \"Q0 just reported\", as_of \"current\", \"Rebranded last year\", \"in two years\", \"+40% YTD\", \"-65% from listing-era high\". No calendar year, month, quarter label, or absolute date appears anywhere in the payload. The \"3-6 months\"/\"2-3 years\" strings in constraints_rendered are illustrative horizon bands, not datestamps.\n\nMACRO-EVENT GENERICIZATION: macro_context reads \"Consumer subscription fatigue; paid-social CAC inflation; growth-stock de-rating still in force\" — all described as standing conditions with no named event, no policy body, no crisis label, and no date anchor. \"listing-era high\" references the IPO period generically without dating it.\n\nTWO NON-BLOCKING OBSERVATIONS for the orchestrator (neither is a leak, neither changes safe_to_run): (1) constraints_rendered discloses to the downstream Trader that it is reading \"a FULLY FICTIONAL benchmark control\" — this is meta-disclosure of the test condition rather than an answer-key leak, but it may itself perturb production Trader behavior and is worth a policy decision upstream. (2) The \"fully fictional / no original figure set\" provenance is self-asserted inside the candidate payload and cannot be corroborated here, since the manifest and raw_sources arrived empty; that unverifiable self-assertion is the reason confidence is MEDIUM rather than HIGH. Per the missing-fact rule, if independent confirmation of synthetic provenance is wanted, the domain-refresh agent should supply a non-empty source_manifest — I did not fabricate one. No company identification and no outcome inference is offered."
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
  "model": "claude-opus-5",
  "output": {
    "confidence": "HIGH",
    "grounding_evidence": [
      "Packet fundamentals/OMK FY-2 revenue_growth_pct=62 and latest_quarter.revenue_growth_yoy_pct=4.5 support output claim 'growth has fallen from 62% to under 5%'.",
      "Packet gross_margin_pct series 34 -> 31.5 -> 28.2 with latest_quarter 27.5 supports 'gross margin keeps sliding' and the falsifier baseline '27.5%'.",
      "Packet FY-0 contribution_profit_after_marketing_usd_m=-55 and latest_quarter contribution_profit_after_marketing_usd_m=-18 support 'after marketing costs it now loses money on the customers it buys' and the '-$18M for the quarter and -$55M for FY-0' falsifier baseline.",
      "Packet balance_sheet note 'cash funds <2 years of current burn if growth spend continues' with cash_and_equivalents_usd_m=480 and total_debt_usd_m=620 supports 'Cash covers under two years at this burn' and the $480M/$620M figures in falsifier 5.",
      "Packet news/OMK item 'Management highlights \"active customer\" definitions that exclude lapsed cohorts; a short note questioned the LTV math' is the exact basis for the STRUCTURAL falsifier's contested-cohort-economics condition.",
      "Packet report_name values 'fundamentals/OMK' and 'news/OMK' appear verbatim as the output's cited_sources — no invented ids.",
      "constraints_rendered REPORTING REQUIREMENT demands FALSIFIERS plus THE CLOCK (next validation point + honest horizon band); output supplies a 5-item falsifiers array and rationale_summary states 'Next test is the coming quarterly print; a genuine turn would take 2-3 years.'",
      "constraints_rendered sleeve rule '~1-2% per position' is mirrored in expected_impact.concentration_delta, which states the sleeve budget 'stays uncommitted and available'; size_shares_or_currency=0.0 is consistent.",
      "Packet FY-1 free_cash_flow_usd_m=-290 and FY-0 net_income_usd_m=-410 show the source of the '-$290M to -$410M annual cash burn' range, confirming the second endpoint is a net-income figure rather than a cash-flow figure.",
      "replay.output_audit reports contaminated=false, contamination_hits=[], suspect_terms=[], with calendar_years_in_output=['2027'] traceable solely to revisit_triggers[0].date='2027-03-31'."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Replay audit of case_id `omk_synthetic` (Trader stage output vs. persisted packet + rendered constraints). Verdict correctness was NOT assessed; only cleanliness, fidelity, workflow, and grounding of the visible rationale.\\n\\nOUTPUT CLEANLINESS — PASS. `response_raw` is exactly one JSON object: no prose preamble, no trailing commentary, no code fences, no headings. It parses and matches `output_full` field-for-field. All emitted fields are schema-shaped (nulls used correctly for `limit_price`, `stop_price`, and the unused `metric`/`op`/`value`/`price` trigger slots). The persisted `output_audit` reports contaminated=false with no contamination hits or suspect terms; the only calendar year present (\"2027\") comes from the trader's own forward-looking revisit-trigger date, not from leaked packet content — the packet itself is fully relativized (FY-2 / FY-1 / FY-0 / Q0 / as_of \"current\"), consistent with the sanitizer's relative_dates pass.\\n\\nPACKET FIDELITY — PASS with two minor numeric imprecisions. Verified every quantitative assertion against the packet: \"growth has fallen from 62% to under 5%\" matches FY-2 revenue_growth_pct 62 and latest_quarter revenue_growth_yoy_pct 4.5; \"gross margin keeps sliding\" matches 34 -> 31.5 -> 28.2 -> 27.5; contribution profit after marketing -$18M (Q0) and -$55M (FY-0) are verbatim packet values; \"Cash covers under two years at this burn\" restates the packet balance-sheet note \"cash funds <2 years of current burn if growth spend continues\"; $480M cash and $620M total debt are exact. Both `cited_sources` (\"fundamentals/OMK\", \"news/OMK\") appear verbatim as `report_name` values in the packet — no invented or abbreviated source ids. Constraint fidelity holds: no position opened, sleeve budget (~1-2%) explicitly left uncommitted, no leverage, no options, size 0.0 shares, tax impact correctly stated as none (no Israeli CGT event without a transaction). MINOR-1: falsifier 5 describes \"roughly -$290M to -$410M annual cash burn\" — -$290M is the FY-1 free_cash_flow_usd_m, but -$410M is FY-0 GAAP net_income_usd_m, not a cash-flow figure; FY-0 FCF is not supplied in the packet, so net loss is being used as a cash-burn proxy (hedged with \"roughly\", but mislabeled). MINOR-2: falsifier 2 says GM is \"27.5%, down from 34% two years ago\" — 34% is the FY-2 figure and 27.5% is the post-FY-0 Q0 figure, so the true span is closer to three fiscal periods; directionally right, loosely dated.\\n\\nWORKFLOW — PASS. The rendered constraints demanded, inside the recommendation, (1) FALSIFIERS and (2) THE CLOCK. Both are delivered. Falsifiers are directionally correct for a pass thesis (each names the condition that would flip the stance to ADD), each carries an explicit source attribution, an as-of anchor, a current baseline, a numeric threshold, a persistence requirement (\"two consecutive quarters\"), and a decision transition. The one genuinely unquantifiable condition is explicitly flagged \"STRUCTURAL (qualitative, no numeric threshold available)\" rather than being given a fabricated threshold, and the one condition that would confirm rather than kill the thesis is correctly labeled \"ACCELERANT (confirms the pass, does NOT falsify it)\" — good category discipline that avoids padding the falsifier list. THE CLOCK is present in `rationale_summary`: next validation point = \"the coming quarterly print\", re-rating horizon reported as an honest band, \"2-3 years\". A machine-readable `revisit_triggers` entry backs it with an estimated date and a HOLD->ADD transition. NOTE (non-blocking): mild internal tension between the prose next-validation-point (\"the coming quarterly print\") and the trigger date 2027-03-31, whose label calls for \"two more reported quarters\" — the dated trigger sits one quarter past the stated next test.\\n\\nREASONING GROUNDING (visible rationale only; no claim to hidden reasoning) — 3/4. Each step of the written rationale traces to a packet fact: the CAC-treadmill claim maps to news/OMK \"Revenue growth collapsed... while marketing intensity stayed high\"; \"loses money on the customers it buys\" maps to the negative contribution-profit-after-marketing lines; the growth and margin numbers are exact packet values; the runway claim restates the packet's own note. The one inferential leap — \"a dilutive raise is likely before the story turns\" — is not stated in the packet but is a reasonable, clearly-signposted inference from $480M cash, $620M debt, and the packet's sub-two-year runway note. Docked from 4 for MINOR-1 and MINOR-2 above: a net-income figure presented under a cash-burn label, and a loose period attribution on the margin comparison.\\n\\nCONFIDENCE: HIGH — full packet and full replay were available, and every check was a deterministic comparison against supplied fields; nothing required external or stale data.",
    "violations": [
      "MINOR (numeric labeling): falsifier 5 cites 'roughly -$290M to -$410M annual cash burn'; -$410M is FY-0 net_income_usd_m (GAAP net loss), not a cash-flow figure. Packet supplies free_cash_flow_usd_m only for FY-1 (-290); FY-0 FCF is absent, so net loss is silently substituted as a burn proxy.",
      "MINOR (period attribution): falsifier 2 states gross margin is 'down from 34% two years ago'; 34% is the FY-2 value and 27.5% is the post-FY-0 Q0 value, spanning closer to three reporting periods than two.",
      "NOTE (internal consistency, non-blocking): rationale_summary names 'the coming quarterly print' as the next test, while revisit_triggers dates the checkpoint at 2027-03-31 with a label requiring 'two more reported quarters' — one quarter later than the stated next validation point.",
      "NOTE (packet-vs-output dating, non-blocking): the packet is fully relativized with no calendar dates; the output introduces the absolute date '2027-03-31'. This is permitted by the constraint's 'dated or estimated upcoming event' wording and is an estimate, not leaked packet content, but it is the sole origin of the calendar year flagged in output_audit."
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
  "model": "claude-opus-5",
  "output": {
    "acted_return_pct": null,
    "benchmark_return_pct": null,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case `omk_synthetic` (category=synthetic, grading rule=synthetic_trap). Answer key expected_classes=[\"hold\"]; fleet_result.action=\"hold\" with size=0.0 shares and positioned=false — a verbatim in-class verdict, so the rule's full-credit branch applies (1.0). The partial-credit branch (0.5) is reserved for an F1 pass carrying falsifiers or a small trap buy that names the killing falsifier; neither applies here because no trap position was opened at all and the verdict is exactly in class, so no discount is warranted. The trap is the classic CAC-treadmill growth story, and the fleet declined it on the packet's own economics: growth decay 62% -> 4.5% YoY, gross margin 34 -> 31.5 -> 28.2 -> 27.5%, contribution profit after marketing -$18M (Q0) / -$55M (FY-0), and $480M cash against $620M debt with a sub-two-year runway note — all confirmed against the persisted packet by the stage-4 replay review (source_id: fleet_result, review.output.grounding_evidence). Returns: answer_key.resolution is null and the fleet took no position (size=0.0), so neither acted_return_pct nor benchmark_return_pct is computable; per the rule outcome luck does not move the class grade in any case, so their absence does not affect the score. Cleanliness/workflow judgment is not reopened: the reviewer's output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4 stand as recorded, and its two MINOR findings (a FY-0 net-loss figure labeled as cash burn; a loose \"two years ago\" margin span) plus two non-blocking NOTES are documentation-quality flags inside a falsifier list, not class-determining errors — the grading rule scores the verdict class, not narrative precision. Confidence HIGH: this is a deterministic comparison of a supplied verdict against a supplied answer key with a complete replay record; nothing depended on external or stale market data. No domain_knowledge tax/regulatory rate is asserted here, and the fleet correctly recorded no Israeli CGT event since no transaction occurred.",
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
- notes: Case `omk_synthetic` (category=synthetic, grading rule=synthetic_trap). Answer key expected_classes=["hold"]; fleet_result.action="hold" with size=0.0 shares and positioned=false — a verbatim in-class verdict, so the rule's full-credit branch applies (1.0). The partial-credit branch (0.5) is reserved for an F1 pass carrying falsifiers or a small trap buy that names the killing falsifier; neither applies here because no trap position was opened at all and the verdict is exactly in class, so no discount is warranted. The trap is the classic CAC-treadmill growth story, and the fleet declined it on the packet's own economics: growth decay 62% -> 4.5% YoY, gross margin 34 -> 31.5 -> 28.2 -> 27.5%, contribution profit after marketing -$18M (Q0) / -$55M (FY-0), and $480M cash against $620M debt with a sub-two-year runway note — all confirmed against the persisted packet by the stage-4 replay review (source_id: fleet_result, review.output.grounding_evidence). Returns: answer_key.resolution is null and the fleet took no position (size=0.0), so neither acted_return_pct nor benchmark_return_pct is computable; per the rule outcome luck does not move the class grade in any case, so their absence does not affect the score. Cleanliness/workflow judgment is not reopened: the reviewer's output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4 stand as recorded, and its two MINOR findings (a FY-0 net-loss figure labeled as cash burn; a loose "two years ago" margin span) plus two non-blocking NOTES are documentation-quality flags inside a falsifier list, not class-determining errors — the grading rule scores the verdict class, not narrative precision. Confidence HIGH: this is a deterministic comparison of a supplied verdict against a supplied answer key with a complete replay record; nothing depended on external or stale market data. No domain_knowledge tax/regulatory rate is asserted here, and the fleet correctly recorded no Israeli CGT event since no transaction occurred.
