"""Tests for the deterministic tokenize-canonical-figures pass.

See argosy/quality/fact_tokenizer.py — a literal matching a registered
canonical fact is rewritten to {{fact:key}}; a literal near the SAME concept
that DIFFERS from canonical is surfaced as a FACT_LITERAL_DRIFT violation,
never silently corrected.
"""
from __future__ import annotations

from argosy.quality.fact_tokenizer import tokenize_bodies, tokenize_text
from argosy.quality.gate_types import GateCheck
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def _resolved(**kv) -> ResolvedPlanNumbers:
    """Build a ResolvedPlanNumbers from {key: (value, unit)}."""
    values = {
        k: ResolvedValue(
            key=k, value=v, unit=unit, status="resolved", source_locator="test",
        )
        for k, (v, unit) in kv.items()
    }
    return ResolvedPlanNumbers(values=values)


SELL = "concentration.nvda_sell_sh"
TARGET_SH = "concentration.nvda_target_sh"
ELIGIBLE = "concentration.nvda_eligible_now_sh"
QUOTA = "concentration.nvda_quota_tax_year_sh"
CAP = "concentration.nvda_cap_pct"
TARGET_PCT = "concentration.nvda_target_pct"
CURRENT = "concentration.nvda_current_pct"
MARGIN_NOR = "retirement.fi_margin_net_of_realization_nis"


# ---------------------------------------------------------------------------
# 1. Substitution of a matching literal.
# ---------------------------------------------------------------------------


def test_matching_share_count_is_tokenized():
    resolved = _resolved(**{SELL: (9479, "shares")})
    text = "The forward glide sells 9,479 shares from Section-102 inventory. NVDA weight is high."
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{SELL}}}}}" in result.text
    assert "9,479" not in result.text
    assert result.violations == []
    assert result.substitutions == [(SELL, "9,479 shares")]


def test_matching_pct_is_tokenized():
    resolved = _resolved(**{CAP: (0.13, "pct")})
    text = "NVDA sits against a binding instrument-level ceiling of 13.0%."
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{CAP}}}}}" in result.text
    assert "13.0%" not in result.text
    assert result.violations == []


def test_matching_nis_margin_is_tokenized():
    resolved = _resolved(**{MARGIN_NOR: (-2071605.87, "nis")})
    text = "The FI sufficiency margin net of realization tax is -₪2,071,606 this run."
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{MARGIN_NOR}}}}}" in result.text
    assert result.violations == []


# ---------------------------------------------------------------------------
# 1b. Exclude proximity — an exclude term only disqualifies a candidate when
# it is actually ATTACHED TO A NUMBER (its own, or a neighbour's), not just
# anywhere nearby. Regression for the "capital-track-eligible inventory"
# over-exclusion bug that was silently suppressing a real drift.
# ---------------------------------------------------------------------------


def test_bare_exclude_adjective_not_attached_to_a_number_lets_drift_surface():
    # "eligible" here modifies "inventory", not any number — it is not
    # attached to a number at all, so the SELL exclusion must NOT fire, the
    # sell anchor must fire, and — because canonical is 9,479, not the
    # drifted 9,417 literal in this sentence — the result must be a
    # FACT_LITERAL_DRIFT violation naming concentration.nvda_sell_sh, not a
    # silent suppression (the real bug measured against plan 106 /
    # decision_run 400: the anchor used to be suppressed entirely, making
    # this drift invisible).
    resolved = _resolved(**{SELL: (9479, "shares")})
    text = (
        "NVDA: the forward glide sells 9,417 shares from Section-102 "
        "capital-track-eligible inventory at the quota pace"
    )
    result = tokenize_text(text, resolved)
    assert "9,417 shares" in result.text  # untouched, never silently corrected
    assert f"{{{{fact:{SELL}}}}}" not in result.text
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.check == GateCheck.FACT_LITERAL_DRIFT
    assert "9,417" in v.detail
    assert SELL in v.detail


