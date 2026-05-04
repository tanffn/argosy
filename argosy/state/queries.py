"""Cross-cutting read-only query helpers.

Pure functions that wrap the SQLAlchemy boilerplate for the few
non-trivial joins that several callers need. Keep these helpers
small + obvious: anything with non-trivial business logic belongs in
its owning module (the agent, the loop, the route), not here.

Currently houses:

  - ``get_user_pension_snapshots(user_id)`` — returns the most recent
    pension-fund snapshot per `(user_id, fund_id)` tuple. Used by
    `TaxAnalystAgent` callers to inject pension performance context
    into the tax-analyst prompt without coupling the agent module to
    the ORM layer.
  - ``record_investor_events(user_id, source, events)`` — bulk-insert
    Phase 4 investor events into ``investor_events``. Called by the
    daily-brief loop after each adapter pull so the home-brief signal
    bullet has a durable, queryable source of recent events.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from argosy.state import db as db_mod
from argosy.state.models import InvestorEvent, PensionFundSnapshot


def _row_to_dict(row: PensionFundSnapshot) -> dict[str, Any]:
    """Render one snapshot ORM row as a plain JSON-safe dict."""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "fund_id": row.fund_id,
        "fund_name": row.fund_name,
        "fund_type": row.fund_type,
        "manager": row.manager,
        "return_pct_12m": (
            float(row.return_pct_12m) if row.return_pct_12m is not None else None
        ),
        "benchmark_return_pct_12m": (
            float(row.benchmark_return_pct_12m)
            if row.benchmark_return_pct_12m is not None
            else None
        ),
        "relative_to_benchmark_pct": (
            float(row.relative_to_benchmark_pct)
            if row.relative_to_benchmark_pct is not None
            else None
        ),
        "balance_nis": (
            float(row.balance_nis) if row.balance_nis is not None else None
        ),
        "snapshot_at": (
            row.snapshot_at.isoformat() if row.snapshot_at else None
        ),
        "source_url": row.source_url,
    }


async def get_user_pension_snapshots(
    user_id: str,
    *,
    only_latest_per_fund: bool = True,
) -> list[dict[str, Any]]:
    """Return pension snapshots for ``user_id`` as plain dicts.

    Args:
        user_id: the user. Cross-user isolation is enforced at the SQL
            level — every code path filters by ``user_id`` before
            touching ``snapshot_at``.
        only_latest_per_fund: when True (default) return only the
            most-recent snapshot per ``fund_id``. When False return
            the entire history ordered by ``snapshot_at`` descending.

    Implementation note: the ``only_latest_per_fund=True`` path uses a
    ``ROW_NUMBER() OVER (PARTITION BY fund_id ORDER BY snapshot_at DESC)``
    window function so the per-fund-latest filter happens in SQL rather
    than fetch-then-Python. SQLite ≥ 3.25 supports window functions and
    Argosy targets 3.40+, so no fallback is needed.

    Returned dicts mirror the column names on ``PensionFundSnapshot``,
    plus ``snapshot_at`` rendered as ISO 8601 for safe JSON
    serialization.
    """
    async with db_mod.get_session() as session:
        if only_latest_per_fund:
            # Window-function path — ROW_NUMBER() over user-scoped rows.
            # The WHERE filters by user_id BEFORE the partition runs, so
            # rn=1 always identifies the user's own most-recent row per
            # fund; there is zero cross-user leakage even when the same
            # fund_id appears for multiple users.
            #
            # Strategy: build a subquery that selects only (id, rn) for
            # the user's rows, then join the full ORM-mapped table on id
            # WHERE rn=1. Pulling just the id from the subquery keeps the
            # outer SELECT clean of duplicate column names.
            rn = (
                func.row_number()
                .over(
                    partition_by=PensionFundSnapshot.fund_id,
                    order_by=PensionFundSnapshot.snapshot_at.desc(),
                )
                .label("rn")
            )
            ranked = (
                select(PensionFundSnapshot.id.label("id"), rn)
                .where(PensionFundSnapshot.user_id == user_id)
                .subquery()
            )
            # Belt-and-braces: also constrain the OUTER select on user_id.
            # The join via the user-scoped subquery already limits this to
            # the user's rows, but a redundant WHERE keeps cross-user
            # isolation visible at the top of the statement and would
            # survive any future refactor that loosens the subquery.
            stmt = (
                select(PensionFundSnapshot)
                .join(ranked, PensionFundSnapshot.id == ranked.c.id)
                .where(PensionFundSnapshot.user_id == user_id)
                .where(ranked.c.rn == 1)
                .order_by(PensionFundSnapshot.snapshot_at.desc())
            )
            result = await session.execute(stmt)
        else:
            result = await session.execute(
                select(PensionFundSnapshot)
                .where(PensionFundSnapshot.user_id == user_id)
                .order_by(PensionFundSnapshot.snapshot_at.desc())
            )
        rows = result.scalars().all()

    return [_row_to_dict(row) for row in rows]


def _hash_text(s: str) -> str:
    """Short stable hash for natural-key fallbacks (32 hex chars)."""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:32]


def _parse_iso(value: Any) -> datetime | None:
    """Best-effort ISO 8601 parse; tolerates ``None`` / non-strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    s = str(value).strip()
    if not s:
        return None
    # Accept Z-suffix and plain ``YYYY-MM-DD`` from SEC plain-text dates.
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s + "T00:00:00+00:00")
    except ValueError:
        return None


