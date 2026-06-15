"""Task 5 — deterministic input-freshness (currency) check.

The system trusts its own stored state as ground truth: macro can read a stale
regime, and the portfolio snapshot can be the *pre-sale* book. A defect that
lives in WHEN an input was captured — not in any single value — slips past every
value-level check, because each number is internally consistent with a stale
world. This gate distrusts stale stored inputs by comparing their capture date
to `today`.

An input is a currency defect when:
  - it has no date at all (we cannot prove it is current), or
  - its age (`today - input_date`) exceeds the input's freshness window.

A future-dated input (negative age — e.g. clock skew or a forward-stamped
report) is treated as fresh, not stale: it is current relative to `today`.

Pure function, no I/O. The freshness windows are the only tunables and are
passed as keyword arguments, not magic constants.
"""
from __future__ import annotations

from datetime import date

from argosy.quality.gate_types import GateCheck, GateViolation


def check_input_freshness(
    *,
    today: date,
    snapshot_date: date | None,
    analyst_report_dates: dict[str, date | None],
    max_snapshot_age_days: int = 2,
    max_report_age_days: int = 3,
) -> list[GateViolation]:
    """Flag stored inputs that are stale (or undated) relative to `today`.

    Args:
        today: the run date the plan is being produced for.
        snapshot_date: the date the portfolio snapshot was captured, or None.
        analyst_report_dates: ``{report_name: capture_date | None}`` for each
            cached analyst output the run relies on (e.g. ``{"macro": ...}``).
        max_snapshot_age_days: freshness window for the snapshot.
        max_report_age_days: freshness window for cached analyst outputs.

    Returns:
        A list of :class:`GateViolation` — empty when every input is current.
        A missing date is a violation (freshness cannot be proven). A
        future-dated input (negative age) is treated as fresh.
    """
    violations: list[GateViolation] = []

    if snapshot_date is None:
        violations.append(
            GateViolation(
                check=GateCheck.INPUT_FRESHNESS,
                detail=(
                    "portfolio snapshot has no capture date — cannot prove it is "
                    "current vs today; treat as a stale/unknown input (pre-sale-book "
                    "class)."
                ),
                locator="snapshot",
            )
        )
    else:
        age = (today - snapshot_date).days
        if age > max_snapshot_age_days:
            violations.append(
                GateViolation(
                    check=GateCheck.INPUT_FRESHNESS,
                    detail=(
                        f"portfolio snapshot is {age} days old "
                        f"(captured {snapshot_date.isoformat()}, today {today.isoformat()}), "
                        f"exceeding the {max_snapshot_age_days}-day freshness window — "
                        "the stored book may be stale (the pre-sale-book currency defect)."
                    ),
                    locator="snapshot",
                )
            )

    for name, report_date in (analyst_report_dates or {}).items():
        if report_date is None:
            violations.append(
                GateViolation(
                    check=GateCheck.INPUT_FRESHNESS,
                    detail=(
                        f"cached analyst output `{name}` has no capture date — cannot "
                        "prove it reflects the current regime; treat as stale/unknown."
                    ),
                    locator=name,
                )
            )
            continue
        age = (today - report_date).days
        if age > max_report_age_days:
            violations.append(
                GateViolation(
                    check=GateCheck.INPUT_FRESHNESS,
                    detail=(
                        f"cached analyst output `{name}` is {age} days old "
                        f"(captured {report_date.isoformat()}, today {today.isoformat()}), "
                        f"exceeding the {max_report_age_days}-day freshness window — "
                        "it may read a stale regime."
                    ),
                    locator=name,
                )
            )

    return violations
