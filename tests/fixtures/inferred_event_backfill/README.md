# Inferred-life-event detector backfill fixtures

Spec E commit #9 — empirical-proof-gate fixtures. Each JSON file is a
synthetic 12-month transaction stream the detector is run against; the
expected-findings shape is encoded both in the fixture's
`expectations` block and in `tests/test_inferred_life_event_backfill.py`.

The "anchor date" for every fixture is **2026-06-01** (matches the test
helper `_now()` in `tests/test_inferred_life_event_detector.py`). All
transaction `occurred_on` strings are ISO dates BEFORE that anchor so
the 365-day lookback window captures them.

## Fixture roster

| file | purpose | expected findings |
|---|---|---|
| `normal_year.json` | Typical household: groceries + utility + mortgage + 4mo kindergarten + car-service + annual insurance | **ZERO findings** (merge gate) |
| `tuition_stop_scenario.json` | 14mo of college tuition payments then 6mo gap | `>= 1` tuition_stopped (or kid_left_home) |
| `recurring_car_scenario.json` | 3 car purchases at ~5y cadence (extended-window lookback) | `>= 1` recurring_car_purchase |
| `wedding_scenario.json` | Single NIS 150k wedding-vendor transfer | `>= 1` wedding_scale_transfer |
| `kindergarten_only.json` | 9 months of NIS 3,500 kindergarten payments + no college/university transactions | **ZERO** kid_started_college findings (spec D #5 pattern-split BLOCKER) |

## Cross-fixture aggregate

The aggregate test asserts that across all 5 fixtures, the
"transparently false positive" pattern surface remains empty — e.g.
the normal_year fixture's NIS 30k family transfer must NOT fire
`wedding_scale_transfer` (strict NIS 100k floor per spec §5.3 / codex
BLOCKER #1 from spec-E-5 review).

## File shape

```json
{
  "name": "<fixture id>",
  "anchor_date": "2026-06-01",
  "lookback_days": 365,
  "description": "...",
  "expectations": {
    "patterns_expected": ["tuition_stopped"],
    "patterns_forbidden": ["wedding_scale_transfer"],
    "findings_total_min": 1,
    "findings_total_max": 5
  },
  "transactions": [
    {
      "occurred_on": "2025-09-01",
      "merchant": "TEL AVIV UNIVERSITY",
      "amount_nis": "6000",
      "direction": "debit",
      "tx_type": "regular"
    },
    ...
  ]
}
```

The loader in the test file converts each row into an
`ExpenseTransaction` ORM instance — `amount_nis` is parsed as Decimal,
`occurred_on` as ISO date.

## Why the dates aren't `today - N days`

Spec E commit #5 + spec §5.4 wire shadow mode against the user's
`created_at` and the orchestrator's `now` arg. The tests use a fixed
clock (`_now() = 2026-06-01`) so the absence/presence math is
deterministic. The fixtures encode absolute ISO dates against the same
clock — if you re-run with a different clock, override
`lookback_days` to widen the window so the fixture's transactions
still land in `[now - lookback_days, now]`.
