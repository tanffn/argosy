---
title: Israeli Section 102 — RSU and Stock-Option Taxation (Capital Gains Track) — 2026
topic: israel_section_102_rsu_taxation
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual_with_employer_equity
last_verified: 2026-08-15
verified_by: per-lot §102 ledger reconciliation vs trustee simulation report (2026-07-09) + Amendment 147 statutory check; SUPERSEDES the 2026-07-08 web-search verification on the holding-clock rule (see correction trail in the holding-period section)
next_refresh_due: 2027-01-31
source_urls:
  - https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation
  - https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide
  - https://www.rnc.co.il/employee-stock-options-israel/
  - https://taxsummaries.pwc.com/israel/individual/income-determination
sources:
  - url: https://www.naspp.com/blog/hiring-in-israel--how-section-102-shapes-equity-compensation
    retrieved: 2026-08-15
    tier: 1
  - url: https://www.vestingstrategy.com/guides/israel-equity-compensation-tax-guide
    retrieved: 2026-08-15
    tier: 2
  - url: https://www.rnc.co.il/employee-stock-options-israel/
    retrieved: 2026-08-12
    tier: 2
  - url: https://taxsummaries.pwc.com/israel/individual/income-determination
    retrieved: 2026-08-15
    tier: 1
---

# Section 102 — Israeli RSU and Stock-Option Taxation (Capital Gains Track)

> **NOTE:** This file lives at `domain_knowledge/tax/israel/section_102.md` per the refresh spec. A historical copy also exists at `domain_knowledge/tax/israel/retirement/section_102.md`; this file is the canonical 2026-refreshed version.

## Summary

`Section 102` of the Israeli Income Tax Ordinance governs taxation of employee stock-based compensation (RSUs, options, and similar awards). Two principal tracks exist; the choice has major rate consequences. The **Capital Gains Track via a Trustee** (`102 הוני באמצעות נאמן`) is the standard election at Israeli high-tech employers (including NVIDIA Israel) and delivers a **25% flat capital-gains rate** on the post-grant appreciation, provided the **24-month trustee holding period from the grant (allotment) date** is observed (Amendment 147, 2006 — see the holding-clock section, including the correction trail).

## Rates / brackets / amounts (2026)

| Track | Tax classification of gain | Statutory rate | Surtax overlay | Holding period | Employer expense deduction |
|---|---|---|---|---|---|
| **102 Capital — Trustee** | Capital gain (on the appreciation slice) | **25%** flat | + 3% general / + 2% capital-source above ₪721,560 → up to 30% | **24 months from grant (allotment) date** | **No** — employer cannot deduct |
| **102 Ordinary — Trustee** | Salary income | Marginal labor rate (up to 47% + 3% surtax = 50%) + NI/health up to ceiling | Surtax applies as labor income | Same trustee mechanism, shorter "12 months" practical relevance | Yes |
| **102 Non-Trustee** | Salary income at vesting | Marginal labor rate | Surtax applies as labor | None | Yes |
| **Section 3(i)** — non-employees, contractors, controlling shareholders | Salary income | Marginal labor rate | Surtax applies | None | n/a |

Sources: NASPP "Hiring in Israel — How Section 102 Shapes Equity Compensation"; VestingStrategy Israel Equity Compensation Tax Guide; RNC Law ESOP 102 vs 3(i) guide; PwC Israel Income Determination (last reviewed 1 Jan 2026).

### Holding-period clock — the precise rule (CORRECTED 2026-07-09)

- The 24-month period runs **from the grant (allotment) date** — the date the awards are allotted to the qualified trustee — per **Amendment 147 to the Income Tax Ordinance (2006)**, which shortened the requirement from the original 2003-reform rule ("until the end of the tax year of grant plus 24 months") to a flat **24 months from allotment**.
  - Example: RSU granted 8 April 2024 → trustee clock ends 8 April 2026 → capital-track eligible from 8 April 2026.
