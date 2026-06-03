---
title: Israeli Section 102 — RSU and Stock-Option Taxation (Capital Gains Track) — 2026
topic: israel_section_102_rsu_taxation
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual_with_employer_equity
last_verified: 2026-06-02
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation
  - https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide
  - https://www.rnc.co.il/employee-stock-options-israel/
  - https://taxsummaries.pwc.com/israel/individual/income-determination
sources:
  - url: https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide
    retrieved: 2026-06-02
    tier: 2
  - url: https://www.rnc.co.il/employee-stock-options-israel/
    retrieved: 2026-06-02
    tier: 2
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-06-02
    tier: 1
---

# Section 102 — Israeli RSU and Stock-Option Taxation (Capital Gains Track)

> **NOTE:** This file lives at `domain_knowledge/tax/israel/section_102.md` per the refresh spec. A historical copy also exists at `domain_knowledge/tax/israel/retirement/section_102.md`; this file is the canonical 2026-refreshed version.

## Summary

`Section 102` of the Israeli Income Tax Ordinance governs taxation of employee stock-based compensation (RSUs, options, and similar awards). Two principal tracks exist; the choice has major rate consequences. The **Capital Gains Track via a Trustee** (`102 הוני באמצעות נאמן`) is the standard election at Israeli high-tech employers (including NVIDIA Israel) and delivers a **25% flat capital-gains rate** on the post-grant appreciation, provided the **24-month trustee holding period from the end of the tax year of grant** is observed.

## Rates / brackets / amounts (2026)

| Track | Tax classification of gain | Statutory rate | Surtax overlay | Holding period | Employer expense deduction |
|---|---|---|---|---|---|
| **102 Capital — Trustee** | Capital gain (on the appreciation slice) | **25%** flat | + 3% general / + 2% capital-source above ₪721,560 → up to 30% | **24 months from end of tax year of grant** | **No** — employer cannot deduct |
| **102 Ordinary — Trustee** | Salary income | Marginal labor rate (up to 47% + 3% surtax = 50%) + NI/health up to ceiling | Surtax applies as labor income | Same trustee mechanism, shorter "12 months" practical relevance | Yes |
| **102 Non-Trustee** | Salary income at vesting | Marginal labor rate | Surtax applies as labor | None | Yes |
| **Section 3(i)** — non-employees, contractors, controlling shareholders | Salary income | Marginal labor rate | Surtax applies | None | n/a |

Sources: NASPP "Hiring in Israel — How Section 102 Shapes Equity Compensation"; VestingStrategy Israel Equity Compensation Tax Guide; RNC Law ESOP 102 vs 3(i) guide; PwC Israel Income Determination (last reviewed 1 Jan 2026).

### Holding-period clock — the precise rule

- The 24-month period runs **from the end of the tax year in which the grant occurred**, deposited with a qualified trustee, **not** from grant date itself.
  - Example: RSU granted 15 March 2024 → grant tax year is 2024 → 24-month clock starts 1 January 2025 → trustee release earliest 1 January 2027.
  - This is the conservative LLM-trap clarification — many summaries say "24 months from grant" which understates the actual wait by up to ~11 months.
- Source: NASPP; VestingStrategy; RNC.

### Capital vs ordinary split for **public-company** RSUs (the user's case)

- The **lesser of (a) FMV at grant** and (b) average closing price over the 30 trading days **preceding grant** — is treated as **ordinary income** taxed at marginal rates at sale.
- The **appreciation above that benchmark** at sale is treated as a **capital gain** taxed at the 25% Section 102 Capital rate.
- For **RSUs specifically** the exercise price is zero, so the entire FMV-at-vest is the ordinary slice at first glance — but the public-company rule pins ordinary to the **grant-date benchmark**, with post-grant appreciation receiving the 25% treatment.
- Source: NASPP; VestingStrategy.

## Application notes

### Trustee mechanism

- Qualified Israeli trustees (e.g., Altshuler Shaham Trusts, Harel, IBI Trust, ESOP Excellence) hold the granted RSU shares for the trustee period.
- The employer's equity plan must be **pre-filed with the Israel Tax Authority** by the trustee, including the local tax annex. Plans become effective 30 days after filing (the "30-day ruling" mechanism) unless the ITA objects.
- The employee cannot direct the shares while held by the trustee. After the 24-month window (and after vesting), the trustee releases shares to the employee's brokerage account (Schwab in the user's case).
- At sale, the employer payroll department typically withholds the **ordinary-income slice** as Israeli income tax in the month of sale; the capital-gain slice is paid by the employee via the annual `דוח שנתי` (tofes 1301), or — for substantial single tranches — via a mid-year `מקדמה` (advance) to avoid late-payment interest.

