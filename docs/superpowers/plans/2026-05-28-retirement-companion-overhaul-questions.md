# Questions collected during autonomous execution

Surfaced when you return. None block progress — I've defaulted with reasoning.

## Wave 1 (Israeli reference values)
- **Q1.1**: Which Israeli pension funds get full mekadem coverage in the shipped YAML?
  - **Default chosen**: Clal + Migdal + Menorah (the three largest; user is at Clal per `clal_pension_*` keys in identity_yaml).
  - **When you'd want to change**: if you've switched funds since the identity_yaml was last refreshed.

## Wave 2 (Safety gates)
- **Q2.1**: Emergency-liquidity floor threshold (months).
  - **Default chosen**: 12 months of essential expenses (essential = burn × 0.6 — heating, food, mortgage, school).
  - **Why 12**: 6 is too thin for a single-earner household with concentrated equity exposure; 24 is overcautious and parks too much cash. 12 matches Bogleheads consensus for high-volatility-asset households.
  - **Override path**: `identity_yaml.retirement_reference_overrides.emergency_liquidity_floor_months`.

- **Q2.2**: NRA estate-tax WARN threshold.
  - **Default chosen**: WARN at $60K (the exemption itself), FAIL at $200K.
  - **Why**: $60K is the legal exposure cliff; $200K is the "you should already be acting" threshold.

## Wave 3 (Trust layer)
- **Q3.1**: Default withdrawal policy.
  - **Default chosen**: Guyton-Klinger guardrails.
  - **Why**: Bengen 4% is too rigid; VPW is too volatile for the user's risk profile; Guyton-Klinger's ratchet-up-in-good-years / cut-when-overdraw-by-20% policy is the empirically best-tested for concentrated-asset households.

- **Q3.2**: Default `target_p_solvent` (the probability-of-ruin threshold for "ON_TRACK" verdict).
  - **Default chosen**: 0.90 at age 95.
  - **Why**: 0.85 is too aggressive given the user's "better safe than sorry" instruction; 0.95 makes retirement-ready age unreachable in most realistic scenarios.

- **Q3.3**: Default Monte Carlo regime (calm / turbulent / regime-switch).
  - **Default chosen**: regime-switch (calm/turbulent/crisis with transition matrix calibrated to post-1970 US data).
  - **Why**: matches your "better safe than sorry"; lognormal-only is what the SDD review flagged.

## Wave 4 (Decision policy)
- **Q4.1**: Glide path policy.
  - **Default chosen**: Vanguard target-date glide (gradual equity decline from 90% at age 30 to 50% at age 65, holding 30% in retirement).
  - **Why**: best-documented; matches Bogleheads consensus.

- **Q4.2**: Healthcare-cost ramp magnitude.
  - **Default chosen**: 1.5%/yr real growth above CPI starting age 65, ramping to 3%/yr after 80.
  - **Why**: OECD Israel data shows ~1-3% real healthcare cost growth above CPI for elderly cohorts.

## Wave 5 (Tax engine)
- **Q5.1**: Tax engine granularity — per-cashflow-source or per-lot?
  - **Default chosen**: per-cashflow-source.
  - **Why**: per-lot is over-engineering for retirement planning (only matters for active trading); can extend later if needed.

## Wave 6 (Balance sheet)
- **Q6.1**: Real-estate appreciation default for Israeli primary residence.
  - **Default chosen**: 3.5%/yr nominal (matches Bank of Israel historical 2000-2024 median for Tel Aviv metro).
  - **Why**: conservative central estimate; user can override per-property.

## Wave 7 (Companion UX)
- **Q7.1**: Action-engine re-compute cadence.
  - **Default chosen**: weekly + on-trigger (the §0 replan-triggers from gap #25 fire on-demand).
  - **Why**: daily is too noisy; monthly misses fast-moving life events; weekly + on-trigger balances both.

---

## Open structural questions
None at this time. Anything that requires user judgement will be added here as I encounter it.
