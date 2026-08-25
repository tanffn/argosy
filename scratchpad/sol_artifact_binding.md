# Artifact-hash binding design

## Decision

Do not half-land artifact binding. A safe minimal patch does not exist in the
current shape because:

- Codex and FM judge the pre-persist synthesis output; the reader judges a
  database-assembled artifact.
- reader reconciliation, cap autocorrection, and fact tokenization can mutate
  persisted horizon markdown after Codex/FM have judged it.
- Codex/reader phase rows are selected by run and phase only, while the FM
  authority is reduced to `DecisionRun.fund_manager_decision` with no verdict
  artifact identity at all.
- `assemble_plan_artifact` is not presently pure: a read-only gate evaluation
  showed fact-token rendering attempt to update `monitor_flags.surfaced_at`.
  A hash implementation must not make authority evaluation depend on that side
  effect.

Adding a hash to only phase 4.5 or phase 5.5 would detect some stale verdicts
but still allow another unbound authority to clear the mutated artifact. That
would create the appearance of safety without the invariant.

## Required invariant

Promotion may clear only when every required authority verdict names the same
artifact identity as the exact persisted draft being promoted:

```text
draft artifact SHA-256
  = deterministic-gate artifact SHA-256
  = Codex artifact SHA-256
  = FM artifact SHA-256
  = whole-artifact-reader artifact SHA-256
```

Any missing hash, hash mismatch, assembly failure, or post-verdict mutation is
a fail-closed authority result. `decision_run_id` remains provenance; it is not
artifact identity.

## Canonical artifact

Introduce a pure `build_review_artifact(plan_version, *, read_only=True)` that
returns immutable bytes plus display text. Canonical bytes should be a
versioned JSON object with sorted keys and UTF-8 encoding, containing every
surface an authority or client can rely on:

- plan version id and artifact-schema version;
- long/medium/short rendered markdown;
- long/medium/short structured horizon JSON;
- structured sections JSON;
- target allocation document and authored overrides;
- audit markdown or an explicit declaration that audit surfaces are excluded;
- any generated dashboard/appendix surface included in authority prompts.

Do not include timestamps, database row ids unrelated to content, or mutable
render telemetry. The same builder must serve reviewer prompts and the
promotion-time recomputation. Split monitor-flag surfacing out of rendering so
`read_only=True` performs no writes.

Store `artifact_schema="review-artifact-v1"` and
`artifact_sha256=<64 lowercase hex>` beside every verdict. A schema version is
necessary so a later canonicalization change cannot compare unlike hashes.

## Persistence changes

1. Add `artifact_schema` and `artifact_sha256` columns to `decision_phases` (or
   a dedicated immutable `authority_verdicts` table). Columns are preferable
   to burying the binding only in `phase_output_json`, because promotion must
   query and index them fail-closed.
2. Persist an explicit phase-5 FM verdict row carrying `approved`, reasons,
   schema, and hash. Stop treating the unbound
   `DecisionRun.fund_manager_decision` scalar as sufficient authority; retain it
   as a denormalized status only.
3. Bind phase 4.5, phase 5, phase 5.3, and phase 5.5 rows to the exact artifact
   bytes supplied to each authority.
4. Record `final_artifact_sha256` on the draft or in an immutable artifact row
   after all mutation has ended. Recompute at promotion rather than trusting
   the stored value alone.

## Reconciliation ordering

The safe order for a synthesis attempt is:

1. Author and persist draft.
2. Run deterministic normalization/tokenization that is permitted to mutate.
3. Freeze artifact V1 and compute H1.
4. Run deterministic gate, Codex, FM, and reader against H1.
5. If any reconciliation mutates the draft, invalidate all H1 verdicts, build
   V2/H2, and rerun all four authorities. Do not carry Codex/FM approval across
   the mutation.
6. Once no mutation follows the final authority pass, persist final Hn and mark
   the run completed.

If the cost of rerunning all authorities after reader feedback is unacceptable,
the safe alternative is to remove in-run reader mutation: keep the BLOCK and
feed its correction to a new authored round. Reusing pre-mutation approvals is
not an option.

## Promotion query

Change `promotion_authorities` to accept the candidate `PlanVersion`, compute
its current review-artifact hash, and select the latest verdict per authority
where all of these match:

- `decision_run_id`;
- `artifact_schema`;
- `artifact_sha256`;
- required phase/authority kind.

Do not select the latest phase first and then ignore a mismatch. A later
verdict for a different hash means the candidate has no verdict. The promotion
receipt should show expected hash, observed hash, and the mismatching authority.

## Cutover and tests

- New runs: hashes are mandatory and missing bindings fail closed.
- Existing runs: do not backfill hashes from current draft content because that
  would falsely attest that old authorities reviewed those bytes. They require
  re-review or an explicit audited legacy override.
- Unit-test canonical serialization stability and one-byte mutation detection.
- Integration-test that gate/Codex/FM/reader all receive the same H1.
- Integration-test reader reconciliation H1 -> H2 invalidates every H1 verdict
  and reruns all authorities.
- Promotion-test missing, malformed, schema-mismatched, and stale hashes.
- Read-only-test artifact building against SQLite `mode=ro`; any write attempt
  must fail the test.