def test_bare_exclude_adjective_not_attached_to_a_number_still_substitutes_when_matching():
    # Same sentence shape, but the literal now EQUALS canonical — the
    # bare "eligible ... inventory ... quota pace" adjective still must not
    # disqualify the candidate, so it substitutes normally.
    resolved = _resolved(**{SELL: (9417, "shares")})
    text = (
        "NVDA: the forward glide sells 9,417 shares from Section-102 "
        "capital-track-eligible inventory at the quota pace"
    )
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{SELL}}}}}" in result.text
    assert "9,417" not in result.text
    assert result.violations == []


def test_exclude_term_attached_to_its_own_number_still_excludes():
    # "quota remaining" sits directly against ITS OWN number (3,924 sh) —
    # self-overlap counts as "attached to a number" (gap 0) — so the SELL
    # anchor must not fire at all: no substitution, no drift violation.
    resolved = _resolved(**{SELL: (9479, "shares")})
    text = "3,924 sh of tax-year 2026 quota remaining, well under the annual allowance."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []
    assert result.substitutions == []


def test_bare_digit_group_like_section_102_does_not_count_as_a_bound_number():
    # "Section-102" contains digits but is never followed by "shares"/"sh",
    # so it must never count as a "number the exclude term is attached to"
    # for proximity purposes — only true unit-candidates (matched by the
    # unit's own value regex) do.
    resolved = _resolved(**{SELL: (9417, "shares")})
    text = "NVDA: the glide sells 9,417 shares from Section-102 eligible inventory at the quota pace"
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{SELL}}}}}" in result.text


def test_tokenized_output_is_idempotent_after_exclude_proximity_fix():
    resolved = _resolved(**{SELL: (9417, "shares")})
    text = (
        "NVDA: the forward glide sells 9,417 shares from Section-102 "
        "capital-track-eligible inventory at the quota pace"
    )
    once = tokenize_text(text, resolved)
    twice = tokenize_text(once.text, resolved)
    assert twice.text == once.text
    assert twice.violations == []
    assert twice.substitutions == []


# ---------------------------------------------------------------------------
# 2. Drift surfaced as a violation and NOT rewritten.
# ---------------------------------------------------------------------------


def test_drifted_share_count_is_flagged_not_rewritten():
    resolved = _resolved(**{SELL: (9479, "shares")})
    text = "The forward glide sells 9,417 shares from Section-102 inventory. NVDA weight is high."
    result = tokenize_text(text, resolved)
    assert "9,417 shares" in result.text  # untouched
    assert "{{fact:" not in result.text
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.check == GateCheck.FACT_LITERAL_DRIFT
    assert "9,417" in v.detail
    assert SELL in v.detail


def test_drifted_pct_is_flagged_not_rewritten():
    resolved = _resolved(**{TARGET_PCT: (0.08, "pct")})
    text = "NVDA 8% policy target is about 1,508 shares — restated: the steering target sits at 9%."
    result = tokenize_text(text, resolved)
    # "9%" near "steering target" diverges from the canonical 8% -> drift.
    assert any(v.check == GateCheck.FACT_LITERAL_DRIFT for v in result.violations)
    assert "9%" in text  # never mutated


# ---------------------------------------------------------------------------
# 3. No substitution / no scanning inside an existing {{fact:...}} token.
# ---------------------------------------------------------------------------


def test_existing_token_is_never_touched():
    resolved = _resolved(**{SELL: (9479, "shares")})
    text = "The glide sells {{fact:concentration.nvda_sell_sh}} shares (NVDA)."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []
    assert result.substitutions == []


def test_number_inside_code_block_is_never_touched():
    resolved = _resolved(**{SELL: (9479, "shares")})
    text = "NVDA sells shares per the glide.\n```\nsell 9,417 shares\n```\n"
    result = tokenize_text(text, resolved)
    assert "9,417" in result.text  # inside the fence — untouched
    assert result.violations == []  # masked, never scanned
    assert result.substitutions == []


def test_number_inside_quoted_extract_is_never_touched():
    resolved = _resolved(**{ELIGIBLE: (9230, "shares")})
    text = (
        'NVDA capital-track-eligible inventory citation: '
        '"Section-102 capital-track-eligible inventory = 9,417 shares" — but the '
        "body states eligible inventory is 9,230 shares now."
    )
    result = tokenize_text(text, resolved)
    # The quoted (wrong-looking) 9,417 must never be scanned/flagged/mutated.
    assert '"Section-102 capital-track-eligible inventory = 9,417 shares"' in result.text
    # The un-quoted matching literal outside the quote IS tokenized.
    assert f"{{{{fact:{ELIGIBLE}}}}}" in result.text
    assert all("9,417" not in v.detail for v in result.violations)


