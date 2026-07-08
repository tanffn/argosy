"""Deterministic skeleton gate for sliced full synthesis (stage A).

Design: docs/design/sliced_full_synthesis.md §2.A.

Runs BEFORE the six-way expansion fan-out — the "cheap early gate" that
converts the most expensive downstream rejection class (a wrong headline
number discovered after 60k output tokens) into a ~3-minute skeleton retry.

Pure / deterministic — no DB, no LLM, no network. Same doctrine as
``patch_reachability.py`` / ``blast_radius.py``: the gate verifies values
against the deterministic manifest and structural invariants; it NEVER
decides what is *right* (the blind gates own that). FULL-first spirit:
checks run in design order and ALL violations are collected (the single
skeleton retry gets every violation fed back at once).

Checks (design §2.A):

1. Every headline ``SynthTarget`` value matches the resolved-numbers
   manifest (``plan_numeric_resolver.resolve_plan_numbers``) — subject-bound
   like ``numeric_source_gate`` (only targets whose LABEL binds to a
   headline subject are checked, against resolved values of the bound unit
   class within the same display-rounding tolerance). ``[derivation
   pending]`` in section key_facts is the sanctioned escape hatch (the
   key-fact text scan reuses ``check_headline_numeric_source``).
2. Corrective mode: every correction's canonical value present in the
   skeleton, wrong/superseded values absent (``corrections_check``
   variants + presence semantics).
3. Delta ``item_id``s resolve against the prior-items index or are
   well-formed new ids; ``status=no_change`` ⇒ empty delta roster for that
   horizon.
4. Section coverage >= the MVP floor (>=12 of 18 canonical ids);
   speculation roster within the cap (short-horizon-only, count, pct).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from argosy.quality.corrections_check import _present, value_variants
from argosy.quality.numeric_source_gate import (
    PENDING_LABEL,
    _matches,
    check_headline_numeric_source,
)

if TYPE_CHECKING:  # pragma: no cover — typing only, keeps the module pure
    from argosy.agents.plan_skeleton_synthesizer import PlanSkeleton
    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers

HORIZONS = ("long", "medium", "short")

# MVP coverage floor — >=12 of the 18 canonical section ids (design §2.A #4).
DEFAULT_COVERAGE_FLOOR = 12

# Well-formed new delta ids: <horizon>.<kind-plural>.<slug> (the synthetic-id
# scheme shared with patch_reachability / _pkg_build_prior_items_index).
_NEW_ID_RE = re.compile(
    r"^(long|medium|short)\."
    r"(targets|themes|actions|speculative_candidates)\."
    r"[a-z0-9][a-z0-9_.-]*$"
)


# ---------------------------------------------------------------------------
# Check 1 — headline targets vs the resolved-numbers manifest.
#
# Subject binding mirrors numeric_source_gate's option-B doctrine: only a
# target whose LABEL states a headline subject is checked (narrative /
# detail targets — an emergency-fund size, a review-in-30-days window — are
# left alone). The bound value must match SOME resolved value of the
# subject's unit class within the shared display-rounding tolerance.
# ---------------------------------------------------------------------------

# (label regex, manifest unit class, manifest key prefixes). A bound target
# is checked ONLY against resolved values under its subject's own keys —
# class-wide pooling would let an NVDA weight "match" an unrelated resolved
# rate (e.g. a statutory tax percentage), or false-flag it against one.
_HEADLINE_LABEL_BINDINGS: tuple[
    tuple[re.Pattern[str], str, tuple[str, ...]], ...
] = (
    (re.compile(r"\bnvda\b.*\b(?:target|weight|cap|concentration)\b|"
                r"\b(?:target|weight|cap|concentration)\b.*\bnvda\b",
                re.IGNORECASE), "pct", ("concentration.",)),
    (re.compile(r"\bnvda\b", re.IGNORECASE), "shares", ("concentration.",)),
    (re.compile(r"\bfi\b.*\btarget\b|\bfinancial\s+independence\b|"
                r"\bnet\s+worth\b|\bcapital\s+target\b|\bnest\s+egg\b",
                re.IGNORECASE), "nis", ("retirement.", "portfolio.")),
    (re.compile(r"\b(?:retirement|fi)\s+age\b|\bearliest\b.*\bage\b",
                re.IGNORECASE), "age", ("retirement.",)),
    (re.compile(r"\bswr\b|\bwithdrawal\s+rate\b|\breal\s+yield\b|"
                r"\breturn\s+assumption\b", re.IGNORECASE), "pct",
     ("retirement.",)),
)

# SynthTarget unit → manifest unit class it can be checked against.
_TARGET_UNIT_CLASS: dict[str, str] = {
    "pct": "pct",
    "pct_of_portfolio": "pct",
    "pct_of_net_worth": "pct",
    "pct_of_liquid": "pct",
    "nis": "nis",
    "shares": "shares",
    "years": "age",
}

# Resolver unit → manifest unit class.
_RESOLVED_UNIT_CLASS: dict[str, str] = {
    "pct": "pct",
    "nis": "nis",
    "shares": "shares",
    "age": "age",
    "year": "year",
}


def _bind_target(
    label: str, unit: str,
) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(unit_class, key_prefixes)`` this target's label binds to,
    or None when the target is not a headline subject (left alone)."""
    unit_class = _TARGET_UNIT_CLASS.get(unit)
    if unit_class is None:
        return None
    for pattern, bound_class, prefixes in _HEADLINE_LABEL_BINDINGS:
        if bound_class == unit_class and pattern.search(label or ""):
            return bound_class, prefixes
    return None


