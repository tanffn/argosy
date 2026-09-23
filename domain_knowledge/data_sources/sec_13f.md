---
topic: sec_form_13f_hr
jurisdiction: us
last_verified: 2026-09-13
next_refresh_due: 2027-05-02
code_references:
  - argosy/adapters/data/sec_13f_adapter.py
sources:
  - url: https://www.sec.gov/files/form13f.pdf
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.sec.gov/divisions/investment/13ffaq.htm
    retrieved: 2026-09-13
    tier: 1
  - url: https://efts.sec.gov/LATEST/search-index?q=&forms=13F-HR
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=13F-HR
    retrieved: 2026-05-02
    tier: 1
---

# SEC Form 13F-HR — quarterly institutional holdings

Form 13F-HR is the quarterly disclosure all institutional investment
managers (banks, insurance companies, hedge funds, family offices,
pensions, etc.) exercising investment discretion over **$100M or more** in
Section 13(f) securities, subject to the rule's measurement and filing conditions, must file
with the SEC under §13(f) of the Securities Exchange Act.

## What 13Fs reveal

- **Long US-listed equity positions.** Common stock, ETFs, ADRs, and
  certain convertibles. The `value` field is reported in *thousands of
  USD* under the legacy format and rounded to the nearest *whole USD*
  under the format implemented **January 3, 2023**. This is not a
  2023-Q3 reporting-period cutoff. Downstream consumers must inspect
  filing/schema provenance rather than assume a multiplier from the quarter.
- **Long calls and puts** on those equities (the `putCall` field on
  the information table row distinguishes them; absent for spot
  positions).

## What 13Fs do **not** reveal

- **Short stock/options positions.** A missing long holding does not prove
  no exposure. Purchased puts are separately reportable but are not short
  positions in the report; other hedges can remain outside the information table.
- **Foreign-listed equities, fixed income, futures, swaps, FX, crypto.**
  Out of scope.
- **Trading dynamics inside the quarter.** A 13F is a single
  end-of-quarter snapshot. Positions taken and closed within the
  quarter are invisible.

## Cadence and lag

- **Filing window.** Generally within 45 days of quarter-end, with applicable
  weekend/holiday adjustments. Calculate the actual year's due date.
- **Confidential treatment.** Position-level relief can be granted for
  specified periods and extended by a new request; one year is not an
  absolute ceiling. An absent public position therefore does not prove
  that the manager lacks it.

## Practical interpretation rules

1. **Prefer YoY change to absolute holding.** A snapshot is one frame;
   the *delta* across quarters is the signal.
2. **Distinguish mandates and instruments.** An equity index ETF is not a
   money-market equivalent. Do not infer cash management or high conviction
   from the wrapper or manager name alone.
3. **Clustering is a research hypothesis.** Cross-filer overlap is a lead to
   investigate, not a proven buy signal or a calibrated probability of success.
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

- **Required User-Agent.** Use the configured real contact identity; verify
  `_user_agent()` and `ARGOSY_SEC_CONTACT_EMAIL`. A placeholder such as
  `admin@argosy.local` is not evidence of compliance or successful access.
- **Rate limit.** SEC guidance limits automated access to 10 requests/second.
  The adapter validates its configured interval and awaits the shared
  `wait_for_sec_request_slot` throttle on each request. This is an implementation
  safeguard, not proof that independent processes or other clients share its limit.