def _form4_event(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map a SEC Form 4 transaction row to an investor_events insert."""
    code = (row.get("transaction_code") or "").upper()
    if not code:
        return None
    kind = row.get("transaction_kind") or "form4"
    filer = row.get("filer_name") or "(unknown filer)"
    role = row.get("role") or ""
    ticker = (row.get("ticker") or "").upper() or None
    shares = row.get("shares")
    price = row.get("price_per_share")
    occurred = _parse_iso(row.get("transaction_date"))
    role_clause = f" ({role})" if role else ""
    parts: list[str] = [f"{filer}{role_clause}"]
    if shares:
        # Be explicit on direction so the bullet reads as a sentence.
        verb = {
            "purchase": "bought",
            "sale": "sold",
            "grant": "granted",
            "option_exercise": "exercised",
            "tax_withholding": "withheld",
            "gift": "gifted",
            "disposition_to_issuer": "sold to issuer",
        }.get(kind, code.lower())
        try:
            shares_int = int(round(float(shares)))
            parts.append(f"{verb} {shares_int:,}")
        except (TypeError, ValueError):
            parts.append(verb)
    else:
        parts.append(kind)
    if ticker:
        parts.append(ticker)
    if price:
        try:
            parts.append(f"@ ${float(price):.2f}")
        except (TypeError, ValueError):
            pass
    headline = " ".join(parts).strip()
    accession = row.get("accession_number") or row.get("accession") or ""
    if accession:
        unique_key = f"{ticker or ''}:{accession}"
    else:
        # Fall back to a hash of the row when the adapter didn't surface
        # an accession (rare but seen in some edge cases).
        unique_key = (
            f"{ticker or ''}:{_hash_text(headline)}"
        )
    return {
        "ticker": ticker,
        "event_kind": kind,
        "headline": headline,
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(row), default=str),
        "unique_key": unique_key[:128],
    }


def _tipranks_event(ticker: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map a TipRanks consensus payload to an investor_events insert."""
    label = (payload.get("consensus_label") or "").strip()
    avg_pt = payload.get("average_price_target")
    n_buy = payload.get("num_buy") or 0
    n_hold = payload.get("num_hold") or 0
    n_sell = payload.get("num_sell") or 0
    if not label and avg_pt is None and (n_buy + n_hold + n_sell) == 0:
        return None
    occurred = _parse_iso(payload.get("last_updated"))
    bits: list[str] = []
    bits.append(f"{ticker} analyst consensus")
    if label:
        bits.append(label)
    if avg_pt is not None:
        try:
            bits.append(f"avg PT ${float(avg_pt):.2f}")
        except (TypeError, ValueError):
            pass
    if (n_buy + n_hold + n_sell) > 0:
        bits.append(f"({n_buy}B/{n_hold}H/{n_sell}S)")
    headline = " — ".join(bits[:2]) + ((" " + " ".join(bits[2:])) if len(bits) > 2 else "")
    ticker_norm = ticker.upper()
    occ_iso = (
        payload.get("last_updated") or
        (occurred.isoformat() if occurred else "")
    )
    unique_key = f"{ticker_norm}:{occ_iso}"
    return {
        "ticker": ticker_norm,
        "event_kind": "analyst_consensus",
        "headline": headline.strip(),
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(payload), default=str),
        "unique_key": unique_key[:128],
    }


