# Fleet-calibration report — 2026-08-31-post-fix.json

| Point | Category | Fleet verdict (conv, size) | In class? | Score | Acted vs benchmark |
|---|---|---|---|---|---|
| amd_2016_f1 | A/F1_lenient | buy (MEDIUM, 5000.0 currency) | Y | 1.0 | +5357% vs +5357% |
| boot_2017 | A/entry | buy (MEDIUM, 10000.0 currency) | Y | 1.0 | +1855% vs +1855% |
| cvna_2023_reentry | A/entry | buy (MEDIUM, 10000.0 currency) | Y | 1.0 | +649% vs +649% |
| hyln_2021 | B/trap | hold (MEDIUM, 0.0 shares) | Y | 1.0 | +0% vs -86% |
| ttcf_t2 | C/exit | sell (HIGH, 133.0 shares) | Y | 1.0 | +0% vs -100% |
| nlf_synthetic | synthetic/synthetic_winner | buy (MEDIUM, 20000.0 currency) | Y | 1.0 | n/a (synthetic) |
| omk_synthetic | synthetic/synthetic_trap | hold (MEDIUM, 0.0 shares) | Y | 1.0 | n/a (synthetic) |

## Category subtotals
- **A**: 3.0/3 points; acting on fleet +2620% vs benchmark +2620% (n=3)
- **B**: 1.0/1 points; acting on fleet +0% vs benchmark -86% (n=1)
- **C**: 1.0/1 points; acting on fleet +0% vs benchmark -100% (n=1)
- **synthetic**: 2.0/2 points
- **TOTAL**: 7.0/7; acting on the fleet at every frozen point: +1572% avg vs +1535% benchmark (n=5)

## Conviction calibration
- HIGH: 1.0/1 (100%)
- MEDIUM: 6.0/6 (100%)