def _shares_match(candidate: float, resolved: float) -> bool:
    # Shares have no display-rounding class in numeric_source_gate; use the
    # same relative band with a 1-share absolute floor.
    tol = max(abs(resolved) * 0.015, 1.0)
    return abs(candidate - resolved) <= tol


def _subject_pool(
    resolved: ResolvedPlanNumbers, cls: str, prefixes: tuple[str, ...],
) -> list[float]:
    pool: list[float] = []
    for key, rv in resolved.values.items():
        if rv.status != "resolved" or rv.value is None:
            continue
        if _RESOLVED_UNIT_CLASS.get(rv.unit) != cls:
            continue
        if not any(key.startswith(p) for p in prefixes):
            continue
        pool.append(float(rv.value))
    return pool


def _value_matches_pool(value: float, cls: str, pool: list[float]) -> bool:
    if not pool:
        # Nothing resolved for this subject → nothing to check against; the
        # derivation-ownership prose rule + the post-assembly scrub own it.
        return True
    if cls == "shares":
        return any(_shares_match(value, rv) for rv in pool)
    if cls == "pct":
        # Resolver pct values may be stored as FRACTIONS (0.08) or percent
        # points (8.0) while SynthTarget values are percent points (codex
        # sliced review blocker #2). Unlike numeric_source_gate._traces
        # (which accepts rv*100 unconditionally and therefore lets a
        # 100x-wrong value through — codex r2), the *100 form is applied
        # ONLY to fraction-looking resolved values (rv <= 1.0): a
        # percent-points 8.0 never accepts 800.0. Residual ambiguity: for
        # rv <= 1.0 both readings are accepted because the resolver's
        # convention cannot be recovered from the value alone.
        return any(
            _matches(value, rv, cls)
            or (abs(rv) <= 1.0 and _matches(value, rv * 100.0, cls))
            for rv in pool
        )
    return any(_matches(value, rv, cls) for rv in pool)


# ---------------------------------------------------------------------------
# Occurrence variants — the skeleton haystack is a model_dump_json, where an
# integer value in a float field renders "4136.0" (same widening as
# patch_reachability).
# ---------------------------------------------------------------------------


