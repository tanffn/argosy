"""Inferred life-event detector (Spec E commit #5).

Reads the ``expense_transactions`` stream and produces phase-change
proposals to the ``action_proposals`` ledger via the existing
``action_proposer_runner`` — never writes directly to ``life_events``.
Two-layer design (spec §5.2):

  1. **Heuristic layer** — pure-Python deterministic detectors.  Each
     heuristic returns 0..N ``HeuristicFinding`` instances with a
     confidence band (high / medium / low).
  2. **LLM disambiguator** — fired only when (a) heuristic confidence
     is medium/low OR (b) the pre-proposal conflict resolver flagged
     the finding for review.  Out-of-scope for this commit's wiring;
     the seam is documented + a unit-test stub exercises it.

Five guardrails (spec §5.4)
===========================

The codex BLOCKER from the spec is the pre-proposal conflict resolver:
both a ``tuition_stopped`` and a ``kid_started_college`` finding firing
on the same counterparty within an overlapping window is structurally
a re-categorisation, NOT a life event.  The resolver runs BEFORE any
proposer call and SUPPRESSES (sets ``dismissed=True``) both findings
when the counterparty is shared; otherwise marks them
``aliased_pair_disambiguator_required`` so the LLM can decide.

Counterparty-continuity check: the secondary guardrail catches the
"single autopay merchant switch" case — the disappearing counterparty
re-appears within 3 months under a different category.  When
detected, the heuristic confidence is DOWNGRADED to ``low`` (forces
the finding through the LLM disambiguator).

Shadow mode (spec §5.4 + Ariel's locked decision)
=================================================

For the first 30 calendar days after a user's account creation, the
detector runs in SHADOW MODE: findings are written with
``dismissed=True`` and ``conflict_resolution='no_conflict'`` but NO
action_proposals row is fired.  The shadow window lets the operator
calibrate the heuristic thresholds against the user's real
transaction stream before any user-visible proposal lands.

Cadence (spec §5.5 + Ariel's locked decision)
=============================================

Run daily at 03:00 IDT — after midnight (idle window), before the
17:00 IDT news pipeline + state observer.  See
``argosy/orchestrator/loops/inferred_life_event_detector.py``.
Per-run cost: heuristic pass is pure Python (sub-second); LLM
disambiguator fires at most ~5x/run (low transaction cardinality).

Public surface
==============

* ``run_detector(session, user_id, *, lookback_days=180,
  shadow_mode=None) -> DetectorSummary`` — the orchestration entry
  point the loop's ``tick`` calls.
* The five heuristics + the conflict resolver + the continuity check
  are exposed as module-private functions so tests can drive them
  individually with synthetic transaction fixtures.

Idempotency
===========

Re-running the detector on the same (user, window, pattern) tuple
hits the natural-key UNIQUE constraint on
``inferred_life_event_findings`` — duplicate INSERTs raise
``IntegrityError`` which is caught + treated as "already processed,
skip".  Verified by ``tests/test_inferred_life_event_detector.py::
test_unique_constraint_idempotent_redetect``.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Literal

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from argosy.state.models import (
    ExpenseTransaction,
    InferredLifeEventFinding,
    User,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — heuristic thresholds (spec §5.3)
# ---------------------------------------------------------------------------

#: Spec §5.3 — tuition_stopped requires 12+ months of recurring then 6+
#: months absence.  Tunable per the backfill verification commit.
TUITION_PRIOR_MONTHS_MIN: int = 12
TUITION_GAP_MONTHS_MIN: int = 6

#: Spec §5.3 — recurring_car_purchase requires >= NIS 60k single
#: transfer + 2 priors + inter-arrival stdev < 1y.
CAR_AMOUNT_THRESHOLD_NIS: Decimal = Decimal("60000")
CAR_PRIOR_COUNT_MIN: int = 2
CAR_INTERARRIVAL_STDEV_HIGH_DAYS: float = 365.0  # < 1y -> high
CAR_INTERARRIVAL_STDEV_MEDIUM_DAYS: float = 730.0  # 1y..2y -> medium

#: Spec §5.3 — wedding_scale_transfer requires single transfer >=
#: NIS 100k.  The spec's confidence rule is "medium always" — no
#: low-confidence band; transfers below NIS 100k DO NOT fire (codex
#: BLOCKER #1 from the spec-E-5 review: a low-threshold variant
#: increased false-positive risk over the 12-month backfill gate
#: without adding signal).
WEDDING_AMOUNT_THRESHOLD_NIS: Decimal = Decimal("100000")

#: Spec §5.3 — recurring_renovation requires >= 3 transactions to
#: construction labels within 90 days totalling >= NIS 50k.
RENOVATION_WINDOW_DAYS: int = 90
RENOVATION_COUNT_MIN: int = 3
RENOVATION_TOTAL_THRESHOLD_NIS: Decimal = Decimal("50000")
RENOVATION_ABSENCE_MONTHS_MIN: int = 18

#: Spec §5.3 — kid_started_college is the inverse of tuition_stopped:
#: 6+ months absent then 3+ months present.
COLLEGE_ABSENCE_MONTHS_MIN: int = 6
COLLEGE_PRESENCE_MONTHS_MIN: int = 3

#: Spec §5.4 shadow-mode threshold — new account (within 30 days) ->
#: shadow.  Ariel's locked decision per spec.
SHADOW_MODE_NEW_ACCOUNT_DAYS: int = 30

#: Spec §5.4 guardrail — counterparty re-appearance window for the
#: continuity check.  If the disappearing counterparty re-appears
#: within 3 months under a different category, downgrade confidence.
CONTINUITY_REAPPEARANCE_DAYS: int = 90

#: Spec §5.4 — tuition-family conflict-pair overlap window.  Two
#: findings whose windows are within this distance are checked for
#: aliased-pair conflict (re-categorisation masked as life event).
CONFLICT_PAIR_OVERLAP_DAYS: int = 90

#: Spec §5.1 — counterparty-label patterns the heuristics match.
#: Substring + lower-case match.  Argosy's expense_transactions don't
#: have an explicit label field; we string-match against
#: ``merchant_raw`` + ``merchant_normalized`` (lower-cased).
_TUITION_LABEL_PATTERNS: tuple[str, ...] = (
    "tuition",
    "school",
    "kindergarten",
    "university",
    "college",
    "academy",
)
#: ``kid_started_college`` uses a STRICTER subset — kindergarten /
#: school enrollments fire ``tuition_stopped`` when they end but
#: must NOT fire ``kid_started_college`` when they begin (a 3-yo
#: entering kindergarten is NOT the same life-event-phase change as
#: an 18-yo entering college).  Codex BLOCKER #2 from the spec-E-5
#: review: the synthetic-year fixture's HAPPY KINDERGARTEN payments
#: would falsely fire kid_started_college without this split.
_COLLEGE_ONLY_LABEL_PATTERNS: tuple[str, ...] = (
    "university",
    "college",
    "academy",
)
_CAR_LABEL_PATTERNS: tuple[str, ...] = (
    "dealer",
    "dealership",
    "leasing",
    "car",
    "auto",
    "garage",
    "motor",
)
_WEDDING_LABEL_PATTERNS: tuple[str, ...] = (
    "wedding",
    "reception",
    "chatuna",
    "marriage",
    "gift",
    "wedding_vendor",
)
_RENOVATION_LABEL_PATTERNS: tuple[str, ...] = (
    "construction",
    "renovation",
    "contractor",
    "builder",
    "plumber",
    "electrician",
    "tiles",
    "shiputs",
)

#: Spec §5.4 — patterns the conflict resolver pairs across.  Only the
#: tuition-family pairs cross-reference (per the spec's "tuition-family
#: pairs" sub-bullet); other patterns are evaluated independently.
_CONFLICT_PAIR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("tuition_stopped", "kid_started_college"),
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


Pattern = Literal[
    "tuition_stopped",
    "recurring_car_purchase",
    "wedding_scale_transfer",
    "recurring_renovation",
    "kid_started_college",
    "phase_drop_other",
]

HeuristicConfidence = Literal["high", "medium", "low"]


@dataclass
class HeuristicFinding:
    """One pre-persistence finding from a heuristic detector.

    The detector orchestration converts these into
    ``InferredLifeEventFinding`` ORM rows after the conflict resolver
    + continuity check pass.
    """

    pattern: Pattern
    heuristic_confidence: HeuristicConfidence
    evidence_window_start: date
    evidence_window_end: date
    evidence_transaction_ids: list[int]
    evidence_summary: str
    #: Stable counterparty key used by the conflict resolver to detect
    #: re-categorisation.  Lowercased + de-duped concatenation of the
    #: matched merchant strings.  ``None`` for heuristics that don't
    #: have a stable counterparty surface (e.g. wedding_scale_transfer
    #: where the counterparty is by definition one-off).
    counterparty_key: str | None = None
    #: Forward-looking flags used by the LLM-disambiguator seam.
    needs_llm_disambiguation: bool = False
    #: Carried through to the ORM row.  Filled by the conflict
    #: resolver pass.
    conflict_resolution: str | None = None


@dataclass
class DetectorSummary:
    """Top-level summary returned by ``run_detector``.

    Mirrors the shape that ``CadenceLoop.tick`` returns into the
    JobRegistry's last-output cache (admin UI surfaces it).
    """

    findings_total: int = 0
    findings_proposed: int = 0
    findings_shadow: int = 0
    findings_dismissed: int = 0
    conflicts_resolved: int = 0
    proposer_calls: int = 0
    shadow_mode: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings_total": self.findings_total,
            "findings_proposed": self.findings_proposed,
            "findings_shadow": self.findings_shadow,
            "findings_dismissed": self.findings_dismissed,
            "conflicts_resolved": self.conflicts_resolved,
            "proposer_calls": self.proposer_calls,
            "shadow_mode": self.shadow_mode,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Heuristic helpers
# ---------------------------------------------------------------------------


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    """Return True iff ``text`` (already lower-cased) contains any
    of the substring patterns."""
    return any(p in text for p in patterns)


def _tx_label_text(tx: ExpenseTransaction) -> str:
    """Lower-cased concatenation of the merchant fields used for
    label-pattern matching."""
    raw = (tx.merchant_raw or "").lower()
    norm = (tx.merchant_normalized or "").lower()
    return f"{raw} {norm}"


def _load_transaction_window(
    session: "Session",
    user_id: str,
    *,
    window_start: date,
    window_end: date,
) -> list[ExpenseTransaction]:
    """Load expense transactions in [window_start, window_end] for
    user, ordered by ``occurred_on`` ascending."""
    stmt = (
        sa.select(ExpenseTransaction)
        .where(ExpenseTransaction.user_id == user_id)
        .where(ExpenseTransaction.occurred_on >= window_start)
        .where(ExpenseTransaction.occurred_on <= window_end)
        .order_by(ExpenseTransaction.occurred_on.asc())
    )
    return list(session.execute(stmt).scalars().all())


def _month_key(d: date) -> tuple[int, int]:
    """Return (year, month) tuple for grouping by calendar month."""
    return (d.year, d.month)


def _months_between(a: date, b: date) -> int:
    """Approximate calendar-month count between two dates (a <= b)."""
    return (b.year - a.year) * 12 + (b.month - a.month)


# ---------------------------------------------------------------------------
# Heuristic #1 — tuition_stopped
# ---------------------------------------------------------------------------


def _detect_tuition_stopped(
    transactions: list[ExpenseTransaction],
    *,
    window_start: date,
    window_end: date,
) -> list[HeuristicFinding]:
    """Detect TUITION-shaped recurring payment that stopped.

    Pattern (spec §5.3 row 1):
      * Recurring monthly debit (amount >= NIS 3000) to a label matching
        the tuition pattern set.
      * Ran for >= TUITION_PRIOR_MONTHS_MIN months.
      * ABSENT for >= TUITION_GAP_MONTHS_MIN months ending at
        ``window_end``.

    Returns one finding per distinct counterparty key that satisfied
    the recurring + gap criteria.  Confidence:
      * ``high`` if prior_count >= 12 AND gap_months >= 6.
      * ``medium`` otherwise (but still meeting the minimum
        thresholds — anything below the minimums yields no finding).
    """
    findings: list[HeuristicFinding] = []
    tuition_txs = [
        tx
        for tx in transactions
        if tx.direction == "debit"
        and tx.amount_nis is not None
        and tx.amount_nis >= Decimal("3000")
        and _matches_any(_tx_label_text(tx), _TUITION_LABEL_PATTERNS)
    ]
    if not tuition_txs:
        return findings

    # Group by counterparty key (normalised merchant string).
    by_cp: dict[str, list[ExpenseTransaction]] = defaultdict(list)
    for tx in tuition_txs:
        cp_key = (tx.merchant_normalized or tx.merchant_raw or "").lower()
        by_cp[cp_key].append(tx)

    for cp_key, txs in by_cp.items():
        if not txs:
            continue
        first = txs[0].occurred_on
        last = txs[-1].occurred_on
        # How many distinct calendar months are covered?
        month_set = {_month_key(tx.occurred_on) for tx in txs}
        prior_months = len(month_set)
        # Gap from last tuition payment to window_end.
        gap_months = _months_between(last, window_end)
        if (
            prior_months < TUITION_PRIOR_MONTHS_MIN
            or gap_months < TUITION_GAP_MONTHS_MIN
        ):
            continue
        if prior_months >= 12 and gap_months >= 6:
            confidence: HeuristicConfidence = "high"
        else:
            confidence = "medium"
        findings.append(
            HeuristicFinding(
                pattern="tuition_stopped",
                heuristic_confidence=confidence,
                evidence_window_start=first,
                # The window's end is the LAST tuition payment date,
                # NOT ``window_end`` — the continuity check + conflict
                # resolver both look "after the stream ended" using
                # this field.  Using window_end here would put both
                # bounds on the same calendar day on every run and
                # make the secondary guardrails no-op.
                evidence_window_end=last,
                evidence_transaction_ids=[int(tx.id) for tx in txs],
                evidence_summary=(
                    f"{prior_months} months of tuition-shaped payments to "
                    f"{cp_key!r} ended {gap_months} months ago "
                    f"(last on {last.isoformat()})."
                ),
                counterparty_key=cp_key,
                needs_llm_disambiguation=(confidence != "high"),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Heuristic #2 — recurring_large_auto / recurring_car_purchase
# ---------------------------------------------------------------------------


def _detect_recurring_large_auto(
    transactions: list[ExpenseTransaction],
    *,
    window_start: date,
    window_end: date,
) -> list[HeuristicFinding]:
    """Detect CAR-scale transaction on a stable ~5y cadence.

    Pattern (spec §5.3 row 2):
      * Single debit >= NIS 60k to AUTO/CAR/DEALER label.
      * >= 2 prior occurrences (so >= 3 total to compute stdev).
      * Inter-arrival stdev < 1y -> high; 1y..2y -> medium; > 2y -> no
        finding (the cadence is too noisy to call "recurring").

    The finding represents the RECURRING-purchase pattern, not the
    most-recent purchase itself.
    """
    findings: list[HeuristicFinding] = []
    car_txs = [
        tx
        for tx in transactions
        if tx.direction == "debit"
        and tx.amount_nis is not None
        and tx.amount_nis >= CAR_AMOUNT_THRESHOLD_NIS
        and _matches_any(_tx_label_text(tx), _CAR_LABEL_PATTERNS)
    ]
    if len(car_txs) < CAR_PRIOR_COUNT_MIN + 1:
        return findings

    # Inter-arrival times in days.
    dates_sorted = sorted(tx.occurred_on for tx in car_txs)
    deltas_days = [
        (dates_sorted[i] - dates_sorted[i - 1]).days
        for i in range(1, len(dates_sorted))
    ]
    if not deltas_days:
        return findings
    stdev_days = (
        statistics.stdev(deltas_days) if len(deltas_days) > 1 else 0.0
    )
    if stdev_days >= CAR_INTERARRIVAL_STDEV_MEDIUM_DAYS:
        return findings
    confidence: HeuristicConfidence = (
        "high" if stdev_days < CAR_INTERARRIVAL_STDEV_HIGH_DAYS else "medium"
    )
    findings.append(
        HeuristicFinding(
            pattern="recurring_car_purchase",
            heuristic_confidence=confidence,
            evidence_window_start=dates_sorted[0],
            evidence_window_end=dates_sorted[-1],
            evidence_transaction_ids=[int(tx.id) for tx in car_txs],
            evidence_summary=(
                f"{len(car_txs)} car-scale purchases ({CAR_AMOUNT_THRESHOLD_NIS}"
                f"+ NIS, AUTO/CAR/DEALER labels) on a "
                f"{stdev_days:.0f}-day-stdev cadence."
            ),
            counterparty_key=None,
            needs_llm_disambiguation=(confidence != "high"),
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Heuristic #3 — wedding_scale_transfer
# ---------------------------------------------------------------------------


def _detect_wedding_scale_transfer(
    transactions: list[ExpenseTransaction],
    *,
    window_start: date,
    window_end: date,
) -> list[HeuristicFinding]:
    """Detect a single >= NIS 100k transfer with wedding-shaped label.

    Pattern (spec §5.3 row 3):
      * Single debit >= NIS 100k (strict floor; no low-confidence
        variant — codex BLOCKER #1 from spec-E-5 review).
      * Counterparty matches wedding/gift/marriage label OR memo string.
      * Confidence: ``medium`` always (per spec — "always ambiguous").
    """
    findings: list[HeuristicFinding] = []
    for tx in transactions:
        if tx.direction != "debit":
            continue
        if tx.amount_nis is None:
            continue
        # Spec §5.3 row 3 — strict NIS 100k floor (codex BLOCKER #1
        # from the spec-E-5 review: any lower threshold widens the
        # false-positive surface without adding signal).
        if tx.amount_nis < WEDDING_AMOUNT_THRESHOLD_NIS:
            continue
        if not _matches_any(_tx_label_text(tx), _WEDDING_LABEL_PATTERNS):
            continue
        # Spec §5.3 "medium always" — wedding-shape is inherently
        # ambiguous (could be a gift, an investment, a one-off
        # purchase from a venue that also rents office space, etc.),
        # so the heuristic never reaches `high` regardless of amount.
        confidence: HeuristicConfidence = "medium"
        findings.append(
            HeuristicFinding(
                pattern="wedding_scale_transfer",
                heuristic_confidence=confidence,
                evidence_window_start=tx.occurred_on,
                evidence_window_end=tx.occurred_on,
                evidence_transaction_ids=[int(tx.id)],
                evidence_summary=(
                    f"Single transfer of {tx.amount_nis} NIS to "
                    f"{tx.merchant_normalized or tx.merchant_raw!r} on "
                    f"{tx.occurred_on.isoformat()} matches wedding-scale "
                    f"label pattern."
                ),
                counterparty_key=None,  # wedding counterparty is one-off
                needs_llm_disambiguation=True,  # always (medium/low)
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Heuristic #4 — recurring_renovation
# ---------------------------------------------------------------------------


def _detect_recurring_renovation(
    transactions: list[ExpenseTransaction],
    *,
    window_start: date,
    window_end: date,
) -> list[HeuristicFinding]:
    """Detect a renovation-cluster (>= 3 tx within 90d, >= NIS 50k).

    Pattern (spec §5.3 row 4):
      * Cluster of 3+ construction/renovation-label transactions
        within a 90-day window summing to >= NIS 50k.
      * Confidence: ``medium`` always (per spec).
    """
    findings: list[HeuristicFinding] = []
    renov_txs = [
        tx
        for tx in transactions
        if tx.direction == "debit"
        and tx.amount_nis is not None
        and _matches_any(_tx_label_text(tx), _RENOVATION_LABEL_PATTERNS)
    ]
    if len(renov_txs) < RENOVATION_COUNT_MIN:
        return findings
    # Greedy cluster pass — walk renov_txs sorted by date; for each
    # starting tx, sweep forward to find others within 90d.
    renov_txs.sort(key=lambda tx: tx.occurred_on)
    used: set[int] = set()
    for i, anchor in enumerate(renov_txs):
        if int(anchor.id) in used:
            continue
        cluster: list[ExpenseTransaction] = [anchor]
        cluster_end = anchor.occurred_on + timedelta(
            days=RENOVATION_WINDOW_DAYS
        )
        for j in range(i + 1, len(renov_txs)):
            tx = renov_txs[j]
            if int(tx.id) in used:
                continue
            if tx.occurred_on > cluster_end:
                break
            cluster.append(tx)
        total = sum((tx.amount_nis or Decimal(0)) for tx in cluster)
        if (
            len(cluster) >= RENOVATION_COUNT_MIN
            and total >= RENOVATION_TOTAL_THRESHOLD_NIS
        ):
            for tx in cluster:
                used.add(int(tx.id))
            findings.append(
                HeuristicFinding(
                    pattern="recurring_renovation",
                    heuristic_confidence="medium",
                    evidence_window_start=cluster[0].occurred_on,
                    evidence_window_end=cluster[-1].occurred_on,
                    evidence_transaction_ids=[
                        int(tx.id) for tx in cluster
                    ],
                    evidence_summary=(
                        f"{len(cluster)} construction-label transactions "
                        f"totalling {total} NIS within "
                        f"{(cluster[-1].occurred_on - cluster[0].occurred_on).days}"
                        f" days."
                    ),
                    counterparty_key=None,
                    needs_llm_disambiguation=True,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Heuristic #5 — kid_started_college (inverse of tuition_stopped)
# ---------------------------------------------------------------------------


def _detect_kid_started_college(
    transactions: list[ExpenseTransaction],
    *,
    window_start: date,
    window_end: date,
) -> list[HeuristicFinding]:
    """Detect SUDDEN APPEARANCE of tuition-shaped recurring payment.

    Pattern (spec §5.3 row 5):
      * Counterparty with no tuition-shaped payments in the first
        ``COLLEGE_ABSENCE_MONTHS_MIN`` months of the window.
      * Then >= ``COLLEGE_PRESENCE_MONTHS_MIN`` months of recurring
        tuition-shaped payments ending at ``window_end``.
      * Confidence: ``high`` if absence >= 6mo + presence >= 3mo;
        ``medium`` otherwise.
    """
    findings: list[HeuristicFinding] = []
    # Codex BLOCKER #2 from spec-E-5 review: use the STRICT
    # college-only label set here (university / college / academy).
    # The shared tuition pattern set includes "kindergarten" / "school"
    # which legitimately match tuition_stopped (an existing
    # kindergarten enrollment ending IS a phase change) but a NEW
    # kindergarten enrollment is NOT "kid_started_college" — it's a
    # toddler aging into kindergarten, which the user logs manually
    # if at all.  The stricter set narrows the false-positive surface.
    college_txs = [
        tx
        for tx in transactions
        if tx.direction == "debit"
        and tx.amount_nis is not None
        and tx.amount_nis >= Decimal("3000")
        and _matches_any(_tx_label_text(tx), _COLLEGE_ONLY_LABEL_PATTERNS)
    ]
    if not college_txs:
        return findings

    # Sort + group by counterparty.
    by_cp: dict[str, list[ExpenseTransaction]] = defaultdict(list)
    for tx in college_txs:
        cp_key = (tx.merchant_normalized or tx.merchant_raw or "").lower()
        by_cp[cp_key].append(tx)

    for cp_key, txs in by_cp.items():
        if not txs:
            continue
        txs.sort(key=lambda t: t.occurred_on)
        first = txs[0].occurred_on
        # Absence months: from window_start to first appearance.
        absence_months = _months_between(window_start, first)
        # Presence months: distinct calendar months in the recent run.
        month_set = {_month_key(tx.occurred_on) for tx in txs}
        presence_months = len(month_set)
        if (
            absence_months < COLLEGE_ABSENCE_MONTHS_MIN
            or presence_months < COLLEGE_PRESENCE_MONTHS_MIN
        ):
            continue
        if (
            absence_months >= COLLEGE_ABSENCE_MONTHS_MIN
            and presence_months >= COLLEGE_PRESENCE_MONTHS_MIN
        ):
            confidence: HeuristicConfidence = "high"
        else:
            confidence = "medium"
        findings.append(
            HeuristicFinding(
                pattern="kid_started_college",
                heuristic_confidence=confidence,
                evidence_window_start=first,
                evidence_window_end=window_end,
                evidence_transaction_ids=[int(tx.id) for tx in txs],
                evidence_summary=(
                    f"{absence_months} months of zero tuition payments to "
                    f"{cp_key!r} followed by {presence_months} months of "
                    f"recurring payments (first on {first.isoformat()})."
                ),
                counterparty_key=cp_key,
                needs_llm_disambiguation=(confidence != "high"),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Heuristic #6 — partner_change (v1.1 stub)
# ---------------------------------------------------------------------------


def _detect_partner_change(
    transactions: list[ExpenseTransaction],
    *,
    window_start: date,
    window_end: date,
) -> list[HeuristicFinding]:
    """Partner-change detection — deferred to v1.1 per spec §5.1.

    The shape (new co-named beneficiaries / joint accounts) requires
    counterparty-graph state Argosy doesn't yet build.  Returning an
    empty list keeps the heuristic registry complete + future-extends
    without a structural change.
    """
    return []


# ---------------------------------------------------------------------------
# Pre-proposal conflict resolver (codex BLOCKER from spec §5.4)
# ---------------------------------------------------------------------------


def _run_pre_proposal_conflict_resolver(
    findings: list[HeuristicFinding],
    *,
    prior_findings: list[HeuristicFinding] | None = None,
) -> int:
    """Detect tuition-family aliased-pair conflicts.

    Codex BLOCKER from spec §5.4 guardrail #5: when ``tuition_stopped``
    + ``kid_started_college`` fire on the SAME counterparty within
    overlapping evidence windows, the underlying signal is a
    re-categorisation, NOT a life event.  This pass:

    * Walks every (a, b) pair in the cross-product of
      ``findings + prior_findings``.
    * For pairs that match ``_CONFLICT_PAIR_PATTERNS`` AND share a
      counterparty key AND have overlapping windows (within
      ``CONFLICT_PAIR_OVERLAP_DAYS``):
        - Both findings are SUPPRESSED (``dismissed=True``,
          ``conflict_resolution='aliased_pair_suppressed'``).
    * For pairs without shared counterparty but overlapping windows:
        - Both findings are marked
          ``conflict_resolution='aliased_pair_disambiguator_required'``
          + ``needs_llm_disambiguation=True``.  The LLM decides whether
          the pair is a single re-categorised event or two distinct.

    Returns the number of findings whose ``conflict_resolution`` was
    SET by this pass (regardless of suppression vs LLM-required).
    """
    if not findings:
        return 0
    prior_findings = prior_findings or []
    pool = findings + prior_findings
    resolved_count = 0

    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            pair_kinds = (a.pattern, b.pattern)
            if (
                pair_kinds not in _CONFLICT_PAIR_PATTERNS
                and tuple(reversed(pair_kinds))
                not in _CONFLICT_PAIR_PATTERNS
            ):
                continue
            # Window overlap test — use a fuzzy "within 90d" envelope
            # so a tuition stream that stopped in March and a college
            # stream that started in May still counts as overlapping.
            a_mid = a.evidence_window_end
            b_mid = b.evidence_window_start
            gap = abs((b_mid - a_mid).days)
            if gap > CONFLICT_PAIR_OVERLAP_DAYS:
                continue
            same_cp = (
                a.counterparty_key is not None
                and b.counterparty_key is not None
                and a.counterparty_key == b.counterparty_key
            )
            if same_cp:
                # Structural re-categorisation evidence: same
                # counterparty in both streams.  Suppress both.
                for f in (a, b):
                    if f.conflict_resolution is None:
                        f.conflict_resolution = "aliased_pair_suppressed"
                        # Mutate dismissal flag too — the orchestrator
                        # honours this BEFORE firing the proposer.
                        f.needs_llm_disambiguation = False
                        resolved_count += 1
            else:
                # Overlap without shared counterparty.  Force LLM
                # disambiguation rather than auto-suppressing.
                for f in (a, b):
                    if f.conflict_resolution is None:
                        f.conflict_resolution = (
                            "aliased_pair_disambiguator_required"
                        )
                        f.needs_llm_disambiguation = True
                        resolved_count += 1
    return resolved_count


def _counterparty_continuity_check(
    finding: HeuristicFinding,
    transaction_history: list[ExpenseTransaction],
) -> bool:
    """Secondary guardrail — detect autopay/merchant-switch masquerading.

    If the disappearing counterparty in a ``tuition_stopped`` finding
    re-appears under a DIFFERENT counterparty key within
    ``CONTINUITY_REAPPEARANCE_DAYS`` of the gap start, the heuristic
    confidence is DOWNGRADED to ``low`` (forces the LLM
    disambiguator).  Returns True iff the check actually downgraded.

    Only applies to ``tuition_stopped`` (the only heuristic with a
    natural "disappeared counterparty" interpretation that re-emerges
    under a different label — the auto / wedding / renovation
    patterns don't have an analogous mode).
    """
    if finding.pattern != "tuition_stopped":
        return False
    if finding.counterparty_key is None:
        return False
    if finding.heuristic_confidence == "low":
        # Already at the lowest band — nothing to do.
        return False
    # Look for any tuition-shaped transactions AFTER the finding's
    # window-end that match a DIFFERENT counterparty key.
    gap_start = finding.evidence_window_end
    gap_horizon = gap_start + timedelta(days=CONTINUITY_REAPPEARANCE_DAYS)
    for tx in transaction_history:
        if tx.occurred_on <= gap_start:
            continue
        if tx.occurred_on > gap_horizon:
            continue
        if tx.direction != "debit":
            continue
        if tx.amount_nis is None or tx.amount_nis < Decimal("3000"):
            continue
        if not _matches_any(_tx_label_text(tx), _TUITION_LABEL_PATTERNS):
            continue
        cp_key = (tx.merchant_normalized or tx.merchant_raw or "").lower()
        if cp_key and cp_key != finding.counterparty_key:
            # Different counterparty under same label family —
            # likely autopay re-routed.  Downgrade.
            finding.heuristic_confidence = "low"
            finding.needs_llm_disambiguation = True
            return True
    return False


# ---------------------------------------------------------------------------
# Shadow-mode classifier
# ---------------------------------------------------------------------------


def _resolve_shadow_mode(
    session: "Session",
    user_id: str,
    *,
    now: datetime,
    shadow_mode_override: bool | None,
) -> bool:
    """Resolve effective shadow mode per spec §5.4.

    Precedence:
      1. ``shadow_mode_override`` (test injection / explicit caller).
      2. Otherwise: TRUE iff the user's ``users.created_at`` is within
         ``SHADOW_MODE_NEW_ACCOUNT_DAYS`` of ``now``.
    """
    if shadow_mode_override is not None:
        return bool(shadow_mode_override)
    row = session.execute(
        sa.select(User).where(User.id == user_id)
    ).scalar_one_or_none()
    if row is None:
        return False
    created_at = row.created_at
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    threshold = now - timedelta(days=SHADOW_MODE_NEW_ACCOUNT_DAYS)
    return created_at > threshold


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


#: Registry of v1 heuristic detectors.  Order is stable for the
#: backfill verification commit; future heuristics extend via append.
HEURISTIC_REGISTRY: tuple[
    Callable[..., list[HeuristicFinding]], ...
] = (
    _detect_tuition_stopped,
    _detect_recurring_large_auto,
    _detect_wedding_scale_transfer,
    _detect_recurring_renovation,
    _detect_kid_started_college,
    _detect_partner_change,
)


def run_detector(
    session: "Session",
    user_id: str,
    *,
    lookback_days: int = 365,
    shadow_mode: bool | None = None,
    now: datetime | None = None,
    proposer_runner: Callable[..., Any] | None = None,
) -> DetectorSummary:
    """Run one full detector pass for ``user_id``.

    Args:
      session: live sync SQLAlchemy Session.
      user_id: tenant id.
      lookback_days: rolling-window size in days (default 365 — the
        spec's 12-month window).
      shadow_mode: override the auto-resolved shadow-mode classifier.
        ``None`` (default) lets the resolver decide based on the
        user's account age.
      now: override clock for tests.  Defaults to UTC now.
      proposer_runner: injected async coroutine factory matching the
        signature of
        ``argosy.services.action_proposer_runner.
        run_action_proposer_for_inferred_event``.  Defaults to the
        production runner.  Tests substitute a stub to avoid the LLM.

    Returns:
      A ``DetectorSummary`` capturing the per-run counts.

    Behaviour:
      1. Resolve shadow mode (per spec §5.4 + Ariel's locked decision).
      2. Load the transaction window.
      3. Run all six heuristics in registry order.
      4. Apply the pre-proposal conflict resolver.
      5. Apply the counterparty-continuity check per finding.
      6. Persist each finding to ``inferred_life_event_findings``.
         UNIQUE-violation -> "already processed" no-op.
      7. For each non-dismissed finding (and not in shadow mode):
         fire the action_proposer_runner.  Capture the returned
         proposal id back into the finding's ``proposed_action_id``.
      8. Return the summary.

    Errors per heuristic are caught + logged + appended to
    ``summary.errors`` — one heuristic failing does NOT break the
    batch (mirrors the state_observer flag-writer pattern).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    summary = DetectorSummary()
    summary.shadow_mode = _resolve_shadow_mode(
        session, user_id, now=now, shadow_mode_override=shadow_mode
    )

    window_end = now.date()
    window_start = window_end - timedelta(days=lookback_days)

    try:
        transactions = _load_transaction_window(
            session,
            user_id,
            window_start=window_start,
            window_end=window_end,
        )
    except Exception as exc:  # noqa: BLE001 — never break detector
        _log.warning(
            "inferred_life_event_detector.tx_load_failed",
            extra={"user_id": user_id, "error": str(exc)[:300]},
        )
        summary.errors.append(f"tx_load_failed: {exc!s}"[:200])
        return summary

    # ---- Step 1: run heuristics ---------------------------------
    raw_findings: list[HeuristicFinding] = []
    for heuristic in HEURISTIC_REGISTRY:
        try:
            raw_findings.extend(
                heuristic(
                    transactions,
                    window_start=window_start,
                    window_end=window_end,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "inferred_life_event_detector.heuristic_failed",
                extra={
                    "heuristic": heuristic.__name__,
                    "error": str(exc)[:300],
                },
            )
            summary.errors.append(
                f"{heuristic.__name__}: {exc!s}"[:200]
            )

    summary.findings_total = len(raw_findings)

    # ---- Step 2: pre-proposal conflict resolver -----------------
    try:
        resolved = _run_pre_proposal_conflict_resolver(raw_findings)
        summary.conflicts_resolved = resolved
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "inferred_life_event_detector.conflict_resolver_failed",
            extra={"error": str(exc)[:300]},
        )
        summary.errors.append(f"conflict_resolver: {exc!s}"[:200])

    # ---- Step 3: continuity check per finding -------------------
    for finding in raw_findings:
        try:
            _counterparty_continuity_check(finding, transactions)
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(
                f"continuity_check({finding.pattern}): {exc!s}"[:200]
            )

    # ---- Step 4: persist + fire proposer ------------------------
    import asyncio

    for finding in raw_findings:
        # Determine the row's terminal-state flags BEFORE the INSERT
        # so the audit trail is complete on the first write.
        suppressed_by_conflict = (
            finding.conflict_resolution == "aliased_pair_suppressed"
        )
        is_dismissed = suppressed_by_conflict
        # In shadow mode, EVERY finding is recorded as dismissed (no
        # proposer call) but the conflict_resolution stays
        # `no_conflict` unless the resolver actually fired.  This
        # preserves the audit shape for the 30-day calibration period.
        if summary.shadow_mode:
            is_dismissed = True
            if finding.conflict_resolution is None:
                finding.conflict_resolution = "no_conflict"
        else:
            if finding.conflict_resolution is None:
                finding.conflict_resolution = "no_conflict"

        row = InferredLifeEventFinding(
            user_id=user_id,
            pattern=finding.pattern,
            heuristic_confidence=finding.heuristic_confidence,
            llm_confirmed=None,  # disambiguator seam — wired in §5.2
            dismissed=is_dismissed,
            evidence_window_start=finding.evidence_window_start,
            evidence_window_end=finding.evidence_window_end,
            evidence_transaction_ids=json.dumps(
                finding.evidence_transaction_ids
            ),
            evidence_summary=finding.evidence_summary,
            conflict_resolution=finding.conflict_resolution,
            proposed_action_id=None,
        )
        session.add(row)
        try:
            session.flush()
            session.commit()
        except IntegrityError:
            # Natural-key collision — already-detected for this
            # (user, pattern, window).  Skip + move on.
            session.rollback()
            _log.info(
                "inferred_life_event_detector.duplicate_finding_skip",
                extra={
                    "user_id": user_id,
                    "pattern": finding.pattern,
                    "window_start": finding.evidence_window_start.isoformat(),
                    "window_end": finding.evidence_window_end.isoformat(),
                },
            )
            continue

        if is_dismissed:
            if summary.shadow_mode:
                summary.findings_shadow += 1
            else:
                summary.findings_dismissed += 1
            continue

        # ---- Step 5: fire the proposer for non-dismissed -------
        runner = proposer_runner or _default_proposer_runner
        try:
            proposal = asyncio.run(
                runner(
                    session,
                    inferred_event=row,
                    user_id=user_id,
                )
            )
            # The runner returns RunResult OR a single proposal id
            # depending on the test shim; normalise the binding.
            proposal_id = _extract_proposal_id(proposal)
            if proposal_id is not None:
                row.proposed_action_id = proposal_id
                session.flush()
                session.commit()
            summary.proposer_calls += 1
            summary.findings_proposed += 1
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            _log.warning(
                "inferred_life_event_detector.proposer_call_failed",
                extra={
                    "finding_id": row.id,
                    "pattern": row.pattern,
                    "error": str(exc)[:300],
                },
            )
            summary.errors.append(
                f"proposer({row.pattern}): {exc!s}"[:200]
            )

    return summary


def _default_proposer_runner(
    session: "Session",
    *,
    inferred_event: InferredLifeEventFinding,
    user_id: str,
) -> Any:
    """Production proposer-runner adapter.

    Imported lazily so the detector module can be loaded in test
    contexts that mock the proposer entirely.
    """
    from argosy.services.action_proposer_runner import (
        run_action_proposer_for_inferred_event,
    )

    return run_action_proposer_for_inferred_event(
        session,
        inferred_event=inferred_event,
        user_id=user_id,
    )


def _extract_proposal_id(result: Any) -> int | None:
    """Pull a proposal id out of whichever shape the runner returned.

    Production: ``RunResult`` with ``.proposals`` list of ORM rows.
    Test stubs: plain int OR None.  Defensive against both.
    """
    if result is None:
        return None
    if isinstance(result, int):
        return result
    # RunResult duck-type — first proposal's id.
    proposals = getattr(result, "proposals", None)
    if proposals:
        first = proposals[0]
        pid = getattr(first, "id", None)
        if isinstance(pid, int):
            return pid
    return None


__all__ = [
    "CAR_AMOUNT_THRESHOLD_NIS",
    "CAR_PRIOR_COUNT_MIN",
    "COLLEGE_ABSENCE_MONTHS_MIN",
    "CONFLICT_PAIR_OVERLAP_DAYS",
    "CONTINUITY_REAPPEARANCE_DAYS",
    "DetectorSummary",
    "HEURISTIC_REGISTRY",
    "HeuristicFinding",
    "RENOVATION_COUNT_MIN",
    "RENOVATION_TOTAL_THRESHOLD_NIS",
    "RENOVATION_WINDOW_DAYS",
    "SHADOW_MODE_NEW_ACCOUNT_DAYS",
    "TUITION_GAP_MONTHS_MIN",
    "TUITION_PRIOR_MONTHS_MIN",
    "WEDDING_AMOUNT_THRESHOLD_NIS",
    "_counterparty_continuity_check",
    "_detect_kid_started_college",
    "_detect_partner_change",
    "_detect_recurring_large_auto",
    "_detect_recurring_renovation",
    "_detect_tuition_stopped",
    "_detect_wedding_scale_transfer",
    "_resolve_shadow_mode",
    "_run_pre_proposal_conflict_resolver",
    "run_detector",
]
