"""finding [6] — stale pending-reviewer-text gate.

A pending fund-manager (FM) objection is a reviewer note attached to the current
artifact. If the objection cites a number for a labeled concept (run-106: "the
medium target is still 3,000 sh/yr") that contradicts the CURRENT draft's value
for the SAME concept (the draft's medium target now reads 5,600 sh/yr), the
objection is STALE: a client sees an unresolved rejection for a defect that has
already been fixed. Flag the contradiction.

Concept matching is anchored on a SHARED CUE so we compare like-with-like: the
phrase "medium target" co-located with a number bearing the "sh/yr" unit. We do
NOT free-match arbitrary numbers — that would compare unrelated quantities.

Per Argosy's fail-loud doctrine this biases toward FALSE-POSITIVE: any
above-rounding divergence between the objection's cited value and the draft's
value for the same anchored concept is flagged.

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
coherence_gate / fx_gate convention.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Sub-rounding band: a difference at or below this many shares/yr is rounding,
# not a real stale-value contradiction. Not a financial constant — a rounding
# guardrail so a 5,600-vs-5,601 restatement does not fire.
_SHARES_ROUNDING_TOL = 1.0

# Anchor the concept on the shared cue "medium target" + a number carrying the
# "sh/yr" unit. We capture the number with optional thousands commas/spaces and
# require the unit token so we only compare the SAME quantity (a share/yr
# accumulation target), never an unrelated figure elsewhere in the prose.
#   - "medium target is still 3,000 sh/yr"
#   - "medium target: 5,600 sh/yr"
#   - "medium target of 5,600 sh/yr"
# The ~25-char window keeps the number bound to its label (skips connectives
# like "is still", ":", "of") without reaching a later sentence.
_MEDIUM_TARGET_SHARES_RE = re.compile(
    r"medium\s+target"          # the concept cue
    r"[^0-9]{0,25}?"            # connectives ("is still", ":", "of"), no digits
    r"(\d[\d,\s]*)"             # the share count (thousands separators allowed)
    r"\s*sh\s*/\s*yr",          # the sh/yr unit, anchoring like-with-like
    re.IGNORECASE,
)


def _first_medium_target_shares(text: str) -> float | None:
    """Return the first "medium target ... N sh/yr" share count in ``text``, or
    None if the anchored concept is absent."""
    m = _MEDIUM_TARGET_SHARES_RE.search(text or "")
    if not m:
        return None
    digits = m.group(1).replace(",", "").replace(" ", "")
    if not digits:
        return None
    return float(digits)


def check_stale_reviewer_text(
    *, plan_text: str, objection_text: str | None = None
) -> list[GateViolation]:
    """Flag a pending FM objection whose cited number contradicts the draft.

    Inputs:
      - ``plan_text``: the CURRENT rendered draft.
      - ``objection_text``: the pending FM objection note attached to the
        artifact (None / empty when there is no pending objection).

    Strategy: anchor on the shared "medium target ... N sh/yr" cue in BOTH the
    objection and the draft; if both express the concept and the numbers differ
    beyond rounding, the objection is stale → one STALE_REVIEWER_TEXT violation.
    Returns ``[]`` when there is no objection, when the concept is absent from
    either side, or when the values agree within rounding.
    """
    if not objection_text:
        return []

    objection_shares = _first_medium_target_shares(objection_text)
    draft_shares = _first_medium_target_shares(plan_text)
    if objection_shares is None or draft_shares is None:
        return []

    if abs(objection_shares - draft_shares) <= _SHARES_ROUNDING_TOL:
        return []

    return [
        GateViolation(
            check=GateCheck.STALE_REVIEWER_TEXT,
            detail=(
                f"pending reviewer (FM) objection cites medium target "
                f"{objection_shares:g} sh/yr, but the current draft's medium "
                f"target reads {draft_shares:g} sh/yr — the objection is STALE "
                "(an unresolved rejection for a value that has since changed). "
                "Re-run or retract the objection before rendering it to the client."
            ),
            locator="medium_target",
        )
    ]
