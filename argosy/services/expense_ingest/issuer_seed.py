"""Issuer-seeded category mapping for cards that pre-categorize (Max).

Two outcomes:
  * UNAMBIGUOUS: map directly to one slug with calibrated confidence.
  * AMBIGUOUS: defer to the LLM, passing the original Hebrew label as a hint.

When sample data shows new ענף values, extend the unambiguous map.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IssuerSeedResult:
    slug: str | None
    confidence: float
    defer_to_llm: bool
    hint: str | None


_UNAMBIGUOUS: dict[str, tuple[str, float]] = {
    "מסעדות":             ("dining_out.restaurants",         0.90),
    "תיירות":             ("travel.vacation_other",          0.85),
    "רפואה ובריאות":      ("healthcare.medical_other",       0.85),
    "ריהוט ובית":         ("housing.home_maintenance",       0.80),
    "סופרמרקטים":         ("food.groceries",                 0.90),
    "חנויות מזון":        ("food.groceries",                 0.90),
    "דלק ותחנות דלק":     ("transportation.fuel",            0.95),
    "לבוש והנעלה":        ("discretionary.shopping_clothing", 0.90),
    "בידור ותרבות":       ("discretionary.entertainment",    0.85),
}

_AMBIGUOUS: set[str] = {
    "ביטוח ופיננסים",
    "תקשורת ומחשבים",
    "מקצועות חופשיים",
}


def map_issuer_category(anaf: str | None) -> IssuerSeedResult:
    if anaf is None:
        return IssuerSeedResult(slug=None, confidence=0.0,
                                defer_to_llm=False, hint=None)
    anaf = anaf.strip()
    if anaf in _UNAMBIGUOUS:
        slug, conf = _UNAMBIGUOUS[anaf]
        return IssuerSeedResult(slug=slug, confidence=conf,
                                defer_to_llm=False, hint=None)
    if anaf in _AMBIGUOUS:
        return IssuerSeedResult(slug=None, confidence=0.50,
                                defer_to_llm=True, hint=anaf)
    return IssuerSeedResult(slug=None, confidence=0.40,
                            defer_to_llm=True, hint=anaf)
