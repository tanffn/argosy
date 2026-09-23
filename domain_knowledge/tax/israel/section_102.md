---
title: Israeli Section 102 — RSU and Stock-Option Taxation (Capital Gains Track) — 2026
topic: israel_section_102_rsu_taxation
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual_with_employer_equity
last_verified: 2026-09-08
verified_by: per-lot §102 ledger reconciliation vs trustee simulation report (2026-07-09) + Amendment 147 statutory check; SUPERSEDES the 2026-07-08 web-search verification on the holding-clock rule (see correction trail in the holding-period section)
next_refresh_due: 2027-01-31
source_urls:
  - https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation
  - https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide
  - https://www.rnc.co.il/employee-stock-options-israel/
  - https://taxsummaries.pwc.com/israel/individual/income-determination
sources:
  - url: file://Resources/2026/Payslip/Ariel/2026_01.pdf
    as_of: 2026-01
    tier: 1
    label: "Already supplied January payroll; historical insured-income evidence, not proof of later months"
  - url: file://Resources/2026/Payslip/Ariel/2026_02.pdf
    as_of: 2026-02
    tier: 1
    label: "Already supplied February payroll"
  - url: file://Resources/2026/Payslip/Ariel/2026_03.pdf
    as_of: 2026-03
    tier: 1
    label: "Already supplied March payroll"
  - url: file://Resources/2026/Payslip/Ariel/2026_04.pdf
    as_of: 2026-04
    tier: 1
    label: "Already supplied April payroll; later sale-month payroll is not established by this record"
  - url: https://www.gov.il/BlobFolder/reports/press-income-tax-brackets/he/SalaryDataDetails_tax_bracket_2026.pdf
    tier: 1
    label: "Official 2026 bracket table; rate source missing from the prior section-102 packet"
  - url: https://www.btl.gov.il/English%20Homepage/Insurance/Ratesandamount/Pages/forSalaried.aspx
    tier: 1
    label: "Public 2026 employee NI rates and ceiling; not evidence that household payroll exhausts the ceiling"
  - url: https://www.btl.gov.il/Laws1/00_0103_000000.pdf
    pdf_pages: [91, 92, 100, 101]
    retrieved: 2026-09-12
    tier: 1
    label: "BTL-hosted consolidated Income Tax Ordinance: selected original pages cover section 91 reporting and section 102, not the entire statute"
  - url: file://Resources/2026/Leumi/usd.xls
    as_of: 2026-05-10
    tier: 1
    label: "Historical USD main cash subaccount 094, printed May 10: February 4/17 and April 29 wire receipts; booking dates are not share-sale dates"
  - url: file://Resources/2026/Leumi/leumi_2026_Jul_13_usd_2.xls
    as_of: 2026-07-13
    tier: 1
    label: "Historical USD main cash subaccount 094, printed July 13: May 18 and June 8 wire receipts, not custody subaccount 968"
  - url: file://Resources/2026/Leumi/תנועות בחשבון מטח 22-08-2026 (1).xls
    as_of: 2026-08-22
    tier: 1
    label: "Historical USD main cash subaccount 094, printed August 22: August 19 wire receipt; cash receipt alone does not establish tax-component allocation"
  - url: file://Resources/2026/Schwab/Nvidia simulation Report.xlsx
    as_of: 2026-06-18
    tier: 1
    label: "Historical trustee simulation; verify its fields, not current holdings or a completed tax payment"
  - url: file://Resources/2026/Schwab/EquityAwardsCenter_EquityDetails_2026822195554.xlsx
    as_of: 2026-08-22
    tier: 1
  - url: file://Resources/2026/Schwab/EquityAwardsCenter_Transactions_20260529103123.csv
    as_of: 2026-05-11
    tier: 1
    label: "Historical transactions June 15, 2022 through May 11, 2026: 74 Lapse blocks with continuation rows and January/February sales; as_of is transaction cutoff, not current holdings"
  - url: file://Resources/2026/Schwab/EquityAwardsCenter_Transactions_20260822165318.csv
    as_of: 2026-08-22
    tier: 1
  - url: https://www.gov.il/BlobFolder/policy/procedures-091224/he/IncomeTax_procedures-091224.pdf
    tier: 1
    retrieved: 2026-09-12
    label: "ITA circular 01/2024: Section 102 plan submission and reporting from 2025"
  - url: https://www.gov.il/BlobFolder/policy/inst-05-2025/he/IncomeTax_inst-05-2025.pdf
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.irs.gov/instructions/i706na
    retrieved: 2026-09-12
    tier: 1
  - url: file://Resources/2025/106/Form%20106%20-%209120%20-%20%D7%9E%D7%9C%D7%90%D7%A0%D7%95%D7%A7%D7%A1.pdf
    retrieved: 2026-08-22
    tier: 1
    label: Historical 2025 employer tax certificate; preserves settled household reconciliation, not current holdings.
  - url: https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation
    retrieved: 2026-08-28
    tier: 2
  - url: https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide
    retrieved: 2026-08-15
    tier: 2
  - url: https://www.rnc.co.il/employee-stock-options-israel/
    retrieved: 2026-09-08
    tier: 2
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-09-07
    tier: 2
