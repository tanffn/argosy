---
title: Israeli Capital Gains, Dividends, and Interest — 2026
topic: israel_capital_gains_and_investment_income
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-09-13
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://taxsummaries.pwc.com/israel/individual/income-determination
  - https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
catalog_sources:
  - id: 121
    as_of: "2026-02 version; exact day unspecified"
    label: "Existing original Plan v2.0 including Appendix B; historical reference, not current authority"
sources:
  - url: https://www.btl.gov.il/Insurance/National%20Insurance/Pages/כיצד%20נקבע%20בסיס%20ההכנסה%20החייבת%20בדמי%20ביטוח.aspx
    tier: 1
    label: "BTL income-base categories; distinguish section-2 income from investment capital gains"
  - url: https://www.btl.gov.il/Laws1/00_0001_000000.pdf
    pdf_pages: [245, 246]
    tier: 1
    label: "National Insurance Law section350(a)(6), qualifying dividend/interest exclusions; not a general exemption for all passive income"
  - url: https://www.leumi.co.il/he/capital-market-tax-reform
    retrieved: 2026-09-12
    tier: 1
    label: "Leumi's own securities-tax withholding explanation; resident-individual scope and 25%/15% distinctions, not a personal tax assessment"
  - url: https://www.discountbank.co.il/private/savings-investments/securities-taxation/updates/
    retrieved: 2026-09-12
    tier: 1
    label: "Discount's own securities withholding guidance, successfully captured despite web-tool 404; distinguishes resident-individual rates and exceptions"
  - url: https://www.btl.gov.il/Laws1/00_0103_000000.pdf
    pdf_pages: [90, 91, 92, 93, 120]
    retrieved: 2026-09-12
    tier: 1
    label: "BTL-hosted consolidated Income Tax Ordinance: sections 88 and 91 on original PDF pages90-92, section92 loss carryforward on page93; distinguish listed-security exception from general historical-asset apportionment"
  - url: https://www.irs.gov/pub/irs-trty/ireland.pdf
    retrieved: 2026-09-13
    tier: 1
    label: "US-Ireland treaty Articles 3, 4, 10 and 23; 15% portfolio dividend ceiling subject to residence, beneficial ownership and eligibility"
  - url: https://www.ishares.com/uk/individual/en/products/253743/ishares-sp-500-b-ucits-etf-acc-fund
    retrieved: 2026-09-12
    tier: 1
    label: "Original CSPX issuer page: ISIN IE00B5BMR087, accumulating share class, Ireland; not proof of fund-specific US treaty qualification"
  - url: https://fund-docs.vanguard.com/FTSE_All-World_UCITS_ETF_USD_Accumulating_9679_EU_INT_UK_EN.pdf
    retrieved: 2026-09-12
    tier: 1
    label: "Original Vanguard factsheet dated 31 July 2026: VWRA, ISIN IE00BK5BQT80, dividends accumulated, Irish UCITS; not a tax-eligibility certificate"
  - url: https://www.gov.il/BlobFolder/generalpage/income-tax-guide-knowyourright/he/Guides_IncomeTax_da-2025.pdf
    pdf_pages: [48, 49, 50, 84, 85]
    tier: 1
    label: "ITA tax-year-2025 filing guide, published 2026; structural rates, loss offsets and filing rules, not a 2026 bracket edition"
  - url: https://www.gov.il/BlobFolder/policy/inst-05-2025/he/IncomeTax_inst-05-2025.pdf
    tier: 1
    retrieved: 2026-09-12
  - url: https://www.irs.gov/pub/irs-lbi/tax-treaty-table-1.pdf
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-09-13
    tier: 2
  - url: https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
    retrieved: 2026-09-13
    tier: 2
---

# Israeli Capital Gains, Dividends, and Interest

## Summary

