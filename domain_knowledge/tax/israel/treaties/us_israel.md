---
topic: us_israel_income_tax_treaty
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2027-12-31
sources:
  - url: https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents
    retrieved: 1900-01-01
    tier: 1
  - url: https://home.treasury.gov/policy-issues/tax-policy/international-tax/tax-treaties
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
---

# US-Israel Income Tax Treaty (1975, in force 1995)

The convention between the United States and Israel for the avoidance of double taxation governs how investment income paid by US sources to an Israeli resident is taxed in each country.

> **Verification status:** `last_verified: 1900-01-01`. Treaty articles cited below are stable (treaty in force since 1995, no recent renegotiation), but withholding rates and procedural forms (W-8BEN cycle) should be re-verified annually.

## Treaty articles relevant to a portfolio investor

| Article | Topic | Practical effect for an Israeli resident at Schwab |
|---|---|---|
| **Article 12 — Dividends** | Reduced withholding | **15% US WHT** on US-corporation portfolio dividends (vs the default 30% statutory rate). 12.5% if the recipient is an Israeli company holding ≥ 10% — not relevant here. |
| **Article 14 — Interest** | Source-based withholding | Generally 17.5% US WHT, but most US Treasury and bank-deposit interest is **portfolio-interest exempt** under US domestic law. |
| **Article 15 — Capital Gains** | Residence taxation | **Israel taxes** capital gains on US securities for an Israeli resident; US generally does **not** tax (nonresident-alien gains on portfolio securities are 0% US WHT). Real estate is an exception (`tax/us/nonresident_withholding.md` covers FIRPTA). |
| **Article 26 — Limitation on Benefits** | Anti-treaty-shopping | The Israeli individual resident automatically qualifies; nothing operational here. |

## Procedural requirement: Form W-8BEN

To claim the 15% (rather than 30%) treaty rate at Schwab, the user must keep a current **W-8BEN** on file. The form is valid through the **end of the third calendar year** following signing — e.g., signed 2024 → valid through 31-Dec-2027.

- The intake agent must record the W-8BEN signature date in `user_context`.
- The annual January cadence triggers a refresh prompt if expiry is within 12 months.
- If the form lapses, Schwab will switch to the 30% default rate without warning. Recovery requires either a refile + going-forward correction or filing 1040-NR for refund (slow).

## Foreign Tax Credit on the Israeli return

When 15% has been withheld in the US:

- The Israeli resident reports the gross dividend on `דוח שנתי` (tofes 1301).
- Israeli statutory dividend tax is 25% (`capital_gains.md`).
- The 15% US WHT is creditable against the 25% Israeli tax → effective combined ≈ 25%, not 40%.
- The Israeli resident pays the *gap* (10% of gross dividend) in shekels.
- This is **only** true with a valid W-8BEN. Without it, 30% is withheld, and the full 30% is creditable but the Israeli liability is still 25%, leaving a 5-percentage-point excess credit that is **not refundable** under the treaty for a typical individual portfolio investor.

## Estate tax — note

The income tax treaty does **not** cover estate tax. There is **no** US-Israel estate tax treaty. US-situs assets held by an Israeli decedent are exposed to US estate tax with only the $60,000 exemption available to non-residents — see `tax/us/estate_tax_nonresidents.md`. This is the reason Plan v2.0 favors **UCITS** (Ireland-domiciled) ETFs over US-domiciled ETFs for non-NVDA US exposure.

## How agents should use this file

- **Cite this file** for any claim about US WHT on US-source dividends, the 15% treaty rate, or the W-8BEN process.
- Pair with `capital_gains.md` for the Israeli-side rate stack.
- Pair with `tax/us/estate_tax_nonresidents.md` for the estate-tax angle (which the income treaty does **not** address).
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Bi-annual** — treaty articles are stable, but interpretations can shift.
- **Annual** — re-verify the W-8BEN procedural requirement and the 15% rate.

## Open issues

- The treaty does not address Israeli `Section 102` RSU mechanics; that is a domestic Israeli rule and lives in `retirement/section_102.md`.
- 401(k) / IRA withdrawals by Israeli residents are not covered above; they have separate treatment (treaty Article 19 governs pensions). Out of scope for the user's current portfolio.
