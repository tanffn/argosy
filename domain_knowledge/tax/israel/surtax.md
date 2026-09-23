---
title: Israeli Surtax on High Income (`מס יסף` / mas yesef) — 2026
topic: israel_surtax_high_income
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-09-13
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
  - https://taxsummaries.pwc.com/israel/individual/income-determination
  - https://barlaw.co.il/practice_areas/tax/client_updates/high-income-earners-in-israel-prepare-to-pay-an-additional-surtax/
sources:
  - url: https://www.gov.il/BlobFolder/policy/inst-05-2025/he/IncomeTax_inst-05-2025.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-lbi/tax-treaty-table-1.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.malam-payroll.com/wp-content/uploads/2026/01/loachezer2026.pdf
    retrieved: 2026-09-13
    tier: 2
    note: ITA 2026 booklet, primary authorship mirrored by payroll provider; separate from the post-March income-tax bracket amendment.
  - url: https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
    retrieved: 2026-09-13
    tier: 2
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-09-13
    tier: 2
  - url: https://barlaw.co.il/practice_areas/tax/client_updates/high-income-earners-in-israel-prepare-to-pay-an-additional-surtax/
    retrieved: 2026-09-13
    tier: 2
---

# Israeli Surtax on High Income (`מס יסף` / mas yesef)

## Summary

The Israeli high-income surtax (`mas yesef`, also called `tosefet mas`) has **two layers** as of 2025–2026:

1. A **3% general surtax** on total annual taxable income above the threshold (all sources combined).
2. A **2% additional capital-source surtax** introduced for 2025 onward, layered on top of (1) when the *capital-source* portion of annual income alone exceeds the same threshold.

Maximum combined surtax on capital-source income above threshold: **5%** (3% + 2%). This raises the effective marginal CGT/dividend rate from 25% to ~**30%** in the surtax zone, not 28% as cached in pre-2025 lore. The Codex audit specifically flagged this as a HARD ship blocker for any tax-consequential trade calculation.

## Rates / brackets / amounts (2026)

| Layer | Rate | Threshold (2026, NIS/year) | Applies to |
|---|---|---|---|
| `mas yesef` general | **3%** | 721,560 | Total annual taxable income from all sources, on the portion above the threshold |
| Additional capital-source surtax | **2%** | 721,560 | The portion of annual *capital-source* income (CG, dividends, interest, real-estate appreciation, rental, royalties, CPI-linkage differentials) above the threshold |
| **Combined max on capital income above threshold** | **5%** | — | Sits on top of statutory 25% CGT → ~30% effective |

Source: ITA implementation instruction 5/2025 (Section 121B / Amendment 276), the ITA 2026 booklet, PwC Israel and Barnea Law. The former CWS article redirects to a marketing homepage and is no longer supporting evidence.

Notes on the threshold:
- The ₪721,560/year threshold corresponds to ₪60,130/month. Re-verify the applicable indexation/freeze legislation each year; it is not an automatic wage-growth adjustment.
- The 2026 threshold is unchanged from 2025 under the Section 120B temporary freeze for 2025–2027, corroborated by the cited ITA sources.
- The 2% additional capital-source surtax was introduced effective tax year **2025** and remains in force for 2026.

## Application notes

### How the surtax stacks on the user's two main events

**Salary + RSU ordinary slice (labor income)**
- Once total annual income passes ₪721,560, marginal labor NIS face: 47% statutory + 3% `mas yesef` = **50%** marginal income tax.
- The 2% additional capital-source layer does **not** apply to labor income.

**NVDA tranche sale (capital gain under 102 Capital)**
- 25% statutory CGT (`capital_gains.md`).
- The 3% layer applies to the excess of total annual taxable income above
  ₪721,560. Attribute incremental surtax to a sale by comparing annual tax with
  and without that sale; do not charge 3% on the entire gain merely because the
  sale crosses the threshold.
- The 2% layer applies to the excess of total annual capital-source income
  above ₪721,560, including relevant dividends/interest and other capital income,
  not just this tranche's capital gain. Use the same incremental calculation.
- For a NVDA tranche realizing > ₪721,560 of capital gain in a year, the effective marginal CGT in the surtax zone = **25% + 5% = 30%**.

**US-source dividends (Schwab)**
- 25% statutory Israeli dividend base tax; ordinary US portfolio dividends for an eligible Israeli individual face a 25% US treaty ceiling, with foreign-tax relief subject to Israeli limitations (`treaties/us_israel.md`). Do not assume a 15% US rate or automatic 10% Israeli top-up.
- Same 3% + 2% surtax stack applies on the portion above threshold → max 30% effective.

### Worked example — NVDA tranche, 2026 (Codex-audit-quality)

Illustration of the **capital slice only**, not a complete sale-tax estimate: assume 2,000 NVDA shares sold @ $200, a settled 30-trading-day pre-grant benchmark of $50, FX 2.94 NIS/USD, no other capital-source income in the year, and salary already above ₪721,560. The ordinary slice and any tax on it are excluded from this example; $50 is an illustrative benchmark, not vest FMV or broker basis.

- Gross gain (USD): 2,000 × ($200 − $50) = $300,000
- Gross gain (NIS): $300,000 × 2.94 = ₪882,000
- 25% statutory CGT on full ₪882k = **₪220,500**
- 3% `mas yesef` general surtax — since combined income > ₪721,560, the full capital gain sits in the surtax zone → 3% × ₪882,000 = **₪26,460**
- 2% additional capital-source surtax — capital-source income alone (₪882,000) exceeds ₪721,560 by ₪160,440, so 2% × ₪160,440 = **₪3,209**
- **Israeli tax attributable to this capital slice:** ~₪250,169 (~**28.4%** of the capital gain; marginal capital-gain NIS above threshold = 30%). Add ordinary-slice tax separately for a complete tranche calculation.

The 2% additional layer is small in absolute terms on a single tranche but compounds materially across multiple quarterly tranches in the same calendar year.

## Stack with related rates

See `brackets_2026.md` for the labor stack and `capital_gains.md` for the capital stack.

## Sources

- [PwC Israel — Individual — Taxes on personal income](https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income) — accessed 2026-06-02
- [PwC Israel — Individual — Income determination](https://taxsummaries.pwc.com/israel/individual/income-determination) — accessed 2026-06-02
- [ITA — implementation instruction 5/2025](https://www.gov.il/BlobFolder/policy/inst-05-2025/he/IncomeTax_inst-05-2025.pdf) — captured 2026-09-12; primary basis for the two surtax layers and 2025–2027 threshold freeze.
- [Barnea — High-Income Earners in Israel Prepare to Pay an Additional Surtax](https://barlaw.co.il/practice_areas/tax/client_updates/high-income-earners-in-israel-prepare-to-pay-an-additional-surtax/) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — recheck legislation; the current Section 120B temporary provision freezes the Section 121B threshold for 2025–2027. Do not apply automatic wage-growth indexation.
- **On legislation** — any amendment touching surtax (the 2% layer was a 2025 amendment) triggers immediate refresh.

## Open issues

- The exact apportionment rule when capital-source income alone is below ₪721,560 but combined income exceeds the threshold is described above per Barnea/PwC. Edge cases (e.g., losses in one category offsetting another) should be verified with a tax advisor for any single transaction above $100k of taxable gain.
- Pension/savings exemptions to the surtax base exist; not modeled here.
