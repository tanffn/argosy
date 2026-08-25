RECOMMENDATION: Keep the 2-year glide and estate de-risking as provisional directions, but do not rely on the ILS 68,543 premium or execute the USD 529k swap basket as currently described. Correct Items 4 and 3, reconcile the swap inventory/equivalents, and ingest the property and legal-ownership facts first. Items 3–6 do not require another owner decision; only the final risk trade-off and facts found only in deeds/loan documents belong with the owner.

## Per-item triage

### Item 3 — annual surtax threshold

**Real and material, severity: medium-high.**

The dated resolver changes eligibility at one future date and then calls the same aggregate calculation once. It does not model sales by calendar year. That is visible in [plan_numeric_resolver.py](/D:/Projects/financial-advisor/argosy/services/plan_numeric_resolver.py:2630) and [tax_simulation_ingest.py](/D:/Projects/financial-advisor/argosy/services/tax_simulation_ingest.py:354).

For a 2-year glide where each year’s Section-102 capital income exceeds ILS 721,560, flat 30% taxation overstates tax by at most:

`2 years × 2% × ILS 721,560 = ILS 28,862`

“At most” matters: interest, dividends, other gains, or losses in each year change how much threshold remains. If other capital-source income already consumes the threshold, the relief can approach zero.

**Minimal correct fix:** persist or derive `(tax_year, lot, shares, expected sale price)` and calculate each year separately:

`0.50 × ordinary_y + 0.28 × capital_y + 0.02 × max(0, capital_y + other_capital_income_y − threshold_y)`

Then sum the years. Do not call this “dated” merely because eligibility was projected to a terminal date.

**Routing:** Argosy. It should derive the lowest-benchmark-first schedule within the already chosen 2-year constraint. Only genuinely off-system capital income or loss carryforwards require documents; if unavailable, publish a range.

---

### Item 4 — optimizer uses the old model

**Real and critical. It directly taints a number used for an owner decision.**

The optimizer still sets `total_taxable_gain = 0.8 × sale`, then applies its blended CGT curve; see [deconcentration_optimizer.py](/D:/Projects/financial-advisor/argosy/services/retirement/deconcentration_optimizer.py:95) and [the adapter](/D:/Projects/financial-advisor/argosy/services/retirement/deconcentration_optimizer.py:374). It neither consumes grant benchmarks nor separates ordinary, capital, breaking, and future-eligible lots.

Worse, it discounts the whole annual tax stream. Under the corrected model that includes the 50% ordinary slice; economically, later payment has lower present value, so this discounting can make the 7-year option appear more attractive even though the nominal ordinary liability is fixed.

**Minimal correct fix:**

- Remove `NVDA_TAXABLE_GAIN_FRACTION`.
- Use the same per-lot authority as `realization_tax_summary`.
- For every candidate horizon, derive eligible sale years and allocate lowest benchmark first.
- Tax each calendar year using the Item 3 formula, including other capital income.
- Model breaking shares as ordinary income.
- Keep nominal tax and discounted present value as separate outputs.
- Make the optimizer and `scripts/adjudicate_nvda_glide_schedule.py` share one tax engine.
- Add parity tests against the independently verified lot oracle before reranking horizons.

**Routing:** Argosy. The owner chooses speed versus concentration risk only after Argosy supplies correct numbers. Do not ask him to choose tax assumptions or lot allocation.

---

### Item 5 — uncommitted moonshot ruling

**Real, severity: medium governance risk; no immediate portfolio number changes.**

The working-tree edit is a valid standing preference and should not remain ephemeral. The executable allocation verifier already implements and cites this ruling in [verifier.py](/D:/Projects/financial-advisor/argosy/services/allocation_author/verifier.py:30), making an uncommitted “authoritative record” particularly fragile.

**Minimal correct fix:**

- Commit the focused estate-tax ruling separately.
- Do not silently bundle the unrelated retrieved-date changes or scratchpad.
- Add an end-to-end regression proving a bounded, disclosed moonshot US-situs buy passes while an identical core/growth buy fails.
- Verify the generic plan-risk kernel also receives sleeve context; changing prose alone must not leave a deterministic gate still flagging the buy.

**Routing:** Argosy. The owner has already ruled. Asking again would be improper.

---

### Item 6 — VHT, AVUV, REET unknown

**Real, severity: low today. It currently does not change the estate number.**

All three are U.S.-domiciled funds and therefore estate-exposed:

