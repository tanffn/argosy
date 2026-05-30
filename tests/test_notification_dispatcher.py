"""Tests for ``argosy/services/notification_dispatcher.py`` (Spec E #3).

Coverage:

  * **Severity-gate**: explicit ``enabled=0`` row for (channel,
    severity, kind) → channel is skipped.
  * **Default opt-out**: missing preference rows → all channels
    enabled (matrix is opt-out per spec §3.3).
  * **In-app dispatch shape**: publish_event is called with the right
    event name + payload schema.
  * **Web-push 410-Gone**: subscription status flips to ``'gone'``;
    subsequent dispatches skip it (active-only fan-out).
  * **Dispatch ledger UNIQUE**: re-dispatch with the same
    notification_id + channel is rejected at the DB layer + reported
    as ``already_dispatched``.
  * **Endpoint shape validation**: malformed URL → ``status='failed'``
    with ``invalid_endpoint_url`` / ``invalid_endpoint_scheme`` /
    ``invalid_endpoint_host``.
  * **VAPID creds missing**: send_web_push returns
    ``status='failed', error='vapid_not_configured'`` (no crash).
  * **CRUD happy paths**: update_preference upserts; add_subscription
    de-dupes on (user, channel, endpoint); remove_subscription
    deletes cleanly.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_notification_dispatcher.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.notification_dispatcher import (
    DispatchSummary,
    add_subscription,
    dispatch_notification,
    remove_subscription,
    update_preference,
)
from argosy.services.web_push import (
    WebPushResult,
    _validate_endpoint_shape,
    send_web_push,
)
from argosy.state.models import (
    Base,
    NotificationDispatchLedger,
    NotificationPreference,
    NotificationSubscription,
    User,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session backed by a tmp_path file DB.

    Mirrors the pattern used in
    ``tests/test_state_observer_flag_writer.py`` — we install the
    ORM-declared schema via ``Base.metadata.create_all`` and rely on
    DB-level UNIQUE constraints declared in the ORM
    ``__table_args__``.  The full migration 0055 CHECKs (channel enum
    etc.) are NOT installed here; tests pass valid enum values so the
    absence is benign.
    """
    db_path = tmp_path / "notif_dispatcher.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    # FK enforcement (the dispatcher relies on subscription_id FK
    # ON DELETE SET NULL — not load-bearing for these tests but
    # matches production behaviour).
    @sa.event.listens_for(engine, "connect")
    def _enable_fks(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    Base.metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_event_subscribers():
    """Drain any in-process subscribers between tests.

    ``argosy.api.events`` keeps a process-global subscribers list; the
    in-app dispatch path appends to it.  Reset between tests so we
    don't leak Queues across the test module.
    """
    from argosy.api import events

    events._reset_for_tests()
    yield
    events._reset_for_tests()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine to completion (Python <3.13 friendly)."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@dataclass
class _StubWebPushSender:
    """Deterministic stand-in for send_web_push."""

    result_by_sub_id: dict[int, WebPushResult]
    calls: list[tuple[int, dict[str, Any]]] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.calls = []

    async def __call__(
        self,
        subscription: NotificationSubscription,
        payload: dict[str, Any],
    ) -> WebPushResult:
        self.calls.append((subscription.id, payload))
        return self.result_by_sub_id.get(
            subscription.id,
            WebPushResult(
                status="sent",
                http_status=201,
                telemetry_endpoint_host="example.test",
            ),
        )


def _seed_subscription(
    session,
    *,
    user_id: str = USER,
    channel: str = "web_push",
    endpoint: str = "https://push.example.test/abc123",
    status: str = "active",
) -> NotificationSubscription:
    sub = NotificationSubscription(
        user_id=user_id,
        channel=channel,
        endpoint=endpoint,
        p256dh="p256dh-bytes",
        auth="auth-bytes",
        status=status,
    )
    session.add(sub)
    session.flush()
    return sub


# ---------------------------------------------------------------------------
# Endpoint shape validation
# ---------------------------------------------------------------------------


class TestEndpointShapeValidation:
    def test_https_endpoint_passes(self):
        ok, err, host = _validate_endpoint_shape(
            "https://fcm.googleapis.com/fcm/send/abc"
        )
        assert ok is True
        assert err is None
        assert host == "fcm.googleapis.com"

    def test_http_endpoint_rejected_scheme(self):
        ok, err, host = _validate_endpoint_shape(
            "http://fcm.googleapis.com/fcm/send/abc"
        )
        assert ok is False
        assert err == "invalid_endpoint_scheme"
        # Hostname is still parsed for telemetry tagging even on reject.
        assert host == "fcm.googleapis.com"

    def test_scheme_only_rejected(self):
        ok, err, _host = _validate_endpoint_shape("https://")
        assert ok is False
        assert err == "invalid_endpoint_host"

    def test_garbage_string_rejected(self):
        ok, err, host = _validate_endpoint_shape("not a url")
        # urlsplit doesn't raise on "not a url" — it returns
        # SplitResult(scheme='', ...).  Our check rejects on scheme.
        assert ok is False
        assert err in ("invalid_endpoint_scheme", "invalid_endpoint_url")
        # Hostname is None / empty for garbage.
        assert not host

    def test_unknown_vendor_host_still_accepted(self):
        # Codex BLOCKER #4: we DO NOT pin a host allowlist.  Unknown
        # vendors must still pass shape validation.
        ok, err, host = _validate_endpoint_shape(
            "https://push.someregionalvendor.io/v2/aaa"
        )
        assert ok is True
        assert err is None
        assert host == "push.someregionalvendor.io"


# ---------------------------------------------------------------------------
# send_web_push: VAPID creds missing
# ---------------------------------------------------------------------------


class TestWebPushVapidConfig:
    def test_send_web_push_returns_failed_when_creds_missing(
        self, sync_session, tmp_path
    ):
        sub = _seed_subscription(sync_session)
        missing_path = tmp_path / "nonexistent_vapid.json"
        assert not missing_path.exists()

        result = _run(
            send_web_push(sub, {"hello": "world"}, creds_path=missing_path)
        )
        assert result.status == "failed"
        assert result.error == "vapid_not_configured"
        assert result.telemetry_endpoint_host == "push.example.test"

    def test_send_web_push_returns_failed_when_bad_endpoint(
        self, sync_session, tmp_path
    ):
        # Bad endpoint should short-circuit BEFORE the VAPID load,
        # so we don't need a valid creds file here.
        sub = _seed_subscription(
            sync_session, endpoint="http://insecure.example.test/x"
        )
        result = _run(send_web_push(sub, {"hello": "world"}))
        assert result.status == "failed"
        assert result.error == "invalid_endpoint_scheme"


# ---------------------------------------------------------------------------
# Preference matrix evaluation
# ---------------------------------------------------------------------------


class TestPreferenceGating:
    def test_default_opt_out_no_rows_means_enabled(
        self, sync_session
    ):
        """Spec §3.3 — opt-out default. No preference rows → enabled."""
        stub = _StubWebPushSender(result_by_sub_id={})
        _seed_subscription(sync_session)

        summary = _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="action_proposal_repatriate_currency",
                severity="warning",
                ref_id=42,
                ref_kind="action_proposal",
                title="t",
                body="b",
                web_push_sender=stub,
            )
        )
        sync_session.commit()

        # Both in_app and web_push should have been attempted (no
        # preference row blocks them).
        assert "in_app" in summary.channels_sent
        assert any(
            s.startswith("web_push:sub=") for s in summary.channels_sent
        ), summary.channels_sent

    def test_explicit_disabled_skips_channel(self, sync_session):
        stub = _StubWebPushSender(result_by_sub_id={})
        _seed_subscription(sync_session)

        # Mute the (in_app, warning, kind) cell.
        update_preference(
            sync_session,
            USER,
            channel="in_app",
            severity="warning",
            kind="action_proposal_repatriate_currency",
            enabled=False,
        )

        summary = _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="action_proposal_repatriate_currency",
                severity="warning",
                ref_id=42,
                ref_kind="action_proposal",
                title="t",
                body="b",
                web_push_sender=stub,
            )
        )
        sync_session.commit()

        # in_app skipped, web_push still goes.
        assert any(
            "in_app:preference_disabled" == s for s in summary.channels_skipped
        ), summary.channels_skipped
        assert "in_app" not in summary.channels_sent
        assert any(
            s.startswith("web_push:sub=") for s in summary.channels_sent
        )


