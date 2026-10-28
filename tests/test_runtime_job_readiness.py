from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from argosy.orchestrator.loops.base import LongRunningJob
from argosy.services.decision_readiness import (
    RECOVERABLE_JOBS,
    collect_decision_readiness,
    long_running_health,
    runtime_job_health,
)
from argosy.services.jobs import JobMetadata, JobRegistry
from argosy.state import db as db_mod
from tests.test_decision_readiness import receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('health,age_hours,expected_attention', [
    ('green', 1, False), ('red', 1, True), ('unknown', 1, True),
    ('amber', 1, True), ('green', 49, True),
])
async def test_batch_work_is_not_attention_unless_health_or_freshness_requires_it(
        engine, health, age_hours, expected_attention):
    from sqlalchemy import select
    from argosy.state.models import JobRun
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now - timedelta(hours=age_hours if name == 'discovery_funnel' else 1),
                      status='ok', error=None, summary='{}')
    await receipt('discovery_funnel', now=now, status='running', error=None)
    async with db_mod.get_session() as session:
        row = (await session.execute(select(JobRun).where(JobRun.status == 'running'))).scalar_one()
        views = [SimpleNamespace(metadata=SimpleNamespace(name=row.job_name, long_running=False),
                                 health=health, currently_running_run_id=row.id)]
        state = await collect_decision_readiness(session, user_id='ariel', now=now,
                                                 runtime_health=runtime_job_health(views))
    job = next(j for j in state['jobs'] if j['name'] == 'discovery_funnel')
    assert job['status'] == 'running'
    assert job['attention_required'] is expected_attention
    assert state['status'] == 'degraded'  # never claims unfinished analysis is ready
    if not expected_attention:
        assert 'in progress' in state['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('new_status,same_id', [('error', True), ('running', False)])
async def test_batch_view_race_cannot_hide_a_failure_or_another_run(engine, new_status, same_id):
    from sqlalchemy import select
    from argosy.state.models import JobRun
    now = datetime.now(UTC)
    await receipt('discovery_funnel', now=now - timedelta(hours=1), status='ok', error=None)
    await receipt('discovery_funnel', now=now, status=new_status)
    async with db_mod.get_session() as session:
        row = (await session.execute(select(JobRun).order_by(JobRun.id.desc()).limit(1))).scalar_one()
        live = {'discovery_funnel': {'batch_run_id': row.id if same_id else row.id - 1,
                                    'status': 'running', 'attention_required': False, 'failure': None}}
        state = await collect_decision_readiness(session, user_id='ariel', now=now, runtime_health=live)
    job = next(j for j in state['jobs'] if j['name'] == 'discovery_funnel')
    assert job['status'] == new_status
    assert job['attention_required'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('status,health,attention', [
    ('connected', 'green', False), ('stopped', 'red', True),
    ('reconnecting', 'red', True), ('connected', 'red', True),
])
async def test_running_service_uses_live_health_not_receipt(engine, status, health, attention):
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now, status='ok', error=None, summary='{}')
    await receipt('continuous_service', now=now, status='running', error=None)
    views = [SimpleNamespace(metadata=SimpleNamespace(name='continuous_service', long_running=True),
        last_run_status=status, health=health, last_run_error='delivery failed' if health == 'red' else None)]
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id='ariel', now=now,
                                                 runtime_health=long_running_health(views))
    service = next(j for j in state['jobs'] if j['name'] == 'continuous_service')
    assert service['recorded_status'] == 'running'
    assert service['status'] == status
    assert service['attention_required'] == attention
    assert bool(service['failure']) == attention
    assert (state['status'] == 'ready') == (not attention)


@pytest.mark.asyncio
async def test_missing_live_status_does_not_assume_running_is_healthy(engine):
    await receipt('continuous_service', now=datetime.now(UTC), status='running', error=None)
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id='ariel')
    assert next(j for j in state['jobs'] if j['name'] == 'continuous_service')['attention_required']


class Service(LongRunningJob):
    name = 'continuous_service'
    problem = None

    async def run(self):
        pass

    def connection_status(self):
        return 'connected'

    def status_snapshot(self):
        return {'notification_error': self.problem}


@pytest.mark.asyncio
async def test_registry_keeps_connected_delivery_failures_visible_then_clears(engine):
    registry = JobRegistry()
    service = Service()
    registry.register(job=service, metadata=JobMetadata(name=service.name, schedule_cron=None,
        schedule_human='continuous', source_kind='notification', description='test', long_running=True))
    service.problem = 'Delivery failed'
    view = await registry.get(service.name)
    assert view.health == 'red' and view.last_run_error == 'Delivery failed'
    service.problem = None
    view = await registry.get(service.name)
    assert view.health == 'green' and view.last_run_error is None
