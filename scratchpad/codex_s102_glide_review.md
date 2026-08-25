VERDICT: **DO NOT LAUNCH — `tax.nvda_embedded_cgt_glide_nis` carries forward the trustee simulation’s 25% capital / 62.17% ordinary withholding mix and applies 30% only to the price change; it is not the settled 30% / 50% sale-tax computation and currently underpublishes the glide tax by about ILS 112.7k.**

1. Ordinary-slice conclusion

Your narrower conclusion is right: the eligible RSU rows contain a benchmark-based ordinary slice.

For the known RSU grants:

- `cost_basis_usd − ordinary_income_usd/shares = 0.010996`
- `sale_price − (capital_income + ordinary_income)/shares = 0.010996`

That equality identifies the offset as the per-share fee deducted from taxable income, not FMV substitution or aggregation error. Grant 246477 especially rules out FMV: $31.9749/share is effectively the $31.9859 trustee benchmark less $0.011, nowhere near $38.4351.

The exact distinguishing query is:

```sql
SELECT
    plan_type,
    grant_id,
    shares,
    cost_basis_usd,
    ordinary_income_usd / shares AS ordinary_per_share,
    cost_basis_usd - ordinary_income_usd / shares AS basis_offset,
    sale_price_usd
      - (capital_income_usd + ordinary_income_usd) / shares AS fee_per_share
FROM tax_simulation_lots
WHERE user_id = 'ariel'
  AND simulation_date = (
      SELECT simulation_date
      FROM tax_simulation_lots
      WHERE user_id = 'ariel'
      ORDER BY ingested_at DESC
      LIMIT 1
  )
  AND eligible = 1;
```

For RSUs, `basis_offset ≈ fee_per_share ≈ 0.010996` confirms benchmark-minus-fee.

But that does not prove that `realization_tax_summary` taxes the slice at 50%. It starts from `gross − net_proceeds`, where the workbook net embeds its withholding rates, and only adjusts the price delta afterward. See [tax_simulation_ingest.py](/D:/Projects/financial-advisor/argosy/services/tax_simulation_ingest.py:448) and [tax_simulation_ingest.py](/D:/Projects/financial-advisor/argosy/services/tax_simulation_ingest.py:485).

There is also one legitimate exception to “every eligible lot”: the 200-share eligible ESPP row has:

- purchase basis: $11.847
- ordinary income: $37.497/share
- capital income: $155.295/share

Its tax split point is approximately $49.355, with the paid purchase basis untaxed. It must remain an explicit `plan_type='ESPP'` branch; the RSU formula `ordinary = shares × B` does not apply unchanged to it.

2. The 6.5% gap

Ranked explanations:

### 1. Confirmed: mixed trustee-withholding rates

For the exact 8,920 shares selected by the resolver:

```text
ordinary income at simulation       $258,983.461495
capital income at simulation      $1,564,027.052000
gross − workbook net                $552,114.867517
```

The workbook tax is almost exactly:

```text
0.25 × 1,564,027.052
+ 0.6217 × 258,983.461495
= $552,016.781011
```

The remaining $98.09 is fees/wire residue.

The resolver then computes:

```text
[$552,114.867517
 + 0.30 × ($215.38 − $204.65) × 8,920]
× 2.991
= ILS 1,737,257.59
```

That is precisely the published token. The source locator claiming “Section 102, 30% effective” is therefore materially misleading; only the price delta receives 30%. See [plan_numeric_resolver.py](/D:/Projects/financial-advisor/argosy/services/plan_numeric_resolver.py:2554).

Applying the settled rates to those same selected income columns:

```text
current capital income
= $1,564,027.052 + ($215.38 − $204.65) × 8,920
= $1,659,738.652

two-year tax
= [0.30 × $1,659,738.652 + 0.50 × $258,983.461495] × 2.991
  − 2 × 0.02 × ILS 721,560
= ILS 1,847,730.86
```

That is ILS 110,473 above the token and explains about 98% of the reported gap.

The handover’s own stated inputs reproduce its number exactly:

```text
0.30 × ILS 4,870,275
+ 0.50 × ILS 835,418
− 2 × 0.02 × ILS 721,560
= ILS 1,849,929.10
```

So the remaining roughly ILS 2,198 is merely different lot/input composition.

### 2. Different lot universe and selection

The sources disagree:

- Resolver: sell 8,920, retain 1,460, eligible pool 9,230.
- Corrected handover: sell 8,857, retain 1,523, eligible pool 9,573.
- Tax simulation still covers 10,940 total shares, while the new snapshot has 10,380 vested.

Furthermore, `_cap_group_shares` is not selecting lowest benchmark first. It orders `grant_date` as a `DD/MM/YYYY` string:

```python
.order_by(TaxSimulationLot.grant_date.asc(), TaxSimulationLot.id.asc())
```

That yields blank ESPP dates first, then `08/04/2024`, `08/06/2022`, `08/06/2023`, `09/07/2021`—neither chronological nor lowest-benchmark order. See [tax_simulation_ingest.py](/D:/Projects/financial-advisor/argosy/services/tax_simulation_ingest.py:409).

Confirm with:

```sql
SELECT id, plan_type, grant_id, grant_date, shares,
       ordinary_income_usd / shares AS ordinary_per_share
FROM tax_simulation_lots
WHERE user_id = 'ariel'
  AND simulation_date = '18/06/2026'
  AND eligible = 1
ORDER BY grant_date ASC, id ASC;
```

