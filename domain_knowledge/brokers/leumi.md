---
topic: leumi_securities_tariff
jurisdiction: israel
last_verified: 2026-08-22
next_refresh_due: 2026-10-15
sources:
  - url: file://Resources/2026/Leumi/bank_charges_ID/ShowMailPDF (3).pdf
    label: "הטבות בעמלות — personalised benefits letter, issued 19/07/2026"
    retrieved: 2026-08-22
    tier: 1
  - url: file://Resources/2026/Leumi/bank_charges_ID/דוח שנתי_מפורט_2025_22-08-2026.pdf
    label: "דוח שנתי מפורט 2025 (bank ID annual report), issued 10/02/2026"
    retrieved: 2026-08-22
    tier: 1
  - url: file://Resources/2026/Leumi/bank_charges_ID/ShowMailPDF.pdf
    label: "התפלגות גביית עמלות ני\"ע — Bank of Israel weighted-average disclosure, H1 2026"
    retrieved: 2026-08-22
    tier: 1
  - url: https://www.leumi.co.il/he/fees/
    label: "Leumi published תעריפון עמלות"
    retrieved: 2026-08-22
    tier: 1
---

# Bank Leumi — securities tariff actually in force for this household

The rates below are the household's **personally negotiated** benefits, not
Leumi's list prices. They come from the 19/07/2026 benefits letter for
securities deposit `44745210`, cross-checked against charges actually levied in
the 2025 annual report and the H1-2026 statements.

> Every benefit here has an **expiry date**. Most of the valuable ones fall on
> **31/12/2026**. Past that date the fallback is the list tariff, which is
> roughly 10x worse. See "Renewal calendar" below.

## Trading commissions

