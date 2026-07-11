# Fleet-calibration benchmark suite

Committed home for the decontaminated time-machine protocol. **Spec:**
`docs/design/fleet_calibration_benchmark.md` — read it before touching anything here.
Proven ad-hoc harness it was lifted from: `tmp/fleet_timemachine/` (2026-07-09/10 runs).

## Layout

- `packets/*.json` — one fixture per **point** (a case may have several time points;
  each is its own fixture). Packets are FROZEN once written (factual-error fixes only,
  logged in the fixture's `changelog` field).
- `run_suite.py` — the runner. Sequential live Opus calls through the production
  `TraderAgent` (long_hold, T2). Persists incrementally after EVERY point.
- `score.py` — scoring + report generation from a persisted run.
- `agent_pipeline.py` — five-stage contracts: construction classifier,
  independent sanitizer, production Trader, replay reviewer, and grader.
- `runs/<date>.json` — raw results, one file per suite run (committed, longitudinal).
- `test_calibration.py` — pytest entry, marked `llm_eval` (excluded from default suite).

## Packet fixture schema

```jsonc
{
  "case_id": "pltr_2023_f1",          // unique, snake_case
  "alias": "MDS",                      // masked ticker used in the packet
  "category": "A",                     // A entry / B trap / C exit / D hold-drawdown / synthetic
  "grading": "F1_lenient",             // F1_lenient | F2_strict | trap | exit | hold_drawdown |
                                       // synthetic_winner | synthetic_trap | entry | rederive
  "real": "PLTR @ 2023-03-01 (~$8.35)",// ground truth — NEVER sent to the LLM
  "freeze_date": "2023-03-01",         // null for synthetics
  "rescale_factor": 3.0,               // per-case k; null for synthetics
  "synthetic": false,
  "expected_classes": ["buy"],         // acceptable TraderProposal.action values
  "positioned": false,                 // true => positions text carries an existing holding
  "positions": "...",                  // full positions_snapshot text sent to the agent
  "constraints_extra": "exit_rule",    // null | "exit_rule" (v77 no-price-exit rule)
  "contamination_terms": ["Palantir", "PLTR", "Karp", "AIP"],  // auto-disqualify on hit
  "resolution": {                      // for the agent-score column; null for synthetics
    "price_at_freeze_real": 8.35,
    "price_at_horizon_real": 80.0,
    "horizon_label": "2y (2025-03)",
    "benchmark_return_pct": 858,       // buy-and-hold freeze->horizon (ride-to-terminal for traps/exits)
    "note": "..."
  },
  "sources": [                         // TEMPORAL-INTEGRITY AUDIT fields: every packet fact
    {"fact": "Q4-2022 first GAAP-profitable quarter, NI $31M",
     "url": "https://www.sec.gov/...", "date": "2023-02-13"}
  ],
  "analyst_reports": [ /* exact TraderAgent wire shape: fundamentals + news dicts */ ],
  "changelog": []                      // logged factual-error fixes only
}
```

## Invariants (enforced by the runner)

1. **Temporal integrity**: any `sources[].date` after `freeze_date` ⇒ the point is
   `disqualified_temporal` — it is not run and never scores.
2. **Decontamination**: alias + rescale-by-k + relative dates + genericized specifics.
   No calendar years anywhere in `analyst_reports`.
3. **Output contamination**: a run whose response names the real company / any term in
   `contamination_terms` is `contaminated` and doesn't score. Calendar years and a global
   real-world-name list produce `suspect` flags for manual adjudication.
4. **Synthetic control pair** (`qbt_synthetic`, `srl_synthetic`) stays in every suite run.
   If the pair fails, the lens regressed — stop, don't interpret the real cases.
5. **Scored-run immutability**: once `<run>_report.md` exists, `run_suite.py`
   refuses to reopen the matching JSON. Choose a new `--out` path.

## Five-stage agent workflow

1. **Classifier + data sourcing** runs only through
   `prepare_classifier.py`, at packet-construction time. It persists an immutable
   sidecar receipt under `classifier_receipts/`; suite scoring only loads and
   verifies that receipt, so a frozen case cannot absorb later knowledge. Real
   preparation calls require `--reviewer-approved`. Existing receipts are
   skipped and reported (never overwritten; write-once).
2. **Sanitizer** sees the exact masked packet/positions/constraints that Trader
   would receive, an independently constructed source manifest from the frozen
   packet's `sources` (plus `raw_sources` verbatim — never the classifier's
   `sourced_facts`), and the contamination denylist. It has no tools and does
   not see the expected verdict or outcome. Any failed or unverifiable protocol
   check prevents the Trader call; synthetic-only inapplicable checks are
   recorded as `not_applicable`.
3. **Production Trader** runs unchanged in `long_hold` / T2 mode. Its exact
   packet snapshot, rendered constraints, raw response, and structured output
   are persisted.
4. **Replay reviewer** reloads that persisted result from disk and audits
   cleanliness, packet fidelity, workflow correctness, and the grounding of the
   visible rationale. It never sees the expected class or eventual outcome and
   never claims access to hidden chain-of-thought.
5. **Grader** reloads the reviewer-enriched replay, then receives the expected
   class and resolution return. Its class/return claims are mechanically
   verified before `score.py` accepts its judgment.

Every stage receipt is written before the next stage reloads. Historical run
documents without stage receipts remain readable through the legacy scorer;
new incomplete, unclean, or mechanically-unverified pipelines do not score.
The generated report includes each stage's full structured output and
deterministic verification receipt; raw model responses remain in the run JSON.

## Running

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe evals/fleet_calibration/prepare_classifier.py --dry-run
# After resident-reviewer coordination only:
.venv/Scripts/python.exe evals/fleet_calibration/prepare_classifier.py --reviewer-approved --only <cases>
.venv/Scripts/python.exe evals/fleet_calibration/run_suite.py --dry-run   # audits + receipt presence, no LLM
.venv/Scripts/python.exe evals/fleet_calibration/run_suite.py --out runs/<new-run>.json
.venv/Scripts/python.exe evals/fleet_calibration/run_suite.py --out runs/<new-run>.json --only pltr_2023_f1,nvda_2023_f2
.venv/Scripts/python.exe evals/fleet_calibration/score.py runs/<date>.json
```

`--dry-run` is mandatory before any real-LLM run. Real scoring is
`llm_eval`-class: coordinate with the resident reviewer before invoking it.
The suite dry-run exits 2 while any classifier sidecar is missing; run the
call-free classifier-preparation dry-run first, then coordinate the real
construction-stage calls before attempting scoring.
