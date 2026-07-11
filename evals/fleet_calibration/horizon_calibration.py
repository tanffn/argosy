"""Horizon-band / clock calibration scoring (benchmark §2b third dimension).

Pure deterministic parsers + comparators over persisted replay rationales.
No LLM calls — stubbed fixtures drive the tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

ScoreLabel = Literal[
    "inside",
    "outside",
    "unestimable_stated",
    "not_applicable",
    "no_band",
]


@dataclass(frozen=True)
class ClockBand:
    """Stated re-rating horizon band, normalized to months."""

    low_months: int
    high_months: int
    raw: str


_BAND_RE = re.compile(
    r"(?P<lo>\d+)\s*[-–—]\s*(?P<hi>\d+)\s*(?P<unit>months?|years?|yrs?|mo)\b",
    re.IGNORECASE,
)
_RERATING_BAND_RE = re.compile(
    r"(?i)(?:re-?rating\s+horizon|honest\s+re-?rating[^:]*:)\s*"
    r"(?P<lo>\d+)\s*[-–—]\s*(?P<hi>\d+)\s*(?P<unit>months?|years?|yrs?|mo)\b"
)
_SINGLE_RE = re.compile(
    r"(?:~|about|approximately|around)?\s*(?P<n>\d+)\s*(?P<unit>months?|years?|yrs?|mo)\b",
    re.IGNORECASE,
)
_UNESTIMABLE_RE = re.compile(
    r"(?i)\b("
    r"unestimable|genuinely\s+hard\s+to|"
    r"hard\s+to\s+(?:pin|estimate|forecast)|"
    r"cannot\s+estimate|not\s+estimable"
    r")\b"
)
_CLOCK_SECTION_RE = re.compile(
    r"(?is)(?:THE\s+CLOCK|CLOCK\s*[:—-]|Honest\s+re-rating\s+horizon)"
    r".{0,400}"
)


def _to_months(n: int, unit: str) -> int:
    u = unit.lower()
    if u.startswith("y"):
        return n * 12
    return n


def _band_from_match(m: re.Match) -> ClockBand:
    lo = _to_months(int(m.group("lo")), m.group("unit"))
    hi = _to_months(int(m.group("hi")), m.group("unit"))
    if lo > hi:
        lo, hi = hi, lo
    return ClockBand(low_months=lo, high_months=hi, raw=m.group(0))


def parse_clock_band(rationale: str) -> ClockBand | Literal["unestimable"] | None:
    """Extract the stated re-rating horizon band from a rationale.

    Prefers an explicit ``re-rating horizon: N-M years`` phrase over a
    next-validation window (``12-18 months`` launch). Returns
    ``\"unestimable\"`` when the fleet explicitly says so, ``None`` when no
    band is found.
    """
    if not rationale:
        return None
    # Prefer explicit re-rating horizon phrasing anywhere in the text.
    m_rr = _RERATING_BAND_RE.search(rationale)
    if m_rr:
        return _band_from_match(m_rr)

    window = rationale
    m_sec = _CLOCK_SECTION_RE.search(rationale)
    if m_sec:
        window = m_sec.group(0)
    if _UNESTIMABLE_RE.search(window) or _UNESTIMABLE_RE.search(rationale):
        if m_sec and _UNESTIMABLE_RE.search(window):
            return "unestimable"
        if not m_sec and _UNESTIMABLE_RE.search(rationale):
            return "unestimable"
    for m in _BAND_RE.finditer(window):
        return _band_from_match(m)
    if m_sec:
        for m in _BAND_RE.finditer(rationale):
            return _band_from_match(m)
    return None


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def months_between(start: date, end: date) -> int:
    """Whole calendar months between two dates (signed)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def score_horizon_calibration(
    stated: ClockBand | Literal["unestimable"] | None,
    *,
    actual_months: int | None,
    resolution_present: bool,
) -> ScoreLabel:
    """Compare stated band to actual re-rating timing."""
    if not resolution_present or actual_months is None:
        if stated == "unestimable":
            return "unestimable_stated"
        if stated is None:
            return "not_applicable" if not resolution_present else "no_band"
        return "not_applicable"
    if stated is None:
        return "no_band"
    if stated == "unestimable":
        return "unestimable_stated"
    if stated.low_months <= actual_months <= stated.high_months:
        return "inside"
    return "outside"


def score_row(
    *,
    rationale: str,
    freeze_date: Any,
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Score one persisted replay against its packet (informational dimension)."""
    resolution = packet.get("resolution")
    stated = parse_clock_band(rationale or "")
    if resolution is None:
        label = score_horizon_calibration(
            stated, actual_months=None, resolution_present=False,
        )
        return {
            "stated": (
                "unestimable" if stated == "unestimable"
                else (
                    {"low_months": stated.low_months, "high_months": stated.high_months, "raw": stated.raw}
                    if isinstance(stated, ClockBand) else None
                )
            ),
            "actual_months": None,
            "score": label,
        }

    cal = resolution.get("clock_calibration") if isinstance(resolution, dict) else None
    rerating = None
    if isinstance(cal, dict):
        rerating = _parse_date(cal.get("rerating_date"))
    freeze = _parse_date(freeze_date)
    actual_months = (
        months_between(freeze, rerating) if freeze and rerating else None
    )
    label = score_horizon_calibration(
        stated,
        actual_months=actual_months,
        resolution_present=True,
    )
    return {
        "stated": (
            "unestimable" if stated == "unestimable"
            else (
                {"low_months": stated.low_months, "high_months": stated.high_months, "raw": stated.raw}
                if isinstance(stated, ClockBand) else None
            )
        ),
        "actual_months": actual_months,
        "score": label,
    }
