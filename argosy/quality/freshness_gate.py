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

import re
from datetime import date

from argosy.quality.gate_types import GateCheck, GateViolation

# Split text into sentence-ish clauses on terminal punctuation / newlines —
# same convention as coherence_gate's _SENTENCE_SPLIT_RE, so a rendered date and
# its (mis)label are matched within the SAME clause, not document-globally.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")
# An ISO date as it would be rendered in user-facing prose.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Phrases that present a date as NOT-overdue. A PAST date carrying any of these
# (and not the word "overdue") is the defect: an overdue gate rendered as if it
# were still pending. "0 days" / "due today" are the boundary forms.
_NOT_OVERDUE_RE = re.compile(
    r"on[\s-]?deck|due today|due in \d+ days?|\bupcoming\b|scheduled for|\b0 days\b",
    re.IGNORECASE,
)
# An explicit "overdue" acknowledgement — the clause is correctly labeled and
# passes regardless of any not-overdue phrasing elsewhere in it.
_OVERDUE_RE = re.compile(r"\boverdue\b", re.IGNORECASE)


def check_output_date_staleness(*, today: date, text: str) -> list[GateViolation]:
    """Flag a PAST (overdue) date rendered as if it were not overdue.

    For each sentence-ish clause, find ISO dates. A clause is a defect when it
    contains a date strictly BEFORE ``today`` AND a not-overdue phrase (the
    ``_NOT_OVERDUE_RE`` set) AND does NOT say "overdue". One violation per
    offending clause (locator = the offending date string).

    A clause that already says "overdue" passes. A future or today date passes.
    Unparseable ISO-looking strings (e.g. ``2026-13-99``) are ignored — we never
    crash a gate on a bad date.
    """
    violations: list[GateViolation] = []
    for raw_clause in _SENTENCE_SPLIT_RE.split(text or ""):
        clause = raw_clause.strip()
        if not clause:
            continue
        if _OVERDUE_RE.search(clause):
            continue  # explicitly acknowledged as overdue — correct labeling
        if not _NOT_OVERDUE_RE.search(clause):
            continue  # no not-overdue framing in this clause
        for m in _ISO_DATE_RE.finditer(clause):
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue  # unparseable (e.g. 2026-13-99) — defensively ignore
            if d < today:
                violations.append(
                    GateViolation(
                        check=GateCheck.OUTPUT_DATE_STALENESS,
                        detail=(
                            f"date {m.group(0)} is before today ({today.isoformat()}) "
                            f"but the clause renders it as not-overdue ({clause!r}); "
                            "an overdue gate must be labeled 'overdue', not 'on-deck' / "
                            "'due in N days' / 'upcoming'."
                        ),
                        locator=m.group(0),
                    )
                )
                break  # one violation per offending clause
    return violations


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