| Item | Instrument | Channel | Rate | Minimum | Valid to |
|---|---|---|---|---|---|
| 4.1.4.1 | Foreign securities | LeumiTrade | 0.07% | **$7** | 19/08/2026 |
| 4.1.4.1 | Foreign securities | LeumiTrade | 0.06% | **disputed — see below** | 31/12/2026 |
| 4.1.1.1 | Israeli securities | LeumiTrade | 0.07% | ₪7 | 31/12/2026 |
| 4.1.1.1 | Non-linked bonds (אג"ח לא צמוד) | — | 0.07% | ₪7 | 31/12/2026 |
| 4.1.1.1 | TASE tracking funds (קרן מחקה) | LeumiTrade | 0.07% | ₪7 | 31/12/2026 |
| 4.1.4.2 | Options | — | $5 per contract | — | 20/11/2026 |

List prices, for reference: foreign 0.900% (min $25, max 30% of payment /
$7,500); Israeli 0.650% (min ₪27, max 30% / ₪7,000); options 5% (min $7/contract).

### UNRESOLVED: the minimum from 20/08/2026

The benefits letter prints two consecutive blocks for item 4.1.4.1. The
23/12/2025–19/08/2026 block states `0.07% מהעסקה` **and** a separate explicit
line `מינימום: 7 דולר`. The 20/08/2026–31/12/2026 block states
`0.06% מהעסקה` and **no minimum line at all**.

The letter's own boilerplate reads:

> ההטבה אינה חלה על תעריפי המינימום אלא אם צוין במפורש אחרת.

Read literally, the minimum reverted to the **list $25** on 20/08/2026. This
may instead be a template/rendering omission — but nothing in the document
supports assuming so.

**Why it matters far more than the headline rate.** At 0.06%, a $25 floor binds
on every ticket below **₪121,250**. In H1-2026, 29 of 30 foreign trades were
below that. Modelled trade-by-trade over the actual H1-2026 tape:

| Scenario | H1-2026 commissions |
|---|---|
| As charged (0.07% / $7 min) | ₪1,132.13 |
| 0.06% with a $25 min | **₪2,262.93** (+100%) |

The percentage becomes decorative — you simply pay a flat $25 per trade.

**Status: awaiting written confirmation from Leumi.** Until an amended benefits
letter arrives, `argosy.services.broker_fees` models the **conservative ($25)**
reading and flags the dispute on every estimate. Do not silently switch to $7.

## Custody (דמי ניהול ניירות ערך)

| Item | Scope | Status | Valid to |
|---|---|---|---|
| 4.1.5.1 | Israeli securities | **Full exemption** | 31/12/2026 |
| 4.1.5.2 | Foreign securities | **Full exemption** | 31/12/2026 |

List: 0.150%/quarter Israeli (max ₪3,700/security, ₪11,500/deposit);
0.200%/quarter foreign (max ₪7,400/security, ₪23,000/deposit).

**Value of the exemption — measure it against reality, not list.** The rate
displayed in the annual report is *annualised* (footnote: `שיעור עמלה שנתי
בפועל`); the quarterly charge is one quarter of it. Verified against Q1-2025,
before the exemption was granted:

| Holding | Value | Charged | Quarterly | Annualised |
|---|---|---|---|---|
| SCHWAB DVD EQ | ₪467,799 | ₪93.56 | 0.020% | 0.08% |
| ROCKET COS | ₪224,381 | ₪44.88 | 0.020% | 0.08% |
| WRLD MSCI (MTF) | ₪185,723 | ₪27.86 | 0.015% | 0.06% |

So the pre-exemption negotiated rate was **0.08%/yr foreign, 0.06%/yr Israeli**
— within noise of the 0.09%/0.06% peer averages. On a ₪3.1M deposit the
exemption is worth roughly **₪2,500/yr** against what was actually being paid,
and ₪19,000–25,000/yr against list. Quote the ₪2,500 figure; the list-price
number is real only if the exemption lapses entirely.

## FX and transfers

| Item | Service | Benefit | Valid to |
|---|---|---|---|
| 5.1.1.1 | Exchange ILS↔FX (list 0.200%, min $7.20, max $3,000) | Exempt | 30/06/2027 |
| 5.1.1.2 | Exchange FX↔FX (list 0.300%, min $10) | Exempt | 30/06/2027 |
| 5.1.1.3 | Self-service exchange withdrawal ($7.20) | Exempt | 30/06/2027 |
| 5.1.9.1 | FX transfer to/from abroad (0.225%, min $22.50, max $150) | 75% off **the percentage only** | 30/06/2027 |
| 5.1.10.2 | FX transfer to/from another Israeli bank >$600 (0.150%, min $22.50) | 75% off the percentage only | 30/06/2027 |
| — | Conversion ILS/FX at שער מוסכם | "0.67% הטבה" | 30/06/2027 |
| — | Conversion FX/FX at שער מוסכם | "1.34% הטבה" | 30/06/2027 |

**Transfer minimums are not discounted.** The 75% benefit cuts 0.225% → 0.056%,
but the **$22.50 minimum stands**, so the percentage only exceeds the floor
above **$40,000** (abroad) or **$60,000** (domestic, at 0.150% → 0.0375%).
Funding the Schwab account in small increments costs a flat $22.50 per transfer,
plus correspondent charges that sit outside the benefit entirely.

**The שער מוסכם spread is not disclosed in any statement.** Fees on conversion
are effectively zero (₪49.87 on ₪250,000 converted in 2025 = 0.0199%), but the
*spread* is invisible. The 1.34% FX/FX figure being exactly 2 × 0.67% suggests
these are per-leg rate improvements against Leumi's customer buy/sell rate — not
a discount off the spread margin. **Unverified.** To measure: record ILS debited
÷ FX credited within seconds of execution and compare to a contemporaneous
market mid.

## Current account

Effectively every teller (list ₪5.50/action) and direct-channel (list
₪1.65/action) fee is exempt through **30/04/2027**. 2025 charges on these lines
were ₪0.00 across 136 actions. Not a material cost centre.

## What the household actually paid

| Period | Amount |
|---|---|
| 2025 — securities buy/sell/redemption | ₪3,304.49 |
| 2025 — custody | **−₪9.24** (net refund) |
| 2025 — all other (options ₪237.30, FX ₪75.58, early repayment ₪60) | ₪372.88 |
| **2025 total** | **≈ ₪3,668** |
| H1-2026 — 33 trades, ₪1,519,376 turnover | ₪1,132.13 (blended 0.0745%) |
| July 2026 alone | ₪801.56 |

**July 2026 is unexplained** — 71% of the entire H1 bill in a single month, and
it predates the 20/08 minimum change. The trade-level detail is not in any
document currently held. Pull it before projecting an annual run-rate.

## Benchmark: what Leumi's other clients pay

From the Bank of Israel weighted-average disclosure Leumi issued for H1-2026,
banded by deposit size. This household sits in the **מעל ₪1,000 אלף** band
(deposit ₪3.12M at 31/12/2025) — the bottom row:

| | Leumi average, this band | This household |
|---|---|---|
| Foreign securities buy/sell | 0.13% | 0.07% |
| Israeli securities buy/sell | 0.10% | 0.07% |
| Custody, Israeli (annual) | 0.06% | 0.00% |
| Custody, foreign (annual) | 0.09% | 0.00% |

The package is more favourable than the average client in the same size band on
all four lines. Do not benchmark against the headline 0.21%/0.15% figures — those
are all-band weighted averages and flatter the comparison.

## Renewal calendar

| Benefit | Expires |
|---|---|
| Options $5/contract | 20/11/2026 |
| Foreign securities 0.06% | **31/12/2026** |
| Israeli securities 0.07% / ₪7 | **31/12/2026** |
| Custody exemption, Israeli **and** foreign | **31/12/2026** |
| Current-account exemptions | 30/04/2027 |
| FX exchange exemptions and transfer benefits | 30/06/2027 |

Everything materially valuable lands on **31/12/2026**. Open the renewal
conversation in **October 2026**, not December.

## Negotiating leverage (factual, not tactical advice)

- A same-owner securities transfer out is capped by Bank of Israel at **₪5 plus
  actual third-party expense** — moving is cheap, which is the leverage.
- The account-portability regime (ניוד חשבונות) moves securities and compatible
  FX balances online in seven business days, free, but is **all-or-nothing**; a
  partial securities transfer must be requested separately. Mortgages, loans,
  deposits and pledged assets do **not** move — so the ₪334k Leumi mortgage is
  weak leverage, contrary to intuition.
- The bank's own letter suggests checking `עסק קטן` classification if turnover
  is under ₪5M. That framework keys on genuine business activity and turnover,
  **not portfolio size**; a personal joint account already receives retail
  tariffs. No obvious benefit here.

## Gotchas

- **Scope is deposit- and channel-specific.** The benefits attach to deposit
  `44745210` and, for foreign trades, the LeumiTrade channel. Phone/banker
  orders, a second deposit, or a newly opened subaccount may fall outside them.
- **No stacking** (`אין כפל הטבות`) — where several benefits could apply, the
  bank applies the single highest.
- **Pass-through costs sit outside the negotiated percentage**: exchange fees,
  SEC/FINRA, ADR fees, correspondent charges, corporate-action and
  securities-transfer expenses.
- The commission percentages here are **execution cost only**. Fund expense
  ratios, tracking difference, bid/ask spread and dividend withholding are
  separate and generally larger. See
  `domain_knowledge/tax/us/estate_tax_nonresidents.md` for the situs question,
  which is unrelated to fees but drives the same instrument choices.