# ---------------------------------------------------------------------------
# 4. No false positive on an unrelated number of the same magnitude.
# ---------------------------------------------------------------------------


def test_unrelated_number_same_magnitude_is_left_alone():
    resolved = _resolved(**{SELL: (9479, "shares")})
    # 9,479 appears, but with no "sell/trim/glide" anchor nearby and no
    # "shares" suffix at all — must not be touched or flagged.
    text = "The household spends about 9,479 NIS a month on groceries."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []
    assert result.substitutions == []


def test_share_count_without_nvda_proximity_is_left_alone():
    resolved = _resolved(**{SELL: (9479, "shares")})
    # Anchor verb + right magnitude + "shares" suffix, but NVDA is never
    # mentioned anywhere nearby — a different instrument's sale.
    text = "The plan sells 9,479 shares of a broad-market index fund next quarter."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []


def test_pending_fact_key_is_never_scanned():
    # No resolved value at all for this key -> the whole concept is skipped,
    # not scanned as a "no canonical to compare" drift.
    resolved = ResolvedPlanNumbers(values={})
    text = "The forward glide sells 9,417 shares. NVDA weight is high."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []


# ---------------------------------------------------------------------------
# 5. Percent vs fraction handling.
# ---------------------------------------------------------------------------


def test_percent_sign_and_bare_fraction_both_tokenize_to_same_key():
    resolved = _resolved(**{CAP: (0.12, "pct")})
    text_pct = "NVDA sits against a binding instrument-level ceiling of 12.0%."
    text_frac = "NVDA sits against a binding instrument-level ceiling of 0.12."
    r1 = tokenize_text(text_pct, resolved)
    r2 = tokenize_text(text_frac, resolved)
    assert f"{{{{fact:{CAP}}}}}" in r1.text
    assert f"{{{{fact:{CAP}}}}}" in r2.text


def test_fraction_is_not_confused_with_a_different_percent_value():
    # 0.12 near the cap anchor should NOT tokenize as an 8% target.
    resolved = _resolved(**{TARGET_PCT: (0.08, "pct")})
    text = "NVDA sits against a binding instrument-level ceiling of 0.12."
    result = tokenize_text(text, resolved)
    # No target-pct anchor phrase is present at all -> left alone, not flagged.
    assert result.text == text
    assert result.violations == []


def test_current_weight_percent_matches_within_rounding_tolerance():
    resolved = _resolved(**{CURRENT: (0.5988216115354782, "pct")})
    text = "**NVDA current weight**: 59.9% — snapshot-derived."
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{CURRENT}}}}}" in result.text


# ---------------------------------------------------------------------------
# tokenize_bodies — multi-horizon wrapper.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. Rule 1 — exclusion is subordinate to the spec's own concept proximity.
# ---------------------------------------------------------------------------


def test_exclude_term_farther_than_own_concept_does_not_disqualify():
    # "(retains 1,523 shares)": target's own concept `retains` sits 1 char
    # from the literal; the excluding term `sell` sits ~10 chars away. Per
    # Rule 1, target's own nearer concept wins — the exclusion must NOT
    # fire, so target's anchor holds and the drift (literal 1,523 vs
    # canonical 1,508) is attributed to TARGET_SH, not SELL.
    resolved = _resolved(**{SELL: (9479, "shares"), TARGET_SH: (1508, "shares")})
    text = (
        "**NVDA settled execution glide — forward shares to sell "
        "(retains 1,523 shares)**: 9417 sh"
    )
    result = tokenize_text(text, resolved)
    matches_1523 = [v for v in result.violations if "1,523" in v.detail]
    assert len(matches_1523) == 1
    assert TARGET_SH in matches_1523[0].detail
    assert SELL not in matches_1523[0].detail


# ---------------------------------------------------------------------------
# 7. Rule 2 — exclusive drift arbitration (at most one violation per literal).
# ---------------------------------------------------------------------------


