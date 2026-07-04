"""HoldingsReviewJob: tick runs the review via a sync session and surfaces the
summary. Injected review_fn + session_factory — no live LLM / real DB."""
from __future__ import annotations

import asyncio

from argosy.services.jobs.holdings_review import (
    HoldingsReviewJob,
    holdings_review_metadata,
)


class _FakeSession:
    def close(self):
        pass


def test_metadata_shape():
    m = holdings_review_metadata()
    assert m.name == "holdings_review"
    assert m.source_kind == "monitor"
    assert m.long_running is True
    assert m.schedule_cron


def test_tick_runs_review_and_returns_summary():
    calls = {}

    def _review(session, user_id, *, min_position_usd):
        calls["user_id"] = user_id
        calls["min"] = min_position_usd
        calls["closed_after"] = False
        return {"reviewed": 3, "actionable": 1, "written": 1, "verdicts": []}

    job = HoldingsReviewJob(
        user_id="ariel", min_position_usd=5_000.0,
        session_factory=lambda: _FakeSession(),
        review_fn=_review,
    )
    out = asyncio.run(job.tick())
    assert out == {"reviewed": 3, "actionable": 1, "written": 1}
    assert job.last_output_summary == out
    assert calls["user_id"] == "ariel" and calls["min"] == 5_000.0


def test_tick_resets_summary_before_work():
    def _boom(session, user_id, *, min_position_usd):
        raise RuntimeError("review failed")

    job = HoldingsReviewJob(session_factory=lambda: _FakeSession(), review_fn=_boom)
    try:
        asyncio.run(job.tick())
    except RuntimeError:
        pass
    # side-channel not left holding a stale prior summary
    assert job.last_output_summary is None
