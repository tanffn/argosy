---
title: US-Israel Income Tax Treaty — Investor-Relevant Articles — 2026
topic: us_israel_income_tax_treaty
jurisdiction: israel
applies_to_year: 2026
applies_to_user: israeli_resident_individual_with_us_source_income
last_verified: 2026-06-02
verified_by: argosy-domain-refresh-agent (claude-opus-4-7) + web-search verification
next_refresh_due: 2027-01-31
source_urls:
  - https://www.irs.gov/pub/irs-trty/israel.pdf
  - https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents
  - https://taxsummaries.pwc.com/israel/corporate/withholding-taxes
  - https://www.irs.gov/instructions/iw8ben
  - https://www.nbn.org.il/life-in-israel/finances/taxes/us-tax-compliance/
sources:
  - url: https://www.irs.gov/pub/irs-trty/israel.pdf
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents
    retrieved: 2026-06-02
    tier: 1
  - url: https://taxsummaries.pwc.com/israel/corporate/withholding-taxes
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.irs.gov/instructions/iw8ben
    retrieved: 2026-06-02
    tier: 1
  - url: https://www.nbn.org.il/life-in-israel/finances/taxes/us-tax-compliance/
    retrieved: 2026-06-02
    tier: 2
---

# US-Israel Income Tax Treaty (1975; in force 1995) — Investor-Relevant Articles

## Summary

The Convention between the United States and Israel for the Avoidance of Double Taxation governs cross-border income flows between the two jurisdictions. For an Israeli-resident portfolio investor with a US broker (e.g., Schwab), the **practically binding provisions** are:

1. **Article 12 — Dividends:** US withholding tax on US-source portfolio dividends reduced from the 30% statutory NRA rate to **15%**.
2. **Article 14 — Interest:** US withholding on most interest reduced; most US-Treasury/bank-deposit interest is already exempt under US domestic portfolio-interest rules.
3. **Article 15 — Capital Gains:** Capital gains on US securities sold by an Israeli-resident NRA are taxed by **Israel only**, not the US (NRA capital-gain exemption holds; real estate is a separate FIRPTA story).
4. **Form W-8BEN at Schwab:** mandatory to claim the 15% rate; valid **3 calendar years** plus the year of signing. Lapse defaults to 30% NRA WHT.
5. **The treaty does NOT cover estate tax.** US-situs assets held by an Israeli decedent face US estate tax with only the **$60,000** NRA exemption — see "Estate tax" section below.

## Rates / brackets / amounts (2026)

### Treaty WHT rates on US-source payments to Israeli residents

| Income type | Treaty Article | US WHT under treaty | Statutory NRA rate (without treaty) | Practical effect |
|---|---|---|---|---|
| Portfolio dividends — individual | Article 12(2)(b) | **15%** | 30% | W-8BEN at Schwab required |
| Dividends — 10%-corporate-shareholder | Article 12(2)(a) | 12.5% | 30% | Not applicable (individual investor) |
| Interest — general | Article 14 | 17.5% | 30% | Most US-source interest is portfolio-interest-exempt under US domestic law (IRC §871(h)), so effective WHT often 0% |
| Capital gains — portfolio securities | Article 15 (in conjunction with US NRA rule) | **0% US** | 0% US for NRA portfolio CG | Israel-only taxation (`section_102.md`, `capital_gains.md`) |
| Real estate (FIRPTA) | Article 15 / US FIRPTA | up to 15% withholding on gross | up to 21% statutory + FIRPTA | Out of scope; US real estate not in the user's portfolio |
| Pensions / 401(k) / IRA | Article 19 | varies | varies | Out of scope for current portfolio |

Sources: IRS Israel treaty text (1975 convention); IRS Israel Tax Treaty Documents landing page; PwC Israel Corporate WHT page; IRS Form W-8BEN instructions; Nefesh B'Nefesh US-Israel Income Tax Update.

## Application notes

### Form W-8BEN — the procedural choke point

- The Schwab account custodian withholds **30% by default** on US-source dividends paid to a non-US address. To claim the **15% treaty rate**, the user must keep a current **W-8BEN** on file.
- **Validity period:** the form is valid through the **end of the third calendar year after signing**. Signed in 2024 → valid through 31-Dec-2027.
- **Recovery from lapse:** If the form lapses and 30% is withheld, recovery requires either (a) filing a refresh form going-forward and accepting the prior over-withholding as sunk, or (b) filing **Form 1040-NR** with the IRS for refund of the 15% over-withheld portion (slow, paper-heavy).
- **Operational rule for Argosy:** the intake/onboarding agent records the user's most recent W-8BEN signature date; the annual January refresh cycle triggers a renewal prompt if the form expires within 12 months.

### Israeli-side mechanics — Foreign Tax Credit reconciliation

When 15% has been withheld at Schwab on a US-source dividend:

1. The Israeli resident reports the gross dividend (in NIS) on the annual `דוח שנתי` (`tofes 1301`).
2. The Israeli statutory dividend rate is **25%** for individuals (`capital_gains.md`).
3. The 15% US WHT is **creditable** against the 25% Israeli tax → effective combined ≈ 25%, not 40%.
4. The Israeli resident pays the **10% gap** in shekels through the Israeli return.
5. The surtax overlay (3% general + 2% capital-source above ₪721,560 — `surtax.md`) is paid on top, with **no US credit available** because the surtax sits above the 25% statutory base.