# ---------------------------------------------------------------------------
# In-app dispatch
# ---------------------------------------------------------------------------


class TestInAppDispatch:
    def test_in_app_calls_publish_event_with_right_shape(
        self, sync_session
    ):
        """publish_event should be called with name='notification.<kind>'
        and a payload dict carrying title/body/severity/ref."""
        captured: list[tuple[str, dict[str, Any]]] = []

        async def _fake_publish(name: str, payload: dict[str, Any]) -> None:
            captured.append((name, payload))

        with patch(
            "argosy.api.events.publish_event",
            side_effect=_fake_publish,
        ):
            summary = _run(
                dispatch_notification(
                    sync_session,
                    USER,
                    kind="observer_flag_fx_observation",
                    severity="critical",
                    ref_id=17,
                    ref_kind="monitor_flag",
                    title="USD/NIS shift",
                    body="Spot moved 22% vs plan baseline.",
                    payload={"flag_id": 17, "deviation": "extreme"},
                    channels=("in_app",),  # web_push not needed for this test
                    web_push_sender=_StubWebPushSender(result_by_sub_id={}),
                )
            )
        sync_session.commit()

        assert "in_app" in summary.channels_sent
        assert len(captured) == 1
        event_name, payload = captured[0]
        assert event_name == "notification.observer_flag_fx_observation"
        assert payload["kind"] == "observer_flag_fx_observation"
        assert payload["severity"] == "critical"
        assert payload["title"] == "USD/NIS shift"
        assert payload["body"] == "Spot moved 22% vs plan baseline."
        assert payload["ref_id"] == 17
        assert payload["ref_kind"] == "monitor_flag"
        assert payload["payload"]["flag_id"] == 17

    def test_in_app_writes_ledger_row(self, sync_session):
        stub = _StubWebPushSender(result_by_sub_id={})
        _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="anomaly",
                severity="warning",
                ref_id=99,
                ref_kind="anomaly_event",
                title="t",
                body="b",
                channels=("in_app",),
                web_push_sender=stub,
            )
        )
        sync_session.commit()

        rows = sync_session.execute(
            sa.select(NotificationDispatchLedger).where(
                NotificationDispatchLedger.channel == "in_app",
                NotificationDispatchLedger.user_id == USER,
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "sent"
        assert rows[0].subscription_id is None


# ---------------------------------------------------------------------------
# Web-push 410-Gone subscription cleanup
# ---------------------------------------------------------------------------


class TestWebPushGoneCleanup:
    def test_410_response_flips_subscription_status_to_gone(
        self, sync_session
    ):
        sub = _seed_subscription(sync_session)
        stub = _StubWebPushSender(
            result_by_sub_id={
                sub.id: WebPushResult(
                    status="gone",
                    http_status=410,
                    error="http_410",
                    telemetry_endpoint_host="push.example.test",
                ),
            }
        )

        summary = _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="x",
                severity="warning",
                ref_id=1,
                ref_kind="r",
                title="t",
                body="b",
                channels=("web_push",),
                web_push_sender=stub,
            )
        )
        sync_session.commit()

        # Subscription flipped.
        sync_session.refresh(sub)
        assert sub.status == "gone"

        # Ledger row written with status='failed' + http_410 message.
        rows = sync_session.execute(
            sa.select(NotificationDispatchLedger).where(
                NotificationDispatchLedger.channel == "web_push"
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].error_message == "http_410"
        # Error captured in summary.
        assert any(
            "sub_gone" in tag for _ch, tag in summary.errors
        ), summary.errors

    def test_subsequent_dispatch_skips_gone_subscription(
        self, sync_session
    ):
        sub = _seed_subscription(sync_session)
        # First dispatch returns 'gone'.
        stub1 = _StubWebPushSender(
            result_by_sub_id={
                sub.id: WebPushResult(
                    status="gone",
                    http_status=410,
                    error="http_410",
                ),
            }
        )
        _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="x",
                severity="warning",
                ref_id=1,
                ref_kind="r",
                title="t",
                body="b",
                channels=("web_push",),
                web_push_sender=stub1,
            )
        )
        sync_session.commit()

        # Second dispatch — a fresh notification_id (different ref_id),
        # but the active-only filter on subscriptions should mean the
        # stub is never called.
        stub2 = _StubWebPushSender(result_by_sub_id={})
        summary = _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="x",
                severity="warning",
                ref_id=2,  # different ref_id → different notification_id
                ref_kind="r",
                title="t",
                body="b",
                channels=("web_push",),
                web_push_sender=stub2,
            )
        )
        sync_session.commit()

        assert stub2.calls == []  # the dead sub was filtered out
        assert any(
            "web_push:no_subscriptions" == s for s in summary.channels_skipped
        )


