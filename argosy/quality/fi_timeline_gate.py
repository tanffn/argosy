"""FI-timeline coherence gate (run-106 finding [1]).

The same FI-crossing concept must not be reported as BOTH "already crossed /
reached today / now" AND a FUTURE FI age / remaining-years (e.g. "FI age 47",
"FI at age 45", "2.0 years remaining to FI"). Those are mutually exclusive
states of one timeline: either FI is behind you (crossed) or it is ahead of you
(a future age / years remaining), not both.

The S22/S23 distinct-FI-age-label rule means two DIFFERENT FI ages are allowed
when each carries its defining label (e.g. "deterministic FI age",
"Typical-scenario FI age") — distinct-by-label ages are not the contradiction.
The contradiction is the UNQUALIFIED "crossed today / reached now" claim
co-existing in the artifact with a future-FI-age / N-years-remaining statement.

Per Argosy's fail-loud doctrine this biases toward FALSE-POSITIVE: if a
"crossed today" claim and a future-FI statement both appear anywhere in the
artifact, we flag it (a spurious flag is safer than a missed contradiction).

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
fx_gate / coherence_gate convention.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# An UNQUALIFIED assertion that the FI crossing is in the PAST/PRESENT — FI is
# already behind us. Small named alternatives, each auditable:
#   - "FI ... already crossed / crossed today / crossed now"
#   - "(financial independence) reached today / reached now / already reached"
#   - "crossed the FI line today/now/already"
# A "today"/"now"/"already" anchor is required so a generic future "you will
# cross FI" is NOT caught — only a present/past crossing claim.
_CROSSED_TODAY_RE = re.compile(
    r"(?:"
    r"(?:fi|financial independence)[^.!?]{0,40}"
    r"(?:already\s+(?:crossed|reached)|"
    r"(?:crossed|reached)[^.!?]{0,20}(?:today|now|already))"
    r"|(?:already\s+(?:crossed|reached)|(?:crossed|reached)[^.!?]{0,20}(?:today|now|already))"
    r"[^.!?]{0,40}(?:fi|financial independence)"
    r"|crossed[^.!?]{0,20}(?:fi|financial independence)[^.!?]{0,20}(?:line\s+)?(?:today|now|already)"
    r")",
    re.IGNORECASE,
)

# A FUTURE FI statement: a specific FI age, OR remaining years to FI. This is
# the "FI is ahead of you" side of the contradiction.
#   - "FI age (is) 47" / "FI age of 45" / "FI at age 45"
#   - "N years remaining" / "N years to FI" / "N years until FI"
# The numeric token is required so generic "your FI age" prose with no number is
# not the trigger — we need a concrete future-FI claim to contradict "crossed".
_FUTURE_FI_AGE_RE = re.compile(
    r"\bfi\b[^.!?]{0,15}?\bage\b[^.!?]{0,15}?\b\d{2}\b"   # "FI age (is/of) 47"
    r"|\bfi\b[^.!?]{0,10}?\bat\s+age\s+\d{2}\b"            # "FI at age 45"
    r"|\bage\b[^.!?]{0,10}?\bfi\b[^.!?]{0,5}?\d{2}\b",      # "age ... FI 45" (loose)
    re.IGNORECASE,
)
_REMAINING_YEARS_RE = re.compile(
    r"\d+(?:\.\d+)?\s*years?\s+(?:remaining|left|to\s+go|until\s+fi|to\s+fi|to\s+reach\s+fi)"
    r"|\d+(?:\.\d+)?\s*(?:more\s+)?years?\s+(?:remaining|before)[^.!?]{0,20}\bfi\b",
    re.IGNORECASE,
)


def check_fi_timeline_coherence(*, plan_text: str) -> list[GateViolation]:
    """Flag a plan that claims FI is "crossed today/now" while also stating a
    future FI age / years remaining.

    Contract:
      - No "crossed today / reached now / already crossed" claim → ``[]``
        (the distinct-by-label future ages alone are NOT a contradiction).
      - A "crossed today" claim AND a future FI statement (a concrete FI age or
        an "N years remaining to FI") co-existing in the artifact → one
        ``FI_TIMELINE_COHERENCE`` violation. The two are mutually exclusive
        timeline states; the artifact reports FI as both behind AND ahead.

    Document-scoped (not sentence-scoped) on purpose: the run-106 defect spread
    the contradiction across separate sentences ("crossed today" in one, "FI
    age 47" in another). Per the fail-loud doctrine this biases toward
    false-positive — a spurious flag is safer than a missed contradiction.
    """
    text = plan_text or ""

    crossed = _CROSSED_TODAY_RE.search(text)
    if not crossed:
        return []

    future = _FUTURE_FI_AGE_RE.search(text) or _REMAINING_YEARS_RE.search(text)
    if not future:
        return []

    return [
        GateViolation(
            check=GateCheck.FI_TIMELINE_COHERENCE,
            detail=(
                "plan claims FI is already CROSSED today/now "
                f"({crossed.group(0).strip()!r}) yet also states a FUTURE FI "
                f"timeline ({future.group(0).strip()!r}). FI cannot be both "
                "behind you (crossed) and ahead of you (a future age / years "
                "remaining). Reconcile to one timeline, or — if the ages are "
                "deliberately distinct — drop the unqualified 'crossed today' "
                "claim and label each FI age by its definition."
            ),
            locator="fi_timeline",
        )
    ]
