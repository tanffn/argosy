"""CapitolTrades adapter (Phase 4).

Source: ``https://www.capitoltrades.com/trades`` — a public site that
aggregates US Congress members' STOCK Act disclosures (Periodic
Transaction Reports). The data is the canonical House/Senate filings
re-rendered as a sortable table.

Methods:

  - ``list_recent_trades(days=30)`` — most-recent trades across all
    politicians.
  - ``list_trades_for_politician(slug)`` — one politician (slug as
    used on the site, e.g. ``nancy-pelosi``).
  - ``list_trades_for_ticker(ticker, days=365)`` — every politician's
    activity on one ticker.

Each row: ``politician_name``, ``party``, ``state``, ``ticker``,
``transaction_type``, ``transaction_date``, ``disclosure_date``,
``amount_range``, ``source_url``.

Implementation notes:

  - HTML scrape with BeautifulSoup. The site renders rows in a
    ``<table>`` with consistent ``data-*`` attributes; we use those
    as primary anchors and fall back to text extraction.
  - 24h cache. The site updates as filings come in; once a day is
    plenty for portfolio-context.
  - Polite ``User-Agent: Argosy/<version>`` on every request.
  - On parse / network failure → ``MissingDataSourceError``.

Test injection:

  - ``http_client=fake`` exposing ``async get(url, *, params=None,
    headers=None) -> Response``-shaped object with ``.text`` /
    ``.content`` / ``.status_code``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from argosy.adapters import MissingDataSourceError
from argosy.adapters.data.cache import CacheKind, cached_call
from argosy.logging import get_logger
from argosy.services.adapter_outcomes import track_adapter_call


def _approx_size_bytes(payload: Any) -> int:
    """Cheap size estimate for adapter-outcome tracking."""
    import json as _json

    try:
        return len(_json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return 0

_log = get_logger("argosy.adapters.capitoltrades")


CAPITOLTRADES_BASE = "https://www.capitoltrades.com"
TRADES_URL = f"{CAPITOLTRADES_BASE}/trades"
POLITICIAN_URL_TPL = f"{CAPITOLTRADES_BASE}/politicians/{{slug}}"

DEFAULT_TIMEOUT = 15.0
DEFAULT_TTL_SECONDS = 60 * 60 * 24       # 24h


def _user_agent() -> str:
    from argosy import __version__

    return (
        f"Argosy/{__version__} "
        "(https://github.com/anthropics/claude-code; "
        "STOCK Act trade fetcher)"
    )


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _user_agent(),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }


class CapitolTradesAdapter:
    """STOCK Act trade feed scraped from capitoltrades.com.

    Args:
        http_client: object exposing ``async get(url, *, params=None,
            headers=None)``. Defaults to ``httpx.AsyncClient``.
        timeout_seconds: per-request timeout.
    """

    PROVIDER = "capitoltrades"

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._http = http_client
        self._timeout = timeout_seconds

    # ----- public API -------------------------------------------------

    async def list_recent_trades(
        self,
        *,
        days: int = 30,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """Recent trades across all politicians, filtered to ``days`` window.

        Raises:
            ValueError: on non-positive ``days``.
            MissingDataSourceError: on outage / parse failure.
        """
        if days <= 0:
            raise ValueError(f"days must be positive; got {days}")

        with track_adapter_call("capitoltrades", target="recent") as _outcome:
            # Include today's date in the cache key so a Monday tick and a
            # Wednesday tick don't collide. ``days`` alone made the key day-
            # independent — at midnight UTC the second tick would silently
            # serve the first tick's stale window even though the underlying
            # date range had moved forward.
            enddt = datetime.now(timezone.utc).date().isoformat()  # noqa: UP017

            async def _fetch() -> list[dict[str, Any]]:
                html_text = await self._fetch_text(TRADES_URL)
                rows = _parse_trades_html(html_text, source_url=TRADES_URL)
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
                return [r for r in rows if _on_or_after(r.get("transaction_date") or "", cutoff)]

            payload = await cached_call(
                kind=CacheKind.PRICES,
                provider=self.PROVIDER,
                key=f"recent:days={days}:enddt={enddt}",
                ttl_seconds=ttl_seconds,
                fetch=_fetch,
            )
            _outcome.set_payload_size_bytes(_approx_size_bytes(payload))
            return payload

    async def list_trades_for_politician(
        self,
        slug: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """All recent trades for one politician (by slug).

        Args:
            slug: site-style slug e.g. ``nancy-pelosi``. We accept and
                normalize spaces / underscores too.
        """
        if not slug:
            raise ValueError("slug is required")
        slug_norm = slug.strip().lower().replace(" ", "-").replace("_", "-")
        url = POLITICIAN_URL_TPL.format(slug=slug_norm)

        async def _fetch() -> list[dict[str, Any]]:
            html_text = await self._fetch_text(url)
            return _parse_trades_html(html_text, source_url=url)

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"by_politician:{slug_norm}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    async def list_trades_for_ticker(
        self,
        ticker: str,
        *,
        days: int = 365,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """All politician trades on ``ticker`` within the given window."""
        if not ticker:
            raise ValueError("ticker is required")
        if days <= 0:
            raise ValueError(f"days must be positive; got {days}")
        ticker_norm = ticker.strip().upper()

        # See ``list_recent_trades`` — pin the cache key to today's date
        # so a multi-day cache TTL doesn't serve a stale window across
        # day boundaries.
        enddt = datetime.now(timezone.utc).date().isoformat()  # noqa: UP017

        async def _fetch() -> list[dict[str, Any]]:
            url = f"{TRADES_URL}?asset={ticker_norm}"
            html_text = await self._fetch_text(url, params={"asset": ticker_norm})
            rows = _parse_trades_html(html_text, source_url=url)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
            return [
                r for r in rows
                if (r.get("ticker") or "").upper() == ticker_norm
                and _on_or_after(r.get("transaction_date") or "", cutoff)
            ]

        return await cached_call(
            kind=CacheKind.PRICES,
            provider=self.PROVIDER,
            key=f"by_ticker:{ticker_norm}:days={days}:enddt={enddt}",
            ttl_seconds=ttl_seconds,
            fetch=_fetch,
        )

    # ----- internals --------------------------------------------------

    async def _fetch_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> str:
        try:
            if self._http is None:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_default_headers()
                ) as client:
                    resp = await client.get(url, params=params or {})
            else:
                resp = await self._http.get(
                    url, params=params or {}, headers=_default_headers()
                )
        except Exception as exc:
            _log.warning("capitoltrades.fetch_failed", url=url, reason=str(exc))
            raise MissingDataSourceError(
                f"capitoltrades unreachable ({exc!s}); url={url}"
            ) from exc
        if getattr(resp, "status_code", 0) != 200:
            raise MissingDataSourceError(
                f"capitoltrades returned HTTP {getattr(resp, 'status_code', '?')} "
                f"for {url}"
            )
        text = getattr(resp, "text", None)
        if text is None:
            raw = getattr(resp, "content", b"")
            text = raw.decode("utf-8", errors="replace")
        return text


# ----------------------------------------------------------------------
# HTML parsing — module-level for direct test exercise
# ----------------------------------------------------------------------


def _parse_trades_html(html_text: str, *, source_url: str) -> list[dict[str, Any]]:
    """Parse the capitoltrades trades table → list of trade dicts.

    Strategy:

      1. Locate the trades ``<table>`` (class ``q-table`` or any table
         whose header row mentions ``Politician`` and ``Traded`` /
         ``Transaction``).
      2. Iterate rows; for each cell prefer the ``data-*`` attribute
         where available, else fall back to text.
      3. Tolerate site rewrites: a missing column → empty string, never
         a parse-time crash.
    """
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html_text, "html.parser")

    table = soup.find("table", class_=re.compile(r"\bq-table\b"))
    if table is None:
        # Fall back: any <table> whose first <th> texts include
        # "Politician" + a date / amount column.
        for cand in soup.find_all("table"):
            head = cand.find("thead") or cand
            txts = " ".join(
                (th.get_text(" ", strip=True) or "").lower()
                for th in head.find_all(["th"])
            )
            if "politician" in txts and ("traded" in txts or "transaction" in txts):
                table = cand
                break

    if table is None:
        raise MissingDataSourceError(
            f"capitoltrades: trades table not found on page; source={source_url}. "
            "The site may have changed."
        )

    body = table.find("tbody") or table
    rows: list[dict[str, Any]] = []
    for tr in body.find_all("tr"):
        cells = tr.find_all(["td"])
        if not cells:
            continue
        text_cells = [c.get_text(" ", strip=True) for c in cells]
        # Heuristic mapping: capitoltrades column order has been:
        #   politician | traded_issuer | published(disclosure) | traded_date
        #   | filed_after | owner | type | size | price
        politician_name, party, state = _split_politician_cell(cells[0])
        ticker, issuer = _split_issuer_cell(cells[1] if len(cells) > 1 else None)
        publish_text = text_cells[2] if len(text_cells) > 2 else ""
        traded_text = text_cells[3] if len(text_cells) > 3 else ""
        tx_type = (text_cells[6] if len(text_cells) > 6 else "").lower()
        amount_range = text_cells[7] if len(text_cells) > 7 else ""

        rows.append(
            {
                "politician_name": politician_name,
                "party": party,
                "state": state,
                "ticker": ticker,
                "issuer": issuer,
                "transaction_type": _normalize_tx_type(tx_type),
                "transaction_date": _coerce_iso_date(traded_text),
                "disclosure_date": _coerce_iso_date(publish_text),
                "amount_range": amount_range,
                "source_url": source_url,
            }
        )
    if not rows:
        # Some pages legitimately have zero rows (filtered ticker with no
        # politicians); return [] rather than raising.
        return []
    return rows


def _split_politician_cell(cell: Any) -> tuple[str, str, str]:
    """Extract (name, party, state) from the leftmost cell.

    The site renders this as something like::

        <td>
          <a>Nancy Pelosi</a>
          <span class="q-field-party">Democrat</span>
          <span>House <span>CA</span></span>
        </td>
    """
    if cell is None:
        return ("", "", "")
    name_el = cell.find("a") or cell.find("h3")
    name = (name_el.get_text(" ", strip=True) if name_el else cell.get_text(" ", strip=True)) or ""
    party = ""
    state = ""
    for sp in cell.find_all(["span", "div"]):
        cls = " ".join(sp.get("class") or []).lower()
        txt = (sp.get_text(" ", strip=True) or "").strip()
        if not txt:
            continue
        if "party" in cls and not party:
            party = txt
        elif re.fullmatch(r"[A-Z]{2}", txt) and not state:
            state = txt
    if not party:
        # Fallback: text contains 'Democrat'/'Republican'/'Independent'.
        full = cell.get_text(" ", strip=True)
        for option in ("Democrat", "Republican", "Independent"):
            if option in full:
                party = option
                break
    return (name.strip(), party, state)


def _split_issuer_cell(cell: Any) -> tuple[str, str]:
    """Extract (ticker, issuer-name) from the issuer cell.

    Renders as ``<td>... <span class="q-field-issuer-ticker">NVDA:US</span> ...</td>``
    or similar; we strip the trailing ``:US`` country tag.
    """
    if cell is None:
        return ("", "")
    ticker = ""
    issuer = ""
    for sp in cell.find_all(["span"]):
        cls = " ".join(sp.get("class") or []).lower()
        txt = (sp.get_text(" ", strip=True) or "").strip()
        if not txt:
            continue
        if "ticker" in cls and not ticker:
            ticker = txt.split(":")[0].upper()
    if not ticker:
        # Fallback: look for an UPPERCASE token of 1-5 chars.
        full = cell.get_text(" ", strip=True)
        m = re.search(r"\b([A-Z]{1,5})(?::US)?\b", full)
        if m:
            ticker = m.group(1)
    a = cell.find("a")
    if a:
        issuer = (a.get_text(" ", strip=True) or "").strip()
    if not issuer:
        issuer = (cell.get_text(" ", strip=True) or "").strip()
    return (ticker, issuer)


def _normalize_tx_type(s: str) -> str:
    s = (s or "").lower()
    if "buy" in s or "purchase" in s:
        return "buy"
    if "sell" in s or "sale" in s:
        return "sell"
    if "exchange" in s:
        return "exchange"
    return s.strip()


def _coerce_iso_date(text: str) -> str:
    """Parse common capitoltrades date formats → ``YYYY-MM-DD`` (or '')."""
    if not text:
        return ""
    s = text.strip()
    # Already ISO?
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    # Patterns like "27 Apr 2026" / "Apr 27 2026" / "2026 Apr 27"
    for fmt in (
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%Y %b %d",
        "%Y %B %d",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # Relative shorthand like "Today" / "Yesterday" / "2 days ago"
    today = datetime.now(timezone.utc).date()
    if s.lower() == "today":
        return today.isoformat()
    if s.lower() == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    m = re.match(r"(\d+)\s*(day|days|week|weeks|month|months)\s*ago", s.lower())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("day"):
            return (today - timedelta(days=n)).isoformat()
        if unit.startswith("week"):
            return (today - timedelta(weeks=n)).isoformat()
        if unit.startswith("month"):
            return (today - timedelta(days=30 * n)).isoformat()
    return ""


def _on_or_after(iso_date: str, cutoff: date) -> bool:
    if not iso_date:
        return False
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return False
    return d >= cutoff


__all__ = [
    "CAPITOLTRADES_BASE",
    "CapitolTradesAdapter",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TTL_SECONDS",
    "POLITICIAN_URL_TPL",
    "TRADES_URL",
    "_coerce_iso_date",
    "_normalize_tx_type",
    "_on_or_after",
    "_parse_trades_html",
]
