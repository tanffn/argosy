---
topic: us_congressional_stock_act_filings
jurisdiction: us
last_verified: 2026-09-08
next_refresh_due: 2027-05-02
code_references:
  - argosy/adapters/data/capitoltrades_adapter.py
sources:
  - url: https://www.ethics.senate.gov/public/index.cfm/ethics-faqs
    tier: 1
    retrieved: 2026-09-12
    label: "Senate primary PTR instructions and excepted investment funds"
  - url: https://ethics.house.gov/financial-disclosure/
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.capitoltrades.com/trades
    retrieved: 2026-08-11
    tier: 2
  - url: https://disclosures-clerk.house.gov/FinancialDisclosure
    retrieved: 2026-08-31
    tier: 1
  - url: https://efdsearch.senate.gov/search/
    retrieved: 2026-05-02
    tier: 1
---

# capitoltrades.com — US Congress STOCK Act trade disclosures

`capitoltrades.com` is a third-party aggregator that reformats US
Congress members' Periodic Transaction Reports (PTRs), filed under the
**Stop Trading on Congressional Knowledge Act of 2012** ("STOCK Act"),
into a sortable web table.

## Background

- Members of the House and Senate, plus their senior staff, must
  disclose any covered transaction (stocks, bonds, certain funds) of
  more than $1,000 within **30 days of being notified**, and not
  later than **45 days after the transaction**.
- Filings are public on `disclosures-clerk.house.gov` (House) and
  `efdsearch.senate.gov` (Senate), subject to their access/use procedures.
  Do not promise a uniform CSV/PDF feed from both. Capitol Trades is downstream,
  and its availability does not establish complete coverage.

## What's in a record

The following is the expected adapter record shape, not a certification of the
current aggregator feed. September-12 live access was blocked (429/403), so current
field coverage and exact amount-bracket endpoints remain unverified. Preserve
the upstream disclosed range; never substitute an estimated exact trade amount.

- Politician name, party, chamber (House / Senate), state.
- Issuer + ticker (sometimes — for funds the ticker is empty).
- Transaction type: buy / sell / exchange.
- Transaction date (when the trade actually happened).
- Disclosure date (when the PTR was filed).
- Amount range — disclosed as a coarse bracket (e.g. `$1,001 – $15K`,
  `$15K – $50K`, `$1M – $5M`). **No exact amounts.**

## Practical caveats

1. **No calibrated return advantage is established here.** Treat disclosures as
   research leads, not evidence that copying them beats the market. A performance
   claim needs named studies or Argosy's dated, out-of-sample evaluation.
2. **Reporting lag.** A trade can be up to 45 days old by the time
   it appears. Combined with PTR-batching by some offices, the
   effective lag is often longer.
3. **Spouse and dependent trades** are also disclosed; some flagship
   "Pelosi"-style trades are technically the spouse's. The aggregator
   doesn't always make this distinction crisp.
4. **No size precision.** A $1M-$5M bracket spans a 5x range; sizing
   any signal-derived position based on the bracket is foolish.
5. **Source-of-truth lives upstream.** capitoltrades.com is not the
   regulator; the canonical filings are at the House/Senate
   disclosure sites. If you need to cite, cite the upstream.

## Useful patterns

- **Research hypotheses, not verified predictive rules:** examine clustered trades
  and relevant committee membership, then evaluate outcomes. Neither attribute
  establishes superior returns or inside knowledge.
- **Pre-IPO / pre-announcement** windows are the historically
  scrutinized cases.

## Adapter usage

`argosy.adapters.data.capitoltrades_adapter.CapitolTradesAdapter`:

- `list_recent_trades(days=30)` — across all members
- `list_trades_for_politician(slug)` — by politician slug
- `list_trades_for_ticker(ticker, days=365)` — by issuer
- 24h cache TTL.

## Etiquette

- Polite User-Agent: `Argosy/<version> ...`.
- The site is HTML-rendered (BeautifulSoup); be tolerant of layout
  drift. The adapter raises `MissingDataSourceError` rather than
  emitting partial data on parser failure.
