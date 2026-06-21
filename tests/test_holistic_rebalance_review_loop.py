"""HolisticRebalanceReviewLoop tests — seam-injected (no live DB / composer).

Covers the SCHEDULING wiring only (the review logic lives in
``argosy/services/holistic_rebalance_review.py`` and is tested separately):

  * the loop calls ``run_holistic_rebalance_review`` with the expected args,
  * the loop surfaces the review's status / leg-count / proposal-written flag,
  * a raising review_fn degrades to an error summary (never crashes the tick),
  * the job metadata is registered with the expected name + source_kind.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from argosy.orchestrator.loops.holistic_rebalance_review import (
    HolisticRebalanceReviewLoop,
    holistic_rebalance_review_metadata,
)


class _FakeSession:
    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_metadata_name_and_source_kind() -> None:
    m = holistic_rebalance_review_metadata()
    assert m.name == "holistic_rebalance_review"
    assert m.source_kind == "monitor"
    assert m.long_running is False
    assert m.schedule_cron == "0 10 1 1,4,7,10 *"


@pytest.mark.asyncio
async def test_tick_calls_review_fn_and_surfaces_summary() -> None:
    calls: list[dict] = []

    def _review_fn(user_id, session, *, write_proposal, now):
        calls.append(
            {
                "user_id": user_id,
                "write_proposal": write_proposal,
                "now": now,
            }
        )
        review = SimpleNamespace(status="ok", legs=[object(), object()])
        return review, True

    loop = HolisticRebalanceReviewLoop(
        user_id="ariel",
        session_factory=lambda: _FakeSession(),
        review_fn=_review_fn,
    )
    summary = await loop.tick()

    assert len(calls) == 1
    assert calls[0]["user_id"] == "ariel"
    assert calls[0]["write_proposal"] is True
    assert calls[0]["now"] is not None  # tick passes a tz-aware run_at
    assert summary["status"] == "ok"
    assert summary["legs"] == 2
    assert summary["proposal_written"] is True
    assert summary["errors"] == []
    # last_output_summary mirrors the returned summary (adapter failure-path hook).
    assert loop.last_output_summary == summary


@pytest.mark.asyncio
async def test_tick_reports_no_write_when_not_actionable() -> None:
    def _review_fn(user_id, session, *, write_proposal, now):  # noqa: ARG001
        return SimpleNamespace(status="ok", legs=[]), False

    loop = HolisticRebalanceReviewLoop(
        user_id="ariel",
        session_factory=lambda: _FakeSession(),
        review_fn=_review_fn,
    )
    summary = await loop.tick()
    assert summary["status"] == "ok"
    assert summary["legs"] == 0
    assert summary["proposal_written"] is False


@pytest.mark.asyncio
async def test_tick_degrades_on_review_error() -> None:
    def _review_fn(*_a, **_k):
        raise RuntimeError("boom")

    loop = HolisticRebalanceReviewLoop(
        user_id="ariel",
        session_factory=lambda: _FakeSession(),
        review_fn=_review_fn,
    )
    summary = await loop.tick()
    assert summary["proposal_written"] is False
    assert summary["errors"] and "boom" in summary["errors"][0]