def test_rule_2a_literal_matching_sibling_canonical_produces_no_drift():
    # "9230 sh" equals ELIGIBLE's canonical exactly, but ELIGIBLE's concept
    # ("eligible") sits outside SELL's clause window here while SELL's own
    # concept ("sold") is close — SELL's anchor fires. Rule 2(a): since the
    # literal equals a sibling's canonical, no drift is reported at all.
    resolved = _resolved(
        **{SELL: (9479, "shares"), ELIGIBLE: (9230, "shares")}
    )
    text = (
        "NVDA shares eligible for the capital-gains rate (the most that can "
        "be sold at that rate today): 9230 sh"
    )
    result = tokenize_text(text, resolved)
    assert result.violations == []


def test_rule_2b_value_nearest_canonical_owns_the_violation():
    # "9417 sh" is close in VALUE to SELL's canonical (9,479) and far from
    # TARGET_SH's canonical (1,461) — even if TARGET's concept text happens
    # to sit nearby too, value-nearest must attribute the drift to SELL.
    resolved = _resolved(
        **{SELL: (9479, "shares"), TARGET_SH: (1461, "shares")}
    )
    text = (
        "**NVDA settled execution glide — forward shares to sell "
        "(retains 1,523 shares)**: 9417 sh"
    )
    result = tokenize_text(text, resolved)
    matches_9417 = [v for v in result.violations if "9417" in v.detail]
    assert len(matches_9417) == 1
    assert SELL in matches_9417[0].detail
    assert TARGET_SH not in matches_9417[0].detail


def test_at_most_one_violation_per_literal_span():
    resolved = _resolved(
        **{SELL: (9479, "shares"), TARGET_SH: (1461, "shares")}
    )
    text = (
        "**NVDA settled execution glide — forward shares to sell "
        "(retains 1,523 shares)**: 9417 sh"
    )
    result = tokenize_text(text, resolved)
    spans_seen = [v.detail.split("`")[1] for v in result.violations]
    assert len(spans_seen) == len(set(spans_seen))


def test_quota_remaining_literal_still_never_claimed_by_sell():
    # Regression guard: the "3,924 sh ... quota remaining" exclusion (a
    # DIFFERENT concept — the annual sale-allowance balance) must still hold
    # under the new Rule 1 / Rule 2 arbitration.
    resolved = _resolved(**{SELL: (9479, "shares"), TARGET_SH: (1508, "shares")})
    text = "3,924 sh of tax-year 2026 quota remaining, well under the annual allowance."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []
    assert result.substitutions == []


# ---------------------------------------------------------------------------
# 8. Defect A — a hyphenated document reference is not a share-count match.
# ---------------------------------------------------------------------------


def test_hyphenated_statute_reference_is_not_a_share_count_candidate():
    # "Section-102" glues "102" to the preceding word via a bare hyphen —
    # not a quantity. Must yield no candidate at all for that "102" (no
    # substitution, no drift violation), even though canonical happens to be
    # exactly 102 and "shares" immediately follows.
    resolved = _resolved(**{SELL: (102, "shares")})
    text = "NVDA: the Section-102 shares that qualify for the capital-gains track."
    result = tokenize_text(text, resolved)
    assert result.text == text
    assert result.violations == []
    assert result.substitutions == []


def test_genuine_non_hyphenated_share_count_still_matches():
    # Same digits, same "shares" suffix, but NOT hyphen-glued to a preceding
    # word — must still match normally.
    resolved = _resolved(**{SELL: (102, "shares")})
    text = "NVDA: the glide sells 102 shares this tax year."
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{SELL}}}}}" in result.text
    assert result.violations == []


def test_dash_surrounded_by_spaces_still_matches_as_share_count():
    # A dash used as a sentence-level separator (spaces on both sides), not
    # glued to a preceding word — must not be treated as a hyphenated
    # identifier and must still match normally.
    resolved = _resolved(**{SELL: (102, "shares")})
    text = "NVDA: the glide sells the remainder - 102 shares - this tax year."
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{SELL}}}}}" in result.text


# ---------------------------------------------------------------------------
# 9. Defect B — the tax-year quota concept now has its own anchor.
# ---------------------------------------------------------------------------


