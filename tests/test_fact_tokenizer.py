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