# ---------------------------------------------------------------------------
# Dispatch ledger idempotency
# ---------------------------------------------------------------------------


class TestDispatchLedgerIdempotency:
    def test_re_dispatch_same_notification_skipped_idempotent(
        self, sync_session
    ):
        stub = _StubWebPushSender(result_by_sub_id={})

        kwargs = dict(
            kind="action_proposal_allocate",
            severity="warning",
            ref_id=5,
            ref_kind="action_proposal",
            title="t",
            body="b",
            channels=("in_app",),
            web_push_sender=stub,
        )

        first = _run(dispatch_notification(sync_session, USER, **kwargs))
        sync_session.commit()
        assert "in_app" in first.channels_sent

        # Re-dispatch with exactly the same identity should be a no-op.
        second = _run(dispatch_notification(sync_session, USER, **kwargs))
        sync_session.commit()
        assert "in_app" not in second.channels_sent
        assert any(
            "in_app:already_dispatched" == s
            for s in second.channels_skipped
        ), second.channels_skipped

        # And the ledger should have exactly ONE successful row.
        rows = sync_session.execute(
            sa.select(NotificationDispatchLedger).where(
                NotificationDispatchLedger.channel == "in_app",
                NotificationDispatchLedger.status == "sent",
            )
        ).scalars().all()
        assert len(rows) == 1

    def test_db_unique_constraint_catches_concurrent_dispatch(
        self, sync_session
    ):
        """DB UNIQUE(user_id, notification_id, channel) is the ground
        truth.  Simulate a race: the application-level pre-check
        passes (no ledger row yet), then a second dispatcher
        intervenes and inserts before we do.  Our INSERT must catch
        IntegrityError and report 'concurrent_dispatch' WITHOUT
        crashing.

        We simulate the race by pre-inserting the ledger row that
        ``_write_ledger`` would write, AFTER the dispatcher's pre-
        check passed.  In production this is what a concurrent
        worker would do; in test we patch the pre-check to always
        return False.
        """
        from argosy.services import notification_dispatcher as dispatcher_mod

        stub = _StubWebPushSender(result_by_sub_id={})

        # Pre-insert a ledger row that the dispatcher won't see during
        # its pre-check (we'll patch _ledger_already_dispatched to
        # return False so the dispatcher proceeds, then the actual
        # INSERT hits the UNIQUE).
        kind = "action_proposal_replan_full"
        severity = "critical"
        ref_id = 99
        ref_kind = "monitor_flag"
        # Build the same notification_id the dispatcher will compute.
        from argosy.services.notification_dispatcher import (
            _compute_notification_id,
        )

        notif_id = _compute_notification_id(
            kind=kind,
            ref_kind=ref_kind,
            ref_id=ref_id,
            severity=severity,
        )
        # Pre-seed the row that simulates "another dispatcher beat us
        # to the INSERT".
        sync_session.add(
            NotificationDispatchLedger(
                user_id=USER,
                notification_id=notif_id,
                channel="in_app",
                status="sent",
            )
        )
        sync_session.commit()

        with patch.object(
            dispatcher_mod,
            "_ledger_already_dispatched",
            return_value=False,
        ):
            summary = _run(
                dispatch_notification(
                    sync_session,
                    USER,
                    kind=kind,
                    severity=severity,
                    ref_id=ref_id,
                    ref_kind=ref_kind,
                    title="t",
                    body="b",
                    channels=("in_app",),
                    web_push_sender=stub,
                )
            )
        sync_session.commit()

        # The dispatcher should have reported the concurrent-dispatch
        # collision, NOT raised IntegrityError.
        assert "in_app" not in summary.channels_sent
        assert any(
            "in_app:concurrent_dispatch" == s
            for s in summary.channels_skipped
        ), summary.channels_skipped

        # And the ledger still has exactly ONE row (the pre-inserted
        # one — our INSERT was rolled back).
        rows = sync_session.execute(
            sa.select(NotificationDispatchLedger).where(
                NotificationDispatchLedger.notification_id == notif_id,
                NotificationDispatchLedger.channel == "in_app",
            )
        ).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# CRUD happy paths