### Consequences of breaking the holding period

- Selling or transferring out of the trustee before the 24-month-from-EoT-year mark: **the entire gain (not just the ordinary slice) reclassifies as ordinary salary income** taxed at marginal rates (up to 50%) plus NI/health (up to ceiling).
- This is a one-way penalty — there is no partial credit and no way to "fix" the early sale.
- The employer/trustee will withhold accordingly at sale; the ITA reconciles on the annual return.
- Source: NASPP; RNC.

### Cash flow at vest (sell-to-cover)

- On vesting, the trustee/payroll instructs Schwab (or the equivalent broker) to **sell-to-cover** enough shares to fund Israeli payroll withholding on the **ordinary income slice** (FMV-at-vest × marginal rate, plus NI/health up to ceiling).
- Remaining net shares are held by the trustee until the 24-month window completes, then released to the employee's account.
- **No cash arrives at Leumi USD until the user sells the post-trustee-release shares and wires the proceeds.** RSUs produce shares, not cash, at vesting.

### Interaction with the US-Israel treaty

- 25% Israeli Section 102 Capital rate applies regardless of broker location — NVDA-at-Schwab is fully subject to Israeli 102 because the user is an Israeli resident.
- US does **not** impose income tax on the capital gain (NRA capital gain = 0% US WHT under treaty Article 15 / US domestic NRA rule).
- US **estate tax** still applies to NVDA shares held by an Israeli decedent because the shares are US-situs — see `treaties/us_israel.md` and (when present) `tax/us/estate_tax_nonresidents.md`.

### Ariel-specific cash-flow corollary (May 2026 portfolio snapshot)

From the user's May 2026 TSV: ~11,471 NVDA shares at Schwab, ~$200/share, ~$2.296M USD. These are post-trustee-release shares; the 24-month Section 102 Capital clock has elapsed for the bulk of tranches. Each quarterly sale tranche under Plan v2.0 realizes a Section 102 Capital gain at 25% + surtax stack (`surtax.md`). The "Avg Price" Schwab tracks is the FMV-at-vest cost basis used for the capital-slice computation, **not** the original grant FMV — the agent must read the broker basis as the basis-for-25%-CGT, with the FMV-at-vest-minus-grant-FMV piece already having been taxed as ordinary income at vest.

## Stack with related rates

For an NVDA tranche realized after the 24-month period:

| Layer | Rate | Notes |
|---|---|---|
| Statutory Section 102 Capital | 25% | On (sale price − broker cost basis) |
| `mas yesef` general (above ₪721,560) | +3% | Almost always applies given the user's salary alone |
| Additional capital-source surtax (above ₪721,560) | +2% | Triggers when capital-source income alone exceeds threshold |
| US WHT on the capital gain | 0% | NRA capital-gain exemption |
| NI / health on the capital gain | 0% | CG not subject to NI |
| **Marginal effective in surtax zone** | **30%** | The number to use for Plan v2.0 NVDA tranche modeling |

## Sources

- [NASPP — Hiring in Israel: How Section 102 Shapes Equity Compensation](https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation) — accessed 2026-06-02
- [VestingStrategy — Israel Equity Compensation Tax: Stock Options, RSUs & Section 102](https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide) — accessed 2026-06-02
- [RNC Law — ESOP 102 vs. 3(i): Tax Routes for Israeli Employee Stock Options](https://www.rnc.co.il/employee-stock-options-israel/) — accessed 2026-06-02
- [PwC Israel — Individual — Income determination](https://taxsummaries.pwc.com/israel/individual/income-determination) — accessed 2026-06-02

## Refresh cadence

- **Annual (January)** — re-verify the 25% rate, the 24-month-from-end-of-tax-year rule, the trustee filing mechanism, and the capital/ordinary split for public-company RSUs.
- **On reform** — Section 102 has been amended multiple times (notably the 2003 reform that introduced the two-track regime). Any further amendment triggers immediate refresh.

## Open issues

- **Section 102 split for joint accounts with non-Israeli spouse** — out of scope for this file; needs a future `married_couple_102.md` if it becomes operational.
- **Dual residency / split-year** scenarios — out of scope.
- **Trustee fees** typically ~0.1–0.3% of released value; not modeled here.
- The exact "**lesser of (a) FMV at grant** and (b) **30-day average pre-grant**" benchmark for the ordinary slice should be confirmed in NVIDIA's specific plan-of-record before the equity_comp_analyst agent uses it for a hard tax projection.
