---
topic: israel_section_102_rsu_taxation
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%9E%D7%A1_%D7%A2%D7%9C_%D7%90%D7%95%D7%A4%D7%A6%D7%99%D7%95%D7%AA_%D7%9C%D7%A2%D7%95%D7%91%D7%93%D7%99%D7%9D
    retrieved: 1900-01-01
    tier: 2
  - url: https://home.treasury.gov/policy-issues/tax-policy/international-tax/tax-treaties
    retrieved: 1900-01-01
    tier: 1
---

# Section 102 — Israeli RSU and Stock-Option Taxation

`Section 102` of the Israeli Income Tax Ordinance governs the taxation of stock-based compensation (RSUs and options) granted to Israeli employees by their employer (or a parent company, e.g., NVIDIA Corporation granting to NVIDIA Israel employees). The section offers two principal tracks; the choice has very material tax-rate consequences.

> **Verification status:** `last_verified: 1900-01-01`. Domain-refresh agent must verify the two-track structure, the 24-month holding period, and the trustee-administration requirement annually.

## The two tracks

| Track | Tax classification | Marginal rate at sale | Holding period | Notes |
|---|---|---|---|---|
| **Capital Gains (102 Capital)** | Capital gains | **25%** flat (plus surtax if applicable — `surtax.md`) | **24 months** from grant date through trustee | Employer cannot deduct the equity expense |
| **Ordinary Income (102 Ordinary)** | Salary income | **Marginal labor rate** (up to 47% + surtax + NI ceiling complications — `brackets_2026.md`, `national_insurance.md`) | No special holding period | Employer can deduct |

The vast majority of high-tech employees (including NVIDIA Israel) elect the **Capital Gains** track because the 25% flat rate is dramatically better than the 47%+ ordinary rate. The election is made by the *employer's plan*, not the individual employee — the employee's job is to *not* break the holding-period rule once the trustee has the shares.

## The trustee mechanism

- Granted RSU shares are held by an Israeli **trustee** (typically `הראל`, `Altshuler Shaham`, etc.) for the holding period.
- Selling before the 24-month mark from grant strips the favorable rate; the gain reverts to ordinary salary income at marginal rates.
- After 24 months (and after vesting), the trustee can release the shares to a brokerage account (Schwab in the user's case) and the employee can sell at the 25% rate.
- The "deemed sale" date for tax purposes is the actual sale date, not the release date.

## What's actually taxed at 25% under 102 Capital

This is the subtle point most LLMs get wrong:

- **Gain from grant date to sale date** is the total to allocate.
- The **portion equal to the FMV at vesting** (or grant — exact pivot depends on the plan) is treated as **ordinary salary income** taxed at marginal labor rates (47%+).
- The **portion above** that FMV — the post-grant/vest *appreciation* — is the part taxed at 25% under 102 Capital.

For NVDA, granted at, say, $20 cost basis and sold at $200, the split is roughly:
- $20 ordinary (47%+) — already withheld by Israeli payroll at vesting.
- $180 capital gain (25% + surtax) — paid via the Israeli return.

This is why the May 2026 TSV shows NVDA at avg-price $200.14 — the cost basis Schwab tracks is the FMV-at-vest used for the *capital* slice, not the original grant price. Both the user and the agent must be careful: the "Avg Price" column is the **broker's** cost basis (post-vest tax basis), not the **economic** cost basis. For 102 Capital purposes, treat the appreciation above the broker basis as the 25% slice.

## Holding-period mistakes that destroy 102 status

- Selling within 24 months of grant date — even after vesting — re-classifies the gain as ordinary salary income.
- Transferring shares out of the trustee account before the 24-month mark.
- Certain corporate actions (rare) can also reset the clock; verify with the employer's stock plan administrator before non-routine transactions.

## Interaction with the US-Israel treaty

- The 25% Israeli tax under 102 Capital applies regardless of where the broker is located; NVDA-at-Schwab is still subject to Israeli 102.
- US does not impose its own tax on the gain because the Israeli resident is an NRA and capital gains are 0% US-WHT (`tax/us/nonresident_withholding.md`); but US **estate tax** still applies to the NVDA shares because they are US-situs (`tax/us/estate_tax_nonresidents.md`).
- The 102 holding period is unrelated to the W-8BEN cycle, but the W-8BEN must remain valid throughout for US-source dividend WHT to be correctly applied at the 15% treaty rate.

## RSU vesting cash flow (matches `LLM_Advisor_Handoff.md` correction #11)

- NVIDIA grants RSUs to NVIDIA Israel employee → trustee receives shares.
- On vesting: a **portion of shares is sold automatically by the broker** ("sell-to-cover" or similar) to fund Israeli payroll tax on the *ordinary* slice (the FMV-at-vest portion at 47%+ marginal rates).
- The **net shares** post-tax are released to Schwab, available to hold or sell.
- If the user later sells at a price above FMV-at-vest, the *additional* gain is the 102 Capital 25% slice and is filed on the Israeli annual return.
- **Cash flow corollary:** RSUs do not produce cash at Schwab — they produce **shares**. The cash arrives at Leumi USD only when the user sells the shares (post-24-month holding) and wires.

## The user's situation

From the May 2026 TSV: 11,471 NVDA shares at Schwab, FMV $200.14, total ~$2.296M USD. Plan v2.0 sells these in quarterly tranches, each one realizing a 102 Capital gain at 25% + surtax in Israel.

## How agents should use this file

- **Cite this file** for any RSU-tax calculation.
- Pair with `brackets_2026.md` for the labor-side ordinary slice.
- Pair with `capital_gains.md` for the standard CGT comparator.
- Pair with `surtax.md` because all NVDA tranches push the user into the surtax band.
- The plan-critique agent must check that any plan item recommending "sell NVDA early" considers the 24-month holding period for any not-yet-vested RSUs.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual** — verify the 25% rate, the two-track structure, and the 24-month period are unchanged.
- **On reform** — Section 102 has been amended several times historically; any amendment triggers immediate refresh.

## Open issues

- Exact mechanics of "ECI vs trustee-released" for shares held jointly with non-Israeli spouses — out of scope here, would need a `married_couple_102.md` if it ever applies.
