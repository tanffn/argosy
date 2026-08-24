"""Every resolver key must be renderable.

A key present in `plan_numeric_resolver._KEY_UNITS` but absent from
`fact_registry.FACT_DISPLAY` RESOLVES to a real number and then renders as
"[derivation pending]". That marker trips the fail-closed leakage gate, so the
plan cannot cite the figure at all — and the fleet types a literal instead,
which drifts between surfaces and comes back as a reviewer BLOCKER.

Three separate findings on runs 448/456 traced to exactly this:

  * codex [C1] "FI target cannot be independently reproduced" — the
    tracked-vs-agent T12 reconciliation needed `spend.annual_t12_donor_check_nis`;
  * reader "two different net-worth results for the same -10% adverse FX
    scenario" — `retirement.fi_shock_net_worth_nis` and
    `retirement.fi_fx_shock_net_worth_nis` were both untokenised;
  * the FIRE-bridge divergence — `retirement.fire_bridge_fi_age_estimate_nis`.

This pin turns the whole class into a test failure instead of a review round.
It is the same defect shape as commit 59f89a4 (the `income.*` keys), which is
why it is worth pinning rather than fixing case by case.
"""
from __future__ import annotations

import re

from argosy.quality.fact_registry import FACT_DISPLAY
from argosy.services.plan_numeric_resolver import _KEY_UNITS


def test_every_resolver_key_has_a_display_format():
    missing = sorted(k for k in _KEY_UNITS if k not in FACT_DISPLAY)
    assert not missing, (
        "resolver keys with no FACT_DISPLAY entry render as "
        "'[derivation pending]' and trip the leakage gate: " + ", ".join(missing)
    )


def test_the_six_keys_that_caused_reviewer_blockers_are_registered():
    """Explicit pin on the ones that actually cost review rounds."""
    for key in (
        "spend.annual_t12_donor_check_nis",
        "retirement.fi_shock_net_worth_nis",
        "retirement.fi_fx_shock_net_worth_nis",
        "retirement.fire_bridge_fi_age_estimate_nis",
        "portfolio.usd_exposure_nis",
        "concentration.nvda_value_nis",
    ):
        assert key in FACT_DISPLAY, f"{key} would render as pending"


def test_display_formats_are_known_values():
    """A typo'd format is as bad as a missing entry — it renders wrong."""
    # Derived from the formats `format_fact` actually branches on, so this
    # cannot drift into a hand-maintained guess.
    import inspect

    from argosy.quality.fact_registry import format_fact

    src = inspect.getsource(format_fact)
    known = set(re.findall(r'display == "([a-z_]+)"', src))
    assert known, "could not read the renderer's format vocabulary"
    unknown = sorted(
        f"{k}={v}" for k, v in FACT_DISPLAY.items() if v not in known
    )
    assert not unknown, "unrecognised display formats: " + ", ".join(unknown)
