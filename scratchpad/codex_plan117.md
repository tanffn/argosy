**RECOMMENDATION — BLOCK plan 117; hold SCHD and redirect all new flow; deploy the idle USD 125.8k into CSPX/EXUS/EIMI over three monthly UCITS tranches; execute SGOV→IBTA separately and delete every instruction to buy or park in SGOV.**

## Job 1 — Amended-plan review

### (a) Headline glide tax: confirmed defect

The plan must not carry ILS 1,849,929 merely because the amendment instruction supplied it. The canonical choices are:

- If the policy is live weight-based: use the resolver’s **8,920 shares sold, 1,460 retained and ILS 1,863,767 tax**.
- If 8,857 sold and 1,523 retained is an intentionally fixed owner-approved schedule: feed 8,857 into the canonical calculator and publish the resulting recalculated tax. Do not assume that ILS 1,849,929 remains correct.

As written, plan 117 mixes a live 13%/8% weight policy with a fixed hand-entered share schedule and a tax figure calculated elsewhere. That is a promotion-blocking provenance and consistency defect, even though the numerical gap is only ILS 13,838.

### (b) Self-referential numeric evidence: confirmed defect

`analyst_report:nvda_glide_amendment` proves only that you instructed the amendment to say those numbers. It does not prove that the numbers remain true.

Before promotion, bind or canonically derive:

- Total vested shares
- Shares sold and retained
- Two-year tax
- Seven-year comparison and pacing premium
- Whole-sale net-retention percentage or amount
- Each household-income component and total

The **ILS 71,700** premium currently has no identified resolver key. Either add one with the seven-year counterfactual inputs attached, or label it a non-binding estimate and keep it out of the headline.

The 68% instruction also needs correction. On the live full-glide arithmetic, gross proceeds are approximately USD 1,921,190 and net proceeds USD 1,298,065: **67.57%**, not exactly 68%. Rounding to 68% is reasonable for orientation but overstates deployable proceeds by about **USD 8.3k** across the whole glide. Execution should use actual settled proceeds, not `gross × 68%`.

### (c) Estate section: material reasoning was lost

Placement solely in `action_items` is not adequate. The estate section should explain:

- Why NVDA and US-domiciled ETFs are US-situs.
- Why Irish corporate ETF shares remove wrapper-level US situs.
- That account splitting changes first-death allocation but does not remove situs.
- That the NVDA glide and non-NVDA UCITS migration are the operative remedy.
- The current exposure, through a resolver token—or “pending,” never a stale literal.

The action item then implements that reasoning. It cannot substitute for it.

Worse, the replacement action says “park proceeds in SGOV.” That preserves the US-domiciled wrapper exposure and contradicts both the estate objective and the already-approved SGOV disposal. This is a direct blocker, not merely suboptimal placement.

### Additional defects you missed

- **Three incompatible NVDA schedules remain.** The frozen horizons still say 3,924 in 2026 plus 5,493 in 2027—**9,417 sold**, retaining 1,523. The amendment says **8,857 sold**. The resolver says **8,920 sold and 1,460 retained**. Plan 117 therefore fails internally even before considering the tax number.

- **The income arithmetic does not add.** ILS 310,642 + ILS 152,044 = **ILS 462,686**, not ILS 468,657. Either identify and cite the missing ILS 5,971 component or correct the household total.

- **The FI margin is stale.** The frozen plan still carries ILS 209,389. The supplied canonical figures imply gross margin of **ILS 330,380**: `−1,533,387 after tax + 1,863,767 glide tax`. The literal needs replacement by its resolver token.

- **The US-situs amount is internally inconsistent.** Plan 117 still says ILS 9,825,302. Using the prompt’s 73.9% of USD 4.1367m and approximately 2.991 USD/ILS gives roughly **ILS 9.14m**, not ILS 9.83m. Moreover, the same-run estate resolver degraded to unavailable during this review because current-money repricing could not clear stale marks. Publish a token/pending state, not either handwritten number.

- **The checklist is not executable.** It says to sell the next tranche while its evidence subtree admits the slice count is still pending. Load the controlling trustee/tax-lot record, resolve the tranche, then authorize the order.

