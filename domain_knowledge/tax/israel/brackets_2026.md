---
title: Israeli Personal Income Tax Brackets — 2026
topic: israel_personal_income_tax_brackets
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-06-02
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
  - https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/
  - https://mysachar.co.il/articles/en/tax-brackets.html
  - https://www.gov.il/en/pages/sa120426-1
sources:
  - url: https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/
    retrieved: 2026-06-02
    tier: 2
  - url: https://mysachar.co.il/articles/en/tax-brackets.html
    retrieved: 2026-06-02
    tier: 2
  - url: https://www.gov.il/en/pages/sa120426-1
    retrieved: 2026-06-02
    tier: 1
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

Source: PwC Israel Individual Tax Summary (last reviewed 1 Jan 2026); CWS Israel 2026 Tax Changes Guide; Mysachar 2026 brackets table; Israel Tax Authority gov.il notice on Amendment 288.

### Credit point value (`נקודת זיכוי`, 2026)

- **₪242 per month** = ₪2,904 per year per credit point.
- Subtracted from gross tax due (not from taxable income).
- Typical Israeli resident male: 2.25 points; female: 2.75 points; married parent gets additional points per child under 18.
- Source: Mysachar 2026 guide.

## Application notes

- **NVIDIA Israel salary band:** A senior employee with NIS ~500k+ annual gross labor income is firmly in the 47% bracket on marginal RSU income; once total income passes ₪721,560, the marginal rate becomes 50% (47% + 3% surtax).
- **Amendment 288 effect for this user:** The widened 20%/31% bands save ~₪400–800/month at the ₪19k–₪25k/month band but do **not** change the marginal rate on the user's RSU vesting NIS — those marginal NIS land in the 47%/50% bracket regardless.
- **Capital gains stay separate.** This file does *not* govern CGT/dividend/interest rates — those are flat 25%/30% under the schedule in `capital_gains.md`.
- **Personal credit points** reduce the tax bill at the bottom of the schedule; they do **not** offset marginal RSU/CG tax for a high-income earner who has already exhausted them.
- **Confidence:** numbers verified against PwC + Israel Tax Authority publication for tax year 2026. Use `confidence=high` until next annual refresh.

## Stack with related rates

| Income source | Statutory marginal at top | + Surtax (`mas yesef`) | + NI/health | Final marginal |
|---|---|---|---|---|
| Labor (RSU vest ordinary slice) | 47% | + 3% above ₪721,560 | Already ceiling-exhausted at this income | **~50%** |
| Capital gain (NVDA tranche, 102 capital) | 25% | + 3% (general) + 2% (capital-source) above ₪721,560 = **+ 5%** | None (CG exempt) | **~30%** |
| US-source dividend (Schwab) | 25% Israeli (15% US WHT creditable) | + 5% (3% general + 2% capital) above threshold | None | **~30%** (gap to US WHT paid via tofes 1301) |

## Sources

- [PwC Israel — Individual — Taxes on personal income](https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income) — accessed 2026-06-02
- [CWS Israel — Israeli Tax Changes 2026: Complete Guide](https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/) — accessed 2026-06-02
- [Mysachar — How Israeli Tax Brackets Work, A 2026 Guide](https://mysachar.co.il/articles/en/tax-brackets.html) — accessed 2026-06-02
- [Israel Tax Authority — gov.il — Income Tax Amendment 288 notice](https://www.gov.il/en/pages/sa120426-1) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — Knesset adjusts brackets and credit-point values. Re-verify against `taxes.gov.il` and PwC tax summary.
- **Ad-hoc** — any mid-year amendment (budget law) triggers immediate refresh.

## Open issues

- Joint-filing and married-couple credit-point interactions are not modeled here.
- Real-estate `Mas Shevach` is out of scope; covered in a future `real_estate.md`.