---

# Section 102 — Israeli RSU and Stock-Option Taxation (Capital Gains Track)

> **NOTE:** This file lives at `domain_knowledge/tax/israel/section_102.md` per the refresh spec. A historical copy also exists at `domain_knowledge/tax/israel/retirement/section_102.md`; this file is the canonical 2026-refreshed version.

## Summary

`Section 102` of the Israeli Income Tax Ordinance governs taxation of employee stock-based compensation (RSUs, options, and similar awards). Two principal tracks exist; the choice has major rate consequences. The **Capital Gains Track via a Trustee** (`102 הוני באמצעות נאמן`) is the standard election at Israeli high-tech employers (including NVIDIA Israel) and delivers a **25% flat capital-gains rate** on the post-grant appreciation, provided the **24-month trustee holding period from the grant (allotment) date** is observed (Amendment 147, 2006 — see the holding-clock section, including the correction trail).

## Rates / brackets / amounts (2026)

| Track | Tax classification of gain | Statutory rate | Surtax overlay | Holding period | Employer expense deduction |
|---|---|---|---|---|---|
| **102 Capital — Trustee** | Capital gain (on the appreciation slice) | **25%** flat | + 3% general / + 2% capital-source above ₪721,560 → up to 30% | **24 months from grant (allotment) date** | **No** — employer cannot deduct |
| **102 Ordinary — Trustee** | Salary income | Marginal labor rate (up to 47% + 3% surtax = 50%) + NI/health up to ceiling | Surtax applies as labor income | Same trustee mechanism, shorter "12 months" practical relevance | Yes |
| **102 Non-Trustee** | Salary income at vesting | Marginal labor rate | Surtax applies as labor | None | Yes |
| **Section 3(i)** — non-employees, contractors, controlling shareholders | Salary income | Marginal labor rate | Surtax applies | None | n/a |

Primary authority for the current capital-track rate, holding clock and public-company benchmark: operative Income Tax Ordinance §102(a)–(b), original pages 100–101 in the BTL-hosted consolidated statute cited above. NASPP, VestingStrategy, RNC and PwC are secondary context, not substitutes for that operative text.

### Holding-period clock — the precise rule (CORRECTED 2026-07-09)

- The 24-month period runs **from the grant (allotment) date** — the date the awards are allotted to the qualified trustee — per **Amendment 147 to the Income Tax Ordinance (2006)**, which shortened the requirement from the original 2003-reform rule ("until the end of the tax year of grant plus 24 months") to a flat **24 months from allotment**.
  - **Current authority / historical attribution:** the operative §102(a) definition specifies 24 months from allotment **and deposit with the trustee**. That current rule was checked directly against the statute. The Amendment 147/year and prior-wording narrative above is retained historical attribution, not independently verified by the selected current-statute pages. A grant date alone does not document an absent trustee deposit.
  - Example: RSU granted 8 April 2024 → trustee clock ends 8 April 2026 → capital-track eligible from 8 April 2026.
