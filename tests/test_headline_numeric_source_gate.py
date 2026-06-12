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
        "retirement.earliest_safe_age": "age",
        "retirement.preservation_age": "age",
        "retirement.required_real_yield_pct": "pct",
        "retirement.return_assumption_pct": "pct",
        "spend.fi_basis_nis": "nis",
        "savings.annual_net_nis": "nis",
        "spend.annual_t12_nis": "nis",
        "concentration.nvda_cap_pct": "pct",
        "concentration.nvda_current_pct": "pct",
        "retirement.liquidity_reserve_nis": "nis",
        "retirement.fi_total_capital_nis": "nis",
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


def test_canonical_earliest_safe_age_sanctioned_by_checker():
    """Dual-track threading regression: when the manifest carries the canonical
    earliest-safe age (46), a synth body stating BOTH 'age 46' (earliest-safe)
    and 'age 49' (the FI/target age) passes the headline-numeric check — neither
    is flagged. This is what the /accept gate-manifest opt-in
    (include_canonical_ages=True) buys."""
    resolved = _resolved(
        **{"retirement.earliest_safe_age": 46.0, "retirement.fi_age": 49.0}
    )
    md = {"long": "The earliest you can safely retire is age 46; the plan targets age 49."}
    assert check_headline_numeric_source(md, resolved) == []


def test_canonical_age_flagged_when_manifest_lacks_it():
    """Inverse: with ONLY fi_age (49) in the manifest, 'age 46' is flagged as an
    unsourced headline age — the exact false-positive the /accept gate-manifest
    opt-in prevents once the synth states the canonical earliest-safe age."""
    resolved = _resolved(**{"retirement.fi_age": 49.0})
    # "retirement age" is a kept headline subject (narrowed set, codex A).
    md = {"long": "The earliest-safe retirement age is age 46."}
    violations = check_headline_numeric_source(md, resolved)
    assert len(violations) == 1
    assert violations[0].check is GateCheck.HEADLINE_NUMERIC_SOURCE
    assert "46" in violations[0].detail


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
    # Uses the kept "retirement age" subject (the narrowed headline set drops
    # the bare "retire" trigger — codex 2026-06-10 option A).
    md = {"long": "Your projected retirement age is age 44."}
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


# --------------------------------------------------------------------------
# (f) SURGICAL scrub — only large NIS amounts in FI-capital context are
#     mutated; everything else is preserved (a live drun showed a broad
#     scrub turning ~44 legit detail numbers into [derivation pending]).
# --------------------------------------------------------------------------

def test_scrub_preserves_small_nis_on_fi_line():
    # Education ₪500,000 on an FI-context line is below the ₪2M floor → kept.
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "FI target funding sets aside ₪500,000 per child for education."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert scrubbed["long"] == md["long"]
    assert log == []


def test_scrub_preserves_large_nis_on_non_fi_line():
    # A large NIS amount that is NOT an FI-capital/net-worth claim is left
    # alone (e.g. an annual income line).
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "Combined annual household income is ₪2,500,000 before tax."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert scrubbed["long"] == md["long"]
    assert log == []


def test_scrub_preserves_pct_and_age_even_on_fi_line():
    # pct / age are no longer mutated by the surgical scrub.
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "FI target needs a 21% yield and retiring at age 44."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert scrubbed["long"] == md["long"]
    assert log == []