I cannot determine the exact remaining ILS 2,198 because the new 8,857-share per-lot schedule is recorded only in the handover, not as a persisted machine-readable schedule. The required missing input is a list of `(tax year, plan type, grant ID, shares sold)` for the corrected glide.

### 3. The 63-share difference is not the 6.5%

Proportionally:

```text
ILS 1,737,257.59 / 8,920 × 63
= ILS 12,269.87
```

Under the settled formula using a low-benchmark $18.3195 lot:

```text
63 × [0.30 × (215.38 − 18.3195) + 0.50 × 18.3195] × 2.991
= ILS 12,865.81
```

That is about 0.7%, not 6.5%. It also has the wrong sign: selling 63 more shares should make the resolver tax higher, yet the resolver is ILS 112,671 lower.

Explaining ILS 112,671 with 63 shares would require ILS 1,788 tax per share, while gross proceeds are only about ILS 644 per share—impossible.

### 4. Tax-year scheduling

The handover gives two annual capital-source thresholds, saving:

```text
2 × 2% × ILS 721,560 = ILS 28,862.40
```

The dated resolver token does not actually calculate two tax years. It merely relabels eligibility as of 2027-12-31 and then aggregates all shares through the same hybrid calculation. That is why dated and undated both resolve to ILS 1,737,258.

I cannot establish the exact final surtax without the per-year sale allocation and each year’s other capital-source income. The required function is a schedule-aware loop over `section_102.capital_tax_ils(..., prior_capital_income_ils=...)`, not `realization_tax_summary(as_of_date=...)`.

### 5. Price, FX, fees, or rounding

These cannot plausibly explain the gap:

- Required price difference: about `$14.08/share`.
- Required FX difference: roughly `0.18 NIS/USD`, around 6%.
- The observed fee offset is only about `$0.011/share`, under ILS 300 across the glide.

3. Launch decision

Do not launch.

The single blocking defect is that the fact token which will be published as the glide’s tax is not computed from the settled tax model. It publishes ILS 1,737,258 where the same-input settled calculation is approximately ILS 1.848M and the handover calculation is ILS 1.849929M.

The 8,920-versus-8,857 scope mismatch also needs reconciliation, but it is not the cause of the 6.5% discrepancy and is not needed to establish the launch blocker.

4. Retention tokens

They represent two legitimate, distinct component rates, but one is actively mislabelled:

- `tax.retention_at_vest_pct = 0.50`: numerically correct as retention of the ordinary-income slice, but “at vest” is false. It occurs at sale.
- `tax.retention_capital_track_pct = 0.70`: correct as retention of the capital-gain slice at the 30% high-income marginal rate. It is not retention of the whole Section-102 capital-track sale.

For an RSU sale, whole-gross retention is approximately:

```text
1 − [0.30(S−B) + 0.50B] / S
= 0.70 − 0.20B/S
```

It is therefore lot- and price-dependent—around 68% here—not either 50% or 70%.

This is user-visible, not merely internal naming debt. The assumption ledger explicitly renders “50% at-vest ordinary-income retention” in [render.py](/D:/Projects/financial-advisor/argosy/orchestrator/flows/plan_synthesis/render.py:775), and the canonical surface does the same in [live_surfaces.py](/D:/Projects/financial-advisor/argosy/quality/live_surfaces.py:297). `figure_registry.py` and `ips.py` only own/transport the fields; they do not combine them into tax arithmetic.

The clean semantics would be:

```text
tax.retention_ordinary_slice_pct = 0.50
tax.retention_capital_gain_slice_pct = 0.70
tax.nvda_glide_net_retention_pct = schedule/lot-specific result
```

5. Real-path proof test

The existing `smoke_fact_tokenize` is insufficient: it declares PASS when the resolver and tokenizer do not throw; it never independently checks the tax value.

Add a `glide-tax-token` path to [smoke_real_paths.py](/D:/Projects/financial-advisor/scripts/smoke_real_paths.py:151) that:

1. Copies the real dev DB to its existing throwaway directory.
2. Loads a real persisted plan and puts  
   `GLIDE={{fact:tax.nvda_embedded_cgt_glide_nis}}`  
   into one copied plan body.
3. Calls the production read path `render_plan_facts(..., use_cache=False)` from [fact_token_render.py](/D:/Projects/financial-advisor/argosy/services/fact_token_render.py:389).
4. Independently queries the selected real lots from the copied DB.
5. Computes capital and ordinary slices directly from `capital_income_usd` and `ordinary_income_usd`, revalues capital to the live price, applies 30%/50%, and applies the actual per-year threshold schedule.
6. Asserts:

```python
assert rendered_amount == round(independent_oracle)
assert provenance["tax.nvda_embedded_cgt_glide_nis"]["value"] == independent_oracle
assert resolved_sell_sh == sum(per_year_sale_shares)
```

Run it as:

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv\Scripts\python.exe scripts\smoke_real_paths.py glide-tax-token
```

On today’s code and DB, that test should fail with approximately:

```text
rendered: ILS 1,737,258
oracle:   ILS 1,847,731–1,849,929
```

The range remains only because the corrected 8,857-share per-lot/per-year schedule is not yet persisted.