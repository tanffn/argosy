"""Run-106 finding [4] — event-currency-consistency gate.

A named/dated money event (e.g. the June-17 RSU tax) must not flip currency
between NIS and USD across surfaces. The June 17 RSU tax estimate that reads
₪180,000 in the body and $52,000 in the appendix is NOT a harmless typo: the
magnitude changes by ~the FX rate (~3×), which silently mis-sizes the after-tax
cash the plan can deploy.

Strategy (pure function, no I/O):
  1. Split the plan text into clause-ish spans on terminal punctuation / newlines.
  2. For each clause, find an EVENT ANCHOR — a date token (month-name day, or an
     ISO ``YYYY-MM-DD``) and/or a labeled event ("RSU tax" / "tax estimate" /
     "tax due"). The anchor is what binds two surfaces to the SAME event; keying
     on it prevents two genuinely DIFFERENT events (each in its own currency)
     from being mistaken for a flip.
  3. In that clause, detect the currency attached to a money amount: NIS
     (``₪`` / "NIS" / "shekel" / "ILS") or USD (``$`` / "USD" / "dollars").
  4. If the same anchor carries BOTH a NIS-denominated and a USD-denominated
     amount across clauses → flag EVENT_CURRENCY_CONSISTENCY.

Per Argosy's fail-loud doctrine this biases toward FALSE-POSITIVE: an anchor
seen in two currencies is flagged even if the surfaces could in principle be
the gross-vs-net of two currencies — a spurious flag is safer than letting a
silent magnitude error through.

Named compiled regexes with WHY comments, matching the coherence_gate
convention. Python source is UTF-8 so the ₪ literal below is fine; the detail
strings deliberately spell out "NIS"/"USD" so the gate never PRINTs ₪ on a
cp1252 console.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# Split into clause-ish spans on terminal punctuation / newlines / em-dashes.
# Each clause is the unit within which a single amount is bound to its currency
# AND its event anchor — the same convention coherence_gate uses for sentences.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?\n;]+|\s—\s|\s-\s")

# An ISO date anchor (YYYY-MM-DD). Normalised to itself as the anchor key.
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

# A month-name + day anchor ("June 17", "Jun 17", "17 June"). Captures the
# month + day so "June 17" and "Jun 17" collapse to the same normalised key.
_MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    "aug(?:ust)?|sep(?:tember)?|sept|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MONTH_DAY_RE = re.compile(
    rf"\b({_MONTHS})\s+(\d{{1,2}})\b"   # "June 17"
    rf"|\b(\d{{1,2}})\s+({_MONTHS})\b",  # "17 June"
    re.IGNORECASE,
)
# Normalise a 3-letter month prefix → a canonical month number key.
_MONTH_KEY = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

# A labeled event anchor — the recurring named money events. "RSU tax" /
# "tax estimate" / "tax due" / "tax bill". Used alongside the date so a clause
# can be bound to "the RSU tax" even when only one surface restates the date.
_LABEL_RE = re.compile(
    r"rsu\s+tax|tax\s+estimate|estimated\s+tax|tax\s+(?:due|bill|liability|payment|withholding)",
    re.IGNORECASE,
)

# Currency attached to a money amount. NIS side: the ₪ symbol, or the words
# NIS / ILS / shekel(s). USD side: a bare $ in front of digits, or the words
# USD / dollar(s). We only treat $ as a currency when it sits next to a number
# so a stray "$" elsewhere is not mis-read.
_NIS_RE = re.compile(r"₪\s*\d|\bNIS\b|\bILS\b|shekel", re.IGNORECASE)
_USD_RE = re.compile(r"\$\s*\d|\bUSD\b|\bdollar", re.IGNORECASE)


def _anchor_keys(clause: str) -> set[str]:
    """The set of normalised event-anchor keys a clause refers to.

    A date (ISO or month-day) and/or a labeled event ("rsu_tax"). Keys are
    normalised so "June 17", "Jun 17", "17 June" and a same-month ISO all
    collapse together; this is what binds two surfaces to the SAME event.
    """
    keys: set[str] = set()

    for m in _ISO_DATE_RE.finditer(clause):
        # ISO 2026-06-17 → month-day key "06-17" so it can match "June 17".
        iso = m.group(1)
        keys.add(f"date:{iso[5:]}")  # MM-DD

    for m in _MONTH_DAY_RE.finditer(clause):
        if m.group(1):  # "June 17" form
            mon, day = m.group(1), m.group(2)
        else:  # "17 June" form
            day, mon = m.group(3), m.group(4)
        mm = _MONTH_KEY[mon[:3].lower()]
        keys.add(f"date:{mm}-{int(day):02d}")

    if _LABEL_RE.search(clause):
        keys.add("label:rsu_tax")

    return keys


def check_event_currency_consistency(*, plan_text: str) -> list[GateViolation]:
    """Flag a named/dated money event whose currency flips between NIS and USD.

    Input: ``plan_text`` — the rendered plan prose across surfaces (body,
    dashboard, appendices) as one string.

    For each event anchor (a date token and/or "RSU tax"/"tax estimate"), record
    the currencies of money amounts in the clauses that mention it. If the same
    anchor is denominated in BOTH NIS and USD, the same event flips currency
    across surfaces — the magnitude changes by ~the FX rate — and that is a
    EVENT_CURRENCY_CONSISTENCY violation. Biases toward false-positive.
    """
    text = plan_text or ""
    # anchor key -> set of currency tags ("NIS" / "USD") seen for that event.
    seen: dict[str, set[str]] = {}

    for raw in _CLAUSE_SPLIT_RE.split(text):
        clause = raw.strip()
        if not clause:
            continue
        has_nis = bool(_NIS_RE.search(clause))
        has_usd = bool(_USD_RE.search(clause))
        if not (has_nis or has_usd):
            # No money amount in this clause — nothing to bind to an anchor.
            continue
        for key in _anchor_keys(clause):
            bucket = seen.setdefault(key, set())
            if has_nis:
                bucket.add("NIS")
            if has_usd:
                bucket.add("USD")

    violations: list[GateViolation] = []
    for key, currencies in seen.items():
        if "NIS" in currencies and "USD" in currencies:
            violations.append(
                GateViolation(
                    check=GateCheck.EVENT_CURRENCY_CONSISTENCY,
                    detail=(
                        f"the money event '{key}' is denominated in BOTH NIS and USD "
                        "across surfaces — the same event flips currency, changing its "
                        "magnitude by ~the FX rate. Pin the event to one currency (or "
                        "label gross-vs-net explicitly)."
                    ),
                    locator=key,
                )
            )
    return violations
