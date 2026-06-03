---
title: Israeli Capital Gains, Dividends, and Interest — 2026
topic: israel_capital_gains_and_investment_income
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-06-02
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://taxsummaries.pwc.com/israel/individual/income-determination
  - https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
  - https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/
sources:
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-06-02
    tier: 1
  - url: https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/
    retrieved: 2026-06-02
    tier: 2
---

# Israeli Capital Gains, Dividends, and Interest

## Summary

Israel taxes investment income for individual residents at **flat statutory rates**, separate from the labor-income progressive schedule (`brackets_2026.md`). The standard rate is **25%** on the *real* (inflation-adjusted) capital gain for securities purchased on or after 1 January 2003. A **30%** rate applies to "substantial shareholders" (≥10% holding) on both capital gains and dividends. The 2026 surtax overlay (`surtax.md`) can add up to **5%** (3% general + 2% capital-source) on the portion of capital income above ₪721,560/year, taking the effective marginal CGT to **30%** in the surtax zone.

## Rates / brackets / amounts (2026)

### Statutory rates — individual residents, post-2003 acquisitions

| Income type | Statutory rate | Source / notes |
|---|---|---|
| Capital gains on securities — real gain | **25%** | PwC: "real gain ... is generally taxed at a rate of 25% for individuals" |
| Capital gains — "substantial shareholder" (≥10% stake at sale or in prior 12 months) | **30%** | PwC; not relevant for ETF/public-stock retail portfolio |
| Dividends — publicly-traded shares (individual) | **25%** | PwC: "Dividends shall generally be subject to tax at the rate of 25%" |
| Dividends — substantial shareholder | **30%** | PwC |
| Interest — general (capital-source) | **25%** | PwC |
| Interest — traded securities / financial-institution deposits not CPI-linked | **15%** | PwC preferential rate |
| **Inflationary component of pre-1994 gains** | 10% | Pre-1994 portion taxed at 10%; post-1994 inflationary component is exempt |

Source: PwC Israel — Individual — Income determination (last reviewed 1 January 2026).

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
- Foreign-currency securities (e.g., NVDA at Schwab): rules treat FX-related component specially; consult a tax advisor for any high-stakes filing. The user's Plan v2.0 Appendix B covers the practical Schwab→Israeli-return reconciliation.

### Withholding at source

- **Israeli custodians** (Leumi, Discount, Mizrahi-Tfahot): withhold 25% on capital gains and dividends from securities held in Israeli accounts at the time of realization.
- **Foreign custodians** (Schwab, Interactive Brokers, etc.): do **not** withhold Israeli tax. The Israeli resident files annually (`דוח שנתי` / tofes 1301) and pays the gap.
- **US-source dividends at Schwab:** 15% US WHT under the US-Israel treaty (W-8BEN required — `treaties/us_israel.md`). Israeli resident credits the 15% against the 25% Israeli liability and pays the 10% gap on the Israeli return; surtax (+3% / +2%) is paid on top with no US credit because the surtax sits above the 25%.

### Loss netting and carryforward

- Realized capital losses net against capital gains in the **same year** (across securities).
- Net realized losses carry forward **indefinitely** until offset against future capital gains.
- **Capital losses cannot offset ordinary income** (salary, business, RSU ordinary slice).
- Tax-loss harvesting is a useful tool to bank carryforwards before a planned large NVDA tranche.

### US-domiciled vs UCITS (Ireland-domiciled) ETFs — dividend layering

- US-domiciled ETFs (e.g., VOO, VTI): 15% US WHT on dividend → 25% Israeli (credit 15%) → ~25% all-in for dividend tax (plus surtax above threshold).
- UCITS (Ireland-domiciled) ETFs (e.g., CSPX, VWRA): Irish-internal 15% treaty WHT on the underlying US dividends; the Israeli investor receives the *fund's* distribution with **0% additional WHT at the Israeli level externally** but the fund-internal WHT is already a 15% drag. Net effective Israeli external tax remains 25% on the distribution received.
- **The estate-tax angle** (`treaties/us_israel.md`) is the dominant reason Plan v2.0 prefers UCITS for non-NVDA US exposure — not the income-tax angle, which roughly washes out.

### Mas Shevach (real estate)

- Capital gains on **Israeli real estate** are **not** taxed under this 25% securities regime. They fall under `Mas Shevach` (`מס שבח`) with its own bracketing and indexation rules. Out of scope here.

## Stack with related rates

| Scenario | Israeli statutory | US WHT | Surtax stack | Net effective |
|---|---|---|---|---|
| NVDA tranche sale (102 Capital, post-24-mo) | 25% | 0% (NRA capital gain) | +3% +2% above threshold | **~30%** marginal in surtax zone |
| US-listed ETF dividend at Schwab | 25% (15% US WHT creditable) | 15% withheld | +3% +2% on Israeli return | **~30%** marginal in surtax zone |
| UCITS distribution (CSPX) | 25% | 0% external (15% internal drag) | +3% +2% | **~30%** marginal in surtax zone |
| Substantial-shareholder dividend | 30% | varies | +3% +2% | up to 35% in surtax zone |

## Sources

- [PwC Israel — Individual — Income determination](https://taxsummaries.pwc.com/israel/individual/income-determination) — accessed 2026-06-02
- [PwC Israel — Individual — Taxes on personal income](https://taxsummaries.pwc.com/israel/individual/taxes-on-personal-income) — accessed 2026-06-02
- [CWS Israel — Israeli Tax Changes 2026: Complete Guide](https://www.cwsisrael.com/israeli-tax-changes-2026-complete-guide/) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — re-verify the 25% / 30% / 15% / 5% surtax stack against PwC + Israel Tax Authority.
- **On legislation** — any Knesset bill touching CGT or surtax triggers refresh.

## Open issues

- Edge-case FX treatment on Schwab-sourced gains where USD strengthens between vest and sale: the FX component allocation between "real gain" and "exchange-rate adjustment" is intricate; defer to a tax advisor for any tranche > $100k of gain.
- The user's CSPX (UCITS) basis tracking through migration events (provider changes, share-class conversions) is not modeled here.
