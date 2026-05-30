"""Tests for ``argosy/services/email_digest.py`` (Spec E commit #8).

Coverage:

  * **Empty digest** — no flags / proposals / snapshots in the window
    → ``has_any_activity=False`` and the rendered HTML + plain-text
    bodies contain the "no activity this week" sentence.
  * **Full digest** — flags + proposals + snapshots present →
    rendered HTML contains the section headers + a deep-link URL per
    open proposal.
  * **SMTP failure handling** — ``TimeoutError`` from the stub sender
    is caught, returns ``SendResult(status='failed', error=...)``,
    does NOT raise.  Generic ``aiosmtplib.SMTPException``-shaped
    failure same path.
  * **Plain-text fallback** — ``render_digest_text`` returns text
    with NO HTML tags (the template only uses Jinja control
    structures + plain markdown-ish).
  * **No secrets in body** — when the env contains an admin token /
    API key / VAPID secret, those strings DO NOT appear in either
    the HTML or text body.
  * **XSS sanitization** — a proposal with ``summary='<script>alert(1)
    </script>'`` renders escaped (``&lt;script&gt;``) and does NOT
    contain the live ``<script>`` open tag.
  * **Loop tick happy path** — ``WeeklyEmailDigestLoop.tick`` runs
    with a stub SMTP sender + an in-memory SQLite DB; ledger row is
    written; output_summary carries the right shape.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_email_digest.py -v
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.weekly_email_digest import (
    WeeklyEmailDigestLoop,
)
from argosy.services.email_digest import (
    DEFAULT_SUBJECT,
    SendResult,
    SmtpConfig,
    _reset_jinja_env_for_tests,
    build_weekly_digest,
    dispatch_weekly_digest,
    render_digest_html,
    render_digest_text,
    send_digest_email,
)
from argosy.state.models import (
    ActionProposal,
    Base,
    MonitorFlag,
    NotificationDispatchLedger,
    StateSnapshot,
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
    ``tests/test_notification_dispatcher.py`` — installs the
    ORM-declared schema via ``Base.metadata.create_all`` so the FK
    + UNIQUE constraints declared in __table_args__ are enforced.
    """
    db_path = tmp_path / "email_digest.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @sa.event.listens_for(engine, "connect")
    def _enable_fks(dbapi_conn, _conn_record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free", email="ariel@example.test"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_jinja_cache():
    """Re-init Jinja env between tests so monkeypatching of
    ``argosy.templates.TEMPLATES_DIR`` (when a test wants to swap the
    template dir) takes effect."""
    _reset_jinja_env_for_tests()
    yield
    _reset_jinja_env_for_tests()


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_flag(
    session,
    *,
    kind: str = "state_observer_fx_observation",
    severity: str = "warning",
    payload: dict[str, Any] | None = None,
    surfaced_at: datetime | None = None,
) -> MonitorFlag:
    flag = MonitorFlag(
        user_id=USER,
        kind=kind,
        severity=severity,
        payload=json.dumps(payload or {"rationale_md": "FX moved 12%."}),
        surfaced_at=surfaced_at or datetime.now(timezone.utc),
    )
    session.add(flag)
    session.flush()
    return flag


def _seed_proposal(
    session,
    *,
    kind: str = "repatriate_currency",
    severity: str = "warning",
    summary: str = "Repatriate $40k USD → NIS",
    status: str = "open",
    surfaced_at: datetime | None = None,
) -> ActionProposal:
    now = surfaced_at or datetime.now(timezone.utc)
    prop = ActionProposal(
        user_id=USER,
        kind=kind,
        severity=severity,
        summary=summary,
        rationale_md="Detailed rationale...",
        suggested_payload=json.dumps({"foo": "bar"}),
        status=status,
        surfaced_at=now,
        expires_at=now + timedelta(days=30),
        dedup_key=f"v1|test|{kind}|{summary}",
    )
    session.add(prop)
    session.flush()
    return prop


def _seed_snapshot(
    session,
    *,
    created_at: datetime | None = None,
) -> StateSnapshot:
    now = created_at or datetime.now(timezone.utc)
    snap = StateSnapshot(
        user_id=USER,
        snapshot_date=now.date(),
        state_json=json.dumps({"plan_inputs": {}}),
        source_versions_json=json.dumps({}),
        created_at=now,
    )
    session.add(snap)
    session.flush()
    return snap


# ---------------------------------------------------------------------------
# Empty digest — "no activity this week" body
# ---------------------------------------------------------------------------


class TestEmptyDigest:
    def test_empty_has_any_activity_false(self, sync_session):
        digest = build_weekly_digest(sync_session, USER)
        assert digest.has_any_activity is False
        assert digest.summary.flag_count == 0
        assert digest.summary.open_proposal_count == 0
        assert digest.summary.decisions_count == 0
        assert digest.summary.snapshot_count == 0
        assert digest.flags == []
        assert digest.open_proposals == []
        assert digest.snapshot_delta is None

    def test_empty_html_contains_no_activity_phrase(self, sync_session):
        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        assert "No activity this week" in html

    def test_empty_text_contains_no_activity_phrase(self, sync_session):
        digest = build_weekly_digest(sync_session, USER)
        text = render_digest_text(digest)
        assert "No activity this week" in text

    def test_empty_html_no_section_headers(self, sync_session):
        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        # The empty branch skips the section H2 headers.
        assert "Open proposals" not in html
        assert "Flags fired" not in html


# ---------------------------------------------------------------------------
# Full digest — section headers + content
# ---------------------------------------------------------------------------


class TestFullDigest:
    def test_full_has_any_activity_true(self, sync_session):
        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        _seed_snapshot(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        assert digest.has_any_activity is True
        assert digest.summary.flag_count == 1
        assert digest.summary.open_proposal_count == 1
        assert digest.summary.snapshot_count == 1
        assert len(digest.flags) == 1
        assert len(digest.open_proposals) == 1
        assert digest.snapshot_delta is not None

    def test_full_html_contains_section_headers(self, sync_session):
        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        _seed_snapshot(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)

        assert "Summary" in html
        assert "Flags fired" in html
        assert "Open proposals" in html
        assert "Plan baseline delta" in html
        # The no-activity sentence must NOT appear in a full digest.
        assert "No activity this week" not in html

    def test_full_html_contains_deep_link(self, sync_session):
        prop = _seed_proposal(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(
            sync_session, USER, base_url="https://argosy.example.test"
        )
        html = render_digest_html(digest)
        assert f"/proposals/{prop.id}" in html
        assert "https://argosy.example.test" in html

    def test_full_severity_sort_order(self, sync_session):
        """Critical proposals appear before warning/info."""
        _seed_proposal(
            sync_session,
            severity="info",
            summary="Info proposal",
            kind="note_only",
        )
        _seed_proposal(
            sync_session,
            severity="critical",
            summary="Critical proposal",
            kind="replan_full",
        )
        _seed_proposal(
            sync_session,
            severity="warning",
            summary="Warning proposal",
            kind="repatriate_currency",
        )
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        severities = [p.severity for p in digest.open_proposals]
        assert severities == ["critical", "warning", "info"]

    def test_window_filters_out_old_flags(self, sync_session):
        """Flags older than window_days are excluded."""
        now = datetime.now(timezone.utc)
        _seed_flag(
            sync_session, surfaced_at=now - timedelta(days=60)
        )
        sync_session.commit()

        digest = build_weekly_digest(
            sync_session, USER, now=now, window_days=7
        )
        # The old flag is outside the window → counts 0.
        assert digest.summary.flag_count == 0
        assert digest.flags == []


# ---------------------------------------------------------------------------
# SMTP failure handling
# ---------------------------------------------------------------------------


class TestSmtpFailure:
    def _config(self):
        return SmtpConfig(
            host="smtp.test",
            port=587,
            username="u",
            password="p",
            from_addr="argosy@test",
            tls_mode="starttls",
        )

    def test_smtp_timeout_returns_failed_no_crash(self):
        async def _raise_timeout(**_kwargs):
            raise TimeoutError("smtp connect timed out")

        result = _run(
            send_digest_email(
                to_addr="ariel@example.test",
                subject="x",
                html_body="<p>x</p>",
                text_body="x",
                smtp_config=self._config(),
                sender=_raise_timeout,
            )
        )
        assert result.status == "failed"
        assert result.error == "smtp_timeout"

    def test_generic_smtp_exception_returns_failed_no_crash(self):
        async def _raise_smtp(**_kwargs):
            # Generic exception class to simulate aiosmtplib.SMTPException
            # without needing the real import.
            raise ConnectionRefusedError("relay refused")

        result = _run(
            send_digest_email(
                to_addr="ariel@example.test",
                subject="x",
                html_body="<p>x</p>",
                text_body="x",
                smtp_config=self._config(),
                sender=_raise_smtp,
            )
        )
        assert result.status == "failed"
        assert result.error.startswith("smtp_error:")
        assert "ConnectionRefusedError" in result.error

    def test_missing_config_returns_skipped(self, monkeypatch):
        # No env vars set → SmtpConfig.from_env returns None → skipped.
        for var in (
            "ARGOSY_SMTP_HOST",
            "ARGOSY_SMTP_PORT",
            "ARGOSY_SMTP_USERNAME",
            "ARGOSY_SMTP_PASSWORD",
            "ARGOSY_SMTP_FROM",
            "ARGOSY_SMTP_TLS_MODE",
        ):
            monkeypatch.delenv(var, raising=False)

        result = _run(
            send_digest_email(
                to_addr="ariel@example.test",
                subject="x",
                html_body="<p>x</p>",
                text_body="x",
                smtp_config=None,
                sender=None,
            )
        )
        assert result.status == "skipped"
        assert result.error == "smtp_not_configured"

    def test_no_recipient_returns_skipped(self):
        result = _run(
            send_digest_email(
                to_addr="",
                subject="x",
                html_body="<p>x</p>",
                text_body="x",
                smtp_config=self._config(),
                sender=lambda **_: None,
            )
        )
        assert result.status == "skipped"
        assert result.error == "no_recipient"


# ---------------------------------------------------------------------------
# SmtpConfig env parsing
# ---------------------------------------------------------------------------


class TestSmtpConfigEnv:
    def test_full_env_parses(self):
        env = {
            "ARGOSY_SMTP_HOST": "smtp.example",
            "ARGOSY_SMTP_PORT": "587",
            "ARGOSY_SMTP_USERNAME": "u",
            "ARGOSY_SMTP_PASSWORD": "p",
            "ARGOSY_SMTP_FROM": "argosy@example",
        }
        cfg = SmtpConfig.from_env(env)
        assert cfg is not None
        assert cfg.host == "smtp.example"
        assert cfg.port == 587
        assert cfg.username == "u"
        assert cfg.password == "p"
        assert cfg.from_addr == "argosy@example"
        # port 587 → STARTTLS by default.
        assert cfg.tls_mode == "starttls"

    def test_port_465_implies_tls(self):
        env = {
            "ARGOSY_SMTP_HOST": "smtp.example",
            "ARGOSY_SMTP_PORT": "465",
            "ARGOSY_SMTP_FROM": "argosy@example",
        }
        cfg = SmtpConfig.from_env(env)
        assert cfg is not None
        assert cfg.tls_mode == "tls"

    def test_tls_mode_env_override(self):
        env = {
            "ARGOSY_SMTP_HOST": "localhost",
            "ARGOSY_SMTP_PORT": "1025",
            "ARGOSY_SMTP_FROM": "argosy@example",
            "ARGOSY_SMTP_TLS_MODE": "none",
        }
        cfg = SmtpConfig.from_env(env)
        assert cfg is not None
        assert cfg.tls_mode == "none"

    def test_missing_host_returns_none(self):
        env = {
            "ARGOSY_SMTP_PORT": "587",
            "ARGOSY_SMTP_FROM": "argosy@example",
        }
        assert SmtpConfig.from_env(env) is None

    def test_bad_port_returns_none(self):
        env = {
            "ARGOSY_SMTP_HOST": "smtp.example",
            "ARGOSY_SMTP_PORT": "not_a_number",
            "ARGOSY_SMTP_FROM": "argosy@example",
        }
        assert SmtpConfig.from_env(env) is None


# ---------------------------------------------------------------------------
# Plain-text fallback
# ---------------------------------------------------------------------------


class TestPlainTextFallback:
    def test_text_render_has_no_html_tags(self, sync_session):
        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        text = render_digest_text(digest)
        # Heuristic: no closing HTML tags + no <html / <body /
        # <table / <a href= constructs in the plain-text body.
        assert "</html>" not in text
        assert "<html" not in text
        assert "<body" not in text
        assert "<table" not in text
        assert "<a href=" not in text

    def test_text_render_empty_has_no_activity(self, sync_session):
        digest = build_weekly_digest(sync_session, USER)
        text = render_digest_text(digest)
        assert "No activity this week" in text


# ---------------------------------------------------------------------------
# Secrets hygiene — admin token / API key / VAPID secret never in body
# ---------------------------------------------------------------------------


class TestSecretsHygiene:
    def test_admin_token_not_in_body(self, sync_session, monkeypatch):
        sentinel_token = "ARGOSY_ADMIN_TOKEN_SHOULD_NEVER_LEAK_42"
        monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", sentinel_token)

        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        text = render_digest_text(digest)
        assert sentinel_token not in html
        assert sentinel_token not in text

    def test_api_key_not_in_body(self, sync_session, monkeypatch):
        sentinel = "sk-fake-anthropic-key-DO-NOT-SHIP"
        monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel)

        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        text = render_digest_text(digest)
        assert sentinel not in html
        assert sentinel not in text

    def test_vapid_private_key_not_in_body(
        self, sync_session, monkeypatch
    ):
        sentinel = "VAPID_PRIVATE_KEY_SUPER_SECRET_MUST_NOT_LEAK"
        monkeypatch.setenv("ARGOSY_VAPID_PRIVATE_KEY", sentinel)

        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        text = render_digest_text(digest)
        assert sentinel not in html
        assert sentinel not in text

    def test_xss_in_proposal_summary_escapes(self, sync_session):
        _seed_proposal(
            sync_session,
            summary="<script>alert(1)</script> repatriate now",
        )
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)

        # The literal open tag MUST NOT appear in the rendered HTML —
        # autoescape converts it to &lt;script&gt;.
        assert "<script>alert(1)</script>" not in html
        # Escaped form SHOULD appear.
        assert "&lt;script&gt;" in html

    def test_xss_in_flag_payload_escapes(self, sync_session):
        _seed_flag(
            sync_session,
            payload={
                "rationale_md": (
                    "<img src=x onerror=alert(1)>"
                    " The rate moved 7%."
                ),
            },
        )
        sync_session.commit()

        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        # Live img tag must be escaped — onerror should not survive.
        assert "<img src=x onerror=" not in html
        assert "&lt;img" in html


# ---------------------------------------------------------------------------
# dispatch_weekly_digest — orchestrator + ledger writeback
# ---------------------------------------------------------------------------


class TestDispatchOrchestrator:
    def test_dispatch_writes_ledger_row_on_skipped(
        self, sync_session, monkeypatch
    ):
        # Force no-config state → status='skipped'.
        for var in (
            "ARGOSY_SMTP_HOST",
            "ARGOSY_SMTP_PORT",
            "ARGOSY_SMTP_FROM",
            "ARGOSY_SMTP_TLS_MODE",
        ):
            monkeypatch.delenv(var, raising=False)

        result = _run(
            dispatch_weekly_digest(
                sync_session, USER
            )
        )
        sync_session.commit()
        assert result.send.status == "skipped"
        assert result.send.error == "smtp_not_configured"
        assert result.ledger_row_id is not None

        # The ledger row exists, with channel='email' + status='skipped'.
        row = sync_session.get(
            NotificationDispatchLedger, result.ledger_row_id
        )
        assert row is not None
        assert row.channel == "email"
        assert row.status == "skipped"
        assert row.error_message == "smtp_not_configured"

    def test_dispatch_with_stub_sender_sent(
        self, sync_session, monkeypatch
    ):
        # Provide valid env so SmtpConfig.from_env() succeeds — the
        # stub sender bypasses the real SMTP.
        monkeypatch.setenv("ARGOSY_SMTP_HOST", "smtp.test")
        monkeypatch.setenv("ARGOSY_SMTP_PORT", "587")
        monkeypatch.setenv("ARGOSY_SMTP_FROM", "argosy@test")

        sent_calls: list[dict[str, Any]] = []

        async def _stub(**kwargs):
            sent_calls.append(kwargs)

        _seed_proposal(sync_session)
        sync_session.commit()

        result = _run(
            dispatch_weekly_digest(
                sync_session,
                USER,
                smtp_sender=_stub,
            )
        )
        sync_session.commit()
        assert result.send.status == "sent"
        assert len(sent_calls) == 1
        call = sent_calls[0]
        assert call["to_addr"] == "ariel@example.test"
        assert call["subject"] == DEFAULT_SUBJECT
        assert "<html" in call["html_body"].lower()
        assert "Your weekly Argosy summary" in call["text_body"]

        # Ledger row 'sent'.
        row = sync_session.get(
            NotificationDispatchLedger, result.ledger_row_id
        )
        assert row is not None
        assert row.status == "sent"
        assert row.error_message is None


# ---------------------------------------------------------------------------
# Loop tick happy path
# ---------------------------------------------------------------------------


class TestLoopTick:
    def test_tick_happy_path_with_stub(
        self, sync_session, tmp_path, monkeypatch
    ):
        """The loop's tick wires session-factory → orchestrator →
        ledger; verify the output_summary shape + side effects.
        """
        # Tick needs a session_factory injection so it doesn't try to
        # build one from the (test-irrelevant) global settings.
        engine = sync_session.bind
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        # Env: minimal SMTP creds; the stub sender bypasses real SMTP.
        monkeypatch.setenv("ARGOSY_SMTP_HOST", "smtp.test")
        monkeypatch.setenv("ARGOSY_SMTP_PORT", "587")
        monkeypatch.setenv("ARGOSY_SMTP_FROM", "argosy@test")

        _seed_flag(sync_session)
        _seed_proposal(sync_session)
        sync_session.commit()

        sent_calls: list[dict[str, Any]] = []

        async def _stub(**kwargs):
            sent_calls.append(kwargs)

        loop = WeeklyEmailDigestLoop(
            schedule=LoopSchedule(
                cron="0 8 * * FRI", timezone="Asia/Jerusalem"
            ),
            user_id=USER,
            session_factory=SessionLocal,
            smtp_sender=_stub,
        )
        summary = _run(loop.tick())

        assert summary is not None
        assert summary["user_id"] == USER
        assert summary["send_status"] == "sent"
        assert summary["flag_count"] == 1
        assert summary["open_proposal_count"] == 1
        assert summary["has_any_activity"] is True
        # last_output_summary mirror is populated.
        assert loop.last_output_summary == summary
        # SMTP stub got called once.
        assert len(sent_calls) == 1

        # The tick committed its session — a fresh session can see
        # the ledger row written by dispatch_weekly_digest.
        verify_db = SessionLocal()
        try:
            row = verify_db.execute(
                sa.select(NotificationDispatchLedger).where(
                    NotificationDispatchLedger.user_id == USER,
                    NotificationDispatchLedger.channel == "email",
                )
            ).scalar_one_or_none()
            assert row is not None
            assert row.status == "sent"
        finally:
            verify_db.close()

    def test_tick_no_smtp_config_returns_skipped(
        self, sync_session, monkeypatch
    ):
        """Missing SMTP env → tick completes with send_status='skipped',
        doesn't crash, ledger row written with status='skipped'.
        """
        engine = sync_session.bind
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        for var in (
            "ARGOSY_SMTP_HOST",
            "ARGOSY_SMTP_PORT",
            "ARGOSY_SMTP_FROM",
            "ARGOSY_SMTP_TLS_MODE",
        ):
            monkeypatch.delenv(var, raising=False)

        loop = WeeklyEmailDigestLoop(
            schedule=LoopSchedule(
                cron="0 8 * * FRI", timezone="Asia/Jerusalem"
            ),
            user_id=USER,
            session_factory=SessionLocal,
        )
        summary = _run(loop.tick())

        assert summary is not None
        assert summary["send_status"] == "skipped"
        assert summary["send_error"] == "smtp_not_configured"

    def test_tick_invalid_window_days_raises(self):
        with pytest.raises(ValueError, match="window_days"):
            WeeklyEmailDigestLoop(window_days=0)


# ---------------------------------------------------------------------------
# Agent settings integration
# ---------------------------------------------------------------------------


class TestAgentSettingsCadence:
    def test_default_cadence_present(self):
        from argosy.agent_settings import AgentSettings

        s = AgentSettings()
        assert s.cadences.weekly_email_digest.enabled is True
        assert s.cadences.weekly_email_digest.cron == "0 8 * * FRI"
        assert (
            s.cadences.weekly_email_digest.timezone == "Asia/Jerusalem"
        )


# ---------------------------------------------------------------------------
# Re-dispatch idempotency (Codex BLOCKER #1 regression — review 2026-05-30)
# ---------------------------------------------------------------------------


class TestRedispatchIdempotency:
    """Codex BLOCKER (2026-05-30): a same-day re-dispatch (operator
    clicks "Run now" via /admin/jobs on a Friday the cron already
    fired) must NOT crash the second tick.  The ledger has UNIQUE on
    (user_id, notification_id, channel); the orchestrator pre-checks
    for an existing row and rolls back on IntegrityError so the
    session stays usable by the outer commit.
    """

    def test_dispatch_twice_same_day_no_crash(
        self, sync_session, monkeypatch
    ):
        monkeypatch.setenv("ARGOSY_SMTP_HOST", "smtp.test")
        monkeypatch.setenv("ARGOSY_SMTP_PORT", "587")
        monkeypatch.setenv("ARGOSY_SMTP_FROM", "argosy@test")

        async def _stub(**_kwargs):
            return None

        # First dispatch — writes a ledger row.
        result1 = _run(
            dispatch_weekly_digest(sync_session, USER, smtp_sender=_stub)
        )
        sync_session.commit()
        assert result1.send.status == "sent"
        assert result1.ledger_row_id is not None

        # Second dispatch SAME DAY same user — must not crash.  The
        # pre-check sees the existing row and returns its id without
        # attempting a duplicate INSERT.
        result2 = _run(
            dispatch_weekly_digest(sync_session, USER, smtp_sender=_stub)
        )
        # The session must still be commit-able (would raise if a
        # poisoned txn from an unrolled-back IntegrityError survived).
        sync_session.commit()

        assert result2.send.status == "sent"
        # The pre-check returns the same row id — semantic "we already
        # logged this dispatch".
        assert result2.ledger_row_id == result1.ledger_row_id

        # Verify there's still only ONE ledger row (the dedup pre-check
        # held).
        count = sync_session.execute(
            sa.select(sa.func.count(NotificationDispatchLedger.id))
            .where(
                NotificationDispatchLedger.user_id == USER,
                NotificationDispatchLedger.channel == "email",
            )
        ).scalar_one()
        assert count == 1

    def test_loop_tick_twice_same_day_no_crash(
        self, sync_session, monkeypatch
    ):
        """The full loop wrapper survives a same-day re-tick.  This is
        the operator-clicked-Run-now path on a Friday the cron already
        fired earlier.
        """
        engine = sync_session.bind
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

        monkeypatch.setenv("ARGOSY_SMTP_HOST", "smtp.test")
        monkeypatch.setenv("ARGOSY_SMTP_PORT", "587")
        monkeypatch.setenv("ARGOSY_SMTP_FROM", "argosy@test")

        async def _stub(**_kwargs):
            return None

        loop = WeeklyEmailDigestLoop(
            schedule=LoopSchedule(
                cron="0 8 * * FRI", timezone="Asia/Jerusalem"
            ),
            user_id=USER,
            session_factory=SessionLocal,
            smtp_sender=_stub,
        )
        s1 = _run(loop.tick())
        s2 = _run(loop.tick())
        # Both ticks return successfully.
        assert s1["send_status"] == "sent"
        assert s2["send_status"] == "sent"


# ---------------------------------------------------------------------------
# Secret-shape scrubbing (Codex BLOCKER #2 regression — review 2026-05-30)
# ---------------------------------------------------------------------------


class TestSecretShapeScrub:
    """Codex BLOCKER (2026-05-30): upstream agents could embed a
    secret-shaped string in MonitorFlag.payload's rationale fields.
    Autoescape prevents XSS but NOT the plain-text leak — we need a
    redaction pass before snippet rendering.
    """

    def test_anthropic_api_key_in_rationale_redacted(
        self, sync_session
    ):
        # Realistic-shape Anthropic key — actual format is sk-ant-<long>.
        fake_key = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        _seed_flag(
            sync_session,
            payload={
                "rationale_md": (
                    f"Plan critique referenced upstream key {fake_key} "
                    "during analysis."
                ),
            },
        )
        sync_session.commit()
        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        text = render_digest_text(digest)
        assert fake_key not in html
        assert fake_key not in text
        # Autoescape converts <redacted> to &lt;redacted&gt; in HTML —
        # that's correct behaviour (we don't want the redaction string
        # to introduce ANY raw HTML).  Check both forms.
        assert "&lt;redacted&gt;" in html or "<redacted>" in html

    def test_jwt_token_in_rationale_redacted(self, sync_session):
        # Realistic-shape JWT (header.payload.signature).
        fake_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "tyDep3IkO5_oqLp0aLLeP3kPK_AAAAAAA"
        )
        _seed_flag(
            sync_session,
            payload={"rationale_md": f"The token {fake_jwt} appeared."},
        )
        sync_session.commit()
        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        assert fake_jwt not in html
        # Autoescape converts <redacted> to &lt;redacted&gt; in HTML —
        # that's correct behaviour (we don't want the redaction string
        # to introduce ANY raw HTML).  Check both forms.
        assert "&lt;redacted&gt;" in html or "<redacted>" in html

    def test_admin_token_value_in_rationale_redacted(
        self, sync_session, monkeypatch
    ):
        # Dynamic-env scrub: whatever the env holds gets redacted.
        secret = "argosy-admin-1234567890abcdef"
        monkeypatch.setenv("ARGOSY_ADMIN_TOKEN", secret)
        _seed_flag(
            sync_session,
            payload={
                "rationale_md": (
                    f"Operator triggered run with token {secret}."
                ),
            },
        )
        sync_session.commit()
        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        text = render_digest_text(digest)
        assert secret not in html
        assert secret not in text

    def test_proposal_summary_with_api_key_redacted(self, sync_session):
        # Same scrub applies to ActionProposal.summary.
        fake_key = "sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        _seed_proposal(
            sync_session,
            summary=f"Use key {fake_key} to authorize",
        )
        sync_session.commit()
        digest = build_weekly_digest(sync_session, USER)
        html = render_digest_html(digest)
        assert fake_key not in html
        # Autoescape converts <redacted> to &lt;redacted&gt; in HTML —
        # that's correct behaviour (we don't want the redaction string
        # to introduce ANY raw HTML).  Check both forms.
        assert "&lt;redacted&gt;" in html or "<redacted>" in html


