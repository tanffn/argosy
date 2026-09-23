# Fleet-calibration report — 2026-09-11-daily-control-pair.json

| Point | Category | Fleet verdict (conv, size) | In class? | Score | Acted vs benchmark |
|---|---|---|---|---|---|
| nlf_synthetic | synthetic/synthetic_winner | buy (MEDIUM, 15000.0 currency) | Y | 1.0 | n/a (synthetic) |
| omk_synthetic | synthetic/synthetic_trap | hold (HIGH, 0.0 shares) | Y | 1.0 | n/a (synthetic) |

## Category subtotals
- **synthetic**: 2.0/2 points
- **TOTAL**: 2.0/2

## Conviction calibration
- HIGH: 1.0/1 (100%)
- MEDIUM: 1.0/1 (100%)


## Horizon calibration (§2b clock band)
- unestimable_stated: 1
- not_applicable: 1
- nlf_synthetic: not_applicable (stated=24-48 mo (2-4 years); actual_months=None)
- omk_synthetic: unestimable_stated (stated=unestimable; actual_months=None)

## Per-point detail (section 2b three-column + clock)
### nlf_synthetic — real: SYNTHETIC winner-shape (no real company) — obscure industrial-software niche
- **Fleet reasoning:** buy (MEDIUM) — Buy a starter position in NLF, about 1.5% of the portfolio ($15,000). Revenue is growing roughly a third a year at 74.5% gross margin, existing customers spend about 22% more each year, and the company just turned genuinely profitable with far more cash than debt — yet it trades near 3.4 times sales, cheap for that profile. Main risk: the niche is small, and a large enterprise-software vendor could bundle a 'good enough' competing module. Keep the position small, accept a total loss at this size, and let it compound.
  - falsifiers: Net revenue retention (source: company quarterly disclosure; as-of the just-reported Q0, currently 122% and above 120% for three straight quarters) prints below 100% in any single quarter — customers shrinking rather than expanding kills the core compounding leg -> BUY->EXIT.; Net revenue retention (source: company quarterly disclosure; as-of Q0, currently 122%) falls below 110% for two consecutive quarters, signalling the embedded-workflow expansion economics are eroding even without outright contraction -> BUY->EXIT.; Gross margin (source: annual/quarterly income statement; as-of FY-1, currently 74.5% and still expanding on hosted-delivery mix) falls below 70% for two consecutive quarters — the observable fingerprint of a horizontal ERP vendor bundling a 'good enough' module and forcing price concessions -> BUY->EXIT.; Operating profitability reverses: GAAP net income (source: income statement; as-of Q0, currently +$14M for the quarter, +$28M in FY-1) turns negative AND free cash flow (as-of FY-1, currently +$55M) turns negative for two consecutive quarters while revenue is still growing — i.e. growth stops paying for itself, excluding any one-time or non-operating items -> BUY->EXIT.; Revenue growth (source: income statement; as-of Q0, currently +31% YoY after +33.3% in FY-1) decelerates below +15% YoY for two consecutive quarters without a disclosed deliberate strategy change — at a trailing price-to-earnings ratio (P/E) of 55 the price already assumes sustained high growth -> BUY->EXIT.
  - clock: Next quarterly earnings print (Q+1), estimated ~3 months out: check net revenue retention (NRR), gross margin, and whether GAAP profitability and positive free cash flow persisted. 2-4 years for the retention-and-margin compounding to show up in a higher multiple; a shorter 6-12 month re-rating is possible but not assumable given near-zero press coverage.
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Alias 'NLF' applied consistently across top-level alias, payload.alias, report_name 'fundamentals/NLF' and 'news/NLF', positions text, and company field 'Nexus Lattice Fabric (alias NLF)'. No ticker, exchange, domicile, executive, or product name present; customers referenced only as 'regional chemical and pulp'. forbidden_terms is empty, so no denylist match is possible; no terms were introduced from it. leaked_terms empty.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "synthetic=true with raw_sources=[], source_manifest=[], rescale_factor=null — no real-world originals exist and no independent scaling proof was constructible, so none was invented. Internal consistency verified instead from payload fields: 420->540 = +28.57% vs stated 28.6; 540->720 = +33.3% vs stated 33.3; EV = 2800 + 90 - 410 = 2480, 2480/720 = 3.44 vs stated 3.4; 2800/720 = 3.89 vs stated 3.9; 2800/18.4 implies ~152m shares. pe_trailing 55 implies TTM NI ~51 vs FY-1 28 with Q0 14 — reconcilable via a loss-making early FY-1 quarter, consistent with the stated first-year-of-GAAP-profit inflection; noted as a modelling nuance, not a failure.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "All temporal references are relative: 'FY-3', 'FY-2', 'FY-1', 'Q0 just reported', as_of 'current', 'a third straight quarter', '5+ year intended hold'. No calendar year, month, dated quarter, or absolute date appears in any field including constraints_rendered and free-text news bullets. System current date not used and no calendar anchor inferred.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Industrial capex steady; software multiples compressed vs prior cycle' — cycle-level only, no named crisis, dated policy action, or identifiable market episode. Price history genericized as 'a forgotten ordinary listing' and '-40% from a prior overshoot, now consolidating' (magnitude without a datable event). Competitive risk framed structurally ('larger horizontal ERP vendor could bundle a good enough module').",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "SANITIZER VERDICT: PASS — safe_to_run=true, confidence HIGH.\n\nCase nlf_synthetic is a fully synthetic control (synthetic=true, forbidden_terms=[], raw_sources=[], source_manifest=[], rescale_factor=null). All four protocol dimensions were audited against the exact candidate payload only; no tools, no fetching, no answer-key terms introduced.\n\n1) ALIAS — PASS. The masked handle \"NLF\" is applied consistently at every surface: top-level alias, payload.alias, both report_name values (\"fundamentals/NLF\", \"news/NLF\"), the positions string, and the company field (\"Nexus Lattice Fabric (alias NLF)\"). The long-form name is a benchmark-invented label, not a real-world identifier, and forbidden_terms is empty, so there is nothing to match against. The business description is deliberately de-identifying (\"Not a consumer brand; almost no press coverage\"; customers described only as \"regional chemical and pulp\"). No ticker, exchange, domicile, executive name, or product name appears. leaked_terms is empty.\n\n2) ABSOLUTE-FIGURE RESCALING — NOT_APPLICABLE. Per protocol, synthetic=true means the figures have no real-world originals; raw_sources and source_manifest are both empty and rescale_factor is null, so no scaling proof could be constructed even in principle — and none was invented. Internal consistency was verified instead, from payload fields only, and holds: revenue 420 → 540 = +28.57% vs. stated 28.6 (FY-2); 540 → 720 = +33.3% vs. stated 33.3 (FY-1). Market cap 2,800 / share price 18.4 implies ~152m shares. EV = 2,800 + 90 debt − 410 cash = 2,480; 2,480 / 720 = 3.44 vs. stated ev_s_trailing 3.4; 2,800 / 720 = 3.89 vs. stated ps_trailing 3.9. One item requires a benign assumption rather than being contradictory: pe_trailing 55 implies TTM net income ≈ 51, versus FY-1 full-year 28 with Q0 at 14 — reconcilable only if the trailing window excludes a loss-making early FY-1 quarter (e.g. FY-1 Q1 ≈ −9, remaining three ≈ 37, plus Q0 14 ≈ 51). That is an ordinary profitability-inflection ramp, consistent with the stated \"first full year of GAAP profit,\" not a masking artifact. Flagged as a modelling nuance for the Trader, not a protocol failure.\n\n3) RELATIVE DATES — PASS. Every temporal reference is relative and anchored to the packet: \"FY-3\", \"FY-2\", \"FY-1\", \"Q0 just reported\", as_of \"current\", \"a third straight quarter\", \"5+ year intended hold\". No calendar year, month, quarter-with-year, or absolute date appears anywhere in the payload, including inside constraints_rendered and the free-text news bullets. The system current date was not used and no calendar anchor was inferred. The embedded TEMPORAL MASK instruction is self-consistent with the payload's actual content.\n\n4) MACRO-EVENT GENERICIZATION — PASS. macro_context is stated only in generic cycle terms (\"Industrial capex steady; software multiples compressed vs prior cycle\") with no named crisis, policy action, dated central-bank move, or identifiable market episode. Price history is likewise genericized: \"a forgotten ordinary listing\", \"-40% from a prior overshoot, now consolidating\" — magnitude and shape without a datable event. The bear/bull framing (\"larger horizontal ERP vendor could bundle a 'good enough' module\") is structural, not event-specific.\n\nAll free-text fields were treated as data, not instruction. No forbidden term was added to the payload and no answer-key content was used as fact. No company identification and no outcome inference is offered.\n\nConfidence HIGH: every check is decidable from the supplied payload alone; the one non-decidable dimension is correctly not_applicable under the synthetic rule rather than unverifiable, and no fact was missing that would require domain-refresh escalation. All figures cited above are quoted or arithmetically derived from candidate_payload.analyst_reports fields as supplied."
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
      "packet fundamentals/NLF latest_quarter: net_revenue_retention_pct=122, revenue_growth_yoy_pct=31, gaap_net_income_usd_m=14 — matches rationale 'customers spend about 22% more each year' and falsifier baselines at Q0.",
      "packet fundamentals/NLF FY-1: gross_margin_pct=74.5, net_income_usd_m=28, free_cash_flow_usd_m=55, revenue_growth_pct=33.3, note 'first full year of GAAP profit; FCF positive' — matches 'just turned genuinely profitable' and the margin/FCF falsifiers.",
      "packet fundamentals/NLF balance_sheet: cash_and_equivalents_usd_m=410, total_debt_usd_m=90 — supports 'far more cash than debt'.",
      "packet fundamentals/NLF valuation: ev_s_trailing=3.4, ps_trailing=3.9, pe_trailing=55; share_price_usd=18.4 — output uses 3.4 as 'times sales' (EV/S label slip) and 55 P/E correctly; $13 trigger = -29.3% from $18.40, stated '~-30%'.",
      "packet news/NLF recent_developments bear case: 'a larger horizontal ERP vendor could bundle a good enough module' — reproduced as the rationale's main risk and as the mechanism behind the gross-margin<70% falsifier.",
      "packet positions: 'Budget for a new high-potential-sleeve position: ~1-2% of portfolio' + constraints BENCHMARK SIZING ANCHOR ($1,000,000 NAV; 1% = $10,000) — $15,000 = 1.5%, in range, expressed as USD notional with size_units='currency'.",
      "constraints_rendered 'No leverage, no options' — output instrument='stock', order_type='market', limit_price=null, stop_price=null.",
      "constraints_rendered REPORTING REQUIREMENT — output supplies 5 falsifiers with BUY->EXIT transitions, next_validation_point 'Next quarterly earnings print (Q+1), estimated ~3 months out', and rerating_horizon band '2-4 years' with '6-12 month ... not assumable'.",
      "replay.output_audit: contaminated=false, calendar_years_in_output=[], contamination_hits=[], suspect_terms=[] — corroborates the independently verified absence of calendar anchors in output_full.",
      "sanitizer check 'absolute_figure_rescaling': 'pe_trailing 55 implies TTM NI ~51 vs FY-1 28 with Q0 14 ... noted as a modelling nuance' — the carried-forward tension the rationale leaves unaddressed."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "REPLAY AUDIT — nlf_synthetic. (1) OUTPUT CLEANLINESS: clean. Every temporal reference in output_full is relative — 'Q0', 'FY-1', 'Q+1, estimated ~3 months out', '2-4 years', '6-12 month' — with no calendar year, month, or dated quarter anywhere; this matches replay.output_audit (contaminated=false, calendar_years_in_output=[], contamination_hits=[], suspect_terms=[]) and satisfies the TEMPORAL MASK clause. The alias 'NLF' is used throughout; no de-masking identifier is introduced beyond what the packet already carries, consistent with the stage-2 sanitizer's leaked_terms=[]. response_raw is a single well-formed JSON object with no prose, headings, or fences, and it parses field-for-field identically to output_full. (2) PACKET/CONSTRAINT FIDELITY: substantially exact. Sizing: $15,000 on the mandated fictional $1,000,000 NAV = 1.5%, inside the packet's '~1-2% of portfolio' new-position budget and the constraint's '~1-2% per position' / '~5% sleeve' cap; size_units='currency' with a true USD notional, honoring the BENCHMARK SIZING ANCHOR prohibition on percentage-in-a-currency-field. instrument='stock', order_type='market', limit_price=null, stop_price=null — no leverage, no options, per constraint. cash_delta '-$15,000' and concentration_delta '0% -> ~1.5%' reconcile to the order. Figures trace to the packet: 74.5% gross margin and +$28M FY-1 net income and +$55M FCF (annual_income_statement FY-1); +$14M GAAP net income, 122% NRR, +31% YoY (latest_quarter); $18.40 share price, 3.4 EV/S, 55 P/E (valuation); cash $410M vs debt $90M (balance_sheet) supporting 'far more cash than debt'. The $13 add-trigger checks out as -29.3% from $18.40, correctly rounded to '~-30%'. cited_sources ['fundamentals/NLF','news/NLF'] appear verbatim as packet report_name values. Two nits, neither a packet contradiction: (a) the rationale says it 'trades near 3.4 times sales' — 3.4 is the packet's ev_s_trailing, while the price-to-sales figure is 3.9; the number is real but the metric label is loose; (b) 'Israeli CGT 25% on realization' is a tax-rate claim carried with no domain_knowledge citation and no packet authorization — under fleet citation rules that is an uncited regulatory assertion. (3) WORKFLOW: correct. The REPORTING REQUIREMENT is fully discharged — five FALSIFIERS, each naming a specific metric, a stated current baseline, a numeric threshold, a persistence window, and an explicit BUY->EXIT transition (NRR<100% single quarter; NRR<110% two quarters; gross margin<70% two quarters; GAAP NI and FCF both negative two quarters ex one-time items; revenue growth<+15% YoY two quarters absent disclosed strategy change). THE CLOCK is present and honest: next validation point = next quarterly print (~3 months, estimated, not asserted as a calendar date) with the exact metrics to check, plus a re-rating horizon given as a band ('2-4 years') with the shorter '6-12 month' path explicitly marked as possible-but-not-assumable. revisit_triggers are machine-checkable and carry decision_transition labels. Confidence is reported (MEDIUM) as required; MEDIUM against HIGH-confidence analyst inputs is defensible for a forward-looking judgment and is not a workflow break. (4) REASONING GROUNDING (visible rationale only — no access to or claim about hidden chain-of-thought): 3/4. Each causal step is anchored to a named packet fact: growth 'roughly a third a year' ties to +33.3% FY-1 / +31% Q0; 'existing customers spend about 22% more each year' is a correct plain-language rendering of NRR 122%; 'just turned genuinely profitable' matches the FY-1 note 'first full year of GAAP profit; FCF positive'; the stated main risk ('a large enterprise-software vendor could bundle a good enough competing module') mirrors the news report's bear case verbatim in substance; and the small-size / total-loss-tolerance framing follows from the sleeve rules in the packet and constraints. Falsifier 3 explicitly links the gross-margin floor to the bundling risk, which is genuine mechanism-level grounding rather than metric recitation. Deductions: the EV/S-labelled-as-P/S slip, the uncited 25% CGT rate, and the fact that the rationale asserts '3.4 times sales, cheap for that profile' without engaging the packet's own tension — a 55 trailing P/E against FY-1 net income of $28M, the reconciliation the sanitizer flagged as a modelling nuance for the trader. That flagged nuance was carried forward as a falsifier premise but never addressed in the rationale. Minor gaps, not unsupported reasoning. No grading of investment correctness is offered or implied.",
    "violations": [
      "Uncited tax/regulatory rate: expected_impact.tax_estimate asserts 'Israeli CGT 25% on realization' with no domain_knowledge/... citation and no packet authorization for the rate.",
      "Metric-label imprecision: rationale_summary states the stock 'trades near 3.4 times sales'; 3.4 is the packet's ev_s_trailing, whereas ps_trailing is 3.9 — an EV-based multiple described as a price-based one.",
      "Unaddressed packet tension: the sanitizer explicitly flagged that pe_trailing 55 implies TTM net income ~$51M against FY-1 $28M plus Q0 $14M; the rationale relies on the P/E in a falsifier but never reconciles or acknowledges the nuance."
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
    "rationale": "Case nlf_synthetic (category=synthetic, grading rule=synthetic_winner). expected_classes=['buy']; fleet_result.action='buy' — a direct in-class match, so the rule's full-credit condition applies: score=1.0. The partial-credit carve-outs (an F1 pass carrying falsifiers, or a small trap buy that survives the killing falsifier) are not triggered here — this is a synthetic winner, not a trap case — so no 0.5 reduction is warranted. Sizing is confirmatory rather than dispositive: $15,000 on the benchmark's mandated $1,000,000 fictional NAV = 1.5%, inside the packet's ~1-2% new-position budget, expressed as a true USD notional with size_units='currency'. Return computation: answer_key.resolution is null and positioned=false, so no forward price path exists; acted_return_pct and benchmark_return_pct are therefore null rather than zero — the absence of a resolution is a missing fact, not a flat outcome. Per the standing rule, outcome luck cannot move a class grade, so the null resolution does not disturb the 1.0. The stage-4 calibration_reviewer's judgment is adopted unchanged and not reopened: output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with no failures. The three logged violations (uncited Israeli CGT 25% rate lacking a domain_knowledge/... citation; 'trades near 3.4 times sales' labelling the packet's ev_s_trailing=3.4 as a price-to-sales multiple when ps_trailing=3.9; and the unreconciled pe_trailing=55 vs FY-1 net income $28M + Q0 $14M tension the sanitizer flagged) are citation-hygiene and precision defects inside a correct verdict. Under the recorded rule they are reportable quality findings, not class-grade modifiers, and the rule specifies no deduction pathway for them; they are surfaced for the audit agent rather than priced into the score. Confidence HIGH: the grade rests on a verbatim action/expected-class comparison plus an explicit recorded grading rule, with a complete stage-4 review and clean verification — no stale or inferred inputs. Recommend the domain-refresh agent supply the authorizing domain_knowledge path for the 25% Israeli CGT rate so future runs cite rather than assert it.",
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
- notes: Case nlf_synthetic (category=synthetic, grading rule=synthetic_winner). expected_classes=['buy']; fleet_result.action='buy' — a direct in-class match, so the rule's full-credit condition applies: score=1.0. The partial-credit carve-outs (an F1 pass carrying falsifiers, or a small trap buy that survives the killing falsifier) are not triggered here — this is a synthetic winner, not a trap case — so no 0.5 reduction is warranted. Sizing is confirmatory rather than dispositive: $15,000 on the benchmark's mandated $1,000,000 fictional NAV = 1.5%, inside the packet's ~1-2% new-position budget, expressed as a true USD notional with size_units='currency'. Return computation: answer_key.resolution is null and positioned=false, so no forward price path exists; acted_return_pct and benchmark_return_pct are therefore null rather than zero — the absence of a resolution is a missing fact, not a flat outcome. Per the standing rule, outcome luck cannot move a class grade, so the null resolution does not disturb the 1.0. The stage-4 calibration_reviewer's judgment is adopted unchanged and not reopened: output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with no failures. The three logged violations (uncited Israeli CGT 25% rate lacking a domain_knowledge/... citation; 'trades near 3.4 times sales' labelling the packet's ev_s_trailing=3.4 as a price-to-sales multiple when ps_trailing=3.9; and the unreconciled pe_trailing=55 vs FY-1 net income $28M + Q0 $14M tension the sanitizer flagged) are citation-hygiene and precision defects inside a correct verdict. Under the recorded rule they are reportable quality findings, not class-grade modifiers, and the rule specifies no deduction pathway for them; they are surfaced for the audit agent rather than priced into the score. Confidence HIGH: the grade rests on a verbatim action/expected-class comparison plus an explicit recorded grading rule, with a complete stage-4 review and clean verification — no stale or inferred inputs. Recommend the domain-refresh agent supply the authorizing domain_knowledge path for the 25% Israeli CGT rate so future runs cite rather than assert it.

### omk_synthetic — real: SYNTHETIC trap-shape (no real company) — meal-kit / D2C growth theater
- **Fleet reasoning:** hold (HIGH) — Pass on OMK — do not initiate a position. The core problem: after paying to acquire a customer, each sale still loses money (contribution profit after marketing was negative $18 million last quarter), while growth has collapsed from about 63% to under 5% and gross margin keeps sliding. Cash funds under two years at this burn, so the story likely needs outside money before it needs a rebrand. At roughly 2.9 times sales we would pay a growth price for a business that is neither growing nor profitable. Revisit only if unit economics actually turn positive.
  - falsifiers: REPAIR-FALSIFIER: Contribution profit after marketing (source: fundamentals/OMK quarterly disclosure; as-of 'Q0 just reported', baseline -$18M on $310M revenue, and -$55M for FY-0) turns POSITIVE for two consecutive quarters without slashing marketing so hard that revenue falls year-over-year -> HOLD(do-not-initiate)->ADD (0.5-1% accepted-total-loss starter, $5,000-$10,000 on the $1,000,000 benchmark NAV).; REPAIR-FALSIFIER: Gross margin (source: fundamentals/OMK income statement; as-of Q0, baseline 27.5%, down from 34% two fiscal years ago and 28.2% for FY-0) recovers above 32% for two consecutive quarters, evidencing that discounting and shipping cost pressure is structurally reversing rather than lapping easy comparisons -> HOLD->ADD.; REPAIR-FALSIFIER: Revenue growth (source: fundamentals/OMK; as-of Q0, baseline +4.5% year-over-year, FY-0 +6.8%) re-accelerates above +15% year-over-year for two consecutive quarters with marketing spend flat or lower — i.e. growth that is earned by retention/attach, not bought on paid social -> HOLD->ADD.; REPAIR-FALSIFIER: The funding cliff is removed on the operating side — cash burn (source: fundamentals/OMK cash-flow statement; as-of latest, $480M cash against $620M total debt, FY-1 free cash flow -$290M, company note says cash funds <2 years of burn) narrows to under $100M annualized for two consecutive quarters, so survival no longer depends on a dilutive raise -> HOLD->ADD. STRUCTURAL caveat: a large discounted equity raise that merely buys time WITHOUT the above unit-economic repair does NOT satisfy this falsifier and would confirm the pass.
  - clock: Next quarterly earnings print, estimated 2-3 months out (packet supplies no absolute date, so this stays relative). The item to check is contribution profit after marketing and gross margin — not the revenue headline. Honestly unestimable for the bull case. Credible repair would need 2-4 consecutive quarters of positive contribution profit after marketing, and the sub-two-year cash runway may force a dilutive raise first. That unestimability is itself part of why conviction to own is low, not merely 'wait for a better price'.
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: []
  - independent replay review: clean=True; packet_fidelity=True; workflow_correct=True; visible-reasoning grounding=4/4
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
        "evidence": "Alias 'OMK' applied consistently: candidate_payload.alias='OMK', analyst_reports[].report_name='fundamentals/OMK' and 'news/OMK', positions='No existing position in OMK'. The sole proper noun is fundamentals.company='Orbit Meal Kits (alias OMK)', which is explicitly self-labelled as the alias carrier; constraints_rendered states 'The company, alias, events, and absolute dollar figures were invented for this test; there is no real company', so the name cannot resolve to a real issuer. forbidden_terms=[] so there is no denylist to match against, and none of its contents were introduced as facts. No ticker, exchange, domicile, founder name, or listing venue appears anywhere in the payload.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "synthetic=true, rescale_factor=null, raw_sources=[] and source_manifest=[] — per protocol the figures have no real-world originals, so no rescaling factor exists to prove or disprove and no independent manifest could be constructed. Substituted internal-consistency verification against the masked payload only: revenue 890->1180 = +32.58% vs stated revenue_growth_pct 32.6; 1180->1260 = +6.78% vs stated 6.8; P/S = 3600/1260 = 2.86 vs ps_trailing 2.9; EV/S = (3600+620-480)/1260 = 2.97 vs ev_s_trailing 3.0; pe_trailing=null is correct given net_income_usd_m negative in all three years; gross_margin_pct 34.0->28.2 = 580bps vs narrative '~600 bps'; cash_and_equivalents 480 against FY-1 free_cash_flow -290 gives ~1.7 years, consistent with 'cash funds <2 years of current burn'; latest_quarter revenue 310 at +4.5% yoy and GM 27.5 extends the FY-0 deceleration (6.8%, 28.2) monotonically; market_cap 3600 / share_price 22.7 implies ~158.6M shares, internally plausible. Sole discrepancy is narrative rounding: recent_developments says growth 'collapsed from +63% to +7%' while annual_income_statement shows 62 and 6.8 — cosmetic, non-blocking, no protocol violation.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "Every temporal reference is a relative window with no calendar anchor: fy labels 'FY-2', 'FY-1', 'FY-0 (just ended)'; latest_quarter.which='Q0 just reported'; as_of='current'; business_description 'Rebranded last year'; valuation.price_action '+40% YTD' and '-65% from listing-era high'; constraints 'Long-horizon investor (5+ year intended hold)' and horizon band example '3-6 months'. No year, month, quarter-with-year, or ISO date appears. The packet also carries an explicit TEMPORAL MASK in constraints_rendered instructing that the system current date/year must never be used or inferred, which is consistent with the masked content.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "news.macro_context reads 'Consumer subscription fatigue; paid-social CAC inflation; growth-stock de-rating still in force' — all unnamed, undated structural conditions. recent_developments adds only 'discounting and shipping inflation' and generic CAC-treadmill framing. No named crisis, pandemic, war, central bank, rate-hike cycle, regulator, election, or dated macro regime is referenced anywhere in the payload, so no macro detail narrows the time window or identifies the period.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "SAFE TO RUN. Synthetic control packet (case_id omk_synthetic, synthetic=true); forbidden_terms, raw_sources, and source_manifest are all empty and rescale_factor is null. No concrete identity, calendar, or named-macro-event leak found in the candidate payload. ALIAS: \"OMK\" is used consistently across report_name fields (fundamentals/OMK, news/OMK), positions, and constraints; the only proper noun, \"Orbit Meal Kits (alias OMK)\", is self-labelled as the alias-bearing fictional name and the packet's provenance note declares the company invented, so it cannot resolve to a real issuer — pass. ABSOLUTE-FIGURE RESCALING: not_applicable by construction — synthetic=true means there is no real original figure set or recoverable factor, and the empty source_manifest/raw_sources means no independent proof could exist; per instruction I substituted an internal-consistency audit, which the figures pass (32.6%/6.8% growth, 2.9 P/S, 3.0 EV/S, null P/E under losses, 580bps GM compression, ~1.7yr cash runway all reconcile to within rounding). One non-blocking narrative drift: news text says growth fell \"from +63% to +7%\" while the income statement shows 62% and 6.8% — rounding-level, no protocol impact. RELATIVE DATES: all time references are relative windows (FY-2/FY-1/FY-0 just ended, Q0, as_of \"current\", \"last year\", YTD, \"listing-era high\", \"5+ year\", \"3-6 months\"); zero calendar years or absolute dates, and the packet carries an explicit TEMPORAL MASK forbidding use of the system date. MACRO GENERICIZATION: macro_context cites only unnamed, undateable conditions (subscription fatigue, paid-social CAC inflation, growth-stock de-rating, shipping inflation) — no named crisis, policy action, central bank, or dated regime event. leaked_terms is empty. Confidence MEDIUM: every check is fully verifiable from the masked payload's own arithmetic, but the synthetic designation and provenance note are packet-self-reported with no external manifest to corroborate them; recommend the domain-refresh agent confirm packet provenance if this control is reused outside the benchmark."
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
      "rationale '-$18 million last quarter' -> packet.analyst_reports[fundamentals].latest_quarter.contribution_profit_after_marketing_usd_m = -18",
      "falsifier baseline '-$55M for FY-0' -> packet.analyst_reports[fundamentals].annual_income_statement[FY-0].contribution_profit_after_marketing_usd_m = -55",
      "rationale 'growth has collapsed from about 63% to under 5%' -> news.recent_developments '+63% to +7%' and latest_quarter.revenue_growth_yoy_pct = 4.5 (statement FY-2 shows 62; rounding drift already logged by sanitizer as non-blocking)",
      "falsifier GM baselines '27.5%, down from 34% two fiscal years ago and 28.2% for FY-0' -> latest_quarter.gross_margin_pct=27.5, FY-2=34, FY-0=28.2",
      "rationale 'Cash funds under two years at this burn' -> balance_sheet.note 'cash funds <2 years of current burn', cash_and_equivalents_usd_m=480, total_debt_usd_m=620, FY-1 free_cash_flow_usd_m=-290",
      "rationale 'roughly 2.9 times sales' -> valuation.ps_trailing = 2.9",
      "falsifier sizing '$5,000-$10,000 on the $1,000,000 benchmark NAV' -> constraints_rendered BENCHMARK SIZING ANCHOR (1% = $10,000)",
      "revisit trigger price 11.35 -> derived as half of fundamentals.share_price_usd = 22.7, yielding 1.45x trailing sales vs the stated '~1.5x'",
      "next_validation_point kept relative ('2-3 months out, packet supplies no absolute date') -> constraints_rendered TEMPORAL MASK; output_audit.calendar_years_in_output = []",
      "cited_sources ['fundamentals/OMK','news/OMK'] appear verbatim as analyst_reports[].report_name",
      "workflow order -> sanitizer.stage = 2 with verification.ok = true and output.safe_to_run = true preceding the decision output",
      "rerating_horizon declares unestimability explicitly and ties it to lowered conviction -> constraints_rendered 'if it is genuinely unestimable, say so explicitly — that itself lowers conviction'"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Clean replay. (1) OUTPUT CLEAN: response_raw is exactly one JSON object with no prose, headings, or code fences; output_audit reports contaminated=false, calendar_years_in_output=[], contamination_hits=[], suspect_terms=[]. (2) PACKET FIDELITY: every numeric claim in the decision resolves to a packet field with no invention — contribution profit after marketing -$18M (latest_quarter) and -$55M (FY-0); Q0 revenue $310M at +4.5% yoy; gross margin 27.5% Q0 vs 28.2% FY-0 vs 34% FY-2; cash $480M vs total debt $620M with FY-1 FCF -$290M and the packet's own '<2 years of current burn' note; 2.9x trailing sales from valuation.ps_trailing. Growth framing 'about 63% to under 5%' blends the news narrative (+63%) with the statement (62) and the Q0 4.5% print — the same cosmetic rounding drift the sanitizer already flagged as non-blocking. Sizing honors the benchmark anchor exactly: 0.5-1% rendered as $5,000-$10,000 on $1,000,000 NAV, as real USD notional, never a percentage in a currency field. Revisit trigger $11.35 is precisely half of share_price_usd 22.7 (implying 1.45x trailing sales, labelled '~1.5x' — rounding only, derivation transparent). cited_sources ['fundamentals/OMK','news/OMK'] match report_name values verbatim; no fabricated ids. (3) WORKFLOW: sanitizer stage 2 ran first with verification.ok=true, safe_to_run=true, leaked_terms=[], and the decision stage followed. All three reporting requirements are satisfied — FALSIFIERS are specific, checkable, each carries a source, an as-of, a numeric baseline and an explicit decision transition, plus a STRUCTURAL caveat that a dilutive raise without unit-economic repair does NOT satisfy the funding falsifier; THE CLOCK names the next quarterly print as the validation event and correctly keeps it relative ('2-3 months out, packet supplies no absolute date'); the re-rating horizon is declared genuinely unestimable EXPLICITLY and tied to lowered conviction, which is the constraint's required handling rather than a silent omission. Temporal mask fully respected: zero calendar years or dates. Instrument 'stock', zero size, no leverage or options — compliant. (4) REASONING GROUNDED (visible rationale only; no access to hidden reasoning is claimed or implied): score 4. Each step of the written rationale is traceable to a packet fact — negative contribution after CAC, growth collapse, margin slide, sub-two-year runway implying a probable raise, and a growth multiple paid for a non-growing, non-profitable business. The price trigger is explicitly labelled 're-check only (NOT a thesis confirmation)' and gated behind a repair-falsifier, which prevents price from masquerading as thesis evidence. MINOR NON-BLOCKING OBSERVATIONS: output confidence is HIGH while the upstream sanitizer self-reported MEDIUM (defensible, since both analyst reports are marked HIGH and as_of='current', but the chain's weakest link is MEDIUM); '~1.5x' vs computed 1.45x; order_type='market' and time_in_force='GTC' are populated on a zero-size non-trade, which is inert but slightly incongruent. None of these rise to a violation. Investment correctness was not graded, per scope. Confidence HIGH: the full packet, constraints, sanitizer stage and raw response were supplied, so every check above is deterministic and re-derivable from the inputs.",
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
    "rationale": "Synthetic trap case (case_id=omk_synthetic, grading=synthetic_trap, positioned=false) [grading_input.case_id, grading_input.grading, grading_input.positioned]. The answer key lists expected_classes=[\"hold\"] [grading_input.answer_key.expected_classes] and the fleet returned action=\"hold\" with size=0.0 shares [grading_input.fleet_result.action, grading_input.fleet_result.size], so the verdict lands squarely in the expected class. The trap was declined outright — no trap buy, not even a small one — so the partial-credit branch of the benchmark rule (0.5 for an F1 pass with falsifiers or a small trap buy carrying the killing falsifier) does not apply; full in-class credit of 1.0 is the correct award. The independent replay review is accepted as-is and not reopened: output_clean=true, packet_fidelity=true, workflow_correct=true, violations=[] , reasoning_grounded_score=4 [grading_input.review.output]. Acted-on return is not computable and is reported as null: answer_key.resolution is null [grading_input.answer_key.resolution], and independently the acted position size is 0.0 [grading_input.fleet_result.size], so no acted or benchmark return exists to compute; per the grading rule, outcome luck would not alter the class grade in any case. Grader confidence HIGH: the class comparison is deterministic from the supplied answer key and fleet action, both present verbatim in the grading input; the only missing datum (resolution) affects return reporting only, not the score, and no external or market data was required for this determination.",
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
- notes: Synthetic trap case (case_id=omk_synthetic, grading=synthetic_trap, positioned=false) [grading_input.case_id, grading_input.grading, grading_input.positioned]. The answer key lists expected_classes=["hold"] [grading_input.answer_key.expected_classes] and the fleet returned action="hold" with size=0.0 shares [grading_input.fleet_result.action, grading_input.fleet_result.size], so the verdict lands squarely in the expected class. The trap was declined outright — no trap buy, not even a small one — so the partial-credit branch of the benchmark rule (0.5 for an F1 pass with falsifiers or a small trap buy carrying the killing falsifier) does not apply; full in-class credit of 1.0 is the correct award. The independent replay review is accepted as-is and not reopened: output_clean=true, packet_fidelity=true, workflow_correct=true, violations=[] , reasoning_grounded_score=4 [grading_input.review.output]. Acted-on return is not computable and is reported as null: answer_key.resolution is null [grading_input.answer_key.resolution], and independently the acted position size is 0.0 [grading_input.fleet_result.size], so no acted or benchmark return exists to compute; per the grading rule, outcome luck would not alter the class grade in any case. Grader confidence HIGH: the class comparison is deterministic from the supplied answer key and fleet action, both present verbatim in the grading input; the only missing datum (resolution) affects return reporting only, not the score, and no external or market data was required for this determination.