def test_scrub_preserves_total_capital_composite():
    # The legit total (perpetuity + reserve) must be recognized when present
    # in the manifest.
    resolved = _resolved(**{
        "retirement.fi_target_nis": 10_386_133.0,
        "retirement.fi_total_capital_nis": 11_836_133.0,
    })
    md = {"long": "Net worth covers the FI target; combined stack is ₪11.84M."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert "₪11.84M" in scrubbed["long"]
    assert log == []


def test_scrub_still_catches_large_fabricated_fi_target():
    resolved = _resolved(**{"retirement.fi_target_nis": 10_390_000.0})
    md = {"long": "FI capital target on the gross base is **₪22.00M**."}
    scrubbed, log = scrub_headline_numeric_source(md, resolved)
    assert PENDING_LABEL in scrubbed["long"]
    assert len(log) == 1


class TestSubjectBinding:
    """Option B (codex 2026-06-10) — only the value stated AS a headline
    subject's value must trace; narrative numbers on the same line are ignored."""

    def test_narrative_number_on_subject_line_not_flagged(self):
        # "30%" is narrative; only the NVDA TARGET value (12%) is bound.
        resolved = _resolved(**{"concentration.nvda_cap_pct": 0.12})
        md = {"long": "NVDA fell 30% in 2022, but the NVDA target is 12%."}
        assert check_headline_numeric_source(md, resolved) == []

    def test_fabricated_subject_value_flagged(self):
        resolved = _resolved(**{"concentration.nvda_cap_pct": 0.12})
        md = {"long": "The NVDA target is 99%."}
        v = check_headline_numeric_source(md, resolved)
        assert len(v) == 1 and "99%" in v[0].detail

    def test_age_skips_parenthetical_percent(self):
        # The "90%" and "95" live in a parenthetical; the bound age is 47.
        resolved = _resolved(**{"retirement.earliest_safe_age": 47.0})
        md = {"long": "Earliest safe retirement age (90% MC solvency to 95) is 47.0."}
        assert check_headline_numeric_source(md, resolved) == []

    def test_unbound_narrative_age_not_flagged(self):
        # A 2-digit number with no headline subject is left alone.
        resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
        md = {"long": "The 2008 crash cut equities 50% over 18 months."}
        assert check_headline_numeric_source(md, resolved) == []


class TestSubjectBindingBeforeAndParens:
    """Codex 2026-06-10 BLOCK fix — value-BEFORE-subject and parenthesized-value
    headline forms must still be caught (the original ₪21M reject was '₪21M FI
    target'); a narrative number not adjacent to the subject must not bind."""

    def test_value_before_subject_fabrication_flagged(self):
        resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
        md = {"long": "The **₪21.00M** FI target is the milestone."}
        v = check_headline_numeric_source(md, resolved)
        assert len(v) == 1 and "21.00M" in v[0].detail

    def test_pct_before_subject_fabrication_flagged(self):
        resolved = _resolved(**{"concentration.nvda_cap_pct": 0.13})
        md = {"long": "A 99% NVDA cap would be reckless."}
        v = check_headline_numeric_source(md, resolved)
        assert len(v) == 1 and "99%" in v[0].detail

    def test_parenthesized_value_fabrication_flagged(self):
        resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
        md = {"long": "FI target (₪21.00M) drives the glide."}
        v = check_headline_numeric_source(md, resolved)
        assert len(v) == 1 and "21.00M" in v[0].detail

    def test_value_before_subject_correct_passes(self):
        resolved = _resolved(**{"retirement.fi_target_nis": 17_300_000.0})
        md = {"long": "The **₪17.30M** FI target sustains spend."}
        assert check_headline_numeric_source(md, resolved) == []

    def test_nonadjacent_narrative_before_subject_not_flagged(self):
        # "30%" is not adjacent to "concentration cap" (words between) → no bind.
        resolved = _resolved(**{"concentration.nvda_cap_pct": 0.13})
        md = {"long": "NVDA fell 30%, raising concentration cap worries."}
        assert check_headline_numeric_source(md, resolved) == []


class TestMcShockNarrativeNotBound:
    """Regression: p10 Monte-Carlo SHOCK/IMPACT percentages mentioned near an
    'NVDA weight' qualifier phrase must not bind to the NVDA weight subject.

    Both are negative, derived sensitivity figures (not the allocation), and
    the subject 'at current NVDA weight' is a descriptive qualifier, not a
    target declaration. The live draft-36 reject surfaced two distinct binding
    bugs these tests pin:

      * cross-clause LEFT reach — '...shock is approximately -50.7%; at current
        NVDA weight ...' wrongly bound -50.7% (from the PRIOR clause) as the
        weight's value, because the value-before-subject clause split took the
        farthest segment instead of the one adjacent to the subject;
      * negative impact AFTER the qualifier — 'p10 portfolio impact at current
        NVDA weight equals approximately -33%' wrongly bound -33% (the impact,
        not the weight) — a negative percent is never an allocation weight/cap.
    """

    def _resolved_conc(self):
        return _resolved(
            **{
                "concentration.nvda_cap_pct": 0.13,
                "concentration.nvda_current_pct": 0.6486,
            }
        )

    def test_cross_clause_shock_not_bound_to_weight(self):
        md = {
            "long": (
                "p10 1y NVDA shock is approximately -50.7%; at current NVDA "
                "weight the implied p10 portfolio impact is approximately -33%."
            )
        }
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_negative_impact_after_qualifier_not_bound(self):
        md = {
            "long": (
                "p10 portfolio impact at current NVDA weight equals "
                "approximately -33 percent — value: `-33 %`"
            )
        }
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_qualifier_context_observation_not_flagged(self):
        # "At current NVDA weight ..." is a preposition-qualified CONDITION, not
        # a target declaration — the trailing number belongs to another noun, so
        # the subject is disarmed and nothing is flagged.
        md = {"long": "At current NVDA weight of 64.86% the book is concentrated."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_positive_fabricated_weight_still_flagged(self):
        # A non-qualified target declaration with a wrong (positive) weight must
        # still be caught.
        md = {"long": "The NVDA target weight is 40%."}
        v = check_headline_numeric_source(md, self._resolved_conc())
        assert len(v) == 1 and "40%" in v[0].detail

    def test_tight_hyphen_cap_value_still_flagged(self):
        # Codex r1 blocker-1 regression: a tight-hyphen "cap -12%" must NOT be
        # silently swallowed as a "negative". It is a separator-typo / wrong cap
        # and must be flagged (real cap is 13%). Keys on the subject, not the sign.
        md = {"long": "NVDA cap -12% of the book."}
        v = check_headline_numeric_source(md, self._resolved_conc())
        assert len(v) == 1 and "12%" in v[0].detail

    def test_given_declaration_not_over_skipped(self):
        # Codex r2 blocker-1 regression: "Given the NVDA cap is 99%" is a
        # DECLARATION (no "current"), so it must still bind and flag the 99%.
        md = {"long": "Given the NVDA cap is 99%, trim aggressively."}
        v = check_headline_numeric_source(md, self._resolved_conc())
        assert len(v) == 1 and "99%" in v[0].detail

    def test_with_current_qualifier_not_bound(self):
        # Codex r2 blocker-2: the preposition varies ("with"), so we key on
        # "current" not the preposition. The -33% impact must not bind.
        md = {"long": "p10 portfolio impact with current NVDA weight equals approximately -33%."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_markdown_bold_current_qualifier_not_bound(self):
        # Codex r2 blocker-2: markdown bold around the qualifier must not defeat
        # the guard.
        md = {"long": "p10 portfolio impact at **current NVDA weight** equals approximately -33%."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_current_weight_declaration_is_flagged(self):
        # Codex r3 blocker-1: "The current NVDA weight is 99%" is a DECLARATION
        # of present state (value attached via "is") — it must bind and flag
        # (real current weight 64.86%). The structural model keys on attachment,
        # so present-state declarations are still verified.
        md = {"long": "The current NVDA weight is 99%."}
        v = check_headline_numeric_source(md, self._resolved_conc())
        assert len(v) == 1 and "99%" in v[0].detail

    def test_current_weight_observation_traces_and_passes(self):
        # The same declaration with the TRUE current weight traces → no flag.
        md = {"long": "The current NVDA weight is 64.86%."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_filler_noun_between_current_and_subject_not_bound(self):
        # Codex r3 blocker-2: "current portfolio NVDA weight" — a filler noun
        # ("portfolio") separates "current" from the subject, but the impact
        # number is still detached by the "equals" other-quantity word.
        md = {"long": "p10 portfolio impact at current portfolio NVDA weight equals approximately -33%."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_impact_clause_before_subject_not_bound(self):
        # The before-subject path also rejects a different-quantity value:
        # "...impact is -33% NVDA weight" must not bind -33% to the weight.
        md = {"long": "The p10 impact is -33% at the NVDA weight today."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_cap_equals_declaration_is_bound(self):
        # Codex r4 blocker-1: "equals" is a legitimate copula, not a
        # different-quantity word. "NVDA cap equals 13%" is a declaration that
        # must bind (and here traces to the resolved 13%).
        md = {"long": "NVDA cap equals 13%."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_cap_equals_fabrication_is_flagged(self):
        # ...and a wrong "equals" declaration must be flagged (not skipped).
        md = {"long": "NVDA cap equals 99%."}
        v = check_headline_numeric_source(md, self._resolved_conc())
        assert len(v) == 1 and "99%" in v[0].detail

    def test_leading_quantity_noun_qualifier_not_bound(self):
        # Codex r4 blocker-2: the different-quantity noun is BEFORE the subject
        # ("p10 portfolio delta at current NVDA weight is -33%") with only a
        # copula after — the subject is a qualifier of "delta", so -33% must not
        # bind to the weight.
        md = {"long": "p10 portfolio delta at current NVDA weight is approximately -33%."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_parenthesized_other_quantity_not_bound(self):
        # Codex r4 nit: a parenthesized different-quantity value must not bind.
        md = {"long": "current NVDA weight (p10 impact -33%)."}
        assert check_headline_numeric_source(md, self._resolved_conc()) == []

    def test_subject_movement_verb_declaration_is_flagged(self):
        # Codex r5 blocker-2: subject-movement verbs ("drop"/"move") describe the
        # SUBJECT's own change — they must NOT be deny words, so a fabricated
        # "cap should drop to 99%" / "target should move to 99%" stays bound and
        # is flagged (real cap 13%).
        for txt in (
            "The NVDA cap should drop to 99%.",
            "The NVDA target weight should move to 99%.",
        ):
            v = check_headline_numeric_source({"long": txt}, self._resolved_conc())
            assert len(v) == 1 and "99%" in v[0].detail, txt
