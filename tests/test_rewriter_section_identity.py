"""Force-preserve must protect ``section_id`` — the field it used to join on.

Regression pin for run 452 (2026-08-24), which died with 14 structural
violations, all "rewriter modified preserved field sections[N].section_id",
before risk/codex/FM/reader ran and without writing a draft.

Cause: ``_force_preserve_structured_fields`` indexed ``before.sections`` by
``(section_id, horizon)`` and looked the rewritten section up by the SAME key.
A key-based join cannot protect the field it joins on — when the rewriter
renamed an id the lookup missed, the code concluded "new section invented",
kept the renamed id, and the validator aborted the run.

Fix: when section COUNTS match, pair positionally and restore identity. When
counts differ the rewriter did something structural and must still fail loudly.
"""
from __future__ import annotations

import pytest

from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
    _force_preserve_structured_fields,
)


class _Ev:
    def __init__(self, tag):
        self.tag = tag

    def __eq__(self, other):
        return isinstance(other, _Ev) and other.tag == self.tag


class _Section:
    """Minimal stand-in exposing the fields force-preserve touches."""

    def __init__(self, section_id, horizon, evidence, body="body"):
        self.section_id = section_id
        self.horizon = horizon
        self.evidence = evidence
        self.body_md = body

    def model_copy(self, *, update):
        s = _Section(self.section_id, self.horizon, self.evidence, self.body_md)
        for k, v in update.items():
            setattr(s, k, v)
        return s


class _Horizon:
    def __init__(self):
        self.deltas_from_prior = []
        self.speculative_candidates = []
        self.targets = []

    def model_copy(self, *, update):
        h = _Horizon()
        h.__dict__.update(self.__dict__)
        h.__dict__.update(update)
        return h


class _Output:
    def __init__(self, sections):
        self.sections = sections
        self.inputs = "provenance"
        self.long = _Horizon()
        self.medium = _Horizon()
        self.short = _Horizon()

    def model_copy(self, *, update):
        o = _Output(self.sections)
        o.__dict__.update(self.__dict__)
        o.__dict__.update(update)
        return o


def test_renamed_section_id_is_restored_not_fatal():
    """The exact run-452 shape: the rewriter renames ids in place."""
    before = _Output([
        _Section("concentration", "medium", _Ev("e1")),
        _Section("tax_plan", "medium", _Ev("e2")),
    ])
    after = _Output([
        _Section("nvda_concentration", "medium", _Ev("TRANSLATED")),
        _Section("section_102_tax", "medium", _Ev("TRANSLATED")),
    ])
    out = _force_preserve_structured_fields(before=before, after=after)
    assert [s.section_id for s in out.sections] == ["concentration", "tax_plan"]
    # Evidence must also come back bit-for-bit.
    assert [s.evidence for s in out.sections] == [_Ev("e1"), _Ev("e2")]


def test_unchanged_ids_still_restore_evidence_only():
    before = _Output([_Section("estate", "long", _Ev("keep"))])
    after = _Output([_Section("estate", "long", _Ev("rewritten"), body="new")])
    out = _force_preserve_structured_fields(before=before, after=after)
    assert out.sections[0].section_id == "estate"
    assert out.sections[0].evidence == _Ev("keep")
    assert out.sections[0].body_md == "new", "prose must stay rewritable"


def test_count_mismatch_is_left_to_the_validator():
    """A dropped or invented section is genuinely structural — do NOT paper
    over it by pairing positionally against a different-length list."""
    before = _Output([
        _Section("a", "long", _Ev("e1")),
        _Section("b", "long", _Ev("e2")),
    ])
    after = _Output([_Section("renamed", "long", _Ev("x"))])
    out = _force_preserve_structured_fields(before=before, after=after)
    # Left as-is so the invariant validator fails loudly on it.
    assert out.sections[0].section_id == "renamed"


@pytest.mark.parametrize("horizon_drift", ["short", "long"])
def test_horizon_drift_is_also_restored(horizon_drift):
    before = _Output([_Section("cashflow", "medium", _Ev("e"))])
    after = _Output([_Section("cashflow_x", horizon_drift, _Ev("e"))])
    out = _force_preserve_structured_fields(before=before, after=after)
    assert out.sections[0].section_id == "cashflow"
    assert out.sections[0].horizon == "medium"
