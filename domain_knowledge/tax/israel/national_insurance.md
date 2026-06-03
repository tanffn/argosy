---
title: Israeli National Insurance (Bituach Leumi) and Health Tax — 2026
topic: israel_national_insurance_and_health_tax
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual
last_verified: 2026-06-02
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://hcat.co/doing-business-in-israel/
  - https://www.malam-payroll.com/national-insurance-updates-for-2026/
  - https://jobcalc.co.il/blog/national-insurance-guide-2026/
  - https://taxsummaries.pwc.com/israel/individual/other-taxes
  - https://www.btl.gov.il/English%20Homepage/Insurance/Ratesandamount/Pages/forSalaried.aspx
sources:
  - url: https://hcat.co/doing-business-in-israel/
    retrieved: 2026-06-02
    tier: 2
  - url: https://www.malam-payroll.com/national-insurance-updates-for-2026/
    retrieved: 2026-06-02
    tier: 2
  - url: https://jobcalc.co.il/blog/national-insurance-guide-2026/
    retrieved: 2026-06-02
    tier: 2
  - url: https://taxsummaries.pwc.com/israel/individual/other-taxes
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.btl.gov.il/English%20Homepage/Insurance/Ratesandamount/Pages/forSalaried.aspx
    retrieved: 2026-06-02
    tier: 1
---

# National Insurance (Bituach Leumi) and Health Tax — 2026

## Summary

Israeli residents pay two parallel, mandatory levies on top of income tax: **National Insurance** (`ביטוח לאומי`) and **Health Tax** (`מס בריאות`). Both are administered by Bituach Leumi (BTL) and are conceptually separate from the Income Tax Ordinance. The 2026 monthly **ceiling** is **₪51,910** (≈ ₪622,920/year); above this ceiling no NI or health tax is collected. The 2026 **reduced-band threshold** is **₪7,703/month**. Combined employee rates are **4.27% low band / 12.17% high band**. **Capital gains are not subject to NI/health tax** — only labor and personal-exertion income are.

## Rates / brackets / amounts (2026)

### Employee rates — salaried resident (`עובד שכיר`)

| Component | Low band (up to ₪7,703/mo) | High band (₪7,703–₪51,910/mo) |
|---|---|---|
| National Insurance — employee portion | **1.04%** | **7.00%** |
| Health Tax — employee portion | **3.23%** | **5.17%** |
| **Combined employee withholding** | **4.27%** | **12.17%** |

- Income above the ceiling of **₪51,910/month** (annualized ₪622,920) is **not** subject to NI or health.
- The ₪51,910 ceiling is unchanged from the 2025-published rate-table source; the ₪7,703 threshold was raised from ₪7,522 (2025) to ₪7,703 effective 1 January 2026.
- Source: Harris Consulting & Tax 2026 guide; Malam-Payroll BTL 2026 update; JobCalc 2026 BTL guide; PwC Israel Other Taxes.

### Self-employed rates (`עצמאי`)

| Component | Low band (up to ₪7,703/mo) | High band (₪7,703–₪51,910/mo) |
|---|---|---|
| National Insurance — self-employed | **2.87%** | **12.83%** |
| Health Tax — self-employed | **3.23%** | **5.17%** |
| **Combined** | **~6.10%** | **~18.00%** |

- Self-employed individuals pay both the employee-equivalent and (roughly) the employer-equivalent contributions because there is no separate employer.
- 52% of self-employed NI contributions are income-tax-deductible (`ניכוי 52%`).
- Source: JobCalc 2026 BTL guide; Malam-Payroll; Harris Consulting & Tax.

### "Not working" / passive-income earners

- For Israeli residents with no labor income but with non-exempt passive income (e.g., rental, certain interest), BTL levies its own bracket schedule. Rate ranges 12.09%–12.17% per the Harris Consulting 2026 reference. Beyond scope of typical RSU/CG analysis for the user.

### Non-residents

- Non-resident employees of an Israeli employer: **0.1% low / 0.87% high** (NI only; no health tax for non-residents). Same ₪51,910 ceiling.