def test_tax_year_quota_literal_is_attributed_to_quota_not_eligible():
    resolved = _resolved(
        **{QUOTA: (3924, "shares"), ELIGIBLE: (9230, "shares")}
    )
    text = "**NVDA tax-year 2026 quota remaining**: 3924 sh"
    result = tokenize_text(text, resolved)
    # Bound to the quota concept (equals its canonical) -> tokenized, no
    # drift, and specifically NOT attributed to ELIGIBLE (the old bug).
    assert f"{{{{fact:{QUOTA}}}}}" in result.text
    assert "3924" not in result.text
    assert result.violations == []
    assert result.substitutions == [(QUOTA, "3924 sh")]


def test_tax_year_quota_drift_is_flagged_against_quota_not_eligible():
    resolved = _resolved(
        **{QUOTA: (3900, "shares"), ELIGIBLE: (9230, "shares")}
    )
    text = "**NVDA tax-year 2026 quota remaining**: 3924 sh"
    result = tokenize_text(text, resolved)
    assert "3924" in result.text  # untouched, never silently corrected
    assert len(result.violations) == 1
    v = result.violations[0]
    assert v.check == GateCheck.FACT_LITERAL_DRIFT
    assert QUOTA in v.detail
    assert ELIGIBLE not in v.detail


def test_quota_spec_never_claims_sell_eligible_or_target_counts():
    # The quota concept's own anchor (quota + tax-year/year) must never fire
    # on the sell/eligible/target sentences, and their own anchors must
    # still correctly claim their own numbers.
    resolved = _resolved(
        **{
            SELL: (9479, "shares"),
            TARGET_SH: (1508, "shares"),
            ELIGIBLE: (9230, "shares"),
            QUOTA: (3924, "shares"),
        }
    )
    text = (
        "NVDA: the forward glide sells 9,479 shares (retains 1,508 shares); "
        "NVDA shares eligible for the capital-gains rate today: 9230 sh."
    )
    result = tokenize_text(text, resolved)
    assert f"{{{{fact:{SELL}}}}}" in result.text
    assert f"{{{{fact:{TARGET_SH}}}}}" in result.text
    assert f"{{{{fact:{ELIGIBLE}}}}}" in result.text
    assert f"{{{{fact:{QUOTA}}}}}" not in result.text
    assert result.violations == []


# ---------------------------------------------------------------------------
# 10. Guard — every anchored key must be registered for rendering.
# ---------------------------------------------------------------------------


def test_every_default_anchor_key_is_registered_in_fact_display():
    # Regression guard for the concentration.nvda_quota_tax_year_sh incident:
    # an AnchorSpec was added to DEFAULT_ANCHORS (making the key
    # substitutable into {{fact:key}}) without a matching FACT_DISPLAY entry.
    # tokenize_text's Phase-2 drift path used to paper over the gap with
    # FACT_DISPLAY.get(spec.key, spec.unit) — the bug is that the ACTUAL
    # renderer for the {{fact:...}} token it substitutes, fact_registry.
    # render_fact / fact_token_render.render_fact, has no such fallback and
    # raises PlaceholderError -> PENDING_LABEL ("[derivation pending]") in
    # the user's live plan. An anchored key that can be substituted MUST be
    # renderable, so every DEFAULT_ANCHORS key must appear in FACT_DISPLAY.
    from argosy.quality.fact_registry import FACT_DISPLAY
    from argosy.quality.fact_tokenizer import DEFAULT_ANCHORS

    missing = [spec.key for spec in DEFAULT_ANCHORS if spec.key not in FACT_DISPLAY]
    assert missing == [], f"anchored but not registered for rendering: {missing}"


def test_tokenize_bodies_aggregates_across_horizons():
    resolved = _resolved(**{SELL: (9479, "shares")})
    bodies = {
        "long": "NVDA glide sells 9,479 shares this year.",
        "medium": "NVDA glide sells 9,417 shares this year.",
        "short": "",
    }
    new_bodies, violations, subs = tokenize_bodies(bodies, resolved)
    assert f"{{{{fact:{SELL}}}}}" in new_bodies["long"]
    assert "9,417 shares" in new_bodies["medium"]
    assert len(violations) == 1
    assert violations[0].locator.startswith("horizon=medium")
    assert subs == [("long", SELL, "9,479 shares")]
