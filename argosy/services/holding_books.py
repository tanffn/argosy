"""Managed vs total book — scoped exclusion for sleeve math.

Policy (output-trust): a holding can be deliberately *unmanaged* (excluded
from sleeve / allocation percentage math) while remaining PRESENT and counted
in the total book used for estate exposure, net worth, FX, tax, and
concentration shock/stress. Absence must never be confused with deliberate
exclusion.

Lifecycle of ``unmanaged_holdings``:
  * CREATE/UPDATE when a snapshot OBSERVES an unmanaged-by-policy row.
  * RETIRE when a snapshot covers that account/location but the symbol is gone
    (genuine sale) — never when the whole location is missing (incomplete TSV).
  * Backfill (migration 0097 + ``backfill_unmanaged_from_snapshots``) seeds from
    the newest historical snapshot that still carried each policy symbol.

Integrity: if the durable book cannot be loaded, or a policy symbol is omitted
from the snapshot while its account is also missing AND no active durable row
exists to restore it, estate/NW consumers must NOT publish HIGH-confidence
figures — they return unavailable/degraded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from argosy.logging import get_logger

log = get_logger(__name__)

# Fallback when the policy table is empty / unreachable (pure helpers, tests).
# Production policy is data-driven via ``unmanaged_symbol_policy``.
DEFAULT_UNMANAGED_SYMBOLS: frozenset[str] = frozenset({"NVDA"})

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"


@dataclass(frozen=True)
class UnmanagedLoadResult:
    """Result of loading durable unmanaged rows.

    ``ok=False`` means the table/query failed — callers MUST NOT treat this as
    an empty book and publish HIGH-confidence estate figures.
    """

    rows: list[Any]
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class TotalBookResult:
    """Total + managed books plus integrity for fail-loud consumers."""

    total: list[dict[str, Any]]
    managed: list[dict[str, Any]]
    load: UnmanagedLoadResult
    # True when a policy symbol is missing from the snapshot, its account is
    # also missing (incomplete ingest), AND we have no active durable row to
    # restore it — OR the durable load failed outright.
    degraded: bool
    degrade_reason: str | None = None


def _as_mapping(p: Any) -> Mapping[str, Any] | None:
    if isinstance(p, Mapping):
        return p
    if hasattr(p, "model_dump"):
        try:
            return p.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(p, "__dict__"):
        return {
            k: getattr(p, k)
            for k in (
                "symbol", "managed", "excluded_from_sleeve_math",
                "usd_value_k", "shares", "current_price", "currency",
                "location", "asset_type", "details", "avg_price",
                "current_value_local", "pct_change", "pct_yearly",
                "review_status",
            )
            if hasattr(p, k)
        }
    return None


def _symbol_of(p: Any) -> str:
    m = _as_mapping(p)
    if m is None:
        return (getattr(p, "symbol", None) or "").strip().upper()
    return str(m.get("symbol") or "").strip().upper()


def _location_of(p: Any) -> str:
    m = _as_mapping(p)
    if m is None:
        return (getattr(p, "location", None) or "").strip()
    return str(m.get("location") or "").strip()


def _norm_location(loc: str) -> str:
    """Collapse 'schwab 876' / 'Schwab' style labels for account coverage."""
    return " ".join((loc or "").strip().lower().split())


def location_account_key(loc: str) -> str:
    """Full account identity = normalized location string.

    NOT just the broker family token. ``schwab 999`` and ``schwab 876`` are
    distinct accounts — a feed that lists one must not retire holdings at the
    other (the partial-feed false-retire bug).
    """
    return _norm_location(loc)


# Quantity observation older than this is genuinely suspect (not a 25-day gap
# on a monthly ingest cadence). Price staleness is handled by LIVE REPRICING
# via snapshot_refresh.reprice_quantity — never by publishing an old mark.
QUANTITY_STALE_DAYS = 90
# Backward-compat alias — callers that still check "valuation age" should
# migrate to quantity_is_stale / live reprice. Kept so older tests import.
STALE_VALUATION_DAYS = QUANTITY_STALE_DAYS


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def quantity_is_stale(
    observed_as_of: Any,
    *,
    today: date | None = None,
    max_age_days: int = QUANTITY_STALE_DAYS,
) -> bool:
    """True when the share count itself is too old to trust."""
    d = _as_date(observed_as_of)
    if d is None:
        return True  # unknown observation date → fail loud
    ref = today or date.today()
    return (ref - d).days > max_age_days


def valuation_is_stale(
    valued_as_of: Any, *, today: date | None = None, max_age_days: int = QUANTITY_STALE_DAYS,
) -> bool:
    """Deprecated alias — quantity age is the durable-book gate now.

    Kept for callers/tests; prefer ``quantity_is_stale`` + live reprice.
    """
    return quantity_is_stale(valued_as_of, today=today, max_age_days=max_age_days)


def dedupe_positions_by_symbol_location(
    positions: Sequence[Any] | None,
) -> list[dict[str, Any]]:
    """Collapse duplicate (symbol, location) rows by KEEPING THE FIRST.

    Never sums — two identical NVDA rows must not become $4.6M. The live
    consistency check flags the raw duplicates as degraded separately.
    """
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for p in positions or []:
        m = _as_mapping(p)
        if m is None:
            continue
        d = dict(m)
        sym = _symbol_of(d)
        loc = _norm_location(_location_of(d))
        if not sym:
            key = ("", loc or f"anon:{len(order)}")
        else:
            key = (sym, loc)
        if key in acc:
            continue  # drop duplicate — do NOT sum money
        acc[key] = d
        order.append(key)
    return [acc[k] for k in order]


def parse_explicit_managed_flag(p: Any) -> bool | None:
    """Honor TSV / JSON overrides.

    Reachable from ingest:
      * ``managed`` / ``excluded_from_sleeve_math`` fields (JSON / stamped)
      * ``review_status`` containing ``managed`` or ``unmanaged``
      * ``details`` containing ``managed:true`` / ``managed:false`` /
        ``excluded_from_sleeve_math``
    """
    m = _as_mapping(p) or {}
    if "managed" in m and m["managed"] is not None:
        return bool(m["managed"])
    if "excluded_from_sleeve_math" in m and m["excluded_from_sleeve_math"] is not None:
        return not bool(m["excluded_from_sleeve_math"])
    review = str(m.get("review_status") or "").strip().lower()
    if review in {"managed", "sleeve", "include"}:
        return True
    if review in {"unmanaged", "excluded", "exclude", "excluded_from_sleeve_math"}:
        return False
    details = str(m.get("details") or "").lower()
    if "managed:true" in details or "sleeve_math:include" in details:
        return True
    if (
        "managed:false" in details
        or "excluded_from_sleeve_math" in details
        or "sleeve_math:exclude" in details
    ):
        return False
    return None


def load_policy_symbols(session: Any, user_id: str) -> frozenset[str]:
    """Data-driven unmanaged-symbol policy. Falls back to DEFAULT on failure."""
    if session is None:
        return DEFAULT_UNMANAGED_SYMBOLS
    try:
        from sqlalchemy import select

        from argosy.state.models import UnmanagedSymbolPolicy

        rows = session.execute(
            select(UnmanagedSymbolPolicy.symbol).where(
                UnmanagedSymbolPolicy.user_id == user_id
            )
        ).scalars().all()
        if not rows:
            return DEFAULT_UNMANAGED_SYMBOLS
        return frozenset(str(s).strip().upper() for s in rows if s)
    except Exception as exc:  # noqa: BLE001
        log.warning("holding_books.policy_load_failed", err=str(exc)[:160])
        return DEFAULT_UNMANAGED_SYMBOLS


def is_managed_position(
    p: Any, *, policy_symbols: frozenset[str] | None = None
) -> bool:
    """True when ``p`` participates in sleeve / allocation percentage math.

    Explicit flags (TSV/JSON) win. Else policy-symbol membership ⇒ unmanaged.
    """
    explicit = parse_explicit_managed_flag(p)
    if explicit is not None:
        return explicit
    sym = _symbol_of(p)
    policy = policy_symbols if policy_symbols is not None else DEFAULT_UNMANAGED_SYMBOLS
    if sym and sym in policy:
        return False
    return True


def managed_positions(
    positions: Sequence[Any] | None,
    *,
    policy_symbols: frozenset[str] | None = None,
) -> list[Any]:
    return [
        p for p in (positions or [])
        if is_managed_position(p, policy_symbols=policy_symbols)
    ]


def total_positions(positions: Sequence[Any] | None) -> list[Any]:
    return list(positions or [])


def position_usd_value_k(p: Any) -> float:
    m = _as_mapping(p)
    raw = (m or {}).get("usd_value_k") if m is not None else getattr(p, "usd_value_k", None)
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def symbol_value_usd_k(positions: Sequence[Any] | None, symbol: str) -> float:
    want = (symbol or "").strip().upper()
    return sum(
        position_usd_value_k(p) for p in (positions or []) if _symbol_of(p) == want
    )


def has_symbol(positions: Sequence[Any] | None, symbol: str) -> bool:
    want = (symbol or "").strip().upper()
    return any(_symbol_of(p) == want for p in (positions or []))


def stamp_management_flags(
    positions: Sequence[Any] | None,
    *,
    policy_symbols: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in positions or []:
        m = _as_mapping(p)
        if m is None:
            continue
        d = dict(m)
        managed = is_managed_position(d, policy_symbols=policy_symbols)
        d["managed"] = managed
        d["excluded_from_sleeve_math"] = not managed
        out.append(d)
    return out


def unmanaged_row_to_position(row: Any) -> dict[str, Any]:
    return {
        "review_status": "",
        "location": getattr(row, "location", "") or "",
        "currency": getattr(row, "currency", "") or "USD",
        "asset_type": getattr(row, "asset_type", "") or "",
        "details": getattr(row, "details", "") or "",
        "symbol": (getattr(row, "symbol", "") or "").upper(),
        "shares": getattr(row, "shares", None),
        "current_price": getattr(row, "current_price", None),
        "avg_price": None,
        "current_value_local": None,
        "usd_value_k": getattr(row, "usd_value_k", None),
        "pct_change": None,
        "managed": False,
        "excluded_from_sleeve_math": True,
        "valued_as_of": getattr(row, "valued_as_of", None),
        "observed_as_of": getattr(row, "observed_as_of", None)
        or getattr(row, "valued_as_of", None),
    }


def load_unmanaged_holding_rows(
    session: Any, user_id: str, *, active_only: bool = True
) -> UnmanagedLoadResult:
    """Load durable unmanaged rows. Failures return ``ok=False`` (never silent [])."""
    if session is None:
        return UnmanagedLoadResult(rows=[], ok=False, error="no session")
    try:
        from sqlalchemy import select

        from argosy.state.models import UnmanagedHolding

        q = select(UnmanagedHolding).where(UnmanagedHolding.user_id == user_id)
        if active_only:
            q = q.where(UnmanagedHolding.status == STATUS_ACTIVE)
        rows = list(session.execute(q).scalars().all())
        return UnmanagedLoadResult(rows=rows, ok=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("holding_books.unmanaged_load_failed", err=str(exc)[:160])
        return UnmanagedLoadResult(rows=[], ok=False, error=str(exc)[:200])


def _row_observed_as_of(row: Any) -> Any:
    return getattr(row, "observed_as_of", None) or getattr(row, "valued_as_of", None)


def _reprice_durable_row(
    row: Any,
    *,
    quote_fn: Any | None,
    fx_usd_nis: float | None,
    fx_usd_eur: float | None,
    today: date,
) -> dict[str, Any] | None:
    """Build a live-priced position from a durable quantity, or None on miss."""
    from argosy.services.snapshot_refresh import reprice_quantity

    shares = getattr(row, "shares", None)
    try:
        sh = float(shares) if shares is not None else 0.0
    except (TypeError, ValueError):
        return None
    if sh <= 0:
        return None
    priced = reprice_quantity(
        symbol=str(getattr(row, "symbol", "") or ""),
        shares=sh,
        currency=str(getattr(row, "currency", None) or "USD"),
        details=str(getattr(row, "details", None) or ""),
        old_price=getattr(row, "current_price", None),
        quote_fn=quote_fn,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
    )
    if priced is None:
        return None
    price, usd_k = priced
    pos = unmanaged_row_to_position(row)
    pos["current_price"] = price
    pos["current_value_local"] = sh * price
    pos["usd_value_k"] = usd_k
    pos["valued_as_of"] = today  # live mark
    pos["repriced"] = True
    return pos


def merge_total_book_positions(
    snapshot_positions: Sequence[Any] | None,
    *,
    unmanaged_rows: Sequence[Any] | None = None,
    policy_symbols: frozenset[str] | None = None,
    include_stale: bool = False,
    today: date | None = None,
    quote_fn: Any | None = None,
    fx_usd_nis: float | None = None,
    fx_usd_eur: float | None = None,
    reprice_failures: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Total book = deduped snapshot + durable quantities REPRICED live.

    Share counts with ``observed_as_of`` within ``QUANTITY_STALE_DAYS`` are
    durable facts. Their value is always computed via the managed-book
    reprice path — never by publishing a stored mark as current money.
    Quantity-stale rows are skipped (integrity marks degraded). Quote misses
    are recorded on ``reprice_failures`` so the caller can degrade loud.
    """
    ref = today or date.today()
    stamped = stamp_management_flags(
        dedupe_positions_by_symbol_location(snapshot_positions),
        policy_symbols=policy_symbols,
    )
    present = {
        (_symbol_of(p), _norm_location(_location_of(p)))
        for p in stamped
        if _symbol_of(p)
    }
    failures = reprice_failures if reprice_failures is not None else []
    for row in unmanaged_rows or []:
        if getattr(row, "status", STATUS_ACTIVE) not in (STATUS_ACTIVE, None, ""):
            continue
        obs = _row_observed_as_of(row)
        if not include_stale and quantity_is_stale(obs, today=ref):
            continue
        sym = (getattr(row, "symbol", "") or "").strip().upper()
        loc = _norm_location(getattr(row, "location", "") or "")
        if not sym or (sym, loc) in present:
            continue
        priced = _reprice_durable_row(
            row, quote_fn=quote_fn,
            fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur, today=ref,
        )
        if priced is None:
            failures.append(
                f"reprice_miss:{sym}@{loc or 'unknown'} "
                f"(observed_as_of={obs})"
            )
            continue
        stamped.append(priced)
        present.add((sym, loc))
    return stamped