# ---------------------------------------------------------------------------


class TestPreferenceCRUD:
    def test_update_preference_inserts_then_updates(self, sync_session):
        row1 = update_preference(
            sync_session,
            USER,
            channel="web_push",
            severity="critical",
            kind="anomaly",
            enabled=False,
        )
        sync_session.commit()
        assert row1.enabled == 0

        row2 = update_preference(
            sync_session,
            USER,
            channel="web_push",
            severity="critical",
            kind="anomaly",
            enabled=True,
        )
        sync_session.commit()
        # Same row, updated.
        assert row1.id == row2.id
        assert row2.enabled == 1

        # Only one row exists.
        rows = sync_session.execute(
            sa.select(NotificationPreference).where(
                NotificationPreference.user_id == USER
            )
        ).scalars().all()
        assert len(rows) == 1

    def test_update_preference_validates_channel(self, sync_session):
        with pytest.raises(ValueError, match="invalid channel"):
            update_preference(
                sync_session,
                USER,
                channel="sms",  # not supported
                severity="warning",
                kind="x",
                enabled=True,
            )

    def test_update_preference_validates_severity(self, sync_session):
        with pytest.raises(ValueError, match="invalid severity"):
            update_preference(
                sync_session,
                USER,
                channel="in_app",
                severity="urgent",  # not in info/warning/critical
                kind="x",
                enabled=True,
            )


