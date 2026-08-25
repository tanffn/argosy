Do not launch another round yet. Run 463 is not one edit from promotion: its persisted draft still fails the deterministic gate, and several “authorities” are not actually reviewing the same final artifact.

1. Where 62.5% came from

It is stale June tradeable-book math, not a narrower sleeve:

- Old: 11,471 shares × $200.14 ≈ $2.296M / $3.67M investable book = 62.52%.
- Current: $2,228,793.61 / $3,925,157.61 tradeable book = 56.782% → 56.8%.

The smoking gun is settled proposal 63. Its cap adjudication accidentally included:

> pace multiple = 62.5/13.0 = 4.81x

That exact text was injected into every run-463 synthesis prompt. The model obeyed it. Remove `62.5` and `4.81x` from the settlement; a cap adjudication must not freeze a snapshot-dependent numerator.

Today’s replacement is 56.8 / 13.0 = 4.37x, but the robust fix is to omit the ratio or derive it from tokens.

2. Do only value-pair corrections land?

Directionally yes, but not absolutely. F5’s narrative funding correction landed. The actual distinction is mechanically testable versus merely aspirational:

- Wrong/canonical pairs give the pipeline deterministic presence/absence checks.
- Narrative corrections without canonical values are explicitly handed to reader judgment.
- The code already supports `required_statement`, but you did not give C6 one. See [corrective_context.py](D:/Projects/financial-advisor/argosy/services/corrective_context.py:124).

Turn C6 into a modal/state replacement:

- WRONG: “9,230 definitively covers 8,918; the entire programme can run at capital rates now.”
- CANONICAL: “9,230 is a pre-sale estimate; 8,918 is a programme ceiling, not a released eligible order.”
- MUST BE ABSENT: “fully covered”, “entire programme runs at capital rates now”, “pool larger than the sale”.
- REQUIRED STATEMENT: use the wording below.

Do the same for the AMBER issue:

- WRONG: “The after-tax gap is a timing problem.”
- REQUIRED: “Tax-year spacing can reduce surtax, but it cannot eliminate the underlying Section-102 capital-gains tax. Timing mitigates the after-tax gap; it does not by itself close it.”

3. Minimum honest C6 wording

Use this nearly verbatim:

> The 9,230 eligible-share count comes from the 18 June tax simulation, before the 560-share sale on 12 August. Because the sold shares have not been mapped back to that simulation, the current eligible balance is between 8,670 and 9,230; it therefore does not yet prove that all 8,918 planned-sale shares qualify. The 8,918 figure is the programme ceiling, not a released order. Each tranche may use only trustee-confirmed eligible lots, must remain within the per-decision size cap, and cumulative sales may not exceed the lesser of 8,918 and the re-derived remaining eligible balance. Any residual waits for its own eligibility date. Missing cost basis prevents an exact after-tax-proceeds estimate; it does not by itself determine the 24-month eligibility clock.

That is executable: it defines the order-release condition and sizing rule without pretending the entire programme is currently eligible.

Do not copy the FM’s “up to 3,700 shares from grants 289172/289173.” The tax simulation contains only 820 shares across those grants, not 3,700. That recommendation is unsupported.

4. Shortest path and Codex’s dual role

One more round is realistic only after a preflight cleanup. Launching it now is another burn.

Codex being both phase-4.5 reviewer and a required promotion authority is not inherently circular; the promotion gate is an aggregator, not another reviewer. The structural defects are elsewhere:

- Reader reconciliation mutates the draft after Codex and FM review it, but Codex and FM are not rerun on the mutated artifact.
- Verdicts are selected by run/phase, not bound to an artifact hash. See [promotion_authorities.py](D:/Projects/financial-advisor/argosy/quality/promotion_authorities.py:1).
- `DecisionRun.status="completed"` is committed before the deterministic gate and reader run. See [orchestrator.py](D:/Projects/financial-advisor/argosy/orchestrator/flows/plan_synthesis/orchestrator.py:1685). Run 463’s worker was still alive when I inspected it, despite the DB saying completed.
- “Rederivation” is not independent: `/accept` marks it approved whenever `HEADLINE_NUMERIC_SOURCE` is clean. See [plan.py](D:/Projects/financial-advisor/argosy/api/routes/plan.py:3914) and [plan.py](D:/Projects/financial-advisor/argosy/api/routes/plan.py:3966).

For today:

1. Let run 463 truly terminate.
2. Remove `62.5/4.81x` from settlement 63.
3. Install the exact C6 and after-tax replacement contracts above.
4. Fix the deterministic false positives below.
5. Run fresh from phase 1—no donor-379 reuse and no old phase-3 checkpoints.
6. Anchor on draft 124, but fix the corrective-context header so it does not simultaneously tell the fleet that plan 92 is the base.
7. Require the first reader pass to clear, so no post-Codex/FM artifact mutation occurs.
8. Run the acceptance gate read-only before attempting promotion.

5. What you were still missing

The largest missed check: I reran the actual deterministic acceptance gate on persisted draft 124 after the surgical reconcile. It still has seven blocking violations across six checks:

- `headline_numeric_source`: unregistered `₪68,403`.
- `fx_unit_direction` ×2: correct percentage-change prose is misread as spot rates:
  - “USD/NIS weakened 1.81 percent”
  - “90-day USD/NIS move is +1.55 percent”
- `event_currency_consistency`: false positive caused by parsing `/domain_knowledge/tax/...` source paths as a generic tax event.
- `history_leak`: the word “supersedes” survives in assumption A11.
- `fi_fx_shock_sufficiency`: “FI reached” lacks “at current FX” in the same sentence.
- `cap_derivation`: draft 124’s persisted allocation document is still 12%, because it was generated before the constant reverted. The code change to 13% does not retroactively repair draft 124.

Also, runs 456/461/463 reused phases 1–2 from run 379. They were not fresh end-to-end runs. That stale donor is why old weights and tax-universe assumptions keep contaminating otherwise fresh resolver output.

So the blunt answer is: your LLM correction is now close; your promotion pipeline is not. Clear the deterministic/code-level defects and stale reuse first. Then one final fresh large run is plausible. Running before that is gambling another round.