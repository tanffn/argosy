---
topic: israel_kupat_gemel
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.gov.il/he/departments/ministry_of_finance
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%A7%D7%95%D7%A4%D7%AA_%D7%92%D7%9E%D7%9C
    retrieved: 1900-01-01
    tier: 2
---

# Kupat Gemel (`קופת גמל`) and Tikun 190

`Kupat Gemel` is an Israeli long-term savings vehicle — a "provident fund" — that is part of the layered Israeli pension/savings system. The classic `kupat gemel` is illiquid until retirement age (currently 60+); however, a parallel mechanism called **Tikun 190** (`תיקון 190`) lets investors over 60 use a `kupat gemel l'haskhala` ("provident fund as a savings track") for significantly tax-advantaged investing without forcing annuitization.

> **Verification status:** `last_verified: 1900-01-01`. Domain-refresh agent must verify ceilings, eligibility ages, and the Tikun 190 rules annually.

## Two flavors

| Flavor | Liquidity | Best for |
|---|---|---|
| **Classic Kupat Gemel** (employer-track or self-funded) | Locked until retirement age (60+); withdrawal as annuity or one-time | Salary-deferred contribution within the wage-ceiling pension/savings basket |
| **Kupat Gemel l'Haskhala (Tikun 190)** | Withdrawable from age 60 (under amendment 190 conditions); penalty if withdrawn earlier | Lump-sum tax-efficient investment for 60+ residents who want flexibility, low CGT (15% nominal), and estate-planning benefits |

## Tikun 190 highlights

- Available to individuals **age 60 or older** (verify against current rule).
- Capital gains taxed at **15% nominal** rate at withdrawal — significantly lower than the standard 25% real-gain CGT (`capital_gains.md`) for typical post-2003 investments.
- No deduction for the contribution (it is post-tax money), but the wrapper materially reduces the on-going CGT load and offers good estate-planning treatment for heirs.
- Can be used to consolidate other retirement balances under more favorable rules.

## The user's situation

- Ariel reports 75,000 NIS in `provident fund` (Dec 2025 snapshot).
- Noga reports 75,000 NIS in `provident fund` (Dec 2025 snapshot).
- The user is currently **mid-40s**, well below the Tikun 190 age threshold of 60. So Tikun 190 is **not a current** lever; it becomes relevant in ~15 years and is worth flagging in the long-horizon roadmap.
- The classic `kupat gemel` employer-side is part of the full Israeli pension/savings basket and likely subsumed in the user's `Pension` and `Executive Insurance` figures.

## Annual contribution ceilings (2025 vintage; verify)

The pension/savings wrapper covering employer + employee contributions to pensions, executive insurance, and `kupat gemel` is jointly capped by the same 2 ceilings:
- "Income up to the ceiling" (~26,400 NIS/month — verify) for one set of deduction rules.
- "Income up to the broader ceiling" (~57,000 NIS/month — verify) for another.

These ceilings are unified across pensions and provident funds; over-contribution above the ceiling generally cannot enter the tax-favored wrapper.

## When this matters for the plan

- **Long-horizon (10–15 years from now):** When the user approaches age 60, evaluate moving a meaningful chunk of taxable Leumi USD assets into a Tikun 190 wrapper to lock in the 15% nominal rate on future gains.
- **Annual:** Verify that Noga's `kupat gemel` is being maximized as part of household tax planning.

## How agents should use this file

- **Cite this file** for any claim about classic `kupat gemel`, Tikun 190 mechanics, or the 60+ age threshold.
- Pair with `keren_hishtalmut.md` (the more immediately relevant tax-advantaged wrapper for this user).
- Pair with `capital_gains.md` for the comparison vs the 25% standard CGT.
- The intake agent should record provider names and balances for both spouses.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual (January)** — ceilings, age thresholds, and rates re-issued.
- **On reform** — Israel periodically reforms the pension/`gemel`/`hishtalmut` rules; any reform triggers a full refresh.

## Performance data

Per-fund 12-month / 36-month / 60-month nominal returns plus the
sector benchmark are available via the gemelnet adapter (see
`domain_knowledge/brokers/gemelnet.md`). When discussing a specific
user's `kupat gemel` performance, query
`argosy.state.queries.get_user_pension_snapshots(user_id)` and cite the
snapshot's `source_url` (`gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx`).
