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
# Subject binding (option B, codex 2026-06-10)
# ---------------------------------------------------------------------------
#
# Instead of "scan every number on a line that mentions a headline keyword"
# (value-only — which flagged legitimate narrative numbers like an FX "30%
# strengthening" or "90% solvency" sitting on a line that happens to say NVDA
# or retirement), we bind a number to its SUBJECT: only the value that is the
# stated value OF a headline subject must trace. A number not bound to a
# subject is narrative and is left alone. This both kills the false positives
# AND closes the value-only wrong-context hole (a "current NVDA 12%" can no
# longer satisfy the *target* — the subject decides which resolved class the
# token is checked against is not needed; we still check value within unit, but
# only the subject's own value is checked at all).
#
# Each binding: (subject regex, unit, window_chars). After a subject match we
# look only in the next ``window_chars`` of the SAME line for the first value
# token of the expected unit; that token must trace (or be the pending
# literal). Subjects are deliberately specific (e.g. "NVDA target", not bare
# "NVDA") so narrative phrasings ("NVDA fell 30%") never bind.
_SUBJECT_BINDINGS: tuple[tuple[re.Pattern[str], str, int], ...] = (
    # FI / net-worth capital headline — the ₪21M-fabrication reject lives here.
    (re.compile(r"\bfi\s+(?:capital\s+)?target\b", re.IGNORECASE), "nis", 60),
    (re.compile(r"\bfinancial\s+independence\s+(?:capital\s+)?(?:target|number)\b", re.IGNORECASE), "nis", 60),
    (re.compile(r"\b(?:fi|retirement)\s+capital\s+target\b", re.IGNORECASE), "nis", 60),
    (re.compile(r"\bnest\s+egg\b", re.IGNORECASE), "nis", 60),
    (re.compile(r"\bnet\s+worth\b", re.IGNORECASE), "nis", 60),
    # Retirement / FI age.
    (re.compile(r"\b(?:retirement|fi)\s+age\b", re.IGNORECASE), "age", 40),
    (re.compile(r"\bearliest[\s-]*(?:safe)?[\s-]*(?:retirement\s+)?age\b", re.IGNORECASE), "age", 40),
    # NVDA target weight + concentration cap (NOT bare "NVDA").
    (re.compile(r"\bnvda\s+(?:strategic\s+)?(?:target|weight)\b", re.IGNORECASE), "pct", 50),
    (re.compile(r"\bnvda\s+(?:concentration\s+)?cap\b", re.IGNORECASE), "pct", 50),
    (re.compile(r"\bconcentration\s+cap\b", re.IGNORECASE), "pct", 50),
    (re.compile(r"\btarget\s+nvda\s+weight\b", re.IGNORECASE), "pct", 50),
)

