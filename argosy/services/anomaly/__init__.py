"""Anomaly-detection services (sprint #2).

Submodules:
  * ``rolling_stats`` — nightly merchant-rolling-statistics recompute
    backing Bucket A detectors (amount outliers).
  * ``bucket_a`` — Bucket A detectors (A1 category robust outlier + A2
    merchant spike). Consumes ``merchant_rolling_stats`` baselines and
    writes ``ExpenseReviewQueue`` rows with deterministic dedup keys
    per spec #2 §4.
"""
