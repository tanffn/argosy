"""#24 — headline_numeric_source gate unit tests.

The deterministic resolver is the approved set of headline numbers. These
tests build a small ``ResolvedPlanNumbers`` manifest directly (no DB, no
agents) and assert the checker:

  (a) passes a markdown headline number that matches a resolved value;
  (b) flags a fabricated headline number with no matching resolved value
      (the ₪21M-FI-target reject);
  (c) passes when the un-derived number is rendered "[derivation pending]";
  (d) does not false-flag numbers outside a headline context (dates, etc.).
"""
from __future__ import annotations

from argosy.quality import check_headline_numeric_source
from argosy.quality.gate_types import GateCheck
from argosy.quality.numeric_source_gate import (
    PENDING_LABEL,
    scrub_headline_numeric_source,
)
from argosy.services.plan_numeric_resolver import (
    ResolvedPlanNumbers,
    ResolvedValue,
)


def _resolved(**vals: float) -> ResolvedPlanNumbers:
    """Build a manifest of RESOLVED values from key=value pairs.

    Units are inferred from the canonical key registry; anything ending in
    ``_pct`` is a pct fraction, ``_age`` an age, otherwise nis.
    """
    units = {
        "portfolio.net_worth_nis": "nis",
        "retirement.fi_target_nis": "nis",
        "retirement.fi_age": "age",
        "retirement.required_real_yield_pct": "pct",
        "retirement.return_assumption_pct": "pct",
        "spend.fi_basis_nis": "nis",
        "savings.annual_net_nis": "nis",
        "spend.annual_t12_nis": "nis",
        "concentration.nvda_cap_pct": "pct",
        "concentration.nvda_current_pct": "pct",
    }
    out: dict[str, ResolvedValue] = {}
    for k, v in vals.items():
        out[k] = ResolvedValue(
            key=k,
            value=v,
            unit=units[k],
            status="resolved",
            source_locator=f"{k} (test)",
        )
    return ResolvedPlanNumbers(values=out)


# --------------------------------------------------------------------------
# (a) matching headline number → no violation
# --------------------------------------------------------------------------

def test_matching_nis_headline_passes():
    resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
    md = {"long": "- Derived FI target: **₪17.30M** (sustains spend)."}
    violations = check_headline_numeric_source(md, resolved)
    assert violations == [], [v.detail for v in violations]


def test_matching_nis_headline_passes_within_rounding_tolerance():
    # Source 17,295,000 displays as ₪17.30M — must still match.
    resolved = _resolved(**{"retirement.fi_target_nis": 17_295_000.0})
    md = {"long": "FI target net worth target is **₪17.30M**."}
    assert check_headline_numeric_source(md, resolved) == []


def test_matching_age_headline_passes():
    resolved = _resolved(**{"retirement.fi_age": 49.0})
    md = {"long": "You could retire at age 49 on the derived path."}
    assert check_headline_numeric_source(md, resolved) == []


def test_matching_pct_headline_passes_fraction_to_points():
    # Resolver stores 0.045 (fraction); markdown shows 4.5%.
    resolved = _resolved(**{"retirement.required_real_yield_pct": 0.045})
    md = {"long": "Required real yield on the portfolio is 4.5%."}
    assert check_headline_numeric_source(md, resolved) == []


def test_matching_raw_nis_headline_passes():
    resolved = _resolved(**{"spend.fi_basis_nis": 277_004.0})
    md = {"medium": "FI spend basis is ₪277,004/yr in the savings plan."}
    assert check_headline_numeric_source(md, resolved) == []


# --------------------------------------------------------------------------
# (b) fabricated headline number → violation
# --------------------------------------------------------------------------

def test_fabricated_fi_target_flagged():
    # Resolver has a ₪17.3M FI target; the markdown asserts a fabricated
    # ₪21M target (the user's #1 reject). Must flag.
    resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
    md = {"long": "- Derived FI target: **₪21.00M** (portfolio milestone)."}
    violations = check_headline_numeric_source(md, resolved)
    assert len(violations) == 1
    assert violations[0].check is GateCheck.HEADLINE_NUMERIC_SOURCE
    assert "21.00M" in violations[0].detail


def test_fabricated_fi_target_flagged_when_resolver_pending():
    # Resolver produced NO fi target (pending) but synth still states ₪21M.
    resolved = ResolvedPlanNumbers(
        values={
            "retirement.fi_target_nis": ResolvedValue.pending(
                "retirement.fi_target_nis", "nis", "pending (test)"
            )
        }
    )
    md = {"long": "FI target net worth: **₪21.00M**."}
    violations = check_headline_numeric_source(md, resolved)
    assert len(violations) == 1
    assert violations[0].check is GateCheck.HEADLINE_NUMERIC_SOURCE