- **Empirical confirmation (trustee's own eligibility engine):** the NVIDIA trustee simulation report dated 2026-06-18 marks grants **289172 / 289173 (granted 2024-04-08)** as capital-track **eligible** ("OK" holding period). Under an end-of-grant-tax-year rule they would only become eligible **2027-01-01** — the trustee's marking is consistent ONLY with 24-months-from-grant-date. Grants 331375 (2025-03-10) and 374434 (2026-03-09) are correspondingly marked "Breaking" (not yet eligible), exactly as from-grant-date predicts.
- **Correction trail:** the 2026-06-02/2026-07-08 refreshes of this file asserted the opposite rule ("24 months from the end of the tax year of grant") based on secondary web summaries (NASPP / VestingStrategy / RNC) and labelled the from-grant-date rule an "LLM trap". That was the **pre-Amendment-147** rule. Several secondary sources still repeat it; the statute as amended in 2006 and the trustee's live eligibility behavior both say **from grant date**. Reviewers: do not "re-fix" this back from those secondary sources without checking Amendment 147 (תיקון 147) itself.
- Sources: Income Tax Ordinance §102 as amended by Amendment 147 (2006); NVIDIA trustee simulation report 2026-06-18 (eligibility markings, `Resources/2026/Schwab/Nvidia simulation Report.xlsx`).

### Capital vs ordinary split for **public-company** RSUs (the user's case)

- Use the settled **30-trading-day pre-grant benchmark**, subject to realized proceeds and the applicable statutory conditions, for the ordinary slice. The detailed trustee reconciliation below supersedes the former “lesser of grant-day spot and average” wording. Do not substitute FMV at vest or broker cost basis.
- Appreciation above the applicable benchmark is the capital slice for eligible Section 102 capital-track awards. Preserve the per-lot eligibility and ILS conversion rules below.

## Application notes

### Trustee mechanism

- Qualified Israeli trustees (e.g., Altshuler Shaham Trusts, Harel, IBI Trust, ESOP Excellence) hold the granted RSU shares for the trustee period.
- The employer's plan/trustee submission must precede allocation by at least
  **30 days**; this is not a 30-day deemed-approval rule. ITA circular 01/2024
  distinguishes that lead time from **90 days without an assessing-officer
  response** for deemed approval (sections 3.1.2–3.1.3). From 2025 the mandatory
  online submission/reporting requirements apply; improper submission can change
  the tax route. Verify employer/trustee compliance from the actual plan records,
  not from a portal label alone.
- A brokerage display or the end of the holding clock does not by itself establish taxable release from the trustee. Use the actual award/trustee records.
- For this household, the settled Form 106/trustee reconciliation below establishes **both slices withheld and reported at sale**. The old draft's claim that the employee separately reserves the entire capital slice was superseded; do not recreate that liability.

### Consequences of breaking the holding period

- Selling or transferring out of the trustee before the 24-month-from-grant-date mark: **the entire gain (not just the ordinary slice) reclassifies as ordinary salary income** taxed at marginal rates (up to 50%) plus NI/health (up to ceiling).
- This is a one-way penalty — there is no partial credit and no way to "fix" the early sale.
- The employer/trustee will withhold accordingly at sale; the ITA reconciles on the annual return.
- Source: NASPP; RNC.

### Cash flow at vest — superseded draft corrected

- The 74 historical Lapse events and trustee reconciliation below show **no withholding at vest for this household**. A “Withhold Shares” portal preference is not proof of a transaction. Both tax slices are settled at sale; do not deduct a fictitious vest-time tax or regard the ordinary slice as already paid.
- Vesting produces shares, not newly available Leumi cash. Use actual sale, withholding and transfer records for deployable proceeds.

### Interaction with the US-Israel treaty

- 25% Israeli Section 102 Capital rate applies regardless of broker location — NVDA-at-Schwab is fully subject to Israeli 102 because the user is an Israeli resident.
- The ordinary NRA capital-gain exemption assumes applicable domestic and treaty
  conditions, including the absence of the Article 15 exceptions. In particular,
  presence in the US for 183 days or more in the tax year requires re-evaluation;
  do not carry a blanket 0% assumption into changed residence or travel facts.
- US **estate tax** still applies to NVDA shares held by an Israeli decedent because the shares are US-situs — see `treaties/us_israel.md` and (when present) `tax/us/estate_tax_nonresidents.md`.

### Ariel-specific cash-flow corollary (2026-08-22 portfolio snapshot)

Historical book (2026-08-22 Schwab equity-details export): **10,380** vested NVDA
shares (RSU/award 8,885 + ESPP 1,495), ~$215.38/share, ~$2.236M USD. Under the
settled 24-month allotment/deposit clock, **8,670** were past the clock on that
date: 8,470 RSU/award shares plus 200 ESPP shares purchased August 31, 2023.
The 903 ESPP shares purchased/deposited August 30, 2024 reach their anniversary
on **August 30, 2026**, not August 22. Holding that snapshot constant, the count
then becomes **9,573**; it is not a current-position claim. Schwab's ESPP
"Qualified" label is not proof of the Israeli trustee clock: the June 18 trustee
simulation marks those 903 shares "Breaking". A further **3,378** unvested
shares vest through 2030-03-15. Neither vesting nor the brokerage display alone
establishes taxable release from the trustee.

### The grant-date benchmark is the 30-TRADING-DAY MEAN — derived 2026-08-22

The settled household model uses the **average closing price over the 30
trading days preceding grant**, subject to realized proceeds and applicable
statutory conditions. The trustee reconciliation below reproduces that
benchmark exactly; it does not establish a separate grant-day-spot minimum:

| Grant | Granted | Trustee "Grant Stock Price (For Tax)" | 30-trading-day mean of NVDA closes before grant | Error |
|---|---|---|---|---|
| 213000 | 2022-06-08 | 18.1159 | 18.1159 | 0.000% |
| 246477 | 2023-06-08 | 31.9859 | 31.9859 | 0.000% |
| 289173 | 2024-04-08 | 87.4976 | 87.4976 | 0.000% |

Note 246477: FMV at grant was 38.4351 but the trustee used 31.9859 — a 17% gap,
so this is genuinely the 30-day mean and not a coincidence of the two agreeing.

**This means the basis for ANY grant is derivable without the trustee**, from
split-adjusted NVDA closes:

```python
c = yf.Ticker("NVDA").history(start=..., auto_adjust=False)["Close"]  # split-adjusted
basis = float(c[c.index <= grant_date].tail(31)[:-1].tail(30).mean())
```

Historical benchmark values; the 2026-06-18 simulation also contains 18.3305
for grant 182406, so it was wrong to describe that value as absent from the
trustee record. Do NOT substitute a vest-FMV proxy for these:

| Grant | Granted | Shares held | §102 basis |
|---|---|---|---|
| 182406 | 2021-07-09 | 1,420 | **18.3305** |
| 331375 | 2025-03-10 | 358 | **126.8600** |
| 374434 | 2026-03-09 | 57 | **185.7690** |

A vest-FMV proxy for 182406 would have used ~149.38 and understated that grant's
gain by roughly **8x**. Because 182406 is the source of most 2026 sales, the
proxy understated the whole 2026 realized gain by 36%.

### Which cost basis feeds the 25% — CORRECTED 2026-08-22

An earlier revision of this file asserted that "the `Avg Price` Schwab tracks is
the FMV-at-vest cost basis used for the capital-slice computation, **not** the
original grant FMV". **That is wrong, and it understates or overstates the
Israeli liability depending on the grant.** The two brokers compute for two
different tax systems:

| | Basis used | For |
|---|---|---|
| Schwab `TotalCostBasis` / `RealizedGainLoss` | **FMV at vest** | US-style reporting |
| Israeli trustee (§102) | **30-trading-day pre-grant benchmark** | the 25% capital slice |

Verified against the trustee's own simulation engine (`Nvidia simulation
Report.xlsx`, 2026-06-18), whose grants span a wide enough price range to
distinguish the rules — a single 2022 grant cannot, because its grant and vest
prices sit within 2.5% of each other:

| Grant | Granted | Grant px | Capital income | = n x (sale - GRANT px)? | Ordinary income | = n x GRANT px? |
|---|---|---|---|---|---|---|
| 213000 | 2022-06-08 | 18.1159 | 52,230 | 52,230 YES | 5,069 | 5,072 YES |
| 246477 | 2023-06-08 | 31.9859 | 36,259 | 36,259 YES | 6,715 | 6,717 YES |
| 289173 | 2024-04-08 | 87.4976 |  9,372 |  9,372 YES | 6,999 | 7,000 YES |

So: **ordinary slice = shares x the 30-day benchmark** (marginal rate, ~62.17%
in the sim); **capital slice = shares x (sale price - benchmark)** at 25% + the
surtax stack (`surtax.md`).

### BOTH slices fall due at SALE — the ordinary slice is NOT settled at vest

Established 2026-08-22 after asserting the opposite. Section 102 makes the
taxable realization event the earlier of *transfer out of the trustee* or *sale*
— **not vesting**. In the historical May 29-named Schwab export (transactions
through May 11), all **74 `Lapse` blocks** have continuation rows explicitly
showing `SharesSoldWithheldForTaxes = 0`, populated `NetSharesDeposited`, and
blank `Taxes`. Reading only the parent Action row incorrectly made those
detail fields appear empty. All 74 match a deposit by award ID and vest date.
Raw parent/deposit quantities versus detail net shares show mixed **1×/10×
scaling** and require normalization before comparison; they are not uniformly
identical raw numbers. This supports **no recorded vest-time withholding** in
that historical export, not a conclusion about sale-tax components or final
annual liability. The trustee's simulation corroborates realization-time tax:
it charges ordinary tax at realization on shares that vested years earlier.

A portal election reading "Withhold Shares" is a *preference*, not evidence that
withholding occurred.

**Consequence — the tax per share is:**

```
tax_per_share ~= 0.30 x (S - B) + 0.50 x B  =  0.30 x S + 0.20 x B
```

where S is the sale price and B the benchmark. **A HIGHER benchmark means HIGHER
total tax**, because it swaps 30%-taxed capital income for 50%-taxed ordinary
income. Two direct consequences, both the reverse of what an earlier draft of
the glide proposed:

* **Sell LOWEST-benchmark lots first** if minimising total tax. (Selling
  highest-benchmark first maximises *shares shed per shekel of capital-source
  income* — a different objective, worth stating explicitly whenever used.)
* **Retaining the lowest-benchmark grant defers the LEAST total tax**, not the
  most. It defers the most *capital* tax while carrying the smallest ordinary
  slice forward.

Estate exposure is unaffected by basis: any 1,523 NVDA shares are the same
US-situs value.

**Consequence for agents: never compute the Israeli §102 liability from Schwab's
`RealizedGainLoss`.** It is the US-basis number. Computing the Israeli figure
requires the grant-date price for each grant. That price is DERIVABLE — see the
30-trading-day-mean section above — so there is no excuse for substituting the
Schwab number.

**Historical six-sale reconciliation, corrected 2026-09-13:** January 28 through
August 12 totals **3,940 shares**, not the previously quoted 3,937. The February
6 export contains six continuation lots totalling 520; the old memo omitted the
last 1-share and 2-share lots. Using every lot's exported sale price and the
trustee simulation benchmarks (182406: $18.3305; 213000: $18.1159) gives a
**pre-expense USD capital-slice estimate of $734,698.766**. The sum of Schwab's
separate US-basis `RealizedGainLoss` fields is **$473,169.36**, not the Israeli
tax base. These are historical USD calculations, **not final ILS taxable income**:
sale-date FX, permitted expenses and actual certificates still govern that
reconciliation. The prior single-FX ILS figure is withdrawn, not a new reserve.

### Withholding: the trustee DOES withhold — in the wire leg, not at Schwab

An earlier revision of this section (2026-08-22, same day) concluded "no
withholding at sale" from Schwab's empty `Taxes` field and its `Forced
Disbursement` equal to the full gross. **That was wrong and would have had the
household reserve ~ILS 644,000 it does not owe.** Schwab does not perform Israeli
withholding; the **trustee** does, between the Schwab disbursement and the wire
that lands at Leumi. The evidence is in the bank statement, not the broker one.

Every 2026 sale, matched to its incoming Leumi USD wire:

| Sale | Proceeds after broker commissions $ | Wired $ | Proceeds-minus-wire $ | % of proceeds | Pre-expense §102 estimate $ | gap / estimate |
|---|---|---|---|---|---|---|
| 01-28 | 107,144.75 | 77,768.88 | 29,375.87 | 27.4% | 96,880 | 30.3% |
| 02-06 | 91,826.70 | 66,554.31 | 25,272.39 | 27.5% | 82,295 | 30.7% |
| 04-20 | 207,538.02 | 150,864.02 | 56,674.00 | 27.3% | 188,479 | 30.1% |
| 05-08 | 121,005.00 | 88,253.43 | 32,751.57 | 27.1% | 110,743 | 29.6% |
| 06-01 | 153,947.69 | 112,229.99 | 41,717.70 | 27.1% | 141,120 | 29.6% |
| 08-12 | 125,325.31 | 93,350.08 | 31,975.23 | 25.5% | 115,183 | 27.8% |
| **TOTAL** | **806,787.47** | **589,020.71** | **217,766.76** | **27.0%** | **734,699** | **29.6%** |

The proceeds-minus-wire column is **not certified tax withholding**: it may
include trustee/wire charges. The $13.29 broker commissions were already deducted
before the sale-header proceeds of $806,787.47; exported prices times shares
total $806,800.76 before those commissions. Do not deduct them twice. The prior
single-FX comparison of ILS651,340 against
ILS644,390 is withdrawn as a current tax reconciliation. Neither a near match
nor the blended gap ratio proves the benchmark or which tax components were
withheld. The settled 30-trading-day benchmark rests on the per-grant
trustee reconciliation, not proximity to a surtax band. Preserve the distinction
between total credited withholding and final annual liability below; the ratio
alone neither proves surtax payment nor establishes additional unpaid tax.

**Consequences for agents.** (a) Never conclude "nothing was withheld" from
broker fields alone: for §102 shares the withholding is invisible at the broker
and must be reconciled across trustee wires and employer payroll. (b) The old
inference that the wire covered only capital tax, leaving roughly ILS100,000
unpaid, was **superseded by the household reconciliation below**; it is not an
operative debt estimate. A wire gap alone does not allocate all withholding
between tax components or prove a final balance. (c) Reconcile per-sale
certificates, Form106 and sale-month payroll before changing any reserve.

### Form 106 closes it — both slices are withheld and reported by the employer

The 2025 Form 106 (`Resources/2025/106/`) is the authoritative reconciliation and
it settles the open questions:

| Line | 2025 |
|---|---|
| משכורת (salary) | ILS 745,431 |
| **שווי הטבה לפי סעיף 102 — מסלול הכנסת עבודה** | **ILS 411,704** |
| **רווח הון מנייר ערך (אחרי תיקון 132)** | **ILS 1,327,411** |
| **תמורה ממכירות ני"ע לפי סעיף 102** | **ILS 1,790,099** |
| מס הכנסה withheld | **ILS 763,650** |

Three conclusions:

1. **The ordinary slice IS taxed at sale** — it appears as employment income in
   the year of SALE, confirming the Lapse-row evidence above.
2. **Both slices are withheld and reported by the employer** under the settled
   household reconciliation. Do not create a duplicate reserve merely because
   Schwab's Israeli-tax fields are empty. Form106 is not a final annual tax
   assessment: its filing notice and the annual return still matter. Neither
   general surtax collection guidance nor that notice proves extra unpaid tax;
   reconcile actual credited withholding before changing the settled reserve.
   ITA instruction 05/2025 §4 excludes additional tax from §91(d) advances and
   directs assessing officers not to include that liability when setting the
   described withholding/advance rates. This statutory collection rule is not
   a transaction-level allocation of the household's actual credited payments;
   do not infer either surtax payment or a new unpaid balance from a blended
   wire ratio. Obtain the relevant certificates and annual reconciliation.
3. **The model reproduces:** capital 1,327,411 + ordinary 411,704 = 1,739,115
   against 1,790,099 of proceeds = **97.2%**. The aggregate gap does not by itself
   identify fees or prove a particular benchmark convention; the benchmark rests
   on the per-grant trustee reconciliation, not this near-equality.

**Semiannual reporting: not applicable.** The Jan-Jun/Jul-Dec advance-reporting
obligation exists where tax is NOT withheld at source. Here it is — trustee
withholding in the wire leg plus employer reporting on the 106. An earlier note
in this file flagged the passed 31-July-2026 deadline as a possible breach; the
106 mechanism dismisses it. Confirm the same lines appear on the 2026 form when
it issues (~March 2027).

**Filing timing — superseded by the above, retained for the reasoning.** Where full tax was not withheld on
marketable securities, Israel generally requires SEMIANNUAL reporting and
advances — Jan-Jun due 31 July, Jul-Dec due 31 January. **31 July 2026 has
passed.** Whether trustee withholding discharged that obligation must be
established from the certificates.

## Stack with related rates

For an NVDA tranche realized after the 24-month period:

| Layer | Rate | Notes |
|---|---|---|
| Statutory Section 102 Capital | 25% | On the capital slice above the settled 30-trading-day pre-grant benchmark, subject to applicable conditions; not broker/vest basis. The ordinary slice is taxed separately. |
| `mas yesef` general (above ₪721,560) | +3% | Almost always applies given the user's salary alone |
| Additional capital-source surtax (above ₪721,560) | +2% | Triggers when capital-source income alone exceeds threshold |
| US WHT on the capital gain | 0% | NRA capital-gain exemption |
| NI / health on the capital gain | 0% | CG not subject to NI |
| **Marginal effective in surtax zone** | **30%** | The number to use for Plan v2.0 NVDA tranche modeling |

## Sources

- [NASPP — Hiring in Israel: How Section 102 Shapes Equity Compensation](https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation) — accessed 2026-06-02
- [VestingStrategy — Israel Equity Compensation Tax: Stock Options, RSUs & Section 102](https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide) — accessed 2026-06-02
- [RNC Law — ESOP 102 vs. 3(i): Tax Routes for Israeli Employee Stock Options](https://www.rnc.co.il/employee-stock-options-israel/) — accessed 2026-06-02
- [PwC Israel — Individual — Income determination](https://taxsummaries.pwc.com/israel/individual/income-determination) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — re-verify the 25% rate, the 24-month-from-grant-date rule (Amendment 147), the trustee filing mechanism, and the capital/ordinary split for public-company RSUs.
- **On reform** — Section 102 has been amended multiple times (notably the 2003 reform that introduced the two-track regime). Any further amendment triggers immediate refresh.

## Open issues

- **Section 102 split for joint accounts with non-Israeli spouse** — out of scope for this file; needs a future `married_couple_102.md` if it becomes operational.
- **Dual residency / split-year** scenarios — out of scope.
- **Trustee fees** typically ~0.1–0.3% of released value; not modeled here.
- Preserve the settled **30-trading-day pre-grant benchmark** and the trustee reconciliation above. Obtain the applicable plan and per-sale certificates for documentary scope/withholding checks; this is not a reason to substitute grant-day spot, vest FMV or broker basis.
