"""Deterministic corrections-landed verification (corrective re-synthesis, part C).

Design: docs/design/corrective_resynthesis.md §2.C.1.

For each correction with a canonical VALUE, check that the value appears in
(and any known-wrong value is absent from) the draft's surfaces — the rendered
horizon markdown + ``target_allocation_json`` + the structured horizon JSON.
Pure lookup/arithmetic: this is the inviolable-arithmetic floor, not a
judgment gate. The judgment pass (is the correction resolved IN SUBSTANCE?)
belongs to the whole-artifact reader, which receives the corrections as a
directive; determinism here only VERIFIES values landed — it never decides
what is correct.

Pure module (no DB, no LLM) — mirrors ``argosy/quality/leakage_gate.py``.
Corrections arrive as plain dicts (``Correction.check_payload()`` shape:
``{"index", "topic", "plan_item_ref", "canonical_values", "wrong_values"}``)
so this module has no import edge back into services.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CorrectionCheck:
    """Outcome for one correction."""

    index: int
    topic: str
    landed: bool
    reason: str


@dataclass
class CorrectionsCheckResult:
    checks: list[CorrectionCheck] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return all(c.landed for c in self.checks)

    def unresolved_payload(self) -> list[dict[str, Any]]:
        """The ``corrective_unresolved`` entries persisted to
        ``synthesis_inputs_json`` (and read by the accept-route gate)."""
        return [
            {"index": c.index, "topic": c.topic, "reason": c.reason}
            for c in self.checks
            if not c.landed
        ]

    def summary(self) -> str:
        unresolved = [c for c in self.checks if not c.landed]
        if not unresolved:
            return (
                f"corrective_check: {len(self.checks)} correction(s), all landed"
            )
        detail = "; ".join(
            f"[{c.index}] {c.topic or '?'}: {c.reason}" for c in unresolved
        )
        return (
            f"corrective_check: {len(self.checks)} correction(s), "
            f"{len(unresolved)} UNRESOLVED — {detail}"
        )


def value_variants(value: Any) -> list[str]:
    """Deterministic textual variants a canonical value may render as.

    Integers match both plain and comma-grouped forms ("4136" / "4,136");
    floats match their shortest repr plus a trailing-zero-stripped 2dp form;
    strings match themselves (stripped). Conservative on purpose — a fuzzy
    matcher has no place in a deterministic gate.
    """
    if isinstance(value, bool):
        return [str(value)]
    if isinstance(value, int):
        return list(dict.fromkeys([str(value), f"{value:,}"]))
    if isinstance(value, float):
        if value == int(value):
            return list(dict.fromkeys([str(int(value)), f"{int(value):,}"]))
        variants = [f"{value:g}"]
        two_dp = f"{value:.2f}".rstrip("0").rstrip(".")
        if two_dp not in variants:
            variants.append(two_dp)
        return variants
    s = str(value).strip()
    return [s] if s else []


_NUMERIC_VARIANT_RE = re.compile(r"^[\d.,-]+$")


def _present(variant: str, text: str) -> bool:
    """Substring presence with digit-boundary guards for numeric variants
    (so canonical "4136" does not match inside "14136" or "41365")."""
    if not variant:
        return False
    if _NUMERIC_VARIANT_RE.match(variant):
        # Boundary guards: no digit/separator-digit run may continue the
        # match on either side — "4136" must not match inside "14136",
        # "41365", or "4136.5" (codex finding #10) — while ordinary prose
        # punctuation ("sell 4,136, retaining" / "of 4,136.") still counts.
        pattern = (
            r"(?<![0-9.,])"
            + re.escape(variant)
            + r"(?!(?:[0-9]|[.,][0-9]))"
        )
        return re.search(pattern, text) is not None
    return variant in text


def check_corrections_landed(
    *,
    corrections: list[dict[str, Any]],
    surfaces: dict[str, str],
) -> CorrectionsCheckResult:
    """Run the deterministic floor over the draft's surfaces.

    Per correction:
      * any known-wrong value still present anywhere → NOT landed
        (this also catches the cosmetically-absorbed case: canonical value
        pasted in while the contradicting figure survives elsewhere);
      * canonical values known and NONE present in any surface → NOT landed;
      * no canonical/wrong values on file → landed by this floor (the reader's
        judgment pass owns substance-only corrections).
    """
    haystack = "\n".join(v or "" for v in surfaces.values())
    result = CorrectionsCheckResult()
    for c in corrections:
        idx = int(c.get("index") or 0)
        topic = str(c.get("topic") or "")
        wrong = [v for v in (c.get("wrong_values") or []) if v is not None]
        canonical = [v for v in (c.get("canonical_values") or []) if v is not None]

        wrong_hit: str | None = None
        for wv in wrong:
            for variant in value_variants(wv):
                if _present(variant, haystack):
                    wrong_hit = variant
                    break
            if wrong_hit:
                break
        if wrong_hit:
            result.checks.append(CorrectionCheck(
                index=idx, topic=topic, landed=False,
                reason=f"wrong value {wrong_hit!r} still present in the draft",
            ))
            continue

        if canonical:
            # EVERY canonical value must land (codex blocker #5 — "any of"
            # let a draft absorb one figure of a multi-value correction,
            # e.g. one leg of a three-year glide schedule, and pass).
            missing = [
                cv for cv in canonical
                if not any(
                    _present(variant, haystack)
                    for variant in value_variants(cv)
                )
            ]
            if missing:
                result.checks.append(CorrectionCheck(
                    index=idx, topic=topic, landed=False,
                    reason=(
                        "canonical value(s) "
                        + ", ".join(
                            repr(value_variants(cv)[-1]) for cv in missing
                        )
                        + " absent from every draft surface"
                    ),
                ))
                continue

        result.checks.append(CorrectionCheck(
            index=idx, topic=topic, landed=True,
            reason=(
                "canonical value present, no wrong value found"
                if canonical or wrong
                else "no deterministic value to check (reader judgment pass owns it)"
            ),
        ))
    return result


__all__ = [
    "CorrectionCheck",
    "CorrectionsCheckResult",
    "check_corrections_landed",
    "value_variants",
]
