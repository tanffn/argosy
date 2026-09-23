"""Official CMA monthly public data fallback; never private account balances.

12m performance is compounded from twelve consecutive published nominal gross
monthly returns. No benchmark or after-fee performance is invented.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException
import json
import math
from urllib.parse import urlencode

import httpx

from argosy.adapters import MissingDataSourceError

RESOURCE = 'a30dcbea-a1d2-482c-ae29-8f781f5025fb'
API = 'https://data.gov.il/api/3/action/datastore_search'
TYPES = {'קרנות השתלמות': 'keren_hishtalmut', 'תגמולים ואישית לפיצויים': 'kupat_gemel',
         'קופת גמל להשקעה': 'kupat_gemel', 'קופת גמל להשקעה - חסכון לילד': 'kupat_gemel',
         'מרכזית לפיצויים': 'kupat_gemel', 'מטרה אחרת': 'kupat_gemel'}


def month_index(value):
    text = str(value)
    if len(text) != 6 or not text.isdigit():
        raise ValueError('Invalid CMA report month')
    year, month = int(text[:4]), int(text[4:])
    date(year, month, 1)
    return year * 12 + month - 1


def fresh_period(value, *, today=None):
    today = today or datetime.now(UTC).date()
    index = month_index(value)
    lag = today.year * 12 + today.month - 1 - index
    if not 1 <= lag <= 3:
        raise ValueError(f'CMA completed monthly report is future/current or stale: {value}')
    year, month = divmod(index, 12)
    return date(year, month + 1, monthrange(year, month + 1)[1]).isoformat()


def identity(row):
    fund_id = row['FUND_ID']
    if type(fund_id) is not int or fund_id <= 0 or not row.get('FUND_NAME'):
        raise ValueError('Invalid CMA fund identity')
    return {'fund_id': str(fund_id), 'name': row['FUND_NAME'],
            'manager': row.get('MANAGING_CORPORATION'),
            'type': TYPES.get(row.get('FUND_CLASSIFICATION'), ''),
            'type_hebrew': row.get('FUND_CLASSIFICATION')}


def compound_returns(records, *, fund_id, source_url, today=None):
    if len(records) < 12:
        raise ValueError('CMA fund lacks twelve monthly observations')
    rows = sorted(records, key=lambda r: month_index(r['REPORT_PERIOD']), reverse=True)
    selected = rows[:12]
    end = month_index(selected[0]['REPORT_PERIOD'])
    last_updated = fresh_period(selected[0]['REPORT_PERIOD'], today=today)
    factor = Decimal(1)
    for offset, row in enumerate(selected):
        if str(row['FUND_ID']) != str(fund_id) or month_index(row['REPORT_PERIOD']) != end - offset:
            raise ValueError('CMA monthly series contains another fund, duplicate or missing month')
        value = row['MONTHLY_YIELD']
        if value is None or isinstance(value, bool):
            raise ValueError('Missing CMA monthly return')
        rate = Decimal(str(value))
        if not rate.is_finite() or rate < -100:
            raise ValueError('Invalid CMA monthly return')
        factor *= 1 + rate / 100
    who = identity(selected[0])
    compounded = float((factor - 1) * 100)
    if not math.isfinite(compounded):
        raise ValueError('CMA compounded return is not finite')
    return {'fund_id': str(fund_id), 'fund_name': who['name'], 'fund_type': who['type'],
            'manager': who['manager'], 'fund_classification': who['type_hebrew'],
            'period': '12m', 'return_pct': compounded,
            'benchmark_return_pct': None, 'relative_to_benchmark_pct': None,
            'last_updated': last_updated, 'source_url': source_url,
            'accessed_at': datetime.now(UTC).isoformat(), 'source': 'cma_ckan',
            'methodology': 'compounded published monthly nominal gross returns; before management fees; no benchmark supplied',
            'monthly_observations': [{'period': str(r['REPORT_PERIOD']), 'return_pct': r['MONTHLY_YIELD']}
                                     for r in selected]}


async def _read(client, **params):
    url = API + '?' + urlencode({'resource_id': RESOURCE, **params})
    response = await client.get(url)
    if response.status_code != 200:
        raise ValueError(f'CMA returned HTTP {response.status_code}')
    if len(response.content) > 4_000_000:
        raise ValueError('CMA response exceeds bounded data budget')
    data = response.json()
    if (not isinstance(data, dict) or data.get('success') is not True
            or not isinstance(data.get('result'), dict)
            or data['result'].get('resource_id') != RESOURCE
            or not isinstance(data['result'].get('records'), list)
            or any(not isinstance(r, dict) for r in data['result']['records'])):
        raise ValueError('Invalid CMA datastore response')
    return data['result'], url


async def fetch(*, fund_id=None, http_client=None, timeout=15):
    """Current fund list or actual 12-month data; raises on unavailable/ambiguous data."""
    async def run(client):
        if fund_id is not None:
            if not str(fund_id).isdigit() or int(fund_id) <= 0:
                raise ValueError('CMA fund ID must be a positive integer')
            result, url = await _read(client, filters=json.dumps({'FUND_ID': int(fund_id)}),
                                      sort='REPORT_PERIOD desc', limit=12)
            return compound_returns(result['records'], fund_id=fund_id, source_url=url)
        latest, _ = await _read(client, sort='REPORT_PERIOD desc', limit=1)
        period = latest['records'][0]['REPORT_PERIOD']
        updated = fresh_period(period)
        result, url = await _read(client, filters=json.dumps({'REPORT_PERIOD': period}), limit=1000)
        rows = result['records']
        if result.get('total_was_estimated') or len(rows) != result['total'] or not rows:
            raise ValueError('CMA latest-month fund list incomplete')
        if any(r['REPORT_PERIOD'] != period for r in rows):
            raise ValueError('CMA latest-month filter not respected')
        funds = [{**identity(row), 'last_updated': updated, 'source_url': url} for row in rows]
        if len({f['fund_id'] for f in funds}) != len(funds):
            raise ValueError('Duplicate CMA fund in current list')
        return funds
    try:
        if http_client is not None:
            return await run(http_client)
        async with httpx.AsyncClient(timeout=timeout, headers={'User-Agent': 'Argosy public fund research'}) as client:
            return await run(client)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, IndexError, DecimalException) as exc:
        raise MissingDataSourceError(f'gemelnet official CMA fallback unavailable: {exc}') from exc
