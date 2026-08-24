"""A reader verdict, once obtained, survives a later empty re-read.

The coherence-reconcile loop re-invokes the whole-artifact reader and
reassigns ``(_reader_verdict, _reader_row)``. A re-read that degrades to
``(None, None)`` — reader kill-switch, dispatch failure, empty assemble —
used to CLEAR a verdict already held, so the ``phase_55`` row was never
persisted and ``read_reader_verdict`` returned ``None``. The promote gate
then failed closed on "authority never ran", which is indistinguishable from
a genuine BLOCK but carries none of its information.

Runs 456 and 461 both logged ``whole_artifact_reader.leakage_blocked`` — a
real, actionable BLOCK — and still recorded no phase-55 row. The reader
authority has never produced a verdict the gate could read.

This pins the latch: the newest NON-EMPTY result wins, and an empty re-read
is ignored rather than destructive.
"""
from __future__ import annotations

import pytest


class _Verdict:
    def __init__(self, assessment):
        self.overall_assessment = assessment

    def model_dump_json(self):
        return f'{{"overall_assessment": "{self.overall_assessment}"}}'


class _Row:
    def __init__(self, tag):
        self.tag = tag


def _latch():
    """The exact latch shape used in run_synthesis."""
    held = {"verdict": None, "row": None}

    def keep(verdict, row):
        if row is not None:
            held["verdict"], held["row"] = verdict, row
        return verdict, row

    return held, keep


def test_empty_reread_does_not_erase_a_block():
    """The live failure: leakage BLOCK, then a re-read returns (None, None)."""
    held, keep = _latch()
    v, r = keep(_Verdict("BLOCK"), _Row("leak"))
    assert r is not None

    v, r = keep(None, None)           # reconcile re-read degrades
    assert r is None, "the local pair still reflects the latest attempt"
    assert held["row"] is not None, "but the held row must survive"
    assert held["verdict"].overall_assessment == "BLOCK"


def test_newer_non_empty_result_supersedes():
    held, keep = _latch()
    keep(_Verdict("BLOCK"), _Row("first"))
    keep(_Verdict("APPROVE"), _Row("second"))
    assert held["row"].tag == "second"
    assert held["verdict"].overall_assessment == "APPROVE"


def test_never_obtained_stays_none():
    """No verdict ever produced must NOT be fabricated into one."""
    held, keep = _latch()
    keep(None, None)
    keep(None, None)
    assert held["row"] is None and held["verdict"] is None


@pytest.mark.parametrize("sequence,expected", [
    ([("BLOCK", "a"), (None, None), (None, None)], "BLOCK"),
    ([("BLOCK", "a"), ("APPROVE", "b"), (None, None)], "APPROVE"),
    ([(None, None), ("BLOCK", "c")], "BLOCK"),
])
def test_latch_sequences(sequence, expected):
    held, keep = _latch()
    for assessment, tag in sequence:
        keep(
            _Verdict(assessment) if assessment else None,
            _Row(tag) if tag else None,
        )
    assert held["verdict"].overall_assessment == expected


def test_orchestrator_wires_the_latch():
    """Guard against the wiring being dropped in a future edit."""
    import inspect

    from argosy.orchestrator.flows.plan_synthesis import orchestrator

    src = inspect.getsource(orchestrator)
    assert "_keep_reader" in src
    # Every reader invocation must go through the latch. A bare call is one
    # whose result is assigned directly rather than passed through _keep_reader.
    import re as _re

    bare = _re.findall(r"=\s*_assemble_and_read\(", src)
    assert not bare, (
        f"{len(bare)} reader call(s) bypass the latch and can erase a held "
        "verdict"
    )
    assert src.count("_assemble_and_read()") >= 4, "reader calls disappeared"
    assert "recovered_held_verdict" in src, "fallback at persist time missing"
