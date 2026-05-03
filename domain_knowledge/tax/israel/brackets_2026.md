---
topic: israel_personal_income_tax_brackets
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.taxes.gov.il/IncomeTax/Pages/IncomeTaxBrackets.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%9E%D7%93%D7%A8%D7%92%D7%95%D7%AA_%D7%9E%D7%A1_%D7%94%D7%9B%D7%A0%D7%A1%D7%94
    retrieved: 1900-01-01
    tier: 2
---

# Israeli Personal Income Tax Brackets

Israeli personal income tax (`מס הכנסה ליחיד`) is progressive. Brackets apply to *annual* taxable income from labor, self-employment, and most ordinary sources. Passive investment income (capital gains, dividends, interest) generally has flat rates outside this bracket schedule — see `capital_gains.md`.

> **Verification status:** `last_verified: 1900-01-01`. The numerical brackets below are anchored to the user's existing 2026 plan (`Jacobs_Wealth_Plan.md`) and 2025 published rules but have **not** been freshly verified against `taxes.gov.il`. The domain-refresh agent must verify against the Israel Tax Authority before any agent uses these numbers in a live recommendation.

## 2026 brackets — labor and ordinary income

The Israeli Income Tax Ordinance (Section 121) sets bracketed marginal rates. Brackets are quoted in NIS of annual taxable income.

| Bracket (annual taxable NIS) | Marginal rate |
|---|---|
| 0 – 84,120 | 10% |
| 84,121 – 120,720 | 14% |
| 120,721 – 193,800 | 20% |
| 193,801 – 269,280 | 31% |
| 269,281 – 560,280 | 35% |
| 560,281 – 721,560 | 47% |
| 721,561 + | 47% (and `surtax.md` adds an additional 3% over the surtax threshold) |

Notes:

- The **47% top bracket** kicks in around 560k NIS of annual labor income. RSU vesting at NVIDIA Israel typically pushes a senior employee into the 47% bracket on the marginal RSU income.
- The **surtax** (`tosefet mas`, `מס יסף`) — see `surtax.md` — adds another 3% on the portion of total taxable income above the high-income threshold (~721k NIS in recent years; verify annually).
- Personal credit points (`נקודות זיכוי`) reduce the gross tax bill and effectively raise the entry point of the schedule. A typical Israeli resident has 2.25 credit points; a married parent has more. Each credit point is worth ~242 NIS/month.

## How agents should use this file

- **Cite this file** for any claim about Israeli marginal tax rates on labor or ordinary income.
- For **passive investment income** (CGT, dividends, interest), cite `capital_gains.md` instead — those are flat rates.
- For **RSU taxation**, reference both this file and `retirement/section_102.md`.
- Numbers are NIS per year; convert at the FX rate on the relevant snapshot (currently ~2.94 USD/NIS per the May 2026 portfolio).
- If `last_verified` is older than 12 months OR is `1900-01-01`, agents must report `confidence=low` and prompt the domain-refresh agent.

## Refresh cadence

- **Annual (January)** — Knesset typically updates brackets and credit-point values for the new tax year.
- **Ad-hoc** — any mid-year amendment (e.g., a budget law) triggers an immediate refresh.

## Open issues

- The 2026 bracket boundaries above are the user's Plan v2.0 working numbers. Until the domain-refresh agent verifies against `taxes.gov.il`, treat with caution.
- Joint filing and married-couple credit-point interactions are not modeled here; covered in a future `married_filing.md` once we verify rules.
