"""A slice that omits roster entries must fail INSIDE the retry envelope.

Regression pin for run 456 (2026-08-24). The ``sections_long`` expansion
returned successfully while omitting all 11 of its roster entries. Because
completeness was only tested at ASSEMBLY — minutes later, after the slice
had already been written to ``decision_phases`` — the retry envelope saw a
success, the checkpoint was persisted as good, and the whole sliced path
degraded to the monolith, which then burned three 900s timeouts.

Two invariants pinned here:

1. ``_slice_completeness_error`` uses the SAME join rules as assembly's
   ``_pair_roster`` (shared ``_roster_missing``), so the pre-checkpoint
   check and the assembly check cannot drift apart.
2. ``_pair_roster``'s existing contract is unchanged by that extraction —
   keyed join, positional fallback on equal counts, loud raise otherwise.
"""
from __future__ import annotations

import pytest

from argosy.orchestrator.flows.plan_synthesis.sliced_phase3 import (
    SlicedAssemblyError,
    _pair_roster,
    _roster_missing,
    _slice_completeness_error,
)


class _Labelled:
    def __init__(self, label):
        self.label = label


class _RosterEntry:
    def __init__(self, section_id, horizon):
        self.section_id = section_id
        self.horizon = horizon


class _Horizon:
    def __init__(self, targets=(), themes=(), actions=()):
        self.targets = list(targets)
        self.theme_roster = list(themes)
        self.action_roster = list(actions)


class _Skeleton:
    def __init__(self, section_roster=(), **horizons):
        self.section_roster = list(section_roster)
        for h in ("long", "medium", "short"):
            setattr(self, h, horizons.get(h) or _Horizon())


class _SectionBatch:
    def __init__(self, sections):
        self.sections = list(sections)


class _HorizonOut:
    def __init__(self, targets=(), themes=(), actions=()):
        self.targets = list(targets)
        self.themes = list(themes)
        self.actions = list(actions)


# --- the exact run-456 shape -------------------------------------------

RUN_456_ROSTER = [
    "client_goals", "net_worth", "capital_sufficiency", "concentration",
    "ips", "estate", "monte_carlo", "withdrawal", "insurance",
    "healthcare", "life_events",
]


def test_run_456_sections_long_hole_is_caught():
    """11 roster entries, zero emitted — the live failure."""
    skeleton = _Skeleton(
        section_roster=[_RosterEntry(s, "long") for s in RUN_456_ROSTER]
    )
    err = _slice_completeness_error(
        "sections_long", _SectionBatch([]), skeleton
    )
    assert err is not None
    assert "client_goals" in err
    assert "0 emitted for 11 roster entries" in err


def test_partial_section_emission_is_caught():
    skeleton = _Skeleton(
        section_roster=[_RosterEntry(s, "long") for s in RUN_456_ROSTER]
    )
    emitted = [_RosterEntry(s, "long") for s in RUN_456_ROSTER[:4]]
    err = _slice_completeness_error(
        "sections_long", _SectionBatch(emitted), skeleton
    )
    assert err is not None and "estate" in err


def test_complete_section_slice_passes():
    skeleton = _Skeleton(
        section_roster=[_RosterEntry(s, "long") for s in RUN_456_ROSTER]
    )
    emitted = [_RosterEntry(s, "long") for s in reversed(RUN_456_ROSTER)]
    assert _slice_completeness_error(
        "sections_long", _SectionBatch(emitted), skeleton
    ) is None


def test_other_horizons_roster_is_not_demanded_of_this_slice():
    """``sections_long`` owns only the long-horizon roster entries."""
    skeleton = _Skeleton(section_roster=[
        _RosterEntry("estate", "long"),
        _RosterEntry("cashflow", "short"),
        _RosterEntry("rebalance", "medium"),
    ])
    assert _slice_completeness_error(
        "sections_long", _SectionBatch([_RosterEntry("estate", "long")]),
        skeleton,
    ) is None


# --- horizon slices ----------------------------------------------------

def test_horizon_slice_missing_action_is_caught():
    skeleton = _Skeleton(long=_Horizon(
        targets=[_Labelled("Global equity")],
        themes=[_Labelled("Concentration")],
        actions=[_Labelled("Sell NVDA tranche"), _Labelled("Refresh ledger")],
    ))
    out = _HorizonOut(
        targets=[_Labelled("Global equity")],
        themes=[_Labelled("Concentration")],
        actions=[_Labelled("Sell NVDA tranche")],
    )
    err = _slice_completeness_error("long", out, skeleton)
    assert err is not None and "action" in err


def test_horizon_slice_complete_passes():
    skeleton = _Skeleton(short=_Horizon(
        targets=[_Labelled("Cash floor")], themes=[], actions=[],
    ))
    out = _HorizonOut(targets=[_Labelled("Cash floor")])
    assert _slice_completeness_error("short", out, skeleton) is None


# --- the shared join must keep assembly's exact semantics --------------

def test_pair_roster_keyed_join_unchanged():
    roster = [_Labelled("a"), _Labelled("b")]
    emitted = [_Labelled("b"), _Labelled("a")]
    pairs, dropped = _pair_roster(
        roster, emitted, lambda x: x.label, what="target", slice_name="long",
    )
    assert dropped == 0
    assert [(r.label, e.label) for r, e in pairs] == [("a", "a"), ("b", "b")]


