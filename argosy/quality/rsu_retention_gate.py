"""RSU net-retention consistency gate (run-106 finding [3]).

The RSU/equity-comp NET retention rate — the share of a vest the household
keeps AFTER tax — is the multiplier that turns a gross vest into deployable
cash. Run-106 reported it three incompatible ways at once: the RSU ledger said
47%, the equity-comp evidence said 65%. A divergence is not cosmetic: it
changes the after-tax cash the plan can actually deploy.

No single agent owns the cross-surface agreement of this rate (the ledger, the
equity-comp evidence, and the prose are produced separately), so it is checked
deterministically here: scan the plan text for retention percentages stated in
an RSU/equity-comp context, and if two or more DISTINCT values (> 1 pp apart)
appear, flag the contradiction.

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
fx_gate / coherence_gate convention. The retention figure must be coupled to a
QUALIFIED equity-comp cue ("net retention", "after-tax retention", "RSU
retention", "equity-comp retention", "net-of-tax retention") — the bare verbs
"retain"/"retained"/"retains" are intentionally NOT cues, because they match
ordinary allocation/performance prose ("retain the position at 13%", "retained
40% of gains") and would over-block legitimate plans.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Distinct-value tolerance in percentage points. Within 1 pp is rounding (64 vs
# 65), not a contradiction. A rounding band, not a financial constant.
_RETENTION_TOLERANCE_PP = 1.0

# A retention percentage stated in an RSU / equity-comp / after-tax context.
# The MATCH must couple an EQUITY-COMP retention cue to the percentage so we only
# pick up retention-of-equity-comp figures, never an unrelated "NVDA is 47%".
#
# WHY only QUALIFIED "retention": the bare verbs "retain"/"retained"/"retains"
# (and the bare "retention" noun) match ordinary allocation/performance prose —
# "retain the NVDA position at a 13% cap", "the fund retained 40% of gains" —
# and would spuriously collect those unrelated percentages, over-blocking real
# plans. The retention figure is only an equity-comp net-retention figure when
# the "retention" noun is immediately qualified by net / after-tax / RSU /
# equity-comp / net-of-tax. So the cue requires that qualifier and the bare verb
# forms are intentionally dropped.
#
# Both orders are covered as small named alternatives so each is auditable:
#   (a) cue-then-number:  "net retention of 47%", "after-tax retention 65%",
#                          "net-of-tax retention 64%", "RSU retention reads 65%"
#   (b) number-then-cue:  "65% net retention", "47% net-of-tax retention"
# A ~24-char window keeps the number bound to its retention cue, not a later
# figure in the same sentence.
_RETAIN_CUE = (
    r"(?:"
    r"net[\s-]*(?:of[\s-]*tax[\s-]*)?retention"   # "net retention", "net-of-tax retention"
    r"|after[\s-]*tax retention"                   # "after-tax retention"
    r"|rsu retention"                              # "RSU retention"
    r"|equity[\s-]*comp(?:ensation)? retention"    # "equity-comp(ensation) retention"
    r")"
)
_RETENTION_PCT_RE = re.compile(
    r"(?:"
    r"(?P<cue_first>" + _RETAIN_CUE + r")[^.!?\n]{0,24}?(?P<num1>\d{1,3}(?:\.\d+)?)\s*%"  # cue → number
    r"|"
    r"(?P<num2>\d{1,3}(?:\.\d+)?)\s*%[^.!?\n]{0,24}?(?P<cue_last>" + _RETAIN_CUE + r")"  # number → cue
    r")",
    re.IGNORECASE,
)


def check_rsu_retention_consistency(*, plan_text: str) -> list[GateViolation]:
    """Flag divergent RSU/equity-comp NET-retention percentages in the plan text.

    Scans ``plan_text`` for retention percentages stated in an RSU/equity-comp/
    after-tax context (anchored on a QUALIFIED "net/after-tax/RSU/equity-comp
    retention" cue, so an unrelated 47% NVDA weight, a "retain ... at 13%" cap,
    or a "retained 40% of gains" figure are NOT picked up). Collects the distinct
    values found; if two
    or more differ by more than 1 percentage point, the retention rate is being
    reported inconsistently — a contradiction that changes the after-tax cash the
    plan can deploy — and a single ``RSU_RETENTION_CONSISTENCY`` violation listing
    the divergent values is emitted.
    """
    text = plan_text or ""
    values: list[float] = []
    for m in _RETENTION_PCT_RE.finditer(text):
        raw = m.group("num1") or m.group("num2")
        if raw is None:
            continue
        pct = float(raw)
        # A retention share is a fraction of a vest; >100% is not a retention.
        if 0.0 < pct <= 100.0:
            values.append(pct)

    if not values:
        return []

    # Distinct = differs from an already-seen value by more than the tolerance.
    distinct: list[float] = []
    for v in values:
        if not any(abs(v - d) <= _RETENTION_TOLERANCE_PP for d in distinct):
            distinct.append(v)

    if len(distinct) < 2:
        return []

    listing = ", ".join(f"{v:g}%" for v in sorted(distinct))
    return [
        GateViolation(
            check=GateCheck.RSU_RETENTION_CONSISTENCY,
            detail=(
                f"RSU/equity-comp NET retention is reported with divergent values "
                f"({listing}) more than {_RETENTION_TOLERANCE_PP:g} pp apart. The "
                "retention rate must agree across the RSU ledger, the equity-comp "
                "evidence, and the prose — it sets the after-tax cash the plan can "
                "deploy. Bind every surface to one derived retention rate."
            ),
            locator="rsu_net_retention",
        )
    ]
