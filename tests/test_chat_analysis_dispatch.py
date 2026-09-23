from __future__ import annotations

from dataclasses import dataclass

import pytest

from argosy.services.chat_advisor.analysis_dispatch import AnalysisDispatcher
from argosy.services.chat_advisor.contracts import ExecutionPolicy, Principal


@dataclass
class Outcome:
    status: str = "approved"
    decision_run_id: int = 42
    proposal_id: int = 7
    action: str = "hold"
    blocked_reason: str | None = None
    blocked_by: str | None = None


class Store:
    household_user_id = "ariel"

    def __init__(self):
        self.rows = {}
        self.by_inbound = {}
        self.progress_rows = []
        self.created = 0

    async def get_request_by_inbound(self, inbound_message_id):
        rid = self.by_inbound.get(inbound_message_id)
        return self.rows.get(rid)

    async def create_request(self, *, inbound_message_id, channel_id, instruments, prompt_text):
        self.created += 1
        rid = f"request-{self.created}"
        row = {"id": rid, "instruments": instruments, "state": "queued", "run_ids": [], "prompt_text": prompt_text}
        self.rows[rid] = row
        self.by_inbound[inbound_message_id] = rid
        return row

    async def get_request(self, request_id):
        return self.rows.get(request_id)

    async def update_request(self, request_id, *, status, error=None, result=None):
        row = self.rows[request_id]
        row.update(state=status, error=error)
        if result is not None:
            row["result"] = result
        return row

    async def link_run(self, request_id, run_id):
        if run_id not in self.rows[request_id]["run_ids"]:
            self.rows[request_id]["run_ids"].append(run_id)

    async def list_recoverable_requests(self):
        return [r for r in self.rows.values() if r["state"] in {"queued", "running"}]

    async def append_progress(self, **event):
        self.progress_rows.append(event)

    async def get_progress(self, request_id):
        return [p for p in self.progress_rows if p["request_id"] == request_id]


def principal(user="ariel"):
    return Principal(
        household_user_id=user, guild_id="g", channel_id="c", user_id="discord-u"
    )


@pytest.mark.asyncio
async def test_request_is_persisted_idempotent_and_bounded():
    store = Store()
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    first = await dispatcher.request(principal(), ["$nvda", "msft"], "message-1")
    replay = await dispatcher.request(principal(), ["AAPL"], "message-1")
    assert first.id == replay.id
    assert first.instruments == ["NVDA", "MSFT"]
    assert store.created == 1
    with pytest.raises(ValueError, match="at most 3"):
        await dispatcher.request(principal(), ["A", "B", "C", "D"], "message-2")
    with pytest.raises(ValueError, match="numeric TASE security IDs"):
        await dispatcher.request(principal(), ["5139951"], "message-3")
    with pytest.raises(PermissionError):
        await dispatcher.progress(principal("another-household"), first.id)


@pytest.mark.asyncio
async def test_worker_uses_canonical_t2_analysis_only_and_links_run():
    store = Store()
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs)
        await kwargs["on_run_opened"](42)
        return Outcome()

    async def unowned(user_id, ticker):
        return False

    dispatcher = AnalysisDispatcher(
        store, runner=runner, ownership_classifier=unowned, auto_schedule=False
    )
    async def canonical_result(**kwargs):
        return {"rationale": "persisted verdict", "falsifiers": ["trial failure"]}
    dispatcher._canonical_result = canonical_result
    request = await dispatcher.request(principal(), ["QURE"], "message-1", review_context="Dated thesis-changing event")
    await dispatcher.worker_entry(request.id)
    assert calls[0]["tier"].value == "T2"
    assert calls[0]["execution_policy"] is ExecutionPolicy.ANALYSIS_ONLY
    assert calls[0]["subject_type"] == "discovery"
    assert calls[0]["consult_mode"] == "long_hold"
    assert calls[0]["review_context"] == "Dated thesis-changing event"
    assert store.rows[request.id]["state"] == "completed"
    assert store.rows[request.id]["run_ids"] == [42]
    assert store.rows[request.id]["result"]["outcomes"][0]["action"] == "hold"
    assert store.rows[request.id]["result"]["outcomes"][0]["rationale"] == "persisted verdict"


