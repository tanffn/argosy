"""Gemelnet (MoF Israeli pension) adapter tests.

Mocks `httpx`; verifies HTML parse + Windows-1255 decoding + fund-type
mapping + cache + graceful failure.
"""

from __future__ import annotations

from typing import Any

import pytest

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.gemelnet_adapter import (
    GEMELNET_INDEX,
    GemelnetAdapter,
    _coerce_float,
    _hebrew_type_to_canonical,
    _parse_funds_table,
    _rank_matches,
)


# ---------------------------------------------------------------------------
# Tiny Hebrew HTML fixture
# ---------------------------------------------------------------------------
# Using actual Hebrew literals — pytest source is UTF-8, but the adapter
# also handles Windows-1255 wire encoding (we test that too below).

_FIXTURE_HTML_HEAD = """\
<!DOCTYPE html>
<html><body>
<table class="gridResults">
  <tr>
    <th>מספר קופה</th>
    <th>שם קופה</th>
    <th>חברה מנהלת</th>
    <th>סוג קופה</th>
    <th>תשואה ל-12 חודשים</th>
    <th>תשואת ייחוס</th>
    <th>תאריך עדכון</th>
  </tr>
  <tr>
    <td>1234</td>
    <td>אלטשולר שחם השתלמות כללי</td>
    <td>אלטשולר שחם</td>
    <td>קרן השתלמות</td>
    <td>12.34</td>
    <td>10.00</td>
    <td>2026-04-30</td>
  </tr>
  <tr>
    <td>5678</td>
    <td>הראל גמל מסלול מנייתי</td>
    <td>הראל</td>
    <td>קופת גמל</td>
    <td>-2.50%</td>
    <td>-1.00%</td>
    <td>2026-04-30</td>
  </tr>
  <tr>
    <td>9999</td>
    <td>מגדל פנסיה ברירת מחדל</td>
    <td>מגדל</td>
    <td>קרן פנסיה</td>
    <td>5,67</td>
    <td>5,00</td>
    <td>2026-04-30</td>
  </tr>
</table>
</body></html>
"""


class _FakeResp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status


class _FakeHttp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self._content = content
        self._status = status
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs: Any) -> _FakeResp:
        self.calls.append(url)
        return _FakeResp(self._content, self._status)


class _FailingHttp:
    async def get(self, url: str, **_kwargs: Any) -> _FakeResp:
        raise OSError("DNS failure (simulated)")


class _BadStatusHttp:
    async def get(self, url: str, **_kwargs: Any) -> _FakeResp:
        return _FakeResp(b"oops", status=503)


# ---------------------------------------------------------------------------
# Pure-function tests (no DB required)
# ---------------------------------------------------------------------------


def test_hebrew_type_mapping() -> None:
    assert _hebrew_type_to_canonical("קופת גמל") == "kupat_gemel"
    assert _hebrew_type_to_canonical("קרן השתלמות") == "keren_hishtalmut"
    assert _hebrew_type_to_canonical("קרן פנסיה") == "kupat_pensia"
    assert _hebrew_type_to_canonical("") == ""
    assert _hebrew_type_to_canonical("nonsense") == ""


def test_coerce_float_handles_locale_variants() -> None:
    assert _coerce_float("12.34") == pytest.approx(12.34)
    assert _coerce_float("-2.50%") == pytest.approx(-2.50)
    assert _coerce_float("5,67") == pytest.approx(5.67)
    assert _coerce_float("") is None
    assert _coerce_float(None) is None
    assert _coerce_float("not a number") is None


def test_parse_funds_table_basic() -> None:
    funds = _parse_funds_table(_FIXTURE_HTML_HEAD)
    assert len(funds) == 3
    by_id = {f["fund_id"]: f for f in funds}
    assert by_id["1234"]["type"] == "keren_hishtalmut"
    assert by_id["5678"]["type"] == "kupat_gemel"
    assert by_id["9999"]["type"] == "kupat_pensia"
    assert "אלטשולר" in by_id["1234"]["name"]


def test_parse_funds_table_no_grid_raises() -> None:
    with pytest.raises(MissingDataSourceError):
        _parse_funds_table("<html><body><p>no table here</p></body></html>")


