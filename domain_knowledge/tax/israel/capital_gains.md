---
topic: israel_capital_gains_and_investment_income
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%9E%D7%A1_%D7%A8%D7%95%D7%95%D7%97%D7%99_%D7%94%D7%95%D7%9F
    retrieved: 1900-01-01
    tier: 2
---

# Israeli Capital Gains, Dividends, and Interest

Israel taxes most passive investment income at **flat statutory rates**, separate from the labor-income brackets (`brackets_2026.md`). Different rates apply to *real* (inflation-adjusted) gains versus nominal gains for assets acquired before specific historical dates; for assets acquired after 2003 the simpler "real-gain at 25%" rule generally governs.

> **Verification status:** `last_verified: 1900-01-01`. Numbers below are the long-standing post-2003 statutory regime as cited in `Jacobs_Wealth_Plan.md` and Bogleheads-Israel community guidance. Domain-refresh agent must verify against the Israel Tax Authority before any agent uses these numbers in a live recommendation.

## Headline rates (post-2003 acquisitions, individual residents)

| Income type | Statutory rate | Notes |
|---|---|---|
| Capital gains on securities (real gain) | **25%** | Long- and short-term treated identically; computed on inflation-adjusted basis |
| Capital gains for "substantial shareholder" (≥10% holding) | 30% | Flagged here; not relevant for the user's portfolio of public ETFs/stocks |
| Dividends from publicly-traded shares | **25%** | 30% if "substantial shareholder" |
| Interest on shekel deposits / NIS bonds | **15%** (real) or 25% (nominal) | Flat regardless of bracket |
| Interest on foreign-currency / linked instruments | **25%** | Flat |

The **`tosefet mas` / surtax** (see `surtax.md`) adds an additional 3% on top of investment income above the surtax threshold. So an NVDA-share sale that pushes total income above the surtax threshold faces an effective 28% marginal CGT, not 25%.

## Withholding at source

- Israeli broker/bank custodians (e.g., Leumi) withhold 25% on capital gains and dividends from securities held in Israeli accounts.
- Foreign-broker accounts (e.g., Schwab) do *not* automatically withhold Israeli tax; the resident must file and pay via annual `דוח שנתי` (`tofes 1301`) and pay the gap.
- US-source dividends at Schwab are subject to 15% US withholding under the US-Israel treaty (W-8BEN required) — see `treaties/us_israel.md`. The Israeli resident then pays the *difference* up to 25% on the Israeli return (10% additional on top of the 15% already withheld in the US), plus surtax if applicable.

## Loss netting and carryforward

- Realized capital losses can be netted against capital gains in the same year.
- Net realized losses can be carried forward indefinitely until offset against future capital gains (verify against current ITA guidance).
- Losses **cannot** be netted against ordinary income (labor, business). Losses on a stock sale at Schwab cannot reduce the user's salary tax bill.
- **Tax-loss harvesting** is therefore a useful tool to stockpile carryforwards before a future large NVDA tranche.

## Real vs nominal gain mechanics

For securities in shekel:
- **Real gain** = (sale price - purchase price) - inflation adjustment of cost basis (CPI between purchase and sale dates).
- For foreign-currency securities, the FX-related gain may be partially exempt as a `שינוי שער חליפין` (exchange-rate change) component depending on the asset class and the exact rule path; rules are intricate and the user should rely on `Resources/Jacobs_Wealth_Plan.md` Appendix B and a tax advisor for actual filings.

## Mas Shevach and Mas Rechisha (real estate)

Capital gains on **Israeli real estate** are *not* taxed at the 25% securities rate; they fall under `Mas Shevach` (`מס שבח`). This file does **not** cover real-estate gains; that lives in a future `real_estate.md`.

## How agents should use this file

- **Cite this file** for every claim about CGT, dividend tax, or interest tax for the user's securities portfolio.
- For the Israeli surtax overlay, also cite `surtax.md`.
- For US-source dividends specifically, also cite `treaties/us_israel.md` for the 15% US WHT layer.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual (January)** — verify rate persists; Israeli budget laws have historically tweaked these (e.g., the "single-tier" rate replaced the older bifurcated long/short scheme in 2003).
- **On legislation** — any Knesset bill touching CGT triggers refresh.

## Open issues

- The user's CSPX/UCITS holdings (Ireland-domiciled) experience the Irish 15% treaty WHT internally rather than US WHT — but the Israeli investor's *outside* WHT is zero on the UCITS distribution itself. Net Israeli tax remains 25% on dividends. This subtle layering is the reason Plan v2.0 prefers UCITS for non-NVDA US exposure.
