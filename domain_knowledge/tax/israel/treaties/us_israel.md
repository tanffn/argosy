---
title: US-Israel Income Tax Treaty — Investor-Relevant Articles — 2026
topic: us_israel_income_tax_treaty
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual_with_us_source_income
last_verified: 2026-09-13
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
code_references:
  - argosy/orchestrator/loops/annual.py
source_urls:
  - https://www.irs.gov/pub/irs-trty/israel.pdf
  - https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents
  - https://taxsummaries.pwc.com/israel/corporate/withholding-taxes
  - https://www.irs.gov/instructions/iw8ben
sources:
  - url: file://Knowledge/tax/us/estate_tax_nonresidents.md
    label: "Current internal cross-reference only, not independent legal authority"
  - url: file://Knowledge/tax/us/nonresident_withholding.md
    label: "Current internal withholding cross-reference only"
  - url: file://Knowledge/tax/israel/capital_gains.md
    label: "Current internal rate-stack cross-reference; confirm against original ITA sources"
  - url: file://Knowledge/tax/israel/surtax.md
    label: "Current internal surtax cross-reference only"
  - url: https://www.gov.il/BlobFolder/policy/inst-05-2025/he/IncomeTax_inst-05-2025.pdf
    tier: 1
    label: "ITA original 2025 surtax guidance relevant to the Israeli side of the rate stack"
  - url: https://www.btl.gov.il/Laws1/00_0103_000000.pdf
    pdf_pages: [91, 92, 120]
    tier: 1
    label: "Consolidated Income Tax Ordinance: Israeli rate provisions, not treaty entitlement"
  - url: https://www.irs.gov/pub/irs-pdf/iw8ben.pdf
    tier: 1
    label: "Complete W-8BEN instructions: expiry and change-in-circumstances sections omitted from the prior HTML capture"
  - url: https://www.irs.gov/individuals/international-taxpayers/firpta-withholding
    tier: 1
    label: "IRS disposition withholding rules and exceptions; no household transaction inferred"
  - url: https://www.irs.gov/instructions/i706na
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-pdf/i706.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/businesses/small-businesses-self-employed/estate-gift-tax-treaties-international
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-lbi/tax-treaty-table-1.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents
    retrieved: 2026-09-13
    tier: 1
  - url: https://taxsummaries.pwc.com/israel/corporate/withholding-taxes
    retrieved: 2026-09-12
    tier: 2
  - url: https://www.irs.gov/instructions/iw8ben
    retrieved: 2026-09-12
    tier: 1
---

# US-Israel Income Tax Treaty (1975; in force 1995) — Investor-Relevant Articles

## Summary

The Convention between the United States and Israel for the Avoidance of Double Taxation governs cross-border income flows between the two jurisdictions. For an Israeli-resident portfolio investor with a US broker (e.g., Schwab), the **practically binding provisions** are:

1. **Article 12 — Dividends:** the general US-source portfolio-dividend ceiling for an eligible Israeli-resident individual is **25%**, not 15%, with valid treaty documentation.
2. **Article 13 — Interest:** the general treaty ceiling is 17.5%; qualifying bank-deposit and portfolio interest can instead be exempt under US domestic law. Article 14 concerns royalties.
3. **Article 15 — Capital Gains:** ordinary portfolio securities gains are generally exempt in the source state, subject to the treaty's exceptions. Under Second Protocol Article X(2), the shareholding exception applies in either direction to direct/indirect ownership of at least 10% of voting power at any time in the preceding 12 months. Other exceptions include real property, specified royalty-property gains, permanent-establishment profits and the individual's 183-day presence test. Do not infer exemption for real estate, partnerships or employment compensation from a ticker or broker alone.
4. **Form W-8BEN:** generally valid through the end of the third calendar year after signing, unless a change in circumstances makes it incorrect. Missing valid documentation can cause statutory withholding.
5. **The treaty does NOT cover estate tax.** The ordinary NRNC credit is **$13,000**; **$60,000** is a filing threshold, not a deduction — see "Estate tax" below.

## Rates / brackets / amounts (2026)

### Treaty WHT rates on US-source payments to Israeli residents

