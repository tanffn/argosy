---
topic: us_nonresident_alien_withholding
jurisdiction: us
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.irs.gov/individuals/international-taxpayers/nonresident-aliens
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.irs.gov/pub/irs-pdf/p515.pdf
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.irs.gov/forms-pubs/about-form-w-8-ben
    retrieved: 1900-01-01
    tier: 1
---

# US Withholding on Income to Nonresident Aliens

A nonresident alien (NRA) — the user's status under US tax law as an Israeli resident with no green card and no substantial presence in the US — is subject to US tax only on certain US-source income, primarily through withholding at source. This file covers the rules relevant to a portfolio investor at Schwab.

> **Verification status:** `last_verified: 1900-01-01`. Numbers below reflect long-standing IRS Pub. 515 guidance. Domain-refresh agent must re-verify annually as Treasury can adjust the published rates.

## Default statutory withholding (no treaty)

| Income type | US WHT rate (statutory NRA) |
|---|---|
| US-corporation dividends | 30% |
| Bank-deposit interest | 0% (statutory exemption) |
| Portfolio interest (most US Treasuries) | 0% (Internal Revenue Code §871(h) exemption) |
| Other interest | 30% |
| Capital gains on US securities | **0%** (NRA gains on portfolio securities not taxed) |
| Real estate gains (FIRPTA) | 15% withholding on gross sales price (recoverable on filing) |
| ECI (effectively connected income) | Graduated rates as if US person; rare for portfolio investors |

## With the US-Israel income tax treaty (`tax/israel/treaties/us_israel.md`)

| Income type | Treaty rate |
|---|---|
| US-corporation dividends | **15%** (with W-8BEN on file) |
| Interest | 17.5% by treaty, but most interest already exempt domestically |
| Capital gains on US securities | 0% in US (Israel taxes per `capital_gains.md`) |

## Forms

- **W-8BEN** — claims the treaty rate; valid through the end of the third calendar year following signing. Schwab requires this on file; expiry causes auto-revert to 30%.
- **1042-S** — Schwab issues to the NRA after each calendar year showing US-source income and US WHT applied. Required for the Israeli return.
- **1040-NR** — only required if the NRA has effectively-connected income, US wages, or wants to claim a refund of over-withheld tax. Most portfolio NRAs do not need to file.

## ETF nuances (US-domiciled vs UCITS)

This is the key practical decision driver for the user's plan:

| Fund domicile | Dividend WHT layer | Estate tax exposure |
|---|---|---|
| **US-domiciled ETF** (e.g., VOO, SCHD, SGOV) held directly | 15% US WHT applied at distribution, creditable in Israel | **Yes** — full US-situs asset; counts toward the $60K NRA exemption — see `estate_tax_nonresidents.md` |
| **UCITS ETF** (e.g., CSPX.L, FWRA.L, IUHC.L) — Ireland-domiciled | The fund itself absorbs Irish 15% WHT internally on US dividends; the *distribution* to the Israeli investor has 0% US WHT | **No** — Irish UCITS shares are not US-situs assets |

For a high-net-worth Israeli investor, the WHT difference between holding VOO directly and holding CSPX is roughly *neutral* (both end at ~15% US tax on US dividends). The **estate-tax** difference is the structural reason Plan v2.0 prefers UCITS for new diversifying buys while keeping the existing NVDA RSU position at Schwab.

## Withholding mechanics at Schwab

- Schwab applies US WHT *at distribution* — the NRA never sees the gross dividend.
- The 1042-S is issued by the broker each February for the prior calendar year.
- For a year with material US-source dividends (the user's SCHD and VOO holdings), the 1042-S is the source-of-truth for the Israeli foreign-tax-credit claim on the Israeli return.

## How agents should use this file

- **Cite this file** for any "what is the US tax on this US-domiciled ETF distribution" question.
- For the matched Israeli side, also cite `tax/israel/capital_gains.md`.
- For the structural UCITS-vs-US ETF reasoning, also cite `estate_tax_nonresidents.md`.
- If `last_verified` is older than 12 months OR `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual** — IRS Pub. 515 re-issued each year; treaty rates rarely change but worth verifying.
- **On-demand** — any change to the W-8BEN form or filing process triggers refresh.
