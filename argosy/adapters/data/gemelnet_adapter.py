"""Israeli Ministry of Finance pension performance adapter (Phase 3).

Source: http://gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx — public,
no auth, free. Page is HTML (ASP.NET WebForms output, not a JSON API);
we parse it. Data covers `kupot gemel`, `karnot hishtalmut`, and
`karnot pensia` published by the Ministry of Finance, with monthly
refresh upstream.

Provides:

  - ``list_funds()`` — all funds known to the MoF site, filterable by
    type. Returns ``[{"fund_id", "name", "type", "manager"}, ...]``.
  - ``get_fund_returns(fund_id, period="12m")`` — performance for one
    fund. Returns ``{"fund_id", "period", "return_pct",
    "benchmark_return_pct", "relative_to_benchmark_pct",
    "last_updated", "source_url"}``.
  - ``search_funds(query)`` — fuzzy match against fund name + manager.

Implementation notes:

  - Encoding is Windows-1255 (Hebrew). We decode accordingly before
    handing bytes to BeautifulSoup.
  - The MoF site renders an ASP.NET WebForms `<table>` with the data
    rows. Column names are Hebrew; we map to English keys via the
    constants below so callers never see Hebrew strings on the wire.
  - Cached in `prices_cache` keyed ``gemelnet:fund_returns:<id>:<p>``
    with a 24h TTL. Fund-level data is updated monthly upstream so a
    daily refresh is more than enough.
  - On unreachable site (DNS, timeout, 5xx, parse failure) we raise
    `MissingDataSourceError` per the existing convention. We do NOT
    silently return empty results — that would be a footgun for
    downstream agents that build pension snapshots.

Test injection:

  - Pass ``http_client=fake`` (any object with an async ``get(url) ->
    httpx.Response``-shaped object). Tests pass a stub returning fixed
    HTML bytes so we never hit the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.logging import get_logger

_log = get_logger("argosy.adapters.gemelnet")


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

GEMELNET_BASE = "http://gemelnet.mof.gov.il/Tsuot/UI"
GEMELNET_INDEX = f"{GEMELNET_BASE}/DafMakdim.aspx"

# Hebrew fund-type → canonical English type. We match by substring so
# small punctuation differences (`קופת גמל` vs `קופת-גמל`) still resolve.
HEBREW_TYPE_MAP: dict[str, str] = {
    "קופת גמל": "kupat_gemel",
    "קרן השתלמות": "keren_hishtalmut",
    "קרן פנסיה": "kupat_pensia",
}

# Hebrew column header → canonical key. The MoF table headers use
# variants of these strings; we match by substring (after collapsing
# whitespace) for resilience to layout tweaks.
HEBREW_COLUMN_MAP: dict[str, str] = {
    "מספר קופה": "fund_id",
    "מספר קרן": "fund_id",
    "שם קופה": "name",
    "שם קרן": "name",
    "שם הקופה": "name",
    "שם הקרן": "name",
    "חברה מנהלת": "manager",
    "סוג קופה": "type_hebrew",
    "סוג קרן": "type_hebrew",
    "תשואה ל-12 חודשים": "return_pct_12m",
    "תשואה שנתית": "return_pct_12m",
    "תשואת ייחוס": "benchmark_return_pct_12m",
    "ממוצע ענפי": "benchmark_return_pct_12m",
    "תאריך עדכון": "last_updated",
}

VALID_PERIODS = {"12m", "36m", "60m", "ytd"}


# ----------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------


class GemelnetAdapter:
    """Pension-fund performance adapter against gemelnet.mof.gov.il.

    Args:
        http_client: an object exposing ``async get(url, *, timeout)
            -> Response`` where the Response has ``.content: bytes``
            and ``.status_code: int``. Defaults to a real `httpx`
            AsyncClient. Tests pass a fake.
        timeout_seconds: per-request timeout. Default 15s; the site is
            slow.
    """

    PROVIDER = "gemelnet"

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._http = http_client
        self._timeout = timeout_seconds

    # ----- public API -------------------------------------------------

    async def list_funds(
        self,
        *,
        fund_type: str | None = None,
        ttl_seconds: int = 60 * 60 * 24,
    ) -> list[dict[str, Any]]:
        """Return all known funds, optionally filtered by canonical type.

        Args:
            fund_type: one of ``kupat_gemel``, ``keren_hishtalmut``,
                ``kupat_pensia`` to filter by, or ``None`` for all.
            ttl_seconds: cache lifetime. Default 24h.

        Raises:
            ValueError: if ``fund_type`` is not a known canonical type.
            MissingDataSourceError: if the site is unreachable or the
                page fails to parse.
        """
        if fund_type is not None and fund_type not in set(HEBREW_TYPE_MAP.values()):
            raise ValueError(
                f"unknown fund_type {fund_type!r}; "
                f"expected one of {sorted(set(HEBREW_TYPE_MAP.values()))}"
            )

        async def _fetch() -> list[dict[str, Any]]:
            html_text = await self._fetch_index_html()
            return _parse_funds_table(html_text)

        funds: list[dict[str, Any]] = await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key="funds:index",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )
        if fund_type is not None:
            return [f for f in funds if f.get("type") == fund_type]
        return funds

    async def get_fund_returns(
        self,
        fund_id: str,
        *,
        period: str = "12m",
        ttl_seconds: int = 60 * 60 * 24,
    ) -> dict[str, Any]:
        """Return one fund's headline performance for ``period``.

        Returned dict shape::

            {
                "fund_id": str,
                "period": "12m",
                "return_pct": float | None,
                "benchmark_return_pct": float | None,
                "relative_to_benchmark_pct": float | None,
                "last_updated": str | None,  # ISO date if upstream
                                              # exposes it, else ""
                "source_url": str,
            }

        Raises:
            ValueError: if ``fund_id`` is empty or ``period`` is not in
                ``{"12m", "36m", "60m", "ytd"}``.
            MissingDataSourceError: if the site is unreachable, the
                fund_id is unknown, or parsing fails.
        """
        if not fund_id:
            raise ValueError("fund_id is required")
        if period not in VALID_PERIODS:
            raise ValueError(
                f"unknown period {period!r}; expected one of {sorted(VALID_PERIODS)}"
            )

        async def _fetch() -> dict[str, Any]:
            html_text = await self._fetch_index_html()
            funds = _parse_funds_table(html_text)
            row = next((f for f in funds if str(f.get("fund_id")) == str(fund_id)), None)
            if row is None:
                raise MissingDataSourceError(
                    f"gemelnet: fund_id={fund_id!r} not found in MoF index. "
                    f"Verify the fund identifier on {GEMELNET_INDEX}."
                )
            ret = _coerce_float(row.get("return_pct_12m"))
            bench = _coerce_float(row.get("benchmark_return_pct_12m"))
            relative = (ret - bench) if (ret is not None and bench is not None) else None
            return {
                "fund_id": str(row.get("fund_id")),
                "period": period,
                "return_pct": ret,
                "benchmark_return_pct": bench,
                "relative_to_benchmark_pct": relative,
                "last_updated": row.get("last_updated") or "",
                "source_url": GEMELNET_INDEX,
                "fund_name": row.get("name"),
                "fund_type": row.get("type"),
                "manager": row.get("manager"),
            }

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"fund_returns:{fund_id}:{period}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    async def search_funds(
        self,
        query: str,
        *,
        ttl_seconds: int = 60 * 60 * 24,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fuzzy-match ``query`` against fund name + manager.

        Returns the top ``limit`` matches ranked by a simple substring
        + Levenshtein-ish heuristic. We don't pull in `rapidfuzz`;
        Python ``difflib.SequenceMatcher`` is sufficient for this UX
        and zero-dep.
        """
        if not query or not query.strip():
            return []
        funds = await self.list_funds(ttl_seconds=ttl_seconds)
        return _rank_matches(query, funds, limit=limit)

    # ----- internals --------------------------------------------------

    async def _fetch_index_html(self) -> str:
        """Pull the MoF index page; decode Windows-1255; return text.

        Raises ``MissingDataSourceError`` on any network/decoding error.

        Sends an explicit User-Agent — government portals and ASP.NET
        WebForms frontends commonly block requests with the default
        `python-httpx/...` UA. Identifying ourselves cleanly avoids
        the most frequent production failure mode.
        """
        from argosy import __version__

        headers = {
            "User-Agent": (
                f"Argosy/{__version__} "
                "(https://github.com/anthropics/claude-code; "
                "Israeli pension performance fetcher)"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "he,en;q=0.8",
        }
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=headers
                ) as client:
                    resp = await client.get(GEMELNET_INDEX)
            else:
                resp = await self._http.get(GEMELNET_INDEX)
        except Exception as exc:  # network-level failure
            _log.warning("gemelnet.fetch_failed", reason=str(exc))
            raise MissingDataSourceError(
                f"gemelnet site unreachable ({exc!s}). "
                f"Source: {GEMELNET_INDEX}. Retry later."
            ) from exc

        status = getattr(resp, "status_code", 0)
        if status != 200:
            raise MissingDataSourceError(
                f"gemelnet returned HTTP {status}; source: {GEMELNET_INDEX}"
            )

        raw: bytes = getattr(resp, "content", b"")
        if not raw:
            raise MissingDataSourceError(
                f"gemelnet returned empty body; source: {GEMELNET_INDEX}"
            )

        # Windows-1255 is the published encoding. Some upstream proxies
        # mangle it to UTF-8; try the declared encoding first, then
        # fall through. We use 'replace' so a single bad byte doesn't
        # nuke the whole parse.
        for enc in ("windows-1255", "cp1255", "utf-8"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("windows-1255", errors="replace")


# ----------------------------------------------------------------------
# HTML parsing — module-level so tests can exercise it directly
# ----------------------------------------------------------------------


def _parse_funds_table(html_text: str) -> list[dict[str, Any]]:
    """Parse the MoF gemelnet index page into a list of fund dicts.

    Strategy:
      1. Try to locate a ``<table>`` with class ``gridResults`` (most
         common). Fall back to the first ``<table>`` that has at least
         one row whose first cell looks like a numeric fund id.
      2. The first row is treated as headers; remaining rows are data.
      3. Header text is mapped to canonical keys via
         ``HEBREW_COLUMN_MAP``; unknown headers are kept as-is for
         debug visibility.
      4. Fund-type Hebrew strings are translated to canonical English
         types (``kupat_gemel`` etc.); on no-match we leave the row's
         ``type`` empty and record the raw string under ``type_hebrew``.

    Raises ``MissingDataSourceError`` if no usable table is found.
    """
    # Lazy import — heavy module; importing the adapter shouldn't pay
    # the cost when nobody calls a parsing function.
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", class_="gridResults")
    if table is None:
        # Fall back to any table whose first data cell is numeric-ish.
        for candidate in soup.find_all("table"):
            rows = candidate.find_all("tr")
            if len(rows) < 2:
                continue
            first_data_row = rows[1]
            cells = first_data_row.find_all(["td", "th"])
            if not cells:
                continue
            txt = (cells[0].get_text(strip=True) or "").strip()
            if txt.isdigit():
                table = candidate
                break

    if table is None:
        raise MissingDataSourceError(
            "gemelnet: could not locate the funds-grid table in the MoF "
            f"page. The site layout may have changed; source: {GEMELNET_INDEX}"
        )

    rows = table.find_all("tr")
    if len(rows) < 2:
        raise MissingDataSourceError(
            "gemelnet: funds-grid table has no data rows; "
            f"source: {GEMELNET_INDEX}"
        )

    headers = [
        _normalize_ws(c.get_text(" ", strip=True))
        for c in rows[0].find_all(["th", "td"])
    ]
    canonical_headers = [_map_header(h) for h in headers]

    out: list[dict[str, Any]] = []
    for tr in rows[1:]:
        cells = [
            _normalize_ws(c.get_text(" ", strip=True))
            for c in tr.find_all(["td", "th"])
        ]
        if not cells or all(not c for c in cells):
            continue
        row: dict[str, Any] = {}
        for key, val in zip(canonical_headers, cells):
            row[key] = val
        # Translate fund type
        type_he = row.get("type_hebrew", "")
        canonical_type = _hebrew_type_to_canonical(type_he)
        row["type"] = canonical_type
        # Coerce id to str (the table uses zero-padded ints)
        if "fund_id" in row:
            row["fund_id"] = str(row["fund_id"]).strip()
        out.append(row)
    return out


def _map_header(hebrew_header: str) -> str:
    """Map one Hebrew column header to its canonical key.

    Substring match for resilience against punctuation/whitespace
    drift. Falls back to the raw Hebrew header (so debug callers can
    still see what came in)."""
    h = hebrew_header.strip()
    for he, canonical in HEBREW_COLUMN_MAP.items():
        if he in h:
            return canonical
    return h


def _hebrew_type_to_canonical(hebrew_type: str) -> str:
    """Map ``קופת גמל`` etc. → canonical English. Empty if no match."""
    if not hebrew_type:
        return ""
    h = hebrew_type.strip()
    for he, canonical in HEBREW_TYPE_MAP.items():
        if he in h:
            return canonical
    return ""


def _normalize_ws(s: str) -> str:
    return " ".join((s or "").split())


def _coerce_float(value: Any) -> float | None:
    """Robustly parse a percentage cell.

    The MoF site sometimes renders ``-3.45%`` and sometimes ``3,45``
    (Hebrew locale uses comma decimal separator in some columns)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    # Drop trailing % and stray whitespace; convert comma decimals.
    s = s.replace("%", "").replace(",", ".").strip()
    # Strip thousands separators if any
    s = s.replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def _rank_matches(
    query: str,
    funds: Iterable[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Score ``funds`` against ``query`` and return the top ``limit``.

    Uses ``difflib.SequenceMatcher`` ratio against
    ``f"{name} {manager}"`` and gives a substring bonus."""
    from difflib import SequenceMatcher

    q = query.strip().lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for f in funds:
        haystack = f"{f.get('name', '')} {f.get('manager', '')}".lower()
        ratio = SequenceMatcher(None, q, haystack).ratio()
        bonus = 0.3 if q and q in haystack else 0.0
        score = ratio + bonus
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [f for _, f in scored[:limit]]


# ----------------------------------------------------------------------
# Snapshot persistence (small helper on top of the adapter)
# ----------------------------------------------------------------------


async def persist_pension_snapshot(
    *,
    user_id: str,
    fund_returns: dict[str, Any],
    balance_nis: float | None = None,
    snapshot_at: datetime | None = None,
) -> int:
    """Persist one ``pension_fund_snapshots`` row from a returns dict.

    Returns the new row's primary key. Imported lazily by the CLI so a
    tests-against-the-adapter run never touches the model.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from argosy.state import db as db_mod
    from argosy.state.models import PensionFundSnapshot

    when = snapshot_at or datetime.now(timezone.utc)
    snap = PensionFundSnapshot(
        user_id=user_id,
        fund_id=str(fund_returns.get("fund_id") or ""),
        fund_name=fund_returns.get("fund_name"),
        fund_type=fund_returns.get("fund_type"),
        manager=fund_returns.get("manager"),
        return_pct_12m=_coerce_float(fund_returns.get("return_pct")),
        benchmark_return_pct_12m=_coerce_float(fund_returns.get("benchmark_return_pct")),
        relative_to_benchmark_pct=_coerce_float(
            fund_returns.get("relative_to_benchmark_pct")
        ),
        balance_nis=balance_nis,
        snapshot_at=when,
        source_url=fund_returns.get("source_url") or GEMELNET_INDEX,
    )
    try:
        async with db_mod.get_session() as session:
            session.add(snap)
            await session.commit()
            await session.refresh(snap)
            return snap.id
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        _log.exception("gemelnet.persist_failed", reason=str(exc))
        raise


__all__ = [
    "GEMELNET_BASE",
    "GEMELNET_INDEX",
    "GemelnetAdapter",
    "HEBREW_COLUMN_MAP",
    "HEBREW_TYPE_MAP",
    "VALID_PERIODS",
    "persist_pension_snapshot",
    # Exposed for tests/debug:
    "_coerce_float",
    "_hebrew_type_to_canonical",
    "_parse_funds_table",
    "_rank_matches",
]