def test_pair_roster_positional_fallback_on_equal_counts():
    """An adversarial slice may rename join keys; equal counts still pair."""
    roster = [_Labelled("a"), _Labelled("b")]
    emitted = [_Labelled("x"), _Labelled("y")]
    pairs, dropped = _pair_roster(
        roster, emitted, lambda x: x.label, what="target", slice_name="long",
    )
    assert dropped == 0
    assert [(r.label, e.label) for r, e in pairs] == [("a", "x"), ("b", "y")]


def test_pair_roster_still_raises_on_a_real_hole():
    roster = [_Labelled("a"), _Labelled("b"), _Labelled("c")]
    emitted = [_Labelled("a"), _Labelled("c")]
    with pytest.raises(SlicedAssemblyError) as exc:
        _pair_roster(
            roster, emitted, lambda x: x.label,
            what="target", slice_name="long",
        )
    assert "'b'" in str(exc.value)


def test_pair_roster_reports_inventions_without_failing():
    roster = [_Labelled("a")]
    emitted = [_Labelled("a"), _Labelled("invented"), _Labelled("also")]
    pairs, dropped = _pair_roster(
        roster, emitted, lambda x: x.label, what="theme", slice_name="long",
    )
    assert dropped == 2 and len(pairs) == 1


@pytest.mark.parametrize("roster,emitted,expected", [
    ([1, 2, 3], [1, 2, 3], []),
    ([1, 2, 3], [1, 3], ["2"]),
    ([1, 2, 3], [9, 8, 7], []),          # count-matched → positional
    ([1, 2, 3], [], ["1", "2", "3"]),
    ([], [1], []),                        # inventions only: not a hole
])
def test_roster_missing_table(roster, emitted, expected):
    assert _roster_missing(roster, emitted, lambda x: x) == expected



# --- the assembly occurrence key (run 456's SECOND failure) ------------

def _skeleton_with(section_ids, horizon="long"):
    from argosy.agents.plan_skeleton_synthesizer import (
        PlanSkeleton, SkeletonHorizon, SkeletonSectionEntry,
    )

    def _h(h):
        return SkeletonHorizon(
            horizon=h, freshness_expected="quarterly", status="no_change",
            posture_summary="steady",
        )

    return PlanSkeleton(
        long=_h("long"), medium=_h("medium"), short=_h("short"),
        section_roster=[
            SkeletonSectionEntry(
                section_id=s, horizon=horizon, one_line_thesis=f"thesis {s}",
            )
            for s in section_ids
        ],
    )


def _emitted(section_ids, horizon="long"):
    from argosy.agents.plan_synthesizer_types import SectionEvidence
    from argosy.agents.plan_slice_synthesizer import Section
    return [
        Section(
            section_id=s, horizon=horizon, title=s.replace("_", " ").title(),
            body_md=f"body for {s}", evidence=SectionEvidence(missing_data=["pinned in test"]),
        )
        for s in section_ids
    ]


def _horizon_outputs():
    from argosy.agents.plan_slice_synthesizer import HorizonSection
    return {
        h: HorizonSection(
            horizon=h, freshness_expected="quarterly", status="no_change",
            posture="steady",
        )
        for h in ("long", "medium", "short")
    }


def test_multi_section_horizon_assembles():
    """The third key element is the OCCURRENCE index, not the list position.

    Run 456 (2026-08-24) cleared the roster-omission bug and immediately hit
    "section roster entry (net_worth, long) has no assembled output". The
    store loop keyed ``assembled_by_key`` by ``enumerate(pairs)`` position
    while the reader counted occurrences of ``(section_id, horizon)``. Those
    agree only for the FIRST section of a horizon, so any horizon holding
    more than one section could never assemble — which is why the sliced
    path kept degrading to the monolith.
    """
    from argosy.orchestrator.flows.plan_synthesis.sliced_phase3 import (
        _assemble_sliced_output,
    )
    out, _ = _assemble_sliced_output(
        skeleton=_skeleton_with(RUN_456_ROSTER),
        horizon_outputs=_horizon_outputs(),
        section_outputs={"long": _emitted(RUN_456_ROSTER)},
    )
    assert [s.section_id for s in out.sections] == RUN_456_ROSTER


def test_repeated_section_id_keeps_both_occurrences():
    """The 3-tuple key exists to allow a roster to repeat a section."""
    from argosy.orchestrator.flows.plan_synthesis.sliced_phase3 import (
        _assemble_sliced_output,
    )
    ids = ["estate", "concentration", "estate"]
    out, _ = _assemble_sliced_output(
        skeleton=_skeleton_with(ids),
        horizon_outputs=_horizon_outputs(),
        section_outputs={"long": _emitted(ids)},
    )
    assert [s.section_id for s in out.sections] == ids


def test_single_section_horizon_still_assembles():
    """The shape that accidentally worked before must keep working."""
    from argosy.orchestrator.flows.plan_synthesis.sliced_phase3 import (
        _assemble_sliced_output,
    )
    out, _ = _assemble_sliced_output(
        skeleton=_skeleton_with(["estate"]),
        horizon_outputs=_horizon_outputs(),
        section_outputs={"long": _emitted(["estate"])},
    )
    assert [s.section_id for s in out.sections] == ["estate"]