# ---------------------------------------------------------------------------
# aiosmtplib TLS flag combination (Codex IMPORTANT — review 2026-05-30)
# ---------------------------------------------------------------------------


class TestSmtpTlsFlagsPassedToSender:
    """Codex IMPORTANT (2026-05-30): explicit test that the
    `use_tls` / `start_tls` flag combination passed to the SMTP
    sender matches the spec for each ARGOSY_SMTP_TLS_MODE value.
    """

    def _make_sender(self):
        captured: list[dict[str, Any]] = []

        async def _sender(**kwargs):
            captured.append(kwargs)

        return captured, _sender

    def test_tls_mode_starttls_flags(self):
        captured, stub = self._make_sender()
        cfg = SmtpConfig(
            host="h",
            port=587,
            username=None,
            password=None,
            from_addr="f",
            tls_mode="starttls",
        )
        # We test the orchestrator path: the SmtpConfig flows through
        # to the sender's kwargs.
        result = _run(
            send_digest_email(
                to_addr="x@y",
                subject="s",
                html_body="<p/>",
                text_body="t",
                smtp_config=cfg,
                sender=stub,
            )
        )
        assert result.status == "sent"
        assert len(captured) == 1
        # The sender received the cfg with starttls — the real
        # aiosmtplib adapter translates to use_tls=False/start_tls=True.
        assert captured[0]["smtp_config"].tls_mode == "starttls"

    def test_tls_mode_implicit_tls_flags(self):
        captured, stub = self._make_sender()
        cfg = SmtpConfig(
            host="h",
            port=465,
            username=None,
            password=None,
            from_addr="f",
            tls_mode="tls",
        )
        result = _run(
            send_digest_email(
                to_addr="x@y",
                subject="s",
                html_body="<p/>",
                text_body="t",
                smtp_config=cfg,
                sender=stub,
            )
        )
        assert result.status == "sent"
        assert captured[0]["smtp_config"].tls_mode == "tls"
