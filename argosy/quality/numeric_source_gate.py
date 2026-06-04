"""Check 6 — headline_numeric_source (#24).

The user's #1 reject was a synthesizer-FABRICATED headline number (a round
₪21M FI target) that traced to nothing. The deterministic resolver
(:func:`argosy.services.plan_numeric_resolver.resolve_plan_numbers`) is the
single source of truth for what the plan's headline numbers are ALLOWED to
be. This gate is the backstop: it tokenizes the headline numbers out of the
user-facing horizon markdown and verifies every one traces to a RESOLVED
value (within a small tolerance) — or is rendered ``[derivation pending]``.

Design (per codex Q2):

* Not a regex-per-number. We tokenize ₪ / percent / age numbers only on
  lines that are in a clearly HEADLINE context (FI target, retirement
  age/year, net worth, savings, spend, NVDA cap/weight), then compare each
  token against the resolver's RESOLVED display-forms. Dates, section
  numbers, table indices, and fleet-receipt token/cost counts are NOT
  headline and are never scanned.
* The resolved values are the approved set. A token matches if, under its
  unit class (nis / pct / age), some resolved value of that class is within
  tolerance. NIS tokens are matched against both the raw value and the
  ``/1e6`` millions form (the renderer emits both ``₪277,004`` and
  ``₪21.00M``).
* ``[derivation pending]`` is the sanctioned escape hatch: the literal is
  never flagged (there is no number to trace).

A token in a headline line that matches NO resolved value of its class and
is not the pending literal is a fabrication → one GateViolation.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from argosy.quality.gate_types import GateCheck, GateViolation

if TYPE_CHECKING:  # pragma: no cover — typing only
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers


# The literal the renderer emits for any un-derived figure. Kept in sync
# with render._pending_label(); duplicated (not imported) so the gate has
# no dependency on the renderer module.
PENDING_LABEL = "[derivation pending]"


# ---------------------------------------------------------------------------
# Headline-context detection
# ---------------------------------------------------------------------------
#
# A line is scanned for headline numbers only when it mentions one of these
# concepts. This is the codex "headline set": the numbers that change a
# user-facing financial conclusion, target, or action. Matching is on
# whole-ish words, case-insensitive. Deliberately conservative — a number on
# a line with no headline keyword is left alone (avoids false positives on
# dates, table indices, section numbers, footnotes).

_HEADLINE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfi\s+target\b",
        r"\bfinancial\s+independence\b",
        r"\bnet\s+worth\b",
        r"\bportfolio\b",
        r"\bretirement\s+age\b",
        r"\bretire\b",
        r"\bfi[\s-]*ready\b",
        r"\bfi\s+age\b",
        r"\bsavings?\b",
        r"\bspend(?:ing)?\b",
        r"\bburn\b",
        r"\bnvda\b",
        r"\bconcentration\s+cap\b",
        r"\bweight\b",
    )
)


def _is_headline_line(line: str) -> bool:
    return any(p.search(line) for p in _HEADLINE_LINE_PATTERNS)


# ---------------------------------------------------------------------------
# Numeric token extraction
# ---------------------------------------------------------------------------
#
# Three token classes, each carrying the magnitude(s) the resolver should be
# able to confirm:
#   nis   — "₪21.00M", "₪277,004", "₪0.82M" → magnitudes {raw, raw*1e6 if M}
#   pct   — "97%", "3.5%"                    → magnitude raw
#   age   — "age 49", "age-49"               → magnitude raw
#
# A ₪ amount with an "M"/"m" suffix is the millions form; without a suffix it
# is the raw figure. We carry BOTH candidate magnitudes for an un-suffixed
# small number too (so "₪21" is matched against 21 and 21,000,000) — but in
# practice the renderer always suffixes millions, so the suffix is the
# primary signal.

_NIS_TOKEN = re.compile(
    r"₪\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>[MmKk])?"
)
_PCT_TOKEN = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*%")
_AGE_TOKEN = re.compile(r"\bage[\s-]*(?P<num>\d{2})\b", re.IGNORECASE)


def _parse_num(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _nis_candidates(num: float, suffix: str | None) -> list[float]:
    """Candidate NIS magnitudes a ₪-token could denote."""
    s = (suffix or "").lower()
    if s == "m":
        return [num * 1_000_000.0]
    if s == "k":
        return [num * 1_000.0]
    # Un-suffixed: it is the raw figure. Also allow the millions reading for
    # small numbers (e.g. a bare "₪21" meaning 21M) so we never false-flag a
    # legitimate-but-unsuffixed headline.
    cands = [num]
    if num < 1000:
        cands.append(num * 1_000_000.0)
    return cands


# ---------------------------------------------------------------------------
# Tolerance: a token "matches" a resolved value within this relative band.
# Headline numbers are rounded for display (₪21.00M vs a 20,995,300 source),
# so an exact match is wrong. 1.5% relative (or ₪10k / 0.2pct / 0.5yr
# absolute floor for tiny magnitudes) absorbs display rounding without
# letting a fabricated round number (₪21M vs a real ₪17M target) slip
# through.
# ---------------------------------------------------------------------------

_REL_TOL = 0.015


def _abs_floor(unit: str) -> float:
    if unit == "nis":
        return 10_000.0
    if unit == "pct":
        return 0.2
    if unit == "age":
        return 0.5
    return 0.0


def _matches(candidate: float, resolved: float, unit: str) -> bool:
    tol = max(abs(resolved) * _REL_TOL, _abs_floor(unit))
    return abs(candidate - resolved) <= tol


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------


def check_headline_numeric_source(
    horizon_text: dict[str, str],
    resolved: "ResolvedPlanNumbers",
) -> list[GateViolation]:
    """Verify every headline number in the markdown traces to a RESOLVED
    value (or is rendered ``[derivation pending]``).

    Args:
        horizon_text: horizon name -> user-facing markdown.
        resolved: the resolver manifest for this plan's decision run.

    Returns one :class:`GateViolation` per headline token that matches no
    resolved value of its unit class. The pending literal is never flagged.
    """
    # Partition the resolved (status=="resolved", value is not None) values
    # by unit class once.
    resolved_by_unit: dict[str, list[tuple[str, float]]] = {
        "nis": [],
        "pct": [],
        "age": [],
    }
    for rv in resolved.values.values():
        if rv.status != "resolved" or rv.value is None:
            continue
        bucket = resolved_by_unit.get(rv.unit)
        if bucket is not None:
            bucket.append((rv.key, float(rv.value)))

    violations: list[GateViolation] = []

    for horizon_name, text in horizon_text.items():
        if not text:
            continue
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            if not _is_headline_line(raw_line):
                continue
            # The number was replaced by the pending literal → nothing to
            # trace on this line for that slot. We still scan other tokens
            # on the line (a line can mix a pending figure and a real one),
            # but the literal itself carries no digits, so it never trips
            # the token regexes.
            line = raw_line

            violations.extend(
                _scan_nis(line, horizon_name, line_no, resolved_by_unit["nis"])
            )
            violations.extend(
                _scan_pct(line, horizon_name, line_no, resolved_by_unit["pct"])
            )
            violations.extend(
                _scan_age(line, horizon_name, line_no, resolved_by_unit["age"])
            )

    return violations


def _violation(
    horizon: str, line_no: int, kind: str, token: str, why: str
) -> GateViolation:
    return GateViolation(
        check=GateCheck.HEADLINE_NUMERIC_SOURCE,
        detail=(
            f"headline {kind} `{token}` traces to no resolved value "
            f"({why}) — render `{PENDING_LABEL}` or fix the source field"
        ),
        locator=f"horizon={horizon} line={line_no}",
    )


def _scan_nis(
    line: str, horizon: str, line_no: int, resolved: list[tuple[str, float]]
) -> list[GateViolation]:
    out: list[GateViolation] = []
    for m in _NIS_TOKEN.finditer(line):
        num = _parse_num(m.group("num"))
        if num is None:
            continue
        cands = _nis_candidates(num, m.group("suffix"))
        ok = any(
            _matches(c, rv, "nis") for c in cands for _, rv in resolved
        )
        if not ok:
            why = (
                "no resolved NIS value within tolerance"
                if resolved
                else "no NIS value resolved at all (resolver pending)"
            )
            out.append(_violation(horizon, line_no, "₪ amount", m.group(0).strip(), why))
    return out


def _scan_pct(
    line: str, horizon: str, line_no: int, resolved: list[tuple[str, float]]
) -> list[GateViolation]:
    # Resolver pct values are FRACTIONS (0.0–1.0; e.g. a 4.5% real yield is
    # stored as 0.045, an NVDA cap of 35% as 0.35) while the markdown shows
    # percent-points ("4.5%", "35%"). The renderer prints `value * 100`, so
    # we compare the token against BOTH the fraction-scaled-to-points form
    # and the raw resolved value (defensive in case a future key is already
    # stored in points).
    out: list[GateViolation] = []
    for m in _PCT_TOKEN.finditer(line):
        num = _parse_num(m.group("num"))
        if num is None:
            continue
        ok = any(
            _matches(num, rv * 100.0, "pct") or _matches(num, rv, "pct")
            for _, rv in resolved
        )
        if not ok:
            why = (
                "no resolved percent within tolerance"
                if resolved
                else "no percent value resolved at all (resolver pending)"
            )
            out.append(_violation(horizon, line_no, "percent", m.group(0).strip(), why))
    return out


def _scan_age(
    line: str, horizon: str, line_no: int, resolved: list[tuple[str, float]]
) -> list[GateViolation]:
    out: list[GateViolation] = []
    for m in _AGE_TOKEN.finditer(line):
        num = _parse_num(m.group("num"))
        if num is None:
            continue
        if not any(_matches(num, rv, "age") for _, rv in resolved):
            why = (
                "no resolved age within tolerance"
                if resolved
                else "no age value resolved at all (resolver pending)"
            )
            out.append(_violation(horizon, line_no, "age", m.group(0).strip(), why))
    return out


__all__ = ["check_headline_numeric_source", "PENDING_LABEL"]
