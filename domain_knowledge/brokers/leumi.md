---
topic: leumi_securities_tariff
jurisdiction: israel
last_verified: 2026-09-08
next_refresh_due: 2026-10-15
code_references:
  - argosy/services/broker_fees.py
catalog_sources:
  - id: 113
    as_of: 2026-09-11
    label: "Existing September 11 securities portfolio export; dated custody evidence, not proof of campaign enrollment, FX execution cost or contribution history"
sources:
  - url: https://www.boi.org.il/publications/pressreleases/30-03-25/
    tier: 1
    label: "Original Bank of Israel benefits framework announcement; verify status and scope rather than search snippets"
  - url: https://www.leumi.co.il/he/node/2551
    retrieved: 2026-09-12
    tier: 1
    label: "Dated February 22, 2026 campaign announcement; not evidence of current enrollment or household eligibility"
  - url: file://Resources/2026/Leumi/bank_charges_ID/ShowMailPDF (4).pdf
    label: "July-2026 collected fees, issued 09/08/2026; page 2 itemizes the full total as securities commissions"
    as_of: 2026-07-31
    tier: 1
  - url: file://Resources/2026/Leumi/bank_charges_ID/ShowMailPDF (1).pdf
    label: "Actual H1-2026 securities custody charges, issued 29/07/2026; historical evidence only"
    retrieved: 2026-09-12
    tier: 1
  - url: file://Resources/2026/Leumi/bank_charges_ID/ShowMailPDF (2).pdf
    label: "Actual H1-2026 securities trade commissions, issued 29/07/2026; does not establish post-August minimum"
    retrieved: 2026-09-12
    tier: 1
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
  - url: https://www.leumi.co.il/he/economic-info/commissions_and_fees
    label: "Leumi published תעריפון עמלות"
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.bankleumi.co.il/static-files/Commissions_Leumi/AmlotYechidimL.pdf
    label: "Published full tariff; distinguish it from the personal benefits letter"
    retrieved: 2026-09-12
    tier: 1
---

# Bank Leumi — documented household benefits and execution-cost estimates

The rates below distinguish the household's **personally negotiated** benefits
from list prices and the conservative planning assumption. The cited July letter,
2025 annual report and H1-2026 charge statements are historical evidence with
different scopes; earlier actual charges do not resolve the post-August minimum.

> Each benefit has its own expiry. The explicit custody exemption ends
> **31/12/2026**, but the additional securities-benefit table remains valid
> through **30/06/2027**. There is no automatic January reversion to list prices.
> Later bank amendments and actual trade-date eligibility remain separate facts.

## Trading commissions