# First value token of each unit in a post-subject window. The subject already
# says "age", so the value is a bare 2-digit — but NOT one followed by "%" (a
# percent in a parenthetical like "worst-10%" / "90% solvency" is not the age).
_AGE_VALUE = re.compile(r"\b(\d{2})\b(?!\s*%)")


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
        for line_no, line in enumerate(text.splitlines(), start=1):
            # Subject-bound: for each headline subject on the line, check ONLY
            # the value stated as that subject's value (the first matching-unit
            # token in the window after the subject). Numbers not bound to a
            # subject are narrative and left alone. Dedup by (line, token) so
            # two overlapping subject patterns (e.g. "retirement age" +
            # "earliest-safe ... age") don't double-count the same value.
            seen: set[tuple[int, str]] = set()
            for pat, unit, window in _SUBJECT_BINDINGS:
                for m in pat.finditer(line):
                    bad = _bind_value(
                        line, m.start(), m.end(), unit, window,
                        resolved_by_unit[unit],
                    )
                    if bad is None:
                        continue
                    kind, token, why = bad
                    if (line_no, token) in seen:
                        continue
                    seen.add((line_no, token))
                    violations.append(
                        _violation(horizon_name, line_no, kind, token, why)
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


# Value-BEFORE-subject forms ("₪21M FI target", "99% NVDA cap", "age 47 ... age")
# — the value must be IMMEDIATELY adjacent (only whitespace / markdown / a
# connector between it and the subject), so a narrative number further left
# ("fell 30%, raising the cap") never binds. Anchored at the end of the left
# window.
_NIS_END = re.compile(r"(₪\s*\d[\d,]*(?:\.\d+)?\s*[MmKk]?)[\s*:=~—-]*$")
_PCT_END = re.compile(r"(\d+(?:\.\d+)?\s*%)[\s*:=~—-]*$")
_AGE_END = re.compile(r"\b(\d{2})[\s*:=~,—-]*$")

_CLAUSE_BOUNDARY = re.compile(r"(?<!\d)[.;](?!\d)|\n")
_KINDS = {"nis": "₪ amount", "pct": "percent", "age": "age"}


def _clause(text: str, *, from_end: bool = False) -> str:
    """Drop parenthetical asides, then return the clause adjacent to the subject
    (decimal-safe). Parens are stripped BEFORE measuring so a number in a
    derivation note (e.g. "(90% MC solvency to 95)") can't be the bound value
    and the stated value following the aside is reached within the window.

    For the window AFTER the subject the adjacent clause is the FIRST segment
    (default). For the window BEFORE the subject (``from_end=True``) the adjacent
    clause is the LAST segment — otherwise the value-before path reaches back
    across a ``;``/``.`` boundary and binds a number from a different clause
    (e.g. "...shock is -50.7%; at current NVDA weight" wrongly bound -50.7%)."""
    text = re.sub(r"\([^)]*\)", " ", text)
    parts = _CLAUSE_BOUNDARY.split(text)
    return parts[-1] if from_end else parts[0]


# A number only binds to a headline subject when it is GRAMMATICALLY ATTACHED to
# it — the subject "owns" the value. It does NOT when the value is really some
# DIFFERENT QUANTITY's value (an impact, shock, delta, drawdown, …). That shows
# up two structurally distinct ways, so we guard both:
#
#   (a) the other-quantity word sits BETWEEN the subject and the value:
#       "current NVDA weight the implied p10 IMPACT is -33%" — the -33% follows
#       "impact". -> _OTHER_QUANTITY, checked between subject and value.
#   (b) the other-quantity word is the grammatical SUBJECT before a prepositional
#       phrase that the headline subject sits inside:
#       "p10 portfolio DELTA at current NVDA weight is -33%" — "delta" owns the
#       -33%; "at current NVDA weight" is a qualifier. -> _QUALIFIER_OF_OTHER,
#       checked on the text BEFORE the subject.
#
# This structural model replaces the brittle preposition/present-state/sign
# heuristics. It FLAGS declarations regardless of phrasing ("The current NVDA
# weight is 99%", "Given the NVDA cap is 99%", "NVDA cap equals 13%") and DROPS
# different-quantity numbers regardless of preposition/filler. A negative cap
# typo ("NVDA cap -12%") stays bound (no other-quantity context) and is flagged.
#
# Deliberately EXCLUDES copulas ("equals"/"is"): they are legitimate declaration
# verbs ("NVDA cap equals 13%"), and the impact phrasings that use them are
# already caught by the (b) left-qualifier guard via their leading noun.
# Pure different-QUANTITY nouns only. Deliberately EXCLUDES subject-movement
# verbs (drop/move/fall/decline/gain/rise/...): those describe the SUBJECT's own
# change ("the NVDA cap should DROP to 99%", "target should MOVE to 99%") and
# must stay BOUND so a fabricated target is flagged (codex r5). A different
# quantity is named by a noun, so keying on nouns avoids that false negative.
_OTHER_QUANTITY_NOUN = (
    r"(?:impact|shock|loss(?:es)?|drawdown|delta|downside|upside|drag|"
    r"stress|sensitivity|var|cvar)"
)
_OTHER_QUANTITY = re.compile(r"\b" + _OTHER_QUANTITY_NOUN + r"\b", re.IGNORECASE)

# (b): the subject is a prepositional qualifier of a leading different-quantity
# noun. Matches when an other-quantity noun appears, then (within the same
# clause — no .;) a preposition + optional state/scope fillers right before the
# subject. "Given the NVDA cap is 99%" does NOT match (no leading quantity noun),
# so genuine declarations still bind.
_QUALIFIER_OF_OTHER = re.compile(
    r"\b" + _OTHER_QUANTITY_NOUN + r"\b[^.;]*?\b"
    r"(?:at|with|under|for|by|given|versus|vs)\s+"
    r"(?:the\s+|current\s+|today'?s\s+|portfolio\s+|full[- ]?book\s+|"
    r"tradeable\s+|a\s+|\*+\s*)*$",
    re.IGNORECASE,
)


def _is_qualifier_of_other_quantity(line: str, subject_start: int) -> bool:
    """True when the subject sits in a prepositional phrase qualifying a leading
    different-quantity noun ("p10 delta AT current NVDA weight ...")."""
    return _QUALIFIER_OF_OTHER.search(line[:subject_start]) is not None


def _value_belongs_to_other_quantity(segment: str, unit: str, *, value_at_end: bool) -> bool:
    """True when the first/last value token in ``segment`` is the value of a
    DIFFERENT quantity (an impact/shock/…), not the headline subject.

    ``value_at_end=False`` (the after-subject clause): a different-quantity word
    appears BEFORE the token. ``value_at_end=True`` (the before-subject clause,
    where the value is end-anchored): the word appears anywhere in the clause
    ahead of the value."""
    regs = {"nis": _NIS_TOKEN, "pct": _PCT_TOKEN, "age": _AGE_VALUE}
    ms = list(regs[unit].finditer(segment))
    if not ms:
        return False
    m = ms[-1] if value_at_end else ms[0]
    return _OTHER_QUANTITY.search(segment[: m.start()]) is not None


def _token_in(
    text: str, unit: str, *, last: bool = False
) -> tuple[str, list[float]] | None:
    """First (or last) value token of ``unit`` in ``text`` → (display, candidates)."""
    if unit == "nis":
        ms = list(_NIS_TOKEN.finditer(text))
    elif unit == "pct":
        ms = list(_PCT_TOKEN.finditer(text))
    else:
        ms = list(_AGE_VALUE.finditer(text))
    if not ms:
        return None
    m = ms[-1] if last else ms[0]
    if unit == "nis":
        num = _parse_num(m.group("num"))
        return (m.group(0).strip(), _nis_candidates(num, m.group("suffix"))) if num is not None else None
    if unit == "pct":
        num = _parse_num(m.group("num"))
        return (m.group(0).strip(), [num]) if num is not None else None
    num = _parse_num(m.group(1))
    return (f"age {m.group(1)}", [num]) if num is not None else None


def _traces(unit: str, candidates: list[float], resolved: list[tuple[str, float]]) -> bool:
    # Resolver pct values are FRACTIONS (0.13) while the markdown shows points
    # (13%); compare a pct token against both rv*100 and rv.
    for c in candidates:
        for _, rv in resolved:
            if unit == "pct":
                if _matches(c, rv * 100.0, "pct") or _matches(c, rv, "pct"):
                    return True
            elif _matches(c, rv, unit):
                return True
    return False


def _bind_value(
    line: str, s: int, e: int, unit: str, window: int,
    resolved: list[tuple[str, float]],
) -> tuple[str, str, str] | None:
    """Bind the value stated AS the subject's value and check it traces.

    Searches in priority order: (1) the clause AFTER the subject (parens
    stripped, boundary-cut) — first token; (2) a short window immediately
    BEFORE the subject — adjacent token, for value-before-subject forms like
    "₪21M FI target" / "99% NVDA cap"; (3) a parenthesized value right after
    the subject — "FI target (₪21M)". Returns ``(kind, token, why)`` on a
    non-tracing value, else ``None`` (no inline value / pending escape / traces).
    """
    # (b) The subject is a prepositional qualifier of a leading different-quantity
    # noun ("p10 portfolio delta at current NVDA weight is -33%") → it states no
    # value of its own; bind nothing.
    if _is_qualifier_of_other_quantity(line, s):
        return None

    right_raw = line[e: e + window]
    right = _clause(right_raw)

    # Pending escape preceding any digit in the after-clause → nothing to trace.
    pend = right.find(PENDING_LABEL)
    fd = re.search(r"\d", right)
    if pend != -1 and (fd is None or pend < fd.start()):
        return None

    # (1) after-subject clause — but only if the value is grammatically attached
    # to the subject (no different-quantity word like "impact"/"equals" between
    # them; otherwise the number is that quantity's value, not the subject's).
    found = None
    if not _value_belongs_to_other_quantity(right, unit, value_at_end=False):
        found = _token_in(right, unit)

    # (2) value-before-subject — must be IMMEDIATELY adjacent (end-anchored), and
    # not the value of a different quantity stated in the same clause.
    if found is None:
        left = _clause(line[max(0, s - 20): s], from_end=True)
        if not _value_belongs_to_other_quantity(left, unit, value_at_end=True):
            end_pat = {"nis": _NIS_END, "pct": _PCT_END, "age": _AGE_END}[unit]
            em = end_pat.search(left)
            if em is not None:
                found = _token_in(em.group(1), unit, last=True)

    # (3) parenthesized value immediately after the subject — "FI target (₪21M)".
    # Same attachment rule: skip when a different-quantity word precedes the value
    # inside the parens ("current NVDA weight (p10 impact -33%)").
    if found is None:
        pm = re.match(r"\s*\(([^)]*)\)", right_raw)
        if pm is not None and not _value_belongs_to_other_quantity(
            pm.group(1), unit, value_at_end=False
        ):
            found = _token_in(pm.group(1), unit)

    if found is None:
        return None
    token, cands = found
    if _traces(unit, cands, resolved):
        return None
    why = (
        f"no resolved {unit} within tolerance"
        if resolved
        else f"no {unit} value resolved at all (resolver pending)"
    )
    return (_KINDS[unit], token, why)


# ---------------------------------------------------------------------------
# Primary scrub — codex's recommended #24 PRIMARY gate. The /accept-time
# checker above only DETECTS; this mutates the user-facing markdown BEFORE
# persist so a synth-fabricated headline number never reaches the draft body.
# Any headline token that traces to no resolved value of its class is replaced
# with the [derivation pending] literal (fail-closed to the sanctioned escape
# hatch). The deterministic resolver remains the single source of truth.
# ---------------------------------------------------------------------------


def _resolved_by_unit(
    resolved: "ResolvedPlanNumbers",
) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {"nis": [], "pct": [], "age": []}
    for rv in resolved.values.values():
        if rv.status != "resolved" or rv.value is None:
            continue
        bucket = out.get(rv.unit)
        if bucket is not None:
            bucket.append((rv.key, float(rv.value)))
    return out


def _token_ok_nis(num: float, suffix: str | None, resolved: list[tuple[str, float]]) -> bool:
    cands = _nis_candidates(num, suffix)
    return any(_matches(c, rv, "nis") for c in cands for _, rv in resolved)


# The mutating scrub is deliberately SURGICAL — far narrower than the
# advisory check above. It only rewrites the headline figure the #1 reject was
# about: a large NIS capital amount presented as the FI / retirement /
# net-worth target that traces to no resolved value. It does NOT mutate
# percentages, ages, or small NIS amounts (income, education, monthly burn,
# RSU tranches, sub-components) — those are legitimate plan detail and a live
# drun showed a broad scrub turning ~44 of them into [derivation pending],
# which destroys the plan. Fabricated pct/age values are still surfaced by the
# advisory check + the codex review; only the load-bearing capital headline is
# fail-closed at persist.

# A NIS amount is only scrubbed on a line in this tight FI-capital context.
_FI_CAPITAL_LINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfi\s+(?:capital\s+)?target\b",
        r"\bfi\s+base\b",
        r"\bfi\s+number\b",
        r"\bfi\s+threshold\b",
        r"\bfinancial\s+independence\b",
        r"\bcapital\s+target\b",
        r"\bnet\s+worth\b",
        r"\bnest\s+egg\b",
        r"\bperpetuity\b",
        r"\bretirement\s+(?:capital|target|corpus|number)\b",
    )
)

