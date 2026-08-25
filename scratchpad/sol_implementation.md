# Sol implementation report

## 1. Files changed

- `db/argosy.db` — updated only `action_proposals.id=63` rationale/payload to remove frozen `62.5`/`4.81x` while preserving `settled_value="13.0"` (ignored runtime state, so not visible in `git diff`).
- `argosy/quality/fx_gate.py` — distinguishes percentage-change prose from a USD/NIS spot-rate claim.
- `argosy/quality/event_currency_gate.py` — masks tax source paths, excludes tax-simulation source prose, and preserves numeric qualifiers such as Section-102 in event identities.
- `argosy/quality/regex_patterns.py` — limits active `supersedes` history leaks to explicit prior-artifact objects, allowing current policy precedence.
- `argosy/services/corrective_context.py` — adds reusable exact C6/after-tax WRONG/CANONICAL/MUST-BE-ABSENT contracts, loads the Codex-only after-tax AMBER, deduplicates the C6 finding, and rejects the unsupported 3,700-share recommendation from the contract.
- `argosy/orchestrator/flows/plan_synthesis/orchestrator.py` — moves the terminal `completed`/`finished_at` commit until after the deterministic gate, reader/reconcile, corrective check, and authority receipt.
- `tests/test_fx_gate.py` — regression coverage for both draft-124 percentage-change phrasings and the still-blocked bare-percent spot form.
- `tests/test_event_currency_gate.py` — regression coverage for `/domain_knowledge/tax/...`, `domain_kb:` tax paths, tax-simulation prose, and distinct Section-102 tax events.
- `tests/test_plan_output_gate.py` — regression coverage for legitimate assumption-ledger `supersedes` plus an explicit prior-plan leak guard.
- `tests/test_corrective_context.py` — verifies both exact contracts load into a corrective round and the unsupported 3,700 value is absent.
- `tests/test_plan_synthesis_instage_gate.py` — verifies the gate and reader both observe `status="running"`, followed by terminal completion.
- `scratchpad/sol_artifact_binding.md` — structural artifact-hash binding design; no partial implementation landed.
- `scratchpad/sol_implementation.md` — this implementation and verification record.

The pre-existing modifications in `domain_knowledge/tax/israel/retirement/section_102.md` and `domain_knowledge/tax/israel/surtax.md` were not touched.

## 2. Tests and outputs

Final authoritative runs:

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_fx_gate.py tests/test_event_currency_gate.py tests/test_plan_output_gate.py -q
145 passed in 2.49s
```

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_corrective_context.py -q
69 passed, 51 warnings in 215.53s (0:03:35)
Warnings: Alembic Config path_separator deprecation only.
```

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_plan_synthesis_instage_gate.py tests/test_plan_synthesis_reader_reconcile.py tests/test_plan_synthesis_v4_e2e.py::test_v4_phase5_pipeline_runs_end_to_end -q
6 passed, 6 warnings in 27.58s
Warnings: Alembic Config path_separator deprecation only.
```

Development/focused runs also performed:

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_fx_gate.py tests/test_event_currency_gate.py tests/test_plan_output_gate.py -q
145 passed in 2.68s
```

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_corrective_context.py::test_run_463_required_statement_contracts_load_from_verdicts tests/test_corrective_context.py::test_verdict_finding_carries_required_statement -q
2 passed, 2 warnings in 9.21s
```

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_plan_synthesis_instage_gate.py -q
2 passed, 2 warnings in 11.19s
```

```text
.venv/Scripts/python.exe -m pytest -m "not llm_eval" tests/test_corrective_context.py::test_run_463_required_statement_contracts_load_from_verdicts tests/test_corrective_context.py::test_verdict_finding_carries_required_statement -q
2 passed, 2 warnings in 8.52s
```

The first full `tests/test_corrective_context.py -q` attempt hit the command's 124-second tool timeout with no pytest result. It was rerun with a 360-second timeout and passed all 69 tests as recorded above; the timeout was not a test failure.

Additional verification:

```text
git diff --check
exit 0; only Git's existing LF-to-CRLF working-copy warnings
```

A whole-file Ruff diagnostic was also run. It reported 92 existing issues across the legacy orchestrator/test files (including pre-existing import ordering, `datetime.UTC`, unused imports, and the existing fixture-import F811 pattern). A narrowed E/F run found only the pre-existing 104-character comment in `regex_patterns.py` and the pre-existing `synth_db` fixture-import F811s; no newly introduced E/F error was identified.