def _sec13f_event(filing: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map a 13F filing summary to an investor_events insert."""
    fund_name = filing.get("fund_name") or filing.get("filer_name") or filing.get("cik") or ""
    period = filing.get("period_of_report") or ""
    accession = filing.get("accession_number") or filing.get("accession") or ""
    cik = str(filing.get("cik") or "")
    occurred = _parse_iso(filing.get("filed_at") or filing.get("period_of_report"))
    if not (fund_name or accession):
        return None
    bits = ["13F filing"]
    if fund_name:
        bits.append(str(fund_name))
    if period:
        bits.append(f"(period {period})")
    headline = " — ".join(bits[:2]) + ((" " + " ".join(bits[2:])) if len(bits) > 2 else "")
    if accession:
        unique_key = f"{cik}:{accession}"
    else:
        # Fall back when no accession (very rare; index might omit it).
        unique_key = f"{cik}:{period}:{_hash_text(str(fund_name))}"
    return {
        "ticker": None,
        "event_kind": "13f_filing",
        "headline": headline.strip(),
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(filing), default=str),
        "unique_key": unique_key[:128],
    }


def _capitoltrades_event(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map a CapitolTrades row to an investor_events insert."""
    politician = row.get("politician_name") or ""
    tx_type = (row.get("transaction_type") or "").lower() or "trade"
    ticker = (row.get("ticker") or "").upper() or None
    amount = row.get("amount_range") or ""
    occurred = _parse_iso(row.get("transaction_date") or row.get("disclosure_date"))
    if not politician and not ticker:
        return None
    parts = [politician.strip() or "(politician)", tx_type]
    if ticker:
        parts.append(ticker)
    if amount:
        parts.append(f"({amount})")
    headline = " ".join(p for p in parts if p)
    trade_id = str(row.get("trade_id") or row.get("id") or "").strip()
    occ_iso = (
        row.get("transaction_date") or
        row.get("disclosure_date") or
        (occurred.isoformat() if occurred else "")
    )
    if trade_id:
        unique_key = f"{ticker or ''}:{trade_id}"
    else:
        unique_key = f"{ticker or ''}:{occ_iso}:{politician}"
    return {
        "ticker": ticker,
        "event_kind": tx_type,
        "headline": headline,
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(row), default=str),
        "unique_key": unique_key[:128],
    }


