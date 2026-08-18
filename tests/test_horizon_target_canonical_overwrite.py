"""RED-7 — horizon Targets must not lag the canonical TargetAllocationDoc.

Pins ``_overwrite_horizon_targets_from_canonical`` (RED-6 disease: a surface
trusting authored/LLM prose instead of the canonical structured document).
This is allocation money-math — the invariant must be pinned directly, not
implied by a passing call site.

Covers:
1. Exact-label match overwrites the authored value in place.
2. A label with no exact canonical counterpart passes through untouched
   (never fuzzy-matched — that's the RED-6 bug).
3. The overwrite log fires with authored + canonical values.
4. Canonical doc absent/unparseable degrades to a no-op, never a crash.
5. The coverage-gap log (no-silent-caps): canonical classes with no
   authored counterpart, and authored allocation-unit targets with no
   canonical counterpart, are both surfaced — never silently dropped.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from types import SimpleNamespace

import pytest

from argosy.agents.plan_synthesizer_types import SynthTarget
from argosy.orchestrator.flows.plan_synthesis.render import (
    _overwrite_horizon_targets_from_canonical,
)

_LOGGER_NAME = "argosy.orchestrator.flows.plan_synthesis.render"


def _target(label, value, unit="pct_of_portfolio"):
    return SynthTarget(
        label=label, value=value, unit=unit,
        stated_at=date(2026, 8, 18), revisit_after=date(2027, 2, 18),
    )


def _section(horizon, targets):
    return SimpleNamespace(horizon=horizon, targets=targets)


def _canonical_json(classes: dict[str, float]) -> str:
    return json.dumps({
        "classes": [
            {"label": label, "target_pct": pct}
            for label, pct in classes.items()
        ],
    })


def _output(medium_targets=(), long_targets=(), short_targets=()):
    return SimpleNamespace(
        long=_section("long", list(long_targets)),
        medium=_section("medium", list(medium_targets)),
        short=_section("short", list(short_targets)),
    )


def test_exact_label_match_overwrites_in_place():
    output = _output(medium_targets=[
        _target("US broad-market core", 28.5),
        _target("International developed (ex-US)", 14.3),
    ])
    canonical = _canonical_json({
        "US broad-market core": 26.92,
        "International developed (ex-US)": 13.51,
    })

    _overwrite_horizon_targets_from_canonical(output, canonical)

    assert output.medium.targets[0].value == 26.92
    assert output.medium.targets[1].value == 13.51


def test_no_exact_match_passes_through_untouched_never_fuzzy():
    # Authored label is a near-miss prose variant of the canonical label
    # (the exact RED-6 shape) — must NOT be fuzzy-matched or corrected.
    output = _output(medium_targets=[
        _target(
            "Global quality growth (screened to avoid NVDA-heavy names)",
            11.0,
        ),
    ])
    canonical = _canonical_json({
        "Global quality growth (ex-NVDA-dense)": 9.5,
    })

    _overwrite_horizon_targets_from_canonical(output, canonical)

    assert output.medium.targets[0].value == 11.0  # unchanged


def test_non_allocation_targets_never_touched():
    # unit='pct' (SWR/return assumptions) and unit='pct_of_liquid' (SGOV
    # floor) are legitimately not allocation classes.
    output = _output(
        long_targets=[_target("Expected real portfolio return", 5.0, unit="pct")],
        short_targets=[_target("SGOV target share of liquid assets", 4.0, unit="pct_of_liquid")],
    )
    canonical = _canonical_json({"US broad-market core": 26.92})

    _overwrite_horizon_targets_from_canonical(output, canonical)

    assert output.long.targets[0].value == 5.0
    assert output.short.targets[0].value == 4.0


def test_overwrite_log_fires_with_authored_and_canonical_values(
    caplog: pytest.LogCaptureFixture,
):
    output = _output(medium_targets=[_target("US broad-market core", 28.5)])
    canonical = _canonical_json({"US broad-market core": 26.92})

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _overwrite_horizon_targets_from_canonical(
            output, canonical, user_id="u1", decision_run_id=109,
        )

    relevant = [
        r for r in caplog.records
        if "horizon_targets_overwritten_from_canonical" in r.getMessage()
    ]
    assert len(relevant) == 1, [r.getMessage() for r in caplog.records]
    msg = relevant[0].getMessage()
    assert "28.5" in msg
    assert "26.92" in msg
    assert "US broad-market core" in msg


def test_canonical_doc_absent_is_a_noop_never_a_crash():
    output = _output(medium_targets=[_target("US broad-market core", 28.5)])

    _overwrite_horizon_targets_from_canonical(output, None)

    assert output.medium.targets[0].value == 28.5


def test_canonical_doc_unparseable_is_a_noop_never_a_crash(
    caplog: pytest.LogCaptureFixture,
):
    output = _output(medium_targets=[_target("US broad-market core", 28.5)])

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _overwrite_horizon_targets_from_canonical(output, "{not valid json")

    assert output.medium.targets[0].value == 28.5
    relevant = [
        r for r in caplog.records
        if "horizon_target_overwrite_parse_failed" in r.getMessage()
    ]
    assert len(relevant) == 1


def test_coverage_gap_logged_both_directions(caplog: pytest.LogCaptureFixture):
    # Authored side: an allocation-unit target with no canonical
    # counterpart (near-miss label — the plan-109 shape).
    output = _output(medium_targets=[
        _target(
            "Global quality growth (screened to avoid NVDA-heavy names)",
            11.0,
        ),
    ])
    # Canonical side: two classes with no authored counterpart at all.
    canonical = _canonical_json({
        "Global quality growth (ex-NVDA-dense)": 9.5,
        "Strategic single-stock (NVDA)": 8.0,
        "US low-volatility equity": 4.6,
    })

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _overwrite_horizon_targets_from_canonical(
            output, canonical, user_id="u1", decision_run_id=109,
        )

    relevant = [
        r for r in caplog.records
        if "horizon_target_canonical_coverage_gap" in r.getMessage()
    ]
    assert len(relevant) == 1, [r.getMessage() for r in caplog.records]
    msg = relevant[0].getMessage()
    # Both directions present.
    assert "Strategic single-stock (NVDA)" in msg
    assert "US low-volatility equity" in msg
    assert "Global quality growth (screened to avoid NVDA-heavy names)" in msg
    # The near-miss canonical label was NOT treated as covered — it must
    # still show up on the canonical-without-authored side.
    assert "Global quality growth (ex-NVDA-dense)" in msg


def test_coverage_gap_not_logged_when_everything_matches(
    caplog: pytest.LogCaptureFixture,
):
    output = _output(medium_targets=[_target("US broad-market core", 28.5)])
    canonical = _canonical_json({"US broad-market core": 26.92})

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _overwrite_horizon_targets_from_canonical(output, canonical)

    relevant = [
        r for r in caplog.records
        if "horizon_target_canonical_coverage_gap" in r.getMessage()
    ]
    assert not relevant