# Only NIS amounts at/above this magnitude are candidates — the FI capital
# target is ~₪10M; education (₪0.5–1.5M), monthly burn, income, and RSU
# tranches all sit below it, so the floor cleanly excludes plan detail.
_SCRUB_NIS_FLOOR = 2_000_000.0


def _is_fi_capital_line(line: str) -> bool:
    return any(p.search(line) for p in _FI_CAPITAL_LINE_PATTERNS)


def _scrub_line(line: str, by_unit: dict[str, list[tuple[str, float]]]) -> tuple[str, list[str]]:
    """Replace a fabricated FI-capital headline NIS amount with the pending
    literal. Returns ``(scrubbed_line, [scrubbed_token, ...])``.

    Surgical: only large NIS amounts (>= the floor) on an FI-capital/net-worth
    line that match no resolved value are scrubbed. Percentages, ages, and
    small NIS amounts are never mutated here.
    """
    if not _is_fi_capital_line(line):
        return line, []
    spans: list[tuple[int, int, str]] = []  # (start, end, original_token)
    for m in _NIS_TOKEN.finditer(line):
        num = _parse_num(m.group("num"))
        if num is None:
            continue
        cands = _nis_candidates(num, m.group("suffix"))
        # Candidate magnitude must clear the FI-capital floor to be eligible.
        if not any(c >= _SCRUB_NIS_FLOOR for c in cands):
            continue
        if not _token_ok_nis(num, m.group("suffix"), by_unit["nis"]):
            spans.append((m.start(), m.end(), m.group(0)))

    if not spans:
        return line, []
    # Apply right-to-left; drop overlaps (a later class re-matching the same
    # offset) by keeping the first span at each start.
    spans.sort(key=lambda s: s[0])
    deduped: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, tok in spans:
        if start >= last_end:
            deduped.append((start, end, tok))
            last_end = end
    scrubbed = line
    removed: list[str] = []
    for start, end, tok in reversed(deduped):
        scrubbed = scrubbed[:start] + PENDING_LABEL + scrubbed[end:]
        removed.append(tok.strip())
    removed.reverse()
    return scrubbed, removed


