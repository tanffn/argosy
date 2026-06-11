"""Tests for the technical-indicator citation-integrity gate.

Root cause this guards (s18): the synthesizer carried a STALE ``RSI 73.4``
for SCHD forward across six plan versions (v30→v35) while the live
TechnicalAnalyst payload for the same run reported ``rsi_14 = 56.05`` with
``signal = hold``. The fund manager (correctly) rejected the draft for a
citation-integrity failure: a load-bearing short-horizon "PAUSE despite the
RSI 73.4 exit signal" rested on a number the cited source did not contain.

This gate is the deterministic backstop: any prose RSI reading bound to a
symbol that contradicts that run's cited technical payload (beyond a small
display tolerance) is a blocking violation — the synth must re-ground the
figure from the live indicator or drop the claim.
"""
from __future__ import annotations

from argosy.quality.gate_types import GateCheck
from argosy.quality.technical_citation_gate import (
    check_technical_citation_integrity,
    parse_indicators_from_report_json,
)


# The exact shape persisted in agent_reports.response_text for the technical
# analyst (verified against run 95, report id 1149).
_RUN95_TECHNICAL_JSON = """
{
  "per_ticker": {
    "AMD": {"ticker": "AMD", "indicators": {"rsi_14": 54.20, "macd": 27.24, "ma_50": 380.45, "price": 473.52}, "signal": "hold"},
    "SCHD": {"ticker": "SCHD", "indicators": {"rsi_14": 56.05, "macd": 0.21, "macd_signal": 0.26, "ma_50": 31.64, "price": 32.57}, "signal": "hold"},
    "SCHG": {"ticker": "SCHG", "indicators": {"rsi_14": 36.04, "macd": 0.07, "ma_50": 33.06, "price": 33.17}, "signal": "hold"}
  },
  "summary": "Mixed.",
  "confidence": 0.6
}
"""


def _indicators() -> dict[str, dict[str, float]]:
    return parse_indicators_from_report_json(_RUN95_TECHNICAL_JSON)


def test_parse_extracts_per_ticker_indicators():
    ind = _indicators()
    assert ind["SCHD"]["rsi_14"] == 56.05
    assert ind["AMD"]["rsi_14"] == 54.20
    assert "SCHG" in ind


def test_parse_bad_json_returns_empty():
    assert parse_indicators_from_report_json("not json") == {}
    assert parse_indicators_from_report_json("") == {}
    assert parse_indicators_from_report_json('{"no_per_ticker": 1}') == {}


def test_catches_the_run95_stale_rsi():
    """The exact reject: SCHD prose asserts RSI 73.4, payload says 56.05."""
    text = {
        "short": (
            "Trim SCHD (Leumi 7,750 sh) — PAUSE despite the RSI 73.4 exit "
            "signal; the staged UCITS migration takes precedence."
        ),
    }
    viols = check_technical_citation_integrity(text, _indicators())
    assert len(viols) == 1
    v = viols[0]
    assert v.check is GateCheck.TECHNICAL_CITATION
    assert "73.4" in v.detail
    assert "SCHD" in v.detail
    assert "56.05" in v.detail  # surfaces the live value to re-ground against
    assert "short" in (v.locator or "")


def test_passes_when_rsi_matches_live_payload():
    text = {"short": "SCHD momentum is neutral (RSI 56.0, hold)."}
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_tolerance_absorbs_display_rounding():
    # 56.05 payload vs 56.1 / 55.0 prose — within the 1.5 display band.
    text = {"short": "SCHD RSI 56.1 today.", "long": "SCHD RSI of 55.0."}
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_ignores_rsi_without_a_symbol_on_the_line():
    # A narrative RSI mention with no symbol to bind to cannot be verified —
    # never flagged (mirrors numeric_source_gate's no-false-positive rule).
    text = {"medium": "We avoid names with RSI 80 or richer momentum."}
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_ignores_qualitative_threshold_references():
    # "RSI > 70" / "RSI above 70" are thresholds, not stated current readings.
    text = {
        "short": "SCHD: trim only if RSI > 70.",
        "medium": "AMD: watch for RSI above 70 before adding.",
    }
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_multi_symbol_line_binds_to_any_matching_symbol():
    # A line naming SCHD AND SCHG with "RSI 36.0" matches SCHG's live 36.04 —
    # not flagged even though it mismatches SCHD, because it traces to SOME
    # on-line symbol's payload.
    text = {"short": "Between SCHD and SCHG, the weak one (RSI 36.0) is SCHG."}
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_multi_symbol_line_flags_when_matching_none():
    text = {"short": "Between SCHD and SCHG, RSI 90.0 looks overbought."}
    viols = check_technical_citation_integrity(text, _indicators())
    assert len(viols) == 1
    assert "90.0" in viols[0].detail


def test_no_payload_for_symbol_skips():
    # Symbol present in prose but absent from the payload → cannot verify.
    text = {"short": "VOO RSI 99.0 is extreme."}
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_empty_inputs_safe():
    assert check_technical_citation_integrity({}, _indicators()) == []
    assert check_technical_citation_integrity({"short": "SCHD RSI 73.4"}, {}) == []


# --- codex-review hardening (s18) -----------------------------------------


def test_codex4_multi_symbol_binds_to_nearest_not_any():
    # "SCHD RSI 36.0 vs SCHG": 36.0 sits next to SCHD, so it must be checked
    # against SCHD (56.05) and FLAGGED — not silently passed because it happens
    # to match the farther SCHG's 36.04.
    text = {"short": "SCHD RSI 36.0 vs SCHG looks divergent."}
    viols = check_technical_citation_integrity(text, _indicators())
    assert len(viols) == 1
    assert "SCHD" in viols[0].detail and "36.0" in viols[0].detail


def test_codex5_threshold_rule_not_flagged():
    # "if RSI 70 or higher" is a rule threshold, not a stated reading.
    text = {
        "short": "SCHD: trim only if RSI 70 or higher.",
        "medium": "AMD: add when RSI 30 or lower.",
        "long": "SCHD stays a hold until RSI 75.",
    }
    assert check_technical_citation_integrity(text, _indicators()) == []


def test_codex6_period_notation_forms():
    # period qualifier must not be captured as the value; all of these are the
    # stale 73.4 reading for SCHD and must flag (SCHD live = 56.05).
    for line in (
        "SCHD RSI-14 73.4 is overbought.",
        "SCHD RSI(14) 73.4 today.",
        "SCHD 14-day RSI 73.4 now.",
    ):
        viols = check_technical_citation_integrity({"short": line}, _indicators())
        assert len(viols) == 1, line
        assert "73.4" in viols[0].detail, line


def test_codex6_period_notation_does_not_misread_period_as_value():
    # "RSI 14-day reading is 56.0" -> the value is 56.0 (matches live), NOT 14.
    text = {"short": "SCHD RSI 14-day reading is 56.0, neutral."}
    assert check_technical_citation_integrity(text, _indicators()) == []
