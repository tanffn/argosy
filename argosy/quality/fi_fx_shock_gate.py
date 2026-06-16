"""Run-106 finding [0] — FX-shock twin of the NVDA-shock sufficiency gate.

The plan asserts capital/FI sufficiency "reached" (same reached-claim idea as
``coherence_gate._REACHED_RE``) but a −10% FX (USD/NIS) shock row shows the
surplus no longer clears the perpetuity base — AND the same sentence carries
NO FX/currency caveat. That is a sufficiency claim true only at the current FX
mark; the existing ``FI_SHOCK_SUFFICIENCY`` covers the NVDA tail, this extends
the identical idea to the currency dimension the run-106 reader flagged as
load-bearing.

Sentence-scoped and biased to FALSE-POSITIVE per Argosy's fail-loud doctrine:
if the FX caveat lives in a *different* sentence we still flag (a spurious flag
is safer than letting an unqualified claim through). A negated clause ("FI is
not yet reached", "below the base") is a DENIAL of sufficiency, not an
assertion, and is not flagged.

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
coherence_gate / fx_gate convention.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# The −10% FX-shock row label in ``fx_shock_result`` (mirrors the NVDA gate's
# "shock_0.30" key shape, but for the currency dimension).
_FX_SHOCK_KEY = "fx_shock_-0.10"

# An unqualified assertion that capital sufficiency / FI is reached. Mirrors
# coherence_gate._REACHED_RE exactly — the same reached-claim detection idea,
# duplicated here to keep this gate a standalone pure function (no import of a
# private name from a sibling gate).
_REACHED_RE = re.compile(
    r"(?:"
    r"capital sufficiency\s*:?\s*reached"
    r"|sufficiency\s*:?\s*reached"
    r"|\bfi\b\s*:?\s*reached"
    r"|\bfi\b[^.!?]{0,60}\breached\b"
    r"|reached[^.!?]{0,60}financial independence"
    r"|financial independence[^.!?]{0,60}reached"
    r"|financially independent"
    r"|(?:full )?(?:financial|capital) sufficiency[^.!?]{0,40}(?:achieved|reached)"
    r")",
    re.IGNORECASE,
)

# A caveat that the "reached" claim is conditional on the FX / currency mark.
# Matches a currency/FX/shekel/USD-NIS/−10% qualifier near the claim so a
# properly-qualified sentence passes: e.g. "a −10% USD/NIS move would erase the
# surplus", "at the current shekel mark", "conditional on the FX rate".
_FX_SHOCK_QUALIFIER_RE = re.compile(
    r"(?:"
    r"usd\s*/\s*(?:nis|ils)"           # the currency pair label
    r"|nis\s*/\s*usd"
    r"|\bfx\b"                          # "FX move", "FX shock"
    r"|currenc(?:y|ies)"               # "currency move"
    r"|shekel|shekels"                 # the NIS name
    r"|exchange rate"
    r"|[-−]10%|[-−]0\.10"              # the explicit −10% / −0.10 shock magnitude
    r")",
    re.IGNORECASE,
)

# A negation near the "reached" token that turns the clause into a DENIAL of
# sufficiency ("FI is not reached", "not yet reached", "below the base", "short
# of the target") — must NOT be flagged. Same set as coherence_gate._NEGATION_RE.
_NEGATION_RE = re.compile(
    r"\b(?:not|isn't|is not|won't|will not|not yet|no longer|below|short of|"
    r"fails? to|does not|doesn't|never)\b",
    re.IGNORECASE,
)

# Split into sentence-ish clauses on terminal punctuation / newlines.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def check_fi_sufficiency_under_fx_shock(
    *, fx_shock_result: dict, plan_text: str
) -> list[GateViolation]:
    """Fail an unqualified "FI reached" claim that a −10% FX shock breaks.

    ``fx_shock_result`` mirrors the NVDA gate's shape: a dict keyed by an
    FX-shock label (``"fx_shock_-0.10"``) → a dict with ``perpetuity_reached:
    bool`` and ``net_worth_nis: float``.

    Contract:
      - If the −10% FX row's ``perpetuity_reached`` is not ``False`` (True, or
        absent) → ``[]`` (the surplus still clears the base; nothing to flag).
      - Otherwise, scan each sentence for a non-negated sufficiency assertion
        (``_REACHED_RE``) that lacks an FX/currency caveat
        (``_FX_SHOCK_QUALIFIER_RE``) in the SAME sentence → one violation.
      - A negated clause is a denial, not an assertion → skip it.
      - A caveat in a different sentence does NOT save the bare claim
        (fail-loud, sentence-scoped).

    One violation is emitted even if several sentences match — the claim class
    is established once.
    """
    violations: list[GateViolation] = []
    text = plan_text or ""
    fx_row = fx_shock_result.get(_FX_SHOCK_KEY) or {}
    breaks_perpetuity = fx_row.get("perpetuity_reached") is False
    if not breaks_perpetuity:
        return violations

    nw = fx_row.get("net_worth_nis")
    for raw_sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        if not _REACHED_RE.search(sentence):
            continue
        if _NEGATION_RE.search(sentence):
            # Denial of sufficiency in this clause — not an assertion.
            continue
        if _FX_SHOCK_QUALIFIER_RE.search(sentence):
            # Properly qualified IN THE SAME sentence.
            continue
        violations.append(
            GateViolation(
                check=GateCheck.FI_FX_SHOCK_SUFFICIENCY,
                detail=(
                    "plan asserts capital/FI sufficiency 'reached' without an FX/"
                    f"currency qualifier in the same sentence ({sentence!r}), but a "
                    f"−10% USD/NIS shock drops net worth to {nw} — below the perpetuity "
                    "base. The 'reached' claim is true only at the current FX mark; "
                    "qualify it with the currency shock or do not claim it "
                    "unconditionally."
                ),
                locator="capital_sufficiency_fx",
            )
        )
        # One violation is enough — dedupe to a single GateViolation.
        break
    return violations
