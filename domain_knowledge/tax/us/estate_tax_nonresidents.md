---
topic: us_estate_tax_nonresidents
jurisdiction: us
last_verified: 2026-09-13
next_refresh_due: 2026-12-31
catalog_sources:
  - id: 120
    as_of: 2026-06-15
    label: "Existing advisor handoff, last-updated date stated in original; historical record of decisions, not renewed consent"
  - id: 121
    as_of: "2026-02 version; exact day unspecified"
    label: "Original Plan v2.0; historical tranche and mitigation wording only, not the current approved plan"
  - id: 116
    as_of: 2026-08-21
    label: "Exact original human moonshot US-situs instruction, not renewed consent"
  - id: 115
    as_of: 2026-08-23
    label: "Historical user statements about Atlanta and account registration, not current ownership verification"
code_references:
  - argosy/services/high_potential_sleeve.py
  - argosy/services/allocation_author/verifier.py
  - argosy/services/allocation_author/packet_assembly.py
  - argosy/services/allocation_author/packet.py
sources:
  - url: file://Resources/Family Finances Status - 26 May.tsv
    as_of: 2026-05
    tier: 1
    label: "Historical user intake; value dates inside the TSV remain authoritative, not current portfolio evidence"
  - url: https://www.irs.gov/pub/irs-pdf/i706.pdf
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/instructions/i706na
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/businesses/small-businesses-self-employed/estate-gift-tax-treaties-international
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns
    retrieved: 2026-09-12
    tier: 1
  - url: https://www.irs.gov/forms-pubs/about-form-706-na
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/pub/irs-pdf/i706na.pdf
    retrieved: 2026-09-13
    tier: 1
---

# US Estate Tax for Non-Resident Aliens

A nonresident who is not a US citizen is generally subject to US federal estate tax on **US-situated** assets. The general **$13,000 unified tax credit** is often described as sheltering $60,000 in the simple no-prior-gifts case; it is **not a $60,000 deduction before applying tax brackets**. There is **no US-Israel estate/gift tax treaty** on the IRS treaty list. Estate-tax residence is based on domicile, not merely income-tax NRA status. Establish the applicable status before applying this regime.

> **Scope:** regulatory rules below are separate from dated portfolio context and the user's investment preferences. Recompute exposure from current records; value a death scenario at **FMV at death**, not purchase cost. The older verification stamp is historical until this corrected file passes re-verification.

## What counts as US-situs

Generally taxable for an NRA decedent:

- **Stock of US corporations**, regardless of where held (NVDA at Schwab → US-situs; even NVDA in an Israeli account is US-situs).
- **Real property** physically located in the US.
- **Tangible personal property** physically in the US (cash, jewelry, art at a US bank/safe).
- **Some debt instruments** issued by US persons (with portfolio-debt exceptions).

Generally **not** US-situs:

- **Qualifying bank deposits** under §2105(b), including the non-ECI condition. Brokerage cash balances and money-market fund shares are not automatically bank deposits.
- **Qualifying portfolio-debt instruments** under §2105(b)/§871(h), subject to the statutory conditions. A Treasury ETF is not the same legal asset as directly held qualifying Treasury debt.
- **Stock of foreign corporations**, even if listed in the US — and importantly, **shares of UCITS ETFs domiciled in Ireland** are foreign-corporation shares, hence **not** US-situs.
- Real estate located outside the US.
- Life insurance proceeds (statutory exclusion).

## Rates (NRA estate tax)

Use the Form 706-NA computation, including allowable deductions, prior gifts where applicable, tentative tax and applicable credits. In the simplified no-prior-gifts/no-other-adjustments case, compute graduated tax on the taxable estate, then subtract the available $13,000 credit (not below zero). The top marginal rate is 40% above $1,000,000 of the table's tax base.

| Graduated-tax base (before applying the credit) | Rate |
|---|---|
| First $10K | 18% |
| ... (graduated) | ... |
| Over $1,000,000 | **40%** |

Practically, anyone holding more than ~$1M of US-situs assets faces a 40% marginal rate on the next dollar.