**Without a valid W-8BEN:** 30% is withheld at source. The full 30% remains creditable against the 25% Israeli liability, but the 5-percentage-point excess credit is **non-refundable** to a typical individual investor on the Israeli return. This is real money lost — about ₪50 per ₪1,000 of gross dividend.

### Capital gains on US securities — Israel-only

- Per Article 15 + US domestic NRA capital-gain rule, an Israeli resident selling US-traded NVDA at Schwab pays **0% US tax** on the gain.
- The gain is fully taxable in Israel at the 25% statutory CGT (`capital_gains.md`), plus the surtax stack (`surtax.md`), and — if the shares are Section 102 RSU-origin — also the Section 102 ordinary/capital split (`section_102.md`).

## Estate tax — outside the income treaty, urgent for HNW portfolio

- **There is no US-Israel estate tax treaty.**
- US-situs assets held by an Israeli decedent (which **includes US-listed shares like NVDA, US-domiciled ETFs, US bank deposits beyond a small exemption**) are exposed to US estate tax.
- The non-resident-alien (NRA) US estate-tax exemption is **$60,000** of US-situs gross estate. Above that, estate tax applies on a progressive schedule up to 40%.
- For a US-situs portfolio of $2.3M+ (the user's NVDA position alone), naive holding through death exposes ~$2.24M to a graduated estate-tax schedule topping out at 40% — a potential ~$700k+ liability.
- This is the dominant reason Plan v2.0 favors **UCITS (Ireland-domiciled) ETFs** (e.g., CSPX) over US-domiciled ETFs (e.g., VOO) for non-NVDA US-equity exposure: Ireland-domiciled funds are **non-US-situs** and avoid US estate-tax exposure entirely.
- A standalone file at `domain_knowledge/tax/us/estate_tax_nonresidents.md` should detail the brackets and mitigation paths (Section 102 RSUs unfortunately cannot be moved to UCITS without realization). If that file does not yet exist, this is a tracked gap.

## Stack with related Israeli files

| Scenario | Treaty layer | Israeli layer (`capital_gains.md` / `section_102.md` / `surtax.md`) | Net effective |
|---|---|---|---|
| Schwab US-dividend, post-W-8BEN | 15% US WHT | 25% Israeli (credit 15%) + 3%/2% surtax | ~30% in surtax zone |
| Schwab US-dividend, W-8BEN lapsed | 30% US WHT | 25% Israeli (credit capped) + 3%/2% surtax | Loses 5pp to non-refundable excess credit |
| Schwab NVDA sale (Section 102 Capital) | 0% US | 25% Israeli + 3%/2% surtax | ~30% in surtax zone |
| Schwab NVDA sale (102 holding broken) | 0% US | up to 50% (ordinary income reclassification) + NI/health up-to-ceiling | up to ~50% |
| Death holding NVDA at Schwab | NRA estate-tax exposure on US-situs | 0% Israeli estate tax (Israel has no estate tax) | up to ~40% US estate tax on amount above $60k |

## Sources

- [IRS — Israel 1975 Income Tax Convention text (PDF)](https://www.irs.gov/pub/irs-trty/israel.pdf) — accessed 2026-06-02
- [IRS — Israel Tax Treaty Documents landing page](https://www.irs.gov/businesses/international-businesses/israel-tax-treaty-documents) — accessed 2026-06-02
- [PwC — Israel Corporate — Withholding taxes](https://taxsummaries.pwc.com/israel/corporate/withholding-taxes) — accessed 2026-06-02
- [IRS — Instructions for Form W-8BEN](https://www.irs.gov/instructions/iw8ben) — accessed 2026-06-02
- [Nefesh B'Nefesh — U.S.-Israel Income Tax Update 2026](https://www.nbn.org.il/life-in-israel/finances/taxes/us-tax-compliance/) — accessed 2026-06-02

## Refresh cadence

- **Bi-annual on treaty articles** — the 1975 convention is stable; no recent renegotiation.
- **Annual on procedural items** — re-verify the W-8BEN 3-year validity rule and the 15%/30% statutory rates.
- **Trigger** — any IRS Treaty Update circular or any change to Israeli FTC computation rules.

## Open issues

- A **PENDING** standalone `domain_knowledge/tax/us/estate_tax_nonresidents.md` file is required to back the estate-tax warning above with NRA bracket detail. If that file does not yet exist, the equity_comp_analyst and concentration_analyst agents should flag the estate-tax exposure verbally until the file is added.
- The 1975 treaty does **not** address Section 102 RSU mechanics (purely domestic Israeli law — `section_102.md`).
- Pensions / 401(k) / IRA treatment under Article 19 is out of scope for the current portfolio; will need its own file if the user accumulates US retirement accounts.
- Possible future renegotiation: there have been periodic discussions of updating the 1975 treaty; track via IRS Treaty Update notices.
