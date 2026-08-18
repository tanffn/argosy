"""Tests for argosy.orchestrator.flows.plan_amendment.section_freeze.

Covers the section-merge helper that makes "amend, never
full-regenerate" actually freeze untouched sections (see the module
docstring for the measured regression this closes: plan 106 -> 107 lost
three sections outright and rewrote every other one, even though
guidance named exactly two).
"""

from __future__ import annotations

from argosy.orchestrator.flows.plan_amendment.section_freeze import (
    PREAMBLE_KEY,
    merge_frozen_sections,
)

PRIOR = """# Long horizon

## Targets
- **NVDA cap**: 15% of net worth

## Appendix — Section-by-section evidence
### Cover & Key Assumptions — `cover_assumptions` (long)
Cover text, prior version.

### FIRE Bridge — `fi_bridge` (medium)
Bridge text, prior version.

### Monte Carlo / Solvency — `monte_carlo` (medium)
Monte Carlo text, prior version.

### Single-Stock Concentration (NVDA) — `concentration` (medium)
Concentration text, prior version — 15% cap.
"""


def test_section_not_in_allow_is_byte_identical_to_prior():
    new = PRIOR.replace("15% of net worth", "12% of net worth").replace(
        "Cover text, prior version.", "Cover text, REWRITTEN by model."
    )
    merged, notes = merge_frozen_sections(PRIOR, new, allow={"concentration"})

    # cover_assumptions was not requested — must be byte-identical to prior,
    # even though the model rewrote it in `new`.
    assert "Cover text, prior version." in merged
    assert "Cover text, REWRITTEN by model." not in merged
    assert any("cover_assumptions" in n and "frozen" in n for n in notes)


def test_section_in_allow_takes_new_text():
    new = PRIOR.replace(
        "Concentration text, prior version — 15% cap.",
        "Concentration text, NEW version — 12% cap.",
    )
    merged, notes = merge_frozen_sections(PRIOR, new, allow={"concentration"})

    assert "Concentration text, NEW version — 12% cap." in merged
    assert "Concentration text, prior version — 15% cap." not in merged
    assert any("concentration" in n and "updated" in n for n in notes)


def test_section_dropped_by_model_is_restored():
    """The monte_carlo regression: prior has it, new dropped it outright."""
    start = PRIOR.index("### Monte Carlo / Solvency")
    end = PRIOR.index("### Single-Stock Concentration")
    new = PRIOR[:start] + PRIOR[end:]
    assert "monte_carlo" not in new

    merged, notes = merge_frozen_sections(PRIOR, new, allow={"concentration"})

    assert "`monte_carlo`" in merged
    assert "Monte Carlo text, prior version." in merged
    assert any("monte_carlo" in n and "restored" in n for n in notes)


def test_unrequested_new_section_is_dropped_with_note():
    new = PRIOR + "\n### Brand New Analysis — `brand_new` (long)\nSurprise section.\n"
    merged, notes = merge_frozen_sections(PRIOR, new, allow={"concentration"})

    assert "brand_new" not in merged
    assert "Surprise section." not in merged
    assert any("brand_new" in n and "dropped" in n for n in notes)


def test_slug_matching_survives_heading_rename():
    new = PRIOR.replace(
        "### FIRE Bridge — `fi_bridge` (medium)",
        "### FIRE / Bridge Plan — `fi_bridge` (long)",
    ).replace("Bridge text, prior version.", "Bridge text, RENAMED heading, new body.")

    # fi_bridge not in allow -> frozen -> prior heading + body preserved,
    # despite the new doc renaming the heading text entirely.
    merged, notes = merge_frozen_sections(PRIOR, new, allow={"concentration"})

    assert "### FIRE Bridge — `fi_bridge` (medium)" in merged
    assert "Bridge text, prior version." in merged
    assert "FIRE / Bridge Plan" not in merged
    assert "RENAMED heading" not in merged


def test_unslugged_heading_matches_via_normalized_key():
    new = PRIOR.replace(
        "## Targets\n- **NVDA cap**: 15% of net worth",
        "## Targets\n- **NVDA cap**: 12% of net worth",
    )
    # `## Targets` has no backtick slug — must still be matchable/freezable.
    merged, notes = merge_frozen_sections(PRIOR, new, allow={"concentration"})

    assert "15% of net worth" in merged
    assert "12% of net worth" not in merged
    assert any(n.startswith("targets:") and "frozen" in n for n in notes)


def test_freeze_except_none_is_not_supported_by_this_function():
    """merge_frozen_sections itself always takes an explicit allow set;
    the "no merge at all" behaviour (freeze_except=None) is a worker-level
    concern, tested at that layer. Sanity: an empty allow set freezes
    everything, including sections with no slug."""
    new = PRIOR.replace("Cover text, prior version.", "Cover text, changed.")
    merged, notes = merge_frozen_sections(PRIOR, new, allow=set())

    assert merged == PRIOR
    assert any("frozen" in n for n in notes)


def test_preamble_preserved_unless_allowed():
    prior = "Intro text.\n\n" + PRIOR
    new = "New intro text.\n\n" + PRIOR

    merged, notes = merge_frozen_sections(prior, new, allow={"concentration"})
    assert merged.startswith("Intro text.")

    merged2, notes2 = merge_frozen_sections(prior, new, allow={PREAMBLE_KEY, "concentration"})
    assert merged2.startswith("New intro text.")
