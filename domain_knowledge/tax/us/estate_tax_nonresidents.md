---
topic: us_estate_tax_nonresidents
jurisdiction: us
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.irs.gov/forms-pubs/about-form-706-na
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.irs.gov/pub/irs-pdf/i706na.pdf
    retrieved: 1900-01-01
    tier: 1
---

# US Estate Tax for Non-Resident Aliens

A non-resident alien (NRA) decedent is subject to US federal estate tax only on **US-situs** assets, and is allowed only a **$60,000** exemption (versus the ~$13.6M unified credit available to US citizens and residents). There is **no** US-Israel estate tax treaty. This is the central structural risk Plan v2.0 mitigates via UCITS ETFs.

> **Verification status:** `last_verified: 1900-01-01`. The $60K exemption is statutory and has not changed materially in decades; rates have been stable since 2013. Domain-refresh agent must verify the $60K figure annually because Congress can amend.

## What counts as US-situs

Generally taxable for an NRA decedent:

- **Stock of US corporations**, regardless of where held (NVDA at Schwab → US-situs; even NVDA in an Israeli account is US-situs).
- **Real property** physically located in the US.
- **Tangible personal property** physically in the US (cash, jewelry, art at a US bank/safe).
- **Some debt instruments** issued by US persons (with portfolio-debt exceptions).

Generally **not** US-situs:

- **Bank deposits** with US banks (statutory exclusion under IRC §2105(b)(1)).
- **Portfolio-debt instruments** (US Treasuries held under §871(h) qualify).
- **Stock of foreign corporations**, even if listed in the US — and importantly, **shares of UCITS ETFs domiciled in Ireland** are foreign-corporation shares, hence **not** US-situs.
- Real estate located outside the US.
- Life insurance proceeds (statutory exclusion).

## Rates (NRA estate tax)

The rate schedule for NRA estates above the $60K exemption is the same graduated schedule that applies to US-citizen estates above their exemption, ramping up to 40% on amounts above ~$1M.

| Taxable estate (NRA, above $60K) | Rate |
|---|---|
| First $10K | 18% |
| ... (graduated) | ... |
| Over $1,000,000 | **40%** |

Practically, anyone holding more than ~$1M of US-situs assets faces a 40% marginal rate on the next dollar.

## The user's exposure

From the May 2026 portfolio (`Family Finances Status - 26 May.tsv`):

- NVDA (Schwab): ~$2.296M of US-situs.
- US-domiciled ETFs (VOO, SCHD, SCHG, SGOV, etc.) at Schwab and at Leumi USD: high six figures of US-situs.
- UCITS ETFs at Leumi (CSPX, FWRA, CNDX, ACWD, etc.): **not** US-situs.
- Real estate in Atlanta: US-situs (mitigated by mortgage debt).
- Israeli real estate, Romanian real estate: not US-situs.

Total US-situs is in the multi-million-dollar range, which is why the plan describes a ~$1M estate-tax tail-risk.

## Mitigation strategies (per Plan v2.0 §9.5)

1. **Migrate non-NVDA US exposure to UCITS** — buy CSPX instead of VOO, IWDP instead of REET, etc. This shrinks US-situs without changing economic exposure.
2. **Reduce NVDA over time** — Plan v2.0's quarterly tranches systematically convert US-situs NVDA into non-US-situs UCITS via Israeli broker proceeds.
3. **Use bank deposits and Treasuries** for the cash-equivalent layer rather than US stock — both statutorily exempt.
4. **Consider term life insurance** sized to the residual exposure — user has explicitly declined this for now (per `LLM_Advisor_Handoff.md` §9.5).
5. **Israeli holding company** — also explicitly declined for now; risk of "wallet company" reclassification, plus cannot transfer NVDA RSUs cleanly.

## Form 706-NA filing

Heirs must file Form 706-NA within 9 months of death if US-situs assets exceed $60K. Penalties for late filing are severe. Reducing US-situs to under $60K eliminates the filing burden entirely.

## How agents should use this file

- **Cite this file** for any reasoning about why UCITS is preferred over US-domiciled ETFs for new buys.
- For the income-tax angle on the same UCITS-vs-US-ETF question, also cite `nonresident_withholding.md`.
- The plan-critique agent should flag any plan item that *adds* US-situs exposure beyond the existing NVDA position as a YELLOW or RED finding, citing this file.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual** — verify the $60K exemption is still $60K and the 40% top rate is unchanged.
- **On legislation** — any US tax-bill movement on estate exemptions triggers refresh.
