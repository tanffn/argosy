---
topic: tipranks_analyst_aggregator
jurisdiction: us
last_verified: 2026-09-13
next_refresh_due: 2027-05-02
code_references:
  - argosy/adapters/data/tipranks_adapter.py
sources:
  - url: https://enterprise.tipranks.com/
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.tipranks.com
    retrieved: 2026-08-09
    tier: 2
  - url: https://www.tipranks.com/about
    retrieved: 2026-09-13
    tier: 2
  # TipRanks's hedge-fund signal is downstream of SEC 13F filings;
  # the SEC FAQ is the upstream regulator-canonical reference.
  - url: https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f
    retrieved: 2026-09-13
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

- A stable unauthenticated quota is **not established**. Do not promise
  10 lookups/day/IP or treat an old operational observation as provider policy.
  TipRanks offers enterprise APIs; that does not imply Argosy has credentials,
  a subscription, or permission to use a private endpoint.
- The adapter caches for **24 hours**, reducing repeated lookups while a cache
  entry remains valid. That does not establish a provider quota, permission,
  or a guarantee that the daily-brief loop stays within provider limits.
- **Tests must not fan out** against the live provider. Read the adapter/code
  evidence for local caching behavior; no quota or daily coverage guarantee follows
  from that cache setting.

## Signal half-life and caveats

1. **Analyst price-target average is anchored.** Brokerage models
   are slow to move; expect a multi-day to multi-week lag after a
   genuine fundamental change.
2. **Consensus label is hysteretic.** Going from `Strong Buy` to
   `Hold` typically requires a meaningful event; intra-quarter the
   label barely budges.
3. **Sentiment interpretation is an uncalibrated hypothesis.** An 80%/20%
   cutoff does not establish a profitable contrarian rule. Check observations,
   provider provenance and dated outcomes before giving it predictive weight.
4. **Hedge-fund signal may lag.** The 45-day deadline applies to 13F filings;
   attributing every TipRanks value to that feed is an Argosy inference,
   not a verified provider freshness promise.
5. **Implementation caveat:** Current per-ticker captures can be denied with
   HTTP 403, so the live signal is not established by the adapter interface.
   HTML/JSON layout can also change. Analyst and
   hedge-fund methods can raise `MissingDataSourceError`; blogger sentiment
   can fall back to an injected Finnhub adapter and ultimately return a
   zero-shaped default. That 0/0 result is **missing evidence**, not an observed
   neutral or bearish reading. Consult provider-outcome telemetry; do not
   use the default as a sourced sentiment fact. A fixed redesign cadence
   or permission to scrape is not established by this document.

## Adapter usage

`argosy.adapters.data.tipranks_adapter.TipRanksAdapter`:

- `get_analyst_consensus(ticker)` → consensus + price target +
  buy/hold/sell counts
- `get_blogger_sentiment(ticker)` → bullish_pct / bearish_pct
- `get_hedge_fund_signal(ticker)` → holding count + recent change
- 24h cache TTL.

## Etiquette

- Polite User-Agent: `Argosy/<version> ...`.
- Caller guidance: do not scrape in parallel. The adapter has no internal
  concurrency limiter; its interface alone does not establish serialized calls.
