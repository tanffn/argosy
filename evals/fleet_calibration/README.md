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

### Decision lab (separate from live recommendations)

```powershell
.venv/Scripts/python.exe -m evals.fleet_calibration.lab --only boot_2017,ttcf_t2 --audit-only
# Prepare NEW case-bound sourcing receipts; existing legacy receipts are never rewritten.
.venv/Scripts/python.exe evals/fleet_calibration/prepare_classifier.py --reviewer-approved --only nlf_synthetic,omk_synthetic,boot_2017,ttcf_t2 --receipts-dir evals/fleet_calibration/classifier_receipts/v2
.venv/Scripts/python.exe -m evals.fleet_calibration.lab --only boot_2017,ttcf_t2 --out evals/fleet_calibration/runs/lab/<new>.json
.venv/Scripts/python.exe -m evals.fleet_calibration.market_outcomes evals/fleet_calibration/runs/lab/<new>.json --case boot_2017 --symbol BOOT
```

Legacy lab protocol 2 prepended the sourced `nlf_synthetic` / `omk_synthetic` controls.
Current lab protocol **3** uses `nlf_synthetic_v3` / `omk_synthetic` instead.
The original NLF fixture and failed runs are preserved: v3 corrects its inconsistent
trailing valuation arithmetic using an explicit year-ago quarter and TTM basis.
It changes neither the expected verdict nor the underlying business/risk story.
Protocol-2 manifests continue to use their original pinned hashes; the versions
cannot substitute each other's controls. Prepare the new case-bound receipt with
`prepare_classifier.py --reviewer-approved --only nlf_synthetic_v3 --receipts-dir evals/fleet_calibration/classifier_receipts/v2`
after the required dry-run and independent review. The receipt binding protocol
remains v2; the control-fixture protocol is v3. A sanitizer rejection remains an
exclusion, never evidence that an investment prediction was correct.
It stops historical interpretation if either control fails. An independent
review exclusion is not a passing run, even when the action matches our label.
Every run uses a new artifact path. The final `.lab.json` seals the decision
artifact; post-decision `.outcomes.json` stores raw price evidence separately.
The Portfolio replay panel shows the latest sealed lab and available outcomes.
New lab qualification requires sourcing receipts bound to the case ID and the
hash of the exact classifier input. Legacy receipts without that provenance
remain history, not certified evidence; the loader prefers `classifier_receipts/v2`.
Review warnings remain visible. Explicit integrity violations or a reviewer
declaring no grounded reasoning prevent certification; no numeric investment
quality threshold is added to the live fleet.

This is the production **Trader judgment** with independent benchmark agents,
not a replay of the entire live allocation workflow. Company masking and
fictional household context protect the test boundary; this command does not
automatically anonymize arbitrary user exports. Fixed 6/12/24-month adjusted
market returns versus SPY are **gross diagnostics**, not actual sized,
after-tax trades. Missing/delisted bars remain unknown, not zero or a silently
removed case. Class agreement, outcome returns and live forward calibration
are different evidence. Curated examples do not establish an unbiased edge,
and aliasing cannot guarantee a model does not remember a historical story.

### Discovery screen + grader replay

This separate lab runs the production quick screen and discovery grader, not
the Trader or full historical universe sourcing. Case manifests and prices are
pinned. V2 screen inputs contain only the exact existing frozen business
description instead of V1's identical generic stub. Packet financials, expected
stage labels and price observations are unchanged. Earlier failed runs remain.
The fictional context boundary is explicit; missing real filings are intentional
for fiction, while unspecified economic facts remain unknown.

```powershell
.venv/Scripts/python.exe -m evals.fleet_calibration.discovery_lab --partition development --audit-only
.venv/Scripts/python.exe -m evals.fleet_calibration.discovery_lab --partition development --out evals/fleet_calibration/runs/lab/<new-dev>.json
.venv/Scripts/python.exe -m evals.fleet_calibration.discovery_lab --partition reserved_evaluation --out evals/fleet_calibration/runs/lab/<new-reserved>.json
.venv/Scripts/python.exe -m evals.fleet_calibration.discovery_lab --outcomes evals/fleet_calibration/runs/lab/<sealed>.json --out evals/fleet_calibration/runs/lab/<new-outcomes>.json
```

Run call-free audit and independent input review before real judgments. Both
controls and integrity reviews must pass. An exclusion or unattempted case is
not a correct prediction. Outcomes are fetched only after decisions are sealed.
6/12/24-month adjusted returns are gross market diagnostics, not allocations or
after-tax trades. Curated examples and retrospective bars cannot establish an
unbiased discovery edge, and masking cannot exclude model memory.

### Evidence-delivery tuning (separate from investment-class controls)

This experiment audits real archived decisions and tests whether the grader
receives and faithfully uses evidence. It does not change expected labels in
failed curated-control runs or use BUY frequency as a success criterion.
Freeze first, independently review the runner, then execute each stage once:

```powershell
.venv/Scripts/python.exe -m evals.fleet_calibration.tuning freeze --out logs/<snapshot>.json
.venv/Scripts/python.exe -m evals.fleet_calibration.tuning audit --snapshot logs/<snapshot>.json --out logs/<audit>.json
.venv/Scripts/python.exe -m evals.fleet_calibration.tuning paired --snapshot logs/<snapshot>.json --partition development --out logs/<development>.json
.venv/Scripts/python.exe -m evals.fleet_calibration.tuning paired --snapshot logs/<snapshot>.json --partition reserved --development logs/<development>.json --out logs/<reserved>.json
# After the audit and paired judgments are sealed; no returns enter any agent.
.venv/Scripts/python.exe -m evals.fleet_calibration.tuning_outcomes --snapshot logs/<snapshot>.json --audit logs/<audit>.json --out logs/<historical-diagnostics>.json
```

Source archive and selection can be preserved after **reviewed mechanical**
runner fixes using `freeze --source-snapshot <sealed-original>` with a new output.
This does not authorize post-result policy changes or reruns of a claimed trial.
Semantic one-shot claims live under `runtime/research_trials/`; changing output
names, freeze timestamps or parent references does not reset them. Do not remove
claims to retry until green. Failed/incomplete attempts remain evidence.

If an attribution check fails only because the quoted words span Markdown bold
formatting or whitespace, `tuning_attribution --source <sealed-run> --out <new>`
can produce a **separate** word-preserving adjudication. Original grades,
findings/severities, model receipts and seals never change. Its normalization
code hash and per-quote proofs are retained. Independently review and declare
that rule before opening reserved results, then apply it to every reserved arm,
not just favorable cases. It cannot repair new words, numbers, case changes,
missing calls or genuinely unsupported quotes. Do not equate workflow completion
with an assessable or favorable result.

Both arms use the same frozen policy and Opus model; only evidence rendering
differs. Tool-free isolated sessions have no schema/session retries. There are
at most16 SDK sessions per four-pair partition, plus at most4 screen-audit sessions.
Sessions can include up to3 internal text turns. API `max_tokens` is not an
effective CLI token cap. Admission checks an observed USD30 stop, including
before assessors; already-inflight calls and failed-call cost uncertainty remain
explicit. A missing/unverified pair makes the behavioral result inconclusive.

The old screen diagnostic retains original policies and all missing denominators.
It measures30-calendar-day adjusted-price moves versus matched SPY, not returns
from BUYs, allocations or fills. Current-provider history, unresolved historical
identity and earlier operator exposure limit retrospective conclusions. A strong
subsequent gain does not establish that a rejected name was predictable.

### Original suite commands

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
