## Decision

**Force closure today—but force run 470’s final draft, never plan 124, and mechanically remove the known estate fragment first. Do not run another LLM regeneration.**

All eight requested corrections landed. The remaining substantive defect is one stale estate subtotal:

- Wrong prose: `$3.0291m` and roughly `$1.19m` tax.
- Canonical: `$3.0515m` / `₪9,127,060`, including AVUV, REET, and VHT.
- Impact: it does not alter any current trade, allocation, NVDA share count, Section-102 calculation, or FI decision.
- But it could contaminate the counsel brief and changes a displayed tax estimate by roughly `$9k`. Therefore it should be mechanically struck, not silently blessed.

The moonshot objection is not a blocker. No moonshot buy is proposed, and executable proposals already enforce genuine sleeve membership, estate disclosure, and a derived dollar cap. The FM correctly passed C3.

## Minimum honest closure

1. Let run 470 produce its final draft—probably plan 125. Do not fall back to 124 if it fails to produce one.

2. Apply a deterministic, non-LLM patch to that draft:

   - Remove `$3,029.1k`.
   - Remove the derived `$1.19M` estate-tax estimate.
   - Publish only `{{fact:concentration.us_situs_estate_exposure_nis}}`.
   - Replace “AVUV/REET/VHT situs classification is missing” with: their domicile classification is US-situs and already included; only the snapshot marker needs backfilling.

3. Run the deterministic output and leakage checks against the exact patched bytes. Do not override leakage.

4. Persist an `acceptance_override` object in `synthesis_inputs_json`, including:

   - Ariel’s authorization and timestamp.
   - Run/draft ID and artifact SHA-256.
   - Authorities overridden and their complete reasons.
   - Canonical estate amount and superseded literals.
   - Prior current plan: 92.
   - Execution holds:
     - no NVDA order until the trustee ledger is refreshed;
     - no NVDA tranche until a deterministic share cap is published;
     - counsel brief must use the domicile-classified canonical estate set;
     - moonshot buys remain subject to the allocation verifier.

5. Keep an `accepted-with-overrides` banner or version-label suffix visible.

The current “audit logging” is only an in-process WebSocket event in [plan.py](/D:/Projects/financial-advisor/argosy/api/routes/plan.py:90); it is not a durable audit ledger. `accepted_by_user_id` is durable, but the override rationale is not. Recording the metadata above is therefore mandatory for an honest force.

Rollback is already atomic through [the rollback route](/D:/Projects/financial-advisor/argosy/api/routes/plan.py:4292), targeting plan 92.

## Exact overrides

Assuming the patched final draft passes the deterministic gate and has `corrective_unresolved=[]`, the minimum call is:

```powershell
$draftId = 125  # replace with run 470's actual produced draft
$uri = "http://127.0.0.1:8000/api/plan/draft/$draftId/accept?user_id=ariel&override_fm_rejection=true&override_promote_gate=true"
Invoke-RestMethod -Method Post -Uri $uri
```

The important missing lever from the question is `override_promote_gate`; Codex or reader BLOCK requires it in [the unified promotion gate](/D:/Projects/financial-advisor/argosy/api/routes/plan.py:4068).

Do **not** add:

- `override_corrective` if the list is empty. The FM verified all eight corrections landed.
- `override_gate` if the deterministic gate passes.
- `override_leakage` under any circumstances here.

If the final accept preflight reports a genuinely blocking deterministic violation, inspect it. Do not automatically pile on `override_gate`.

## If you decline to force

One PATCH round should be enough; two is the absolute ceiling.

Order:

1. Register any new derived fact required—preferably the USD estate subtotal; omit the tax estimate rather than typing it.
2. Patch only the estate rationale and estate missing-data fields.
3. Add the exact required statement covering AVUV/REET/VHT inclusion.
4. Optionally tighten the moonshot wording to recite the already-enforced verifier conditions.
5. Reuse run 470’s fresh phases 1–2; run only the bounded PATCH/review path.

If the first round re-seeds unrelated digits, do not launch a second broad authoring pass. Apply a deterministic artifact patch and force the now-stale reviewer verdict.

## Structural fix

I agree completely with the diagnosis.

The present tokenizer already approximates “replace matching registered literals,” but it runs post-synthesis, is best-effort, covers only anchored facts, and merely reports mismatches; see [orchestrator.py](/D:/Projects/financial-advisor/argosy/orchestrator/flows/plan_synthesis/orchestrator.py:1789) and [fact_tokenizer.py](/D:/Projects/financial-advisor/argosy/quality/fact_tokenizer.py:1). That cannot guarantee consistency.

The smallest effective construction rule is:

> At Phase-3 slice validation, reject every raw financial literal—money, percentages, share counts, weights, rates—unless it is a registered `{{fact:key}}` or `{{derived:key}}` token.

Rejecting only literals that match canonical values is insufficient: it catches a typed `13` but does not prevent the contradictory typed `12`.

Dates, statute numbers, and identifiers should use explicit typed exceptions. Executable action amounts should be `FactRef` fields rather than strings. Only after every slice passes should the deterministic renderer insert digits. Then the LLM authors language, never financial values, and this entire failure class becomes impossible by construction.

## What I would do

I would wait for run 470’s final draft, mechanically remove the four estate-text defects, persist the override record and execution holds, verify deterministic/leakage cleanliness, then accept with `override_fm_rejection=true&override_promote_gate=true`.

I would close it tonight. I would not regenerate it again.