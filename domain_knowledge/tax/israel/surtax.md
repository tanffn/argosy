---
title: Israeli Surtax on High Income (`מס יסף` / mas yesef) — 2026
topic: israel_surtax_high_income
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-06-02
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
  - https://taxsummaries.pwc.com/israel/individual/income-determination
  - https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/
  - https://barlaw.co.il/practice_areas/tax/client_updates/high-income-earners-in-israel-prepare-to-pay-an-additional-surtax/
sources:
  - url: https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
    retrieved: 2026-06-02
    tier: 1
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/
    retrieved: 2026-06-02
    tier: 2
  - url: https://barlaw.co.il/practice_areas/tax/client_updates/high-income-earners-in-israel-prepare-to-pay-an-additional-surtax/
    retrieved: 2026-06-02
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

Source: PwC Israel Individual Tax Summary; PwC Income Determination; CWS Israel 2026 Tax Changes Guide; Barnea Law client update on the additional 2% surtax.

Notes on the threshold:
- The ₪721,560/year threshold corresponds to ₪60,130/month. Indexed annually to wage growth — re-verify each January.
- The 2026 threshold is unchanged from 2025 per PwC (last reviewed 1 Jan 2026).
- The 2% additional capital-source surtax was introduced effective tax year **2025** and remains in force for 2026.

## Application notes

### How the surtax stacks on the user's two main events

**Salary + RSU ordinary slice (labor income)**
- Once total annual income passes ₪721,560, marginal labor NIS face: 47% statutory + 3% `mas yesef` = **50%** marginal income tax.
- The 2% additional capital-source layer does **not** apply to labor income.

**NVDA tranche sale (capital gain under 102 Capital)**
- 25% statutory CGT (`capital_gains.md`).
- If the *combined* income for the year is above ₪721,560 → +3% general surtax on the capital gain.
- If the capital-gain portion *alone* exceeds ₪721,560 → +2% additional capital-source surtax on the excess.
- For a NVDA tranche realizing > ₪721,560 of capital gain in a year, the effective marginal CGT in the surtax zone = **25% + 5% = 30%**.

**US-source dividends (Schwab)**
- 25% statutory Israeli dividend tax (after crediting 15% US WHT under the treaty — see `treaties/us_israel.md`).
- Same 3% + 2% surtax stack applies on the portion above threshold → max 30% effective.

### Worked example — NVDA tranche, 2026 (Codex-audit-quality)

Assume: 2,000 NVDA shares sold @ $200, FMV-at-vest cost basis $50, FX 2.94 NIS/USD, salary already pushed total income past ₪721,560 for the year.

- Gross gain (USD): 2,000 × ($200 − $50) = $300,000
- Gross gain (NIS): $300,000 × 2.94 = ₪882,000
- 25% statutory CGT on full ₪882k = **₪220,500**
- 3% `mas yesef` general surtax — since combined income > ₪721,560, the full capital gain sits in the surtax zone → 3% × ₪882,000 = **₪26,460**
- 2% additional capital-source surtax — capital-source income alone (₪882,000) exceeds ₪721,560 by ₪160,440, so 2% × ₪160,440 = **₪3,209**
- **Total Israeli tax on this tranche:** ~₪250,169 (~**28.4%** effective on this tranche; marginal NIS above threshold = 30%)

The 2% additional layer is small in absolute terms on a single tranche but compounds materially across multiple quarterly tranches in the same calendar year.

## Stack with related rates

See `brackets_2026.md` for the labor stack and `capital_gains.md` for the capital stack.

## Sources

- [PwC Israel — Individual — Taxes on personal income](https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income) — accessed 2026-06-02
- [PwC Israel — Individual — Income determination](https://taxsummaries.pwc.com/israel/individual/income-determination) — accessed 2026-06-02
- [CWS Israel — Israeli Tax Changes 2026: Complete Guide](https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/) — accessed 2026-06-02
- [Barnea — High-Income Earners in Israel Prepare to Pay an Additional Surtax](https://barlaw.co.il/practice_areas/tax/client_updates/high-income-earners-in-israel-prepare-to-pay-an-additional-surtax/) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — threshold is wage-indexed; rates can be modified by budget law.
- **On legislation** — any amendment touching surtax (the 2% layer was a 2025 amendment) triggers immediate refresh.

## Open issues

- The exact apportionment rule when capital-source income alone is below ₪721,560 but combined income exceeds the threshold is described above per Barnea/PwC. Edge cases (e.g., losses in one category offsetting another) should be verified with a tax advisor for any single transaction above $100k of taxable gain.
- Pension/savings exemptions to the surtax base exist; not modeled here.