def test_rank_matches_substring_bonus() -> None:
    funds = [
        {"fund_id": "1", "name": "Altshuler Shaham Pension", "manager": "Altshuler"},
        {"fund_id": "2", "name": "Harel Gemel Equity", "manager": "Harel"},
        {"fund_id": "3", "name": "Migdal Default", "manager": "Migdal"},
    ]
    ranked = _rank_matches("harel", funds, limit=3)
    assert ranked[0]["fund_id"] == "2"


# ---------------------------------------------------------------------------
# Adapter tests against in-memory SQLite (cache lives in prices_cache)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_funds_decodes_windows_1255_and_caches(engine: None) -> None:
    encoded = _FIXTURE_HTML_HEAD.encode("windows-1255")
    fake = _FakeHttp(encoded)
    adapter = GemelnetAdapter(http_client=fake)

    funds = await adapter.list_funds()
    assert {f["fund_id"] for f in funds} == {"1234", "5678", "9999"}

    # Second call comes from the cache; underlying client not re-hit.
    funds_again = await adapter.list_funds()
    assert funds_again == funds
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_list_funds_filters_by_type(engine: None) -> None:
    encoded = _FIXTURE_HTML_HEAD.encode("windows-1255")
    adapter = GemelnetAdapter(http_client=_FakeHttp(encoded))
    only_gemel = await adapter.list_funds(fund_type="kupat_gemel")
    assert len(only_gemel) == 1
    assert only_gemel[0]["fund_id"] == "5678"


@pytest.mark.asyncio
async def test_list_funds_invalid_type_raises(engine: None) -> None:
    adapter = GemelnetAdapter(http_client=_FakeHttp(b""))
    with pytest.raises(ValueError):
        await adapter.list_funds(fund_type="not_a_type")


@pytest.mark.asyncio
async def test_get_fund_returns_computes_relative(engine: None) -> None:
    encoded = _FIXTURE_HTML_HEAD.encode("windows-1255")
    adapter = GemelnetAdapter(http_client=_FakeHttp(encoded))
    payload = await adapter.get_fund_returns("1234")
    assert payload["fund_id"] == "1234"
    assert payload["return_pct"] == pytest.approx(12.34)
    assert payload["benchmark_return_pct"] == pytest.approx(10.00)
    assert payload["relative_to_benchmark_pct"] == pytest.approx(2.34)
    assert payload["source_url"] == GEMELNET_INDEX
    assert payload["fund_type"] == "keren_hishtalmut"


@pytest.mark.asyncio
async def test_get_fund_returns_unknown_id_raises(engine: None) -> None:
    encoded = _FIXTURE_HTML_HEAD.encode("windows-1255")
    adapter = GemelnetAdapter(http_client=_FakeHttp(encoded))
    with pytest.raises(MissingDataSourceError):
        await adapter.get_fund_returns("does-not-exist")


@pytest.mark.asyncio
async def test_get_fund_returns_invalid_period_raises(engine: None) -> None:
    adapter = GemelnetAdapter(http_client=_FakeHttp(b""))
    with pytest.raises(ValueError):
        await adapter.get_fund_returns("1234", period="bogus")


@pytest.mark.asyncio
async def test_get_fund_returns_empty_id_raises(engine: None) -> None:
    adapter = GemelnetAdapter(http_client=_FakeHttp(b""))
    with pytest.raises(ValueError):
        await adapter.get_fund_returns("")


@pytest.mark.asyncio
async def test_search_funds(engine: None) -> None:
    encoded = _FIXTURE_HTML_HEAD.encode("windows-1255")
    adapter = GemelnetAdapter(http_client=_FakeHttp(encoded))
    hits = await adapter.search_funds("הראל", limit=5)
    assert any(h["fund_id"] == "5678" for h in hits)


@pytest.mark.asyncio
async def test_unreachable_site_raises_missing_data_source(engine: None) -> None:
    adapter = GemelnetAdapter(http_client=_FailingHttp())
    with pytest.raises(MissingDataSourceError):
        await adapter.list_funds()


@pytest.mark.asyncio
async def test_bad_status_raises_missing_data_source(engine: None) -> None:
    adapter = GemelnetAdapter(http_client=_BadStatusHttp())
    with pytest.raises(MissingDataSourceError):
        await adapter.list_funds()