- **Empirical confirmation (trustee's own eligibility engine):** the NVIDIA trustee simulation report dated 2026-06-18 marks grants **289172 / 289173 (granted 2024-04-08)** as capital-track **eligible** ("OK" holding period). Under an end-of-grant-tax-year rule they would only become eligible **2027-01-01** — the trustee's marking is consistent ONLY with 24-months-from-grant-date. Grants 331375 (2025-03-10) and 374434 (2026-03-09) are correspondingly marked "Breaking" (not yet eligible), exactly as from-grant-date predicts.
- **Correction trail:** the 2026-06-02/2026-07-08 refreshes of this file asserted the opposite rule ("24 months from the end of the tax year of grant") based on secondary web summaries (NASPP / VestingStrategy / RNC) and labelled the from-grant-date rule an "LLM trap". That was the **pre-Amendment-147** rule. Several secondary sources still repeat it; the statute as amended in 2006 and the trustee's live eligibility behavior both say **from grant date**. Reviewers: do not "re-fix" this back from those secondary sources without checking Amendment 147 (תיקון 147) itself.
- Sources: Income Tax Ordinance §102 as amended by Amendment 147 (2006); NVIDIA trustee simulation report 2026-06-18 (eligibility markings, `Resources/2026/Schwab/Nvidia simulation Report.xlsx`).

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

- Selling or transferring out of the trustee before the 24-month-from-grant-date mark: **the entire gain (not just the ordinary slice) reclassifies as ordinary salary income** taxed at marginal rates (up to 50%) plus NI/health (up to ceiling).
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

Current book (2026-08-22 Schwab exports): **10,380** vested NVDA shares at Schwab
(RSU/award 8,885 + ESPP 1,495), ~$215.38/share, ~$2.236M USD, of which **9,573**
are past the 24-month clock. A further **3,378** unvested shares vest through
2030-03-15. These are post-trustee-release shares.

### The grant-date benchmark is the 30-TRADING-DAY MEAN — derived 2026-08-22

The statute says the ordinary slice is "the lesser of (a) FMV at grant and (b)
the average closing price over the 30 trading days preceding grant". In practice
the trustee has used **(b) every time**, and the rule reproduces EXACTLY:

| Grant | Granted | Trustee "Grant Stock Price (For Tax)" | 30-trading-day mean of NVDA closes before grant | Error |
|---|---|---|---|---|
| 213000 | 2022-06-08 | 18.1159 | 18.1159 | 0.000% |
| 246477 | 2023-06-08 | 31.9859 | 31.9859 | 0.000% |
| 289173 | 2024-04-08 | 87.4976 | 87.4976 | 0.000% |

Note 246477: FMV at grant was 38.4351 but the trustee used 31.9859 — a 17% gap,
so this is genuinely the 30-day mean and not a coincidence of the two agreeing.

**This means the basis for ANY grant is derivable without the trustee**, from
split-adjusted NVDA closes:

```python
c = yf.Ticker("NVDA").history(start=..., auto_adjust=False)["Close"]  # split-adjusted
basis = float(c[c.index <= grant_date].tail(31)[:-1].tail(30).mean())
```

Derived on that basis for the grants the 2026-06-18 trustee report does not
cover — do NOT substitute a vest-FMV proxy for these:

| Grant | Granted | Shares held | §102 basis |
|---|---|---|---|
| 182406 | 2021-07-09 | 1,420 | **18.3305** |
| 331375 | 2025-03-10 | 358 | **126.8600** |
| 374434 | 2026-03-09 | 57 | **185.7690** |

A vest-FMV proxy for 182406 would have used ~149.38 and understated that grant's
gain by roughly **8x**. Because 182406 is the source of most 2026 sales, the
proxy understated the whole 2026 realized gain by 36%.

### Which cost basis feeds the 25% — CORRECTED 2026-08-22

An earlier revision of this file asserted that "the `Avg Price` Schwab tracks is
the FMV-at-vest cost basis used for the capital-slice computation, **not** the
original grant FMV". **That is wrong, and it understates or overstates the
Israeli liability depending on the grant.** The two brokers compute for two
different tax systems:

| | Basis used | For |
|---|---|---|
| Schwab `TotalCostBasis` / `RealizedGainLoss` | **FMV at vest** | US-style reporting |
| Israeli trustee (§102) | **Grant-date price** | the 25% capital slice |

Verified against the trustee's own simulation engine (`Nvidia simulation
Report.xlsx`, 2026-06-18), whose grants span a wide enough price range to
distinguish the rules — a single 2022 grant cannot, because its grant and vest
prices sit within 2.5% of each other:

| Grant | Granted | Grant px | Capital income | = n x (sale - GRANT px)? | Ordinary income | = n x GRANT px? |
|---|---|---|---|---|---|---|
| 213000 | 2022-06-08 | 18.1159 | 52,230 | 52,230 YES | 5,069 | 5,072 YES |
| 246477 | 2023-06-08 | 31.9859 | 36,259 | 36,259 YES | 6,715 | 6,717 YES |
| 289173 | 2024-04-08 | 87.4976 |  9,372 |  9,372 YES | 6,999 | 7,000 YES |

So: **ordinary slice = shares x grant-date price** (marginal rate, ~62.17% in the
sim); **capital slice = shares x (sale price - grant-date price)** at 25% + the
surtax stack (`surtax.md`).

**Consequence for agents: never compute the Israeli §102 liability from Schwab's
`RealizedGainLoss`.** It is the US-basis number. Computing the Israeli figure
requires the grant-date price for each grant. That price is DERIVABLE — see the
30-trading-day-mean section above — so there is no excuse for substituting the
Schwab number.

**2026 realized §102 capital gain (computed 2026-08-22): $734,227 = ILS
2,196,072** across 3,937 shares in six sales. Schwab's US-basis figure for the
same sales is $472,911 — only 64% of the true Israeli gain.

### Withholding: the trustee DOES withhold — in the wire leg, not at Schwab

An earlier revision of this section (2026-08-22, same day) concluded "no
withholding at sale" from Schwab's empty `Taxes` field and its `Forced
Disbursement` equal to the full gross. **That was wrong and would have had the
household reserve ~ILS 644,000 it does not owe.** Schwab does not perform Israeli
withholding; the **trustee** does, between the Schwab disbursement and the wire
that lands at Leumi. The evidence is in the bank statement, not the broker one.

Every 2026 sale, matched to its incoming Leumi USD wire:

| Sale | Gross $ | Wired $ | Withheld $ | % of gross | §102 gain $ | withheld / gain |
|---|---|---|---|---|---|---|
| 01-28 | 107,144.75 | 77,768.88 | 29,375.87 | 27.4% | 96,880 | 30.3% |
| 02-06 | 91,826.70 | 66,554.31 | 25,272.39 | 27.5% | 81,820 | 30.9% |
| 04-20 | 207,538.02 | 150,864.02 | 56,674.00 | 27.3% | 188,479 | 30.1% |
| 05-08 | 121,005.00 | 88,253.43 | 32,751.57 | 27.1% | 110,745 | 29.6% |
| 06-01 | 153,947.69 | 112,229.99 | 41,717.70 | 27.1% | 141,120 | 29.6% |
| 08-12 | 125,325.31 | 93,350.08 | 31,975.23 | 25.5% | 115,183 | 27.8% |
| **TOTAL** | **806,787.47** | **589,020.71** | **217,766.76** | **27.0%** | **734,227** | **29.7%** |

Withheld ILS 651,340 against a statutory computation of ILS 644,390 — agreeing
to **1.1%**. That agreement is itself the strongest available confirmation that
the 30-trading-day-mean basis above is right: an incorrect basis would not
produce a withheld/gain ratio sitting exactly in the 28-30% surtax band.

**Consequences for agents.** (a) There is NO unreserved 2026 capital-tax
liability; do not add one to the finite-liability reserve. (b) The annual return
reconciles the withheld amount against the true liability — expect a small
refund or top-up, not a six-figure payment. (c) Never conclude "nothing was
withheld" from broker fields alone: for §102 shares the withholding is invisible
at the broker and only appears as the gap between gross proceeds and the wire.

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

- **Annual (January)** — re-verify the 25% rate, the 24-month-from-grant-date rule (Amendment 147), the trustee filing mechanism, and the capital/ordinary split for public-company RSUs.
- **On reform** — Section 102 has been amended multiple times (notably the 2003 reform that introduced the two-track regime). Any further amendment triggers immediate refresh.

## Open issues

- **Section 102 split for joint accounts with non-Israeli spouse** — out of scope for this file; needs a future `married_couple_102.md` if it becomes operational.
- **Dual residency / split-year** scenarios — out of scope.
- **Trustee fees** typically ~0.1–0.3% of released value; not modeled here.
- The exact "**lesser of (a) FMV at grant** and (b) **30-day average pre-grant**" benchmark for the ordinary slice should be confirmed in NVIDIA's specific plan-of-record before the equity_comp_analyst agent uses it for a hard tax projection.
