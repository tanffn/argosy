"""Bounded issuer identity reads from the SEC's current public ticker directory.

No tenant data, model-supplied URL, or arbitrary query is sent to the provider.
This establishes identity only, never an investment verdict or tradeability.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import httpx

from argosy.adapters.data.sec_13f_adapter import _default_headers

DIRECTORY_URL = "https://www.sec.gov/files/company_tickers.json"
_lock = threading.Lock()
_directory: tuple[datetime, list[dict]] | None = None


def issuer_directory() -> tuple[datetime, list[dict]]:
    global _directory
    with _lock:
        now = datetime.now(UTC)
        if _directory and now - _directory[0] < timedelta(hours=6):
            return _directory
        response = httpx.get(DIRECTORY_URL, headers=_default_headers(), timeout=8)
        response.raise_for_status()
        payload = response.json()
        rows = list(payload.values())
        if not rows or not all(isinstance(row, dict) and {"ticker", "title", "cik_str"} <= row.keys()
                               for row in rows):
            raise ValueError("SEC issuer directory schema unavailable")
        _directory = (datetime.now(UTC), rows)
        return _directory


def lookup_identity(queries: list[str]) -> dict:
    checked, directory = issuer_directory()
    queries = [query.strip()[:80] for query in queries[:3] if query.strip()]
    matches = []
    for row in directory:
        matched = [q for q in queries if q.upper() == row["ticker"].upper()
                   or q.casefold() in row["title"].casefold()]
        if matched:
            matches.append({"ticker": row["ticker"], "issuer": row["title"],
                            "cik": str(row["cik_str"]).zfill(10), "matched_queries": matched})
    return {"queries": queries, "matches": matches[:10], "total_matches": len(matches),
            "verified_at": checked.isoformat(), "source_url": DIRECTORY_URL,
            "coverage": "SEC public issuer directory; absence is not proof a non-US/private instrument does not exist."}