## Application notes

### Capital gains are **not** subject to NI/health

- **Capital gains, dividends, and interest are not subject to National Insurance or health tax** for individual investors. PwC explicitly confirms: "Capital gains are not subject to national insurance contributions."
- This means an NVDA tranche sale incurs only the income-tax-side stack (25% statutory CGT + surtax) — no NI/health.

### RSU vesting interaction (this is where it matters)

- The **ordinary-income slice** at RSU vest (`section_102.md`) **is** subject to NI/health up to the monthly ceiling, because it is classified as labor income.
- For a NVIDIA Israel senior with monthly base salary already above ₪51,910/month, the **NI/health ceiling is already exhausted by base salary** — incremental RSU vest income in the same month faces **0% marginal NI/health**, only the 47%/50% income tax + surtax.
- The "12% take-home cut" mental model from junior-employee planning **does not apply** at the user's income level for marginal NIS.
- This is the key NI/health insight for Plan v2.0 RSU modeling: at the user's profile, NI/health is a flat fixed cost determined by base salary, not a variable cost that scales with RSU vesting.

### Sell-to-cover and Section 102

- The sell-to-cover at vest funds Israeli payroll withholding on the ordinary slice. This withholding includes the (already-exhausted-at-ceiling) NI/health drag plus the marginal income tax.
- Practically: if base salary > ₪51,910/mo, the sell-to-cover percentage of vested RSU shares ≈ marginal income tax + surtax only (47%–50%), not income tax + NI/health.

## Stack with related rates

For a senior NVIDIA Israel employee at base salary > ₪51,910/month:

| Income source | NI/health marginal | Income tax marginal | Combined marginal at top |
|---|---|---|---|
| Base salary, below ₪7,703/mo (only the first slice) | 4.27% | per bracket | varies |
| Base salary, ₪7,703–₪51,910/mo | 12.17% | per bracket | varies |
| Base salary, > ₪51,910/mo | **0%** | up to 50% | **~50%** |
| RSU ordinary slice (above ceiling) | **0%** | 47% + 3% surtax | **~50%** |
| RSU capital slice (102 Capital) | **0%** | 25% + 3%/2% surtax | **~30%** |
| NVDA capital gain post-sale | **0%** | 25% + 3%/2% surtax | **~30%** |

## Sources

- [Harris Consulting & Tax — Doing Business in Israel (2026 update)](https://hcat.co/doing-business-in-israel/) — accessed 2026-06-02
- [Malam-Payroll — National Insurance Updates for 2026](https://www.malam-payroll.com/national-insurance-updates-for-2026/) — accessed 2026-06-02
- [JobCalc — Bituach Leumi 2026 Complete Guide](https://jobcalc.co.il/blog/national-insurance-guide-2026/) — accessed 2026-06-02
- [PwC Israel — Individual — Other taxes](https://taxsummaries.pwc.com/israel/individual/other-taxes) — accessed 2026-06-02
- [Bituach Leumi — For salaried workers, Rates and amounts of insurance contributions](https://www.btl.gov.il/English%20Homepage/Insurance/Ratesandamount/Pages/forSalaried.aspx) — accessed 2026-06-02 (HTTP 419 during fetch; rates confirmed via Harris, Malam, JobCalc)

## Refresh cadence

- **Annual (January)** — BTL re-indexes the ceiling and the reduced-band threshold; rates can also change by amendment.
- **Mid-year** — only on legislation; rare.

## Open issues

- BTL English page `btl.gov.il/English_Homepage/.../forSalaried.aspx` returned HTTP 419 during this refresh. Rates were triangulated across three current 2026 sources (Harris, Malam-Payroll, JobCalc). Re-verify against the live BTL page when accessible.
- Edge cases: pension contributions reduce the NI base; not modeled here.
- The "not working / passive" bracket schedule that applies to early-retirement scenarios with no labor income is documented above only at the headline level. If/when the user transitions to retirement-mode income, this file needs a dedicated retirement-NI section.