def scrub_headline_numeric_source(
    horizon_text: dict[str, str],
    resolved: "ResolvedPlanNumbers",
) -> tuple[dict[str, str], list[str]]:
    """Scrub fabricated headline numbers out of the user-facing markdown.

    Surgical: only a large NIS amount (>= ₪2M) on an FI-capital/net-worth
    line that traces to no resolved value is replaced with
    ``[derivation pending]``. ``_scrub_line`` self-gates on the tight
    FI-capital context, so the loop calls it on every line (the broad
    advisory ``_is_headline_line`` is NOT used here — it both over-matches
    detail lines and under-matches phrasings like "FI capital target").
    Returns ``(scrubbed_horizon_text, scrub_log)``.
    """
    by_unit = _resolved_by_unit(resolved)
    scrubbed_text: dict[str, str] = {}
    scrub_log: list[str] = []
    for horizon_name, text in horizon_text.items():
        if not text:
            scrubbed_text[horizon_name] = text
            continue
        out_lines: list[str] = []
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            new_line, removed = _scrub_line(raw_line, by_unit)
            out_lines.append(new_line)
            for tok in removed:
                scrub_log.append(f"{horizon_name} line={line_no} token=`{tok}`")
        # Preserve a trailing newline if the original had one.
        joined = "\n".join(out_lines)
        if text.endswith("\n"):
            joined += "\n"
        scrubbed_text[horizon_name] = joined
    return scrubbed_text, scrub_log


__all__ = [
    "check_headline_numeric_source",
    "scrub_headline_numeric_source",
    "PENDING_LABEL",
]
