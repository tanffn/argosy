"""Retirement-age-label gate — run-106 finding [2].

The resolver DELIBERATELY distinguishes two retirement ages:
  - ``earliest_safe_age`` — the HEADLINE age the user reads as "you can retire".
  - ``fi_age``            — the FIRE-bridge SIZING age the bridge sleeve funds
                            from.
They are NOT meant to be equal. So the invariant is NOT "the two ages match".
It is:
  (a) each age is LABELED BY ITS DEFINITION everywhere it appears, AND
  (b) the bridge sleeve is sized from the resolver's CHOSEN sizing age (today
      that is ``fi_age``): ``bridge_start_age == fi_age``.

The run-106 defect: headline 46 / ``fi_age`` 46, but the bridge sleeve was
sized from age 47 to 60 — silently dropping one year of bridge funding vs the
prior plan (46→60). That is a real regression even though the two ages "look
fine" individually; no per-surface specialist owns the bridge-vs-sizing-age
composition, so it is gated deterministically here.

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
fx_gate / coherence_gate convention.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Two ages within this many years of each other are the SAME age once rounded
# (47.0 vs 47); a rounding band, not a financial constant.
_AGE_ROUNDING_TOL = 0.5

# An age stated as the HEADLINE retirement age. The defining label
# ("earliest-safe", "earliest safe", "headline") immediately precedes a bare
# "retirement age N" / "retire at N" phrasing → this age IS labeled by its
# definition and must NOT be read as an unlabeled contradiction.
# WHY a separate "labeled" regex: we want to find UNLABELED retirement ages, so
# we first locate every retirement-age mention, then exclude the labeled ones.
_LABELED_RETIREMENT_AGE_RE = re.compile(
    r"(?:earliest[\s-]*safe|headline|deterministic)"  # the defining qualifier
    r"[^.!?]{0,30}?"                                   # up to ~30 chars of connective prose
    r"(?:retirement age|retire(?:ment)?(?:\s+at)?)"    # the age concept
    r"[^0-9]{0,12}?"
    r"(\d{2})",                                         # the two-digit age
    re.IGNORECASE,
)
# Any retirement-age mention at all (labeled or not). Used to detect UNLABELED
# ages: a match here that is NOT also a labeled match is a bare "retirement age
# N" the reader cannot disambiguate.
_ANY_RETIREMENT_AGE_RE = re.compile(
    r"(?:retirement age|retire(?:ment)?\s+at)"
    r"[^0-9]{0,12}?"
    r"(\d{2})",
    re.IGNORECASE,
)
# An age stated as the FI-BRIDGE SIZING age. The defining label ("FI-bridge
# sizing", "FIRE-bridge sizing", "bridge sizing", "FI age") precedes the age →
# labeled by its definition.
_LABELED_SIZING_AGE_RE = re.compile(
    r"(?:fi[\s-]*bridge|fire[\s-]*bridge|bridge)\s*sizing"  # "FI-bridge sizing"
    r"[^0-9]{0,18}?(\d{2})"
    r"|"
    r"\bfi\s*age\b[^0-9]{0,12}?(\d{2})",                    # "FI age 47"
    re.IGNORECASE,
)
# A bridge-sleeve FUNDING span "from age N (to M)". This is the age the bridge
# is actually SIZED FROM in the prose; it must equal the sizing age. Used both
# to read the prose-stated bridge start and to detect an UNLABELED start age
# that contradicts an unlabeled headline retirement age.
_BRIDGE_FROM_AGE_RE = re.compile(
    r"bridge[^.!?]{0,40}?from age\s*(\d{2})"   # "bridge ... from age 47"
    r"|from age\s*(\d{2})[^.!?]{0,40}?bridge",  # "from age 47 ... bridge"
    re.IGNORECASE,
)


def _round_eq(a: float, b: float) -> bool:
    return abs(a - b) <= _AGE_ROUNDING_TOL


def check_retirement_age_labels(
    *,
    plan_text: str,
    earliest_safe_age: int | float | None = None,
    fi_age: int | float | None = None,
    bridge_start_age: int | float | None = None,
) -> list[GateViolation]:
    """Flag a bridge sized from the wrong age, or unlabeled contradictory ages.

    Inputs: the rendered ``plan_text`` plus the resolver's three ages — the
    headline ``earliest_safe_age``, the FIRE-bridge ``fi_age`` (the chosen
    sizing age), and the ``bridge_start_age`` the sleeve is actually sized from.

    Two invariants (NOT forced equality of the two headline/sizing ages):
      1. ``bridge_start_age`` provided and != ``fi_age`` (within rounding) → the
         bridge is sized from an age other than the resolver's chosen sizing age
         (the run-106 defect: headline 46 / fi_age 46 but bridge sized from 47).
      2. Two DIFFERENT ages stated in prose without distinguishing labels — an
         unlabeled "retirement age 46" alongside an unlabeled "bridge from age
         47" reads as a contradiction. Biased to FALSE-POSITIVE per the
         fail-loud doctrine.
    """
    violations: list[GateViolation] = []
    text = plan_text or ""

    # --- (1) bridge must be sized from the resolver's CHOSEN sizing age -------
    if bridge_start_age is not None and fi_age is not None and not _round_eq(
        float(bridge_start_age), float(fi_age)
    ):
        violations.append(
            GateViolation(
                check=GateCheck.RETIREMENT_AGE_LABEL,
                detail=(
                    f"FIRE-bridge sleeve is sized from age {bridge_start_age}, but the "
                    f"resolver's chosen sizing age (fi_age) is {fi_age}. The bridge must "
                    f"be sized from the chosen sizing age; sizing it from "
                    f"{bridge_start_age} silently drops "
                    f"{abs(float(bridge_start_age) - float(fi_age)):g} year(s) of bridge "
                    "funding vs the prior plan (run-106 finding [2])."
                ),
                locator="bridge_start_age",
            )
        )

    # --- (2) unlabeled, contradictory ages in prose --------------------------
    # Collect prose-stated ages by labeled-ness. A retirement-age mention that
    # is NOT covered by the labeled regex is an UNLABELED headline age.
    labeled_ret_spans = [m.span() for m in _LABELED_RETIREMENT_AGE_RE.finditer(text)]
    unlabeled_ret_ages: list[int] = []
    for m in _ANY_RETIREMENT_AGE_RE.finditer(text):
        # Was this mention captured by a labeled match (overlapping span)?
        labeled = any(
            not (m.end() <= ls or m.start() >= le) for ls, le in labeled_ret_spans
        )
        if not labeled:
            unlabeled_ret_ages.append(int(m.group(1)))

    # A bridge-from-age span is "labeled" if the prose ties it to the sizing
    # age (the labeled sizing regex matched) — otherwise the bridge start is a
    # bare age the reader cannot reconcile against the unlabeled retirement age.
    has_sizing_label = _LABELED_SIZING_AGE_RE.search(text) is not None
    bridge_from_ages: list[int] = []
    for m in _BRIDGE_FROM_AGE_RE.finditer(text):
        age = m.group(1) or m.group(2)
        if age is not None:
            bridge_from_ages.append(int(age))

    if unlabeled_ret_ages and bridge_from_ages and not has_sizing_label:
        for ret_age in unlabeled_ret_ages:
            for bridge_age in bridge_from_ages:
                if ret_age != bridge_age:
                    violations.append(
                        GateViolation(
                            check=GateCheck.RETIREMENT_AGE_LABEL,
                            detail=(
                                f"prose states an unlabeled retirement age {ret_age} "
                                f"alongside a bridge funded from age {bridge_age} with no "
                                "distinguishing label. Two different ages stated without "
                                "their defining labels (earliest-safe vs FI-bridge sizing) "
                                "read as a contradiction; label each by its definition."
                            ),
                            locator="retirement_age_prose",
                        )
                    )
                    # One contradiction is enough to establish the class.
                    return violations

    return violations
