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

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

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
    return {
        "ticker": ticker,
        "event_kind": kind,
        "headline": headline,
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(row), default=str),
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
    return {
        "ticker": ticker.upper(),
        "event_kind": "analyst_consensus",
        "headline": headline.strip(),
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(payload), default=str),
    }


def _sec13f_event(filing: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map a 13F filing summary to an investor_events insert."""
    fund_name = filing.get("fund_name") or filing.get("filer_name") or filing.get("cik") or ""
    period = filing.get("period_of_report") or ""
    accession = filing.get("accession_number") or ""
    occurred = _parse_iso(filing.get("filed_at") or filing.get("period_of_report"))
    if not (fund_name or accession):
        return None
    bits = ["13F filing"]
    if fund_name:
        bits.append(str(fund_name))
    if period:
        bits.append(f"(period {period})")
    headline = " — ".join(bits[:2]) + ((" " + " ".join(bits[2:])) if len(bits) > 2 else "")
    return {
        "ticker": None,
        "event_kind": "13f_filing",
        "headline": headline.strip(),
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(filing), default=str),
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
    return {
        "ticker": ticker,
        "event_kind": tx_type,
        "headline": headline,
        "occurred_at": occurred,
        "payload_json": json.dumps(dict(row), default=str),
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

    Args:
        user_id: owner of the rows. Required so the home-brief query
            stays cross-user safe.
        source: the originating adapter — ``sec_form4``, ``sec_13f``,
            ``tipranks``, ``capitoltrades``. Unknown sources are stored
            as raw rows with ``event_kind=source`` and a generic headline
            (no fancy formatting).
        events: an iterable of payload dicts (Form 4 / CapitolTrades /
            13F filing summary), or a ``{ticker: payload}`` mapping
            (TipRanks consensus). The helper picks the right shape per
            ``source``.

    Returns:
        Number of rows inserted. Zero on empty input or all-skipped
        events (e.g., a payload that has no parseable fields).
    """
    if not events or not source or not user_id:
        return 0

    mapper = _MAPPERS.get(source)
    rows: list[dict[str, Any]] = []

    if source == "tipranks":
        # TipRanks comes in as ``{ticker: consensus_dict}``.
        if isinstance(events, Mapping):
            iter_pairs: Iterable[tuple[str, Mapping[str, Any]]] = (
                (str(k), v) for k, v in events.items()
                if isinstance(v, Mapping)
            )
        else:
            return 0
        for ticker, payload in iter_pairs:
            event = _tipranks_event(ticker, payload)
            if event is not None:
                rows.append(event)
    else:
        # All other sources come in as a list of payload dicts.
        if isinstance(events, Mapping):
            iterable: Iterable[Any] = events.values()
        else:
            iterable = events
        for entry in iterable:
            if isinstance(entry, list):
                # Some adapters return list-of-list (e.g., 13F watchlist
                # is ``[{cik, filings:[...]}]``); flatten if present.
                for sub in entry:
                    if isinstance(sub, Mapping):
                        mapped = mapper(sub) if mapper else None
                        if mapped is not None:
                            rows.append(mapped)
            elif isinstance(entry, Mapping):
                mapped = mapper(entry) if mapper else None
                if mapped is None and mapper is None:
                    # Unknown source: store a generic row.
                    mapped = {
                        "ticker": (entry.get("ticker") or "").upper() or None,
                        "event_kind": source,
                        "headline": str(entry.get("headline") or "")[:512],
                        "occurred_at": _parse_iso(entry.get("occurred_at")),
                        "payload_json": json.dumps(dict(entry), default=str),
                    }
                if mapped is not None:
                    rows.append(mapped)

    if not rows:
        return 0

    async with db_mod.get_session() as session:
        for r in rows:
            session.add(
                InvestorEvent(
                    user_id=user_id,
                    source=source,
                    ticker=r.get("ticker"),
                    event_kind=r.get("event_kind") or source,
                    headline=r.get("headline") or "",
                    occurred_at=r.get("occurred_at"),
                    payload_json=r.get("payload_json") or "",
                )
            )
        await session.commit()
    return len(rows)


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
