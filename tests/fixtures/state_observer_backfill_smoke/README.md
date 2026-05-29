# `state_observer_backfill_smoke/` fixtures

Smoke-test fixtures for `argosy/scripts/state_observer_backfill.py` --
the empirical merge gate for Spec B (state-observer agent design,
2026-05-29).

## Why this exists

The backfill script (Spec B commit #5) runs the StateObserverAgent
against historical state snapshots and asserts -- per Appendix C of
the spec -- that the FX 3.6 -> 2.8 case surfaces as an *emergent* flag
on the most recent snapshot. The acceptance gate is:

  K=5 samples per snapshot, M=4 of K samples must surface an
  `macro.fx_*` primary_field at severity `warning` or `critical` on
  the most-recent snapshot, AND median severity across the K samples
  per date must be non-decreasing as the deviation grows.

A real-LLM run of this gate spends Opus tokens and is `llm_eval`-marked.
The fixtures in this directory let CI exercise the script's REPORT
logic + GATE arithmetic + DEGRADED-MODE behavior **without** any LLM
call:

- `fx_3p6_to_2p8_acceptance.json` -- five mock snapshots showing the
  progressive FX deviation (3.5 -> 3.4 -> 3.1 -> 2.95 -> 2.8 over five
  monthly snapshots), with K=5 canned `StateObserverOutput`
  candidate-lists per snapshot. The dry-run path of the backfill
  script reads this and emits matching candidate lists.

## Fixture contract

The dry-run agent reads the fixture, indexes by `snapshot_date`, and
returns the appropriate canned `StateObserverOutput` per sample. The
ordering of samples within a snapshot is preserved (sample 0 is run 0,
etc.) -- this is what gives the fixture its **deterministic** quality:
the dry-run report is byte-identical across invocations.

The fixture is consciously designed so that:

- Snapshot 1 (T-120d, baseline at FX=3.5): 0 FX flags. Tests the
  "below the materiality threshold" path -- the observer does NOT
  flag a sub-3% deviation as an FX issue.
- Snapshot 2 (T-90d, minor drift at FX=3.4): 4/5 FX flags at info
  severity. Tests the "rising but still info" path.
- Snapshot 3 (T-60d, warning band at FX=3.1): 5/5 FX flags at warning.
- Snapshot 4 (T-30d, warning-to-critical at FX=2.95): 5/5 FX flags
  mixing warning + critical. Tests that the acceptance gate's
  "warning OR critical" tolerance works.
- Snapshot 5 (T0, current at FX=2.8): 5/5 FX flags, 4/5 critical +
  1/5 warning. **This is the merge gate** -- the most-recent
  snapshot's flag-count must satisfy M>=4 of K=5 at warning|critical.

The "median severity non-decreasing" property follows: info -> info ->
warning -> warning/critical -> critical/warning across the five dates.

## How the dry-run agent uses this

The backfill script's `_FakeStateObserverAgent` (defined in
`argosy/scripts/state_observer_backfill.py`) takes a path to this
fixture and an iteration counter; when `run(...)` is called, it
returns the `samples[iter_idx]` candidate-list for the snapshot whose
date matches the incoming `snapshot_date` kwarg.

If `snapshot_date` is missing from the fixture (e.g. the script
asked for a date with no canned data), the fake agent returns an
empty `StateObserverOutput` and notes the gap in the report. This
graceful-degradation contract is what lets the script handle a
real DB with fewer than 5 historical snapshots (the actual `db/argosy.db`
has only one portfolio snapshot at 2026-03-24) without false-failing.

## Not for real-LLM use

This fixture documents the SHAPE the dry-run path emits. It is NOT a
ground-truth target for the live LLM runs -- the live Opus output
will differ in rationale phrasing, cited_sources ordering, and
severity exactly (per Appendix C.4: "expected_backfill_shape.json is
a shape fixture, not a value fixture"). The acceptance gates test
STRUCTURE, not values.

## Updating the fixture

When the spec's acceptance methodology changes (e.g. K bumps from 5
to 7, or the merge gate adds a "median severity non-decreasing"
assertion), update this fixture FIRST so the dry-run tests catch any
script-side mismatch before the live-LLM run discovers it.

Per [[feedback_accuracy_over_cost]]: the live-LLM run remains the
authoritative empirical gate; this fixture just makes the script's
logic test-coverable without spending Opus tokens on every CI run.