- VHT is Vanguard’s domestic U.S. health-sector ETF. [Vanguard](https://investor.vanguard.com/investment-products/etfs/profile/vht)
- AVUV is a series of the Delaware-organized American Century ETF Trust. [SEC filing](https://www.sec.gov/Archives/edgar/data/1710607/0001710607-20-000248-index.htm)
- REET is an iShares U.S. ETF traded on NYSE Arca. [iShares](https://www.ishares.com/us/products/268752/ishares-global-reit-reit-etf)

The current gate already counts `None` conservatively as US-situs, so authoritative classification improves confidence and suppresses warnings but should not move the total.

**Minimal correct fix:** add reference rows and tests:

- VHT: equity / healthcare / U.S. exposure / ETF / `estate_safe=False`
- AVUV: equity / small-cap value / U.S. exposure / ETF / `estate_safe=False`
- REET: real estate / global exposure / ETF / `estate_safe=False`

Also add all three to the explicit U.S.-situs set. Do this even if a sale is pending; “pending” is not “sold.”

**Routing:** Argosy. These are public instrument facts.

---

### USD 529k UCITS swap recommendation

**The direction is strong, but the proposed basket is not yet executable as stated. Severity: critical because it proposes certain tax today.**

The inheritance premise supporting it is correct—see Question 3—but four other problems remain:

1. **The inventory does not reconcile.** The six amounts written in the request total about USD 464.7k, not USD 529k. The current snapshot reaches approximately USD 529.25k only by also including the second SCHD and VOO holdings, both SCHG holdings, and VHT/AVUV/REET.

2. **The USD 25.8k tax is plausible but not final.** Current snapshot values and average prices produce about USD 93.0k of positive gain and approximately USD 26.1k at 28%. Exact Israeli tax requires tax lots, sale-date FX, capital losses/carryforwards, and other annual capital income.

3. **Some “equivalents” are not equivalent.**

   - VOO→CSPX and QQQM→CNDX are close index substitutions.
   - SGOV→IBTA materially increases duration: SGOV is 0–3 months, while IBTA tracks 1–3 year Treasuries and currently has roughly 1.85-year duration. [SGOV](https://www.ishares.com/us/products/314116/ishar), [IBTA](https://www.ishares.com/uk/individual/en/products/287340/)
   - SCHD tracks the Dow Jones U.S. Dividend 100 Index, while FUSA tracks Fidelity’s separate U.S. Quality Income Index. They are related style exposures, not twins. [SCHD](https://www.schwabassetmanagement.com/products/schd), [FUSA](https://www.fidelityinternational.com/FILPS/Documents/en/current/pro.en.xx.IE00BYXVGY31.pdf)
   - VTV, SPMO, SCHG, AVUV, VHT, and REET need explicit destination decisions.

4. **Estate exposure must be calculated per legal owner, not as one household pool.** The USD 60k threshold applies to each decedent’s U.S.-situated gross estate. Account registration, beneficial ownership, and property title can materially change the result.

**Minimal correct fix:** produce an account-and-lot-level swap ledger that reconciles exactly to USD 529.25k, identifies a destination for every line, reports tracking/style drift, computes tax after usable losses, and models estate exposure separately for Ariel and Noga. Ingest both properties before using the worldwide-asset denominator for mortgage deductions.

Directionally, the trade remains compelling: if the full USD 529k is currently in the 40% marginal estate-tax band, removing it can eliminate roughly USD 212k of death-time exposure in exchange for about USD 26k of current tax. But that comparison is contingent on ownership, continued holding, mortality timing, deductible debts, and replacement equivalence.

**Routing:** Argosy derives the inventory, tax, replacements, and expected-value comparison. Route only these to the owner:

- deeds/account ownership and mortgage documents;
- whether preserving a specific factor exposure is preferred when no close UCITS analogue exists;
- the final preference between certain tax now and contingent estate risk later.

## Priority order

1. **Swap/property/ownership reconciliation** — largest prospective transaction and estate exposure; current basket and denominator are incomplete.
2. **Item 4** — the horizon-ranking engine is invalid and affected an owner choice.
3. **Item 3** — current glide tax and FI margin are conservatively overstated, potentially by about ILS 28.9k.
4. **Item 5** — persist the already-made owner ruling and test all consumers.
5. **Item 6** — authoritative cleanup; current conservative total is already directionally correct. Implement opportunistically during the swap reconciliation.

## Specific questions

### 1. Does the corrected model shrink the 2-vs-7 premium?

**No. That conclusion does not follow structurally, and the current lot data points the other way.**

Let `C` be total capital income and `O` ordinary income. Ignoring discounting, total tax across a horizon is:

`0.50O + 0.28C + annual 2% band tax`

Both `0.50O` and `0.28C` cancel when comparing two horizons. The old model’s base 28% component was also timing-invariant. Therefore, introducing an ordinary slice does not itself shrink the horizon premium; only the amount and yearly distribution of `C` determine the annual-band difference.

On the current 8,920-share oracle:

- Correct capital income: approximately ILS 5.028m
- Ordinary income: approximately ILS 0.710m
- Old assumed capital gain, `80% × sale`: approximately ILS 4.597m

Under equal annual tranches and no other capital income, the threshold-only 2-vs-7 premium is therefore about:

- Correct model: **ILS 71.7k**
- Old 80%-gain model: **ILS 63.1k**

So the corrected capital base is larger, because selling lowest-benchmark lots makes `S−B` about 87.5% of sale proceeds—not 80%. The premium grows in this simplified comparison.

The optimizer’s 2% discount makes the hypothesis even less likely: deferring the newly recognized ordinary tax reduces the 7-year present value, increasing the apparent fast-glide premium.

What could change or falsify that numerical result:

- a different lot/share schedule;
- materially different future sale prices;
- other capital income consuming annual thresholds;
- capital-loss offsets;
- uneven tranches;
- eligibility/breaking shares;
- annual threshold changes;
- removing or changing the tax-payment discount.

Therefore I cannot certify the final premium without rerunning the corrected schedule, but “it must shrink because ordinary tax is fixed” is false. The quoted ILS 68,543 may survive only by coincidence.

### 2. Does the Atlanta property create estate exposure at zero equity?

**Possibly—but historical gross cost is irrelevant. Current fair market value, title, debt recourse, and whether the mortgage was actually funded decide it.**

The database currently says the property was never delivered, the USD 219,475 mortgage was never drawn, and both current value and loan are zero. That conflicts with the new description of property “against a mortgage.”

The branches are:

- **Asset/current legal interest genuinely worth zero:** inclusion value is zero. A former USD 318k contract price is not date-of-death estate value.
- **Positive-FMV property with recourse mortgage:** report full FMV as U.S. gross estate. The debt is then multiplied by `U.S. gross estate ÷ worldwide gross estate`; zero economic equity does not make it disappear.
- **Positive-FMV property with nonrecourse mortgage:** generally include only the equity of redemption—FMV less nonrecourse debt, floored at zero.
- **Never delivered/no title:** the asset may instead be a contractual claim against the developer, requiring separate situs and valuation analysis.

Form 706-NA expressly lists U.S. real estate as U.S.-situated, applies the worldwide pro-rata deduction on Part IV lines 5–6, and requires filing when U.S.-situated gross assets exceed USD 60,000. [Current Form 706-NA](https://www.irs.gov/pub/irs-pdf/f706na.pdf), [IRS instructions](https://www.irs.gov/instructions/i706na), [filing threshold](https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns)

Today, the securities already exceed USD 60k, so a 706-NA filing obligation would exist regardless of Atlanta. To resolve Atlanta I need the deed/closing status, current appraisal, current loan statement, promissory note and guaranty/recourse language, ownership percentage, and developer-bankruptcy claim documents.

### 3. Is Israeli no-step-up on death correct?

**Yes, for these post-31 March 1981 inherited securities.**

Section 88 treats inheritance itself as outside the definition of a sale. For assets inherited from a decedent dying after 31 March 1981, the heir’s original price and acquisition date are those that would have applied had the decedent sold—the tax-continuity/carryover-basis rule, not date-of-death fair market value. The official statutory text states both rules, and an Israeli district-court decision describes the heirs as stepping into the decedent’s shoes. [Israeli Income Tax Ordinance, §88](https://www.gov.il/BlobFolder/legalinfo/law_pkudat_mas_hachnasa/he/LegalInformation_kesher_%D7%A4%D7%A7%D7%95%D7%93%D7%AA%20%D7%9E%D7%A1%20%D7%94%D7%9B%D7%A0%D7%A1%D7%94%20%5B%D7%A0%D7%95%D7%A1%D7%97%20%D7%97%D7%93%D7%A9%5D%20-%20%D7%9C%D7%90%20%D7%9E%D7%A8%D7%95%D7%91%D7%93.pdf), [court explanation](https://www.gov.il/BlobFolder/legalinfo/law12837-06-17/he/LegalInformation_kesher_12837-06-17.pdf)

Thus the Israeli embedded gain is generally deferred through death, not erased. That materially supports the UCITS swap, although the time value of potentially decades of additional deferral and the heirs’ future residence still belong in the economic comparison.