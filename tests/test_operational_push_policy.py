"""A red status is diagnostic evidence, not automatically a request for the user."""
from datetime import UTC, datetime, time
import json
from types import SimpleNamespace

import pytest

from argosy.services.chat_advisor.contracts import OutboxEvent
from argosy.services.chat_advisor.notification_policy import CRITICAL, OPERATIONAL, delivery_events
from argosy.services.decision_readiness import job_user_action
from argosy.services.jobs.registry import JobMetadata, JobView
from argosy.transport.discord_advisor.status_board import critical_job_events
from tests.test_decision_readiness import receipt


@pytest.mark.parametrize('error,actionable', [
    ('authentication_error: please log in', True),
    ('insufficient credit', True),
    ('smtp_not_configured', True),
    ('database is locked', False),
    ('The read operation timed out', False),
    ('429 rate limit', False),
    ('verification_incomplete: need review', False),
    ('unknown implementation failure', False),
    ('[DESKTOP_INTERRUPTED] Another window kept focus', False),
])
async def test_only_concrete_user_remedies_generate_operational_push(engine, error, actionable):
    name = 'alpha_capture_daily' if 'DESKTOP' in error else 'news_daily'
    await receipt(name, now=datetime.now(UTC), error=error)
    view = JobView(metadata=JobMetadata(name, None, 'daily', 'maintenance', 'test'),
                   health='red', last_run_status='error')
    class Registry:
        async def list(self): return [view]
        def get_job(self, name): return SimpleNamespace(enabled=True)
    events = await critical_job_events(Registry(), SimpleNamespace(household_user_id='ariel'))
    assert bool(events) is actionable
    if actionable:
        assert events[0].user_action
        assert events[0].user_action in events[0].body
        assert 'Argosy needs your action' in events[0].body
    # A retry already underway does not page on its old failure.
    view.currently_running_run_id = 123
    assert await critical_job_events(Registry(), SimpleNamespace(household_user_id='ariel')) == []


def test_public_source_access_denial_does_not_invent_a_login_task():
    row = SimpleNamespace(job_name='sec_earnings_daily', status='error', error_message='HTTP403',
        output_summary=json.dumps({'failure': {'code': 'authentication', 'retryable': False}}))
    assert job_user_action(row) is None


def test_explicit_capture_login_is_a_human_action_but_normal_work_is_not():
    row = SimpleNamespace(job_name='alpha_capture_daily', status='ok', error_message=None,
                          output_summary='{"status":"waiting_for_login"}')
    assert job_user_action(row)['code'] == 'alpha_login'
    row.output_summary = '{"status":"captured"}'
    assert job_user_action(row) is None


def test_delivery_boundary_rejects_generic_ops_even_from_another_producer():
    generic = OutboxEvent('ops', 'v1', OPERATIONAL, 'Jobs need attention')
    human = OutboxEvent('auth', 'v1', OPERATIONAL, 'Sign in again', user_action='Sign in again')
    risk = OutboxEvent('risk', 'v1', CRITICAL, 'Portfolio risk requires a decision')
    delivered = delivery_events([generic, human, risk], now=datetime(2026, 9, 26, 5, tzinfo=UTC),
                                timezone='UTC', overview_time=time(8))
    assert delivered == [human, risk]


def test_push_always_spells_out_the_required_action():
    event = OutboxEvent('auth', 'v1', OPERATIONAL, 'Model access failed', user_action='Sign in again')
    result = delivery_events([event], now=datetime(2026, 9, 26, 5, tzinfo=UTC),
                             timezone='UTC', overview_time=time(8))
    assert result[0].body == 'Model access failed\nNext action: Sign in again'


async def test_real_discord_gateway_forwards_silent_delivery(monkeypatch):
    from argosy.transport.discord_advisor.gateway import DiscordPyGateway
    captured = {}
    class Channel:
        async def send(self, text, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id=1)
    gateway = DiscordPyGateway.__new__(DiscordPyGateway)
    async def channel(_): return Channel()
    monkeypatch.setattr(gateway, '_channel', channel)
    monkeypatch.setattr(gateway, '_assert_private_delivery_channel', lambda _: None)
    await gateway.send('private-status', 'Routine board update', nonce='board-1', silent=True)
    assert captured['silent'] is True
    assert captured['allowed_mentions'].everyone is False


async def test_shared_remedy_is_printed_once_for_multiple_failed_jobs(engine):
    names = ('news_daily', 'thesis_monitor_daily')
    for name in names:
        await receipt(name, now=datetime.now(UTC), error='authentication_error: please log in')
    class Registry:
        async def list(self):
            return [JobView(metadata=JobMetadata(name, None, 'daily', 'maintenance', 'test'),
                            health='red', last_run_status='error') for name in names]
        def get_job(self, name): return SimpleNamespace(enabled=True)
    events = await critical_job_events(Registry(), SimpleNamespace(household_user_id='ariel'))
    assert len(events) == 1
    assert events[0].body.count('Sign in to Claude again') == 1
    assert all(name.replace('_', ' ') in events[0].body for name in names)
