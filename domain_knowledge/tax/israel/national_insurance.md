---
topic: israel_national_insurance_and_health_tax
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.btl.gov.il/Insurance/Rates/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.btl.gov.il/English%20Homepage/Insurance/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
---

# National Insurance (Bituach Leumi) and Health Tax

Israeli residents pay two parallel, mandatory levies on top of income tax:

1. **National Insurance** (`ביטוח לאומי`) — funds pensions, disability, unemployment, maternity, etc.
2. **Health Tax** (`מס בריאות`) — funds the public health basket (`סל בריאות`).

Both are administered by Bituach Leumi (BTL). They are **not** part of the Income Tax Ordinance, so they are conceptually separate from the brackets in `brackets_2026.md`.

> **Verification status:** `last_verified: 1900-01-01`. Numbers below are anchored to 2025 published BTL rates and the user's plan. The domain-refresh agent must verify against `btl.gov.il` annually (BTL ceiling adjusts in January).

## Income bands

Bituach Leumi distinguishes a *low-rate band* (below ~60% of the average wage) and a *high-rate band* (up to a monthly ceiling). Income above the monthly ceiling is **not** subject to NI or health tax — this matters greatly for high earners and RSU vests.

Reference monthly ceiling (2025/2026 vintage; verify yearly):
- Approximate monthly ceiling: ~50,695 NIS (2025). Updated annually based on average-wage indexing.
- Annual equivalent: ~608,000 NIS.

## Employee rates (employee withholding from salary)

| Component | Low band (below ~60% avg wage) | High band (up to ceiling) |
|---|---|---|
| National Insurance — employee | ~0.40% | ~7.00% |
| Health tax — employee | ~3.10% | ~5.00% |
| **Combined employee withholding** | **~3.50%** | **~12.00%** |

Employer also pays an employer-side National Insurance contribution; that is the employer's cost, not the employee's.

## Self-employed rates (`עצמאי`)

| Component | Low band | High band |
|---|---|---|
| National Insurance — self-employed | ~2.87% | ~12.83% |
| Health tax — self-employed | ~3.10% | ~5.00% |
| **Combined** | **~5.97%** | **~17.83%** |

Self-employed individuals pay both the employee-equivalent and the employer-equivalent contributions because there is no separate employer.

## How RSU vesting interacts with NI/health

- RSU vesting income falls under the labor-income classification → NI + health tax apply *up to the monthly ceiling*.
- Above the ceiling, the marginal NI+health load drops to zero — only `brackets_2026.md` (income tax) and `surtax.md` apply.
- For a NVIDIA Israel employee with 500k NIS salary already over the annual ceiling, an additional RSU vest within the same year is *not* subject to incremental NI/health (already maxed) but still subject to 47% income tax + 3% surtax above its threshold.

## How agents should use this file

- **Cite this file** for any NI- or health-tax claim, especially for "what is the take-home from an RSU vest?" calculations.
- The combined ~12% high-band rate stops at the monthly ceiling. Agents calculating tax on a marginal NIS of high-income RSU income should remember NI/health is **already fully consumed** and therefore the *marginal* take-home depends only on income tax + surtax + employer pension match.
- If `last_verified` is older than 12 months OR `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual (January)** — BTL re-indexes the ceiling and may adjust rates.
- **Mid-year** — only on legislation; rare.