| Income type | Treaty Article | US WHT under treaty | Statutory NRA rate (without treaty) | Practical effect |
|---|---|---|---|---|
| Portfolio dividends — individual | Article 12(2)(a) | **25%** | 30% | Valid treaty documentation and eligibility required |
| Qualifying corporate recipient | Article 12(2)(b), as amended | 12.5% subject to conditions | 30% | Not the rate for an individual; voting ownership alone is insufficient |
| Interest — general | Article 13 | 17.5% | 30% | Qualifying portfolio/deposit interest can be exempt under domestic law |
| Capital gains — portfolio securities | Article 15 (in conjunction with US NRA rule) | **0% US** | 0% US for NRA portfolio CG | Israel-only taxation (`section_102.md`, `capital_gains.md`) |
| Real estate (FIRPTA) | Article 15 / domestic §1445 | Generally 15% of amount realized, with exceptions | Withholding is not final tax; taxpayer-specific computation | Outside ordinary listed-portfolio gains; see `tax/us/nonresident_withholding.md` |
| Private pensions and annuities | Article 20 | depends on classification | varies | Outside this portfolio-income summary; Article 19 is not the private-pensions article |

Sources: IRS Israel treaty text (1975 convention); IRS Israel Tax Treaty Documents landing page; PwC Israel Corporate WHT page; IRS Form W-8BEN instructions.

**Source interpretation:** the IRS PDF includes historical introductory correspondence describing 15%. That is not the operative general dividend rate. Read Article 12(2)(a), the protocols, and the IRS treaty-table Israel row together. The 1993 protocol's Article VII applies the general rate to US regulated investment company dividends; specially designated interest-related distributions can have domestic-law exemptions. Do not apply the ordinary-dividend rate to every SGOV distribution without its actual tax classification.

## Application notes

### Form W-8BEN — the procedural choke point

- Ordinary US dividends are generally subject to **30% statutory NRA withholding** absent treaty relief. The eligible individual's treaty ceiling is **25%**; maintain a valid **W-8BEN**. Actual Schwab processing must be checked against account records, not assumed from this legal summary.
- **Validity period:** the form is valid through the **end of the third calendar year after signing**. Signed in 2024 → valid through 31-Dec-2027.
- **Recovery from lapse:** refresh documentation and investigate broker reimbursement/set-off procedures or a **Form 1040-NR** refund as applicable. For an ordinary dividend eligible for 25%, 30% withholding is a **5-percentage-point** excess, not 15 points. Do not declare over-withholding permanently lost without checking correction/refund options.
- **Current implementation:** the annual loop emits a generic W-8BEN refresh
  reminder. No signature-date capture or date-driven twelve-month expiry check
  was found in the current intake/annual code. Do not claim the system has
  verified the account's current form or will automatically catch its expiry.
  Account status/signature-date tracking is unfinished work; use the actual
  Schwab record for account-specific advice.

### Israeli-side mechanics — Foreign Tax Credit reconciliation

For an ordinary dividend with 25% US withholding, Article 26 provides Israeli foreign-tax relief subject to Israeli law and its credit limitations:

1. The Israeli resident reports the gross dividend (in NIS) on the annual `דוח שנתי` (`tofes 1301`).
2. The Israeli statutory dividend rate is **25%** for individuals (`capital_gains.md`).
3. Under the simplified assumption of a fully usable 25% foreign-tax credit against a 25% Israeli base liability, the additional Israeli base tax is zero; combined base tax remains 25%, not zero.
4. There is **no automatic 10% Israeli top-up** under that assumption. Actual credits depend on income classification, evidence and Israeli limitations.
5. Apply applicable surtax separately using `surtax.md`. This treaty summary does not establish blanket creditability, carryforward or surtax-credit treatment for a specific return.

**Over-withholding:** do not assume the full 30% is creditable in Israel or permanently lost. Establish the treaty-permitted US liability, available US correction/refund, and Israeli credit ceiling independently.

### Capital gains on US securities — Israel-only

- An ordinary NRA portfolio sale can qualify for US exemption. A broker location or NVDA ticker does not prove residency, presence, source-of-compensation or treaty eligibility. Preserve the separately evidenced Section 102 treatment; this summary does not reclassify employee compensation.
- The gain is fully taxable in Israel at the 25% statutory CGT (`capital_gains.md`), plus the surtax stack (`surtax.md`), and — if the shares are Section 102 RSU-origin — also the Section 102 ordinary/capital split (`section_102.md`).

