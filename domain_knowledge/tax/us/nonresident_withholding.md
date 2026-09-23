---
topic: us_nonresident_alien_withholding
jurisdiction: us
last_verified: 2026-09-13
next_refresh_due: 2026-12-31
sources:
  - url: https://www.irs.gov/pub/irs-prior/i1042s--2025.pdf
    tier: 1
    label: "Original 2025 Form1042-S instructions for the historical broker forms, not a current W-8BEN certificate"
  - url: file://Resources/2025/106/1042S%20-%202025_2026-03-10_876.PDF
    retrieved: 2026-09-13
    tier: 1
    label: Historical 2025 account withholding report; not proof of current W-8BEN status or future distributions.
  - url: https://www.irs.gov/instructions/i706na
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-trty/ireland.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-lbi/tax-treaty-table-1.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/publications/p515
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/instructions/iw8ben
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/individuals/international-taxpayers/nonresident-aliens
    retrieved: 2026-08-27
    tier: 1
  - url: https://www.irs.gov/pub/irs-pdf/p515.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/forms-pubs/about-form-w-8-ben
    retrieved: 2026-09-13
    tier: 1
---

# US Withholding on Income to Nonresident Aliens

## Historical 2025 broker-form reconciliation

The existing Schwab 1042-S packet named `1042S - 2025_2026-03-10_876.PDF`
contains these three distinct form identifiers (recipient copies repeat them).
These are historical reported amounts, not evidence of present W-8BEN validity,
future withholding, or an automatically allowable Israeli foreign-tax credit.

| Form identifier | Income code | Reported gross USD | Chapter3 exemption / rate | Federal tax withheld USD |
|---|---|---|---|---|
| 2025088149 | 01 | 7.00 | 00 / 17.50% | 1.00 |
| 2025088150 | 01 | 975.00 | 02 / 0.00% | 244.00 |
| 2025088151 | 06 | 320.00 | 00 / 25.00% | 80.00 |

**Open broker reconciliation:** form2025088150 pairs exemption code02 and a
zero stated rate with $244 withheld and credited. Ask Schwab to explain the
classification and withholding, and supply a corrected form if needed. The
packet alone does not establish the cause, refund entitlement, or the Israeli
credit treatment. Do not automatically credit, reclaim or reclassify that $244.
This concrete follow-up replaces a generic request to re-review all withholding
rules; the other two forms do not establish current account compliance either.

A nonresident alien (NRA) — the user's status under US tax law as an Israeli resident with no green card and no substantial presence in the US — is subject to US tax only on certain US-source income, primarily through withholding at source. This file covers the rules relevant to a portfolio investor at Schwab.

> **Scope:** public rules for an eligible Israeli-resident individual who is a US nonresident alien. This file does not establish the user's actual residency, W-8BEN validity, distribution classifications or broker withholding. Check account records for those facts. The prior verification date remains historical until the corrected document passes re-verification.

## Default statutory withholding (no treaty)

| Income type | US WHT rate (statutory NRA) |
|---|---|
| US-corporation dividends | 30% |
| Qualifying non-ECI bank-deposit interest | 0% (statutory exemption) |
| Qualifying portfolio interest | 0% under §871(h), subject to documentation and exclusions |
| Other interest | 30% |
| Ordinary portfolio securities gains | Generally exempt for an NRA; check US presence, ECI, real-property and other exceptions before applying 0% |
| Real estate dispositions (FIRPTA) | Generally 15% of amount realized, not final gain tax. For qualifying transferee-residence purchases: no withholding at ≤$300k, generally 10% above $300k through $1m, 15% above $1m; other exceptions/certificates require their own review (IRS Pub. 515). |
| ECI (effectively connected income) | Graduated rates as if US person; rare for portfolio investors |

## With the US-Israel income tax treaty (`tax/israel/treaties/us_israel.md`)

| Income type | Treaty rate |
|---|---|
| Ordinary US-corporation portfolio dividends | **25%**, Article 12(2)(a), for an eligible individual with valid treaty documentation |
| Interest | 17.5% by treaty, but most interest already exempt domestically |
| Ordinary portfolio securities gains | Generally 0% in US under applicable domestic/treaty rules; exceptions remain (`treaties/us_israel.md`) |

## Forms

- **W-8BEN** — generally valid through the end of the third calendar year following signing, unless a change of circumstances makes it incorrect. Expiry or missing documentation can cause statutory withholding; verify the actual account status.
- **1042-S** — US withholding-agent report of reportable payments and withholding. Reconcile the actual broker form with statements and the evidence required for an Israeli foreign-tax-credit claim; this file does not verify an account's form delivery.
- **1040-NR** — filing depends on facts, including US business/ECI, tax not satisfied by withholding, or claiming an eligible refund. Do not treat this brief list as an exhaustive filing rule.

## ETF nuances (US-domiciled vs UCITS)

This is the key practical decision driver for the user's plan:

| Fund domicile | Dividend WHT layer | Estate tax exposure |
|---|---|---|
| **US corporate ETF**, ordinary dividends (e.g., ordinary VOO/SCHD distributions) | General 25% US treaty ceiling for an eligible Israeli individual; Israeli foreign-tax credit subject to limitations | Generally US-situs; use the estate computation in `estate_tax_nonresidents.md` |
| **US RIC interest-related / short-term capital-gain distributions** | Certain properly designated distributions are exempt from chapter 3 withholding under domestic law. For SGOV, check fund tax supplements and actual broker classification, not the ordinary-equity dividend rate | Distribution exemption does not make shares of a US fund non-US-situs |
| **Irish corporate UCITS** holding US stocks | Eligible fund can incur 15% US tax on underlying US dividends under the US-Ireland treaty; this is **US withholding at fund level**, not Irish tax. Investor-level Israeli tax is separate | Foreign corporate shares are generally non-US-situs; verify legal structure |

Do **not** assume US ETFs and Irish UCITS are income-tax neutral. Compare fund-level leakage, whether a credit is available to the investor, distribution versus accumulation, realization timing, fees and estate exposure. An accumulating share class such as CSPX does not distribute its reinvested income to the investor as a cash dividend. A recommendation needs an instrument-specific after-tax comparison.

## Account reconciliation — instructions, not verified broker observations

- Broker statements should show gross distributions and any withholding; reconcile both rather than infer gross from cash received.
- Form 1042-S is generally due by March 15 of the following year (next business day when applicable), subject to applicable extensions/corrections. Do not promise February delivery by Schwab.
- For material US-source dividends, reconcile Form 1042-S, any corrections, statements and distribution classification. A form by itself does not establish that every withheld amount is eligible for Israeli credit.

## How agents should use this file

- **Cite this file** for any "what is the US tax on this US-domiciled ETF distribution" question.
- For the matched Israeli side, also cite `tax/israel/capital_gains.md`.
- For the structural UCITS-vs-US ETF reasoning, also cite `estate_tax_nonresidents.md`.
- If `last_verified` is older than 12 months OR `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual** — IRS Pub. 515 re-issued each year; treaty rates rarely change but worth verifying.
- **On-demand** — any change to the W-8BEN form or filing process triggers refresh.
