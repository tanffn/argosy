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
fx_gate / coherence_gate convention. Per Argosy's fail-loud doctrine this biases
toward FALSE-POSITIVE — anchoring on "retention"/"retain" keeps unrelated
percentages (NVDA weight, sleeve targets) out, but a borderline match is flagged
rather than silently dropped.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Distinct-value tolerance in percentage points. Within 1 pp is rounding (64 vs
# 65), not a contradiction. A rounding band, not a financial constant.
_RETENTION_TOLERANCE_PP = 1.0

# A retention percentage stated in an RSU / equity-comp / after-tax context.
# The MATCH must couple a "retention"/"retain" cue to the percentage so we only
# pick up retention-of-equity-comp figures, never an unrelated "NVDA is 47%".
# Both orders are covered as small named alternatives so each is auditable:
#   (a) cue-then-number:  "net retention of 47%", "retain 65% (net)",
#                          "after-tax retention 65%", "net-of-tax retention 64%",
#                          "RSU retention reads 65%"
#   (b) number-then-cue:  "65% net retention", "47% retained after tax"
# A ~24-char window keeps the number bound to its retention cue, not a later
# figure in the same sentence.
_RETAIN_CUE = r"(?:net[\s-]*(?:of[\s-]*tax[\s-]*)?retention|after[\s-]*tax retention|rsu retention|equity[\s-]*comp(?:ensation)? retention|retention|retains?|retained|retain)"
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
    after-tax context (anchored on a "retention"/"retain" cue, so an unrelated
    47% NVDA weight is NOT picked up). Collects the distinct values found; if two
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
