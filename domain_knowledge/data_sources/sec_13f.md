---
topic: sec_form_13f_hr
jurisdiction: us
last_verified: 2026-05-02
next_refresh_due: 2027-05-02
sources:
  - url: https://www.sec.gov/divisions/investment/13ffaq.htm
    retrieved: 2026-05-02
    tier: 1
  - url: https://efts.sec.gov/LATEST/search-index?q=&forms=13F-HR
    retrieved: 2026-05-02
    tier: 1
  - url: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=13F-HR
    retrieved: 2026-05-02
    tier: 1
---

# SEC Form 13F-HR — quarterly institutional holdings

Form 13F-HR is the quarterly disclosure all institutional investment
managers (banks, insurance companies, hedge funds, family offices,
pensions, etc.) with > $100M in qualifying US-equity AUM must file
with the SEC under §13(f) of the Securities Exchange Act.

## What 13Fs reveal

- **Long US-listed equity positions.** Common stock, ETFs, ADRs, and
  certain convertibles. The `value` field is reported in *thousands of
  USD* before 2023-Q3 and in *whole USD* from 2023-Q3 onward — the
  SEC changed the format mid-stream; downstream consumers must
  detect which schema applies.
- **Long calls and puts** on those equities (the `putCall` field on
  the information table row distinguishes them; absent for spot
  positions).

## What 13Fs do **not** reveal

- **Short positions.** 13F is long-only by statute. A fund showing 0
  on a 13F isn't necessarily flat the name — they could be net short
  via puts or off-13F instruments.
- **Foreign-listed equities, fixed income, futures, swaps, FX, crypto.**
  Out of scope.
- **Trading dynamics inside the quarter.** A 13F is a single
  end-of-quarter snapshot. Positions taken and closed within the
  quarter are invisible.

## Cadence and lag

- **Filing window.** Must be filed within 45 days of quarter-end.
  Quarter-end → 13F: 12/31 → 2/14, 3/31 → 5/15, 6/30 → 8/14, 9/30 → 11/14.
- **Confidential treatment.** Filers can request confidential delay
  for individual positions for up to 1 year. Berkshire famously did
  this for its 2020 building of a Chevron position.

## Practical interpretation rules

1. **Prefer YoY change to absolute holding.** A snapshot is one frame;
   the *delta* across quarters is the signal.
2. **Skim past tracker funds and index ETFs** when reading a value-
   investor's 13F — the long tail of a Berkshire 13F is often money-
   market-equivalent index ETFs, not Buffett conviction picks.
3. **Cluster across filers.** A position appearing simultaneously in
   3+ unrelated value-shop 13Fs is more meaningful than one filer
   loading up alone.
4. **Mind the 45-day lag.** By the time you read a 13F, the position
   may already be closed. Use 13Fs for *pattern recognition over
   quarters*, not for short-term timing.

## Adapter usage

`argosy.adapters.data.sec_13f_adapter.Sec13FAdapter` exposes:

- `list_recent_13f(days=90)` → all recent filings
- `get_filer_history(cik, quarters=4)` → one filer over time
- `get_filing_holdings(accession_number)` → parsed information table

90-day cache TTL — quarterly cadence makes anything tighter wasteful.

## SEC API etiquette

- **Required User-Agent.** SEC blocks/throttles missing or generic
  UAs; we send `Argosy/<version> admin@argosy.local` per their
  written policy.
- **Rate limit.** 10 req/sec sustained. The adapter doesn't fan out
  parallel calls, so we're naturally below.