## 3. Read-only deterministic acceptance gate on draft 124

Both measurements used `argosy.api.routes.plan._run_plan_output_gate` plus `_gate_blocking_checks` with SQLAlchemy connected to:

```text
sqlite:///file:D:/Projects/financial-advisor/db/argosy.db?mode=ro&uri=true
```

This is the real acceptance helper and blocker split, with SQLite enforcing no writes. The helper's artifact assembly attempted to update `monitor_flags.surfaced_at`; read-only SQLite rejected that write and the helper continued. I did not call the production `/accept` route. No FM-dialogue or plan rows were created: `max(decision_runs.id)` remains 469 and `max(plan_versions.id)` remains 124.

Re-measured before implementation: **5 blocking violations**:

1. `ips_allocation_sum`
2. `headline_numeric_source`
3. `event_currency_consistency` (false positive)
4. `cap_derivation`
5. `fi_fx_shock_sufficiency`

The earlier seven-count could no longer be reproduced because the live reader-reconcile worker had already removed the two FX prose matches and the `supersedes` prose from persisted draft 124. Their regressions are nevertheless fixed and tested.

After implementation: **4 blocking violations**. `event_currency_consistency` is gone. The remaining violations are genuine draft-content defects:

- `headline_numeric_source` — long line 168 publishes unregistered `₪68,403`; no resolved NIS value is within tolerance. The gate correctly demands a registered derivation or pending label.
- `fi_fx_shock_sufficiency` — the sentence says FI is reached without an FX qualifier, while the deterministic minus-10% USD/NIS result is below the total FI capital target. The claim itself needs qualification.
- `cap_derivation` — draft 124's persisted allocation document is still 12.0% and attributes the change from current plan 92's 13.0% to the user. The settled cap remains 13.0%; a future authored draft must regenerate the document and state Argosy's derivation.
- `ips_allocation_sum` — the medium structured IPS exposes only two `pct_of_portfolio` sleeves (8.0% NVDA and 10.1% cash), totaling 18.1%, although that schema surface is defined as the full 100% medium-horizon allocation partition. Missing sleeves are artifact content, not a parser mistake.

## 4. Not done

- Did not launch any synthesis or amendment run.
- Did not promote or modify any `plan_versions.role`; the only current plan remains 92.
- Did not change the 13.0 NVDA cap, 3.0% SWR, NIS 300,000 FI basis, or any other household policy value.
- Did not edit either protected tax file.
- Did not clean up stale `fm_objection_dialogue` run 469. It predates this implementation, remains `running`, and cleanup was not authorized; importantly, the read-only gate created no successor rows.
- Did not implement artifact-hash binding. It is structural: Codex/FM and reader currently review different surfaces, FM has no hash-bound verdict, post-review mutation invalidates earlier verdicts, and artifact assembly is not pure. The complete safe design is in `scratchpad/sol_artifact_binding.md`.
- Did not run the entire repository suite; verification was scoped to every changed subsystem and the real acceptance helper, per repository test guidance.
- Did not commit.

## 5. Review hardest

1. Review the required-statement matcher and contract strictness in `corrective_context.py`. It intentionally re-feeds only the named Codex after-tax contract, deduplicates C6 across FM/reader, and replaces heuristic extracted figures so the unsupported “up to 3,700 shares” recommendation cannot leak into the next round.
2. Review lifecycle exception/cancellation paths around the moved terminal commit. Normal, reader-reconcile, and v4 end-to-end paths are tested; a crash after draft persistence now correctly cannot advertise `completed`, but the owning worker must still mark terminal failures as failed.
3. Review that event-path masking is narrow enough to ignore only domain tax provenance and model/source uses while retaining real dated/named tax-event currency flips.
4. Inspect proposal 63 directly because `db/` is ignored: `settled_value` must be `13.0`, and neither `rationale_md` nor `suggested_payload` may contain `62.5` or `4.81x`.
5. Treat artifact binding as one atomic follow-up. Hashing only one or two authorities would be a false assurance; the design requires a pure canonical artifact and all authorities bound to the same final hash.

Final constraint audit: current plan ids `[92]`; max plan version `124`; max decision run `469`; settlement 63 cap `13.0`; frozen numerator/multiple absent.