_NUMERIC_STR_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _occurrence_variants(value: Any) -> list[str]:
    variants = list(value_variants(value))
    if isinstance(value, bool):
        return variants
    if isinstance(value, int):
        variants.append(f"{value}.0")
    if isinstance(value, float) and value == int(value):
        variants.append(f"{int(value)}.0")
    if isinstance(value, str):
        # A numeric-looking string ("3.00", "2.944") must also match its
        # float-JSON rendering in the skeleton dump ("3.0" / "2.944") —
        # the live FX-rate correction class arrives as a string.
        s = value.strip().replace(",", "")
        if _NUMERIC_STR_RE.match(s):
            f = float(s)
            widened = f"{int(f)}.0" if f == int(f) else f"{f:g}"
            if widened not in variants:
                variants.append(widened)
    return variants


def _occurs(value: Any, text: str) -> bool:
    return any(_present(v, text) for v in _occurrence_variants(value))


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SkeletonGateResult:
    violations: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return not self.violations

    def render_violations_block(self) -> str:
        return "\n".join(f"- {v}" for v in self.violations)

    def to_payload(self) -> dict[str, Any]:
        return {"passes": self.passes, "violations": list(self.violations)}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def check_skeleton(
    *,
    skeleton: PlanSkeleton,
    resolved: ResolvedPlanNumbers | None = None,
    corrections: list[dict[str, Any]] | None = None,
    directives: list[dict[str, Any]] | None = None,
    prior_item_ids: set[str] | None = None,
    coverage_floor: int = DEFAULT_COVERAGE_FLOOR,
    speculation_cap_pct: float | None = None,
    speculation_cap_concurrent: int | None = None,
) -> SkeletonGateResult:
    """Run the deterministic pre-fan-out gate over a stage-A skeleton.

    ``corrections`` / ``directives`` are ``check_payload()`` dict shapes
    (``{"index", "topic", "canonical_values", "wrong_values"}``) so this
    module keeps no import edge back into services. ``prior_item_ids`` is
    the set of item_ids from the prior-items index (delta lineage floor).
    """
    result = SkeletonGateResult()
    skeleton_text = skeleton.model_dump_json()

    # ---- 1. headline targets + key-fact text vs the manifest -------------
    if resolved is not None:
        for horizon in HORIZONS:
            hz = getattr(skeleton, horizon)
            for t in hz.targets:
                bound = _bind_target(t.label, t.unit)
                if bound is None:
                    continue
                cls, prefixes = bound
                pool = _subject_pool(resolved, cls, prefixes)
                if not _value_matches_pool(float(t.value), cls, pool):
                    result.violations.append(
                        f"[manifest] {horizon} target {t.label!r} value "
                        f"{t.value} {t.unit} matches no resolved "
                        f"{cls}-class value in the derived-numbers "
                        "manifest — use the DERIVED HEADLINE NUMBERS "
                        f"verbatim or state {PENDING_LABEL!r}"
                    )
        # Section key_facts are free text — reuse the markdown headline
        # checker (PENDING_LABEL is its sanctioned escape hatch).
        key_fact_text = "\n".join(
            f"{e.one_line_thesis}\n" + "\n".join(e.key_facts)
            for e in skeleton.section_roster
        )
        try:
            for v in check_headline_numeric_source(
                {"skeleton_sections": key_fact_text}, resolved,
            ):
                # "(resolver pending)" marks a unit class with NOTHING
                # resolved — same semantics as the empty subject pool
                # above: nothing to check against, the derivation-
                # ownership rule + the post-assembly scrub own it. Only
                # a value that CONTRADICTS an actually-resolved class
                # fails the skeleton.
                if "(resolver pending)" in v.detail:
                    continue
                result.violations.append(
                    f"[manifest] section key_facts: {v.detail}"
                )
        except Exception:  # noqa: BLE001 — advisory scan, never crash the gate
            pass

    # ---- 2. corrective values ---------------------------------------------
    for c in corrections or []:
        idx = c.get("index")
        topic = c.get("topic") or "?"
        for wv in c.get("wrong_values") or []:
            if wv is None:
                continue
            if _occurs(wv, skeleton_text):
                result.violations.append(
                    f"[corrective] correction [{idx}] {topic}: wrong value "
                    f"{wv!r} still present in the skeleton — it must be "
                    "absent"
                )
        canonical = [
            v for v in (c.get("canonical_values") or []) if v is not None
        ]
        missing = [cv for cv in canonical if not _occurs(cv, skeleton_text)]
        if missing:
            result.violations.append(
                f"[corrective] correction [{idx}] {topic}: canonical "
                f"value(s) {missing!r} absent from the skeleton — the "
                "skeleton is where numbers are decided; state them in the "
                "targets / key_facts"
            )
    for d in directives or []:
        idx = d.get("index")
        for sv in d.get("wrong_values") or []:
            if sv is None:
                continue
            if _occurs(sv, skeleton_text):
                result.violations.append(
                    f"[corrective] directive [D{idx}]: superseded value "
                    f"{sv!r} still present in the skeleton — apply the "
                    "adjudicated verdict verbatim"
                )

    # ---- 3. delta roster ----------------------------------------------------
    prior_ids = prior_item_ids or set()
    for delta in skeleton.delta_roster:
        hz = getattr(skeleton, delta.horizon, None)
        if hz is not None and hz.status == "no_change":
            result.violations.append(
                f"[deltas] horizon {delta.horizon!r} has status=no_change "
                f"but delta {delta.item_id!r} is in the roster — no_change "
                "means an EMPTY delta roster for that horizon"
            )
        if delta.item_id in prior_ids:
            continue
        if delta.change_kind == "removed":
            # A removal must reference a PRIOR item — a made-up id removes
            # nothing.
            result.violations.append(
                f"[deltas] removed-delta item_id {delta.item_id!r} does "
                "not resolve against the prior-items index — removals must "
                "use the original item_id"
            )
            continue
        if not _NEW_ID_RE.match(delta.item_id):
            result.violations.append(
                f"[deltas] delta item_id {delta.item_id!r} neither "
                "resolves against the prior-items index nor is a "
                "well-formed new id (<horizon>.<kind>.<slug>)"
            )
        elif not delta.item_id.startswith(delta.horizon + "."):
            result.violations.append(
                f"[deltas] delta item_id {delta.item_id!r} does not match "
                f"its declared horizon {delta.horizon!r}"
            )

    # ---- 4. coverage + speculation cap ---------------------------------------
    distinct_sections = {e.section_id for e in skeleton.section_roster}
    if len(distinct_sections) < coverage_floor:
        result.violations.append(
            f"[coverage] section roster covers {len(distinct_sections)} "
            f"distinct canonical section_ids — the floor is "
            f"{coverage_floor} of 18"
        )
    for horizon in ("long", "medium"):
        hz = getattr(skeleton, horizon)
        if hz.speculative_candidates:
            result.violations.append(
                f"[speculation] {horizon} horizon carries "
                f"{len(hz.speculative_candidates)} speculative candidate(s) "
                "— speculation is short-horizon only"
            )
    short_cands = skeleton.short.speculative_candidates
    if (
        speculation_cap_concurrent is not None
        and len(short_cands) > speculation_cap_concurrent
    ):
        result.violations.append(
            f"[speculation] {len(short_cands)} candidates exceed the "
            f"concurrent-position cap ({speculation_cap_concurrent})"
        )
    if speculation_cap_pct is not None:
        for cand in short_cands:
            if cand.suggested_position_pct_of_net_worth > speculation_cap_pct:
                result.violations.append(
                    f"[speculation] candidate {cand.ticker!r} at "
                    f"{cand.suggested_position_pct_of_net_worth} of net "
                    f"worth exceeds the cap ({speculation_cap_pct})"
                )
            if not cand.risk_ceiling_check:
                result.violations.append(
                    f"[speculation] candidate {cand.ticker!r} lacks "
                    "risk_ceiling_check=true"
                )

    return result


__all__ = [
    "DEFAULT_COVERAGE_FLOOR",
    "SkeletonGateResult",
    "check_skeleton",
]