Israel taxes investment income for individual residents under statutory investment-income regimes, separate from the labor-income progressive schedule (`brackets_2026.md`). The standard portfolio rate is **25%** on the *real* (inflation-adjusted) capital gain for ordinary listed shares. Acquisition date alone does not establish the treatment of every security: historical acquisition/listing transitions, private assets and employee Section-102 awards require their own rules. A **30%** rate applies to "substantial shareholders" (≥10% holding) on both capital gains and dividends. The 2026 surtax overlay (`surtax.md`) can add up to **5%** (3% general + 2% capital-source) on the portion of capital income above ₪721,560/year, taking the effective marginal CGT to **30%** in the surtax zone.

**Historical listed-share exception:** §91(b1)(1b) excludes securities listed
before the January-1-2012 change date from the general historical-asset linear
apportionment and applies §91(b)(1)/(2). Do not apply a blanket 20%/25% split to
ordinary shares bought in 2003-2011 that were already listed before 2012. Being
listed only at the eventual sale date does not establish this exception.

## Rates / brackets / amounts (2026)

### Standard statutory rates — individual residents; special regimes checked separately

| Income type | Statutory rate | Source / notes |
|---|---|---|
| Capital gains on securities — real gain | **25%** | PwC: "real gain ... is generally taxed at a rate of 25% for individuals" |
| Capital gains — "substantial shareholder" (≥10% stake at sale or in prior 12 months) | **30%** | PwC; not relevant for ETF/public-stock retail portfolio |
| Dividends — publicly-traded shares (individual) | **25%** | PwC: "Dividends shall generally be subject to tax at the rate of 25%" |
| Dividends — substantial shareholder | **30%** | PwC |
| Interest — general (capital-source) | **25%** | PwC |
| Interest — traded securities / financial-institution deposits not CPI-linked | **15%** | PwC preferential rate |
| **Inflationary component of pre-1994 gains** | 10% | Pre-1994 portion taxed at 10%; post-1994 inflationary component is exempt |

Source: PwC Israel — Individual — Income determination (last reviewed 29 June 2026), with the cited ITA filing guidance and surtax instruction for primary confirmation.

### Surtax overlay on top of the above (2026)

| Layer | Rate | Threshold | Effective marginal CGT in surtax zone |
|---|---|---|---|
| `mas yesef` general | +3% | ₪721,560/yr total income | 25% + 3% = 28% |
| Additional capital-source surtax | +2% | ₪721,560/yr capital-source income | 25% + 3% + 2% = **30%** |

See `surtax.md` for full mechanics.

## Application notes

### Real vs nominal gain mechanics

- Real gain = sale price − (cost basis × (CPI at sale ÷ CPI at purchase)).
- The inflationary component accrued **after 1 January 1994** is **exempt**.
- The inflationary component accrued **before 1 January 1994** is taxed at 10%.
- Foreign-currency securities (e.g., NVDA at Schwab): rules treat the FX-related component specially; use the applicable rule and actual transaction/FX records for a filing calculation. The February-2026 Plan v2.0 Appendix B contains illustrative RSU and tax-loss worksheets, not a Schwab-to-Israeli-return FX reconciliation method. The earlier reference claiming otherwise is withdrawn; do not ask the user to supply a document merely to justify that mistaken citation.

### Withholding at source

- **Israeli custodians** generally withhold 25% on taxable real gains and dividends
  from ordinary listed shares for resident individuals, as described in Leumi's
  and Discount's own guidance. Do not apply this blindly to every security or
  account: non-linked debt can use 15%, and exemptions, historical transitions,
  special status and loss offsets affect actual withholding. Reconcile the bank's
  transaction and tax certificates; withholding is not the final annual liability.
- **Ordinary foreign brokerage accounts** generally do not withhold Israeli tax;
  reconcile actual documents and applicable reporting/advance-payment obligations,
  not just the annual return. Section-102 trustee payroll/withholding is a separate
  arrangement: a Schwab display does not mean no Israeli tax was withheld on RSUs.
