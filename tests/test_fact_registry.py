"""TDD for the canonical fact registry (steps 1-3 of the codex-converged fix):

  1. format_fact / render_fact  — render a number from ONE canonical source in
     its declared display policy (the LLM never types the digits).
  2. render_placeholders        — substitute {{fact:key}} tokens; unresolved /
     missing / unknown = deterministic build failure (no silent passthrough).
  3. find_unauthorized_numbers  — the detect->prevent gate: any ₪ / % / "age NN"
     financial magnitude in body prose that is NOT inside a {{fact:}} placeholder
     is a violation (the build fails before critics see a drift-prone artifact).
"""
from __future__ import annotations

import pytest

from argosy.quality.fact_registry import (
    FACT_DISPLAY,
    PlaceholderError,
    find_unauthorized_numbers,
    format_fact,
    render_fact,
    render_placeholders,
)


# --- tiny ResolvedValue/registry stub (no DB, no resolver dependency) -------
class _RV:
    def __init__(self, value, unit, status="resolved"):
        self.value, self.unit, self.status = value, unit, status


class _Resolved:
    def __init__(self, d):
        self._d = d

    def get(self, key):
        return self._d.get(key)


# --- Phase 2b: retention split, FI-crossing year, total net worth are bindable
def test_retention_and_fi_crossing_are_placeholder_bindable():
    """The synthesizer must be able to placeholder the RSU retention rates and the
    FI-crossing year (the prose-vs-registry drift the run-117 reader A/B caught:
    prose '~47%' vs canonical 50%/70%). These keys must be in FACT_DISPLAY and
    render canonically."""
    assert FACT_DISPLAY.get("tax.retention_at_vest_pct") == "pct"
    assert FACT_DISPLAY.get("tax.retention_capital_track_pct") == "pct"
    assert FACT_DISPLAY.get("retirement.fi_crossing_year") == "year"
    assert FACT_DISPLAY.get("portfolio.total_net_worth_incl_residence_nis") == "nis_millions"

    resolved = _Resolved({
        "tax.retention_at_vest_pct": _RV(0.50, "pct"),
        "tax.retention_capital_track_pct": _RV(0.70, "pct"),
        "retirement.fi_crossing_year": _RV(2027.0, "year"),
    })
    assert render_fact("tax.retention_at_vest_pct", resolved) == "50.0%"
    assert render_fact("tax.retention_capital_track_pct", resolved) == "70.0%"
    assert render_fact("retirement.fi_crossing_year", resolved) == "2027"


def test_format_fact_year_is_a_plain_integer_year():
    assert format_fact(2027.0, "year", display="year") == "2027"


# --- step 1: format_fact matches the existing renderer's display forms ------
def test_format_fact_nis_full_matches_render_n():
    # render._n(x) == f"₪{x:,.0f}"
    assert format_fact(-86565.0, "nis", display="nis") == "₪-86,565"
    assert format_fact(11836133.0, "nis", display="nis") == "₪11,836,133"


def test_format_fact_nis_millions():
    assert format_fact(11749568.0, "nis", display="nis_millions") == "₪11.75M"


def test_format_fact_pct_from_fraction():
    # resolver stores percentages as FRACTIONS (0-1); display is percent-points.
    assert format_fact(0.12, "pct", display="pct") == "12.0%"
    assert format_fact(0.03, "pct", display="pct") == "3.0%"


def test_format_fact_age():
    assert format_fact(46.0, "age", display="age") == "age 46"


def test_render_fact_reads_value_from_resolver():
    resolved = _Resolved({"portfolio.liquid_net_worth_nis": _RV(11749568.0, "nis")})
    # the registry knows this key's display policy
    assert "portfolio.liquid_net_worth_nis" in FACT_DISPLAY
    assert render_fact("portfolio.liquid_net_worth_nis", resolved) == "₪11.75M"


def test_render_fact_pending_value_raises():
    resolved = _Resolved({"retirement.fi_age": _RV(None, "age", status="pending")})
    with pytest.raises(PlaceholderError):
        render_fact("retirement.fi_age", resolved)


# --- step 2: placeholder rendering ------------------------------------------
def test_render_placeholders_substitutes_known_facts():
    resolved = _Resolved({
        "portfolio.liquid_net_worth_nis": _RV(11749568.0, "nis"),
        "retirement.fi_age": _RV(49.0, "age"),
    })
    text = "Liquid net worth is {{fact:portfolio.liquid_net_worth_nis}} at age {{fact:retirement.fi_age}}."
    out = render_placeholders(text, resolved)
    assert out == "Liquid net worth is ₪11.75M at age age 49."
    assert "{{fact:" not in out


def test_render_placeholders_unknown_key_is_build_failure():
    resolved = _Resolved({})
    with pytest.raises(PlaceholderError):
        render_placeholders("see {{fact:not.a.real.key}}", resolved)


def test_render_placeholders_unresolved_value_is_build_failure():
    resolved = _Resolved({"portfolio.liquid_net_worth_nis": _RV(None, "nis", status="pending")})
    with pytest.raises(PlaceholderError):
        render_placeholders("nw {{fact:portfolio.liquid_net_worth_nis}}", resolved)


def test_render_placeholders_non_strict_leaves_unrenderable_token():
    # The best-effort assembly wiring leaves an unresolved token in place (for the
    # gate to surface) instead of aborting; resolvable facts still render.
    resolved = _Resolved({"retirement.fi_age": _RV(49.0, "age")})
    text = "age {{fact:retirement.fi_age}} key {{fact:not.a.key}}"
    out = render_placeholders(text, resolved, strict=False)
    assert "age age 49" in out
    assert "{{fact:not.a.key}}" in out  # left for the gate


# --- step 3: ban-unauthorized-numbers gate ----------------------------------
def test_ban_gate_flags_raw_financial_numbers_in_prose():
    text = "Liquid net worth is ₪11,687,926, a margin of ₪-148,208, NVDA at 12% by age 46."
    viols = find_unauthorized_numbers(text)
    rendered = " ".join(v.token for v in viols)
    assert "₪11,687,926" in rendered
    assert "₪-148,208" in rendered or "148,208" in rendered
    assert any("12%" in v.token for v in viols)
    assert any("46" in v.token for v in viols)  # the age


def test_ban_gate_allows_numbers_inside_placeholders():
    text = ("Liquid net worth is {{fact:portfolio.liquid_net_worth_nis}}, "
            "margin {{fact:retirement.fi_margin_signed_nis}}, NVDA "
            "{{fact:concentration.nvda_cap_pct}} by {{fact:retirement.earliest_safe_age}}.")
    assert find_unauthorized_numbers(text) == []


def test_ban_gate_ignores_pending_literal_and_non_financial_ints():
    # the pending escape hatch + bare years/counts/section numbers are not
    # financial magnitudes and must NOT be flagged.
    text = "Target is [derivation pending]. See section 3. The 2026-07-01 tranche has 2 lots."
    assert find_unauthorized_numbers(text) == []