## Estate tax — outside the income treaty, urgent for HNW portfolio

- **There is no US-Israel estate tax treaty.**
- Stock of **US corporations** (including typical US corporate ETFs) is generally US-situs, regardless of listing/custodian. Qualifying US bank deposits have a statutory exclusion, not a small-dollar exemption; see `estate_tax_nonresidents.md` for conditions and cash-versus-fund distinctions.
- In the ordinary nonresident-not-citizen case, the unified credit is **$13,000**, corresponding to tax on $60,000 in a simple case. The $60,000 filing test includes relevant gifts; it is not a deduction from the estate before applying the brackets.
- Compute exposure from ownership records and a stated death-date FMV scenario. Do not reuse a dated NVDA valuation or subtract $60,000 before applying the graduated schedule; see the worked computation in `estate_tax_nonresidents.md`.
- This is the dominant reason Plan v2.0 favors **UCITS (Ireland-domiciled) ETFs** (e.g., CSPX) over US-domiciled ETFs (e.g., VOO) for non-NVDA US-equity exposure: Ireland-domiciled funds are **non-US-situs** and avoid US estate-tax exposure entirely.
- `domain_knowledge/tax/us/estate_tax_nonresidents.md` exists and supplies the separate estate computation and mitigation discussion; it is explicitly attached above for cross-reference checks. Section 102 RSUs cannot be exchanged for UCITS without addressing realization. Do not resurrect the obsolete “file may be missing” task.

## Stack with related Israeli files

| Scenario | Treaty layer | Israeli layer (`capital_gains.md` / `section_102.md` / `surtax.md`) | Net effective |
|---|---|---|---|
| Ordinary US dividend, eligible individual with W-8BEN | 25% US WHT | 25% Israeli base, credit subject to limitations; surtax separately | 25% base if credit fully usable; additional surtax depends on annual facts |
| Ordinary US dividend without current documentation | Possible 30% statutory withholding | Establish actual allowable credit and surtax separately | Check broker/IRS correction or refund; do not presume permanent loss |
| Schwab NVDA sale (Section 102 Capital) | 0% US | 25% Israeli + 3%/2% surtax | ~30% in surtax zone |
| Schwab NVDA sale (102 holding broken) | 0% US | up to 50% (ordinary income reclassification) + NI/health up-to-ceiling | up to ~50% |
| Death holding US corporate shares | NRA estate-tax exposure on death-date FMV | Israeli treatment separately | Graduated US computation less applicable credit; **not** 40% times FMV minus $60k |

## Sources

- [IRS — Israel 1975 Income Tax Convention text (PDF)](https://www.irs.gov/pub/irs-trty/israel.pdf) — accessed 2026-06-02
- [IRS — Israel Tax Treaty Documents landing page](https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents) — accessed 2026-06-02
- [PwC — Israel Corporate — Withholding taxes](https://taxsummaries.pwc.com/israel/corporate/withholding-taxes) — accessed 2026-06-02
- [IRS — Instructions for Form W-8BEN](https://www.irs.gov/instructions/iw8ben) — accessed 2026-06-02

## Refresh cadence

- **Bi-annual on treaty articles** — the 1975 convention is stable; no recent renegotiation.
- **Annual on procedural items** — re-verify W-8BEN validity and the applicable statutory/treaty rate by income category.
- **Trigger** — any IRS Treaty Update circular or any change to Israeli FTC computation rules.

## Open issues

- `domain_knowledge/tax/us/estate_tax_nonresidents.md` supplies the separate estate-tax computation; refresh its sources and compute death-date exposure instead of using a flat deduction shortcut.
- The 1975 treaty does **not** address Section 102 RSU mechanics (purely domestic Israeli law — `section_102.md`).
- Pensions / 401(k) / IRA classification and Article 20 treatment are outside this portfolio summary; Article 19 is not the private-pensions article.
- Possible future renegotiation: there have been periodic discussions of updating the 1975 treaty; track via IRS Treaty Update notices.
