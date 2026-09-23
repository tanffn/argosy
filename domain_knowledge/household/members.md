---
topic: Household members — residence, citizenship, and what they imply
jurisdiction: household
last_verified: 2026-09-13
next_refresh_due: 2027-08-23
profile_fields:
  - user_date_of_birth
  - spouse_date_of_birth
  - tax_residency
  - spouse_tax_residency
  - primary_residence_country
catalog_sources:
  - id: 119
    as_of: 2026-09-13
    label: "Assistant-recorded excerpt of Ariel's explicit declarations for both spouses; user declaration, not identity-document verification"
  - id: 115
    as_of: 2026-08-23
    label: "Original user conversation records; later same-day account clarification must not be omitted"
sources:
  - url: https://www.irs.gov/instructions/i706
    retrieved: 2026-09-13
    tier: 1
    label: "Schedule E instructions: noncitizen surviving spouse and contribution tracing; not proof of household ownership"
  - url: https://www.germany.info/us-en/service/09-taxes/taxes-estates-954266
    retrieved: 2026-09-13
    tier: 1
    label: "German Foreign Office treaty text, Articles 1 and 4; treaty domicile is not inferred from nationality alone"
  - url: file://Resources/Family Finances Status - 26 May.tsv
    as_of: 2026-05-29
    tier: 1
  - url: file://Resources/2026/Leumi/bank_charges_ID/ShowMailPDF (1).pdf
    as_of: 2026-07-29
    tier: 1
  - url: https://www.gesetze-im-internet.de/erbstg_1974/__2.html
    tier: 1
  - url: https://www.irs.gov/instructions/i706na
    retrieved: 2026-09-13
    tier: 1
  - url: https://www.irs.gov/businesses/small-businesses-self-employed/estate-gift-tax-treaties-international
    retrieved: 2026-09-13
    tier: 1
  - source: Ariel, directly, in conversation 2026-08-23
    retrieved: 2026-08-23
    tier: 1
  - source: Ariel, directly, in conversation 2026-09-13
    retrieved: 2026-09-13
    tier: 1
    label: "Explicit NO to US citizenship and green-card holding for Ariel; follow-up explicitly confirms the same for Noga"
---

# Household members

Attributed household inputs, with their original dates, not a current identity
or custodian verification. `last_verified` checks the supporting records and legal
analysis; it does not renew private facts or instructions. These drive estate-tax situs, treaty
access and filing obligations, and were previously recorded **nowhere** — the
estate reasoning in `../tax/us/estate_tax_nonresidents.md` ran without them and an
agent asked Ariel for Noga's status on 2026-08-23. Cite this file instead.

## Ariel

- **Israeli tax resident — recorded planning input**, not independently established
  by the recovered August-23 conversation or a current residency certificate.
- **Not a US citizen; not a US green-card holder — explicitly confirmed by
  Ariel on 2026-09-13.** This current user declaration supersedes the earlier
  unconfirmed attribution; do not ask him to repeat this answered question.
  Do not infer estate/gift-tax domicile solely from income-tax residency or
  immigration status. The nonresident-not-citizen estate model is conditional
  on the relevant domicile facts.
- Employed by NVIDIA; holds Section-102 trustee RSUs. See
  `../tax/israel/section_102.md`.

## Noga

- **Israeli tax resident — recorded planning input**, not inferred from citizenship
  and not independently reverified by the recovered August-23 conversation.
- **German citizen.**
- Ariel said on 2026-08-23 that Noga had **never been in Germany**. Preserve this
  dated statement; it is not a current residency certificate.
- **Not a US citizen; not a US green-card holder — explicitly reconfirmed by
  Ariel on 2026-09-13 for Noga**, consistent with his 2026-08-23 statement.
  Do not ask him to repeat this answered question. These two confirmed facts
  alone do not determine other US tax-residency or estate-domicile tests.

### What Noga's status implies

1. **No US-citizen spouse ⇒ no unlimited marital estate deduction.** That relief
   requires a US-citizen surviving spouse, or a QDOT. Israel has **no US
   estate/gift tax treaty**, so nothing restores it.
2. **The §2040(a) joint-account rule bites.** For a non-US-citizen survivor there
   is no automatic 50/50 split: the **entire** jointly-held US-situs asset is
   included in the first decedent's gross estate unless the survivor proves she
   furnished her own consideration. Assets originally gifted to her by the
   decedent do not count as proof.
3. **Joint registration does not itself forfeit a separate estate's credit.**
   Each decedent's estate is computed from the legally includible interests;
   contribution tracing determines joint-property inclusion. The ordinary
   nonresident-not-citizen credit is $13,000, economically sheltering $60,000
   in a simple case, not a $60,000 deduction or an automatic equal split.
4. **German citizenship is not automatically useless here.** A US–Germany estate
   and gift tax treaty exists, but treaty benefits generally follow **domicile**,
   not nationality; do not equate the stated Israeli tax residence with a
   concluded treaty-domicile analysis. Separately, German inheritance
   tax (ErbStG §2) has an extended-liability rule for German nationals who
   emigrated recently. The stated never-in-Germany history gives no evidence of
   a recent emigration from Germany; do not invent one. Other domicile, residence,
   German-situs property or special statutory connections still require their
   actual facts before treaty relief or German liability can be concluded.

## Accounts

**Do not treat every account as joint.** On 2026-08-23 Ariel first said the account
was joint, then clarified at 06:16 UTC: “Schwab is mine, RSU under my name (work)”.
The cited July Leumi statement names both spouses. These dated records support
distinguishing Leumi joint registration from Ariel's Schwab/RSU ownership; they
are not a fresh custodian check. Joint title alone does not establish each spouse's
includible share. Obtain ownership/contribution evidence before any retitling.

## Real estate

- **Israeli primary residence ("Keret")** — roughly ILS 3M, Ariel's dated
  **2026-08-23** gross-value estimate in catalog record 115 (original message
  04:57:23 UTC, source line 684). This supersedes the older May intake estimate
  of ILS 2.5M; neither figure is current valuation evidence or net equity.
- **Atlanta, USA — GONE, and not US-situs.** Ariel paid an advance to a developer
  that went out of business; the money was lost in full and **nothing is
  registered in his name**. There is no US real property and no delivered title,
  so it contributes **zero** to the US-situs estate base. Earlier records
  describing "$318K against a $219K mortgage" are the original contract terms,
  not a current holding. The zero property exposure is based on Ariel's
  explicit declaration above, not an independently verified loan-origination
  history. The earlier categorical claim that the mortgage was never drawn
  is withdrawn; do not infer an outstanding debt from the old contract either.
- **Romania — two units, Pipera and Obor** (EUR-denominated).

## Capital losses

**No available Israeli capital-loss carryforwards were reported** (Ariel,
2026-08-23). The user said a prior miscalculation had already been refunded;
this is not proof that a tax refund mechanically consumes a loss carryforward.
Do not budget a carryforward without a loss schedule or a newer supporting record.
