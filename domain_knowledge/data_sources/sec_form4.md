---
topic: sec_form_4_insider_transactions
jurisdiction: us
last_verified: 2026-09-13
next_refresh_due: 2027-05-02
code_references:
  - argosy/adapters/data/sec_form4_adapter.py
  - argosy/adapters/data/sec_13f_adapter.py
  - argosy/adapters/data/sec_rate_limit.py
catalog_sources:
  - id: 123
    as_of: 2026-03-18
    label: "Exact ownership4Document.xsd.xml bytes from official SEC ownershipxmltechspec-v5-5.zip; original archive catalog122, not executed XML"
  - id: 124
    as_of: 2026-03-18
    label: "Exact ownershipDocumentCommon.xsd.xml bytes from same official SEC v5.5 archive, raw original archive catalog122"
sources:
  - url: https://www.ecfr.gov/api/versioner/v1/titles.json
    tier: 1
    label: "Official eCFR publication status: title17 up_to_date_as_of2026-09-10 observed Sept13; do not invent a later published version"
  - url: https://www.ecfr.gov/api/versioner/v1/versions/title-17.json?part=240&section=240.16a-3
    tier: 1
    label: "Official section-specific version/amendment history, including post-April2025 changes if any"
  - url: https://www.ecfr.gov/api/versioner/v1/full/2026-09-10/title-17.xml?part=240&section=240.16a-3
    tier: 1
    label: "Official section XML at the latest published coverage date observed Sept13, not a claim of unpublished later law"
  - url: https://www.sec.gov/submit-filings/technical-specifications
    tier: 1
    label: "SEC current index identifies Ownership v5.5 implemented March18,2026; do not substitute the old v5.1 landing page"
  - url: https://www.sec.gov/about/developer-resources
    tier: 1
    label: "SEC Fair Access primary guidance, including ten requests per second"
  - url: https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f
    tier: 1
    label: "SEC Form13F filing timing"
  - url: https://www.govinfo.gov/content/pkg/CFR-2025-title17-vol4/pdf/CFR-2025-title17-vol4-sec240-16a-3.pdf
    tier: 1
    label: "Official 2025 CFR edition, section 240.16a-3(g): filing deadline and deemed execution; check subsequent amendments before current reliance"
  - url: https://www.sec.gov/files/33-11138-fact-sheet.pdf
    tier: 1
    retrieved: 2026-09-13
    label: "SEC final-rule fact sheet: Form 4/5 checkbox, gifts and compliance dates"
  - url: https://www.sec.gov/files/form4.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.sec.gov/edgar/searchedgar/ownershipformcodes.html
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.investor.gov/introduction-investing/investing-basics/glossary/forms-3-4-and-5
    retrieved: 2026-09-12
    tier: 1
---

# SEC Form 4 — insider transactions

Form 4 is the per-transaction disclosure that "insiders" — corporate
officers, directors, and beneficial owners of more than 10% of a class
of registered equity — generally must file with the SEC within **two business
days** of a reportable transaction in their company's securities. Applicable
exceptions and deemed-execution-date rules require the Form 4 instructions.

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
- **D** — disposition to the issuer pursuant to Rule 16b-3(e); not a generic
  open-market sale code.
- **X** — exercise of an in-the-money or at-the-money derivative.
- **O** — exercise of an out-of-the-money derivative.
- **C** — conversion of a derivative security.
- **W** — acquisition or disposition by will or laws of descent/distribution.

## Practical interpretation rules

1. **Research hypothesis:** investigate clustered purchases, but no calibrated
   advantage over a lone purchase is established in this file.
2. **Discount A/M/F.** Grant-driven activity is mechanical
   compensation — not a sentiment signal.
3. **Check 10b5-1 evidence:** inspect the structured checkbox/adoption date
   and transaction-linked footnotes. The adapter parses `aff10b5One`; a
   document-level indicator does not automatically classify every transaction.
4. **Mind the role.** A 10%-owner's trade has different implications
   than an independent director's. Officers (esp. CEO/CFO) carry the
   most informational weight.
5. **Two-day filing rule.** Form 4 is much fresher than 13F (which
   has a 45-day lag). For tactical signal, Form 4 wins.

## Cluster heuristics

- **Uncalibrated candidate features:** insider count, purchase window,
  size relative to holdings/float, and post-earnings timing. The former
  3-insider / 30-day / 0.5%-float cutoffs and 60–90-day “tradable” claim
  had no supporting study here. Evaluate dated outcomes before assigning
  predictive strength; do not call them proven academic findings.
- **Lone-sell** — typically not a signal absent corroborating data.
  Insiders sell for many non-information reasons (tax, divorce,
  diversification).

## Adapter usage

`argosy.adapters.data.sec_form4_adapter.SecForm4Adapter`:

- `get_recent_form4_for_ticker(ticker, days=30)` — issuer-side view
- `get_recent_form4_for_filer(cik, days=90)` — insider-side view
- 24h cache TTL. Successful daily collection reduces lag; it does not guarantee
  complete coverage of late filings, corrections, outages or retrieval limits.

## SEC API etiquette

Same as 13F: configured real contact identity (`ARGOSY_SEC_CONTACT_EMAIL`),
not the placeholder `admin@argosy.local`; observe the SEC's 10 requests/second
limit and the adapter's shared throttle. Configuration alone does not prove access.
