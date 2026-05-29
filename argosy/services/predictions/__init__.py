"""Predictions ledger service package — Spec C.

Per-source writer adapters live in ``writers.py``; the outcome evaluator
(spec commit #4) and reliability accessor (commit #5) land as sibling
modules in this package.

Public surface (commit #3 — writer adapters):

  * ``write_discord_prediction``
  * ``write_news_signal_prediction``
  * ``write_per_position_thesis_prediction``
  * ``write_state_observer_prediction``
  * ``write_monitor_flag_prediction``

Each writer is per-source idempotent on a deterministic ``message_id``
(re-running with the same source-stable id returns the existing row, no
duplicate insert). See ``writers.py`` for the per-source ``message_id``
formulas + ``evaluation_due_at`` / ``evaluation_method`` selection rules.
"""
from __future__ import annotations

from argosy.services.predictions.writers import (
    write_discord_prediction,
    write_monitor_flag_prediction,
    write_news_signal_prediction,
    write_per_position_thesis_prediction,
    write_state_observer_prediction,
)

__all__ = [
    "write_discord_prediction",
    "write_monitor_flag_prediction",
    "write_news_signal_prediction",
    "write_per_position_thesis_prediction",
    "write_state_observer_prediction",
]
