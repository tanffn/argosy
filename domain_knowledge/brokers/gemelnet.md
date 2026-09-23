---
topic: gemelnet_mof_pension_data
jurisdiction: israel
last_verified: 2026-09-23
next_refresh_due: 2027-05-02
code_references:
  - argosy/adapters/data/gemelnet_adapter.py
  - argosy/adapters/data/gemelnet_ckan.py
  - argosy/orchestrator/loops/annual.py
  - argosy/orchestrator/scheduler.py
  - argosy/state/queries.py
  - argosy/adapters/data/cache.py
sources:
  - url: https://data.gov.il/api/3/action/datastore_search?resource_id=a30dcbea-a1d2-482c-ae29-8f781f5025fb&filters=%7B%22FUND_ID%22%3A117%7D&sort=REPORT_PERIOD%20desc&limit=12
    tier: 1
    label: "Official monthly observations for a real public fund used as a technical query example, not synthetic test data or a claim this is the user's fund. Verify months/yields before deriving 12m performance."
  - url: https://gemelnet.cma.gov.il/views/dafMakdim_Link1.aspx
    tier: 1
    label: "Portal coverage page requested by the current review; access and contents must be verified"
  - url: https://gemelnet.cma.gov.il/views/dafMakdim_Link5.aspx
    tier: 1
    label: "Additional portal coverage page requested by the current review; not assumed verified"
  - url: https://gemelnet.cma.gov.il/views/dafMakdim_Link3.aspx
    retrieved: 2026-09-23
    tier: 1
    label: "Primary methodology: monthly reports, nominal gross returns, comparison with market indices"
  - url: https://www.gov.il/BlobFolder/reports/pension_fund/he/pension_found_2025.pdf
    tier: 1
    label: "Civil Service Commission guide: separate official provident and pension comparison portals"
  - url: https://gemelnet.cma.gov.il/views/dafmakdim.aspx
    retrieved: 2026-09-23
    tier: 1
  - url: https://data.gov.il/api/3/action/package_show?id=gemelnet
    retrieved: 2026-09-23
    tier: 1
    label: "Live CKAN package metadata, success=true; regulator publisher and resource list"
  - url: https://data.gov.il/api/3/action/resource_show?id=a30dcbea-a1d2-482c-ae29-8f781f5025fb
    retrieved: 2026-09-23
    tier: 1
---

# Gemel Net — Capital Market Authority public provident-fund performance

`gemelnet.cma.gov.il` is the Capital Market, Insurance & Savings Authority's
public **provident-fund / hishtalmut** comparison portal. Pension-fund comparisons
use the separate Pensia Net system; do not claim Gemel Net covers every pension
vehicle. A public Gemel Net dataset also exists on data.gov.il. Provider availability
does not prove Argosy's existing adapter successfully parses the current site.

> Where this fits: the user's *private* account-level data (current
> balances, vesting, contributions) is held by the manager (Migdal,
> Harel, Altshuler Shaham, ...) — Argosy does not scrape that. We
> only consume the *fund-level public performance* data via gemelnet.
> Use the manager's statement or the authorized pension clearing house to
> identify accounts. A confirmed fund_id enables fund-level performance lookup,
> not automatic access to the private account.

## What it provides

- The authority's methodology describes monthly fund reports and **nominal gross
  returns before management fees**, with comparisons to selected market indices.
  It does not establish a universal publication-lag guarantee or prove this
  adapter's expected sector-average column exists on the current page.
- The legacy adapter extracts a 12m field, fund/manager labels and an expected
  benchmark field. On legacy transport/grid failure, the adapter now falls back
  to the official CMA CKAN resource `a30dcbea-a1d2-482c-ae29-8f781f5025fb`.
  The fallback compounds twelve consecutive published `MONTHLY_YIELD` values;
  it is a derived nominal gross return, not a published 12m field or a net-of-fee
  personal return. It preserves all twelve inputs and their periods, source URL
  and access timestamp. Missing months, duplicates, invalid yields and data more
  than three report months old are unavailable, not zero. Benchmark and relative
  return stay null because this resource does not supply a matched benchmark.
  Its 36m/60m/YTD requests explicitly fail; snapshot storage is
  12m-only. These are implementation facts, not current-feed verification.
