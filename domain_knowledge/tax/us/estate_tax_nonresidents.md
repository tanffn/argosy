---
topic: us_estate_tax_nonresidents
jurisdiction: us
last_verified: 2026-08-15
next_refresh_due: 2026-12-31
sources:
  - url: https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns
    retrieved: 2026-08-15
    tier: 1
  - url: https://www.irs.gov/forms-pubs/about-form-706-na
    retrieved: 2026-08-15
    tier: 1
  - url: https://www.irs.gov/pub/irs-pdf/i706na.pdf
    retrieved: 2026-08-15
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

## Sleeve carve-out for the x10 moonshot sleeve (Ariel's ruling, 2026-08-21)

**This is a scoped exception to the flat rule above, not a weakening of it.** Ariel,
2026-08-21: *"For moonshot it's ok to buy US-situs. If we can find moonshot in
israel / eu / other that's also great, but might be harder to find. We need
moonshot!"* — given directly in response to the deploy-cash flow blocking every
candidate in the x10 / high-growth ("moonshot") sleeve except NVDA and falling back
to a single distressed Israeli micro-cap (INVZ) as the sleeve's only buyable name.

- **Scope:** ONLY the permanent high-growth / "moonshot" sleeve (the x10-asymmetry
  sleeve; see `argosy/services/high_potential_sleeve.py::X10_SLEEVE_MANDATE`,
  `sigma_class == "high_growth_basket"`). CORE and growth sleeves are NOT covered —
  a US-domiciled ETF/stock bought as a core position is still subject to the flat
  rule above and must be UCITS where an equivalent exists.
- **Conditions that must hold for a moonshot US-situs buy to be acceptable:**
  1. The position is genuinely attributed to the moonshot sleeve (not merely
     labeled so) — attribution is sleeve-membership by ticker against the plan's
     own moonshot instrument list, not a free-text claim.
  2. The estate-tax consequence is stated explicitly in the buy's justification —
     "this is a US-situs single name inside the moonshot sleeve; it adds to the
     $60K-exemption / up-to-40%-marginal-rate NRA estate-tax base described in this
     file" (or equivalent) — a buy that is silent on the tradeoff is not disclosed
     and must be revised.
  3. The total US-situs dollars added to the sleeve by one proposal stays within a
     bounded cap derived from the sleeve's own target allocation (see
     `argosy/services/allocation_author/verifier.py` — the cap is the sleeve's
     single-name carve-out share, ~40% by construction of
     `high_potential_sleeve.py`'s seed design, of the sleeve's target-pct-of-book
     dollar size). It is not open-ended.
  4. Preference for non-US-situs moonshot names (Israel / EU / elsewhere) continues
     to apply where a comparably asymmetric candidate exists — the carve-out
     permits US-situs, it does not prefer it.
- **NVDA remains the separately-sanctioned name** it already was — unaffected by
  this carve-out and not counted against the moonshot cap above.
- **Everything else in this file is unchanged.** Core-sleeve UCITS preference,
  the $60K exemption, the 40% top marginal rate, and the mitigation strategies all
  continue to govern every position outside the moonshot sleeve.

## Form 706-NA filing

Heirs must file Form 706-NA within 9 months of death if US-situs assets exceed $60K. Penalties for late filing are severe. Reducing US-situs to under $60K eliminates the filing burden entirely.

## How agents should use this file

- **Cite this file** for any reasoning about why UCITS is preferred over US-domiciled ETFs for new buys.
- For the income-tax angle on the same UCITS-vs-US-ETF question, also cite `nonresident_withholding.md`.
- The plan-critique agent should flag any plan item that *adds* US-situs exposure beyond the existing NVDA position as a YELLOW or RED finding, citing this file — EXCEPT bounded moonshot-sleeve buys that satisfy the carve-out conditions above, which are expected and should not be flagged solely for being US-situs (still flag if a condition is unmet).
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual** — verify the $60K exemption is still $60K and the 40% top rate is unchanged.
- **On legislation** — any US tax-bill movement on estate exemptions triggers refresh.