def assess_total_book_integrity(
    snapshot_positions: Sequence[Any] | None,
    *,
    load: UnmanagedLoadResult,
    policy_symbols: frozenset[str],
    today: date | None = None,
    reprice_failures: Sequence[str] | None = None,
) -> tuple[bool, str | None]:
    """Return (degraded, reason).

    Degraded when:
      * durable load failed, OR
      * a policy symbol is absent from the snapshot and there is no durable
        quantity within QUANTITY_STALE_DAYS to restore, OR
      * a policy symbol needs restore but live reprice/FX failed.
    """
    if not load.ok:
        return True, f"unmanaged_holdings load failed: {load.error or 'unknown'}"

    ref = today or date.today()
    snap = dedupe_positions_by_symbol_location(snapshot_positions)
    snap_syms = {_symbol_of(p) for p in snap if _symbol_of(p)}
    snap_accounts = {
        location_account_key(_location_of(p))
        for p in snap
        if location_account_key(_location_of(p))
    }
    active_by_sym: dict[str, list[Any]] = {}
    for row in load.rows:
        sym = (getattr(row, "symbol", "") or "").strip().upper()
        if sym:
            active_by_sym.setdefault(sym, []).append(row)

    if reprice_failures:
        # Quote/FX infrastructure failed for a needed restore — loud, real.
        return True, (
            "live reprice unavailable for durable unmanaged quantity — "
            + "; ".join(reprice_failures)
        )

    for sym in policy_symbols:
        if sym in snap_syms:
            continue
        durable = active_by_sym.get(sym) or []
        fresh_qty = [
            r for r in durable
            if not quantity_is_stale(_row_observed_as_of(r), today=ref)
        ]
        if fresh_qty:
            continue  # merge will reprice — fine once quote succeeds
        if durable:
            ages = [str(_row_observed_as_of(r)) for r in durable]
            return True, (
                f"policy symbol {sym} omitted from snapshot; share count(s) "
                f"stale or undated (observed_as_of={ages}; "
                f"max_age_days={QUANTITY_STALE_DAYS}) — refusing unconfirmed quantity"
            )
        has_schwabish = any(
            (a == "schwab" or a.startswith("schwab ")) for a in snap_accounts
        )
        if not snap_accounts or not has_schwabish:
            return True, (
                f"policy symbol {sym} omitted from snapshot and no active "
                f"durable row to restore (incomplete ingest — schwab account "
                f"absent)"
            )
    return False, None


