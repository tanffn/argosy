---
topic: israel_surtax_high_income
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%9E%D7%A1_%D7%99%D7%A1%D7%A3
    retrieved: 1900-01-01
    tier: 2
---

# Israeli Surtax on High Income (`מס יסף` / `tosefet mas`)

A flat additional 3% levy applies to taxable income above a high-income threshold. It is calculated on **total** annual taxable income — labor income, capital gains, dividends, interest, real-estate gain — combined.

> **Verification status:** `last_verified: 1900-01-01`. Threshold below is the user's plan-vintage figure (~721k NIS). Domain-refresh agent must verify the current threshold and rate annually.

## Mechanics

- **Rate:** 3% (flat).
- **Threshold (2024–2026 vintage):** approximately 721,560 NIS of total taxable annual income; verify yearly. Some pension/savings exemptions apply.
- **Calculation:** 3% × (total taxable income − threshold). Apply to the marginal NIS only.

## Why this matters for this user

- A NIS 500k salary at NVIDIA already lands the user near the surtax threshold.
- Adding RSU vesting income (~500k NIS gross) pushes well above the threshold for most years; the marginal RSU NIS faces 47% income tax + 3% surtax = **50% marginal rate** before NI/health (which are above-ceiling exhausted for this profile — see `national_insurance.md`).
- Capital gains on NVDA tranches push *all* annual taxable income upward, and any portion above the surtax threshold incurs an extra 3% on top of the 25% CGT → **28% effective CGT** in the surtax zone.

## Worked example

A NVDA tranche sale of 2,000 shares at $200, FX 2.94, with $50/share cost basis:

- Gross gain in USD: 2,000 × ($200 − $50) = $300,000
- Gross gain in NIS: $300,000 × 2.94 = 882,000 NIS
- 25% statutory CGT: 882,000 × 25% = 220,500 NIS
- 3% surtax on the portion above threshold: complicated by the rest of the year's income; if labor income already filled the surtax bracket, the *full* 882k of CGT also incurs 3% surtax → 26,460 NIS.
- **Total Israeli tax on this tranche:** ~246,960 NIS (~28% effective).

Surtax is small in absolute %, but it compounds with everything else.

## How agents should use this file

- **Cite this file** for any "marginal effective rate" calculation that targets the surtax band.
- Combine with `brackets_2026.md` (47% bracket) and `national_insurance.md` (NI ceiling) to produce a complete marginal-rate stack.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual (January)** — threshold is wage-indexed; Knesset has tweaked it.
- **On budget law** — any reform touching the surtax (occasionally proposed) triggers immediate refresh.
