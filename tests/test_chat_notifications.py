from datetime import UTC, datetime

import pytest

from argosy.services.chat_advisor.contracts import Citation, Principal
from argosy.services.chat_advisor.notifications import (
    NotificationProducer,
    NotificationSourceUnavailable,
    material_version,
)
from argosy.services.chat_advisor.outbound import OutboundFilter
from argosy.services.inbox.types import (
    InboxAction,
    InboxFeed,
    InboxItem,
    InboxLiveness,
    PriorityBucket,
)


def _item(status="actionable"):
    return InboxItem(
        id="trade:1",
        kind="trade",
        title="Trim concentration",
        why_now="Above plan cap",
        primary_action=InboxAction("approve", "Review"),
        amount_usd=1000,
        body={"status": status, "currency": "USD", "rationale_version": "v1"},
        bucket=PriorityBucket.RISK_REDUCTION,
    )


def test_material_version_includes_semantic_title_and_rationale():
    a = _item()
    b = _item()
    b.title = "Different phrasing"
    assert material_version(a) != material_version(b)
    b = _item()
    b.body["rationale_version"] = "v2"
    assert material_version(a) != material_version(b)


def test_redaction_and_chunks_are_discord_safe():
    text = (
        '"account_id": "123456" U1234567 IL620108000000099999999 sk-ant-secretvalue123 "D:\\Google Drive\\Family Finances\\x.pdf" @everyone '
        + "x" * 4100
    )
    chunks = OutboundFilter.chunk(
        text, [Citation("proposal", "1", "2026-01-01", url="https://example.com/a")]
    )
    joined = "".join(chunks)
    assert all(len(chunk) <= 2000 for chunk in chunks)
    assert "123456" not in joined and "U1234567" not in joined and "IL620" not in joined
    assert "sk-ant-" not in joined and "Google Drive" not in joined
    assert "@everyone" not in joined
    assert "https://example.com/a" in joined


def test_inline_source_link_is_not_duplicated_in_footer():
    body = "- Headline ([source](https://example.test/article))."
    chunks = OutboundFilter.chunk(body, [Citation("news_signal", "1", "2026-09-19",
                                                 url="https://example.test/article?rss=true")])
    assert chunks == [body]


def test_still_current_suppresses_resolved(monkeypatch):
    current = [_item()]

    def feed():
        return InboxFeed(current, InboxLiveness("now", 1, 0, True, True), "v", "now")

    monkeypatch.setattr(
        "argosy.services.chat_advisor.notifications.build_inbox", lambda *a, **k: feed()
    )
    producer = NotificationProducer(
        session_factory=lambda: _FakeSession(), clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    principal = Principal("a", "g", "c", "u")
    event = producer.current_events(principal)[0]
    current[0] = _item("resolved")
    assert producer.still_current(principal, event) is False


class _FakeSession:
    bind = None

    def close(self):
        pass


def test_canonical_policy_suppression_is_not_source_failure(monkeypatch):
    feed = InboxFeed([_item()], InboxLiveness("now", 1, 0, True, True), "v", "now",
                     dropped=[{"id": "old", "reason": "superseded_by_current_order_sheet"},
                              {"id": "small", "reason": "below_materiality"}])
    monkeypatch.setattr("argosy.services.chat_advisor.notifications.build_inbox", lambda *a, **k: feed)
    producer = NotificationProducer(session_factory=_FakeSession)
    events = producer.current_events(Principal("a", "g", "c", "u"))
    assert len(events) == 1
    assert events[0].action_id == "trade:1"


def test_canonical_source_issue_without_dropped_rows_still_blocks_notification_state(monkeypatch):
    feed = InboxFeed([_item()], InboxLiveness("now", 1, 0, True, True), "v", "now",
                     issues=[{"code": "trade_plan_unavailable", "message": "Read failed"}])
    monkeypatch.setattr("argosy.services.chat_advisor.notifications.build_inbox", lambda *a, **k: feed)
    producer = NotificationProducer(session_factory=_FakeSession)
    with pytest.raises(NotificationSourceUnavailable, match="trade_plan_unavailable"):
        producer.current_events(Principal("a", "g", "c", "u"))


def test_partial_inbox_never_closes_or_emits(monkeypatch):
    feed = InboxFeed(
        [_item()],
        InboxLiveness("now", 1, 0, True, True),
        "v",
        "now",
        dropped=[{"id": "<adapter:trade>", "reason": "source_error", "kind": "trade"}],
    )
    monkeypatch.setattr(
        "argosy.services.chat_advisor.notifications.build_inbox", lambda *a, **k: feed
    )
    producer = NotificationProducer(session_factory=lambda: _FakeSession())
    with pytest.raises(NotificationSourceUnavailable):
        producer.current_events(Principal("a", "g", "c", "u"))