| Item | Instrument | Channel | Rate | Minimum | Valid to |
|---|---|---|---|---|---|
| 4.1.4.1 | Foreign securities | LeumiTrade | 0.07% | **$7** | 19/08/2026 |
| 4.1.4.1 | Foreign securities, page-1 row alone | LeumiTrade | 0.06% | List $25 on this row; superseded by greater applicable benefit below | 31/12/2026 |
| 4.1.1.1 | Israeli securities | LeumiTrade | 0.07% | ₪7 | 31/12/2026 |
| 4.1.1.1 | Non-linked bonds (אג"ח לא צמוד) | — | 0.07% | ₪7 | 31/12/2026 |
| 4.1.1.1 | TASE tracking funds (קרן מחקה) | LeumiTrade | 0.07% | ₪7 | 31/12/2026 |
| 4.1.4.2 | Options | — | $5 per contract | — | 20/11/2026 |

List prices, for reference: foreign 0.900% (min $25, max 30% of payment /
$7,500); Israeli 0.650% (min ₪27, max 30% / ₪7,000); options 5% (min $7/contract).

### Resolved source omission: the additional benefits table

The benefits letter prints two consecutive blocks for item 4.1.4.1. The
23/12/2025–19/08/2026 block states `0.07% מהעסקה` **and** a separate explicit
line `מינימום: 7 דולר`. The 20/08/2026–31/12/2026 block states
`0.06% מהעסקה` and **no minimum line at all**.

The letter's own boilerplate reads:

> ההטבה אינה חלה על תעריפי המינימום אלא אם צוין במפורש אחרת.

That is only page 1. **Pages 7–9 explicitly supply competing minima**, and
pages 6/9 say the greatest applicable benefit wins without stacking. Reading
only the first table caused the old $25-vs-$7 dispute. The written overlap rule
resolves it; a new bank letter is not intrinsically required to interpret this one.

Additional schedule, valid **01/07/2026–30/06/2027**:

| Service/channel | Lower portfolio-value tier | Upper portfolio-value tier |
|---|---|---|
| Foreign, banker | 0.06%, min $6 | 0.06%, min $6 |
| Foreign, direct | 0.06%, min $6 | 0.05%, min $6 |
| Israeli, banker | 0.06%, min ₪9 | 0.06%, min ₪9 |
| Israeli, direct | 0.06%, min ₪9 | 0.05%, min ₪9 |

The letter prints lower tier ₪0–3,499,999 and upper tier “above ₪3,500,000”.
Do not invent current portfolio eligibility or silently resolve the literal exact
boundary. The foreign $6 minimum is common to both channels and both tiers.
For overlapping Israeli rows through December, compare **complete** fees:
`min(max(0.0007 × ticket, 7), max(applicable_rate × ticket, 9))`, subject to
tariff caps and actual channel eligibility. Do not combine the lowest percentage
with the lowest minimum into an invented schedule.

`broker_fees.LEUMI_FOREIGN` now uses the documented **0.06%/$6** planning row,
without assuming upper-tier eligibility. Before separately charged expenses,
$3,000/$10,000/$50,000 tickets cost **$6/$6/$30**; the rate/minimum crossover
is **$10,000**. The exported Israeli constant is the original 0.07%/₪7 row,
not an automatic best-row selector. Existing historical H1 charges remain under
their original terms; do not reprice them as if July benefits applied retroactively.

## Custody (דמי ניהול ניירות ערך)

| Item | Scope | Status | Valid to |
|---|---|---|---|
| 4.1.5.1 | Israeli securities | **Full exemption** | 31/12/2026 |
| 4.1.5.2 | Foreign securities | **Full exemption** | 31/12/2026 |

List: 0.150%/quarter Israeli (max ₪3,700/security, ₪11,500/deposit);
0.200%/quarter foreign (max ₪7,400/security, ₪23,000/deposit).

The page-9 schedule provides **0.015% per quarter** for both Israeli and foreign
custody through 30/06/2027. The full exemption wins through December. After that,
the benefit percentage—not list—survives, subject to applicable minimums/caps.
Page 9 prints minima of 5.50/security and 27/deposit for Israeli, 30/security and
60/deposit for foreign; currency/aggregation follow the applicable tariff.
Do not treat 0.06% annualized as a universally all-in custody charge.

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
number is relevant only if no lower valid benefit survives; the additional
0.015% quarterly schedule currently extends through June 2027.

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

**The שער מוסכם spread is not disclosed in any statement**, but it has now been
measured. Fees on conversion are effectively zero (₪49.87 on ₪250,000 converted
in 2025 = 0.0199%); the cost sits in the rate.

Measured 21/08/2026 against the BoI representative rate of 2.9910, using each
bank's own שער העברות והמחאות:

| Bank | Half-spread (one-way) | Agorot (full bid/ask) |
|---|---|---|
| Mizrahi-Tefahot | 0.80% | 4.79 |
| Beinleumi | 0.90% | 5.36 |
| **Leumi** | **0.92%** | 5.50 |
| Mercantile | 1.01% | 6.03 |
| Discount | 1.10% | 6.60 |

**0.92% is the RACK rate — it is not what this household pays.** The benefits
letter grants "0.67% הטבה" on the שער מוסכם, so the effective one-way cost is
roughly **0.92% − 0.67% ≈ 0.25%**, or ~₪625/yr on the ₪250,000 converted in
2025. Do not quote the 0.92% as this household's cost; that error was made twice
during the 2026-08-22 review.

**The base of the 0.67% needs confirmation.** A percentage-point reduction
and a percentage discount on the spread are different operations. The old
“roughly $250 per $100,000” comparison was not derived from a specified common
base and must not be used. Reconcile actual ILS debited / FX credited, direction,
timestamp and the applicable reference rate; a daily representative rate is not
an executable simultaneous market quote. Preserve the conditional estimate above
as an illustration, not a verified household execution cost.

**Unverified negotiation/competitor comparison:** no dated, cited quotes in this
file establish a market negotiation ceiling or the current competing FX spreads.
Do not reject an offer or declare Leumi cheapest from forum consensus. Compare
written executable quotes using the same currency amount, direction, timestamp,
spread definition and transfer costs. Historical fixed-agora illustrations are
not current offers, and the household's own 0.25% estimate remains conditional
on the benefit interpretation above.

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
| July 2026 — securities commissions collected, net of refunds | ₪801.56 |

The July amount is supported by `ShowMailPDF (4).pdf`, issued 09/08/2026:
page 2 itemizes ₪10.53 Israeli plus ₪791.03 foreign securities commissions,
which sum to the full ₪801.56. This is the **collection month**, not necessarily
the transaction month; amounts are net of refunds, foreign-currency charges use
the representative rate at collection, and third-party expenses are excluded.
Preserve those scope differences when comparing H1 charges; one month does not
establish a future annual securities-fee run rate.

## Historical brokerage comparison (2026-08-22; not current verified offers)

**On trading: barely.** Modelled at ~70 trades/yr, ₪45,000 average ticket,
40 foreign + 30 Israeli, USD/ILS 2.9910:

| Provider | Annual trading cost |
|---|---|
| Psagot Trade | ₪1,528 |
| Excellence Trade | ₪1,543 |
| Meitav Trade | ₪1,678 |
| IBI Trade | ₪1,977 |
| **Leumi, current package** | **₪2,205** |

Switching brokerage saves **₪530–680/yr**. Negotiating the *rate* from 0.07% to
0.05% saves ~₪630/yr — the same benefit without moving. Negotiating the
*minimum* appeared small under the historical **$7** scenario. That conclusion
does **not** establish today's economics: the additional schedule has a **$6**
foreign minimum. Recompute trade-by-trade at the applicable date before relying
on the comparison. The competitor inputs below lack attached dated quotes and
are unverified research leads, not operative prices or a reason to switch brokers.

**Peer benchmarks, >₪1M band, from each bank's Directive 414 disclosure:**
Leumi 0.10% Israeli / 0.13% foreign / 0.06% / 0.09% custody · Hapoalim 0.16% /
0.15% / 0.09% / 0.07% · Mizrahi 0.13% / 0.12% / 0.08% / 0.11%. The current
package beats every one of those. Best commonly-reported negotiated bank deals
cluster at **0.06–0.08% with a ₪5–7 minimum** and custody exempt.

⚠ **Interactive Israel is not IBKR.** It is a brand of MEXEM Ltd (Cyprus,
CySEC) operating as an IBKR introducing broker under a s.49a permit, and per its
own site **Israeli private clients cannot trade TASE through it** (recheck current
account terms). The old **~₪1.9M Israeli-ETF** claim was not supported by a cited
current classification and must not disqualify a move; derive the TASE-listed
share from the current holdings first. The historical FX illustration is
(0.002%, min $2) but a wire costs ~$135, which erases most of the gain at this
conversion volume.

## Leverage available in 2026

- **Directive 414** (הוראת ניהול בנקאי תקין 414, "גילוי עלות שירותים בניירות
  ערך") compels every bank to publish semi-annually the weighted-average
  commission it *actually* collected, banded by portfolio size. It is the bank's
  own admission of what it charges comparable clients — the single most useful
  document to put on the table.
- **Historical February-22 campaign announcement:** the existing-account route
  required a securities transfer of at least ₪100,000 for a refund based on the
  lower of annual portfolio loss and buy/sell commissions, capped at ₪3,000 and
  calculated in January 2027. The full custody waiver belonged to the separate
  new/inactive-customer route, not automatically to an existing-account transfer.
  Current enrollment window and household eligibility are unverified; do not
  present this dated announcement as an available offer without written terms.
- **Pepper:** the former zero-cost campaign claim has no captured dated offer.
  Do not present it as an available comparator. The published tariff has charges;
  any promotion requires its actual eligibility, expiry and written terms.
- A separate Leumi campaign offering a year of commission-free trading drew
  **public criticism from the Banking Supervisor**.
- The Competition Authority declared the five big banks a **קבוצת ריכוז**
  (concentration group); the banks appealed, live as of August 2026.
- The **BoI ₪3bn benefits framework** (₪1.5bn/yr, Q2-2025 → Q1-2027) obliges
  banks to report delivered amounts. A customer may legitimately ask what share
  their branch has allocated.

## Open question: are third-party expenses riding on top?

Leumi's tariff §4(א)(4) note 8 reserves the right to charge third-party expenses
**in addition** to the negotiated percentage — foreign broker fees, SEC/FINRA,
stamp duty, SWIFT. Hapoalim publishes its equivalent openly at $0.004/share, min
$3/trade. Whether Leumi actually levies these here is **unresolved**; only a
trade confirmation settles it. Check one before assuming 0.07% is all-in.

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
all four lines on this historical comparison. The 0.21%/0.15% figures belong to
the **smallest-deposit band**, not an all-band average; do not substitute them.

## Renewal calendar

| Benefit | Expires |
|---|---|
| Options $5/contract | 20/11/2026 |
| Foreign securities 0.06% | **31/12/2026** |
| Israeli securities 0.07% / ₪7 | **31/12/2026** |
| Custody exemption, Israeli **and** foreign | **31/12/2026** |
| Current-account exemptions | 30/04/2027 |
| FX exchange exemptions and transfer benefits | 30/06/2027 |
| Additional portfolio-banded securities schedule | **30/06/2027** |

Review the December exemptions in October, and the additional schedule before
June expiry. December is not a blanket cliff to list tariffs.

## Negotiating leverage (factual, not tactical advice)

- A same-owner securities transfer out is capped by Bank of Israel at **₪5 plus
  actual third-party expense** — moving is cheap, which is the leverage.
- The account-portability regime (ניוד חשבונות) moves a current account online
  in ~7 business days, free. **Whether a securities portfolio travels with it is
  DISPUTED** and must be checked before it is used as leverage: one review held
  that securities and compatible FX balances move; a second, better-sourced one
  held that a תיק ני"ע is **not** part of the automated flow and moves only by
  separate in-kind transfer instruction, with the ISA's push to extend one-click
  switching to securities accounts never enacted. Assume the separate
  instruction until confirmed. Either way mortgages, loans, deposits and pledged
  assets do **not** move — so the ₪334k Leumi mortgage is weak leverage,
  contrary to intuition.
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
