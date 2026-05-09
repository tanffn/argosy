"""fx.boi_client — fetches daily rates from Bank of Israel API."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from argosy.services.fx.boi_client import fetch_range
from argosy.services.fx.errors import FXRateUnavailable


FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "fx" / "boi_2026-04.json"


def _fixture_response(status: int = 200) -> MagicMock:
    """Build a stub httpx.Response from the captured fixture."""
    body = FIXTURE.read_text(encoding="utf-8")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body
    resp.json.return_value = json.loads(body)
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp,
        )
    return resp


def test_fetch_range_returns_daily_rates_from_fixture():
    with patch("argosy.services.fx.boi_client.httpx.Client") as mock_client:
        instance = mock_client.return_value.__enter__.return_value
        instance.get.return_value = _fixture_response()
        rows = fetch_range(date(2026, 4, 1), date(2026, 4, 8), ["USD", "EUR"])
    # Each row is (date, currency, Decimal). At least one USD row in window.
    assert any(ccy == "USD" for _, ccy, _ in rows)
    for d, ccy, r in rows:
        assert isinstance(d, date)
        assert ccy in {"USD", "EUR"}
        assert isinstance(r, Decimal)
        assert r > 0


def test_fetch_range_raises_on_http_error():
    with patch("argosy.services.fx.boi_client.httpx.Client") as mock_client:
        instance = mock_client.return_value.__enter__.return_value
        instance.get.side_effect = httpx.ConnectError("boom")
        with pytest.raises(FXRateUnavailable):
            fetch_range(date(2026, 4, 1), date(2026, 4, 8), ["USD"])


def test_fetch_range_returns_empty_on_zero_currencies():
    """No currencies requested -> empty list, no HTTP call."""
    rows = fetch_range(date(2026, 4, 1), date(2026, 4, 8), [])
    assert rows == []