## Disqualified / not scored
### sdc_2021
- agent_pipeline — review failed: output_clean
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
        "evidence": "Full-text scan of candidate_payload finds no verbatim or near-verbatim match to any forbidden_terms entry. Identity-adjacent details are abstracted rather than named: storefront concept rendered as 'retail kiosks', the named chairside competitor as 'the chairside incumbent', the controlling individual as 'Founder-family controls the vote'. Alias strings used are 'CBO' / 'ClearBite Oral', which collide with no denylist term. Business description stays at category level ('Direct-to-consumer clear dental aligners sold via tele-dentistry').",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven from source_manifest only. Manifest[0] (SEC EDGAR filing, publication_date 2021-11-08): latest-quarter revenue $138M, net loss -$89M, marketing and selling $96M. At declared rescale_factor 1.4: 138x1.4=193.2 vs payload 193; 89x1.4=124.6 vs payload -125; 96x1.4=134.4 vs payload 134 — all within rounding. No unscaled manifest figure appears in the payload (138, 511, 630-650, 120-140 absent). Ratios correctly unscaled and consistent: -18.3% YoY matches manifest[0]; 134/193=69.4% supports the stated ~70% marketing intensity; EV/S=(2240+1050-308)/919=3.24 vs stated 3.2; P/S=2240/919=2.44 vs stated 2.5; 1051->919=-12.6% vs stated -12.5%. Caveat: FY-2/FY-1 income-statement rows, cash 308, debt 1050, market cap 2240 and price 5.67 have no manifest counterpart, so their factor is inferred from internal consistency rather than proven against a primary line.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "Payload contains no absolute date, calendar year, or fiscal-year label. Time is expressed only relatively: as_of 'current', latest_quarter.which 'Q3, reported ~1 week ago', annual rows keyed 'FY-2'/'FY-1', and price action anchored to 'IPO-era peak'. The manifest publication dates (2021-11-08) and the FY2021 labels do not appear anywhere in candidate_payload, including constraints_rendered and positions.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Consumer discretionary soft for lower-income cohorts; digital advertising attribution disrupted industry-wide', and recent_developments repeats 'digital-ad attribution changes' — the specific vendor privacy/attribution program and any named pandemic-era macro event are abstracted with no platform, vendor, regulator, product, or program name. Level of genericity matches the manifest's own 'digital-ad attribution breakage' phrasing without supplying a dateable or issuer-identifying hook.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Independent sanitizer audit of case_id sdc_2021 candidate payload (synthetic=false, declared rescale_factor 1.4). VERDICT: safe_to_run = true; no concrete leaks found in the payload the Trader would see.\n\n(1) ALIAS — PASS. Full-text scan of candidate_payload (alias, company, positions, constraints_rendered, both analyst_reports) returns zero verbatim or near-verbatim matches against the forbidden_terms denylist. Identity-adjacent facts are correctly abstracted: retail storefront concept → \"retail kiosks\"; named chairside competitor → \"the chairside incumbent\"; controlling individual → \"Founder-family controls the vote\". The alias \"CBO\"/\"ClearBite Oral\" collides with no denylist string. Note: the denylist was treated strictly as a denylist; no term from it was used as a fact.\n\n(2) ABSOLUTE_FIGURE_RESCALING — PASS, proven independently from source_manifest arithmetic only (no reliance on any upstream classifier's asserted facts). Manifest entry [0] (SEC EDGAR filing, publication_date 2021-11-08) states latest-quarter revenue $138M, net loss -$89M, marketing and selling $96M. Applying the declared factor 1.4: 138×1.4 = 193.2 → payload 193; 89×1.4 = 124.6 → payload 125; 96×1.4 = 134.4 → payload 134. All three land within rounding of the payload's latest_quarter block, and no manifest figure survives unscaled anywhere in the payload (138, 511, 630–650, 120–140 from manifest entries [0]–[2] do not appear). Ratios are correctly left unscaled and remain internally consistent under scaling: revenue_growth_yoy_pct -18.3 matches manifest [0]; marketing/revenue 134/193 = 69.4% ≈ the stated \"≈70%\"; EV/S = (2240 + 1050 − 308)/919 = 3.24 vs stated 3.2; P/S = 2240/919 = 2.44 vs stated 2.5; implied share count 2240/5.67 = 395M is scale-invariant as expected. Coverage caveat: the annual_income_statement rows (FY-2, FY-1) and the balance_sheet/market_cap/share_price items have no counterpart in the three-entry manifest, so their scaling factor cannot be confirmed against a primary line; they are nonetheless internally consistent (1051→919 = -12.6% vs stated -12.5%). Partial-coverage note recorded rather than a separate unverifiable verdict, since every manifest-covered figure proves cleanly at 1.4.\n\n(3) RELATIVE_DATES — PASS. The payload carries no absolute date anywhere: periods are expressed as \"as_of: current\", \"which: Q3, reported ~1 week ago\", \"FY-1\"/\"FY-2\", and \"stock -80%+ from IPO-era peak\". The manifest's 2021-11-08 publication dates and the FY-year labels do not appear in the payload. constraints_rendered contains no dated anchors.\n\n(4) MACRO_EVENT_GENERICIZATION — PASS. The two macro drivers are stated as \"Consumer discretionary soft for lower-income cohorts\" and \"digital advertising attribution disrupted industry-wide\" / \"digital-ad attribution changes\" — the vendor-specific privacy/attribution change and any named pandemic-era event are both abstracted away, with no platform, vendor, regulator, or program name given. This matches the manifest's own wording (\"digital-ad attribution breakage\") at a genericity level that does not date or identify the issuer.\n\nOPERATIONAL FLAG (outside the four checks, action required by the caller): the raw_sources and source_manifest blocks in this sanitizer input are NOT masked — the issuer identity is recoverable from the filer CIK, the URL slugs, and the newswire headline text. These blocks are sanitizer-side proof material only and MUST NOT be forwarded to, or quoted into, the Trader context; only candidate_payload is clear to pass through. If the downstream harness attaches source_manifest to the Trader prompt by default, that path leaks the identity even though the payload itself is clean.\n\nNo fabricated facts were introduced. Confidence HIGH: all four verdicts rest on direct textual inspection of the supplied payload plus arithmetic against the supplied manifest; the only residual uncertainty is the unproven-but-consistent scaling of the annual and balance-sheet figures, which does not change the pass/fail outcome. No domain-refresh escalation needed."
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
      "packet fundamentals/CBO latest_quarter.revenue_growth_yoy_pct = -18.3 -> rationale 'revenue fell 18% year-over-year last quarter'",
      "packet marketing_and_selling_usd_m 134 / revenue_usd_m 193 = 69.4% -> rationale 'marketing eats about 70% of every sales dollar'; packet note itself says 'marketing spend ≈ 70% of quarterly revenue'",
      "packet gaap_net_income_usd_m -125, balance_sheet cash 308, total_debt 1050 -> rationale 'lost $125M in the quarter against $308M of cash and $1.05B of debt'",
      "constraints BENCHMARK SIZING ANCHOR '1% = $10,000' -> falsifier 'a 0.5-1% / $5,000-$10,000 accepted-total-loss starter' (USD notional, not a percentage in a currency field)",
      "packet report_name fields 'fundamentals/CBO' and 'news/CBO' -> cited_sources matches verbatim; no invented source ids",
      "packet latest_quarter.which 'Q3, reported ~1 week ago' -> next_validation_point 'Q3 was reported roughly one week ago, so the next print is an estimated 2-3 months out' (grounded) BUT continues 'approximately late Nov–Dec 2026' (ungrounded)",
      "sanitizer relative_dates evidence: 'Payload contains no absolute date, calendar year, or fiscal-year label' — establishes the 2026 anchor could not have come from the packet",
      "constraints REPORTING REQUIREMENT 'if it is genuinely unestimable, say so explicitly' -> rerating_horizon 'A genuine re-rating horizon is NOT estimable with the data available'",
      "packet positions 'No existing position in CBO' -> action hold with size_shares_or_currency 0 and cash_delta '$0 — no trade placed.'",
      "sanitizer safe_to_run=true, verification.ok=true, stage 2 precedes trader run -> workflow ordering intact"
    ],
    "output_clean": false,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "REPLAY AUDIT — case sdc_2021 (freeze_date 2021-11-15). Verdict: one cleanliness failure, otherwise faithful and well-formed. I graded only form, fidelity, workflow and visible-rationale grounding; no view is expressed on whether 'hold' was the right call.\\n\\n(1) OUTPUT CLEANLINESS — FAIL. The packet is deliberately date-free (sanitizer relative_dates check: \\\"Payload contains no absolute date, calendar year, or fiscal-year label\\\"; time is given only as \\\"Q3, reported ~1 week ago\\\", \\\"FY-1\\\"/\\\"FY-2\\\", \\\"as_of: current\\\"). The trader nonetheless emitted an absolute calendar anchor in next_validation_point: \\\"approximately late Nov–Dec 2026\\\". That year is (a) not derivable from any packet field and (b) inconsistent with the 2021-11-15 freeze — it is an out-of-band wall-clock anachronism injected into a historical replay. The persisted output_audit records calendar_years_in_output=[\\\"2026\\\"] yet sets contaminated=false; on my read the automated flag is under-called, since the year both fabricates a fact and breaks the replay's temporal isolation. The relative portion of the same field (\\\"an estimated 2-3 months out\\\") is legitimate and packet-derived; only the parenthetical calendar anchor is defective. No other leakage: the unmasked sources block (SEC/newswire URLs, real issuer name) is nowhere quoted or alluded to in the output, and no de-masking attempt appears.\\n\\n(2) PACKET FIDELITY — PASS. Every numeric assertion resolves exactly to a packet field: revenue -18.3% YoY (rendered \\\"fell 18%\\\"), marketing $134M on $193M = 69.4% (rendered \\\"about 70%\\\", and the packet's own note says \\\"marketing spend ≈ 70% of quarterly revenue\\\"), GAAP quarterly loss -$125M, cash $308M, total debt $1,050M (rendered \\\"$1.05B\\\"). Constraint compliance is exact: the $1,000,000 NAV anchor is applied correctly (\\\"0.5-1% / $5,000-$10,000\\\" — actual USD notional, not a percentage in a currency field); instrument=stock with no leverage or options; the reporting requirement is satisfied with explicit FALSIFIERS and an explicit CLOCK. cited_sources uses the two report_name strings verbatim from the packet (\\\"fundamentals/CBO\\\", \\\"news/CBO\\\"); no source id is invented. size_shares_or_currency=0 is consistent with a no-trade decision.\\n\\n(3) WORKFLOW — PASS. Stage-2 sanitizer ran first, returned safe_to_run=true with four pass verdicts and verification.ok=true; the trader then ran on the masked payload only. response_raw is a single JSON object with no prose, headings, or fences, and output_full is a faithful parse of it. Falsifiers carry metric, source, as-of baseline and a decision transition; one is armed as a machine-checkable revisit_trigger (cash < $150M, HOLD->SELL) while the compound/qualitative ones are correctly declared unarmed. The rerating horizon honours the 'unestimable must be stated' clause rather than inventing a band. Minor semantic wrinkle, not a failure: a HOLD->SELL transition is odd for a candidate with no position (packet: \\\"No existing position in CBO\\\") — the falsifier text repairs this by defining it as permanent disqualification, but the transition label is loose. Also worth surfacing for the harness owner: the sanitizer's OPERATIONAL FLAG warns that raw_sources/source_manifest must never reach the trader context; this replay record cannot prove what the trader was actually handed, only that nothing from it surfaced in the output.\\n\\n(4) VISIBLE-RATIONALE GROUNDING — 3/4. I assessed only the written rationale; no claim is made about hidden reasoning. Each step of rationale_summary maps to a packet fact: shrinking sales -> -18.3% YoY; broken CAC -> 134/193 marketing intensity plus the news report's \\\"CAC/LTV math visibly broken\\\"; balance-sheet burn -> -$125M against $308M cash and $1,050M debt, echoing the packet's \\\"funds losses rather than a moat\\\". Falsifier baselines are individually sourced and arithmetically correct. Deductions: the \\\"late Nov–Dec 2026\\\" anchor is an ungrounded inserted fact, and the assertion that \\\"the demand decline is accelerating\\\" is a reasonable but not fully demonstrated reading (FY-1 -12.5% vs latest quarter -18.3% supports it directionally, yet the packet's own YTD framing is not addressed). Not full marks; otherwise step-by-step grounded.\\n\\nCONFIDENCE: HIGH — the complete packet, constraints, sanitizer record and raw trader response are all present, so every finding above rests on direct textual comparison rather than inference. No domain-refresh escalation needed.",
    "violations": [
      "ANACHRONISM / OUTPUT CLEANLINESS: next_validation_point states 'approximately late Nov–Dec 2026' — an absolute calendar year absent from the packet and inconsistent with freeze_date 2021-11-15; the packet is entirely date-free by design per the sanitizer's relative_dates pass.",
      "FABRICATED DETAIL: the same 2026 calendar anchor is an unsourced fact appended to an otherwise packet-derived relative estimate ('an estimated 2-3 months out'), which was sufficient on its own.",
      "AUDIT UNDER-CALL: persisted output_audit records calendar_years_in_output=['2026'] but sets contaminated=false with empty contamination_hits; the year should have been flagged as a hit given the 2021 freeze.",
      "MINOR / SEMANTIC: revisit_trigger decision_transition 'HOLD->SELL' is incoherent for a candidate with zero position ('No existing position in CBO'); the intended meaning (permanent disqualification) is only recoverable from the falsifier prose."
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
    "rationale": "Case sdc_2021 (category B, grading rule = \"trap\"). expected_classes = [\"hold\"]; fleet_result.action = \"hold\" with size 0.0 shares and positioned=false, so the verdict lands squarely in the expected class and the trap was fully avoided — full credit 1.0 per the recorded rule (grading input answer_key.expected_classes and fleet_result). The partial-credit carve-outs in the benchmark rule do not apply: there was no F1-pass-with-falsifiers situation and no small trap buy, because size_shares_or_currency = 0 means no capital was committed at all (fleet_result.size = 0.0, size_units = \"shares\"). Acted-on return: with zero position established, exposure to the resolution path is nil, so acted_return_pct = 0.0% versus the benchmark's ride-to-zero benchmark_return_pct = -100% (answer_key.resolution: price_at_freeze_real $4.05 on 2021-11-15 vs price_at_horizon_real $0.00, horizon_label \"ride-to-zero (Ch.11 2023-09)\", Yahoo SDCCQ close fetched 2026-07-11). The fleet therefore avoided a 100-point drawdown, but per the grading protocol outcome luck does not modify the class grade — the score reflects the in-class verdict, not the favourable terminal outcome. The stage-4 calibration_reviewer (claude-opus-5) returned output_clean=false on a single anachronism (the ungrounded \"late Nov–Dec 2026\" calendar anchor in next_validation_point, inconsistent with freeze_date 2021-11-15), plus packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, confidence HIGH. Per instruction I do not reopen that cleanliness/workflow judgment; it is recorded for the audit trail but is a separate axis from the class grade and does not reduce the 1.0. The rationale's underwriting was substantively grounded in the packet — revenue -18.3% YoY, marketing $134M on $193M revenue (69.4%), GAAP quarterly loss -$125M against $308M cash and $1,050M total debt (per the reviewer's grounding_evidence, all resolved verbatim to packet fields fundamentals/CBO and news/CBO) — i.e. the pass was reached for the right reasons (broken customer economics and a balance sheet funding losses), not by accident. Confidence HIGH: the resolution, action, size and grading rule are all supplied as primary inputs to this grading step, so no stale or inferred data underpins the class determination.",
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


## Horizon calibration (§2b clock band)
- not_applicable: 3
- no_band: 4
- amd_2016_f1: not_applicable (stated=12-18 mo (12-18 months); actual_months=None)
- boot_2017: no_band (stated=none; actual_months=None)
- cvna_2023_reentry: no_band (stated=none; actual_months=None)
- hyln_2021: no_band (stated=none; actual_months=None)
- ttcf_t2: no_band (stated=none; actual_months=None)
- nlf_synthetic: not_applicable (stated=none; actual_months=None)
- omk_synthetic: not_applicable (stated=none; actual_months=None)

## Per-point detail (section 2b three-column + clock)
### amd_2016_f1 — real: AMD @ 2016-02-29 ($2.14 adj); F1 per spec appendix (post FY2015 10-K, before T_parabola / THATIC)
- **Fleet reasoning:** buy (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026', '2028']
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Scanned every string field of candidate_payload (alias, positions, constraints_rendered, both analyst_reports incl. business_description, recent_developments, macro_context, notes) against all 12 forbidden_terms; zero verbatim or case-variant matches. Masked entity is referred to only as 'Corvid Microsystems (alias CVM)'. Real-world proper nouns are replaced by generic descriptors: 'the dominant CPU incumbent', 'BOTH major game consoles', 'a respected chip-engineer CEO', 'a CLEAN-SHEET CPU core', 'semi-custom silicon'. No fab partner, no product brand, no executive name appears. Residual (not a leak, no action): the descriptive combination '#2 in BOTH CPU and discrete GPU' + 'dual-console semi-custom annuity' is narrowly identifying by description alone, but the protocol denylist is not breached and no denylisted token was added by me.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven independently from source_manifest/raw_sources only, at the stated rescale_factor 1.7. SEC 10-K manifest entry (https://www.sec.gov/Archives/edgar/data/2488/000000248816000047/amd-123115x10k.htm, publication_date 2016-02-18): revenue $3,991M x 1.7 = $6,784.7M vs payload 6785 (match); net loss -$660M x 1.7 = -$1,122M vs payload -1122 (exact); cash $785M x 1.7 = $1,334.5M vs payload 1334.5 (exact); total debt $2,260M x 1.7 = $3,842M vs payload 3842 (exact); equity deficit -$412M x 1.7 = -$700.4M vs payload -700 (match); interest expense $160M x 1.7 = $272M vs payload 272 (exact). Ratios correctly left unscaled: -27.5% YoY appears identically in manifest and payload. Internal consistency of non-manifest absolutes confirmed against the same factor: FY-2 growth 9360/9008 = +3.91% vs stated 3.9; ps_trailing 2898/6785 = 0.427 vs stated 0.43; ev_s (2898+3842-1334.5)/6785 = 0.797 vs stated 0.8. Limitation recorded: market_cap_usd_m 2898 and share_price_usd 6.1 have no manifest counterpart, so their pre-scale originals cannot be independently reconstructed; they are nonetheless arithmetically coherent with the six manifest-verified figures, so the check as a whole passes rather than being downgraded.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "candidate_payload contains no absolute calendar date, year, quarter label, or era marker anywhere. All temporal references are relative to an unspecified 'now': fiscal years labelled 'FY-3'/'FY-2'/'FY-1 (latest full year, just filed)', as_of 'current', 'FY-1 annual just filed (~2 weeks ago)', 'a respected chip-engineer CEO took over ~two years ago', '~12-18 months from launching', 'in the prior year', '~-50% over ~2 years', 'Revenue nearly halved over four years'. The real 2015-2016 publication_dates exist only in raw_sources/source_manifest, which are sanitizer-side inputs and are not part of the payload handed to the Trader.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "Named-actor and node-specific macro detail present in the manifest (https://www.anandtech.com/show/9447/intel-10nm-and-kaby-lake, publication_date 2015-07-16, describing a specific incumbent's node-cadence slip with explicit process-node numbers) is rendered in the payload only as 'The dominant CPU incumbent showed a process-node cadence slip in the prior year — the first visible crack in a decades-long lead': no company name, no nanometre figure, no dated announcement. Cycle backdrop is likewise generic — 'PC market shrinking mid-single-digits; server demand growing with cloud buildouts; semi cycle soft after an inventory correction' — with no year, index level, or named macro event. The clean-sheet-core roadmap from the analyst-day manifest entry (https://ir.amd.com/news-events/press-releases/detail/611/amd-outlines-sharpened-focus-at-2015-financial-analyst-day, publication_date 2015-05-06) is carried over with the +40% IPC claim but with the codename stripped.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "SAFE TO RUN. All four protocol dimensions pass. (1) Alias: no forbidden term appears verbatim or in variant form anywhere in the payload; every named entity is replaced by a generic role descriptor. Residual re-identification risk from the descriptive profile is noted for the benchmark owner but is not a concrete leak and I have not narrowed it further. (2) Absolute-figure rescaling: independently proven at factor 1.7 from source_manifest alone — six manifest-backed figures (revenue, net loss, cash, total debt, equity deficit, interest expense) reconstruct to the payload values exactly or within rounding, and ratios/growth rates are correctly left unscaled and cross-check internally (P/S 0.427 vs 0.43, EV/S 0.797 vs 0.8, FY-2 growth 3.91% vs 3.9). Market cap and share price have no manifest counterpart so their originals cannot be reconstructed; they are arithmetically coherent with the verified set, which is recorded as a limitation rather than a failure. (3) Relative dates: payload is free of any absolute date, year, or era marker; all time references are relative to an unspecified present, and the real publication dates live only in the sanitizer-side manifest. (4) Macro genericization: the incumbent's node-cadence slip and the semi-cycle backdrop are stated without company names, process-node numbers, or dated events. Forbidden_terms were treated strictly as a denylist and no term from it was introduced into any output field. No company identification and no outcome inference is offered. Confidence HIGH: every rescaling claim is arithmetic over the frozen primary-source manifest (SEC 10-K filing plus two secondary entries), all cited by URL and publication_date exactly as supplied."
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
      "Sizing math checks against the constraint anchor: constraints_rendered states '0.5% = $5,000, 1% = $10,000' and output_full.size_shares_or_currency=5000.0 with size_units='currency' and cash_delta '-$5,000' — a real USD notional, not a percentage in a currency field.",
      "Balance-sheet claims reproduce packet fields exactly: falsifier 1 cites 'current baseline $1,334.5M against $3,842M total debt, -$700M stockholders' equity and $272M annual interest expense' vs packet balance_sheet {cash_and_equivalents_usd_m: 1334.5, total_debt_usd_m: 3842, stockholders_equity_usd_m: -700, annual_interest_expense_usd_m: 272}.",
      "Margin falsifier quotes the packet caveat rather than inventing one: 'REPORTED 27% with management asserting inventory write-downs are one-time and structural margin is nearer ~32%' vs FY-1 note 'GM depressed by inventory write-downs; structural GM nearer ~32%' and gross_margin_pct 27.",
      "Product-clock falsifier is anchored in news/CVM recent_developments[0]: '~12-18 months from launching a CLEAN-SHEET CPU core; management claims ~40% IPC uplift in internal benchmarks' — the output preserves the INTERNAL-benchmark qualifier and demands 'independent third-party benchmarks'.",
      "Asymmetry thesis is packet-derived, not asserted: news/CVM states 'most models assume the new core gains little share and value the equity mainly on the console annuity', which supports the rationale line 'today's price is roughly covered by the contracted console business alone'; valuation.ev_s_trailing 0.8 matches the 'EV/sales of 0.8x' cited in the floor-erosion falsifier.",
      "Citation hygiene: cited_sources ['fundamentals/CVM','news/CVM'] match analyst_reports[*].report_name verbatim; the sanitizer-side sources block URLs (sec.gov 10-K, ir.amd.com, anandtech.com) are correctly absent from the trader output.",
      "Gate ordering verified: sanitizer.stage=2 with output.safe_to_run=true, leaked_terms=[], all four checks 'pass', and verification={'ok': true, 'failures': []} precede the consult output.",
      "Date arithmetic independently recomputed: 2026-08-31 + 18 months = 2028-02-29 (2028 is a leap year), matching both rerating_horizon and revisit_triggers[1].date."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "CLEAN / FAITHFUL / WORKFLOW-CORRECT; visible rationale grounded with minor gaps (3/4). (1) OUTPUT CLEANLINESS — pass. No de-masking anywhere in output_full or response_raw: the issuer is referred to only as CVM / \"the new processor\" / \"the contracted console business\"; no real issuer, product codename, incumbent, foundry, console brand or executive name appears. This matches replay.output_audit (contaminated=false, contamination_hits=[], suspect_terms=[]). The two calendar years surfaced by the audit (\"2026\",\"2028\") are the agent's own system-present anchor and a forward date derived from it — they do not reveal the true 2016-02-29 freeze era; if anything they point away from it, so era-leak risk is nil. (2) PACKET FIDELITY — pass. Every absolute the trader asserts reproduces a packet field verbatim: cash 1334.5, total debt 3842, stockholders' equity -700, interest expense 272 (all from analyst_reports[0].balance_sheet); gross margin 27% reported vs ~32% structural and the inventory-write-down caveat (annual_income_statement FY-1 note); EV/S 0.8 (valuation.ev_s_trailing); share price 6.1 consistent with the $4.00 trigger sitting below it; \"~40% IPC ... internal benchmarks\" and \"~12-18 months from launching\" quoted from news/CVM recent_developments[0]; \"revenue -27.5%\" implied by the FY-1 print. Sizing obeys the BENCHMARK SIZING ANCHOR exactly: size_shares_or_currency=5000 with size_units=\"currency\" is a true USD notional (0.5% of the $1,000,000 fictional NAV = $5,000), not a percentage smuggled into a currency field, and cash_delta \"-$5,000\" agrees. Instrument \"stock\", order_type \"market\" with limit_price/stop_price null, no leverage and no options — consistent with the no-leverage/no-options constraint. cited_sources [\"fundamentals/CVM\",\"news/CVM\"] match the packet report_name fields character-for-character; the trader correctly did NOT cite the sanitizer-side sources block (the SEC/AnandTech/IR URLs), which would have de-masked the issuer. No fabricated packet facts detected. (3) WORKFLOW — pass. Stage 2 sanitizer ran before the consult, returned safe_to_run=true with leaked_terms=[] across all four protocol checks and verification.ok=true with zero failures, so the consult was authorized. The consult then satisfied every element of the REPORTING REQUIREMENT: four FALSIFIERS are recorded inside the recommendation payload, each source-tagged, as-of-dated and terminated with an explicit decision_transition; THE CLOCK is present as both next_validation_point (next quarterly print, \"~2-3 months out\") and rerating_horizon stated as an honest band (\"2-4 years\") rather than a point estimate; revisit_triggers are machine-checkable (metric_condition on cash_and_equivalents_usd_m < 800; dated_event 2028-02-29; price_below 4.0). A confidence band (MEDIUM) is reported per fleet rule 3, and the tax field correctly declines to assert a rate because no domain_knowledge tax file was supplied to the consult — that is the right behavior under the citation rule rather than an omission. (4) REASONING GROUNDING — 3/4. The visible chain is checkable end to end: asymmetry claim (\"price roughly covered by the console business, so the chip is close to free\") is anchored in news/CVM's statement that consensus \"value[s] the equity mainly on the console annuity\" plus the 0.8x EV/S; the survival risk (\"debt far exceeds cash, book value is negative\") maps to 3842 vs 1334.5 and -700; the one-shot framing quotes the balance-sheet note \"turnaround must work\"; the date arithmetic is arithmetically correct (2026-08-31 + 18 months = 2028-02-29, a valid leap date) and matches the guided 12-18 month window; and the MEDIUM band is explicitly justified by a named epistemic limit (\"I could NOT independently separate the one-time write-down from operating margin in the supplied inputs\"), with a falsifier written to test that exact unverified claim. Gaps holding it below 4: the $800M runway threshold, the $4.00 add level and the \"~$1.1B\" watch level in next_validation_point are analyst-chosen numbers with no packet derivation (the $800M one is at least self-labelled as forward-only and ~40% below the actual), the $1.1B watch level and the $800M exit level are never reconciled to each other, the \"+1-2 years for results to show up\" increment behind the 2-4 year band is asserted rather than derived, and \"roughly Nov 2026\" assumes a fiscal calendar the deliberately date-relative packet does not supply. All are disclosed rather than dressed as packet facts, which is why this is \"minor gaps\" and not \"mixed\". I express no view on whether buy was the right call or on the outcome, and I make no claim to have seen any hidden reasoning — this grades only the written rationale.",
    "violations": [
      "MINOR / non-blocking (fidelity): position sized at 0.5% of NAV while the packet's positions field says 'Budget for a new high-potential-sleeve position: ~1-2% of portfolio'. Read as a cap ('up to ~5%' sleeve, '~1-2% per position'), under-sizing is permissible and the output discloses it plainly ('below the ~1-2% per-position band'), but the accompanying phrase 'deliberately sized at the bottom' is loose — $5,000 is below the band's bottom, not at it.",
      "MINOR / non-blocking (internal consistency): next_validation_point monitors 'whether the cash balance holds above ~$1.1B' while the hard exit trigger is cash < $800M. Both are forward, analyst-chosen levels with no packet derivation, and the output never reconciles the soft watch level to the hard exit level.",
      "MINOR / non-blocking (grounding): the $4.00 HOLD->ADD level and the '+1-2 years for share gains and margin recovery to show up' increment behind the '2-4 years' band are judgment values with no stated derivation from packet figures (share_price_usd 6.1 is the only anchor supplied).",
      "MINOR / non-blocking (semantics): revisit_triggers[2] uses decision_transition 'HOLD->ADD' on a position that this very order opens as a BUY; the intent (add to the starter on weakness, conditioned on cash > $800M and roadmap reaffirmation) is clear, but the from-state label is inconsistent with the other two triggers' 'BUY->EXIT'.",
      "OBSERVATION (not a violation): output introduces absolute calendar dates (2026-08-31 anchor, 2028-02-29, 'roughly Nov 2026') into a packet the sanitizer deliberately kept date-relative. These derive from the agent's own present date, are internally consistent, and do not expose the 2016-02-29 freeze era, so output_clean is unaffected."
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
    "rationale": "Case amd_2016_f1 (category A, grading rule F1_lenient). expected_classes = [\"buy\"]; fleet_result.action = \"buy\" — an exact in-class verdict, so the rule's full-credit condition applies and score = 1.0. No partial-credit condition is triggered: F1_lenient's 0.5 band is reserved for an F1 pass that is off-class-but-defensible (e.g. a pass carrying the killing falsifier, or a trap-side small buy); here the verdict is squarely inside the expected class, so no discount is taken. The stage-4 calibration_reviewer returned output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with zero failures, and logged only MINOR / non-blocking items (0.5% vs ~1-2% sizing band, the unreconciled ~$1.1B watch vs $800M exit level, the judgment-derived $4.00 add level, and a 'HOLD->ADD' from-state label on a freshly opened position). Per instruction I do not reopen that cleanliness/workflow judgment, and none of those minor items is a class-changing defect under the recorded rule. Acted-on return: positioned=false, so the buy opens a fresh position and the acted return equals the benchmark path over the recorded horizon — freeze price $2.14 (2016-02-29 close, post FY2015 10-K filed 2016-02-18) to horizon price $116.78 at 6y (2022-02-01) = (116.78 − 2.14) / 2.14 = +5357.0% [answer_key.resolution, yfinance adjusted closes fetched 2026-07-11]. Benchmark return over the same window is the same +5357.0% [answer_key.resolution.benchmark_return_pct]. That extraordinary outcome is recorded for the ledger only and contributes nothing to the class grade — outcome luck never moves the score, and the grade would be identical had the position gone to zero, which the fleet's own rationale explicitly priced as possible. Confidence HIGH: the class comparison is a deterministic match against a supplied answer key, the resolution prices and retrieval date are primary-source fields in the grading input, and the independent replay review is complete with no open failures.",
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
- notes: Case amd_2016_f1 (category A, grading rule F1_lenient). expected_classes = ["buy"]; fleet_result.action = "buy" — an exact in-class verdict, so the rule's full-credit condition applies and score = 1.0. No partial-credit condition is triggered: F1_lenient's 0.5 band is reserved for an F1 pass that is off-class-but-defensible (e.g. a pass carrying the killing falsifier, or a trap-side small buy); here the verdict is squarely inside the expected class, so no discount is taken. The stage-4 calibration_reviewer returned output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with zero failures, and logged only MINOR / non-blocking items (0.5% vs ~1-2% sizing band, the unreconciled ~$1.1B watch vs $800M exit level, the judgment-derived $4.00 add level, and a 'HOLD->ADD' from-state label on a freshly opened position). Per instruction I do not reopen that cleanliness/workflow judgment, and none of those minor items is a class-changing defect under the recorded rule. Acted-on return: positioned=false, so the buy opens a fresh position and the acted return equals the benchmark path over the recorded horizon — freeze price $2.14 (2016-02-29 close, post FY2015 10-K filed 2016-02-18) to horizon price $116.78 at 6y (2022-02-01) = (116.78 − 2.14) / 2.14 = +5357.0% [answer_key.resolution, yfinance adjusted closes fetched 2026-07-11]. Benchmark return over the same window is the same +5357.0% [answer_key.resolution.benchmark_return_pct]. That extraordinary outcome is recorded for the ledger only and contributes nothing to the class grade — outcome luck never moves the score, and the grade would be identical had the position gone to zero, which the fleet's own rationale explicitly priced as possible. Confidence HIGH: the class comparison is a deterministic match against a supplied answer key, the resolution prices and retrieval date are primary-source fields in the grading input, and the independent replay review is complete with no open failures.

### boot_2017 — real: Boot Barn (BOOT) @ 2017-06-02 ($6.30); obscure western/work retailer during the store-based-retail-decline narrative; ~20x by mid-2024
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
        "evidence": "Full-text scan of candidate_payload against all six forbidden strings: zero verbatim matches. Entity is consistently presented under the alias ('Prairie Outfitters (alias POT)'); report_name values are alias-scoped ('fundamentals/POT', 'news/POT'); positions and constraints reference only the alias. No source URLs, registrant identifiers, or manifest text were carried into the payload — the analyst reports contain no links at all. Two residual vectors flagged as advisory, neither a denylist hit: (a) a lowercase common noun naming the product category appears in business_description and is a substring of one denylisted ticker string, but it is unavoidable vocabulary for describing the sector and is not the ticker in ticker form; (b) balance_sheet.note discloses '~220 doors across ~30 states', an operational count that is outside the stated dollar-scaling scope and therefore passed through unscaled — it is a genuine narrowing signal and should be reported as a band rather than a near-exact figure.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven solely from source_manifest arithmetic against the masked payload; no classifier-agent facts used. Manifest annual net sales 629.8 -> payload 1323 (x2.1004). Manifest growth rate +10.7% implies prior-year 629.8/1.107 = 568.9 -> payload FY-2 1195 (x2.1005). Manifest quarterly sales 163 -> payload 342 (x2.098). Manifest GAAP net income 14.2 -> payload 30 (x2.11). Manifest adjusted net income 14.9 -> payload 31 (x2.08). Manifest GAAP annual EPS 0.53 cross-checks via the payload's own disclosed multiple: share_price 13.23 / pe_trailing 11.9 = 1.112 implied trailing EPS = 0.53 x 2.098. All six reconcile to the declared factor within rounding. Internal consistency holds independently: market_cap 357 / share_price 13.23 implies ~27.0M shares (share count correctly preserved rather than scaled); ps_trailing 357/1323 = 0.270 matches the stated 0.27; ev_s_trailing (357 + 462 - 32)/1323 = 0.595 matches the stated 0.59. Ratios are correctly exact and unscaled versus the manifest: +10.7% growth, +0.3% annual SSS, -0.9% quarterly SSS, ~-20% adjusted EPS YoY, ~15% post-print selloff. Limitation recorded: cash_and_equivalents 32 and total_debt 462 have no manifest counterpart, so those two line items are individually unproven though consistent with the scaled set.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No absolute calendar token (year, month, quarter-with-year, or filing date) appears anywhere in candidate_payload. Period labels are strictly relative: 'FY-2', 'FY-1 (just reported)', as_of 'current', latest_quarter.which 'Q4 / FY just reported', 'guide for next year is flat SSS', 'Just printed a soft year', 'Next-year guide'. The vintage-bearing publication_date values in source_manifest were not carried through. The phrase 'an extra week' is a 53-week fiscal-calendar artifact used to explain the sales/SSS divergence, not a date disclosure.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Broad de-rating of store-based specialty retail; soft apparel traffic; energy/ag end-markets uneven' — no named macro event, no named year, no named index level, no named peer bankruptcy. The manifest's specific driver language about depressed commodity prices in key markets is rendered generically as uneven energy/ag end-markets. Dominant online competitors are referenced only as 'large ecommerce platforms' and 'e-comm share gains by giants', never named. The structural-impairment narrative is described as a prevailing market view rather than pinned to a datable episode.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "CLEARED TO RUN, with two non-blocking advisories. (1) Alias: a full-text scan of candidate_payload returns zero verbatim hits against the six-item denylist; the entity is uniformly rendered as the alias in company, report_name, positions, and constraints fields. Two residual re-identification vectors exist that are not denylist hits: a lowercase product-category common noun that happens to be a substring of one denylisted ticker string, and an unscaled operational footprint (\"~220 doors across ~30 states\") that, combined with the narrow category description, materially narrows the candidate set for a sector-literate reader. Recommend widening the door/state counts to bands before production use. (2) Absolute-figure rescaling: PROVEN independently from source_manifest at the declared factor. Six manifest-anchored dollar quantities reconcile to the payload within rounding (annual sales, prior-year implied sales via the stated growth rate, quarterly sales, GAAP net income, adjusted net income, and trailing EPS recovered from the disclosed P/E and share price), and the derived figures are mutually consistent (market cap / share price implies a stable share count; P/S and EV/S recompute exactly from the payload's own line items). Cash and total debt do not appear in the manifest and so are individually unproven, but they are consistent with the rest of the scaled set. All ratios, growth rates, and margins are correctly left unscaled and match the manifest exactly. (3) Relative dates: clean — no absolute calendar token appears anywhere in the payload; period references are purely relative. (4) Macro genericization: clean — sector conditions and end-market softness are described in generic terms with no named year, named event, or named competitor. Non-protocol observation for the harness owner, offered without prejudice to the outcome: the news report is framed as an explicit bull-vs-bear dialectic that closes on the contrarian side, which is a leading-narrative risk for a downstream decision agent even though it breaches none of the four checks."
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
      "Sizing anchor honored exactly: constraint 'Therefore 0.5% = $5,000, 1% = $10,000' -> output size_shares_or_currency=10000.0, size_units='currency', concentration_delta 'POT goes 0% -> ~1.0% of portfolio NAV ($10,000 of the $1,000,000 benchmark NAV)'; a true USD notional, not a percentage in a currency field, and inside the '~1-2% per position' / '~5% sleeve' caps.",
      "'under 12 times earnings' <- packet valuation.pe_trailing = 11.9.",
      "'sales rose 10.7%' <- annual_income_statement FY-1 revenue_growth_pct = 10.7.",
      "'profit per share fell about 20%' <- FY-1 note 'adj EPS down ~20% YoY despite sales growth'.",
      "'$462M of debt against $32M of cash' <- balance_sheet total_debt_usd_m = 462, cash_and_equivalents_usd_m = 32.",
      "'current baseline 2.3%, i.e. $31M on $1,323M' <- adjusted_net_income_usd_m 31 / revenue_usd_m 1323 = 2.34%; arithmetic verified.",
      "'-0.9% for the quarter and +0.3% for the full year, with management guiding flat for next year' <- latest_quarter.same_store_sales_pct = -0.9, FY-1 same_store_sales_pct = 0.3, latest_quarter.note 'guide for next year is flat SSS'.",
      "'Revenue growth of 10.7% came from new doors and an extra week, not from existing stores' <- news/POT recent_developments: 'sales up on new doors and an extra week, but adjusted earnings down and Q4 SSS negative'.",
      "Constraint 'No leverage, no options' honored: instrument='stock', order_type='market', limit_price=null, stop_price=null.",
      "Citation discipline: cited_sources ['fundamentals/POT','news/POT'] appear verbatim as packet report_name values; the real de-masking URLs in the sources block (SEC/EDGAR, trade press) were not surfaced in the output.",
      "REPORTING REQUIREMENT discharged: four falsifiers each with metric + as-of baseline + threshold + 'BUY->EXIT'; next_validation_point = 'Next quarterly earnings print ... first quarter reported against the flat same-store-sales guide'; rerating_horizon = '2-4 years' delivered as an honest band with the explicit unmodellable-pace caveat ('no EBITDA, free cash flow, interest expense or gross margin').",
      "Structural falsifier is honestly downgraded rather than dressed up: 'STRUCTURAL (no numeric threshold disclosed in the packet, so flagged qualitative rather than dressed up as precise)' <- news/POT bull case ('rising exclusive-brand mix', full-price discipline) carries no figures.",
      "No-fabrication rule honored on tax: 'no tax-rate reference file was supplied in this packet, so no rate is asserted here' — no invented domain_knowledge path or rate.",
      "Masking respected: output never names a real registrant, never flags the alias as a data defect, and does not echo the sanitizer's flagged narrowing vector ('~220 doors across ~30 states').",
      "Machine-readable revisit_triggers (cash < 15.0; debt > 525.0; price_below 10.0 vs packet share_price_usd 13.23, explicitly labeled 'a review prompt, not a thesis break') are consistent with, and do not contradict, the prose falsifiers.",
      "Harness output_audit corroborates cleanliness: contaminated=false, contamination_hits=[], suspect_terms=[]; sole calendar_years_in_output entry is '2026' (session year, not case vintage 2017)."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "Replay audit of case boot_2017 (freeze 2017-06-02), masked candidate POT, decision = BUY $10,000.\n\nOUTPUT CLEANLINESS — PASS. The decision output contains no real-entity identifiers, no source URLs from the `sources` block (which would have de-masked the registrant), and no facts dated after the 2017-06-02 freeze. It does not attempt to identify the real company and does not treat the masking as a data-quality defect, per the provenance note. It also does not repeat the sanitizer's flagged narrowing vector (the \"~220 doors across ~30 states\" footprint). The only calendar token is the session year 2026, consistent with the harness's own output_audit (contaminated=false, contamination_hits=[]); it is session metadata, not case-vintage or post-freeze leakage.\n\nPACKET FIDELITY — PASS with two minor advisories. Every quantitative claim in the output reconciles to a packet field: 11.9x P/E, +10.7% revenue growth, ~-20% adjusted EPS, $462M debt / $32M cash, 2.3% adjusted net margin (31/1323 = 2.34%), -0.9% quarterly and +0.3% annual SSS, flat forward guide. Sizing honors the benchmark anchor exactly ($10,000 = 1.0% of the $1,000,000 NAV, expressed as a true USD notional in a currency field, inside the ~1-2%/position and ~5% sleeve caps). Instrument/order fields respect \"no leverage, no options\". Citations use only alias-scoped report_names that appear verbatim in the packet. Two nits: (a) the \"~3.2% FY-2 margin\" baseline is a derivation (FY-2 adjusted net income is not in the packet) but is attributed to the annual income statement; (b) a trigger labeled \"below half of the current $32M baseline\" encodes <$15.0M rather than <$16.0M.\n\nWORKFLOW CORRECTNESS — PASS. The fleet reporting requirement is fully discharged: four falsifiers, each with a named metric, an as-of baseline, a numeric threshold (or an explicit, honest \"no numeric threshold disclosed\" flag for the structural leg) and a BUY->EXIT transition; a stated NEXT VALIDATION POINT (next quarterly print vs the flat-SSS guide); and a re-rating horizon given as an honest band (\"2-4 years\") with an explicit statement of what cannot be modelled from the packet (no EBITDA, FCF, interest expense, gross margin) and why that caps conviction at MEDIUM. Confidence band reported. The tax field correctly declines to assert an Israeli CGT rate because no domain_knowledge file was supplied — the no-fabrication rule was honored rather than papered over. Machine-readable revisit_triggers mirror the prose falsifiers without contradiction.\n\nREASONING GROUNDEDNESS — 3/4. The visible rationale is traceable step by step to packet facts, and it correctly foregrounds the tension the packet actually contains (revenue growth from new doors plus an extra week versus negative quarterly SSS and a levered balance sheet), rather than restating the news report's contrarian framing. Deductions are for the three grounding gaps: the FY-2 margin derivation presented with a stronger source attribution than the packet supports, the label/threshold mismatch on the cash trigger, and the import of the session date (2026-08-31 / \"roughly Nov 2026\") into a packet that was deliberately kept date-free — an out-of-packet fact used as an analytic anchor. None of these are fabrications; all are hedged or self-evidently derived, so this is \"grounded with minor gaps\" rather than \"mixed\".\n\nNo assessment of whether BUY was the right call is offered or implied; only cleanliness, fidelity, workflow, and grounding were graded. This review is based solely on the persisted packet and replay text supplied — no hidden reasoning was accessed or inferred.",
    "violations": [
      "ADVISORY (replay hygiene, non-blocking): next_validation_point anchors to an out-of-packet fact — 'estimated 2-3 months from 2026-08-31 (roughly Nov 2026)' — although the packet is deliberately date-free (sanitizer relative_dates check: 'No absolute calendar token ... appears anywhere in candidate_payload') and the case freeze is 2017-06-02. The session date is imported as an analytic anchor. No identity or post-freeze leakage results, and the agent did hedge ('exact fiscal calendar not supplied in the packet, so kept relative'), but the purely relative form would have been the faithful rendering.",
      "ADVISORY (over-attributed derivation): falsifier #1 sources the FY-2 baseline to 'fundamentals/POT annual income statement, adjusted net income divided by revenue ... already down from roughly 3.2% in FY-2'. The packet discloses only FY-2 revenue ($1,195M); FY-2 adjusted net income is absent. The 3.2% is inferred from the FY-1 note 'adj EPS down ~20% YoY' (31 / 0.8 = 38.75; 38.75 / 1195 = 3.24%) and silently assumes a stable share count. Hedged with 'roughly', but cited as if disclosed.",
      "NIT (label/threshold mismatch): revisit_trigger label reads 'liquidity stress: cash below half of the current $32M baseline' while the encoded threshold is value=15.0 (half of $32M is $16.0M). The encoded number is the operative one and is internally consistent with the falsifier prose ('fall below $15M'); only the label is loose."
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
    "acted_return_pct": 1855.0,
    "benchmark_return_pct": 1855.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case boot_2017 (category A, entry grading, positioned=false). expected_classes = [\"buy\"]; fleet_result.action = \"buy\" — an exact in-class match, so the benchmark rule awards the full 1.0. The partial-credit carve-outs in the recorded rule (an F1 pass carrying falsifiers, or a small trap buy against the killing falsifier) do not apply here: this is a straight expected-class hit, not a trap case, so no 0.5 discount is warranted. Acted return: the fleet took a $10,000 (~1.0% of the $1,000,000 benchmark NAV) long starter at the freeze price of $6.30 real (answer_key.resolution.price_at_freeze_real), resolving at $123.15 real on the 7y horizon (2024-06-03, answer_key.resolution.price_at_horizon_real). (123.15 - 6.30) / 6.30 = +1854.8%, consistent with the recorded answer_key.resolution.benchmark_return_pct of 1855. Because the action was a full-direction long entry with no partial fill or hedge, acted return equals the benchmark return at 1855%; sizing (1% of NAV) affects portfolio contribution, not the per-case acted return figure. Outcome luck is explicitly excluded from the class grade — the +1855% resolution neither raises nor could have lowered the 1.0, which rests solely on the verdict class. Per instruction, the calibration_reviewer's stage-4 findings (output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with three non-blocking advisories/nits) are not reopened and carry no weight in this score. Confidence HIGH: the verdict class, the expected class, and both resolution prices are supplied verbatim in the grading input, and the return arithmetic reconciles to the recorded benchmark figure.",
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
- notes: Case boot_2017 (category A, entry grading, positioned=false). expected_classes = ["buy"]; fleet_result.action = "buy" — an exact in-class match, so the benchmark rule awards the full 1.0. The partial-credit carve-outs in the recorded rule (an F1 pass carrying falsifiers, or a small trap buy against the killing falsifier) do not apply here: this is a straight expected-class hit, not a trap case, so no 0.5 discount is warranted. Acted return: the fleet took a $10,000 (~1.0% of the $1,000,000 benchmark NAV) long starter at the freeze price of $6.30 real (answer_key.resolution.price_at_freeze_real), resolving at $123.15 real on the 7y horizon (2024-06-03, answer_key.resolution.price_at_horizon_real). (123.15 - 6.30) / 6.30 = +1854.8%, consistent with the recorded answer_key.resolution.benchmark_return_pct of 1855. Because the action was a full-direction long entry with no partial fill or hedge, acted return equals the benchmark return at 1855%; sizing (1% of NAV) affects portfolio contribution, not the per-case acted return figure. Outcome luck is explicitly excluded from the class grade — the +1855% resolution neither raises nor could have lowered the 1.0, which rests solely on the verdict class. Per instruction, the calibration_reviewer's stage-4 findings (output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with three non-blocking advisories/nits) are not reopened and carry no weight in this score. Confidence HIGH: the verdict class, the expected class, and both resolution prices are supplied verbatim in the grading input, and the return arithmetic reconciles to the recorded benchmark figure.

### cvna_2023_reentry — real: CVNA @ 2023-08-01 ($49.11 as-traded); re-entry after 2023-07-19 debt exchange + record GPU/Adj EBITDA print
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
        "evidence": "Full-text scan of candidate_payload (alias, analyst_reports, constraints_rendered, positions) found zero verbatim or near-verbatim matches to any of the 7 forbidden_terms. Identity-bearing specifics are genericized: the acquired wholesale-auction subsidiary appears only as 'a physical wholesale-auction network acquired at the prior cycle peak'; leadership appears only as 'Founder-controlled' with no surname; no retail-kiosk/tower motif and no creditor-counterparty name appears anywhere. Ticker and issuer render solely as 'MZXQ' / 'Masked Auto Marketplace (alias MZXQ)'. ADVISORY (outside the audited object): the raw_sources and source_manifest entries carry issuer-identifying hostnames and a CIK-bearing filing path; those blocks must NOT be forwarded to the Trader in any form, and the payload as-is contains no citation URLs, which is correct.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Independently derived from source_manifest only, testing the declared factor 1.15: quarterly revenue $2.968B x1.15 = $3,413.2M vs payload 3413; total GPU $6,520 x1.15 = $7,498 vs payload 7498; Adjusted EBITDA $155M x1.15 = $178.3M vs payload 178; GAAP net loss -$105M x1.15 = -$120.8M vs payload -121; cash $677M x1.15 = $778.6M vs payload 779; debt reduction >$1.2B x1.15 = >$1.38B vs payload '>$1380M'; cash-interest relief >$430M/yr x1.15 = >$494.5M/yr vs payload '>$494M/yr'. Seven of seven manifest-provable monetary figures match at a single consistent constant, and no payload figure reproduces an un-rescaled manifest value. Ratios are preserved unscaled as required (revenue -24% YoY; units ~-35% YoY; >90% noteholder support). Non-monetary unit count is jittered rather than scaled (payload 75,840 vs manifest 76,530, -0.9%), which is acceptable de-searchability but introduces a ~0.9% internal inconsistency: payload units x payload GPU = $568.6M of gross profit versus $573.8M implied by scaling the manifest product. Two soft residuals, neither identity-revealing: the narrative phrase 'Cash ~mid-hundreds of millions' tracks the un-rescaled cash magnitude better than the payload's own scaled 779, and payload figures not covered by the manifest (FY-1/FY-2 revenue and net income, total_debt_usd_m 5980, market_cap_usd_m 10652, share_price_usd 56.48, EV/S 1.01, P/S 0.68) cannot be proven from the supplied manifest and were not credited toward this verdict.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No absolute date, month, or year string appears anywhere in candidate_payload. Fiscal periods are relative labels ('FY-2', 'FY-1'), the reporting anchor is relative ('as_of': 'current'; 'Q2, reported ~2 weeks ago'), and historical framing is relative ('Two years ago the firm looked like a survival risk', 'the information set that said survival risk a year ago', 'prior cycle high', 'prior cycle peak'). The manifest's dated maturity tranches ('2025/2027 unsecured maturities') were genericized to 'extend maturities' and 'removes the near-term maturity wall', so no calendar year survives. The interest-relief duration is carried as a relative span ('for two years'). Nothing in the payload discloses the true as-of date or lets it be back-solved.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The macro_context reads 'Used-vehicle prices off the shock-era peak; auto retail demand soft; credit tightening still in the background' — the demand/price shock is referenced only as 'shock-era' with no named pandemic, stimulus program, central-bank body, hiking-cycle label, or dated policy action. Cycle references elsewhere are likewise abstract ('prior cycle peak', 'prior cycle high', 'debt-spiral year'). The credit backdrop is stated as a standing condition rather than a datable event, so no macro anchor lets the period be pinned.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Clean on all four protocol dimensions; no concrete leak found, so safe_to_run=true. Aliasing holds: none of the seven forbidden terms appear in candidate_payload, and the subsidiary, founder, and creditor references are all genericized. Rescaling is independently proven from source_manifest alone — all seven manifest-provable monetary figures reconcile at exactly the declared 1.15 constant, with ratios and growth rates left unscaled, and no un-rescaled value survives into the payload. Relative dating is complete (no year, month, or date string anywhere; dated maturity tranches genericized), and macro framing stays abstract ('shock-era peak', 'credit tightening in the background'). Three advisories for the audit trail, none blocking: (1) the retail-unit count is jittered ~0.9% rather than scaled, creating a small units-times-GPU inconsistency against the manifest-implied gross profit; (2) the narrative phrase 'cash ~mid-hundreds of millions' better matches the pre-scaling magnitude than the payload's own scaled cash line and should be re-worded to the scaled band; (3) a material block of payload figures — both FY income-statement rows, total debt, market cap, share price, and the EV/S and P/S multiples — is not covered by the manifest and therefore was not credited as proven, which is the main reason confidence is MEDIUM rather than HIGH; a manifest extension covering the annual and market-cap lines would let a future audit prove the factor end-to-end. Separately, the raw_sources and source_manifest blocks themselves carry issuer-identifying hostnames and a CIK-bearing filing path and must never be forwarded to the Trader — the payload correctly contains no URLs today. Residual re-identification risk from the qualitative business description remains non-zero but is a design property of the packet, not a protocol violation."
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
      "Sizing anchor honored: constraints_rendered states '1% = $10,000'; output_full.size_shares_or_currency=10000.0 with size_units='currency' and concentration_delta '0% -> ~1.0% of the $1,000,000 benchmark NAV; sleeve remains well under the ~5% cap'.",
      "GPU figure exact: packet analyst_reports[0].latest_quarter.total_gpu_usd = 7498; output falsifier 1 cites 'current baseline $7,498'.",
      "Adjusted EBITDA exact: packet latest_quarter.adjusted_ebitda_usd_m = 178; output revisit trigger reads 'baseline +$178M in Q2' with metric 'adjusted_ebitda_usd_m', op '<=', value 0.0.",
      "GAAP loss exact and used against the bull case: packet latest_quarter.gaap_net_income_usd_m = -121; output falsifier 4 states 'GAAP net income is still negative at -$121M in Q2, so the inflection claim rests on GPU and Adjusted EBITDA'.",
      "Debt threshold is packet arithmetic: balance_sheet.total_debt_usd_m = 5980 and news 'cut >$1380M of debt'; output falsifier 3 sets the test at 'fails to fall below $4,600M, or subsequently re-rises above $5,980M'.",
      "Unit trade-off grounded: news 'units fell ~35% YoY — a deliberate profitability-over-growth pivot'; rationale mirrors this as 'made real money per car sold instead of just selling more cars'.",
      "Announced-vs-executed distinction caught: packet recent_developments says 'support agreement with >90% of unsecured noteholders'; output conditions on 'the first balance sheet published after the noteholder exchange closes' and 'if the reported debt reduction does not appear in the filings'.",
      "Citations valid: packet report_name values are 'fundamentals/MZXQ' and 'news/MZXQ'; output cited_sources = ['fundamentals/MZXQ','news/MZXQ'] — verbatim, with no substitution of the real EDGAR/investor-relations URLs held in the sources block.",
      "Clock requirement satisfied: constraints demand 'THE CLOCK ... and the expected re-rating horizon as an honest band'; output supplies next_validation_point (Q3 print) and rerating_horizon 'Honest band: 6-12 months ... 2-4 years'.",
      "No-fabrication behavior on tax: output states 'Israeli capital-gains treatment not quantified here — no domain_knowledge tax file was supplied with this consult' rather than asserting an uncited rate.",
      "Ungrounded threshold (gap): packet share_price_usd = 56.48, but the price_below trigger at 35.0 is never derived or justified against it.",
      "Unengaged packet data (gap): valuation block supplies ev_s_trailing 1.01, ps_trailing 0.68, market_cap_usd_m 10652; none appear in rationale_summary, falsifiers, or horizon reasoning."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "CLEAN / FAITHFUL / WORKFLOW-CORRECT, with two non-blocking advisories.\n\nOUTPUT CLEANLINESS (pass): response_raw is exactly one JSON object — no prose, headings, or code fences. No issuer-identifying term appears anywhere in the output; the instrument is carried solely as \"MZXQ\". Critically, the four real source URLs in the `sources` block (which carry an issuer-identifying hostname and a CIK-bearing EDGAR path) were NOT reproduced — `cited_sources` contains only \"fundamentals/MZXQ\" and \"news/MZXQ\", both of which appear verbatim as `report_name` values in the packet. No invented or paraphrased source_ids. The only calendar year emitted is 2026, matching `output_audit.calendar_years_in_output` with contaminated=false; it derives from the harness's ambient date, not from the 2023-08-01 freeze date, so it is anti-leakage rather than leakage.\n\nPACKET FIDELITY (pass): every asserted figure reconciles to a packet field — total GPU $7,498 (fundamentals.latest_quarter.total_gpu_usd 7498), Adjusted EBITDA +$178M (178), GAAP net income -$121M (-121), total debt $5,980M (5980), announced reduction >$1,380M (\">$1380M\"), units ~-35% YoY (news: \"units fell ~35% YoY\"). Sizing obeys the benchmark anchor exactly: size_shares_or_currency=10000.0 with size_units=\"currency\" = 1.0% of the stipulated $1,000,000 NAV, correctly stated as a USD notional and not a percentage smuggled into a currency field; concentration_delta correctly notes the ~5% sleeve cap is not approached. Constraint compliance on instrument type holds (stock, market order, no options, no leverage). The $4,600M debt falsifier is honest arithmetic off packet values (5980 - 1380). Numbers not in the packet ($6,000 GPU floor, $35 price level, 2026-11-15) are judgment thresholds, not asserted facts, and none contradict packet data. The tax field correctly declines to quantify Israeli CGT and states that no domain_knowledge file was supplied rather than fabricating a rate — correct behavior under the no-fabrication rule, though it stops short of explicitly routing the gap to the domain-refresh agent.\n\nWORKFLOW (pass): both mandated reporting elements are present and substantive. FALSIFIERS are specific and checkable, each with a named metric, a stated baseline, a threshold, and a decision transition; one is explicitly structural (one-time gain on debt extinguishment vs. genuine operating improvement), which is the correct discriminator for this fact pattern. THE CLOCK is complete: a named next validation point (Q3 print) plus an honest two-tier band (6-12 months for confirmation, 2-4 years for full re-rating), with the width of the band explicitly tied back to residual leverage and to the 1% rather than 2% sizing. Confidence=MEDIUM is defensible under the stated band definitions given a single quarter of evidence.\n\nREASONING GROUNDING (3/4): the visible rationale is step-by-step traceable. \"Heavily indebted\" ties to 5980 debt against 779 cash; \"still loss-making on a full accounting basis\" ties to GAAP -121 sitting alongside positive Adj EBITDA 178; \"made real money per car sold instead of just selling more cars\" is the correct reading of record GPU 7498 against ~-35% units and -24% revenue; \"only one quarter of proof\" justifies the starter size and MEDIUM confidence. Notably, falsifier 3 catches an internal inconsistency in the packet itself — business_description says the exchange was \"just completed\" while recent_developments describes only a support agreement — and resolves it correctly by conditioning on the first post-exchange balance sheet actually printing the reduction. Gaps holding this at 3 rather than 4: (a) the $35 price trigger is never derived or explained against the packet's $56.48 share price; (b) the packet supplies valuation multiples (EV/S 1.01, P/S 0.68, market cap 10652) that the rationale never engages, so the buy case rests entirely on operational inflection with no stated view on price paid.\n\nADVISORY (harness, not model): the dated trigger 2026-11-15 is ~3 years off the 2023-08-01 case freeze date. The packet was deliberately date-free (sanitizer relative_dates check: \"No absolute date, month, or year string appears anywhere\"), so the model had no way to know the true period and correctly anchored on its ambient date. No penalty assessed, but the dated triggers are not actionable on the real historical timeline.\n\nI grade only cleanliness, fidelity, workflow, and grounding of the written rationale. I make no judgment on whether BUY was the correct call, and I make no claim of access to any hidden reasoning.",
    "violations": [
      "ADVISORY (non-blocking, precision): revisit_triggers[1] carries a hard date '2026-11-15' for the Q3 print, but the packet supplies no earnings calendar — only 'Q2, reported ~2 weeks ago'. next_validation_point correctly hedges this as 'estimated ~2-3 months out', yet the trigger object itself presents the date without an estimated flag, overstating precision relative to the packet.",
      "ADVISORY (non-blocking, internal consistency): the price_below trigger sets decision_transition 'HOLD->ADD' while the recommended action is 'buy' and the label reads 'consider adding on weakness' — the stated transition does not match the position state the order establishes.",
      "ADVISORY (non-blocking, harness artifact): output dates the next validation point to ~November 2026 against a case freeze_date of 2023-08-01. This is attributable to the ambient session date, not to model contamination — the packet was intentionally date-free per the sanitizer's relative_dates pass — but the dated triggers are therefore not actionable on the true replay timeline."
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
    "acted_return_pct": 648.9,
    "benchmark_return_pct": 648.9,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Entry-grading case (positioned=false), expected_classes=[\"buy\"]. The fleet verdict was action=\"buy\" (a $10,000 / ~1% starter in MZXQ), which lands squarely in the expected class, so the benchmark rule assigns the full in-class grade of 1.0. No partial-credit condition applies: this is not an F1-pass-with-falsifiers case and not a trap-buy-with-killing-falsifier case, so the 0.5 band is not invoked, and there is no basis to drop to 0.0 given the class match. Sizing conservatism (starter position, MEDIUM confidence, \"only one quarter of proof\") does not change the class — the recorded rule grades the action class, not the size. Acted-on return is computed from the supplied resolution: price_at_freeze_real 49.11 → price_at_horizon_real 367.78 over the 2y horizon to 2025-08-01, i.e. (367.78/49.11 − 1) = +648.9%, matching the recorded benchmark_return_pct of 648.9 [answer_key.resolution, grading input]. Because the action was a long entry with no partial fill or scaling recorded, acted_return_pct equals benchmark_return_pct at 648.9%; the outsized realized gain is outcome luck and neither raises nor lowers the class grade, and the same 1.0 would stand had the position gone to zero. Per instruction I do not reopen the stage-4 calibration_reviewer's judgment, which recorded output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, and three non-blocking advisories (an over-precise 2026-11-15 trigger date, a HOLD->ADD transition label inconsistent with an opening buy, and a harness ambient-date artifact) — all advisory-only and explicitly non-blocking, so they do not reduce the class score. Confidence=HIGH: the verdict class, the answer key, the grading rule, and both resolution prices are all supplied primary inputs, and the return arithmetic reconciles exactly to the recorded benchmark figure with no stale or missing facts requiring domain-refresh escalation.",
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
- notes: Entry-grading case (positioned=false), expected_classes=["buy"]. The fleet verdict was action="buy" (a $10,000 / ~1% starter in MZXQ), which lands squarely in the expected class, so the benchmark rule assigns the full in-class grade of 1.0. No partial-credit condition applies: this is not an F1-pass-with-falsifiers case and not a trap-buy-with-killing-falsifier case, so the 0.5 band is not invoked, and there is no basis to drop to 0.0 given the class match. Sizing conservatism (starter position, MEDIUM confidence, "only one quarter of proof") does not change the class — the recorded rule grades the action class, not the size. Acted-on return is computed from the supplied resolution: price_at_freeze_real 49.11 → price_at_horizon_real 367.78 over the 2y horizon to 2025-08-01, i.e. (367.78/49.11 − 1) = +648.9%, matching the recorded benchmark_return_pct of 648.9 [answer_key.resolution, grading input]. Because the action was a long entry with no partial fill or scaling recorded, acted_return_pct equals benchmark_return_pct at 648.9%; the outsized realized gain is outcome luck and neither raises nor lowers the class grade, and the same 1.0 would stand had the position gone to zero. Per instruction I do not reopen the stage-4 calibration_reviewer's judgment, which recorded output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, and three non-blocking advisories (an over-precise 2026-11-15 trigger date, a HOLD->ADD transition label inconsistent with an opening buy, and a harness ambient-date artifact) — all advisory-only and explicitly non-blocking, so they do not reduce the class score. Confidence=HIGH: the verdict class, the answer key, the grading rule, and both resolution prices are all supplied primary inputs, and the return arithmetic reconciles exactly to the recorded benchmark figure with no stale or missing facts requiring domain-refresh escalation.

### hyln_2021 — real: Hyliion (HYLN) @ 2021-06-01 ($10.61); obscure Class-8 e-powertrain blank-check-merger winner-shape with $0 revenue; -86% by mid-2024
- **Fleet reasoning:** hold (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
- **Reasoning-integrity audit:** temporal OK; contamination hits: []; suspects: []; years in output: ['2026']
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
  "model": "claude-opus-5",
  "output": {
    "checks": [
      {
        "check": "alias",
        "evidence": "Full-text scan of candidate_payload for all six forbidden_terms returned zero matches. Substitutions in place: product -> 'range-extender electric architecture'/'flagship long-haul architecture'; sponsor -> 'blank-check merger'; CEO -> 'Charismatic founder-CEO' (unnamed); OEM -> 'OEM dependency' (unnamed). Alias 'VHS'/'VoltHaul Systems' is not an initialism or morphological derivation of any denylisted string. Forbidden terms occur only in raw_sources, source_manifest and the forbidden_terms key, none of which are part of the Trader-visible payload. Non-blocking residual: 'natural-gas generator + battery', 'Class-8', 'fleet advisory council' and the capitalized 'Hybrid' are distinctive but absent from the denylist, so they do not constitute a concrete leak.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Proven independently from source_manifest (sec.gov filing dated 2021-05-17) against the masked payload at the declared factor 0.85: net loss -16.6 x 0.85 = -14.11 vs payload -14; R&D 9.3 x 0.85 = 7.905 vs payload 8; SG&A 7.4 x 0.85 = 6.29 vs payload 6; cash 335 x 0.85 = 284.75 vs payload 285; investments ~297 x 0.85 = 252.45 vs payload 252; revenue 0 x 0.85 = 0 vs payload 0. Six of six manifest-covered absolutes reconcile within rounding. GAP, stated not fabricated: market_cap_usd_m 1530, share_price_usd 9.02 and FY-1 net_income_usd_m -60 have no counterpart in source_manifest or raw_sources, so their scaling is unverifiable from the frozen packet; no proof is asserted for them.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "candidate_payload contains no absolute calendar year, month-name, or ISO date. Temporal references are exclusively relative: as_of 'current'; fy 'FY-1'; 'Q1, 10-Q filed ~2 weeks ago'; 'Went public via a blank-check merger months ago'; 'guides first Hybrid revenue in the back half of the year'; 'year-end demos'; 'prior-year blank-check warrant accounting'. The 2021-05-11/17/19 dates present in source_manifest do not propagate into the payload.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The manifest fact (ttnews.com, 2021-05-19) names a dated regulator-driven restatement affecting ~80 blank-check vehicles. The payload reduces this to 'Restating prior-year blank-check warrant accounting (industry-wide issue); delayed some financial commentary' and 'blank-check complex starting to re-rate as warrant accounting and lockups hit' — regulator, announcement date, and the ~80-vehicle population count are all stripped. Remaining macro colour ('freight cycle firm', 'ESG/decarbonization capital abundant') is undated and regime-level, not a pinpointable event.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Payload is clean of concrete leaks and safe to run. (1) ALIAS: no forbidden term appears anywhere in candidate_payload; the flagship product, sponsor vehicle, CEO, and OEM partner are all rendered generically. Residual (non-blocking) identifiability risk: the payload retains distinctive descriptors — \"natural-gas generator + battery range-extender for Class-8 trucks\", \"fleet advisory council\", and the capitalized product word \"Hybrid\" — which are not on the denylist but are narrow enough that a well-read Trader could plausibly infer the issuer; recommend the packet builder consider softening these in future cases. (2) RESCALING: independently proven at factor 0.85 from source_manifest for six figures (net loss -16.6 -> -14.11 ~ -14; R&D 9.3 -> 7.91 ~ 8; SG&A 7.4 -> 6.29 ~ 6; cash 335 -> 284.75 ~ 285; investments ~297 -> 252.45 ~ 252; revenue 0 -> 0). Three payload figures — market_cap_usd_m 1530, share_price_usd 9.02, FY-1 net_income_usd_m -60 — have no manifest counterpart and are NOT provable either way; they are internally plausible but I am not asserting them as verified. Marked pass on the provable set with that gap stated explicitly rather than inventing proof. (3) RELATIVE DATES: no absolute calendar year or dated event anywhere in the payload. (4) MACRO GENERICIZATION: the warrant-accounting restatement is presented as an undated \"industry-wide issue\" with the regulator, date, and ~80-vehicle population count stripped. Confidence MEDIUM: three unpriced figures lack manifest coverage, so scaling coverage is partial; recommend the domain-refresh agent extend the manifest to cover market cap and share price for future cases in this family."
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
      "rationale 'zero sales' <- packet.analyst_reports[fundamentals/VHS].latest_quarter.revenue_usd_m = 0 and annual_income_statement[FY-1].revenue_usd_m = 0",
      "rationale 'ten trucks installed but none sold' <- packet.analyst_reports[news/VHS].recent_developments: 'Ten Hybrid units installed in the latest quarter — deployments, not sold-and-recognized volume.'",
      "rationale 'paying roughly a billion dollars above its cash' <- derived from packet market_cap_usd_m 1530 minus (cash_and_equivalents_usd_m 285 + marketable_investments_usd_m 252 = 537) = 993; arithmetic checks, though 'cash' is used as shorthand for cash-plus-investments",
      "rationale 'cash pile funds years of burn, so survival isn't the question' <- packet balance_sheet.note 'post-listing cash pile funds ~multi-year burn at current rate; no revenue yet' and total_debt_usd_m = 0",
      "falsifier #4 baseline '$14m GAAP net loss on $8m R&D plus $6m SG&A' <- packet.latest_quarter gaap_net_income_usd_m -14, rd_usd_m 8, sga_usd_m 6",
      "falsifier #1 as-of anchor 'latest Q1 10-Q filed ~2 weeks ago' <- packet.latest_quarter.which",
      "falsifier #3 'only commercial artifacts are a non-binding fleet advisory council and chassis deliveries for demos' <- packet news/VHS bulls line listing 'fleet advisory council, chassis deliveries for demos'",
      "next_validation_point 'first guided back-half Hybrid revenue' and 'year-end flagship long-haul demos' <- packet news/VHS: 'Management guides first Hybrid revenue in the back half of the year and year-end demos of the flagship long-haul architecture.'",
      "next_validation_point secondary item 'in-flight blank-check warrant-accounting restatement' <- packet news/VHS: 'Restating prior-year blank-check warrant accounting (industry-wide issue); delayed some financial commentary.' and fundamentals note 'warrant-accounting restatement in flight'",
      "sizing '0.5-1% accepted-total-loss starter, $5,000-$10,000 at the $1,000,000 benchmark NAV' <- replay.constraints_rendered BENCHMARK SIZING ANCHOR (0.5% = $5,000, 1% = $10,000) and the ~1-2% per-position sleeve budget in packet.positions",
      "cited_sources ['fundamentals/VHS','news/VHS'] both appear verbatim as packet.analyst_reports[*].report_name — no fabricated source_id",
      "workflow gate <- sanitizer.stage = 2, output.safe_to_run = true, output.leaked_terms = [], verification.ok = true, verification.failures = []",
      "cleanliness <- replay.output_audit.contaminated = false, contamination_hits = [], suspect_terms = [], calendar_years_in_output = ['2026'] (session-relative rendering, not frozen-period leakage)"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay audit of case_id hyln_2021 (freeze_date 2021-06-01), packet alias VHS. CLEANLINESS: `replay.response_raw` is exactly one JSON object with no prose, headings, or code fences; it parses to `replay.output_full` field-for-field. No real issuer name, product name, sponsor vehicle, executive name, or OEM partner appears anywhere in the output; the trader repeats only the packet's own generic descriptors ('blank-check merger', 'Hybrid', 'flagship long-haul architecture'). No 2021-era or post-freeze market knowledge is imported — the only calendar year emitted is 2026 (replay.output_audit.calendar_years_in_output = [\"2026\"], contaminated=false, contamination_hits=[]), which is the session-relative rendering of the packet's undated 'year-end demos' phrase, not a leak of the frozen period. PACKET FIDELITY: every absolute figure the output asserts traces verbatim to `packet.analyst_reports`: revenue $0m, 10 Hybrid units installed (deployments, not recognized), $14m quarterly GAAP net loss, $8m R&D, $6m SG&A, 'Q1 10-Q filed ~2 weeks ago', multi-year runway, non-binding fleet advisory council + chassis deliveries. The one derived number — 'roughly a billion dollars above its cash' — reconciles exactly against the packet (market_cap 1530 less cash 285 + marketable investments 252 = 537, i.e. 993); the shorthand 'cash' for cash-plus-investments is loose but arithmetically correct, the only imprecision found. CONSTRAINT FIDELITY: the $1,000,000 benchmark NAV anchor is applied correctly — the flip-to-buy starter is expressed as '0.5-1% ... $5,000-$10,000', matching the rendered mapping, and no percentage is smuggled into a currency field (action=hold, size 0.0 shares, no USD notional at all). All three mandated reporting elements are present in dedicated schema fields: FALSIFIERS (four, three flip-to-buy plus one explicitly labelled hardening/confirming condition), NEXT VALIDATION POINT (next quarterly 10-Q ~2-3 months out carrying guided back-half revenue, then year-end demos), and an honest re-rating band (6-12 months for first read, 2-4 years for commercial scale, with the width explicitly declared unestimable-beyond-that and folded into conviction rather than hidden). The masking instruction is honoured: no attempt to de-anonymize, and the alias is never treated as a data defect. WORKFLOW: stage-2 sanitizer ran and returned safe_to_run=true, leaked_terms=[], verification.ok=true with failures=[], and the trader ran downstream of it on the masked payload. cited_sources lists only 'fundamentals/VHS' and 'news/VHS', both appearing verbatim as report_name values in the packet — no invented, abbreviated, or constructed source_id. Structured revisit_triggers are internally consistent with the prose falsifiers and each carries a decision_transition. OBSERVATIONS (not violations, no verdict impact): (a) falsifier #1 (two consecutive revenue quarters with positive gross margin) has no matching structured revisit_trigger, but the output self-discloses this as a deliberate compound/persistence condition left qualitative; (b) action='hold' is used to mean 'do not initiate' on a name the packet confirms is not held — semantically loose, though the rationale removes all ambiguity ('do not initiate a position, not even a small starter'); (c) confidence=MEDIUM sits below the HIGH bands of both input reports, but the rerating_horizon field supplies the justification (irreducible outcome width). GRADING NOTE: this review assesses only cleanliness, fidelity, workflow, and grounding of the written rationale; no judgement is offered on whether hold was the correct investment call, and no claim is made about any hidden reasoning.",
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
    "acted_return_pct": 0.0,
    "benchmark_return_pct": -86.0,
    "confidence": "HIGH",
    "in_expected_class": true,
    "rationale": "Case hyln_2021 is a category-B trap with expected_classes = [\"hold\"]. The fleet verdict was action=\"hold\" with size 0.0 shares and an explicit \"do not initiate a position, not even a small starter\" rationale — an exact in-class match, and specifically the correct refusal on a trap case (no starter, no partial-credit buy). Per the benchmark rule, an in-class verdict scores 1.0; no falsifier-based or trap-buy partial-credit adjustment applies because there was no buy of any size. Acted-on return: positioned=false and size=0.0, so no capital was exposed and acted_return_pct = 0.0 versus benchmark_return_pct = -86% (answer_key.resolution: 10.61 -> 1.48 over the 3y horizon to 2024-05-31, yfinance closes retrieved 2026-07-11); the fleet avoided the full drawdown. Outcome luck does not alter the class grade, and the favourable -86% realization is noted only for the return computation, not as credit. The stage-4 calibration_reviewer returned output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=4, violations=[] with verification.ok=true; that cleanliness/workflow judgment is accepted as final and not reopened here. Confidence HIGH: the expected class, the recorded action, the resolution prices and the grading rule are all supplied verbatim in the grading input, so no external or stale data was needed. The fleet's own MEDIUM confidence on the underlying call does not cap grading confidence, since the grade depends on the recorded verdict and answer key rather than on live market data.",
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
- notes: Case hyln_2021 is a category-B trap with expected_classes = ["hold"]. The fleet verdict was action="hold" with size 0.0 shares and an explicit "do not initiate a position, not even a small starter" rationale — an exact in-class match, and specifically the correct refusal on a trap case (no starter, no partial-credit buy). Per the benchmark rule, an in-class verdict scores 1.0; no falsifier-based or trap-buy partial-credit adjustment applies because there was no buy of any size. Acted-on return: positioned=false and size=0.0, so no capital was exposed and acted_return_pct = 0.0 versus benchmark_return_pct = -86% (answer_key.resolution: 10.61 -> 1.48 over the 3y horizon to 2024-05-31, yfinance closes retrieved 2026-07-11); the fleet avoided the full drawdown. Outcome luck does not alter the class grade, and the favourable -86% realization is noted only for the return computation, not as credit. The stage-4 calibration_reviewer returned output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=4, violations=[] with verification.ok=true; that cleanliness/workflow judgment is accepted as final and not reopened here. Confidence HIGH: the expected class, the recorded action, the resolution prices and the grading rule are all supplied verbatim in the grading input, so no external or stale data was needed. The fleet's own MEDIUM confidence on the underlying call does not cap grading confidence, since the grade depends on the recorded verdict and answer key rather than on live market data.

### ttcf_t2 — real: TTCF @ 2021-08-16 (~$17.50); Q2-21 miss + guidance cut
- **Fleet reasoning:** sell (HIGH) —
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
        "evidence": "Scanned the full candidate_payload (analyst_reports, constraints_rendered, positions) against the 12-term denylist: zero verbatim or near-verbatim occurrences. Issuer is referred to only as 'PVF' / 'Provida Foods (alias PVF)'. Counterparties are genericized: 'warehouse-club private-label volume', 'one of the largest mass-market retailers', '~1,800 stores of the largest grocery chain', 'two tortilla/food-plant acquisitions', 'the category's pure-play leader'. The listing vehicle is described only as 'the listing structure'. RESIDUAL (non-blocking, no denylisted term used): the combination of the exact '~1,800 stores' door count, 'southwestern/tortilla' product line and 'plant-forward frozen food' is narrowing but does not constitute a concrete leak.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "Verified independently against source_manifest at the declared factor 3.7, using only manifest facts + masked payload. Manifest 2021-08-12: rev $50.7M x3.7 = 187.6 vs payload 188; GAAP net loss -$53.2M x3.7 = -196.8 vs payload -197; valuation allowance $46.0M x3.7 = 170.2 vs payload '~170'; adj-EBITDA guide -$14 to -$17M x3.7 = -51.8 to -62.9 vs payload '-52 to -63'; prior +$2 to +$4M x3.7 = +7.4 to +14.8 vs payload '+7 to +15'. Manifest 2021-03-19: FY rev $148.5M x3.7 = 549.5 vs payload 549. Manifest 2021-05-18: acquisitions ~$37M x3.7 = 136.9 vs payload '~137'. No payload absolute figure matches an unscaled manifest value. Ratios correctly left unscaled and exact: 75%, 45.9%, GM 14.6/15.7, GM guide 16-22 from 20-25. Figures NOT covered by the manifest (market_cap 5300, cash 519, debt 10, share_price 64.75, entry 74.95, FY guide 870-895) cannot be proven from the manifest; they are internally consistent with a uniform 3.7 scaling (5300/880 = 6.0 = stated P/S; 64.75/74.95 = -13.6% = stated -14%; 870/549 = +58.5% = stated '+~59%'), so no unscaled leak is detectable, but their scaling is asserted rather than proven.",
        "verdict": "pass"
      },
      {
        "check": "relative_dates",
        "evidence": "No absolute dates, years, quarters-with-years, or month names appear anywhere in candidate_payload. All temporal anchors are relative: 'FY-1 (the entry-year)', as_of 'current', 'Q0 = Q2 of the current FY, reported ~4 days ago', 'by the end of next quarter', 'for several quarters', 'Entered three quarters ago'. The dated URLs and 2021 publication dates exist only in raw_sources/source_manifest, which are sanitizer-side inputs and are not part of the payload the Trader sees.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Reopening economy; freight and food-input inflation squeezing every food manufacturer; speculative listing-vehicle cohort broadly deflating' — no named macro event, no pandemic/central-bank/index reference, no year. The blank-check merger vehicle is genericized to 'listing-vehicle cohort' and 'warrant exercises/exchanges from the listing structure'; the sector de-rating is described only as 'the plant-based theme has cooled sharply'. RESIDUAL (non-blocking): 'reopening economy' plus 'freight inflation' loosely time-stamps the regime without naming it.",
        "verdict": "pass"
      }
    ],
    "confidence": "HIGH",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Payload is clear to hand to the production Trader. All four protocol dimensions pass. (1) Alias: no denylisted term appears in any field; the issuer, its retail counterparties, its acquisition targets and its listing vehicle are all referred to generically. (2) Absolute-figure rescaling: independently reconstructed from source_manifest only — every manifest-covered dollar figure reproduces the payload value at exactly the declared 3.7 factor (rev, net loss, valuation allowance, both EBITDA guide bands, FY revenue, acquisition consideration), and no payload figure equals an unscaled source value; ratios, growth rates and margins are correctly carried through unscaled and exact. The balance-sheet, market-cap and price figures are not covered by the manifest and so are asserted rather than proven, but they are internally consistent with the same uniform scaling (implied P/S 6.0, implied -14% vs entry, implied +~59% guide growth all reconcile). (3) Relative dates: the payload contains no absolute date, year or dated quarter — every anchor is relative to entry or to the current quarter. (4) Macro genericization: the regime is described only in generic terms with no named event or year. Two residual, non-blocking re-identification risks worth noting to the harness owner rather than the Trader: the verbatim '~1,800 stores' door count carried across from source, and the 'southwestern/tortilla product line' descriptor, together narrow the candidate set even though neither uses a forbidden term. Recommend the harness consider banding the store count on future packets."
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
      "SIZING RECONCILED: packet positions '~1% of portfolio at entry ... entry at $74.95/share' + constraint '1% = $10,000' => 10,000/74.95 = 133.4 -> output size_shares_or_currency 133.0, size_units 'shares'.",
      "CASH DELTA EXACT: 133 x $64.75 (packet share_price_usd 64.75) = $8,611.75 -> output 'cash_delta': '+$8.6K (133 shares x $64.75)'.",
      "LOSS EXACT: basis 133 x $74.95 = $9,968.35 vs proceeds $8,611.75 = -$1,356.60 -> output 'Realized loss of ~-$1.4K against a ~$10.0K cost basis (entry $74.95)'.",
      "CONCENTRATION EXACT: $8,611.75 / $1,000,000 NAV = 0.861% -> output 'PVF ~0.86% of NAV ... -> 0%; frees ~0.9pp of the ~5% sleeve cap'.",
      "MARGIN CLAIM GROUNDED: packet latest_quarter.gross_margin_pct 15.7 and news/PVF entry milestone '(b) gross-margin expansion toward 20%+' -> rationale 'gross margin was 15.7% against a 20%-plus target'.",
      "GUIDANCE CLAIM GROUNDED: packet guidance 'adj_ebitda_usd_m: CUT to -52 to -63, from +7 to +15 — the year flips from profitable to a meaningful loss' -> rationale 'guidance was just cut, flipping the year from a small profit to a sizeable loss'; same figures reused in falsifier #3 and in revisit trigger 'fy_adjusted_ebitda_guidance_usd_m >= 0'.",
      "NON-CASH ITEM CORRECTLY SEPARATED: packet net_loss_note 'includes a one-time NON-CASH ~170 deferred-tax valuation allowance ... ex-item operating loss ~-27' -> rationale 'Ignore the huge headline loss — that is mostly a non-cash tax write-off; the operating loss is the real issue.'",
      "CONCENTRATION CLAIM GROUNDED: packet customer_concentration top_2 71%, split [39,32] and news/PVF 'Customer concentration has NOT improved yet (top-2 still ~71%)' -> rationale 'Customer concentration is unchanged at ~71%'; falsifier #2 uses the same 71% / 39-32 baseline.",
      "DILUTION CLAIM GROUNDED: packet balance_sheet.share_count_note 'share count is ~30% higher than at entry' -> rationale '~30% more shares outstanding, we would be paying more for the whole company than at entry' (cap math, not price action — satisfies the EXIT DISCIPLINE directive).",
      "CLOCK DERIVED FROM PACKET: latest_quarter.which 'Q0 = Q2 of the current FY, reported ~4 days ago' -> next_validation_point 'Q2 was reported ~4 days ago, so the next print is estimated ~2-3 months out'.",
      "HORIZON DERIVED FROM PACKET QUOTE: news/PVF 'marketing spend to win the new shelf space will run heavy for several quarters' -> rerating_horizon 'the earliest credible evidence of gross margin sustainably above 20% at scale is 4-6 quarters away', with explicit unestimability statement as the reporting requirement demands.",
      "CITATION INTEGRITY: cited_sources ['fundamentals/PVF','news/PVF'] match report_name fields verbatim; the four real-world URLs in the sources block (Tattooed Chef / Target / Kroger / Karsten PRs) are neither cited nor referenced, and no denylisted proper noun appears in the output.",
      "SANITIZER SCALING SPOT-CHECK REPRODUCED: source 2021-08-12 revenue $50.7M x3.7 = 187.6 vs packet 188; net loss -$53.2M x3.7 = -196.8 vs packet -197; allowance $46.0M x3.7 = 170.2 vs packet ~170; source 2021-03-19 $148.5M x3.7 = 549.5 vs packet 549; source 2021-05-18 ~$37M x3.7 = 136.9 vs packet ~137.",
      "UNGROUNDED ELEMENT (the deduction): 'roughly Nov 2026' and revisit_triggers[1].date '2026-11-30' have no antecedent in the packet, which the sanitizer confirms contains 'no absolute dates, years, quarters-with-years, or month names'."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "INDEPENDENT REPLAY AUDIT — case ttcf_t2 (freeze 2021-08-16), masked packet \"PVF\".\n\nVERDICT-NEUTRAL: I do not grade whether SELL was the right investment call; only cleanliness, fidelity, workflow, and grounding of the visible rationale.\n\n1) OUTPUT CLEANLINESS — PASS. `response_raw` is exactly one JSON object, no prose/headings/code fences, and parses to `output_full` without discrepancy (null-normalized keys in revisit_triggers only). No real-world identifiers leak: the raw `sources` block names Tattooed Chef, Target, Kroger, New Mexico Food Distributors and Karsten Tortilla Factory, and none of those strings — nor any 2021 date, nor the freeze date — appear anywhere in the trader output. The trader cited only `fundamentals/PVF` and `news/PVF`, both of which appear verbatim as `report_name` values in the packet; no fabricated or paraphrased source_id. This is consistent with the recorded `output_audit` (contaminated=false, contamination_hits=[]). One era artifact: the output anchors to calendar year 2026 (\"roughly Nov 2026\", revisit trigger date 2026-11-30), a year absent from the packet and presumably inherited from the runtime clock. It confers no look-ahead knowledge of the 2021 issuer, so I do not treat it as contamination, but it is an unsupported absolute anchor (see §4).\n\n2) PACKET / CONSTRAINT FIDELITY — PASS. Every arithmetic step reconciles to the packet and the $1,000,000 NAV anchor:\n- Slot: \"~1% of portfolio at entry\" at $74.95 => $10,000/74.95 = 133.4 -> 133 shares. Cost basis 133 x 74.95 = $9,968 ≈ the stated \"~$10.0K\".\n- Proceeds: 133 x $64.75 = $8,611.75 => stated \"+$8.6K\". Exact.\n- Realized loss: 8,611.75 - 9,968.35 = -$1,356.60 => stated \"~-$1.4K\". Exact.\n- Concentration: 8,611.75 / 1,000,000 = 0.861% => stated \"~0.86% -> 0%\", \"frees ~0.9pp\" of the ~5% sleeve cap. Exact.\n- Sizing is expressed in `size_units: shares` (133), so the constraint that a currency-denominated size must be a true USD notional and never a percentage-in-a-currency-field is not violated; `cash_delta` is likewise a real notional.\n- `instrument: stock`, no options, no leverage, no margin — matches the mandate.\nEXIT DISCIPLINE: the client directive forbids exiting on price alone and sanctions reduction only when (a) a recorded thesis falsifier fires or (b) the sleeve breaches cap. The position is DOWN -14%, so the \"sold after it doubled\" scar pattern is structurally inapplicable, and the written rationale grounds the exit exclusively in broken thesis legs (margin trajectory, unchanged concentration, guidance flip) plus cap-math dilution — not in price action. The one price-adjacent sentence (\"~30% more shares outstanding, we would be paying more for the whole company than at entry\") is an enterprise-value/cap-math argument, which the directive explicitly endorses as the re-derivation frame. No breach.\n\n3) WORKFLOW CORRECTNESS — PASS. The fleet REPORTING REQUIREMENT is fully discharged: (1) FALSIFIERS are present and, correctly for a SELL verdict, are inverted into PAUSE-falsifiers that would invalidate the recommendation — each carries a named metric, a source attribution, an as-of, a current baseline, a threshold, a persistence condition, and a typed decision_transition. The fourth is explicitly declared qualitative with an honest reason (\"no segment-level margin line is disclosed today\"), rather than inventing a threshold. (2) THE CLOCK is present: next validation point = the Q3 print, correctly derived from \"Q0 = Q2 ... reported ~4 days ago\" to \"~2-3 months out\", with three named checkable items. (3) The re-rating horizon is given as a band (\"18-24 months at best\") AND the requirement that genuine unestimability be stated explicitly is met head-on (\"the band cannot be tightened — that unestimability is itself part of why conviction ... is gone\"). Machine-readable `revisit_triggers` mirror the prose falsifiers with consistent transitions. Missing-fact handling is honest: no domain_knowledge tax file was supplied and the trader refuses to assert a rate rather than fabricating one.\n\n4) REASONING GROUNDED (visible rationale only — I make no claim to hidden chain-of-thought) — 3/4. Essentially every load-bearing assertion traces to a packet field: GM 15.7% vs the entry thesis's 20%+ milestone; prior-FY 14.6%; the GM guide cut to 16-22 from 20-25; the adj-EBITDA flip to -52/-63 from +7/+15; the correct and non-obvious separation of the -197 GAAP headline into a ~170 non-cash valuation allowance versus the ~-27 ex-item operating loss; top-2 concentration still 71% (39/32); ~30% higher share count; ~$137M of cash acquisitions; the 12,000-door promise; and the \"several quarters\" marketing quote used to derive the 4-6 quarter margin-proof window. The single gap: the Nov / 2026-11-30 calendar anchor is not derivable from the packet, which contains only relative time markers — it is self-flagged as estimated (\"exact date not supplied\"), which mitigates but does not remove the gap. Hence 3 (grounded with minor gaps) rather than 4.\n\nMINOR, NON-BLOCKING OBSERVATIONS (listed in `violations` for the record; none rise to a failed dimension): (i) the 2026 calendar anchor above; (ii) \"No capital-gains tax due on a loss\" is a regulatory-flavored statement carried without a domain_knowledge path, though the trader immediately disclaims it and defers to the standing tax rules; (iii) confidence=HIGH is retained despite the acknowledged missing tax file — defensible, since the tax rule is peripheral to a loss-sale verdict and the packet's own analyst reports are marked HIGH, but no domain-refresh referral was raised.\n\nSANITIZER STAGE (stage 2) is internally consistent with the packet: I re-derived the 3.7 scaling spot-checks it claims (50.7 x 3.7 = 187.6 ≈ 188; -53.2 x 3.7 = -196.8 ≈ -197; 46.0 x 3.7 = 170.2 ≈ 170; 148.5 x 3.7 = 549.5 ≈ 549; 37 x 3.7 = 136.9 ≈ 137) and confirm no payload figure equals an unscaled source value, and that its self-declared unproven set (market cap, cash, prices) reconciles internally (5300/880 = 6.0 P/S; 64.75/74.95 = -13.6% ≈ -14%). Its residual re-identification flags (verbatim \"~1,800 stores\", \"southwestern/tortilla\") are correctly routed to the harness owner, and the trader output does not repeat the 1,800 figure.",
    "violations": [
      "MINOR — unsupported calendar anchor: output states 'roughly Nov 2026' and emits revisit trigger date '2026-11-30'; the packet contains no absolute dates or years (all anchors relative: 'reported ~4 days ago', 'Entered three quarters ago'), so the year is inherited from the runtime clock rather than packet facts. Self-flagged as estimated; no look-ahead advantage, so not treated as contamination.",
      "MINOR — uncited regulatory statement: 'No capital-gains tax due on a loss' is a tax-rule claim carried without a domain_knowledge/... path, per the citation rule. Substantially mitigated: the trader explicitly states no domain_knowledge tax file was supplied, makes no rate claim, and defers offset treatment to the standing tax rules.",
      "MINOR — confidence=HIGH retained despite an acknowledged missing input (no tax-rules file) and no domain-refresh referral raised. Defensible because the tax rule is peripheral to a loss-sale verdict and both packet analyst reports are themselves marked HIGH, but the referral step was skipped."
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
    "rationale": "Case ttcf_t2, category C, grading rule \"exit\". answer_key.expected_classes = [\"sell\"]; fleet_result.action = \"sell\" (full slot, 133.0 shares) — a direct in-class match, so the benchmark rule awards full credit 1.0. No partial-credit condition applies: this is not an F1-pass-with-falsifiers case and not a trap-buy-with-killing-falsifier case, so the 0.5 band is not warranted. Return computation per answer_key.resolution: the ride-to-zero benchmark is price_at_freeze_real 17.5 -> price_at_horizon_real 0.0 = benchmark_return_pct -100 (terminal, Chapter 11 2023-07, equity ~zero). The fleet exited the entire position at the freeze, so the acted-on position carries no exposure past the freeze price: acted_return_pct = 0, i.e. the full -100 drawdown was avoided. Outcome luck is not a grading input here and does not alter the class grade — the verdict scores 1.0 on class match alone, and the favorable resolution is reported, not rewarded. Per instruction, the stage-4 calibration_reviewer's judgment is not reopened: it recorded output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, confidence HIGH, verification.ok=true with no failures, and its three logged items (2026 calendar anchor, uncited \"no CGT on a loss\" statement, HIGH confidence retained without a domain-refresh referral) are all classified MINOR and non-blocking, so none of them downgrade the class grade. Grading confidence HIGH: the class match, the grading rule, and both resolution anchors are supplied verbatim in the grading input and require no external or stale data.",
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
- notes: Case ttcf_t2, category C, grading rule "exit". answer_key.expected_classes = ["sell"]; fleet_result.action = "sell" (full slot, 133.0 shares) — a direct in-class match, so the benchmark rule awards full credit 1.0. No partial-credit condition applies: this is not an F1-pass-with-falsifiers case and not a trap-buy-with-killing-falsifier case, so the 0.5 band is not warranted. Return computation per answer_key.resolution: the ride-to-zero benchmark is price_at_freeze_real 17.5 -> price_at_horizon_real 0.0 = benchmark_return_pct -100 (terminal, Chapter 11 2023-07, equity ~zero). The fleet exited the entire position at the freeze, so the acted-on position carries no exposure past the freeze price: acted_return_pct = 0, i.e. the full -100 drawdown was avoided. Outcome luck is not a grading input here and does not alter the class grade — the verdict scores 1.0 on class match alone, and the favorable resolution is reported, not rewarded. Per instruction, the stage-4 calibration_reviewer's judgment is not reopened: it recorded output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, confidence HIGH, verification.ok=true with no failures, and its three logged items (2026 calendar anchor, uncited "no CGT on a loss" statement, HIGH confidence retained without a domain-refresh referral) are all classified MINOR and non-blocking, so none of them downgrade the class grade. Grading confidence HIGH: the class match, the grading rule, and both resolution anchors are supplied verbatim in the grading input and require no external or stale data.

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
        "evidence": "Alias 'NLF' is used consistently at every level of the payload (top-level alias, candidate_payload.alias, report_name 'fundamentals/NLF' and 'news/NLF', positions text 'No existing position in NLF'). The only entity name present, 'Nexus Lattice Fabric (alias NLF)', is self-declared fictional by candidate_payload.constraints_rendered ('this is a FULLY FICTIONAL benchmark control. The company, alias, events... were invented'). No ticker symbol, exchange listing, CIK/ISIN, executive name, customer name, or geographic HQ appears; customers are described only as 'regional chemical and pulp customers; neither is a household name'. forbidden_terms is empty, so there is no denylist entry to match; no denylist term was introduced by me. No de-anonymizing detail found.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "case flag synthetic=true and rescale_factor=null; raw_sources=[] and source_manifest=[] are both empty, so there is no frozen-packet original figure set against which a scaling factor could exist or be recovered. Per protocol, rescaling is not_applicable for synthetic cases and internal consistency was verified instead, using only arithmetic on candidate_payload fields: (a) revenue_growth_pct checks out — 540/420-1 = 28.6% and 720/540-1 = 33.3%, matching the stated FY-2 and FY-1 values; (b) ps_trailing — 2800/720 = 3.89 vs stated 3.9; (c) ev_s_trailing — (2800 + 90 - 410)/720 = 3.44 vs stated 3.4; (d) share count implied by market_cap_usd_m 2800 / share_price_usd 18.4 = ~152m shares, a coherent magnitude; (e) net income trajectory -35 -> -12 -> +28 is consistent with latest_quarter gaap_net_income_usd_m 14 and the narrative 'first full year of GAAP profit'. One residual tension, flagged not failed: pe_trailing 55 does not reconcile to FY-1 net income alone (2800/28 = 100x); it only reconciles on a trailing-twelve-month basis (implied TTM net income ~51m, i.e. the loss-making early quarter of FY-1 rolling off and Q0's 14m rolling on). That reading is plausible given the stated profitability inflection but is not stated explicitly in the payload. Also minor: Q0 revenue 205 vs +31% YoY implies a year-ago quarter of ~157, which places it early in FY-1 and leaves an unusually flat sequential step from an implied FY-1 exit quarter. Neither item is a de-anonymization risk.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "All time references are relative or anchor-free. Fiscal periods are labelled 'FY-3', 'FY-2', 'FY-1'; the quarter is 'Q0 just reported'; as_of is 'current'. No calendar year, month name, quarter-with-year (e.g. 'Q3 FY2024'), earnings date, or ISO date appears anywhere in candidate_payload. Duration language is purely relative and non-anchoring: 'a third straight quarter', 'first full year', 'multi-year contracts', 'prior cycle', '5+ year intended hold', and the reporting-requirement horizon examples '3-6 months' / '2-3 years'. The session date in the operating context was not injected into the payload and no absolute anchor can be derived from the payload alone.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "The only macro statement is news report macro_context: 'Industrial capex steady; software multiples compressed vs prior cycle.' — a regime description with no named event, no policy body, no crisis label, and no date. Related market language is likewise genericized: valuation.price_action reads 'quiet grind higher off a forgotten ordinary listing; -40% from a prior overshoot, now consolidating' (listing and drawdown referenced without era, venue, or timing), and constraints_rendered mentions 'one large concentrated technology position being glided down per plan' without naming it. The competitive risk is generic ('a larger horizontal ERP vendor could bundle a good enough module') and names no vendor. No pandemic, rate-cycle, election, war, or index-event tell that would fix the packet to a period.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audit of the exact candidate payload across all four protocol dimensions: PASS on aliasing, relative dates, and macro-event genericization; absolute-figure rescaling is not_applicable because synthetic=true with rescale_factor=null and both raw_sources and source_manifest empty, so no original figure set exists to prove scaling against. In lieu of rescaling I verified internal consistency arithmetically from payload fields only: revenue growth rates (28.6%, 33.3%), P/S (3.89 vs 3.9 stated), EV/S (3.44 vs 3.4 stated), and the implied ~152m share count all reconcile. Two non-blocking tensions are recorded for the record: pe_trailing 55 reconciles only on a TTM net-income reading (~51m implied), not against FY-1 net income of 28m (which would be ~100x); and Q0's +31% YoY implies a year-ago comparable early in FY-1, leaving a flat implied sequential step. Neither is a de-anonymization vector. No concrete leak found: no ticker, no real entity name, no customer or executive identity, no calendar anchor, no named macro event. forbidden_terms was supplied empty and was treated strictly as a denylist — no term from it was introduced into this output, and leaked_terms is correspondingly empty. Per instruction, no attempt was made to identify a company or to infer the eventual outcome; the payload's own provenance note asserts no real-world original exists. Confidence MEDIUM rather than HIGH: aliasing, date, and macro checks are fully verifiable from the masked payload text itself, but with an empty source_manifest I cannot independently corroborate the synthetic=true flag from a frozen packet — I am relying on the case flag and the payload's self-declared provenance note. Recommendation: verdict safe_to_run=true; if the harness intends the manifest to be non-empty for this case, the domain-refresh agent should confirm the empty raw_sources/source_manifest is intentional for synthetic controls rather than a packet-assembly gap."
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
      "packet fundamentals/NLF share_price_usd=18.4 → output cash_delta '-$20,000 (≈1,087 shares at $18.40)'; 20000/18.4 = 1086.96, arithmetic confirmed",
      "constraints_rendered 'BENCHMARK SIZING ANCHOR: use a fictional total portfolio NAV of $1,000,000... 2% = $20,000' → size_shares_or_currency=20000.0 with size_units='currency', satisfying 'must be an actual USD notional, never a percentage encoded in a currency field'",
      "constraints_rendered 'high-potential sleeve (up to ~5% of portfolio total, ~1-2% per position)' + packet positions 'Budget for a new high-potential-sleeve position: ~1-2% of portfolio' → concentration_delta 'NLF 0% → ~2.0%... sleeve moves to ~2% of the ~5% cap'; at the top of the permitted band, not over it",
      "packet latest_quarter net_revenue_retention_pct=122, note 'NRR >120% for a third straight quarter' → falsifier 1 'currently 122%, above 120% for three straight quarters', threshold <110% for two consecutive quarters or <100% single print",
      "packet FY-1 gross_margin_pct=74.5 + news/NLF 'Gross margin still expanding as hosted delivery mix rises' → falsifier 2 'currently 74.5% and still expanding as hosted delivery mix rises', threshold <70%",
      "packet FY-1 free_cash_flow_usd_m=55, net_income_usd_m=28, latest_quarter gaap_net_income_usd_m=14 → falsifier 3 reproduces '+$14M', '+$28M', '+$55M' exactly",
      "packet balance_sheet cash_and_equivalents_usd_m=410 vs total_debt_usd_m=90 → rationale 'more cash than debt'",
      "packet latest_quarter revenue_growth_yoy_pct=31 and FY-1 revenue_growth_pct=33.3 → falsifier 4 'currently +31% YoY, FY-1 +33.3%'",
      "news/NLF bear case 'a larger horizontal ERP vendor could bundle a good enough module' → rationale 'a large enterprise-software vendor bundles a good enough version and squeezes pricing', with gross margin named as the detection instrument",
      "packet report_name fields 'fundamentals/NLF' and 'news/NLF' → cited_sources matches both verbatim; no source id outside the packet is cited",
      "constraints_rendered 'A hard-to-estimate horizon is NOT optional to report: if it is genuinely unestimable, say so explicitly — that itself lowers conviction' → rerating_horizon '2-4 years... The exact timing of any re-rating is genuinely unestimable, and that uncertainty is why conviction is MEDIUM and size is capped at the sleeve band'",
      "sanitizer output safe_to_run=true, leaked_terms=[], verification {failures: [], ok: true}; replay output_audit contaminated=false, contamination_hits=[], suspect_terms=[]",
      "COUNTER-EVIDENCE (defect): sanitizer relative_dates evidence 'No calendar year, month name, quarter-with-year... or ISO date appears anywhere in candidate_payload' vs output next_validation_point '(~Nov-Dec 2026)' and output_audit calendar_years_in_output=['2026']",
      "COUNTER-EVIDENCE (defect): packet valuation {ev_s_trailing: 3.4, ps_trailing: 3.9} vs rationale_summary 'priced near 3.4 times annual sales'",
      "NON-PROPAGATION CHECK: sanitizer flagged pe_trailing=55 as reconciling only on a TTM basis (2800/28 = ~100x against FY-1 net income); the output never cites P/E, so the incoherent packet metric was not carried forward"
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 3,
    "summary": "INDEPENDENT REPLAY AUDIT — case nlf_synthetic (stage-2 sanitizer PASS/safe_to_run=true, verification.ok=true). I audited only the persisted packet, constraints_rendered, and the replayed output; I make no judgment on whether BUY was the right call and I claim no access to hidden reasoning — reasoning_grounded_score grades the written rationale only.\n\nOUTPUT CLEANLINESS — PASS. response_raw is exactly one well-formed JSON object with no prose, headings, or code fences, and it deserializes cleanly into output_full with no field drift. The harness contamination scan reports contaminated=false, contamination_hits=[], suspect_terms=[]. No forward-looking/outcome knowledge, no real-company identifiers, no ticker beyond the alias NLF, no leaked denylist term (sanitizer leaked_terms=[]). Cited_sources are exactly the two report_name strings present verbatim in the packet (\"fundamentals/NLF\", \"news/NLF\") — no invented or paraphrased ids.\n\nPACKET FIDELITY — PASS on every quoted figure; four defects noted, none a misquote. Verified against packet: share_price_usd 18.4 → \"$18.40\"; latest_quarter.revenue_growth_yoy_pct 31 → \"~31% a year\"; net_revenue_retention_pct 122 → \"existing customers spending ~22% more each year\"; gaap_net_income_usd_m 14 and FY-1 net_income_usd_m 28 → \"already genuinely profitable\"; cash 410 vs total_debt 90 → \"more cash than debt\"; gross_margin_pct 74.5 and free_cash_flow_usd_m 55 reproduced exactly in falsifiers 2 and 3; the ERP-bundling bear case is lifted correctly from news/NLF. Constraint arithmetic checks out: $20,000 = 2.0% of the mandated $1,000,000 benchmark NAV, size_units=\"currency\" carries a true USD notional (not a percentage smuggled into a currency field, as the anchor explicitly forbids), and 20000/18.4 = 1086.96 ≈ the stated \"≈1,087 shares\". Sleeve discipline holds — 2% is the top of the \"~1-2% per position\" band and 2% of the \"~5%\" sleeve cap, matching the stated concentration_delta. instrument=stock, no leverage, no options, order_type=market, TIF=GTC: all within mandate. Notably the output does NOT propagate the one internally-incoherent packet metric the sanitizer flagged (pe_trailing 55 reconciles only on a TTM basis, not against FY-1 net income of 28) — P/E is never invoked.\n\nWORKFLOW — PASS. The fleet REPORTING REQUIREMENT is satisfied in the structured-schema equivalent of the \"**Recommendation:**\" section: (1) FALSIFIERS — four checkable conditions, each stated with metric, source attribution, as-of label, current baseline, threshold, persistence rule, and an explicit decision_transition (three BUY->EXIT, one ADD->HOLD); (2) THE CLOCK — next_validation_point names a concrete testable event (the first quarterly print after Q0) with the three specific metrics to be checked, and rerating_horizon gives an honest band (\"2-4 years\") while explicitly declaring the exact re-rating timing \"genuinely unestimable\" and tying that admission to the MEDIUM conviction and to capping size at the sleeve band. That is precisely the behavior the constraint demanded of a hard-to-estimate horizon. revisit_triggers are schema-valid and non-redundant with the falsifier list. Confidence band reported (MEDIUM), defensible for a single-packet, self-contained synthetic control.\n\nREASONING GROUNDING — 3/4 (grounded with minor gaps). Nearly every visible inferential step is traceable to a packet field, and the falsifiers uniquely do the hard work of naming baseline, source, and direction rather than gesturing. The thesis chain (retention >120% three straight quarters + GAAP inflection + net cash → compounder; boring niche + no coverage → unre-rated) maps cleanly onto news/NLF's recent_developments and the fundamentals valuation block, and the rationale correctly designates retention and gross margin — not price — as the monitored variables, which is the honest instrumentation of its own bear case. Deductions for: (a) an absolute calendar anchor not present anywhere in the packet; (b) a metric-label mismatch on the headline valuation claim; (c) an unexplained price threshold; (d) an uncited tax assertion. The falsifier thresholds (110/100/70/15) are analyst judgment rather than packet-derived, but each is stated explicitly relative to a quoted packet baseline, so I treat them as disclosed judgment, not as ungrounded fabrication.\n\nNET: clean, faithful, workflow-complete replay with four minor grounding/citation defects, the calendar-anchor injection being the only one that materially breaches the packet's deliberately date-blind design.",
    "violations": [
      "CALENDAR ANCHOR INJECTED (most material): next_validation_point states 'estimated 2-3 months out (~Nov-Dec 2026)'. The packet is deliberately date-blind — as_of='current', latest_quarter.which='Q0 just reported', fiscal periods labelled FY-3/FY-2/FY-1 — and the sanitizer's relative_dates check confirmed 'no calendar year, month name... appears anywhere in candidate_payload'. The year 2026 is not derivable from any packet fact and matches the replay session date (2026-08-31), i.e. it comes from the harness clock, not the evidence. The harness scan logged calendar_years_in_output=['2026'] yet returned contaminated=false; flagging it here as a fidelity defect regardless. The '2-3 months out' spacing is itself an assumption — the packet never states a reporting cadence or fiscal calendar.",
      "METRIC LABEL MISMATCH: rationale_summary says NLF is 'priced near 3.4 times annual sales'. 3.4 is packet valuation.ev_s_trailing (enterprise value / sales); the price-to-sales figure in the same block is ps_trailing 3.9. Both figures exist in the packet, but the user-facing sentence attaches the EV-based multiple to a price-based description. Understates the equity multiple by ~13% in the one number a lay reader is most likely to anchor on.",
      "UNGROUNDED PRICE THRESHOLD: revisit_triggers[1] sets a HOLD->ADD prompt at price_below $14.00 (~24% below the packet's share_price_usd 18.4). No derivation is offered — no support level, no valuation floor, no multiple-based fair-value anchor, and nothing in valuation.price_action ('-40% from a prior overshoot, now consolidating') yields 14. Unlike the four falsifiers, this trigger carries no source attribution or baseline.",
      "UNCITED TAX CLAIM: expected_impact.tax_estimate asserts 'No tax event on purchase; capital-gains tax applies only on a future sale' with no domain_knowledge/... path cited. Fleet rule requires tax/regulatory claims to cite the authorizing domain-knowledge file even when the proposition is generic; as written it is an uncited regulatory assertion."
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
    "rationale": "Case nlf_synthetic, grading rule \"synthetic_winner\". answer_key.expected_classes = [\"buy\"]; fleet_result.action = \"buy\" — exact in-class match, so the benchmark rule assigns 1.0. No partial-credit condition applies: this is not an F1/hold verdict carried by falsifiers and not a trap buy with a killing falsifier, so the 0.5 band is not reachable here. Sizing is within the stated mandate and does not affect the class grade: size=20000.0 with size_units=\"currency\" equals 2.0% of the constraints_rendered benchmark NAV of $1,000,000, at the top of the \"~1-2% per position\" high-potential-sleeve band and inside the \"~5%\" sleeve cap (per the recorded constraints_rendered and packet \"positions\" block quoted in the review's grounding_evidence). Return computation is not possible: answer_key.resolution is null and no price path or benchmark series is supplied, so acted_return_pct and benchmark_return_pct are reported as null rather than estimated (fleet rule 4 — do not fabricate a missing fact). Per instruction, outcome luck is irrelevant to the class grade in any event. I do not reopen the stage-4 calibration_reviewer's judgment: output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with no failures. The four logged violations (calendar anchor \"~Nov-Dec 2026\" injected into a date-blind packet; ev_s_trailing 3.4 labelled as price-to-sales where packet ps_trailing=3.9; underived $14.00 revisit trigger vs packet share_price_usd=18.4; uncited tax assertion lacking a domain_knowledge/... path) are grounding and citation defects flagged inside a PASS review; under the recorded rule they do not demote an in-class verdict, and they are surfaced here for the audit agent rather than netted against the score. Confidence HIGH: the class determination rests entirely on primary records supplied in the grading input (answer_key.expected_classes, fleet_result.action, grading=\"synthetic_winner\") — no external or stale data is involved. Sources cited are the supplied grading-input records only; no source_id outside the input block is invoked.",
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
- notes: Case nlf_synthetic, grading rule "synthetic_winner". answer_key.expected_classes = ["buy"]; fleet_result.action = "buy" — exact in-class match, so the benchmark rule assigns 1.0. No partial-credit condition applies: this is not an F1/hold verdict carried by falsifiers and not a trap buy with a killing falsifier, so the 0.5 band is not reachable here. Sizing is within the stated mandate and does not affect the class grade: size=20000.0 with size_units="currency" equals 2.0% of the constraints_rendered benchmark NAV of $1,000,000, at the top of the "~1-2% per position" high-potential-sleeve band and inside the "~5%" sleeve cap (per the recorded constraints_rendered and packet "positions" block quoted in the review's grounding_evidence). Return computation is not possible: answer_key.resolution is null and no price path or benchmark series is supplied, so acted_return_pct and benchmark_return_pct are reported as null rather than estimated (fleet rule 4 — do not fabricate a missing fact). Per instruction, outcome luck is irrelevant to the class grade in any event. I do not reopen the stage-4 calibration_reviewer's judgment: output_clean=true, packet_fidelity=true, workflow_correct=true, reasoning_grounded_score=3/4, verification.ok=true with no failures. The four logged violations (calendar anchor "~Nov-Dec 2026" injected into a date-blind packet; ev_s_trailing 3.4 labelled as price-to-sales where packet ps_trailing=3.9; underived $14.00 revisit trigger vs packet share_price_usd=18.4; uncited tax assertion lacking a domain_knowledge/... path) are grounding and citation defects flagged inside a PASS review; under the recorded rule they do not demote an in-class verdict, and they are surfaced here for the audit agent rather than netted against the score. Confidence HIGH: the class determination rests entirely on primary records supplied in the grading input (answer_key.expected_classes, fleet_result.action, grading="synthetic_winner") — no external or stale data is involved. Sources cited are the supplied grading-input records only; no source_id outside the input block is invoked.

### omk_synthetic — real: SYNTHETIC trap-shape (no real company) — meal-kit / D2C growth theater
- **Fleet reasoning:** hold (MEDIUM) —
  - falsifiers: NOT RECORDED
  - clock: NOT STATED
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
        "evidence": "Subject is named only as alias \"OMK\" throughout: top-level alias=\"OMK\", candidate_payload.alias=\"OMK\", company=\"Orbit Meal Kits (alias OMK)\", report_name \"fundamentals/OMK\" and \"news/OMK\", positions text \"No existing position in OMK\". forbidden_terms is an empty list, so no denylisted string could be matched; no real issuer name, ticker, exchange, domicile, founder/executive name (only the generic role 'CMO'), or product brand appears. Descriptors are category-level only ('direct-to-consumer prepared-meal and grocery-bundle subscription', 'urban professionals'). No leak.",
        "verdict": "pass"
      },
      {
        "check": "absolute_figure_rescaling",
        "evidence": "synthetic=true, rescale_factor=null, raw_sources=[] and source_manifest=[] — there are no real-world originals and no independent manifest from which scaling could be proven or disproven, so per protocol this dimension is not_applicable rather than unverifiable-by-omission. Internal consistency verified instead, from candidate_payload only: revenue 890 -> 1180 = +32.58% vs stated revenue_growth_pct 32.6 (match); 1180 -> 1260 = +6.78% vs stated 6.8 (match); ps_trailing 2.9 vs market_cap_usd_m 3600 / revenue_usd_m 1260 = 2.86 (match at 1dp rounding); ev_s_trailing 3.0 vs (3600 + total_debt 620 - cash 480) / 1260 = 2.97 (match); gross_margin_pct 34 -> 31.5 -> 28.2 = 580 bps cumulative vs narrative '~600 bps' (consistent); cash_and_equivalents 480 vs FY-1 free_cash_flow -290 supports balance_sheet note '<2 years of current burn'; share_price_usd 22.7 vs market_cap 3600 implies ~158.6M shares (plausible, reveals no real float). Latest quarter revenue 310 at +4.5% yoy sits below the FY-0 full-year growth of 6.8%, consistent with the stated sequential deceleration. Only defect: news bullet says growth fell 'from +63% to +7%' while the statements read 62 and 6.8 — narrative rounding, non-material, no rescaling signal.",
        "verdict": "not_applicable"
      },
      {
        "check": "relative_dates",
        "evidence": "Every temporal reference is relative or unanchored: fy labels 'FY-2', 'FY-1', 'FY-0 (just ended)'; latest_quarter.which 'Q0 just reported'; as_of 'current'; 'Rebranded last year'; 'collapsed ... in two years'; price_action '+40% YTD' and '-65% from listing-era high'; macro 'still in force'. No calendar year, month, quarter-with-year, fiscal-year-end date, listing date, or dated filing/event appears anywhere in candidate_payload, so the packet cannot be pinned to a wall-clock period. The reporting requirement's horizon examples ('3-6 months', '2-3 years') are duration bands, not anchors. No leak.",
        "verdict": "pass"
      },
      {
        "check": "macro_event_genericization",
        "evidence": "macro_context reads 'Consumer subscription fatigue; paid-social CAC inflation; growth-stock de-rating still in force' — regime descriptions with no named central-bank action, rate level, pandemic, war, election, index drawdown percentage, or year. Supporting bullets stay firm-specific ('discounting and shipping inflation', 'a short note questioned the LTV math', 'rebrand does not fix churn'), and 'listing-era high' references the subject's own price history rather than a datable market event. Nothing here narrows the window to an identifiable macro episode.",
        "verdict": "pass"
      }
    ],
    "confidence": "MEDIUM",
    "leaked_terms": [],
    "safe_to_run": true,
    "summary": "Audit of case_id omk_synthetic (synthetic=true, forbidden_terms empty, raw_sources/source_manifest empty, rescale_factor null). No concrete leak found: the payload refers to the subject only as \"OMK\" / \"Orbit Meal Kits (alias OMK)\" and carries an explicit fictional-control provenance note. All period references are relative (FY-2 / FY-1 / FY-0 just ended / Q0 just reported / as_of \"current\" / \"last year\" / \"in two years\" / YTD / \"listing-era high\") with no absolute calendar dates, quarters-with-years, or datable event anchors. Macro framing (\"consumer subscription fatigue; paid-social CAC inflation; growth-stock de-rating still in force\") is generic and names no dated policy action, crisis, or year. Absolute-figure rescaling is not_applicable per protocol because synthetic=true and the manifest is empty; in its place I verified internal consistency and the figures reconcile: 1180/890-1 = 32.6% matches revenue_growth_pct 32.6; 1260/1180-1 = 6.8% matches 6.8; market_cap 3600 / revenue 1260 = 2.86 ≈ ps_trailing 2.9; EV = 3600 + 620 - 480 = 3740, 3740/1260 = 2.97 ≈ ev_s_trailing 3.0; gross margin 34.0 -> 28.2 = 580 bps, consistent with the \"~600 bps\" narrative; cash 480 against FY-1 FCF -290 supports \"<2 years of current burn\"; share_price 22.7 against market_cap 3600 implies ~158.6M shares, plausible and unpinned. One cosmetic narrative rounding gap: the news report says growth \"collapsed from +63% to +7%\" while the statements carry 62 and 6.8 — a rounding/paraphrase artifact, not a leak and not a consistency failure. Sizing anchor is a stated fictional $1,000,000 NAV with explicit USD notional mapping (0.5% = $5,000, 1% = $10,000, 2% = $20,000), so no percentage is smuggled into a currency field. Safe to run. Per fleet rules I note that alias-collision screening against any external registry could not be performed (no tools, empty manifest); if the fleet wants positive proof that \"Orbit Meal Kits\" collides with no real issuer, that is a job for the domain-refresh agent."
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
      "Rationale 'sales growth fell from 62% to under 7% in two years' maps to packet annual_income_statement: FY-2 revenue_growth_pct 62 and FY-0 revenue_growth_pct 6.8.",
      "Rationale 'gross margin slid from 34% to 27.5%' maps to FY-2 gross_margin_pct 34 and latest_quarter gross_margin_pct 27.5; falsifier #2 restates the full ladder 34.0 -> 31.5 -> 27.5 with correct FY labels.",
      "Rationale 'loses money on each customer after paying to acquire them' is grounded in latest_quarter.contribution_profit_after_marketing_usd_m -18 and FY-0 contribution_profit_after_marketing_usd_m -55, plus the news bullet 'unit economics never cleared contribution after CAC'.",
      "Falsifier #1 arithmetic '-$18M on $310M revenue, roughly -5.8% of revenue' checks out: 18/310 = 5.81%; both inputs are packet values (latest_quarter revenue_usd_m 310).",
      "Rationale 'shares still trade near three times sales' is supported by valuation.ps_trailing 2.9 and ev_s_trailing 3.0; 'after a 40% run on a rebrand' quotes valuation.price_action '+40% YTD on consumer platform rebrand'.",
      "Rationale 'Cash covers under two years of burn, so a dilutive raise is a real risk' restates balance_sheet.note 'cash funds <2 years of current burn if growth spend continues' rather than inventing a burn model.",
      "Falsifier #3 cites cash $480M against $620M total debt, FY-1 free cash flow -$290M and FY-0 net income -$410M — all four figures appear verbatim in the packet's balance_sheet and annual_income_statement.",
      "Falsifier #4 grounds the structural disqualifier in a packet news item: 'Management highlights active customer definitions that exclude lapsed cohorts; a short note questioned the LTV math.'",
      "Sizing statements are anchored to the rendered constraint: '0.5-1% ($5,000-$10,000)' and '0% of the $1,000,000 benchmark NAV' match the stated anchor 0.5% = $5,000, 1% = $10,000.",
      "next_validation_point declines to invent a date — 'No exact date is available in the supplied packet, so this is kept as a relative event rather than a fabricated ISO date' — consistent with latest_quarter.which 'Q0 just reported' and as_of 'current'.",
      "rerating_horizon reports an explicit band ('12-24 months') and states the band's own uncertainty and its cause (untested paid-social spend reduction), satisfying the constraint that an unestimable horizon be declared rather than omitted.",
      "cited_sources ['fundamentals/OMK','news/OMK'] match the packet report_name fields exactly; no source identifier outside the packet is referenced."
    ],
    "output_clean": true,
    "packet_fidelity": true,
    "reasoning_grounded_score": 4,
    "summary": "Replay audit of case omk_synthetic (decision agent output vs. persisted packet + rendered constraints). CLEANLINESS: pass. The output names the subject only as \"OMK\"; the packet's fuller alias string (\"Orbit Meal Kits\") is never echoed, and no real issuer, ticker, exchange, executive name, or product brand appears. No calendar year, month, or datable event anchor occurs anywhere in output_full or response_raw — every temporal reference is relative (\"FY-0\", \"Q0 just reported\", \"next quarterly print\", \"2-3 months out\", \"12-24 months\"), which matches the persisted output_audit (contaminated=false, calendar_years_in_output=[], no suspect terms) and is consistent with the stage-2 sanitizer's relative_dates and macro_event_genericization passes. PACKET FIDELITY: pass; every numeric assertion traces verbatim to the packet. Growth 62% -> 6.8% (\"under 7%\") matches annual_income_statement FY-2/FY-0; gross margin 34% -> 27.5% matches FY-2 annual and latest_quarter; \"near three times sales\" matches ps_trailing 2.9 / ev_s_trailing 3.0; \"+40% run on a rebrand\" matches valuation.price_action \"+40% YTD\"; \"cash covers under two years of burn\" restates the balance_sheet note rather than inventing a burn calculation; contribution profit after marketing -$18M on $310M revenue is correctly derived as \"roughly -5.8% of revenue\" (18/310 = 5.81%) and -$55M for FY-0 matches; cash $480M vs debt $620M, FY-1 FCF -$290M and FY-0 net loss -$410M all match. Share price $22.7 matches. Forward thresholds ($250M cash, 32.0% gross margin, two-quarter persistence) are analyst-chosen future conditions, each explicitly framed against a cited current packet value, so they are not fabricated facts. CONSTRAINT FIDELITY: pass. The benchmark sizing anchor is respected — action=hold with size_shares_or_currency=0.0 deploys no capital, expected_impact expresses the sleeve as \"0% of the $1,000,000 benchmark NAV\", and the only sizing figures given (0.5-1% = $5,000-$10,000) map correctly onto the $1,000,000 NAV with USD notional stated explicitly, never a percentage smuggled into a currency field. No leverage or options are proposed; instrument=stock. The agent evaluated strictly on supplied facts and made no attempt to identify a real company. WORKFLOW: pass. All three fleet reporting requirements are present inside the recommendation payload: (1) FALSIFIERS — four, split repair-side (contribution profit turns positive two consecutive quarters without revenue decline; gross margin >=32.0% two consecutive quarters), confirm-side liquidity (cash <$250M with no non-dilutive financing), and structural (restatement/audit challenge of the contested \"active customer\" definition), each with an explicit decision_transition and, where left qualitative, an explicit justification for why no point-trigger was set; (2) THE CLOCK — next_validation_point identifies the next quarterly print and the two specific lines that decide it; (3) HORIZON — an honest band (\"12-24 months\") that also states why the band is genuinely uncertain, satisfying the requirement that hard-to-estimate horizons be reported rather than omitted. Critically, the agent declined to fabricate an ISO date and said so explicitly (\"No exact date is available in the supplied packet\"), which is the correct handling of a missing fact. A confidence band (MEDIUM) is reported, and cited_sources uses the packet's report_name strings verbatim (\"fundamentals/OMK\", \"news/OMK\") with no invented identifiers. The two revisit_triggers are internally consistent and the price trigger is explicitly labelled a review prompt rather than a buy signal, so it does not contradict the standing pass. Two non-blocking observations, recorded as neither violations nor grounding failures: (a) order_type/time_in_force are populated (\"market\"/\"GTC\") on a zero-size no-trade, which is schema-harmless but semantically inert; (b) the price trigger at $12.0 is described as \"roughly half today's $22.7\" when the exact half is $11.35 — a stated approximation, not a misquote of a packet figure. I did not and cannot assess whether the hold verdict is the investment-correct outcome, and I make no claim of access to hidden reasoning; the grounding score below grades only the written rationale, falsifiers, and horizon text.",
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
    "rationale": "Case omk_synthetic is a synthetic trap graded under the `synthetic_trap` rule, with expected_classes = [\"hold\"] (answer_key.expected_classes, grading input). The fleet verdict was action=\"hold\" with size=0.0 shares (fleet_result), i.e. no capital deployed and no position initiated — a verbatim match to the single expected class, so in_expected_class=true and the full in-class score of 1.0 applies. Partial credit (0.5) is not applicable here: the 0.5 band is reserved by the benchmark rule for an F1-style pass carrying falsifiers or a small trap buy that names the killing falsifier, and neither situation arises when the verdict is already in-class. The stage-4 calibration_reviewer (claude-opus-5) returned output_clean=true, packet_fidelity=true, workflow_correct=true, violations=[], reasoning_grounded_score=4, and verification.ok=true with no failures; per instruction I do not reopen that cleanliness/workflow judgment, and it does not modify the class grade in either direction. Return computation: answer_key.resolution is null and positioned=false, so there is no resolved price path and no benchmark series in the grading input; acted_return_pct and benchmark_return_pct are therefore left null rather than estimated — a synthetic trap case has no market outcome to realize, and fabricating one would violate the no-invented-facts rule. Consistent with that, outcome luck is irrelevant to this grade by construction. Confidence is HIGH: the grade is fully determined by primary inputs supplied in the grading payload (answer_key.expected_classes, fleet_result.action, fleet_result.size, grading=\"synthetic_trap\"), with no stale or missing data required. No domain_knowledge tax/regulatory rates and no external market data were used, so no such citations are owed; the only sources referenced by the underlying decision were the packet-internal report_names, which the reviewer confirmed verbatim. No fact needed for this grade is missing, so no domain-refresh escalation is recommended.",
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
- notes: Case omk_synthetic is a synthetic trap graded under the `synthetic_trap` rule, with expected_classes = ["hold"] (answer_key.expected_classes, grading input). The fleet verdict was action="hold" with size=0.0 shares (fleet_result), i.e. no capital deployed and no position initiated — a verbatim match to the single expected class, so in_expected_class=true and the full in-class score of 1.0 applies. Partial credit (0.5) is not applicable here: the 0.5 band is reserved by the benchmark rule for an F1-style pass carrying falsifiers or a small trap buy that names the killing falsifier, and neither situation arises when the verdict is already in-class. The stage-4 calibration_reviewer (claude-opus-5) returned output_clean=true, packet_fidelity=true, workflow_correct=true, violations=[], reasoning_grounded_score=4, and verification.ok=true with no failures; per instruction I do not reopen that cleanliness/workflow judgment, and it does not modify the class grade in either direction. Return computation: answer_key.resolution is null and positioned=false, so there is no resolved price path and no benchmark series in the grading input; acted_return_pct and benchmark_return_pct are therefore left null rather than estimated — a synthetic trap case has no market outcome to realize, and fabricating one would violate the no-invented-facts rule. Consistent with that, outcome luck is irrelevant to this grade by construction. Confidence is HIGH: the grade is fully determined by primary inputs supplied in the grading payload (answer_key.expected_classes, fleet_result.action, fleet_result.size, grading="synthetic_trap"), with no stale or missing data required. No domain_knowledge tax/regulatory rates and no external market data were used, so no such citations are owed; the only sources referenced by the underlying decision were the packet-internal report_names, which the reviewer confirmed verbatim. No fact needed for this grade is missing, so no domain-refresh escalation is recommended.
