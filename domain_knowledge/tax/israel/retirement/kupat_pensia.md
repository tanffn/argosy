---
topic: israel_kupat_pensia
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.gov.il/he/departments/topics/pension_funds
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.btl.gov.il/Insurance/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%A7%D7%A8%D7%9F_%D7%A4%D7%A0%D7%A1%D7%99%D7%94_%D7%9E%D7%A7%D7%99%D7%A4%D7%94
    retrieved: 1900-01-01
    tier: 2
---

# Kupat Pensia (`קרן פנסיה`)

A `kupat pensia` (formal Hebrew: `קרן פנסיה מקיפה חדשה` — "new comprehensive pension fund") is the **mandatory salary-deferred** Israeli pension vehicle. Every employed Israeli resident contributes by law from their second month of employment; the employer matches; and a slice of every paycheck routes to severance coverage. Withdrawal is locked until retirement age, and the standard payout is a lifetime annuity rather than a lump sum — which is what makes it different from `kupat gemel`.

> **Verification status:** `last_verified: 1900-01-01`. Domain-refresh agent must verify contribution rates, ceilings, retirement ages, and the default-fund (`קרן פנסיה ברירת מחדל`) regime annually.

## How it works

| Aspect | Rule |
|---|---|
| Mandatory? | Yes — `Pension Compulsory Order` (`צו ההרחבה לפנסיית חובה`) covers every employee. Self-employed have a parallel obligation (`חוק הפנסיה לעצמאים`). |
| Employee contribution | 6% of monthly salary (verify yearly) — pre-tax up to the wage ceiling |
| Employer contribution — pension | 6.5% of monthly salary (verify yearly) — to retirement savings |
| Employer contribution — severance | 6% of monthly salary (verify yearly) — counts toward statutory severance (`פיצויי פיטורין`) on termination |
| Salary ceiling for the pension wrapper | Indexed to the average wage; jointly capped with `kupat gemel` and executive insurance — see `kupat_gemel.md` |
| Liquidity | **Locked until retirement age** (currently 67 for men, 65→67 for women under the gradual reform — verify) |
| Withdrawal at retirement | **Lifetime annuity by default**; partial lump-sum permitted from the portion above the *recognized capital* (`קצבה מזכה`) threshold |
| Tax on monthly contributions | Employee contribution is pre-tax up to the ceiling; employer contributions are not imputed as income |
| Tax on annuity at retirement | The annuity is taxable income, but the recognized portion benefits from a 35–67% exemption depending on age and the user's `תקרת קצבה מזכה` allocation |
| Survivor benefits | **Built-in** — the fund pools longevity and disability risk; widow/widower and orphan annuities continue automatically per the fund rulebook |

## Default-fund regime (`קרן פנסיה ברירת מחדל`)

If an employee doesn't actively elect a pension fund, Israeli regulation routes them to a state-tendered **default fund** at one of a small set of approved providers (the list is re-tendered every few years). Default funds carry capped management fees:

- Default-fund management fee on **deposits** (`דמי ניהול מהפקדות`): typically capped near 1.0% (verify).
- Default-fund management fee on **accumulated balance** (`דמי ניהול מצבירה`): typically capped well below 0.25% (verify).

These caps are materially below market rates for actively-elected funds; one of Argosy's plan-critique checks is "is the user paying default-fund fees, or did they elect a fund that's now charging more than the default would?".

## Why it's different from `kupat gemel`

A `kupat pensia` and a `kupat gemel` look superficially similar (both are long-term, tax-advantaged retirement vehicles), but the legal and economic structures diverge:

| Feature | `kupat pensia` | `kupat gemel` |
|---|---|---|
| Mandatory? | Yes (employees) | No (voluntary or employer-elected) |
| Survivor / disability pooling | **Yes** — risk pooled across fund members; built into the fund mechanics | No — pure savings vehicle |
| Withdrawal default | **Annuity** (lifetime monthly payment) | **Lump sum**, unless the user elects an annuity track |
| Tikun 190 applicability | No — Tikun 190 is a `kupat gemel` mechanism | Yes (after age 60, see `kupat_gemel.md`) |
| Severance routing | **Yes** — the 6% employer-side severance contribution lives here | No — severance routes to severance-pay vehicles (`pitzuyim`) |

The survivor-pooling distinction is the single biggest reason employees should not blindly consolidate everything into a `kupat gemel` track: doing so loses the longevity-and-disability insurance baked into the pension fund.

