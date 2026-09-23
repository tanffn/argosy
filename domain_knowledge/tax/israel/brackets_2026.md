---
title: Israeli Personal Income Tax Brackets — 2026
topic: israel_personal_income_tax_brackets
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-09-13
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
  - https://mysachar.co.il/articles/en/tax-brackets.html
  - https://fs.knesset.gov.il/25/law/25_lsr_12235101.pdf
sources:
  - url: https://www.btl.gov.il/Laws1/00_0103_000000.pdf
    pdf_pages: [42, 43, 67, 120]
    retrieved: 2026-09-13
    tier: 1
    label: "Selected original Ordinance pages: resident/travel/woman credit points, child-credit provisions and section 125B dividend rates; eligibility remains personal"
  - url: https://fs.knesset.gov.il/25/law/25_lsr_12235101.pdf
    retrieved: 2026-09-13
    tier: 1
    note: "Sefer HaChukim 3511, 31 March 2026, printed page 416 (PDF page 4): Chapter C, Amendment 288, effective 1 January 2026"
  - url: https://www.gov.il/BlobFolder/reports/press-income-tax-brackets/he/SalaryDataDetails_tax_bracket_2026.pdf
    retrieved: 2026-09-13
    tier: 1
    note: "Finance Ministry April-2026 publication, page 1: credit point NIS 242/month. Not authority for health tax: page 2 says 5.16%, conflicting with the current BTL 5.17% table."
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.gov.il/BlobFolder/policy/inst-05-2025/he/IncomeTax_inst-05-2025.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.btl.gov.il/English%20Homepage/Insurance/Ratesandamount/Pages/forSalaried.aspx
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.gov.il/BlobFolder/dynamiccollectorresultitem/employers-info-300326-1/he/IncomeTax_IncomeTaxEmployersInfo_employers-info-300326-1.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.malam-payroll.com/wp-content/uploads/2026/01/loachezer2026.pdf
    retrieved: 2026-09-13
    tier: 2
    note: ITA publication mirror; credit points and ceilings, NOT the superseded January bracket table.
  - url: https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
    retrieved: 2026-09-13
    tier: 2
  - url: https://mysachar.co.il/articles/en/tax-brackets.html
    retrieved: 2026-09-13
    tier: 2
---

# Israeli Personal Income Tax Brackets — 2026

## Summary

Israeli personal income tax (`מס הכנסה ליחיד`) is progressive over seven bands from 10% to 50% (the top band is the 47% statutory bracket plus the 3% `mas yesef` surtax). Brackets apply to *annual* taxable income from labor, self-employment, and other "personal-exertion" sources. Passive investment income (capital gains, dividends, interest) is taxed under flat-rate regimes outside this schedule — see `capital_gains.md`. **Tax year 2026 brackets reflect Amendment 288 to the Income Tax Ordinance**, which widened the 20% and 31% bands effective 1 January 2026.

## Rates / brackets / amounts (2026)

### Annual taxable income (NIS) — labor / personal-exertion sources

| Bracket (annual NIS) | Bracket (monthly NIS, ÷12) | Marginal rate |
|---|---|---|
| 0 – 84,120 | 0 – 7,010 | 10% |
| 84,121 – 120,720 | 7,011 – 10,060 | 14% |
| 120,721 – 228,000 | 10,061 – 19,000 | **20%** (widened from 193,800 in 2025 by Amendment 288) |
| 228,001 – 301,200 | 19,001 – 25,100 | **31%** (widened from 269,280 in 2025 by Amendment 288) |
| 301,201 – 560,280 | 25,101 – 46,690 | 35% |
| 560,281 – 721,560 | 46,691 – 60,130 | 47% |
| 721,561 + | 60,131 + | **50%** (= 47% + 3% `mas yesef` surtax — see `surtax.md`) |

Source: the cited ITA 30 March 2026 circular and 2026 helper booklet; PwC Israel Individual Tax Summary (reviewed 29 June 2026). The January booklet's middle brackets are superseded by the March circular; its other ceilings must be checked separately.

### Credit point value (`נקודת זיכוי`, 2026)

- **₪242 per month** = ₪2,904 per year per credit point.
- Subtracted from gross tax due (not from taxable income).
- Typical Israeli resident male: 2.25 points; female: 2.75 points; married parent gets additional points per child under 18.
- Value source: Finance Ministry April-2026 publication, page 1; point eligibility
  is separate from the value and requires its own applicable household facts.

## Application notes

- **Marginal rate needs actual taxable income:** ₪500,000 alone is below the ₪560,280 start of the 47% annual bracket. Apply each slice of additional ordinary income to the actual year-to-date/annual base, not job seniority. Above ₪721,560 total taxable income the ordinary marginal income-tax stack can reach 50%.
- **Amendment 288:** widening the 20%/31% bands reduces tax by up to about ₪420/month under a full-year comparable-income calculation, not ₪800. The user's actual saving depends on taxable income and credits; keep the settled Section 102 classification separate from this bracket calculation.
- **Capital gains stay separate.** This file does *not* govern CGT/dividend/interest rates — those are flat 25%/30% under the schedule in `capital_gains.md`.
- **Personal credit points** reduce the tax bill at the bottom of the schedule; they do **not** offset marginal RSU/CG tax for a high-income earner who has already exhausted them.
- **Confidence:** use the current verification report and source edition. A historical stamp is not a blanket high-confidence instruction after corrections or amendments.

## Stack with related rates

| Income source | Statutory marginal at top | + Surtax (`mas yesef`) | + NI/health | Final marginal |
|---|---|---|---|---|
| Labor (including the Section 102 ordinary slice at sale, not vest) | 47% | + 3% above ₪721,560 | Apply actual payroll/NI ceiling facts; do not infer from job title | **~50% income tax**, NI separately if applicable |
| Capital gain (NVDA tranche, 102 capital) | 25% | + 3% (general) + 2% (capital-source) above ₪721,560 = **+ 5%** | None (CG exempt) | **~30%** |
| Ordinary US-source portfolio dividend, eligible individual | 25% Israeli base; US treaty ceiling 25%, credit subject to limitations | 3% general / 2% capital each uses its applicable annual threshold | None | No automatic 10% base-tax gap; net tax depends on usable credits and annual facts |

## Sources

- [PwC Israel — Individual — Taxes on personal income](https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income) — accessed 2026-06-02
- [Mysachar — How Israeli Tax Brackets Work, A 2026 Guide](https://mysachar.co.il/articles/en/tax-brackets.html) — accessed 2026-06-02
- [Knesset — Sefer HaChukim 3511, Amendment 288](https://fs.knesset.gov.il/25/law/25_lsr_12235101.pdf) — March 31, 2026, printed page 416; captured 2026-09-12

## Refresh cadence

- **Annual (January)** — Knesset adjusts brackets and credit-point values. Re-verify against `taxes.gov.il` and PwC tax summary.
- **Ad-hoc** — any mid-year amendment (budget law) triggers immediate refresh.

## Open issues

- Joint-filing and married-couple credit-point interactions are not modeled here.
- Real-estate `Mas Shevach` is out of scope; covered in a future `real_estate.md`.