- **Ordinary US-source portfolio dividends:** the eligible Israeli individual's treaty ceiling is **25%** with valid W-8BEN documentation — `treaties/us_israel.md`. Assuming a fully usable credit against 25% Israeli base tax, additional Israeli base tax is zero, but combined base tax is still 25%. No automatic 10% top-up. Actual broker withholding, available credits and surtax treatment require the relevant account/return facts; this is not a blanket surtax-credit ruling.

### Loss netting and carryforward

- Realized capital losses net against capital gains in the **same year** (across securities).
- Current-year securities losses can also offset eligible securities interest
  and dividends. For income from other securities, the applicable income-tax
  rate must not exceed 25%; this is not a general offset against bank interest.
  Apply the statutory income/security conditions, not merely the income label.
- Unused eligible capital losses carry forward **indefinitely**, subject to
  reporting requirements. Securities losses arising from 2006 onward, once
  carried forward, offset future capital gains (including eligible real-estate
  gains), **not interest/dividends**. Pre-2006 securities losses have separate
  transitional rules; do not apply the post-2006 rule to them automatically.
- **Capital losses cannot offset ordinary income** (salary, business, RSU ordinary slice).
- Tax-loss harvesting is a useful tool to bank carryforwards before a planned large NVDA tranche.

Source: ITA tax-year-2025 filing guide, loss-offset instructions (same text in
the cited PDF edition, PDF pages 48–49 / printed pages 46–47). No household carryforward is established by
this general rule; use actual loss schedules and filing records.

### US-domiciled vs UCITS (Ireland-domiciled) ETFs — dividend layering

- US corporate ETFs' **ordinary dividends** (e.g., VOO/VTI): generally 25% US treaty withholding for the eligible individual. If fully creditable against the Israeli 25% base liability, combined base tax remains 25%; apply surtax separately. Properly designated exempt RIC distributions need their own classification.
- An eligible Irish fund may incur **US** treaty withholding of 15% on underlying US dividends. This is fund-level leakage, not an automatic individual foreign-tax credit. Investor-level tax and timing are separate; accumulating CSPX/VWRA share classes do not pay those reinvested dividends to the investor as cash distributions.
- **No blanket tax neutrality:** compare fund-level leakage, credit eligibility, distribution/accumulation, realization timing and fees alongside estate exposure. The old claim that the income-tax difference washes out was unsupported. Do not change the user's vehicle policy solely from this correction.

### Mas Shevach (real estate)

- Capital gains on **Israeli real estate** are **not** taxed under this 25% securities regime. They fall under `Mas Shevach` (`מס שבח`) with its own bracketing and indexation rules. Out of scope here.

## Stack with related rates

| Scenario | Israeli statutory | US WHT | Surtax stack | Net effective |
|---|---|---|---|---|
| NVDA tranche sale (102 Capital, post-24-mo) | 25% | 0% (NRA capital gain) | +3% +2% above threshold | **~30%** marginal in surtax zone |
| Ordinary US corporate ETF dividend, eligible individual | 25% base, FTC subject to limitations | 25% treaty ceiling | Check annual income and credit rules | 25% combined base if fully creditable; surtax separate |
| Irish UCITS | Depends on distribution versus realized disposal and share class | Fund-level US withholding on underlying income | Check annual facts | Instrument-specific computation; CSPX accumulation is not a cash distribution |
| Substantial-shareholder dividend | 30% | varies | +3% +2% | up to 35% in surtax zone |

## Sources

- [PwC Israel — Individual — Income determination](https://taxsummaries.pwc.com/israel/individual/income-determination) — accessed 2026-06-02
- [PwC Israel — Individual — Taxes on personal income](https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — re-verify the 25% / 30% / 15% / 5% surtax stack against PwC + Israel Tax Authority.
- **On legislation** — any Knesset bill touching CGT or surtax triggers refresh.

## Open issues

- Edge-case FX treatment on Schwab-sourced gains where USD strengthens between vest and sale: the FX component allocation between "real gain" and "exchange-rate adjustment" is intricate; defer to a tax advisor for any tranche > $100k of gain.
- The user's CSPX (UCITS) basis tracking through migration events (provider changes, share-class conversions) is not modeled here.
