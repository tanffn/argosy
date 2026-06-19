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
    # The gap must not contain another '%' so the cue binds to the NEAREST
    # percentage, not a distant one across an intervening rate (live pv56:
    # "3% surtax; ~47% net retention" must bind 47%, not the 3% surtax).
    r"(?P<cue_first>" + _RETAIN_CUE + r")[^.!?\n%]{0,24}?(?P<num1>\d{1,3}(?:\.\d+)?)\s*%"  # cue → number
    r"|"
    r"(?P<num2>\d{1,3}(?:\.\d+)?)\s*%[^.!?\n%]{0,24}?(?P<cue_last>" + _RETAIN_CUE + r")"  # number → cue
    r")",
    re.IGNORECASE,
)

# Tax-treatment buckets. The AT-VEST / ordinary-income rate (~47%) and the
# CAPITAL-TRACK / Section-102 long-term rate (~72%) are DIFFERENT treatments
# yielding DIFFERENT legitimate retention rates — not a contradiction. Only flag
# divergence WITHIN one bucket (codex 2026-06-19). A match with no treatment cue
# nearby is "unknown" — two distinct unknowns still flag (fail-loud, the run-106
# same-vest case). Context window scanned around each match for these cues.
_CAPITAL_CUE_RE = re.compile(
    r"capital[\s-]*track|section[\s-]*102|long[\s-]*term|\bcgt\b|capital[\s-]*gains",
    re.IGNORECASE,
)
_ORDINARY_CUE_RE = re.compile(
    r"at[\s-]*vest|\bordinary\b|\bmarginal\b|\bwage\b|vest[\s-]*event|at[\s-]*the[\s-]*vest",
    re.IGNORECASE,
)
# Chars on each side of a retention match scanned for a tax-treatment cue.
_TREATMENT_CONTEXT_CHARS = 80


def _treatment_bucket(text: str, start: int, end: int) -> str:
    """Bucket a retention match by the NEAREST tax-treatment cue. Adjacent
    sentences (at-vest … capital-track …) overlap in a fixed window, so pick the
    cue closest to the match rather than a fixed precedence — otherwise an at-vest
    rate inherits a neighbouring capital-track cue and mis-buckets."""
    lo = max(0, start - _TREATMENT_CONTEXT_CHARS)
    ctx = text[lo: end + _TREATMENT_CONTEXT_CHARS]
    mid = start - lo  # match position within ctx
    best_bucket = "unknown"
    best_dist = None
    for bucket, rx in (("capital", _CAPITAL_CUE_RE), ("ordinary", _ORDINARY_CUE_RE)):
        for cm in rx.finditer(ctx):
            # distance from the cue to the match (0 if overlapping).
            dist = 0 if cm.start() <= mid <= cm.end() else min(
                abs(cm.start() - mid), abs(cm.end() - mid)
            )
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_bucket = bucket
    return best_bucket


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
    # Collect (value, tax-treatment bucket). Compare only within a bucket: an
    # at-vest ordinary rate and a capital-track long-term rate are different
    # treatments, not a contradiction.
    by_bucket: dict[str, list[float]] = {}
    for m in _RETENTION_PCT_RE.finditer(text):
        raw = m.group("num1") or m.group("num2")
        if raw is None:
            continue
        pct = float(raw)
        # A retention share is a fraction of a vest; >100% is not a retention.
        if 0.0 < pct <= 100.0:
            bucket = _treatment_bucket(text, m.start(), m.end())
            by_bucket.setdefault(bucket, []).append(pct)

    # Distinct values that diverge WITHIN any single bucket.
    distinct: list[float] = []
    for vals in by_bucket.values():
        bucket_distinct: list[float] = []
        for v in vals:
            if not any(abs(v - d) <= _RETENTION_TOLERANCE_PP for d in bucket_distinct):
                bucket_distinct.append(v)
        if len(bucket_distinct) >= 2:
            for v in bucket_distinct:
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
