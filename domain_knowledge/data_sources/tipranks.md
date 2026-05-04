---
topic: tipranks_analyst_aggregator
jurisdiction: us
last_verified: 2026-05-02
next_refresh_due: 2027-05-02
sources:
  - url: https://www.tipranks.com
    retrieved: 2026-05-02
    tier: 2
  - url: https://www.tipranks.com/about
    retrieved: 2026-05-02
    tier: 2
  # TipRanks's hedge-fund signal is downstream of SEC 13F filings;
  # the SEC FAQ is the upstream regulator-canonical reference.
  - url: https://www.sec.gov/divisions/investment/13ffaq.htm
    retrieved: 2026-05-02
    tier: 1
---

# TipRanks — analyst sentiment aggregator

TipRanks aggregates Wall-Street analyst ratings, financial-blogger
sentiment, and 13F-derived hedge-fund positioning into per-ticker
summary scores. The free tier is a public website at
`https://www.tipranks.com/stocks/<TICKER>/forecast` (and sibling
pages for `blogger-opinions` and `hedge-funds-activity`).

## What we consume

- **Analyst consensus.** A `Strong Buy` / `Moderate Buy` / `Hold` /
  `Moderate Sell` / `Strong Sell` label, plus the count of analysts
  in each bucket and the average price target.
- **Blogger sentiment.** A bullish-pct / bearish-pct split derived
  from financial-blog posts TipRanks tracks.
- **Hedge-fund signal.** A count of 13F filers holding the name and
  a `increased` / `decreased` / `unchanged` recent-change indicator.
  This is downstream of public 13Fs (so it shares 13F's 45-day lag).

## Free tier and rate limits

- Roughly **10 lookups per day per IP** for unauthenticated traffic.
- The adapter caches for **24 hours** which keeps a daily-brief loop
  well under the limit (1 lookup per ticker per day).
- **Tests must not fan out** in tight loops; production code uses the
  cache; `daily_brief.py` caps fan-out to 10 tickers.

## Signal half-life and caveats

1. **Analyst price-target average is anchored.** Brokerage models
   are slow to move; expect a multi-day to multi-week lag after a
   genuine fundamental change.
2. **Consensus label is hysteretic.** Going from `Strong Buy` to
   `Hold` typically requires a meaningful event; intra-quarter the
   label barely budges.
3. **Blogger sentiment is noisy.** It captures retail-adjacent mood
   more than informed positioning. Useful as a contrarian indicator
   at extremes (>80% bullish, <20% bullish), low signal in the middle.
4. **Hedge-fund signal lags.** Same 45-day lag as the underlying
   13Fs — see `sec_13f.md`.
5. **Layout instability.** TipRanks rewrites their HTML and
   `__NEXT_DATA__` payload shape every few quarters. The adapter
   tries the JSON blob first, falls back to regex extraction, and
   raises `MissingDataSourceError` on a full miss rather than
   guessing.

## Adapter usage

`argosy.adapters.data.tipranks_adapter.TipRanksAdapter`:

- `get_analyst_consensus(ticker)` → consensus + price target +
  buy/hold/sell counts
- `get_blogger_sentiment(ticker)` → bullish_pct / bearish_pct
- `get_hedge_fund_signal(ticker)` → holding count + recent change
- 24h cache TTL.

## Etiquette

- Polite User-Agent: `Argosy/<version> ...`.
- Never scrape in parallel; the adapter is intentionally sequential.
