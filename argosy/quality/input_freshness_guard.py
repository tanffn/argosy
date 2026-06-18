# argosy/quality/input_freshness_guard.py
"""Refuse to synthesize on stale / low-confidence inputs.

A plan derived on a holdings snapshot that predates a trade is "one trade stale" — every
downstream number is wrong, and no coherence loop can fix bad inputs. This guard BLOCKs
synthesis and emits a ``needs_refresh`` list routed to ingest, rather than producing a
stale plan. Load-bearing inputs flagged LOW confidence (e.g. an unstable savings floor)
also block until stabilized.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class FreshnessVerdict:
    ok: bool
    needs_refresh: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def check_input_freshness(
    *, snapshot_as_of: date, today: date, max_age_days: int = 7,
    low_confidence_inputs: list[str] | None = None,
) -> FreshnessVerdict:
    needs: list[str] = []
    reasons: list[str] = []
    age = (today - snapshot_as_of).days
    if age > max_age_days:
        needs.append("holdings_snapshot")
        reasons.append(
            f"holdings snapshot is {age}d old (> {max_age_days}d) -> refresh before synth"
        )
    for k in low_confidence_inputs or []:
        needs.append(k)
        reasons.append(f"{k}: LOW confidence -> stabilize to one defensible figure first")
    return FreshnessVerdict(not needs, needs, reasons)
