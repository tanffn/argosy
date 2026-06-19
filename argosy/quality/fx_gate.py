"""FX unit/direction gate.

The USD/NIS rate must be expressed as NIS-per-USD (~3.0). The recurring synth
defect is the INVERTED rate (~0.33, USD-per-NIS) or a rate accidentally
rendered as a percent ("USD/NIS 0.34%"). Either mislabels the currency and
poisons every downstream NIS↔USD conversion.

The 2.5–4.5 band used here is a PLAUSIBILITY guardrail — a sanity range wide
enough to never reject a real NIS/USD print, narrow enough to catch inversion,
percent-misrender, and absurd values. It is NOT a financial constant or a
forecast; it is a rounding/plausibility band documented as such.

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
coherence_gate convention.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Plausibility band for a real NIS-per-USD print. A sanity guardrail, not a
# financial constant: anything outside this is structurally wrong (inverted,
# percent-misrendered, or absurd), never just "an unusual but valid rate".
_FX_PLAUSIBLE_LO = 2.5
_FX_PLAUSIBLE_HI = 4.5

# Find a "USD/NIS" (or "USD/ILS") label followed within ~15 chars by a number,
# tolerating connective words like "of" / "at" / "=" / ":". Captures the number
# and an optional trailing '%' so we can tell a mislabeled-percent from a bare
# rate. ~15-char window keeps the number bound to its label, not a later figure.
_FX_LABEL_NUM_RE = re.compile(
    r"USD\s*/\s*(?:NIS|ILS)"      # the pair label (NIS or ILS alias)
    r"(?P<gap>[^0-9]{0,15}?)"     # up to ~15 non-digit chars ("of", "at", ":", "=")
    r"(?P<num>\d+(?:\.\d+)?)"     # the rate number
    r"\s*(?P<pct>%?)",            # optional immediate percent sign
    re.IGNORECASE,
)

# A currency sign in the gap means the bound number is an AMOUNT, not the rate
# ("every 0.10 move in USD/NIS = ₪386,527 of net worth" — ₪386,527 is the
# sensitivity, not the pair value). A bare "USD/NIS 3.02" has no sign → still scanned.
_CURRENCY_SIGNS = ("₪", "$", "€", "£", "¥")

# A duration unit right after the number means it is a WINDOW length, not a rate
# ("BOI USD/NIS 90-day low → high" — 90 is the look-back, the rate is stated
# separately). Anchored at the char after the number so only an immediate suffix counts.
_DURATION_SUFFIX_RE = re.compile(
    r"^\s*-?\s*(?:day|days|week|weeks|month|months|year|years|yr|yrs|quarter|quarters)\b",
    re.IGNORECASE,
)


def check_fx_unit_direction(
    *, plan_text: str, fx_usd_nis: float | None = None
) -> list[GateViolation]:
    """Flag an inverted / mislabeled / out-of-band USD/NIS rate.

    Two independent inputs:
      - ``fx_usd_nis``: the resolved numeric rate, when available. Outside the
        [2.5, 4.5] plausibility band → violation (inverted ~0.33, a percent like
        0.34, or absurd).
      - ``plan_text``: scanned for "USD/NIS" / "USD/ILS" + a nearby number. A
        number rendered as a percent (immediate ``%``) → violation (mislabeled);
        a number < 1.0 → violation (inverted USD-per-NIS); a bare number outside
        the plausibility band → violation (out of band).
    """
    violations: list[GateViolation] = []

    if fx_usd_nis is not None and not (_FX_PLAUSIBLE_LO <= fx_usd_nis <= _FX_PLAUSIBLE_HI):
        violations.append(
            GateViolation(
                check=GateCheck.FX_UNIT_DIRECTION,
                detail=(
                    f"USD/NIS resolved as {fx_usd_nis} — outside the "
                    f"{_FX_PLAUSIBLE_LO}–{_FX_PLAUSIBLE_HI} NIS/USD plausibility band. "
                    "Likely inverted (USD-per-NIS ~0.33), a percent, or absurd; the "
                    "rate must be NIS-per-USD (~3.0)."
                ),
                locator="fx_usd_nis",
            )
        )

    text = plan_text or ""
    for m in _FX_LABEL_NUM_RE.finditer(text):
        # Carve-out 1: a currency sign in the gap → the number is an AMOUNT, not
        # the rate (an FX-sensitivity ₪/$ figure stated next to the pair label).
        if any(sign in m.group("gap") for sign in _CURRENCY_SIGNS):
            continue
        # Carve-out 2: a duration unit immediately after → a WINDOW length (e.g.
        # "USD/NIS 90-day"), not the pair value.
        if _DURATION_SUFFIX_RE.match(text[m.end():]):
            continue
        num = float(m.group("num"))
        is_percent = m.group("pct") == "%"
        if is_percent:
            violations.append(
                GateViolation(
                    check=GateCheck.FX_UNIT_DIRECTION,
                    detail=(
                        f"'{m.group(0).strip()}' renders USD/NIS as a PERCENT — the "
                        "pair is a NIS-per-USD rate (~3.0), not a percentage."
                    ),
                    locator="fx_usd_nis",
                )
            )
        elif num < 1.0:
            violations.append(
                GateViolation(
                    check=GateCheck.FX_UNIT_DIRECTION,
                    detail=(
                        f"'{m.group(0).strip()}' renders USD/NIS as {num} (< 1.0) — "
                        "that is the INVERTED USD-per-NIS direction; state NIS-per-USD "
                        "(~3.0)."
                    ),
                    locator="fx_usd_nis",
                )
            )
        elif not (_FX_PLAUSIBLE_LO <= num <= _FX_PLAUSIBLE_HI):
            violations.append(
                GateViolation(
                    check=GateCheck.FX_UNIT_DIRECTION,
                    detail=(
                        f"'{m.group(0).strip()}' renders USD/NIS as {num} — outside the "
                        f"{_FX_PLAUSIBLE_LO}–{_FX_PLAUSIBLE_HI} NIS/USD plausibility band."
                    ),
                    locator="fx_usd_nis",
                )
            )
    return violations