- Treat the adapter's pension-type mapping as a parser label only. Pension-fund
  comparisons use Pensia Net, not a promise of pension coverage in Gemel Net.

## Refresh cadence

- Upstream reports are monthly. The former 6–8-week publication-lag claim was
  unsupported; check the actual report period and publication date before use.
- Argosy adapter cache: 24h TTL (`kv_cache` rows keyed
  `gemelnet:fund_returns:<id>:<period>:v2`). Daily refresh is more than
  enough given monthly upstream cadence.
- Annual loop: has an optional injected `pension_refresh_callable(user_id)`.
  The loop itself does not enumerate `identity.pensions`; that behavior would
  have to be implemented by the callable. The supplied scheduler constructs
  `AnnualLoop` without this callable, so that construction does not run pension
  refresh. Do not promise per-fund automatic updates based on this hook alone.

## Adapter API surface

`argosy.adapters.data.gemelnet_adapter.GemelnetAdapter`:

- `list_funds(*, fund_type=None)` — full universe; optionally filtered
  to one canonical type.
- `get_fund_returns(fund_id, *, period="12m")` — `{return_pct,
  benchmark_return_pct, relative_to_benchmark_pct, last_updated,
  source_url, ...}`. `relative_to_benchmark_pct` is nullable and only meaningful
  with a sourced, matched benchmark; null is not an under-performance signal.
- `search_funds(query)` — fuzzy match against name + manager.

## Citation guidance

Every `pension_fund_snapshots` row carries a `source_url`; legacy rows may name
the MoF `DafMakdim.aspx` page, while fallback results name the actual CKAN query.
Agents quoting fund-level performance MUST cite the stored source URL and
the underlying report period, not treat fetch time as the return's period.
New snapshot writes append `pension.snapshot.source` audit evidence containing
the original return payload, including period/methodology/monthly observations.
`get_user_pension_snapshots` returns that user-scoped evidence separately from
`snapshot_at`; legacy rows without it have null source evidence, not a newly
verified period.
Historical provenance is not proof that the legacy endpoint works. For policy / rule claims (contribution ceilings,
withdrawal rules) cite the relevant `domain_knowledge/tax/israel/...`
file instead — gemelnet is *performance data*, not tax law.

## Failure mode

The older HTTP `gemelnet.mof.gov.il/Tsuot/UI` endpoint can fail DNS resolution;
the adapter records that failure before attempting the official CKAN fallback.
Public data availability does not prove any private account has been matched,
or that an automatic pension-refresh job is wired. Verify the actual caller
and its receipt before making either claim.

If both paths fail, the adapter raises `MissingDataSourceError`. A caller must
surface that failure rather than represent it as a successful empty refresh.
This document does not certify every CLI caller's error handling or imply that
the currently unwired annual pension refresh has run.

## Hebrew text handling

The legacy adapter has a Windows-1255 decoding path; that is not a claim about
every current CMA/API response. Downstream text uses Unicode. Verify the actual
response encoding when repairing the connector.

## How agents should use this file

- **TaxAnalystAgent**: when discussing the user's pension/keren
  hishtalmut/kupat gemel performance, query
  `argosy.state.queries.get_user_pension_snapshots(user_id)` and cite
  the `source_url` plus this file.
- **AdvisorAgent**: when surfacing an under-performance gap, cite
  this file alongside the relevant tax-domain file (e.g.
  `domain_knowledge/tax/israel/retirement/keren_hishtalmut.md`).
- **GapTrackerAgent**: compare only when a sourced, period-matched benchmark
  exists. The CKAN fallback has no benchmark, so its null relative return is
  not evidence of either underperformance or outperformance. Broad parser
  categories are not the user's private product/tax classification.

## User-facing helpers

When users don't know their `fund_id`, point them to:

- **Swiftness** (https://swiftness.co.il/) — access to the official pension
  clearing house (Masleka Pensionit), identified in the cited Civil Service
  Commission guide. Access requires the relevant identity/authorization process
  and may carry a fee; it is not a generic commercial portfolio-review tool.
- Do not confuse **Har HaBituach** (insurance-policy information) with the
  **pension clearing house** (Masleka Pensionit). Confirm the official service
  and the needed authorization before asking the user to submit private details.

These are *user-facing pointers* only. Argosy never scrapes private
account data on the user's behalf without explicit credentials and
auditable consent.
