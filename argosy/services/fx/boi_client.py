"""Bank of Israel public exchange-rate fetcher.

NOTE: BoI migrated their public API surface in 2023-2024. The exact URL
shape MUST be re-verified before each substantial change to this file.

As of 2026-05-09 the live endpoint is the legacy ``PublicApi`` URL
(``https://www.boi.org.il/PublicApi/GetExchangeRates``) — the SDMX
``edge.boi.gov.il`` dataflows tried during implementation returned 404.
The PublicApi response shape is::

    {
      "exchangeRates": [
        {
          "key": "USD",
          "currentExchangeRate": 2.907,
          "currentChange": 0,
          "unit": 1,
          "lastUpdate": "2026-05-08T09:22:02.8257076Z"
        },
        ...
      ]
    }

Caveats baked into the parser:

* The JSON key is lowercase ``exchangeRates``. Older docs (and the legacy
  parser sketch in the plan) used capital ``ExchangeRates``; we accept both.
* The endpoint ignores ``startDate``/``endDate`` query params and returns
  the latest snapshot only — every row carries the same ``lastUpdate``.
  Callers that need historical rows must invoke this once per business
  day they want, or rely on ``fx.cache`` walkback.
* The ``unit`` field describes how many units of the foreign currency the
  ``currentExchangeRate`` value applies to (e.g. JPY is quoted per 100).
  We normalise to ILS-per-1-currency-unit by dividing by ``unit``.

If a future SDMX migration goes live and replaces this endpoint, update
``_BOI_URL`` and extend ``_parse_response`` with a new branch — keep the
existing branches around for compatibility while the cutover stabilises.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from argosy.services.fx.errors import FXRateUnavailable

# CONFIRM-DURING-IMPL — see module docstring. Live as of 2026-05-09.
_BOI_URL = "https://www.boi.org.il/PublicApi/GetExchangeRates"
_TIMEOUT_S = 10
_MAX_RETRIES = 3


def fetch_range(
    start: date, end: date, currencies: list[str],
) -> list[tuple[date, str, Decimal]]:
    """Fetch BoI representative rates for [start, end] across ``currencies``.

    Returns rows as ``(date, currency, rate)`` where ``rate`` is units of
    ILS per 1 unit of currency (i.e. ``unit`` already divided out for
    quotes like JPY which BoI publishes per 100).

    Empty list if ``currencies`` is empty.

    Raises ``FXRateUnavailable`` on connection failure or malformed
    response. The PublicApi endpoint currently ignores ``start``/``end``
    and returns the latest snapshot only; we still send the params for
    forwards-compatibility with whatever range-aware endpoint replaces it.
    """
    if not currencies:
        return []

    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "currencies": ",".join(currencies),
    }
    wanted = {c.upper() for c in currencies}
    last_err: Exception | None = None
    for _ in range(_MAX_RETRIES):
        try:
            with httpx.Client(timeout=_TIMEOUT_S) as client:
                resp = client.get(_BOI_URL, params=params)
            resp.raise_for_status()
            rows = _parse_response(resp.text)
            return [row for row in rows if row[1] in wanted]
        except httpx.ConnectError as e:
            last_err = e
            continue
        except (httpx.HTTPStatusError, ValueError, KeyError) as e:
            # Don't retry on 4xx/5xx or parse failures — they're not transient.
            raise FXRateUnavailable(f"BoI fetch failed: {e}") from e
    raise FXRateUnavailable(
        f"BoI fetch failed after {_MAX_RETRIES} retries: {last_err}"
    ) from last_err


def _parse_response(body: str) -> list[tuple[date, str, Decimal]]:
    """Parse the BoI response into ``(date, ccy, rate)`` rows.

    Tolerates three shapes:

    1. The current PublicApi snapshot — ``{"exchangeRates":[...]}``
       (lowercase key). Each entry carries ``key``, ``currentExchangeRate``,
       ``unit``, and ``lastUpdate``.
    2. Legacy PublicApi history — ``{"ExchangeRates":[...]}`` (capital
       ``E``), same fields per entry. Older implementations of the API
       returned this shape and some mirrors still do.
    3. SDMX flat JSON — ``{"data": {"dataSets":[...]}, ...}``. Best-effort:
       walks the first ``dataSet``'s series/observations and joins against
       the ``CURRENCY``/``TIME_PERIOD`` dimension members. Untested
       against a live response — kept as a forward-looking branch.

    Raises ``FXRateUnavailable`` if none of the branches match.
    """
    data = json.loads(body)

    # Shape 1 + 2: PublicApi (lowercase or capitalised key).
    rates_list: list[dict[str, Any]] | None = None
    if isinstance(data, dict):
        for key in ("exchangeRates", "ExchangeRates"):
            if isinstance(data.get(key), list):
                rates_list = data[key]
                break

    if rates_list is not None:
        rows: list[tuple[date, str, Decimal]] = []
        for entry in rates_list:
            try:
                ccy = entry["key"]
                rate = Decimal(str(entry["currentExchangeRate"]))
                unit = Decimal(str(entry.get("unit", 1) or 1))
                if unit != 1:
                    rate = rate / unit
                last_update = entry["lastUpdate"]
                d = datetime.fromisoformat(
                    last_update.replace("Z", "+00:00")
                ).date()
                rows.append((d, ccy, rate))
            except (KeyError, ValueError, ArithmeticError):
                continue
        return rows

    # Shape 3: SDMX flat JSON (best-effort, forward-compat).
    if (
        isinstance(data, dict)
        and isinstance(data.get("data"), dict)
        and isinstance(data["data"].get("dataSets"), list)
        and data["data"]["dataSets"]
    ):
        try:
            return _parse_sdmx(data["data"])
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise FXRateUnavailable(
                f"BoI SDMX response could not be parsed: {e}"
            ) from e

    raise FXRateUnavailable(
        "BoI response in unrecognized shape — update _parse_response. "
        f"First 200 chars: {body[:200]}"
    )


def _parse_sdmx(payload: dict[str, Any]) -> list[tuple[date, str, Decimal]]:
    """Best-effort SDMX flat-JSON parser.

    Untested against a live response — exercised only if BoI completes
    the SDMX migration. The shape expected here is roughly:

        {
          "dataSets": [{"series": {"<seriesKey>": {"observations": {"<obsKey>": [value, ...]}}}}],
          "structure": {
            "dimensions": {
              "series": [{"id": "CURRENCY", "values": [{"id": "USD"}, ...]}, ...],
              "observation": [{"id": "TIME_PERIOD", "values": [{"id": "2026-04-08"}, ...]}],
            }
          }
        }
    """
    dataset = payload["dataSets"][0]
    series = dataset["series"]
    structure = payload["structure"]
    series_dims = structure["dimensions"]["series"]
    obs_dims = structure["dimensions"]["observation"]

    # Locate the CURRENCY dimension among series dims.
    currency_dim_idx: int | None = None
    for i, dim in enumerate(series_dims):
        if dim.get("id", "").upper() in {"CURRENCY", "CCY"}:
            currency_dim_idx = i
            break
    if currency_dim_idx is None:
        raise KeyError("SDMX series dims have no CURRENCY-like dimension")
    currency_values = [v["id"] for v in series_dims[currency_dim_idx]["values"]]

    # TIME_PERIOD lives on the observation dimension.
    time_dim = None
    for dim in obs_dims:
        if dim.get("id", "").upper() in {"TIME_PERIOD", "TIME"}:
            time_dim = dim
            break
    if time_dim is None:
        raise KeyError("SDMX obs dims have no TIME_PERIOD-like dimension")
    time_values = [v["id"] for v in time_dim["values"]]

    rows: list[tuple[date, str, Decimal]] = []
    for series_key, series_payload in series.items():
        idx_parts = [int(p) for p in series_key.split(":")]
        ccy = currency_values[idx_parts[currency_dim_idx]]
        for obs_key, obs_value in series_payload.get("observations", {}).items():
            time_idx = int(obs_key.split(":")[0])
            iso = time_values[time_idx]
            d = date.fromisoformat(iso[:10])
            raw = obs_value[0] if isinstance(obs_value, list) else obs_value
            if raw is None:
                continue
            rows.append((d, ccy, Decimal(str(raw))))
    return rows
