---
topic: gemelnet_mof_pension_data
jurisdiction: israel
last_verified: 2026-05-02
next_refresh_due: 2027-05-02
sources:
  - url: http://gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx
    retrieved: 2026-05-02
    tier: 1
  - url: https://www.gov.il/he/departments/ministry_of_finance
    retrieved: 2026-05-02
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%92%D7%9E%D7%9C%D7%A0%D7%98
    retrieved: 2026-05-02
    tier: 2
---

# Gemelnet — Israeli Ministry of Finance pension performance dataset

`gemelnet.mof.gov.il` is the Capital Markets, Insurance & Savings Authority's
public portal for performance data on every Israeli pension/savings
vehicle: `kupot gemel`, `karnot hishtalmut`, and `karnot pensia`. It is
the canonical, free, no-auth source for fund-level returns.

> Where this fits: the user's *private* account-level data (current
> balances, vesting, contributions) is held by the manager (Migdal,
> Harel, Altshuler Shaham, ...) — Argosy does not scrape that. We
> only consume the *fund-level public performance* data via gemelnet.
> User-side pointers (Wobi, Swiftness, nofryers.com) help users find
> their accounts; once they have the fund_id, we can track performance.

## What it provides

- Per-fund 12-month, 36-month, 60-month, and YTD nominal returns.
- A "benchmark" / sector-average return (`תשואת ייחוס` /
  `ממוצע ענפי`) for relative-performance comparison.
- Fund-type classification (`קופת גמל`, `קרן השתלמות`, `קרן פנסיה`).
- The managing company (`חברה מנהלת`).
- Last-update date (refreshed monthly upstream).

## Refresh cadence

- Upstream: monthly. The MoF publishes after end-of-month NAVs are
  rolled up — typically 6-8 weeks after the close of a calendar month.
- Argosy adapter cache: 24h TTL (`prices_cache` rows keyed
  `gemelnet:fund_returns:<id>:<period>`). Daily refresh is more than
  enough given monthly upstream cadence.
- Annual loop: opportunistically refreshes every fund the user has
  declared in `identity.pensions`.

## Adapter API surface

`argosy.adapters.data.gemelnet_adapter.GemelnetAdapter`:

- `list_funds(*, fund_type=None)` — full universe; optionally filtered
  to one canonical type.
- `get_fund_returns(fund_id, *, period="12m")` — `{return_pct,
  benchmark_return_pct, relative_to_benchmark_pct, last_updated,
  source_url, ...}`. The `relative_to_benchmark_pct` is the
  return-minus-benchmark gap that the gap_tracker / advisor surfaces
  as an under-performance signal.
- `search_funds(query)` — fuzzy match against name + manager.

## Citation guidance

Every `pension_fund_snapshots` row carries a `source_url` (the MoF
`DafMakdim.aspx` page). Agents quoting fund-level performance MUST
cite that URL. For policy / rule claims (contribution ceilings,
withdrawal rules) cite the relevant `domain_knowledge/tax/israel/...`
file instead — gemelnet is *performance data*, not tax law.

## Failure mode

The MoF site is occasionally unreachable (DNS hiccups, evening
maintenance windows). The adapter raises `MissingDataSourceError` in
that case; callers (CLI, annual loop) catch it and degrade to "no
update this run" rather than crashing the parent process.

## Hebrew text handling

The wire encoding is **Windows-1255**. Argosy decodes once at the
adapter boundary; everything downstream (DB, API, agent prompts) uses
UTF-8. Round-tripping Hebrew through SQLite with `aiosqlite` is
exercised in the round-trip tests.

## How agents should use this file

- **TaxAnalystAgent**: when discussing the user's pension/keren
  hishtalmut/kupat gemel performance, query
  `argosy.state.queries.get_user_pension_snapshots(user_id)` and cite
  the `source_url` plus this file.
- **AdvisorAgent**: when surfacing an under-performance gap, cite
  this file alongside the relevant tax-domain file (e.g.
  `domain_knowledge/tax/israel/retirement/keren_hishtalmut.md`).
- **GapTrackerAgent**: a `relative_to_benchmark_pct < -1.0` over a 12m
  window is a candidate gap — surface as "fund X under-performing the
  sector average by N%; consider switching."

## User-facing helpers

When users don't know their `fund_id`, point them to:

- **Wobi** (https://www.wobi.co.il/) — comparison portal with fund
  metadata.
- **Swiftness** (https://swiftness.co.il/) — pension/finance review
  service that surfaces the user's holdings.
- **nofryers.com** — Hebrew-language consumer guides.
- The pension-clearing-house (`har ha-bituach`,
  https://har-habituah.gov.il/) — single-portal view of every pension
  account the user holds, identified by `fund_id`.

These are *user-facing pointers* only. Argosy never scrapes private
account data on the user's behalf without explicit credentials and
auditable consent.
