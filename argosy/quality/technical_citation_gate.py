"""Check 8 — technical_citation_integrity.

Sibling of :mod:`argosy.quality.numeric_source_gate`. That gate guards the
HEADLINE numbers (FI capital, retirement age, NVDA cap) against the
deterministic resolver. This one guards SYMBOL-LEVEL TECHNICAL INDICATOR
readings cited in the prose against the run's own TechnicalAnalyst payload —
the source the prose claims to be quoting.

Root cause (s18): the synthesizer carried a stale ``RSI 73.4`` for SCHD
forward across six plan versions while the live payload reported
``rsi_14 = 56.05`` (signal=hold). A short-horizon "PAUSE despite the RSI
73.4 exit signal" rested on a number the cited source did not contain — a
citation-integrity failure the fund manager (correctly) rejected. The prior
plan's prose was outranking the fresh agent payload.

Design (mirrors numeric_source_gate's no-false-positive discipline):

* We bind a stated reading to a SYMBOL: a number is only checked when an
  indicator keyword (``RSI``) is followed by a bare value AND at least one
  symbol that HAS a payload reading for that indicator appears on the same
  line. A reading bound to no on-line symbol is narrative and is left alone.
* A value "traces" if it is within a small display tolerance of the live
  reading for ANY symbol named on the line. So a multi-symbol line that
  quotes one symbol's RSI never false-flags against the other's.
* Qualitative threshold phrasings (``RSI > 70``, ``RSI above 70``) are NOT
  stated current readings and are never matched.

Only RSI is enforced today: it is bounded ``[0, 100]`` and the ``RSI <n>``
form is unambiguous, so the check is safe from the false positives a
free-floating price/MACD scan would invite. The ``_INDICATORS`` table makes
adding another bounded indicator a one-line change once it is proven safe.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from argosy.quality.gate_types import GateCheck, GateViolation

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Indicator registry: payload_key -> (prose keyword, value regex, abs tolerance)
# ---------------------------------------------------------------------------
#
# The value pattern matches "RSI 73.4", "RSI: 73.4", "RSI of 73.4",
# "RSI is 73.4", "RSI=73.4", "RSI-14 73.4", "RSI(14) 73.4", "14-day RSI 73.4",
# "RSI 14-day reading is 56.0" — but NOT "RSI > 70" / "RSI above 70" (those
# never reach a captured value). An optional PERIOD qualifier ("(14)", "-14",
# "14-day") is consumed separately so it is never mistaken for the reading.
_RSI_VALUE = re.compile(
    r"\bRSI\b"
    r"(?:\s*\(\s*\d{1,2}\s*\))?"          # RSI(14)
    r"(?:\s*-\s*\d{1,2}(?!\s*\.))?"        # RSI-14  (not RSI-14.x)
    r"(?:\s*\d{1,2}\s*-?\s*day)?"          # RSI 14-day / 14 day
    r"\s*(?:of|is|at|reading|=|:|,|\s)*?"  # copula words (NOT comparison ops)
    r"(\d{1,3}(?:\.\d+)?)",
    re.IGNORECASE,
)

# payload_key -> (compiled value regex, absolute tolerance, display label)
_INDICATORS: dict[str, tuple[re.Pattern[str], float, str]] = {
    "rsi_14": (_RSI_VALUE, 1.5, "RSI"),
}

# A reading is a THRESHOLD/rule, not a stated current value, when these cues
# sit just before the keyword or just after the captured value. We skip those
# (e.g. "trim only if RSI 70 or higher", "add when RSI above 30").
_THRESHOLD_BEFORE = re.compile(
    r"\b(if|when|whenever|once|until|unless|above|below|over|under|"
    r"exceeds?|reach(?:es|ed)?|past|beyond|cross(?:es|ed)?|stays?|holds?)\b"
    r"|[<>]=?\s*$",
    re.IGNORECASE,
)
_THRESHOLD_AFTER = re.compile(
    r"^\s*(?:or\s+(?:higher|lower|above|below|more|less)|\+|or\s+richer|"
    r"threshold|\bor\s+above\b)",
    re.IGNORECASE,
)


def parse_indicators_from_report_json(
    response_text: str,
) -> dict[str, dict[str, float]]:
    """Parse a TechnicalAnalyst ``response_text`` into ``{SYMBOL: {key: value}}``.

    The persisted shape is ``{"per_ticker": {SYM: {"indicators": {...}}}}``.
    Returns an empty dict on any parse failure or a missing ``per_ticker``
    block (the gate then simply cannot run — fail-open here; the /accept
    fail-closed branch handles enforce-mode skips).
    """
    if not response_text:
        return {}
    try:
        payload = json.loads(response_text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    per_ticker = payload.get("per_ticker")
    if not isinstance(per_ticker, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for symbol, entry in per_ticker.items():
        if not isinstance(symbol, str) or not isinstance(entry, dict):
            continue
        indicators = entry.get("indicators")
        if not isinstance(indicators, dict):
            continue
        clean: dict[str, float] = {}
        for key, value in indicators.items():
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                continue
        if clean:
            out[symbol.upper()] = clean
    return out


def load_run_technical_indicators(
    db: "Session", decision_run_id: object,
) -> dict[str, dict[str, float]]:
    """Load the latest technical-analyst indicator payload for a run.

    ``decision_run_id`` may be an int (95) or the synth string form
    (``"plan-synth-95"``); both candidate ``decision_id`` values are tried.
    Best-effort: any DB/parse failure returns ``{}`` so the gate degrades to
    "could not run" rather than breaking the accept path.
    """
    try:
        from sqlalchemy import select

        from argosy.state.models import AgentReport
    except Exception:  # pragma: no cover — import guard
        return {}

    raw = str(decision_run_id)
    digits = re.sub(r"\D", "", raw)
    candidates = {raw}
    if digits:
        candidates.add(digits)
        candidates.add(f"plan-synth-{digits}")

    try:
        row = db.execute(
            select(AgentReport)
            .where(
                AgentReport.agent_role.like("%technical%"),
                AgentReport.decision_id.in_(tuple(candidates)),
            )
            .order_by(AgentReport.created_at.desc())
        ).scalars().first()
    except Exception:  # noqa: BLE001 — defensive
        return {}
    if row is None:
        return {}
    return parse_indicators_from_report_json(row.response_text or "")


def check_technical_citation_integrity(
    horizon_text: dict[str, str],
    indicators: dict[str, dict[str, float]],
) -> list[GateViolation]:
    """Flag prose indicator readings that contradict the cited payload.

    Args:
        horizon_text: horizon name -> user-facing markdown.
        indicators: ``{SYMBOL: {indicator_key: value}}`` from the run's
            TechnicalAnalyst report (see ``load_run_technical_indicators``).

    Returns one :class:`GateViolation` per stated reading that traces to no
    on-line symbol's live value of that indicator. Readings that cannot be
    bound to a symbol (or whose symbols carry no payload for the indicator)
    are never flagged.
    """
    if not horizon_text or not indicators:
        return []

    symbols = list(indicators.keys())
    violations: list[GateViolation] = []

    for horizon_name, text in horizon_text.items():
        if not text:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            # All symbol occurrences on the line, as (symbol, start) pairs.
            occ: list[tuple[str, int]] = []
            for s in symbols:
                for sm in re.finditer(rf"\b{re.escape(s)}\b", line):
                    occ.append((s, sm.start()))
            if not occ:
                continue
            for payload_key, (pattern, tol, label) in _INDICATORS.items():
                for m in pattern.finditer(line):
                    # Skip THRESHOLD/rule contexts ("if RSI 70 or higher").
                    before = line[max(0, m.start() - 24): m.start()]
                    after = line[m.end(): m.end() + 16]
                    if _THRESHOLD_BEFORE.search(before) or _THRESHOLD_AFTER.search(after):
                        continue
                    stated_str = m.group(1)
                    try:
                        stated = float(stated_str)
                    except (TypeError, ValueError):
                        continue
                    # Bind to the NEAREST symbol that carries this indicator
                    # (a value is attributed to the symbol it sits closest to,
                    # not to "any on-line symbol" — which would false-negative
                    # "SCHD RSI 36 vs SCHG" against SCHG's 36).
                    anchor = m.start()
                    nearest: str | None = None
                    best = 1 << 30
                    for s, pos in occ:
                        if payload_key not in indicators[s]:
                            continue
                        d = abs(pos - anchor)
                        if d < best:
                            best, nearest = d, s
                    if nearest is None:
                        continue  # no on-line symbol carries this indicator
                    live = indicators[nearest][payload_key]
                    if abs(stated - live) <= tol:
                        continue
                    violations.append(
                        GateViolation(
                            check=GateCheck.TECHNICAL_CITATION,
                            detail=(
                                f"prose {label} {stated_str} for {nearest} "
                                f"contradicts the current technical payload "
                                f"({nearest}={live:g}) — re-ground from the live "
                                f"indicator or drop the claim"
                            ),
                            locator=f"horizon={horizon_name} line={line_no}",
                        )
                    )

    return violations


__all__ = [
    "check_technical_citation_integrity",
    "load_run_technical_indicators",
    "parse_indicators_from_report_json",
]