Illustration only: with a $100,000 taxable estate, no prior gifts and the full $13,000 credit, tentative tax is $23,800 and tax after that credit is $10,800. This is not the result of applying the brackets to $40,000. Real liabilities also depend on deductions, worldwide-estate disclosure and other facts.

## The user's exposure

**Historical context, not a current valuation:** the following came from the May 2026 portfolio (`Family Finances Status - 26 May.tsv`). Annual public-source verification does not verify these private balances; refresh them from current portfolio/ownership records before using them:

- NVDA (Schwab): ~$2.296M of US-situs.
- US-domiciled ETFs (VOO, SCHD, SCHG, SGOV, etc.) at Schwab and at Leumi USD: high six figures of US-situs.
- UCITS ETFs at Leumi (CSPX, FWRA, CNDX, ACWD, etc.): **not** US-situs.
- The historical Atlanta entry is **superseded** by the user's 2026-08-23 account
  in `household/members.md`: failed developer advance and nothing registered in
  his name. The earlier claim that a mortgage was never drawn is unsupported and
  withdrawn; do not infer a current debt from old contract figures either. Do not
  count those figures as owned US real estate. Any surviving contractual claim
  would need its own legal classification.
- Israeli real estate, Romanian real estate: not US-situs.

That historical snapshot put US-situs exposure in the multi-million-dollar range.
It is not a current valuation or a current ~$1M tax calculation; derive today's
exposure and death-scenario liability from the current book and actual ownership.

## Mitigation strategies and historical planning decisions

1. **Migrate non-NVDA US exposure to UCITS** — buy CSPX instead of VOO, IWDP instead of REET, etc. This shrinks US-situs without changing economic exposure.
2. **Reduce NVDA over time** — the February-2026 Plan v2.0 proposed quarterly NVDA tranches and UCITS reinvestment. That historical proposal is not proof that sales, reinvestment or an automatic glide executed. Use the current approved plan, actual fills and current holdings for today's concentration and estate exposure; do not restore the old tranche amounts from this source.
3. **Use bank deposits and Treasuries** for the cash-equivalent layer rather than US stock — both statutorily exempt.
4. **Consider term life insurance** sized to the residual exposure — `LLM_Advisor_Handoff.md` §9.5 records the user's **May 2, 2026** decision to decline estate-bridge term life insurance. This is the dated planning record (catalog120), not a claim that the household owns no other life insurance.
5. **Israeli holding company** — the same May 2 planning record records a decline, citing wallet-company concerns and limitations for RSUs. Preserve the recorded choice; this historical rationale is not a legal opinion on every possible structure. The source names changed exposure, health and implementation circumstances as reasons to revisit, not as permission to reverse the choice automatically.

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
     graduated NRA estate-tax base, with the ordinary $13K credit and a top
     marginal rate of 40%, described in this
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
  the ordinary $13K credit (not a $60K deduction), the 40% top marginal rate, and the mitigation strategies all
  continue to govern every position outside the moonshot sleeve.

## Form 706-NA filing

The executor generally files within 9 months of death; extensions may apply. The $60,000 filing test includes death-date US-situated assets **plus applicable adjusted taxable gifts and the historical gift-tax specific exemption**, as described in Form 706-NA instructions. Holding assets below $60,000 alone does not prove no filing obligation. Filing threshold and tax payable are different questions.

## How agents should use this file

- **Cite this file** for any reasoning about why UCITS is preferred over US-domiciled ETFs for new buys.
- For the income-tax angle on the same UCITS-vs-US-ETF question, also cite `nonresident_withholding.md`.
- The plan-critique agent should flag any plan item that *adds* US-situs exposure beyond the existing NVDA position as a YELLOW or RED finding, citing this file — EXCEPT bounded moonshot-sleeve buys that satisfy the carve-out conditions above, which are expected and should not be flagged solely for being US-situs (still flag if a condition is unmet).
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual** — verify the ordinary $13K credit, separate $60K filing test and 40% top marginal rate.
- **On legislation** — any US tax-bill movement on estate exemptions triggers refresh.