def test_fabricated_age_flagged():
    resolved = _resolved(**{"retirement.fi_age": 49.0})
    md = {"long": "You could retire at age 44 comfortably."}
    violations = check_headline_numeric_source(md, resolved)
    assert len(violations) == 1
    assert "44" in violations[0].detail


# --------------------------------------------------------------------------
# (c) "[derivation pending]" → no violation
# --------------------------------------------------------------------------

def test_derivation_pending_label_not_flagged():
    # No fi target resolved; renderer emitted the pending sentinel instead
    # of a number. There is no digit to trace → no violation.
    resolved = ResolvedPlanNumbers(
        values={
            "retirement.fi_target_nis": ResolvedValue.pending(
                "retirement.fi_target_nis", "nis", "pending (test)"
            )
        }
    )
    md = {"long": "- Derived FI target: **[derivation pending]** (no source)."}
    assert check_headline_numeric_source(md, resolved) == []


# --------------------------------------------------------------------------
# (d) conservative — non-headline numbers are left alone
# --------------------------------------------------------------------------

def test_non_headline_line_not_scanned():
    # A date / section number on a line with NO headline keyword must not
    # trip the gate even though no resolved value matches.
    resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
    md = {
        "long": (
            "## Section 3 — review on 2031-04-15\n"
            "See table row 7 for the 12-month cadence.\n"
        )
    }
    assert check_headline_numeric_source(md, resolved) == []


def test_fleet_receipt_costs_not_scanned():
    # Fleet-receipt token/cost lines carry no headline keyword.
    resolved = _resolved(**{"portfolio.net_worth_nis": 8_000_000.0})
    md = {"long": "| `withdrawal_sequencer` | 12,345 | 6,789 | $0.4210 |"}
    assert check_headline_numeric_source(md, resolved) == []


def test_empty_resolved_flags_headline_number():
    # No resolved values at all → any headline number is unverifiable.
    resolved = ResolvedPlanNumbers(values={})
    md = {"long": "Net worth today is **₪8.00M**."}
    violations = check_headline_numeric_source(md, resolved)
    assert len(violations) == 1
    assert violations[0].check is GateCheck.HEADLINE_NUMERIC_SOURCE


def test_multiple_horizons_locator_reports_horizon():
    resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
    md = {
        "long": "FI target: **₪17.30M**.",          # matches
        "short": "FI target: **₪21.00M**.",          # fabricated
    }
    violations = check_headline_numeric_source(md, resolved)
    assert len(violations) == 1
    assert "horizon=short" in (violations[0].locator or "")


# --------------------------------------------------------------------------
# (e) PRIMARY scrub — fabricated headline numbers replaced with the pending
#     literal BEFORE persist (codex-recommended #24 primary gate).
# --------------------------------------------------------------------------

def test_scrub_replaces_fabricated_fi_target():
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "- Derived FI target: **₪21.00M** sustains spend."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert PENDING_LABEL in scrubbed["long"]
    assert "₪21.00M" not in scrubbed["long"]
    assert len(log) == 1
    assert "₪21.00M" in log[0]


def test_scrub_keeps_matching_number():
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "- Derived FI target: **₪10.39M** sustains spend."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert scrubbed["long"] == md["long"]
    assert log == []


def test_scrub_mixed_line_replaces_only_fabricated():
    # FI target matches (10.39M); the carried-forward 21M does not.
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "FI target is **₪10.39M**, up from a prior ₪21.00M."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert "₪10.39M" in scrubbed["long"]
    assert "₪21.00M" not in scrubbed["long"]
    assert scrubbed["long"].count(PENDING_LABEL) == 1


def test_scrub_leaves_non_headline_lines_untouched():
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "Section 21.5 of the appendix lists ₪999,999 in fees."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    # No headline keyword on the line → not scanned, not scrubbed.
    assert scrubbed["long"] == md["long"]
    assert log == []


def test_scrub_pending_literal_not_double_scrubbed():
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": f"- Derived FI target: {PENDING_LABEL}."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert scrubbed["long"] == md["long"]
    assert log == []


def test_scrub_preserves_trailing_newline():
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "FI target **₪21.00M**.\n"}
    scrubbed, _ = scrub_headline_numeric_source(md, resolved)
    assert scrubbed["long"].endswith("\n")
