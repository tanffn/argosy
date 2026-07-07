"""Tests for monitor-flag expiry hygiene.

Two halves:
* the writer default — ``_maybe_promote_cautions`` stamps
  ``expires_at`` on new ``alpha_report_caution`` flags;
* the one-time backfill — existing NULL-expiry caution rows get
  ``surfaced_at + 14d`` through the service (never deleted), while
  kinds with their own lifecycle are untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from argosy.services.alpha_report_analyst_runner import (
    DEFAULT_CAUTION_TTL_DAYS,
    _maybe_promote_cautions,
)
from argosy.services.monitor_flag_hygiene import backfill_missing_flag_expiry
from argosy.state.models import MonitorFlag, User

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def _seed_user(SF, user_id: str = "ariel") -> None:
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


def _seed_flag(
    SF,
    *,
    kind: str,
    surfaced_at: datetime,
    expires_at: datetime | None,
    dedup_key: str,
) -> int:
    with SF() as s:
        flag = MonitorFlag(
            user_id="ariel",
            kind=kind,
            severity="warning",
            payload=json.dumps({"caution": "x"}),
            surfaced_at=surfaced_at,
            expires_at=expires_at,
            dedup_key=dedup_key,
        )
        s.add(flag)
        s.commit()
        return flag.id


class TestCautionWriterDefaultTTL:
    def test_new_caution_flag_gets_default_expiry(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        with SF() as s:
            _maybe_promote_cautions(
                s,
                user_id="ariel",
                news_signal_id=999,
                analysis_id=1,
                cautions=["warning: MSFT under 400 is a red flag for QQQ"],
                surfaced_at=NOW.replace(tzinfo=None),
            )
            s.commit()
        with SF() as s:
            flag = (
                s.query(MonitorFlag)
                .filter(MonitorFlag.kind == "alpha_report_caution")
                .one()
            )
            assert flag.expires_at is not None
            delta = flag.expires_at - flag.surfaced_at
            assert delta == timedelta(days=DEFAULT_CAUTION_TTL_DAYS)


class TestExpiryBackfill:
    def test_null_expiry_caution_row_is_backfilled_not_deleted(
        self, client_with_db
    ):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        surfaced = (NOW - timedelta(days=38)).replace(tzinfo=None)
        flag_id = _seed_flag(
            SF,
            kind="alpha_report_caution",
            surfaced_at=surfaced,
            expires_at=None,
            dedup_key="v1|alpha_report_caution|1.deadbeef",
        )
        with SF() as s:
            count = backfill_missing_flag_expiry(s)
        assert count == 1
        with SF() as s:
            flag = s.get(MonitorFlag, flag_id)
            assert flag is not None  # audit row kept
            assert flag.expires_at == surfaced + timedelta(days=14)
            # 38-day-old caution: computed expiry is in the past — the
            # active-flags filter drops it from every projection.
            assert flag.expires_at < NOW.replace(tzinfo=None)

    def test_backfill_is_idempotent(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        _seed_flag(
            SF,
            kind="alpha_report_caution",
            surfaced_at=(NOW - timedelta(days=3)).replace(tzinfo=None),
            expires_at=None,
            dedup_key="v1|alpha_report_caution|2.cafebabe",
        )
        with SF() as s:
            assert backfill_missing_flag_expiry(s) == 1
        with SF() as s:
            assert backfill_missing_flag_expiry(s) == 0

    def test_kinds_with_their_own_lifecycle_untouched(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        flag_id = _seed_flag(
            SF,
            kind="thesis_monitor_weakened",
            surfaced_at=(NOW - timedelta(days=40)).replace(tzinfo=None),
            expires_at=None,
            dedup_key="v1|thesis_monitor|ariel|XXX|weakened",
        )
        with SF() as s:
            assert backfill_missing_flag_expiry(s) == 0
        with SF() as s:
            assert s.get(MonitorFlag, flag_id).expires_at is None

    def test_rows_with_expiry_already_set_untouched(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        _seed_user(SF)
        surfaced = (NOW - timedelta(days=2)).replace(tzinfo=None)
        expires = surfaced + timedelta(days=5)
        flag_id = _seed_flag(
            SF,
            kind="alpha_report_caution",
            surfaced_at=surfaced,
            expires_at=expires,
            dedup_key="v1|alpha_report_caution|3.feedface",
        )
        with SF() as s:
            assert backfill_missing_flag_expiry(s) == 0
        with SF() as s:
            assert s.get(MonitorFlag, flag_id).expires_at == expires