@pytest.mark.asyncio
async def test_reused_run_is_reference_only_never_owned_or_closed_by_new_request():
    store = Store()
    closed = []
    async def runner(**kwargs):
        return {"status": "blocked", "decision_run_id": 42, "blocked_by": "verdict_defended",
                "news_assessment": {"standing_verdict_id": 7, "disposition": "reuse"}}
    async def owned(*args):
        return True
    dispatcher = AnalysisDispatcher(store, runner=runner, ownership_classifier=owned, auto_schedule=False)
    async def failed_read(**kwargs):
        assert kwargs["standing_verdict_id"] == 7
        raise RuntimeError("temporary read failure")
    async def close(run_ids, status):
        closed.extend(run_ids)
    dispatcher._canonical_result = failed_read
    dispatcher._close_runs = close
    request = await dispatcher.request(principal(), ["ABC"], "m", review_context="news")
    await dispatcher.worker_entry(request.id)
    assert store.rows[request.id]["run_ids"] == []
    assert closed == []
    assert store.rows[request.id]["state"] == "failed"


@pytest.mark.asyncio
async def test_canonical_reuse_pins_verdict_id_and_household(engine):
    from argosy.state import db as db_mod
    from argosy.state.models import User, Verdict
    async with db_mod.get_session() as session:
        session.add_all([User(id="ariel"), User(id="other")])
        await session.flush()
        first = Verdict(user_id="ariel", subject="ABC", verdict="HOLD", conviction="LOW", reasoning_md="Reviewed thesis")
        other = Verdict(user_id="other", subject="ABC", verdict="SELL", conviction="HIGH", reasoning_md="Private")
        session.add_all([first, other])
        await session.commit()
        first_id, other_id = first.id, other.id
    dispatcher = AnalysisDispatcher(Store(), auto_schedule=False)
    result = await dispatcher._canonical_result(ticker="ABC", run_id=None, proposal_id=None,
        allow_standing=True, standing_verdict_id=first_id)
    assert result["verdict_id"] == first_id and result["rationale"] == "Reviewed thesis"
    forbidden = await dispatcher._canonical_result(ticker="ABC", run_id=None, proposal_id=None,
        allow_standing=True, standing_verdict_id=other_id)
    assert forbidden["verdict_id"] is None


@pytest.mark.asyncio
async def test_cancel_and_retry_remain_owner_scoped():
    store = Store()
    dispatcher = AnalysisDispatcher(store, auto_schedule=False)
    request = await dispatcher.request(principal(), ["NVDA"], "message-1")
    cancelled = await dispatcher.cancel(principal(), request.id)
    assert cancelled.state == "cancelled"
    retried = await dispatcher.retry(principal(), request.id)
    assert retried.state == "queued"


@pytest.mark.asyncio
async def test_restart_fails_running_request_without_duplicate_fleet():
    store = Store()
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs)

    dispatcher = AnalysisDispatcher(store, runner=runner, auto_schedule=False)
    request = await dispatcher.request(principal(), ["NVDA"], "message-1")
    store.rows[request.id].update(state="running", run_ids=[77])
    closed = []

    async def close_runs(run_ids, status):
        closed.append((list(run_ids), status))

    dispatcher._close_runs = close_runs
    assert await dispatcher.recover() == []
    assert calls == []
    assert closed == [([77], "failed")]
    assert store.rows[request.id]["state"] == "failed"
    assert "explicitly retry" in store.rows[request.id]["error"]


@pytest.mark.asyncio
async def test_request_schedules_once_and_can_be_awaited():
    store = Store()
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs["ticker"])
        await kwargs["on_run_opened"](42)
        return Outcome()

    async def owned(user_id, ticker):
        return True

    dispatcher = AnalysisDispatcher(store, runner=runner, ownership_classifier=owned)

    async def canonical_result(**kwargs):
        return {}

    dispatcher._canonical_result = canonical_result
    request = await dispatcher.request(principal(), ["NVDA"], "message-auto")
    await dispatcher.schedule(request.id)
    await dispatcher.wait(request.id)
    assert calls == ["NVDA"]
    assert store.rows[request.id]["state"] == "completed"
