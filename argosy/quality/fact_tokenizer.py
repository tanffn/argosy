"""Deterministic post-synthesis pass: tokenize canonical figures.

The synthesizer writes canonical figures (NVDA glide sell count, NVDA cap/
target/current weight, FI margins, …) as raw digits in prose. Every re-roll
of a section (even a narrowly-scoped amendment) re-derives those digits from
scratch, so they drift — the exact failure the fact-registry / ``{{fact:}}``
placeholder protocol (``argosy.quality.fact_registry``) exists to prevent.
That protocol only helps when the LLM actually EMITS the token; in practice
it still types digits.

This module is the deterministic backstop that runs AFTER synthesis, before
the FM / whole-artifact reader ever see the draft:

  * a literal that matches its registered canonical value (see
    ``argosy.quality.fact_registry.FACT_DISPLAY``) is REWRITTEN to
    ``{{fact:key}}`` in place — the rendered text is unchanged today, and
    immune to future re-rolls because it is no longer a literal;
  * a literal that is clearly about the same concept (proximity to a
    concept-specific anchor phrase) but DIFFERS from canonical is NEVER
    silently corrected — it is surfaced as a ``GateCheck.FACT_LITERAL_DRIFT``
    violation so the fabrication is visible, not quietly papered over.

False-positive safety (this rewrites the user's financial plan text, so a
wrong substitution is worse than no substitution):

  * every fact key requires an explicit :class:`AnchorSpec` — a concept
    anchor phrase that must sit within a small window of the number, and
    (for NVDA concepts) the literal "NVDA" within a wider proximity window.
    Modelled on ``argosy.services.assembled_artifact._extract_prose_nvda_values``
    (phrase anchoring + proximity window) and
    ``argosy.quality.cap_target_autocorrect`` (never invent a number, only
    reword/replace what's already grounded).
  * a fact key with NO :class:`AnchorSpec` is never scanned — under-reaching
    is the safe default; a key must be explicitly, narrowly anchored to be
    eligible for rewriting at all.
  * numbers already inside a ``{{fact:...}}`` token, a fenced/inline code
    span, a blockquote line, or a double-quoted extract are masked out
    before scanning and are never touched.
  * "matches canonical" uses a TIGHT tolerance (near-exact, absorbing only
    display rounding) — anything anchored but outside that tolerance is
    drift, never silently rewritten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from argosy.quality.fact_registry import FACT_DISPLAY, FACT_SOURCE_ALIAS, format_fact
from argosy.quality.gate_types import GateCheck, GateViolation

if TYPE_CHECKING:  # pragma: no cover — typing only
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers


# ---------------------------------------------------------------------------
# Masking — never touch a figure inside an existing token / code / citation.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\{\{fact:[A-Za-z0-9_.]+\}\}")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_BLOCKQUOTE_LINE_RE = re.compile(r"^>.*$", re.MULTILINE)
# Double-quoted source extracts ("Section-102 ... = 9,230 shares") — citation
# text that must be reproduced verbatim, never rewritten.
_DOUBLE_QUOTED_RE = re.compile(r"“[^”\n]{2,240}”|\"[^\"\n]{2,240}\"")


def _mask_protected_spans(text: str) -> str:
    """Blank out (length-preserving) spans that must never be scanned or
    rewritten. Blanking (not deleting) keeps every downstream match position
    identical to the original text, so replacement spans slice cleanly out
    of the UNMASKED original."""

    def _blank(m: re.Match) -> str:
        return " " * len(m.group(0))

    out = text
    for pat in (_FENCED_CODE_RE, _TOKEN_RE, _INLINE_CODE_RE, _BLOCKQUOTE_LINE_RE, _DOUBLE_QUOTED_RE):
        out = pat.sub(_blank, out)
    return out


# ---------------------------------------------------------------------------
# Anchor specs — one per registered fact key we are confident enough to
# tokenize. A key absent here is never scanned (safe under-reach).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorSpec:
    key: str
    unit: str  # "sh" | "pct" | "nis"
    concept_any: tuple[re.Pattern[str], ...]
    window: int = 80
    exclude_any: tuple[re.Pattern[str], ...] = ()
    global_term: re.Pattern[str] | None = None
    global_window: int = 400


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_NVDA_TERM = _p(r"\bnvda\b")

# NVDA share-count concepts. Each requires a DISTINCT anchor verb so the
# three counts never cross-attribute the same number.
_ANCHOR_NVDA_SELL_SH = AnchorSpec(
    key="concentration.nvda_sell_sh", unit="sh",
    concept_any=(_p(r"\bsell(?:s|ing)?\b|\bsold\b|\btrim(?:s|med|ming)?\b|\bglide\b"),),
    # "quota remaining" (NOT bare "quota") excludes the DIFFERENT per-tax-year
    # sale-allowance concept ("tax-year 2026 quota remaining ... in the 2026
    # tax-year sell allowance" — mentions "sell" but is not the glide's total
    # sell count). Bare "quota" is too broad: the legitimate glide-sell
    # sentence itself reads "...sells 9,417 shares ... at the quota PACE,
    # holding fresh vests..." — "quota" there describes the SELL concept's
    # own pacing, not the other concept, so it must not exclude.
    exclude_any=(_p(r"\beligible\b|\bretain\b|\bremaining\s+shares\b|\bquota\s+remaining\b"),),
    global_term=_NVDA_TERM,
)
_ANCHOR_NVDA_TARGET_SH = AnchorSpec(
    key="concentration.nvda_target_sh", unit="sh",
    # "remaining" alone is too generic — "the remaining tax-year 2026 QUOTA is
    # 3,924 shares" is a DIFFERENT concept (the annual sale-quota balance),
    # not the post-glide retained target. Require "remaining" to sit directly
    # against "shares" (the target-retention phrasing: "retains ... shares",
    # "N remaining shares") rather than firing on any nearby "remaining".
    concept_any=(_p(r"\bretain(?:s|ed|ing)?\b|\btarget\b|\bpost-sale\b|\bremaining\s+shares\b|\bkeep\b"),),
    exclude_any=(_p(r"\bsell(?:s|ing)?\b|\bsold\b|\beligible\b|\bquota\s+remaining\b"),),
    global_term=_NVDA_TERM,
)
_ANCHOR_NVDA_ELIGIBLE_NOW_SH = AnchorSpec(
    key="concentration.nvda_eligible_now_sh", unit="sh",
    concept_any=(_p(r"\beligible\b|\bvested\b"),),
    exclude_any=(_p(r"\bsell(?:s|ing)?\b|\btarget\b|\bretain\b"),),
    global_term=_NVDA_TERM,
)

# NVDA policy percentages — same domain-specific phrases the whole-artifact
# extractor already uses (assembled_artifact._PROSE_NVDA_CAP_RE /
# _PROSE_NVDA_TARGET_RE), so the anchor vocabulary is proven not to collide
# with tax rates / returns / other-sleeve allocations.
# NOTE on exclude_any: cap and target are ROUTINELY named in the SAME
# breath ("steers NVDA to an 8% single-stock marker inside the binding
# ceiling" — one clause, both concepts). Excluding each other's anchor
# phrase here would suppress BOTH from ever matching such a (very common)
# sentence. Cross-attribution is instead resolved by the two-phase
# match-then-drift pass in tokenize_text: every spec in the "pct" group
# gets a crack at MATCHING first (only the spec whose canonical the number
# actually equals wins substitution + masks the span), so cap and target
# never fight over the same literal — the arithmetic itself decides, not a
# textual exclusion.
_ANCHOR_NVDA_CAP_PCT = AnchorSpec(
    key="concentration.nvda_cap_pct", unit="pct",
    concept_any=(_p(r"\bhard\s+cap\b|\bbinding\s+ceiling\b|\bconcentration\s+cap\b|\binstrument-level\s+ceiling\b"),),
    global_term=_NVDA_TERM, window=60,
)
_ANCHOR_NVDA_TARGET_PCT = AnchorSpec(
    key="concentration.nvda_target_pct", unit="pct",
    concept_any=(_p(
        r"\bsteering\s+target\b|\bips\s+sleeve\b|\bpolicy\s+target\b|\bpolicy\s+steering\b"
        r"|\bsingle-stock\s+(?:marker|target)\b|\bsteers?\s+nvda\b"
        r"|\bpolicy\s+marker\b|\bsteering\s+weight\b"
    ),),
    global_term=_NVDA_TERM, window=60,
)
_ANCHOR_NVDA_CURRENT_PCT = AnchorSpec(
    key="concentration.nvda_current_pct", unit="pct",
    concept_any=(_p(r"\bcurrent\s+(?:nvda\s+)?weight\b|\bcurrently\s+(?:at|holds?)\b|\bcurrent\s+concentration\b"),),
    exclude_any=(_p(r"\bhard\s+cap\b|\bsteering\s+target\b"),),
    global_term=_NVDA_TERM, window=40,
)

# Retirement / tax NIS margins — the "invented ₪209,389 margin" bug class.
# "net of realization" is a distinctive phrase (see fact_registry.py comment
# on retirement.fi_margin_net_of_realization_nis); the glide variant always
# co-occurs with "glide", so the two never cross-attribute the same number.
_ANCHOR_FI_MARGIN_NET_OF_REALIZATION = AnchorSpec(
    key="retirement.fi_margin_net_of_realization_nis", unit="nis",
    concept_any=(_p(r"net\s+of\s+realization"),),
    exclude_any=(_p(r"\bglide\b"),),
    window=100,
)
_ANCHOR_FI_MARGIN_NET_OF_REALIZATION_GLIDE = AnchorSpec(
    key="retirement.fi_margin_net_of_realization_glide_nis", unit="nis",
    concept_any=(_p(r"net\s+of\s+realization"),),
    global_term=_p(r"\bglide\b"), global_window=120,
    window=100,
)
_ANCHOR_FI_MARGIN_SIGNED = AnchorSpec(
    key="retirement.fi_margin_signed_nis", unit="nis",
    concept_any=(_p(r"\bfi\s+margin\b|\bsufficiency\s+margin\b"),),
    exclude_any=(_p(r"net\s+of\s+realization"),),
    window=60,
)

DEFAULT_ANCHORS: tuple[AnchorSpec, ...] = (
    _ANCHOR_NVDA_SELL_SH,
    _ANCHOR_NVDA_TARGET_SH,
    _ANCHOR_NVDA_ELIGIBLE_NOW_SH,
    _ANCHOR_NVDA_CAP_PCT,
    _ANCHOR_NVDA_TARGET_PCT,
    _ANCHOR_NVDA_CURRENT_PCT,
    _ANCHOR_FI_MARGIN_NET_OF_REALIZATION_GLIDE,
    _ANCHOR_FI_MARGIN_NET_OF_REALIZATION,
    _ANCHOR_FI_MARGIN_SIGNED,
)


# ---------------------------------------------------------------------------
# Value token regexes + candidate/tolerance/span logic, per unit.
# ---------------------------------------------------------------------------

# Share count: digits immediately followed by "shares"/"sh" — never a bare
# number (that would match any narrative integer of the same magnitude).
_SH_VALUE_RE = re.compile(r"(?<![\d,.])(\d[\d,]*)(?!\.\d)\s*(?:shares?\b|sh\b)", re.IGNORECASE)
_PCT_VALUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# Bare-fraction percent form ("0.12" for a 12% rate) — only 2-4 decimal
# digits, not immediately followed by '%' (that is the percent-sign form
# above) or preceded by another digit/decimal (avoid slicing "10.12").
_FRAC_VALUE_RE = re.compile(r"(?<![\d.])0\.(\d{2,4})(?!\d)(?!\s*%)")
_NIS_VALUE_RE = re.compile(r"₪\s*(\d[\d,]*(?:\.\d+)?)\s*([MmKk])?(?![A-Za-z])")


def _sh_candidate(m: re.Match) -> float | None:
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _pct_candidate_percent(m: re.Match) -> float | None:
    try:
        return float(m.group(1)) / 100.0
    except ValueError:
        return None


def _pct_candidate_frac(m: re.Match) -> float | None:
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _nis_candidate(m: re.Match) -> float | None:
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suf = (m.group(2) or "").lower()
    if suf == "m":
        return v * 1_000_000.0
    if suf == "k":
        return v * 1_000.0
    return v


def _sh_tol(canonical: float) -> float:
    return 0.5  # share counts are whole numbers — near-exact only


def _pct_tol(canonical: float) -> float:
    return 0.0005  # 0.05 percentage-point, in fraction units


def _nis_tol(canonical: float) -> float:
    return max(abs(canonical) * 0.005, 500.0)


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    candidate: Callable[[re.Match], float | None]
    tol: Callable[[float], float]
    # Which sub-span of the match gets replaced by the token: "full" (the
    # whole match, e.g. "₪2.07M" or "13.0%") or "group1" (just the digits,
    # e.g. "9,479" out of "9,479 shares" — the surrounding word stays).
    span: str


_UNIT_RULES: dict[str, tuple[_Rule, ...]] = {
    "sh": (_Rule(_SH_VALUE_RE, _sh_candidate, _sh_tol, "group1"),),
    "pct": (
        _Rule(_PCT_VALUE_RE, _pct_candidate_percent, _pct_tol, "full"),
        _Rule(_FRAC_VALUE_RE, _pct_candidate_frac, _pct_tol, "full"),
    ),
    "nis": (_Rule(_NIS_VALUE_RE, _nis_candidate, _nis_tol, "full"),),
}


def _span(m: re.Match, kind: str) -> tuple[int, int]:
    if kind == "group1":
        return m.start(1), m.end(1)
    return m.start(0), m.end(0)


# Clause boundary for the concept_any/exclude_any context — the SENTENCE the
# number is in, not a blind character window. Two share counts fifteen words
# apart but in DIFFERENT sentences ("...about 1,508 shares at current
# prices. The forward glide sells 9,417 shares...") must not cross-
# contaminate each other's anchor match just because they fall inside the
# same fixed-width window. Decimal-safe (won't split "13.0%" mid-number),
# mirrors numeric_source_gate._CLAUSE_BOUNDARY, EXTENDED with the comma: a
# single run-on prose sentence often stacks multiple, unrelated declarations
# ("...to an 8% single-stock marker inside the binding ceiling, anchors a US
# broad-market core of 28.5%, and diversifies across..." — one sentence, two
# completely different concepts). A comma is still never a boundary between
# digit groups (thousands separators, "9,479") thanks to the same
# digit-adjacency guard used for the other punctuation.
_CLAUSE_BOUNDARY = re.compile(r"(?<!\d)[.;!?,](?!\d)|\n")


def _clause_bounded_context(masked: str, start: int, end: int, window: int) -> str:
    lo = max(0, start - window)
    hi = min(len(masked), end + window)
    left = masked[lo:start]
    right = masked[end:hi]
    left_bounds = list(_CLAUSE_BOUNDARY.finditer(left))
    if left_bounds:
        left = left[left_bounds[-1].end():]
    right_bound = _CLAUSE_BOUNDARY.search(right)
    if right_bound is not None:
        right = right[: right_bound.start()]
    return left + masked[start:end] + right


def _anchor_ok(masked: str, start: int, end: int, spec: AnchorSpec) -> bool:
    ctx = _clause_bounded_context(masked, start, end, spec.window)
    if not any(p.search(ctx) for p in spec.concept_any):
        return False
    if any(p.search(ctx) for p in spec.exclude_any):
        return False
    if spec.global_term is not None:
        glo = max(0, start - spec.global_window)
        ghi = min(len(masked), end + spec.global_window)
        if not spec.global_term.search(masked[glo:ghi]):
            return False
    return True


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


@dataclass
class TokenizeResult:
    text: str
    violations: list[GateViolation] = field(default_factory=list)
    substitutions: list[tuple[str, str]] = field(default_factory=list)  # (key, literal)


def _is_match(candidate: float, canonical_value: float, tol: float) -> bool:
    """Signed-value magnitude matching (mirrors numeric_source_gate): a ₪/pct/
    sh literal in prose is often written UNSIGNED with the sign carried by the
    words ("short by ₪X", "-₪2.07M"); a negative canonical value also traces
    to the literal's bare magnitude. This only WIDENS what counts as a match
    for a genuinely-resolved negative value — it can never sanction an
    unrelated fabricated number (the tolerance band is unchanged)."""
    return abs(candidate - canonical_value) <= tol or (
        canonical_value < 0 and abs(candidate - abs(canonical_value)) <= tol
    )


def _canonical_value(resolved: "ResolvedPlanNumbers", key: str) -> tuple[float, str] | None:
    """Return ``(value, unit)`` for a RESOLVED registered fact, or None."""
    source_key = FACT_SOURCE_ALIAS.get(key, key)
    rv = resolved.get(source_key)
    if rv is None or getattr(rv, "status", None) != "resolved" or getattr(rv, "value", None) is None:
        return None
    return float(rv.value), getattr(rv, "unit", "")


def tokenize_text(
    text: str,
    resolved: "ResolvedPlanNumbers",
    *,
    anchors: tuple[AnchorSpec, ...] = DEFAULT_ANCHORS,
    horizon: str = "",
) -> TokenizeResult:
    """Scan ``text`` for literals anchored to a registered fact concept.

    A literal within tolerance of the canonical value is REWRITTEN to
    ``{{fact:key}}`` in place. A literal anchored to the concept but OUTSIDE
    tolerance is left untouched and reported as a
    ``GateCheck.FACT_LITERAL_DRIFT`` violation. Idempotent: already-tokenized
    text (or text with no anchored literals) round-trips unchanged.
    """
    if not text:
        return TokenizeResult(text=text)

    violations: list[GateViolation] = []
    substitutions: list[tuple[str, str]] = []
    working = text

    active: list[tuple[AnchorSpec, float, str]] = []
    for spec in anchors:
        canon = _canonical_value(resolved, spec.key)
        if canon is None:
            continue
        active.append((spec, canon[0], canon[1]))

    # Two phases, grouped by unit ("sh" / "pct" / "nis"): a single clause
    # commonly names TWO sibling concepts sharing the same unit together
    # ("the 8% steering target sits inside the 13% hard cap" — cap_target_
    # autocorrect's own canonical example). If concepts were scanned one at
    # a time end-to-end, an early spec would misfire "drift" on a literal
    # that actually belongs to a LATER spec in the same group, before that
    # later spec ever got a chance to correctly match it. Running every
    # spec's MATCH pass first (locking a correct match in via
    # {{fact:...}}-masking) before any spec's DRIFT pass starts means a
    # literal that truly matches some sibling concept is never available to
    # be mis-flagged as that sibling's drift.
    units = sorted({spec.unit for spec, _, _ in active})
    for unit in units:
        group = [(spec, cv, ru) for spec, cv, ru in active if spec.unit == unit]
        rules = _UNIT_RULES.get(unit, ())

        # Phase 1 — match-and-substitute only, across every spec in the group.
        for spec, canonical_value, _resolver_unit in group:
            for rule in rules:
                masked = _mask_protected_spans(working)
                matches = list(rule.regex.finditer(masked))
                for m in reversed(matches):  # right-to-left: positions stay valid
                    if not _anchor_ok(masked, m.start(0), m.end(0), spec):
                        continue
                    candidate = rule.candidate(m)
                    if candidate is None:
                        continue
                    if _is_match(candidate, canonical_value, rule.tol(canonical_value)):
                        start, end = _span(m, rule.span)
                        token = f"{{{{fact:{spec.key}}}}}"
                        working = working[:start] + token + working[end:]
                        substitutions.append((spec.key, m.group(0).strip()))

        # Phase 2 — drift-only, on whatever's left after every match in the
        # group has been tokenized away.
        for spec, canonical_value, resolver_unit in group:
            for rule in rules:
                masked = _mask_protected_spans(working)
                for m in reversed(list(rule.regex.finditer(masked))):
                    if not _anchor_ok(masked, m.start(0), m.end(0), spec):
                        continue
                    candidate = rule.candidate(m)
                    if candidate is None:
                        continue
                    if _is_match(candidate, canonical_value, rule.tol(canonical_value)):
                        continue  # matched a DIFFERENT spec's canonical — not this one's drift
                    rendered = format_fact(
                        canonical_value, resolver_unit, display=FACT_DISPLAY[spec.key]
                    )
                    violations.append(
                        GateViolation(
                            check=GateCheck.FACT_LITERAL_DRIFT,
                            detail=(
                                f"literal `{m.group(0).strip()}` near concept `{spec.key}` "
                                f"diverges from canonical {rendered} — surfaced, NOT "
                                "auto-corrected"
                            ),
                            locator=(
                                f"horizon={horizon} pos={m.start(0)}" if horizon
                                else f"pos={m.start(0)}"
                            ),
                        )
                    )

    return TokenizeResult(text=working, violations=violations, substitutions=substitutions)


def tokenize_bodies(
    bodies: dict[str, str],
    resolved: "ResolvedPlanNumbers",
    *,
    anchors: tuple[AnchorSpec, ...] = DEFAULT_ANCHORS,
) -> tuple[dict[str, str], list[GateViolation], list[tuple[str, str, str]]]:
    """Apply :func:`tokenize_text` to every horizon body.

    Returns ``(new_bodies, violations, substitutions)`` where
    ``substitutions`` is ``[(horizon, key, literal), ...]`` across all
    bodies, for phase-completion logging."""
    new_bodies: dict[str, str] = {}
    all_violations: list[GateViolation] = []
    all_subs: list[tuple[str, str, str]] = []
    for horizon, text in (bodies or {}).items():
        result = tokenize_text(text or "", resolved, anchors=anchors, horizon=horizon)
        new_bodies[horizon] = result.text
        all_violations.extend(result.violations)
        all_subs.extend((horizon, key, literal) for key, literal in result.substitutions)
    return new_bodies, all_violations, all_subs


__all__ = [
    "AnchorSpec",
    "DEFAULT_ANCHORS",
    "TokenizeResult",
    "tokenize_text",
    "tokenize_bodies",
]
