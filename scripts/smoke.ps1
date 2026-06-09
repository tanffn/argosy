# Argosy smoke test — fast routine verification during a session.
#
# Run THIS for per-change confidence (spine consistency guardrail + money-math
# core + frequently-touched subsystems), ~2-3 min. Run the FULL suite only ONCE,
# at the end of a session/sprint (and before a PR):
#   $env:PYTHONIOENCODING="utf-8"; .venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider -q
# See auto-memory: feedback_smoke_test_not_full_suite.
$env:PYTHONIOENCODING = "utf-8"
$files = @(
  "tests/test_cross_surface_consistency.py",  # THE spine guardrail (every surface reconciles to the plan)
  "tests/test_target_allocation_doc.py",       # canonical TargetAllocationDoc schema + builder
  "tests/test_allocation_plan.py",             # allocation engine money-math
  "tests/test_allocation_glidepath.py",        # /plan glidepath -> doc
  "tests/test_retirement_stochastic_fx.py",    # FX sigma/mu money-math
  "tests/test_per_ticker_analysts.py",         # decision fleet (long-hold default)
  "tests/test_scheduler.py",                   # cadence scheduler
  "tests/test_monitor_drift.py",               # emergent monitor
  "tests/test_monitor_macro_shift.py",
  "tests/test_hour_loop.py"
)
& .venv/Scripts/python.exe -m pytest -m "not llm_eval" -p no:cacheprovider -q @files