def _news_event(ticker: str, item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map a Finnhub-style news headline to an investor_events insert.

    Headline format: ``<TICKER> · <TITLE> (<source name or domain>)``.
    Finnhub provides an explicit ``source`` (e.g. ``"Reuters"``); when
    absent we fall back to parsing the URL hostname so unsourced rows
    still render. Empty title → skip (Finnhub returns occasional
    ghost rows).
    """
    title = (item.get("headline") or item.get("title") or "").strip()
    if not title:
        return None
    # Prefer the explicit `source` field (Finnhub provides it). Fall
    # back to parsing the URL hostname so unsourced rows still render.
    src = (item.get("source") or "").strip()
    if not src:
        url = item.get("url") or ""
        if isinstance(url, str) and url:
            try:
                from urllib.parse import urlparse

                src = urlparse(url).hostname or ""
                # Drop any leading "www." for a tighter bullet.
                if src.startswith("www."):
                    src = src[4:]
            except (TypeError, ValueError):
                src = ""
    occurred: datetime | None
    raw_dt = item.get("datetime")
    if isinstance(raw_dt, (int, float)):
        try:
            occurred = datetime.fromtimestamp(float(raw_dt), tz=UTC)
        except (OSError, OverflowError, ValueError):
            occurred = None
    else:
        occurred = _parse_iso(raw_dt or item.get("published_at"))
    ticker_norm = (ticker or "").upper() or None
    bits = [t for t in [ticker_norm, title] if t]
    headline = " · ".join(bits)
    if src:
        headline = f"{headline} ({src})"
    url = item.get("url") or ""
    if isinstance(url, str) and url:
        unique_key = f"{ticker_norm or ''}:{url}"
    else:
        unique_key = f"{ticker_norm or ''}:{_hash_text(title)}"
    return {
        "ticker": ticker_norm,
        "event_kind": "news",
        "headline": headline[:512],
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(item), default=str),
        "unique_key": unique_key[:128],
    }


_MAPPERS: dict[str, Any] = {
    "sec_form4": _form4_event,
    "tipranks": _tipranks_event,
    "sec_13f": _sec13f_event,
    "capitoltrades": _capitoltrades_event,
}


async def record_investor_events(
    user_id: str,
    source: str,
    events: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
) -> int:
    """Persist investor events for one user from one source.

    Idempotent: each row carries a ``unique_key`` derived from natural
    keys in the source payload (accession number, URL, trade id, …).
    A unique constraint on ``(user_id, source, unique_key)`` plus
    dialect-aware ``INSERT ... ON CONFLICT DO NOTHING`` means the same
    insider trade landing in N consecutive daily-brief ticks produces
    one row, not N.

    Args:
        user_id: owner of the rows. Required so the home-brief query
            stays cross-user safe.
        source: the originating adapter — ``sec_form4``, ``sec_13f``,
            ``tipranks``, ``capitoltrades``, or ``news``.
        events: shape depends on ``source``:
            - ``sec_form4`` / ``sec_13f`` / ``capitoltrades``: list of
              payload dicts.
            - ``tipranks``: ``{ticker: consensus_dict}`` mapping.
            - ``news``: ``{ticker: [item_dict, ...]}`` mapping.

    Returns:
        Number of rows inserted (NOT ignored by ON CONFLICT). Zero on
        empty input, all-skipped events, or all-duplicate events.
    """
    if not events or not source or not user_id:
        return 0

    rows: list[dict[str, Any]] = []

    if source == "tipranks":
        # TipRanks comes in as ``{ticker: consensus_dict}``.
        if not isinstance(events, Mapping):
            return 0
        for ticker, payload in events.items():
            if not isinstance(payload, Mapping):
                continue
            event = _tipranks_event(str(ticker), payload)
            if event is not None:
                rows.append(event)
    elif source == "news":
        # News comes in as ``{ticker: [item_dict, ...]}``. Each ticker
        # maps to a list of headline items; we emit one investor_events
        # row per item so the home brief surfaces the latest single
        # headline rather than a batch.
        if not isinstance(events, Mapping):
            return 0
        for ticker_key, items in events.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                mapped = _news_event(str(ticker_key), item)
                if mapped is not None:
                    rows.append(mapped)
    else:
        # ``sec_form4`` / ``sec_13f`` / ``capitoltrades`` — flat list of
        # payload dicts. (No caller passes a Mapping for these sources.)
        mapper = _MAPPERS.get(source)
        if mapper is None:
            return 0
        for entry in events:
            if not isinstance(entry, Mapping):
                continue
            mapped = mapper(entry)
            if mapped is not None:
                rows.append(mapped)

    if not rows:
        return 0

    # Pick the dialect-appropriate ON CONFLICT statement so duplicate
    # inserts no-op cleanly. SQLAlchemy core insert dialect helpers
    # support ``on_conflict_do_nothing`` for sqlite + postgres; tests
    # run on sqlite, prod runs on whatever the deployment configures.
    inserted = 0
    async with db_mod.get_session() as session:
        # Resolve the dialect from the bound engine (session.bind is
        # the AsyncSession bind; ``.dialect`` reaches through).
        dialect_name = "sqlite"
        try:
            bind = session.get_bind()
            dialect_name = bind.dialect.name
        except Exception:  # pragma: no cover - defensive
            pass
        stmt_factory = pg_insert if dialect_name == "postgresql" else sqlite_insert

        for r in rows:
            values = {
                "user_id": user_id,
                "source": source,
                "ticker": r.get("ticker"),
                "event_kind": r.get("event_kind") or source,
                "headline": r.get("headline") or "",
                "occurred_at": r.get("occurred_at"),
                "payload_json": r.get("payload_json") or "",
                "unique_key": (r.get("unique_key") or "")[:128],
            }
            stmt = stmt_factory(InvestorEvent).values(**values)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["user_id", "source", "unique_key"]
            )
            result = await session.execute(stmt)
            # rowcount is 1 on insert, 0 on conflict-skip.
            if (result.rowcount or 0) > 0:
                inserted += 1
        await session.commit()
    return inserted


async def get_latest_investor_event(user_id: str) -> dict[str, Any] | None:
    """Return the most recent investor event for ``user_id``, or None.

    Ordering: ``occurred_at DESC`` first (preferred — actual event time),
    then ``ingested_at DESC`` as a tiebreaker for rows where the adapter
    couldn't parse an event date. ``user_id`` is enforced in the WHERE
    clause for cross-user isolation.
    """
    async with db_mod.get_session() as session:
        # Two-stage ordering so that NULL occurred_at rows fall to the
        # back of their ingestion-time bucket rather than mixing in.
        stmt = (
            select(InvestorEvent)
            .where(InvestorEvent.user_id == user_id)
            .order_by(
                InvestorEvent.occurred_at.is_(None),
                InvestorEvent.occurred_at.desc(),
                InvestorEvent.ingested_at.desc(),
            )
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "user_id": row.user_id,
        "ticker": row.ticker,
        "source": row.source,
        "event_kind": row.event_kind,
        "headline": row.headline,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "ingested_at": row.ingested_at.isoformat() if row.ingested_at else None,
        "payload_json": row.payload_json,
    }


__all__ = [
    "get_latest_investor_event",
    "get_user_pension_snapshots",
    "record_investor_events",
]
