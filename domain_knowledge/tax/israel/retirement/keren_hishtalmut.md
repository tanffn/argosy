---
topic: israel_keren_hishtalmut
jurisdiction: israel
last_verified: 1900-01-01
next_refresh_due: 2026-12-31
sources:
  - url: https://www.taxes.gov.il/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.btl.gov.il/Insurance/Pages/default.aspx
    retrieved: 1900-01-01
    tier: 1
  - url: https://www.kolzchut.org.il/he/%D7%A7%D7%A8%D7%9F_%D7%94%D7%A9%D7%AA%D7%9C%D7%9E%D7%95%D7%AA
    retrieved: 1900-01-01
    tier: 2
---

# Keren Hishtalmut (`קרן השתלמות`)

A Keren Hishtalmut is an Israeli savings vehicle originally meant for "advanced training" but in practice used as the most powerful tax-advantaged liquid investment account available to Israeli employees. Contributions made within the statutory ceiling, after a 6-year vesting period, can be withdrawn **tax-free on both principal and growth**.

> **Verification status:** `last_verified: 1900-01-01`. Numbers below are 2024–2025 vintage. Domain-refresh agent must verify ceilings annually as they index with the average wage.

## How it works

| Aspect | Rule |
|---|---|
| Employer contribution | Up to 7.5% of monthly salary (employer-side; tax-deductible to employer) |
| Employee contribution | Up to 2.5% of monthly salary (employee-side; from net pay or pre-tax depending on plan) |
| Salary ceiling for the tax-free wrapper | **~15,712 NIS/month** (2024–2025 figure; verify yearly) — annual equivalent ~188,500 NIS |
| Maximum deductible contributions | 10% of salary up to ceiling = ~1,571 NIS/month all-in (employer 7.5% + employee 2.5%) |
| Vesting period for tax-free withdrawal | **6 years** from first contribution (or earlier for "training/study" purposes) |
| Tax on withdrawal after 6 years | **0%** on the principal AND on capital gains, up to the ceiling-funded portion |
| Tax on contributions above the ceiling | Not allowed inside the wrapper; excess is paid as taxable salary |
| Investment options | Can be invested in stock, bond, or mixed tracks at multiple providers |

## Why this is unusually valuable

- **Liquid after 6 years** — unlike pension or `kupat gemel` accounts, the user can withdraw without retirement-age constraints.
- **Zero capital-gains tax** on the wrapper — genuine tax-free compounding within the 188.5k NIS/yr ceiling.
- **Effectively a 47%+ instant return** on the marginal contribution because the alternative is the 47% top bracket on the same labor income.

## Self-employed track

A self-employed individual can contribute up to ~4.5% of qualifying earnings to a personal `keren hishtalmut` and also benefit from a 7% pre-tax deduction (subject to its own ceilings). Less generous than the employee track but still useful.

## The user's situation (from May 2026 TSV)

- Ariel reports 384,000 NIS in `Keren Hishtalmut` (Dec 2025 snapshot).
- Noga's value is `?` — not provided.
- Plan-critique agents should flag missing Noga data as a YELLOW item: this is a material asset and the plan currently understates the family's tax-advantaged liquid pool.
- The user is at NVIDIA Israel salary 500k NIS/yr, comfortably above the 188.5k ceiling, so the wrapper is **maxed out** by the employer plan automatically. No optimization opportunity there beyond ensuring contributions actually land at the chosen track.

## Withdrawal mechanics

After 6 years of vesting:
- Funds can be withdrawn in cash with **no tax** up to the ceiling-funded portion.
- If left in place beyond 6 years, the wrapper continues to compound tax-free indefinitely until withdrawal.
- Partial withdrawals are allowed; the remaining balance continues compounding.
- The 6-year clock is per *plan*, not per contribution; any new contributions to the same plan continue under the same vesting clock.

## How agents should use this file

- **Cite this file** for any "what is the after-tax return on this NIS?" calculation that involves Keren Hishtalmut.
- The intake agent should ask for current `Keren Hishtalmut` balance and provider for **both spouses**.
- The plan-critique agent should flag absent or stale `Keren Hishtalmut` data as YELLOW.
- If `last_verified` is older than 12 months OR is `1900-01-01`, report `confidence=low`.

## Refresh cadence

- **Annual (January)** — ceilings index with average-wage.
- **On legislation** — any Knesset bill touching the wrapper rules triggers refresh.

## Performance data

The MoF gemelnet portal publishes per-fund 12m / 36m / 60m / YTD
returns plus a sector benchmark for every `keren hishtalmut`. The
Argosy adapter is documented in
`domain_knowledge/brokers/gemelnet.md`. When citing a fund's recent
performance, query
`argosy.state.queries.get_user_pension_snapshots(user_id)` and cite
the row's `source_url`. A 12m relative-to-benchmark gap of more than
~1pp warrants a "consider switching providers" gap entry.

## Open issues

- The exact January 2026 ceiling needs verification; the figures above use the 2025 ceiling as the working number.