## Withdrawal options at retirement

At statutory retirement age, the user has three broad paths:

1. **Annuity** — the default. Convert accumulated balance into a monthly lifetime payment via the fund's actuarial conversion factor. Best for users who want longevity insurance and a stable retirement income.
2. **Partial lump sum + annuity** — withdraw the portion above the *recognized capital* (`היוון`) threshold as a lump sum (subject to a one-time tax computation), use the remainder for the annuity. Common when the user has a specific large near-retirement spending need (mortgage payoff, real-estate purchase).
3. **Full lump sum** — only available below the recognized-capital threshold; not generally available for the bulk of an Israeli middle-class user's pension balance.

The `מס הכנסה` rules around `קצבה מזכה` (the lifetime exemption pot allocated across various retirement vehicles) interact with these decisions; cite `domain_knowledge/tax/israel/brackets_2026.md` and the standalone `recognized_pension_exemption.md` (when added) for exact mechanics.

## Surviving-spouse mechanics

This is the feature that most differentiates `kupat pensia` from `kupat gemel`:

- The pension fund pools mortality risk across all members. Members who die early subsidize the longevity of members who outlive the actuarial median.
- A **surviving-spouse annuity** (`קצבת שאירים`) is built in: on the user's death, the spouse receives a percentage (typically 60%, verify) of the user's pension annuity for the rest of their life.
- An **orphan annuity** is built in for dependent children (typically 30% per child up to a cap, verify).
- A **disability annuity** is built in: if the user is declared unable to work before retirement age, a monthly payment kicks in, scaled to accumulated rights.

The cost of these built-in insurance components is folded into the fund's expense ratio — the user doesn't pay a separate premium, but their accumulated balance grows slightly slower than a pure-savings vehicle would have. This is the trade-off the mandatory-pension regime forces.

## The user's situation

- The user is a NVIDIA Israel employee — by mandate, has a `kupat pensia` running every month with both employee and employer contributions.
- The intake agent should ask for: provider name, balance (NIS), employee contribution rate, employer match, and whether the fund is the **default-fund** (`ברירת מחדל`) variant or actively elected. If actively elected, also capture the management-fee numbers.
- Plan-critique should flag: management-fee gap vs. the default-fund cap (a "pay 0.5% on accumulation when the default would have charged 0.22%" finding is a YELLOW item, not a buying recommendation but a re-elect-the-default suggestion).

## How agents should use this file

- **Cite this file** for any claim about Israeli mandatory-pension mechanics, surviving-spouse annuities, default-fund fee caps, or annuitization rules.
- Pair with `kupat_gemel.md` and `keren_hishtalmut.md` when comparing the three pension vehicles.
- Pair with `national_insurance.md` for the Bituach Leumi pension (a separate state-paid layer).
- The intake agent should record provider name, balance, contribution rate, and employer match for both spouses.
- The plan-critique agent should flag: (a) absent contribution data, (b) management-fee gap vs. default-fund caps, (c) consolidation proposals that would lose the survivor-pooling.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual (January)** — contribution rates, retirement ages, and the default-fund tender refresh.
- **On reform** — the Israeli pension regime is periodically reformed (most recently the 2017 mandatory-pension expansion to self-employed); any reform triggers a full refresh.

## Performance data

The MoF gemelnet portal publishes per-fund 12m / 36m / 60m / YTD returns plus a sector benchmark for every `kupat pensia`. The Argosy adapter is documented in `domain_knowledge/brokers/gemelnet.md`. Snapshots flow into `pension_fund_snapshots` keyed by `fund_type="kupat_pensia"`. When citing a fund's recent performance, query `argosy.state.queries.get_user_pension_snapshots(user_id)` and cite the row's `source_url` (`gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx`).

A 12m relative-to-benchmark gap of more than ~1pp warrants a "consider switching providers" gap entry, with the caveat that a pension fund switch is administratively heavier than a `kupat gemel` switch (the new fund has to honor accumulated rights and the survivor-pooling actuarial position).

## Open issues

- The exact January 2026 contribution rates and ceilings need verification; the figures above use the 2025 vintage as the working number.
- The default-fund tender list rotates; verify which providers are on the current list before quoting a default-fund fee floor.
- The recognized-capital (`קצבה מזכה`) exemption interacts with `keren hishtalmut` and `kupat gemel` annuitization in non-obvious ways; cross-reference `tax/israel/brackets_2026.md` before making any annuity-vs-lump-sum recommendation.
