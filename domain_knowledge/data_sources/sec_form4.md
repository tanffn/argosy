---
topic: sec_form_4_insider_transactions
jurisdiction: us
last_verified: 2026-05-02
next_refresh_due: 2027-05-02
sources:
  - url: https://www.sec.gov/about/forms/form4data.pdf
    retrieved: 2026-05-02
    tier: 1
  - url: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4
    retrieved: 2026-05-02
    tier: 1
  - url: https://www.investor.gov/introduction-investing/investing-basics/glossary/forms-3-4-and-5
    retrieved: 2026-05-02
    tier: 1
---

# SEC Form 4 — insider transactions

Form 4 is the per-transaction disclosure that "insiders" — corporate
officers, directors, and beneficial owners of more than 10% of a class
of registered equity — must file with the SEC within **two business
days** of a transaction in their company's securities.

## What's in a Form 4

Each filing has a `<reportingOwner>` (the insider), an `<issuer>`
(the company), and a `<nonDerivativeTable>` and/or
`<derivativeTable>` of transactions. Each transaction row carries:

- `transactionCode` — the most important field; see codes below.
- `transactionDate` — when the trade happened (not when filed).
- `transactionShares`, `transactionPricePerShare`.
- `sharesOwnedFollowingTransaction` — post-trade holdings, useful
  for tracking conviction.

## Transaction codes (most common)

- **P** — open-market or private *purchase*. Highest signal: the
  insider chose to put their own cash into more stock.
- **S** — open-market or private *sale*. Read with care; many sales
  are 10b5-1 plan-driven (scheduled and routine).
- **A** — *grant/award* (RSU, stock award). Mechanical — not a
  conviction signal.
- **M** — *exercise/conversion* of a derivative (e.g. options
  exercise into common stock). Often paired with same-day F or S.
- **F** — *payment of exercise price or tax* via shares delivered.
  Mechanical — net of withholding.
- **G** — *bona fide gift*.
- **D** — sale to issuer (rare; e.g. buyback participation).
- **X**, **C**, **W** — option exercises, conversions, wills.

## Practical interpretation rules

1. **Cluster of P codes > lone P.** Multiple insiders buying within a
   short window is a stronger signal than one CEO buying alone.
2. **Discount A/M/F.** Grant-driven activity is mechanical
   compensation — not a sentiment signal.
3. **Scrutinize S codes for 10b5-1 plans.** A footnote citing "Rule
   10b5-1 trading plan adopted on YYYY-MM-DD" means the trade was
   pre-scheduled months in advance. Helpful as a sanity-check but
   doesn't carry the same weight as a discretionary sale.
4. **Mind the role.** A 10%-owner's trade has different implications
   than an independent director's. Officers (esp. CEO/CFO) carry the
   most informational weight.
5. **Two-day filing rule.** Form 4 is much fresher than 13F (which
   has a 45-day lag). For tactical signal, Form 4 wins.

## Cluster heuristics

- **Cluster buy** — 3+ insiders buying within 30 days, at least one
  with P code, total value > 0.5% of float = strong signal.
- **CEO buy after earnings** — the strongest single-name signal in
  the academic literature; tradable for 60-90 days post-filing.
- **Lone-sell** — typically not a signal absent corroborating data.
  Insiders sell for many non-information reasons (tax, divorce,
  diversification).

## Adapter usage

`argosy.adapters.data.sec_form4_adapter.SecForm4Adapter`:

- `get_recent_form4_for_ticker(ticker, days=30)` — issuer-side view
- `get_recent_form4_for_filer(cik, days=90)` — insider-side view
- 24h cache TTL (filings are within 2 business days of transaction;
  daily refresh catches everything).

## SEC API etiquette

Same as 13F: polite User-Agent (`Argosy/<version> admin@argosy.local`),
≤10 req/sec.