def positions_for_books(
    snapshot_positions: Sequence[Any] | None,
    *,
    unmanaged_rows: Sequence[Any] | None = None,
    policy_symbols: frozenset[str] | None = None,
    today: date | None = None,
    quote_fn: Any | None = None,
    fx_usd_nis: float | None = None,
    fx_usd_eur: float | None = None,
    reprice_failures: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    policy = policy_symbols if policy_symbols is not None else DEFAULT_UNMANAGED_SYMBOLS
    total = merge_total_book_positions(
        snapshot_positions,
        unmanaged_rows=unmanaged_rows,
        policy_symbols=policy,
        include_stale=False,
        today=today,
        quote_fn=quote_fn,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
        reprice_failures=reprice_failures,
    )
    managed = [p for p in total if is_managed_position(p, policy_symbols=policy)]
    return total, managed


class TotalBookDegraded(Exception):
    """Raised when a consumer must refuse to publish money from a degraded book."""

    def __init__(self, reason: str | None):
        self.reason = reason or "total book degraded"
        super().__init__(self.reason)


def load_total_book(
    session: Any,
    user_id: str,
    snapshot_positions: Sequence[Any] | None,
    *,
    today: date | None = None,
    quote_fn: Any | None = None,
    fx_usd_nis: float | None = None,
    fx_usd_eur: float | None = None,
) -> TotalBookResult:
    """Single entry point for total/managed books + integrity.

    Durable unmanaged quantities are REPRICED live (managed-book path). A
    missing quote / FX is a real infrastructure failure and marks degraded.
    """
    ref = today or date.today()
    policy = load_policy_symbols(session, user_id)
    load = load_unmanaged_holding_rows(session, user_id, active_only=True)

    # Resolve FX for USD conversion of non-USD durable rows (NVDA is USD).
    fx_nis = fx_usd_nis
    fx_eur = fx_usd_eur
    if fx_nis is None and session is not None:
        try:
            from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row
            snap = get_latest_snapshot_row(session, user_id)
            if snap is not None:
                fx_nis = float(snap.fx_usd_nis) if snap.fx_usd_nis else None
                fx_eur = float(snap.fx_usd_eur) if snap.fx_usd_eur else None
        except Exception:  # noqa: BLE001
            pass

    reprice_failures: list[str] = []
    # Catch double-counting on the RAW snapshot BEFORE merge drops duplicates.
    degraded = False
    reason: str | None = None
    try:
        books_consistency_check_positions(snapshot_positions)
    except AssertionError as exc:
        degraded = True
        reason = f"duplicate snapshot rows: {exc}"

    total, managed = positions_for_books(
        snapshot_positions,
        unmanaged_rows=load.rows if load.ok else [],
        policy_symbols=policy,
        today=ref,
        quote_fn=quote_fn,
        fx_usd_nis=fx_nis,
        fx_usd_eur=fx_eur,
        reprice_failures=reprice_failures,
    )
    integ_degraded, integ_reason = assess_total_book_integrity(
        snapshot_positions,
        load=load,
        policy_symbols=policy,
        today=ref,
        reprice_failures=reprice_failures,
    )
    if integ_degraded:
        degraded = True
        reason = integ_reason or reason

    # Partition identity on the resulting books (total = managed + unmanaged).
    try:
        total_k = sum(position_usd_value_k(p) for p in total)
        managed_k = sum(position_usd_value_k(p) for p in managed)
        unmanaged_k = sum(
            position_usd_value_k(p)
            for p in total
            if not is_managed_position(p, policy_symbols=policy)
        )
        books_consistency_check(
            total_usd_k=total_k,
            managed_usd_k=managed_k,
            unmanaged_usd_k=unmanaged_k,
        )
    except AssertionError as exc:
        degraded = True
        reason = f"book inconsistency: {exc}"
    return TotalBookResult(
        total=total, managed=managed, load=load,
        degraded=degraded, degrade_reason=reason,
    )


def load_total_and_managed_books(
    session: Any,
    user_id: str,
    snapshot_positions: Sequence[Any] | None,
    *,
    today: date | None = None,
    quote_fn: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load books or raise ``TotalBookDegraded`` — never silently drop the flag.

    Prefer ``load_total_book`` when the caller can handle ``degraded`` itself.
    """
    result = load_total_book(
        session, user_id, snapshot_positions, today=today, quote_fn=quote_fn,
    )
    if result.degraded:
        raise TotalBookDegraded(result.degrade_reason)
    return result.total, result.managed


def _upsert_one(
    session: Any,
    user_id: str,
    *,
    symbol: str,
    location: str,
    shares: float | None,
    current_price: float | None,
    usd_value_k: float | None,
    currency: str,
    asset_type: str,
    details: str,
    reason: str,
    valued_as_of: date | None = None,
    observed_as_of: date | None = None,
) -> Any | None:
    """Upsert one active row inside a SAVEPOINT — never rolls back the caller."""
    from sqlalchemy import select

    from argosy.state.models import UnmanagedHolding

    sym = (symbol or "").strip().upper()
    loc = (location or "").strip()
    if not sym:
        return None
    vas = _as_date(valued_as_of)
    oas = _as_date(observed_as_of) or vas
    nested = session.begin_nested()
    try:
        row = session.execute(
            select(UnmanagedHolding).where(
                UnmanagedHolding.user_id == user_id,
                UnmanagedHolding.symbol == sym,
                UnmanagedHolding.location == loc,
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if row is None:
            row = UnmanagedHolding(
                user_id=user_id,
                symbol=sym,
                location=loc,
                shares=shares,
                current_price=current_price,
                usd_value_k=usd_value_k,
                currency=currency or "USD",
                asset_type=asset_type or "",
                details=details or "",
                reason=reason or "excluded_from_sleeve_math",
                status=STATUS_ACTIVE,
                updated_at=now,
                retired_at=None,
                valued_as_of=vas,
                observed_as_of=oas,
            )
            session.add(row)
        else:
            row.shares = shares
            row.current_price = current_price
            row.usd_value_k = usd_value_k
            row.currency = currency or "USD"
            row.asset_type = asset_type or ""
            row.details = details or ""
            row.reason = reason or row.reason
            row.status = STATUS_ACTIVE
            row.retired_at = None
            row.updated_at = now
            if vas is not None:
                row.valued_as_of = vas
            if oas is not None:
                row.observed_as_of = oas
        nested.commit()
        return row
    except Exception as exc:  # noqa: BLE001
        nested.rollback()
        log.warning(
            "holding_books.unmanaged_upsert_failed",
            symbol=sym, location=loc, err=str(exc)[:160],
        )
        return None


def _retire_one(session: Any, row: Any, *, reason: str) -> bool:
    nested = session.begin_nested()
    try:
        row.status = STATUS_RETIRED
        row.retired_at = datetime.now(timezone.utc)
        row.reason = reason
        row.updated_at = row.retired_at
        nested.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        nested.rollback()
        log.warning(
            "holding_books.unmanaged_retire_failed",
            symbol=getattr(row, "symbol", None), err=str(exc)[:160],
        )
        return False


def sync_unmanaged_from_positions(
    session: Any,
    user_id: str,
    positions: Sequence[Any] | None,
    *,
    commit: bool = True,
    valued_as_of: date | None = None,
    retire_absent_accounts: bool = False,
) -> dict[str, int]:
    """Lifecycle sync: upsert observed unmanaged rows; retire genuine sales.

    Account identity is the **full** normalized location (``schwab 876`` ≠
    ``schwab 999``).

    Retire when the exact account is present in the snapshot but the symbol
    is gone (observed sale at that account).

    KEEP when the exact account is missing — incomplete TSV. Account closure
    is a SEPARATE explicit operation (``retire_unmanaged_account``); the
    ``retire_absent_accounts`` flag is rejected (raises) so a catastrophic
    ingest override cannot recreate the partial-feed false-retire bug.

    Uses SAVEPOINTs — never rolls back the caller's outer session.
    """
    if retire_absent_accounts:
        raise ValueError(
            "retire_absent_accounts is no longer supported on sync — use "
            "retire_unmanaged_account(account_location=...) for an explicit "
            "single-account closure"
        )
    counts = {"upserted": 0, "retired": 0, "errors": 0}
    if session is None:
        return counts
    policy = load_policy_symbols(session, user_id)
    stamped = stamp_management_flags(
        dedupe_positions_by_symbol_location(positions),
        policy_symbols=policy,
    )
    vas = _as_date(valued_as_of) or date.today()

    observed_keys: set[tuple[str, str]] = set()
    for p in stamped:
        if is_managed_position(p, policy_symbols=policy):
            continue
        sym = _symbol_of(p)
        loc = _location_of(p)
        if not sym:
            continue
        observed_keys.add((sym, _norm_location(loc)))
        try:
            if _upsert_one(
                session, user_id,
                symbol=sym, location=loc,
                shares=p.get("shares"),
                current_price=p.get("current_price"),
                usd_value_k=p.get("usd_value_k"),
                currency=str(p.get("currency") or "USD"),
                asset_type=str(p.get("asset_type") or ""),
                details=str(p.get("details") or ""),
                reason="observed_in_snapshot",
                valued_as_of=vas,
                observed_as_of=vas,
            ) is not None:
                counts["upserted"] += 1
            else:
                counts["errors"] += 1
        except Exception as exc:  # noqa: BLE001 — never abort caller's txn
            log.warning(
                "holding_books.unmanaged_upsert_raised",
                symbol=sym, err=str(exc)[:160],
            )
            counts["errors"] += 1

    # Exact accounts present (full location identity).
    snap_accounts = {
        location_account_key(_location_of(p))
        for p in stamped
        if location_account_key(_location_of(p))
    }
    snap_sym_at_account: set[tuple[str, str]] = {
        (_symbol_of(p), location_account_key(_location_of(p)))
        for p in stamped
        if _symbol_of(p) and location_account_key(_location_of(p))
    }

    load = load_unmanaged_holding_rows(session, user_id, active_only=True)
    if not load.ok:
        counts["errors"] += 1
        return counts

    for row in load.rows:
        sym = (getattr(row, "symbol", "") or "").strip().upper()
        loc = getattr(row, "location", "") or ""
        acct = location_account_key(loc)
        key = (sym, _norm_location(loc))
        if key in observed_keys:
            continue
        # Sale at this exact account: account present, symbol gone.
        if acct and acct in snap_accounts and (sym, acct) not in snap_sym_at_account:
            if _retire_one(session, row, reason="retired_after_observed_sale"):
                counts["retired"] += 1
            else:
                counts["errors"] += 1
            continue
        # Account entirely absent → KEEP (incomplete ingest). Closure is
        # retire_unmanaged_account only.

    if commit:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("holding_books.unmanaged_sync_commit_failed", err=str(exc)[:160])
            counts["errors"] += 1
    return counts


def retire_unmanaged_account(
    session: Any,
    user_id: str,
    *,
    account_location: str,
    reason: str,
    actor: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Explicit single-account closure — retires unmanaged rows at ONE location.

    Unlike a blunt catastrophic-drop override, this never touches other
    accounts. ``account_location`` is matched via ``location_account_key``
    (full identity).
    """
    acct = location_account_key(account_location)
    if not acct:
        raise ValueError("account_location is required")
    load = load_unmanaged_holding_rows(session, user_id, active_only=True)
    if not load.ok:
        raise RuntimeError(f"unmanaged load failed: {load.error}")
    retired = 0
    for row in load.rows:
        if location_account_key(getattr(row, "location", "") or "") != acct:
            continue
        note = (
            f"retired_account_closure:{acct}"
            + (f":actor={actor}" if actor else "")
            + (f":{reason}" if reason else "")
        )
        if _retire_one(session, row, reason=note[:500]):
            retired += 1
    if commit:
        session.commit()
    log.warning(
        "holding_books.account_closure",
        user_id=user_id, account=acct, retired=retired,
        actor=actor, reason=reason,
    )
    return {"account": acct, "retired": retired, "actor": actor, "reason": reason}

def backfill_unmanaged_from_snapshots(
    session: Any, *, user_id: str | None = None
) -> int:
    """Idempotent backfill: newest snapshot per (user, symbol, location).

    Runnable against a fixture DB. Does not touch the live DB unless the
    caller points the session at it. Returns number of rows upserted.
    """
    from sqlalchemy import select

    from argosy.state.models import PortfolioSnapshotRow, UnmanagedSymbolPolicy

    n = 0
    # Ensure policy seeds exist for the user(s).
    user_ids: list[str]
    if user_id:
        user_ids = [user_id]
    else:
        from argosy.state.models import User
        user_ids = list(session.execute(select(User.id)).scalars().all())

    for uid in user_ids:
        for sym in DEFAULT_UNMANAGED_SYMBOLS:
            existing = session.execute(
                select(UnmanagedSymbolPolicy).where(
                    UnmanagedSymbolPolicy.user_id == uid,
                    UnmanagedSymbolPolicy.symbol == sym,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(UnmanagedSymbolPolicy(user_id=uid, symbol=sym))

        policy = load_policy_symbols(session, uid)
        snaps = session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == uid)
            .order_by(PortfolioSnapshotRow.id.desc())
        ).scalars().all()
        seen: set[tuple[str, str]] = set()
        for snap in snaps:
            try:
                positions = json.loads(snap.positions_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for p in positions:
                if not isinstance(p, dict):
                    continue
                sym = str(p.get("symbol") or "").strip().upper()
                if sym not in policy:
                    continue
                loc = str(p.get("location") or "").strip()
                key = (sym, _norm_location(loc))
                if key in seen:
                    continue
                seen.add(key)
                if _upsert_one(
                    session, uid,
                    symbol=sym, location=loc,
                    shares=p.get("shares"),
                    current_price=p.get("current_price"),
                    usd_value_k=p.get("usd_value_k"),
                    currency=str(p.get("currency") or "USD"),
                    asset_type=str(p.get("asset_type") or ""),
                    details=str(p.get("details") or ""),
                    reason="backfill_from_historical_snapshot",
                    valued_as_of=_as_date(getattr(snap, "snapshot_date", None)),
                    observed_as_of=_as_date(getattr(snap, "snapshot_date", None)),
                ) is not None:
                    n += 1
    session.commit()
    return n


def parse_positions_json(raw: str | None) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def investable_usd_k(positions: Sequence[Any] | None) -> float:
    """Sum usd_value_k across positions (incl. unmanaged; excl. nothing)."""
    return sum(position_usd_value_k(p) for p in (positions or []))


def implied_nvda_weight_frac(
    resolved: Any,
    *,
    tradeable_securities_nis: float | None = None,
) -> float | None:
    """NVDA weight as a fraction of the **tradeable securities** book (0–1).

    Canonical denominator matches ``nvda_concentration_pct`` / the 13% cap —
    NEVER total net worth (cash + real estate). When status is ``excluded``,
    ``tradeable_securities_nis`` is required; without it returns None (fail
    loud) rather than silently substituting net worth.

    Returns None for unavailable/pending — NEVER coerces to 0.0.
    """
    w = resolved.get("concentration.nvda_current_pct") if resolved is not None else None
    if w is not None and getattr(w, "status", None) == "resolved" and w.value is not None:
        return float(w.value)
    if w is not None and getattr(w, "status", None) == "excluded":
        nv = resolved.get("concentration.nvda_value_nis")
        if (
            nv is not None and getattr(nv, "status", None) == "resolved"
            and nv.value is not None
            and tradeable_securities_nis is not None
            and float(tradeable_securities_nis) > 0
        ):
            return float(nv.value) / float(tradeable_securities_nis)
        return None
    return None


def tradeable_securities_nis_for_user(
    session: Any, user_id: str, *, fx_usd_nis: float | None = None,
) -> float | None:
    """Tradeable securities book in NIS for the implied-weight denominator."""
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row
    from argosy.services.wealth_dashboard import tradeable_securities_usd_k

    snap = get_latest_snapshot_row(session, user_id)
    if snap is None:
        return None
    book = load_total_book(session, user_id, parse_positions_json(snap.positions_json))
    if book.degraded:
        return None
    usd_k = tradeable_securities_usd_k(book.total)
    if usd_k <= 0:
        return None
    fx = fx_usd_nis
    if fx is None or fx <= 0:
        fx = float(snap.fx_usd_nis or 0) or None
    if fx is None or fx <= 0:
        return None
    return usd_k * 1000.0 * float(fx)


# ---------------------------------------------------------------------------
# Snapshot ingest guards (stale date + catastrophic drop)
# ---------------------------------------------------------------------------


class SnapshotIngestRejected(Exception):
    """Raised when a snapshot write would recreate the Jul-13 NVDA wipe."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# Drop more than half the named positions or half the securities value.
_CATASTROPHIC_FRACTION = 0.50
_CATASTROPHIC_MIN_OLD_POSITIONS = 8
_CATASTROPHIC_MIN_OLD_USD_K = 100.0


def _named_position_count(positions: Sequence[Any] | None) -> int:
    return sum(1 for p in (positions or []) if _symbol_of(p) and _symbol_of(p) not in {"-", "—"})


def _securities_usd_k(positions: Sequence[Any] | None) -> float:
    total = 0.0
    for p in positions or []:
        m = _as_mapping(p) or {}
        at = str(m.get("asset_type") or "").lower()
        if "cash" in at:
            continue
        if not _symbol_of(p) or _symbol_of(p) in {"-", "—"}:
            continue
        total += position_usd_value_k(p)
    return total


def assess_snapshot_ingest(
    *,
    latest_row: Any | None,
    new_positions: Sequence[Any] | None,
    new_snapshot_date: Any | None,
    allow_stale: bool = False,
    allow_catastrophic_drop: bool = False,
) -> None:
    """Raise ``SnapshotIngestRejected`` when the write would be destructive."""
    if latest_row is None:
        return
    if not allow_stale:
        old_date = getattr(latest_row, "snapshot_date", None)
        if old_date is not None and new_snapshot_date is not None:
            if new_snapshot_date < old_date:
                raise SnapshotIngestRejected(
                    "stale_snapshot_date",
                    f"incoming snapshot_date {new_snapshot_date} precedes "
                    f"latest {old_date}",
                )
    if allow_catastrophic_drop:
        return
    try:
        old_positions = json.loads(getattr(latest_row, "positions_json", None) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        old_positions = []
    old_n = _named_position_count(old_positions)
    new_n = _named_position_count(new_positions)
    if old_n >= _CATASTROPHIC_MIN_OLD_POSITIONS and new_n < old_n * _CATASTROPHIC_FRACTION:
        raise SnapshotIngestRejected(
            "catastrophic_position_drop",
            f"named positions {old_n} → {new_n} "
            f"(below {_CATASTROPHIC_FRACTION:.0%} retention)",
        )
    old_v = _securities_usd_k(old_positions)
    new_v = _securities_usd_k(new_positions)
    if old_v >= _CATASTROPHIC_MIN_OLD_USD_K and new_v < old_v * _CATASTROPHIC_FRACTION:
        raise SnapshotIngestRejected(
            "catastrophic_value_drop",
            f"securities usd_k {old_v:.1f} → {new_v:.1f} "
            f"(below {_CATASTROPHIC_FRACTION:.0%} retention)",
        )


def books_consistency_check_positions(positions: Sequence[Any] | None) -> None:
    """Fail when duplicate (symbol, location) rows remain after merge.

    Double-counted NVDA is money-wrong; this is the live guard invoked from
    ``load_total_book``.
    """
    seen: set[tuple[str, str]] = set()
    for p in positions or []:
        sym = _symbol_of(p)
        if not sym:
            continue
        key = (sym, _norm_location(_location_of(p)))
        if key in seen:
            raise AssertionError(
                f"duplicate position after merge: {key[0]} @ {key[1]!r}"
            )
        seen.add(key)


def books_consistency_check(
    *,
    total_usd_k: float,
    managed_usd_k: float,
    unmanaged_usd_k: float,
    tol: float = 1.0,
    positions: Sequence[Any] | None = None,
) -> None:
    """Fail when total ≠ managed + unmanaged, or when duplicates remain."""
    if positions is not None:
        books_consistency_check_positions(positions)
    if abs(total_usd_k - (managed_usd_k + unmanaged_usd_k)) > tol:
        raise AssertionError(
            f"book inconsistency: total={total_usd_k} "
            f"managed={managed_usd_k} unmanaged={unmanaged_usd_k}"
        )


__all__ = [
    "DEFAULT_UNMANAGED_SYMBOLS",
    "STATUS_ACTIVE",
    "STATUS_RETIRED",
    "QUANTITY_STALE_DAYS",
    "STALE_VALUATION_DAYS",
    "SnapshotIngestRejected",
    "TotalBookDegraded",
    "TotalBookResult",
    "UnmanagedLoadResult",
    "assess_snapshot_ingest",
    "assess_total_book_integrity",
    "backfill_unmanaged_from_snapshots",
    "books_consistency_check",
    "books_consistency_check_positions",
    "dedupe_positions_by_symbol_location",
    "has_symbol",
    "implied_nvda_weight_frac",
    "investable_usd_k",
    "is_managed_position",
    "load_policy_symbols",
    "load_total_and_managed_books",
    "load_total_book",
    "load_unmanaged_holding_rows",
    "location_account_key",
    "managed_positions",
    "merge_total_book_positions",
    "parse_explicit_managed_flag",
    "parse_positions_json",
    "position_usd_value_k",
    "positions_for_books",
    "quantity_is_stale",
    "retire_unmanaged_account",
    "stamp_management_flags",
    "symbol_value_usd_k",
    "sync_unmanaged_from_positions",
    "total_positions",
    "tradeable_securities_nis_for_user",
    "unmanaged_row_to_position",
    "valuation_is_stale",
]
