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
    r"[^0-9]{0,15}?"              # up to ~15 non-digit chars ("of", "at", ":", "=")
    r"(\d+(?:\.\d+)?)"            # the rate number
    r"\s*(%?)",                   # optional immediate percent sign
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

    for m in _FX_LABEL_NUM_RE.finditer(plan_text or ""):
        num = float(m.group(1))
        is_percent = m.group(2) == "%"
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
