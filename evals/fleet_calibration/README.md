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

## Running

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe evals/fleet_calibration/run_suite.py --dry-run   # audits only, no LLM
.venv/Scripts/python.exe evals/fleet_calibration/run_suite.py            # full suite (hours; Opus)
.venv/Scripts/python.exe evals/fleet_calibration/run_suite.py --only pltr_2023_f1,nvda_2023_f2
.venv/Scripts/python.exe evals/fleet_calibration/score.py runs/<date>.json
```