- **Eligibility wording should use the trustee record.** Safer language is “the trustee-confirmed Section-102 eligibility date, ordinarily 24 months from grant/deposit with the trustee,” rather than treating vest or grant date alone as dispositive. The statutory administration turns on the trustee holding period. [Israel Tax Authority guidance](https://www.gov.il/BlobFolder/policy/procedures-190325-1/he/IncomeTax_procedures-190325-1.pdf)

- **The allocation target still automatically maps SCHD to FUSA.** Job 2’s conclusion below supersedes that. A frozen target telling the engine to force an SCHD→FUSA swap would remain an executable contradiction even after repairing these six sections.

Bottom line: mechanics pass; content fails. I would block on the conflicting NVDA schedules, noncanonical tax, SGOV instruction, stale FI/estate numbers and income arithmetic.

## Job 2 — SCHD replacement closed

The longest common history runs from DHSA’s 3 November 2016 inception through 31 July 2026:

| Fund | Annualized total return | Cumulative return | USD 286.15k hypothetical ending value |
|---|---:|---:|---:|
| DHSA | 9.64% | +145.0% | ~USD 701.2k |
| SCHD | ~13.19% | ~+234.2% | ~USD 956.3k |
| Difference | ~3.55 pp/year | ~89.2 pp | **~USD 255.1k** |

DHSA’s figure is the issuer’s net, dividend-reinvested since-inception return. SCHD’s identical-date figure is a custom reinvested-return reconstruction; Schwab does not publish that exact custom interval, but its official 31 July 2026 table reports **12.65% annualized over ten years**, which is consistent with the reconstruction. [WisdomTree DHSA factsheet](https://dataspanapi.wisdomtree.com/pdr/documents/FACTSHEET/UCITS/EU/EN-GB/IE00BD6RZT93/), [Schwab SCHD performance](https://www.schwabassetmanagement.com/products/schd?page=1)

DHSA has not been inferior in every regime: its last three years were 14.91% annualized versus SCHD’s 13.99%, both to 31 July 2026. But over its complete life it has not matched SCHD, and it is not an index clone. No Irish UCITS product is shown as tracking the exact Dow Jones U.S. Dividend 100 Index. [S&P index and linked products](https://www.spglobal.com/spdji/en/indices/dividends-factors/dow-jones-us-dividend-100-index/)

**Final verdict: hold SCHD and redirect new flow only.**

For this existing position, the contingent estate saving does not justify accepting DHSA’s demonstrated full-period performance give-up—especially after adding Israeli realization tax and the owner’s explicit reluctance to sell. That is a historical comparison, not a forecast, but there is insufficient evidence to force the swap.

Operationally:

- Stop SCHD purchases and DRIP.
- Redirect dividends and new savings to the broad UCITS allocation below.
- Do not buy DHSA or FUSA merely to imitate SCHD.
- Revisit an SCHD sale only if a capital-loss offset appears, embedded gain falls materially, or health/estate urgency changes. “Hold permanently” would be unnecessarily absolute.

## Job 3 — Deployment closed

Yes—the USD 125.8k is deployable independently of plan 117’s NVDA defects, assuming it is genuinely above the ILS 30,000 floor and any already-incurred tax obligations.

Normalize plan 117’s three broad-equity targets—CSPX 26.92%, EXUS 13.51%, EIMI 4.92%—and deploy:

- **CSPX: 59.36% — approximately USD 74.7k**
- **EXUS: 29.79% — approximately USD 37.5k**
- **EIMI: 10.85% — approximately USD 13.6k**

Because the plan explicitly elected DCA, use three equal tranches:

| Timing | CSPX | EXUS | EIMI | Total |
|---|---:|---:|---:|---:|
| Next LSE session | USD 24.9k | USD 12.5k | USD 4.55k | ~USD 41.9k |
| 30 days later | USD 24.9k | USD 12.5k | USD 4.55k | ~USD 41.9k |
| 60 days later | Balance to target | Balance to target | Balance to target | ~USD 41.9k |

Use actual converted cash and round the final tranche for whole shares and fees. Do not add to FWRA or ACWD; retain them, but direct new flow to the cleaner CSPX/EXUS/EIMI sleeves rather than expanding overlapping all-world wrappers.

The SGOV contradiction is real. Execute the already-approved **USD 85.5k SGOV→IBTA** swap separately. IBTA is a 1–3-year bond holding, not same-day household liquidity, so do not count it toward the ILS 30,000 floor or a near-term tax reserve. Replace the plan instruction with: **“Do not purchase SGOV; hold undistributed cash directly until the scheduled UCITS tranche, and execute the approved SGOV→IBTA migration separately.”**