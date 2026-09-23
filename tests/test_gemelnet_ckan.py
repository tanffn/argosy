from datetime import UTC, date, datetime
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data import gemelnet_ckan as ckan


def series():
    return [{'FUND_ID': 117, 'FUND_NAME': 'Fund', 'MANAGING_CORPORATION': 'Manager',
        'FUND_CLASSIFICATION': 'קרנות השתלמות', 'REPORT_PERIOD': 202600+m if m > 0 else 202512+m,
        'MONTHLY_YIELD': 1} for m in range(7, -5, -1)]


def test_compounding_provenance_not_benchmark_or_net_return():
    result = ckan.compound_returns(series(), fund_id='117', source_url='https://source', today=date(2026,9,13))
    assert result['return_pct'] == pytest.approx(float((Decimal('1.01')**12-1)*100))
    assert result['benchmark_return_pct'] is None and result['relative_to_benchmark_pct'] is None
    assert result['last_updated'] == '2026-07-31'
    assert len(result['monthly_observations']) == 12
    assert result['fund_type'] == 'keren_hishtalmut' and 'before management fees' in result['methodology']


@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'null', 'nan', 'overflow', 'below_zero', 'wrong_fund', 'stale', 'future'])
def test_incomplete_or_invalid_data_is_never_a_neutral_return(failure):
    rows = series()
    if failure == 'missing': rows.pop()
    if failure == 'duplicate': rows[-1] = dict(rows[-2])
    if failure == 'null': rows[3]['MONTHLY_YIELD'] = None
    if failure == 'nan': rows[3]['MONTHLY_YIELD'] = 'NaN'
    if failure == 'overflow':
        for row in rows: row['MONTHLY_YIELD'] = 1e300
    if failure == 'below_zero': rows[3]['MONTHLY_YIELD'] = -101
    if failure == 'wrong_fund': rows[3]['FUND_ID'] = 999
    today = date(2027,1,1) if failure == 'stale' else date(2026,6,1) if failure == 'future' else date(2026,9,13)
    with pytest.raises(ValueError):
        ckan.compound_returns(rows, fund_id='117', source_url='https://source', today=today)


@pytest.mark.asyncio
async def test_real_adapter_path_uses_fallback_and_retains_missing_benchmark(engine, monkeypatch):
    from argosy.adapters.data.gemelnet_adapter import GemelnetAdapter
    # Freeze only freshness clock for fixture months; same real transport branch.
    original = ckan.fresh_period
    monkeypatch.setattr(ckan, 'fresh_period', lambda v, **kw: original(v, today=date(2026,9,13)))
    calls = []
    async def respond(request):
        calls.append(str(request.url))
        if request.url.host != 'data.gov.il':
            return httpx.Response(503)
        params = parse_qs(urlparse(str(request.url)).query)
        assert json.loads(params['filters'][0]) == {'FUND_ID': 117}
        return httpx.Response(200, json={'success': True, 'result': {
            'resource_id': ckan.RESOURCE, 'records': series()}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GemelnetAdapter(http_client=client).get_fund_returns('117', ttl_seconds=0)
    assert len(calls) == 2 and result['source'] == 'cma_ckan'
    assert result['benchmark_return_pct'] is None


@pytest.mark.asyncio
async def test_truncated_fund_list_fails_closed(monkeypatch):
    monkeypatch.setattr(ckan, 'fresh_period', lambda *a, **kw: '2026-07-31')
    async def respond(request):
        return httpx.Response(200, json={'success': True, 'result': {
            'resource_id': ckan.RESOURCE, 'records': series()[:1], 'total': 2000}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MissingDataSourceError, match='incomplete'):
            await ckan.fetch(http_client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [[], {'success': True, 'result': None}, {'success': False}])
async def test_malformed_provider_response_is_explicit_unavailable(body):
    async def respond(request):
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MissingDataSourceError):
            await ckan.fetch(http_client=client)


@pytest.mark.asyncio
async def test_saved_snapshot_retains_source_months_and_is_user_scoped(engine):
    from argosy.adapters.data.gemelnet_adapter import persist_pension_snapshot
    from argosy.state import db as db_mod
    from argosy.state.models import AuditLog, User
    from argosy.state.queries import get_user_pension_snapshots
    async with db_mod.get_session() as session:
        session.add_all([User(id='ariel'), User(id='other')])
        await session.commit()
    payload = ckan.compound_returns(series(), fund_id='117', source_url='https://source', today=date(2026,9,13))
    snapshot = await persist_pension_snapshot(user_id='ariel', fund_returns=payload,
                                             snapshot_at=datetime(2026, 9, 13, tzinfo=UTC))
    async with db_mod.get_session() as session:
        session.add(AuditLog(user_id='other', event_type='pension.snapshot.source',
            entity_type='pension_snapshot', entity_id=str(snapshot), payload_json='{"private":"other user"}'))
        await session.commit()
    rows = await get_user_pension_snapshots('ariel')
    assert len(rows) == 1
    assert rows[0]['source_evidence'] == payload
    assert rows[0]['source_evidence']['last_updated'] == '2026-07-31'
    assert rows[0]['snapshot_at'].startswith('2026-09-13')
    assert await get_user_pension_snapshots('other') == []