class TestSubscriptionCRUD:
    def test_add_subscription_inserts_new_row(self, sync_session):
        sub = add_subscription(
            sync_session,
            USER,
            channel="web_push",
            endpoint="https://push.example.test/abc",
            p256dh="p256-bytes",
            auth="auth-bytes",
        )
        sync_session.commit()
        assert sub.id is not None
        assert sub.status == "active"
        assert sub.p256dh == "p256-bytes"

    def test_add_subscription_dedupes_on_user_channel_endpoint(
        self, sync_session
    ):
        first = add_subscription(
            sync_session,
            USER,
            channel="web_push",
            endpoint="https://push.example.test/abc",
            p256dh="p1",
            auth="a1",
        )
        sync_session.commit()

        # Re-POSTing with new crypto material updates the existing row.
        second = add_subscription(
            sync_session,
            USER,
            channel="web_push",
            endpoint="https://push.example.test/abc",
            p256dh="p2",
            auth="a2",
        )
        sync_session.commit()

        assert first.id == second.id
        assert second.p256dh == "p2"
        assert second.auth == "a2"

    def test_add_subscription_revives_gone_subscription(self, sync_session):
        first = add_subscription(
            sync_session,
            USER,
            channel="web_push",
            endpoint="https://push.example.test/abc",
        )
        first.status = "gone"
        sync_session.commit()

        second = add_subscription(
            sync_session,
            USER,
            channel="web_push",
            endpoint="https://push.example.test/abc",
        )
        sync_session.commit()
        assert second.id == first.id
        assert second.status == "active"

    def test_add_subscription_rejects_bad_webpush_endpoint(
        self, sync_session
    ):
        with pytest.raises(ValueError, match="shape validation"):
            add_subscription(
                sync_session,
                USER,
                channel="web_push",
                endpoint="http://insecure.example.test/x",  # http, not https
            )

    def test_remove_subscription_deletes_row(self, sync_session):
        sub = add_subscription(
            sync_session,
            USER,
            channel="web_push",
            endpoint="https://push.example.test/abc",
        )
        sync_session.commit()
        sid = sub.id

        remove_subscription(sync_session, sid)
        sync_session.commit()

        row = sync_session.get(NotificationSubscription, sid)
        assert row is None

    def test_remove_subscription_is_idempotent_on_missing_id(
        self, sync_session
    ):
        # Should not raise on a non-existent id.
        remove_subscription(sync_session, 999_999)
        sync_session.commit()


# ---------------------------------------------------------------------------
# DispatchSummary shape sanity
# ---------------------------------------------------------------------------


class TestDispatchSummaryShape:
    def test_summary_returns_notification_id(self, sync_session):
        stub = _StubWebPushSender(result_by_sub_id={})
        summary = _run(
            dispatch_notification(
                sync_session,
                USER,
                kind="x",
                severity="warning",
                ref_id=7,
                ref_kind="z",
                title="t",
                body="b",
                channels=("in_app",),
                web_push_sender=stub,
            )
        )
        sync_session.commit()
        # Deterministic dedup key.
        assert summary.notification_id.startswith("x|z:7|warning|")
        assert isinstance(summary, DispatchSummary)
