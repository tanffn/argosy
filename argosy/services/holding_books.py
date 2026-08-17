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

# ---------------------------------------------------------------------------
# Valuation-clock policy (Stream D — binding for every money surface)
# ---------------------------------------------------------------------------
# LIVE clock: every money-publishing surface values the book against
# ``date.today()`` (or an explicit test ``today=``). Snapshot date is
# quantity / observation metadata only (``observed_as_of``,
# ``assumptions.snapshot_date``).
#
# Consequences:
#   * Stored marks older than MARK_STALE_DAYS vs the live clock must
#     live-reprice or degrade — never publish July dollars as "today".
#   * Live quotes ALWAYS stamp ``valued_as_of`` to the valuation clock
#     (today), never to ``snapshot_date``. A live price wearing a
#     historical stamp is an output-trust violation.
#   * Dashboard / net-worth / plan resolver / ``/portfolio/snapshot``
#     share this clock; the UI must surface the as-of date.
#   * Historical "as-of snapshot" math is NOT offered on money surfaces.
VALUATION_CLOCK_POLICY = "live"

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"

# Material disagreement between totals_json and the auditable row sum (usd_k).
_TOTALS_ROW_DISAGREE_ABS_K = 1.0
_TOTALS_ROW_DISAGREE_FRAC = 0.02


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
                "review_status", "observed_as_of", "valued_as_of",
                "carried_forward", "mark_stale",
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

# A stored mark older than this must be live-repriced before it can publish
# as current money — whether the symbol is present in the snapshot or restored
# from the durable unmanaged book. 0 = must be valued today.
MARK_STALE_DAYS = 0

# Display-name renames that are NOT sales (same account, same units).
# Keys/values are exact TSV symbol strings (Hebrew preserved).
KNOWN_SYMBOL_RENAMES: dict[str, str] = {
    'מחקה ת"א-200': 'ת"א-200',
}


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


def mark_is_stale(
    valued_as_of: Any,
    *,
    today: date | None = None,
    max_age_days: int = MARK_STALE_DAYS,
) -> bool:
    """True when a stored PRICE/mark is too old to publish as current money.

    A missing date is NOT a fresh date — undated marks are stale. Callers that
    know the snapshot as-of should stamp ``valued_as_of`` / ``observed_as_of``
    first (see ``load_total_book``); if still undated after that, refuse to
    publish the last known value as today's money.
    """
    d = _as_date(valued_as_of)
    if d is None:
        return True
    ref = today or date.today()
    return (ref - d).days > max_age_days


def stamp_mark_dates(
    positions: Sequence[Any] | None,
    *,
    snapshot_date: date | None,
) -> list[dict[str, Any]]:
    """Fill missing valued/observed dates from the snapshot as-of."""
    as_of = _as_date(snapshot_date)
    out: list[dict[str, Any]] = []
    for p in positions or []:
        m = _as_mapping(p)
        if m is None:
            continue
        d = dict(m)
        if as_of is not None:
            if d.get("valued_as_of") is None:
                d["valued_as_of"] = as_of
            if d.get("observed_as_of") is None:
                d["observed_as_of"] = as_of
        out.append(d)
    return out


def valuation_is_stale(
    valued_as_of: Any, *, today: date | None = None, max_age_days: int = QUANTITY_STALE_DAYS,
) -> bool:
    """Deprecated alias — quantity age is the durable-book gate now.

    Kept for callers/tests; prefer ``quantity_is_stale`` + live reprice.
    """
    return quantity_is_stale(valued_as_of, today=today, max_age_days=max_age_days)


def normalize_symbol_identity(symbol: str) -> str:
    """Map known renames onto the canonical symbol string."""
    s = (symbol or "").strip()
    return KNOWN_SYMBOL_RENAMES.get(s, s)


def accounts_covered_from_positions(positions: Sequence[Any] | None) -> frozenset[str]:
    """Account keys the feed actually mentioned (coverage ≠ emptiness)."""
    return frozenset(
        location_account_key(_location_of(p))
        for p in (positions or [])
        if location_account_key(_location_of(p))
    )


def position_feed_fingerprint(positions: Sequence[Any] | None) -> list[list[Any]]:
    """Content fingerprint of a feed: symbol, account, shares, usd_value_k.

    Used by ``latest_matches_snapshot`` so same-shape symbol replacements
    (CSPX,NKE → CSPX,NEW) are never treated as no-ops.
    """
    rows: list[list[Any]] = []
    for p in positions or []:
        m = _as_mapping(p)
        if m is None:
            continue
        try:
            shares = round(float(m.get("shares") or 0.0), 6)
        except (TypeError, ValueError):
            shares = 0.0
        try:
            usd = round(float(m.get("usd_value_k") or 0.0), 3)
        except (TypeError, ValueError):
            usd = 0.0
        rows.append([
            (m.get("symbol") or "").strip().upper(),
            location_account_key(_location_of(m))
            or _norm_location(str(m.get("location") or "")),
            shares,
            usd,
        ])
    rows.sort()
    return rows


def ensure_default_unmanaged_policy(session: Any, user_id: str) -> list[str]:
    """Idempotent: seed ``DEFAULT_UNMANAGED_SYMBOLS`` for ``user_id``.

    Migration 0097 only seeds users present at upgrade time. New tenants
    must receive the same integrity gate via onboarding.
    """
    if session is None or not user_id:
        return []
    from sqlalchemy import select

    from argosy.state.models import UnmanagedSymbolPolicy

    seeded: list[str] = []
    for sym in sorted(DEFAULT_UNMANAGED_SYMBOLS):
        existing = session.execute(
            select(UnmanagedSymbolPolicy).where(
                UnmanagedSymbolPolicy.user_id == user_id,
                UnmanagedSymbolPolicy.symbol == sym,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(UnmanagedSymbolPolicy(user_id=user_id, symbol=sym))
            seeded.append(sym)
    return seeded


def derive_accounts_carried_from_dates(
    positions: Sequence[Any] | None,
    *,
    snapshot_date: date | None,
) -> list[str]:
    """Accounts whose quantity vintage predates the book snapshot_date."""
    snap = _as_date(snapshot_date)
    if snap is None:
        return []
    carried: set[str] = set()
    for p in positions or []:
        acct = location_account_key(_location_of(p))
        if not acct:
            continue
        obs = _as_date(
            (_as_mapping(p) or {}).get("observed_as_of")
            if _as_mapping(p) is not None
            else None
        )
        if obs is not None and obs < snap:
            carried.add(acct)
    return sorted(carried)


def accounts_carried_provenance(
    *,
    reconstructed_accounts: Sequence[str],
    latest_accounts: Sequence[str] | set[str] | frozenset[str],
    latest_row: Any | None = None,
    reconstructed_positions: Sequence[Any] | None = None,
    latest_positions: Sequence[Any] | None = None,
) -> list[str]:
    """Honest ``accounts_carried`` for dry-run / noop reporting.

    When the latest row already matches the reconstruction, do NOT report
    ``[]`` merely because those accounts are now present — prefer the
    carry list recorded on the row, else derive from observed_as_of.
    """
    latest_set = set(latest_accounts or [])
    fresh_carry = sorted(a for a in reconstructed_accounts if a not in latest_set)

    if latest_row is None:
        return fresh_carry

    # Prefer comparing books when both sides are available.
    if latest_positions is not None and reconstructed_positions is not None:
        if not books_match_for_restore(latest_positions, reconstructed_positions):
            return fresh_carry
    elif fresh_carry:
        return fresh_carry

    # No-op / already-restored: report stored provenance.
    try:
        totals = json.loads(getattr(latest_row, "totals_json", None) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        totals = {}
    stored = [str(a) for a in (totals.get("accounts_carried") or []) if a]
    if stored:
        return stored
    return derive_accounts_carried_from_dates(
        latest_positions if latest_positions is not None else reconstructed_positions,
        snapshot_date=getattr(latest_row, "snapshot_date", None),
    )


def resolve_prior_positions_by_account_coverage(
    session: Any,
    user_id: str,
) -> list[dict[str, Any]]:
    """Rebuild the prior book from each account's most recent covering snapshot.

    The globally latest snapshot is the wrong reference when it omitted an
    account (partial feed). Coverage ≠ emptiness: walk history newest-first
    and take the first observation of each account key. Each carried row
    keeps / receives ``observed_as_of`` from the snapshot that covered it.
    """
    from sqlalchemy import desc, select

    from argosy.state.models import PortfolioSnapshotRow

    if session is None:
        return []
    rows = session.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(
            desc(PortfolioSnapshotRow.imported_at),
            desc(PortfolioSnapshotRow.id),
        )
    ).scalars().all()

    seen_accounts: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            positions = json.loads(getattr(row, "positions_json", None) or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(positions, list):
            continue
        snap_date = _as_date(getattr(row, "snapshot_date", None))
        by_acct: dict[str, list[dict[str, Any]]] = {}
        for p in positions:
            m = _as_mapping(p)
            if m is None:
                continue
            d = dict(m)
            ak = location_account_key(_location_of(d))
            if not ak:
                continue
            by_acct.setdefault(ak, []).append(d)
        for ak, acct_rows in by_acct.items():
            if ak in seen_accounts:
                continue
            seen_accounts.add(ak)
            for d in acct_rows:
                if d.get("observed_as_of") is None and snap_date is not None:
                    d["observed_as_of"] = snap_date
                if d.get("valued_as_of") is None and snap_date is not None:
                    d["valued_as_of"] = snap_date
                d["coverage_source_snapshot_id"] = getattr(row, "id", None)
                out.append(d)
    return out


def _position_shares(p: Any) -> float | None:
    m = _as_mapping(p) or {}
    raw = m.get("shares")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _symbol_key_for_merge(symbol: str) -> str:
    """Identity key: renamed symbols collapse; bare hyphen stays distinct."""
    s = normalize_symbol_identity(symbol)
    if s == "-":
        return "-"
    return s.strip().upper()


@dataclass(frozen=True)
class AccountMergeResult:
    """Per-account merge of an incoming feed onto the prior book."""

    positions: list[dict[str, Any]]
    accounts_covered: tuple[str, ...]
    accounts_carried: tuple[str, ...]
    renames: tuple[tuple[str, str, str, float], ...]  # old, new, account, shares


def merge_positions_per_account(
    *,
    prior_positions: Sequence[Any] | None,
    incoming_positions: Sequence[Any] | None,
    incoming_snapshot_date: date | None,
    prior_snapshot_date: date | None = None,
) -> AccountMergeResult:
    """Merge an incoming feed onto the prior book by account coverage.

    Lifecycle (matches ``unmanaged_holdings``):
      * A feed that does not mention an account carries NO information about
        it — those holdings are carried forward with their own
        ``observed_as_of``.
      * Within a covered account, disappearance of a symbol is a sale.
      * Known renames (and 1:1 same-shares replacements) are NOT scored as
        sale + purchase.
      * If a feed contains BOTH sides of a rename alias in the same account,
        that is a conflict — refuse rather than silently dropping money.
      * Degenerate ``-`` symbols never collapse across rows (anon keys) and
        are never rename pairs.
    """
    incoming_date = _as_date(incoming_snapshot_date)
    prior_date = _as_date(prior_snapshot_date)
    covered = accounts_covered_from_positions(incoming_positions)

    prior_by_acct: dict[str, list[dict[str, Any]]] = {}
    for p in prior_positions or []:
        m = _as_mapping(p)
        if m is None:
            continue
        d = dict(m)
        ak = location_account_key(_location_of(d))
        if not ak:
            continue
        # Preserve prior observation dates if already stamped.
        if d.get("observed_as_of") is None and prior_date is not None:
            d["observed_as_of"] = prior_date
        if d.get("valued_as_of") is None and prior_date is not None:
            d["valued_as_of"] = prior_date
        prior_by_acct.setdefault(ak, []).append(d)

    incoming_by_acct: dict[str, list[dict[str, Any]]] = {}
    for p in incoming_positions or []:
        m = _as_mapping(p)
        if m is None:
            continue
        d = dict(m)
        ak = location_account_key(_location_of(d))
        if not ak:
            continue
        # Do NOT canonicalize yet — dual-alias conflict detection needs raws.
        d["observed_as_of"] = incoming_date
        d["valued_as_of"] = incoming_date
        d["carried_forward"] = False
        incoming_by_acct.setdefault(ak, []).append(d)

    merged: list[dict[str, Any]] = []
    carried_accounts: list[str] = []
    renames: list[tuple[str, str, str, float]] = []

    # Carry accounts the feed did not cover.
    for ak, rows in prior_by_acct.items():
        if ak in covered:
            continue
        carried_accounts.append(ak)
        for d in rows:
            c = dict(d)
            c["carried_forward"] = True
            merged.append(c)

    # Replace covered accounts from the feed; detect renames vs sales.
    for ak in sorted(covered):
        prior_rows = prior_by_acct.get(ak) or []
        inc_rows = incoming_by_acct.get(ak) or []

        # Dual-alias in the same feed/account is a conflict, not a rename.
        canon_to_raws: dict[str, set[str]] = {}
        for d in inc_rows:
            raw_sym = str(d.get("symbol") or "")
            if not raw_sym or raw_sym == "-":
                continue
            ck = _symbol_key_for_merge(raw_sym)
            if not ck or ck == "-":
                continue
            canon_to_raws.setdefault(ck, set()).add(raw_sym)
        for ck, raws in canon_to_raws.items():
            if len(raws) > 1:
                raise SnapshotIngestRejected(
                    "alias_conflict",
                    f"feed contains multiple aliases of the same identity "
                    f"{sorted(raws)!r} in account {ak!r}; refusing to "
                    f"collapse (a rename must never delete money)",
                )

        # Canonical form is a MERGE KEY only — never mutate the stored
        # symbol string. Rewriting ``מחקה ת"א-200`` → ``ת"א-200`` silently
        # breaks symbol-keyed consumers (instrument_plan_classes, tests,
        # classification). Keep the feed's original symbol authoritative.
        prior_by_sym: dict[str, dict[str, Any]] = {}
        for d in prior_rows:
            sk = _symbol_key_for_merge(str(d.get("symbol") or ""))
            if sk and sk not in {"", "-"} and sk not in prior_by_sym:
                prior_by_sym[sk] = d

        inc_by_sym: dict[str, dict[str, Any]] = {}
        for d in inc_rows:
            sk = _symbol_key_for_merge(str(d.get("symbol") or ""))
            # Empty / hyphen keys stay distinct per occurrence — two `-`
            # lots must never collide, overwrite, or rename each other.
            if not sk or sk == "-":
                sk = f"__anon:{len(inc_by_sym)}"
            if sk not in inc_by_sym:
                inc_by_sym[sk] = d

        # Record known renames when prior had the old name and the feed
        # carries any alias of the canonical identity (raw symbol preserved).
        for old_sym, new_sym in KNOWN_SYMBOL_RENAMES.items():
            old_k = _symbol_key_for_merge(old_sym)
            new_k = _symbol_key_for_merge(new_sym)
            if old_k in prior_by_sym and new_k in inc_by_sym:
                sh = _position_shares(inc_by_sym[new_k])
                if sh is not None:
                    renames.append((old_sym, new_sym, ak, sh))

        # Heuristic: within this account, unmatched prior↔incoming with equal
        # shares (exactly one pair) is a rename, not a sale+buy.
        # General mechanism — not limited to KNOWN_SYMBOL_RENAMES entries.
        gone = {
            sk: prior_by_sym[sk]
            for sk in prior_by_sym
            if sk not in inc_by_sym and sk not in {"", "-"}
            and not sk.startswith("__anon:")
        }
        appeared = {
            sk: inc_by_sym[sk]
            for sk in inc_by_sym
            if sk not in prior_by_sym and not sk.startswith("__anon:")
            and sk not in {"", "-"}
        }
        if len(gone) == 1 and len(appeared) == 1:
            (old_k, old_d), = gone.items()
            (new_k, new_d), = appeared.items()
            old_sh = _position_shares(old_d)
            new_sh = _position_shares(new_d)
            if (
                old_sh is not None and new_sh is not None
                and abs(old_sh - new_sh) < 1e-9
            ):
                renames.append((
                    str(old_d.get("symbol") or old_k),
                    str(new_d.get("symbol") or new_k),
                    ak,
                    float(new_sh),
                ))

        merged.extend(inc_by_sym[sk] for sk in inc_by_sym)

    return AccountMergeResult(
        positions=merged,
        accounts_covered=tuple(sorted(covered)),
        accounts_carried=tuple(sorted(set(carried_accounts))),
        renames=tuple(renames),
    )


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


def load_explicit_policy_symbols(session: Any, user_id: str) -> frozenset[str]:
    """Configured unmanaged-symbol policy ONLY — empty when none configured.

    Unlike ``load_policy_symbols``, does NOT fall back to
    ``DEFAULT_UNMANAGED_SYMBOLS``. Integrity checks that would refuse to
    publish a book must use this: an empty policy table must not invent an
    NVDA-must-be-present requirement that degrades every partial seed.
    """
    if session is None:
        return frozenset()
    try:
        from sqlalchemy import select

        from argosy.state.models import UnmanagedSymbolPolicy

        rows = session.execute(
            select(UnmanagedSymbolPolicy.symbol).where(
                UnmanagedSymbolPolicy.user_id == user_id
            )
        ).scalars().all()
        return frozenset(str(s).strip().upper() for s in rows if s)
    except Exception as exc:  # noqa: BLE001
        log.warning("holding_books.explicit_policy_load_failed", err=str(exc)[:160])
        return frozenset()


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


def _reprice_position_dict(
    pos: dict[str, Any],
    *,
    quote_fn: Any | None,
    fx_usd_nis: float | None,
    fx_usd_eur: float | None,
    today: date,
) -> dict[str, Any] | None:
    """Live-reprice one snapshot position dict; None on miss / unpriceable."""
    from argosy.services.snapshot_refresh import reprice_quantity

    shares = pos.get("shares")
    try:
        sh = float(shares) if shares is not None else 0.0
    except (TypeError, ValueError):
        return None
    if sh <= 0:
        return None
    priced = reprice_quantity(
        symbol=str(pos.get("symbol") or ""),
        shares=sh,
        currency=str(pos.get("currency") or "USD"),
        details=str(pos.get("details") or ""),
        old_price=pos.get("current_price"),
        quote_fn=quote_fn,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
    )
    if priced is None:
        return None
    price, usd_k = priced
    out = dict(pos)
    out["current_price"] = price
    out["current_value_local"] = sh * price
    out["usd_value_k"] = usd_k
    out["valued_as_of"] = today
    out["mark_stale"] = False
    out["repriced"] = True
    return out


def _position_mark_date(pos: Mapping[str, Any] | dict[str, Any]) -> Any:
    return pos.get("valued_as_of") or pos.get("observed_as_of")


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
    durable facts. Stored marks — whether the symbol is present in the
    snapshot or restored from the durable book — are never published as
    current money when stale: they are live-repriced, or omitted with a
    failure recorded on ``reprice_failures``.
    """
    ref = today or date.today()
    stamped = stamp_management_flags(
        dedupe_positions_by_symbol_location(snapshot_positions),
        policy_symbols=policy_symbols,
    )
    failures = reprice_failures if reprice_failures is not None else []

    # Reprice (or refuse) stale marks on positions ALREADY in the snapshot.
    refreshed: list[dict[str, Any]] = []
    for pos in stamped:
        mark_date = _position_mark_date(pos)
        explicit_stale = bool(pos.get("mark_stale"))
        # Ephemeral in-memory books (sleeve unit tests) may omit mark dates
        # entirely — treat those as caller-supplied current values. Stored
        # marks must be stamped by ``load_total_book`` (which forces
        # ``mark_stale`` when still undated after stamping).
        if mark_date is None and not explicit_stale:
            refreshed.append(pos)
            continue
        needs_reprice = explicit_stale or mark_is_stale(mark_date, today=ref)
        if not needs_reprice:
            refreshed.append(pos)
            continue
        at = str(pos.get("asset_type") or "").lower()
        sym = _symbol_of(pos)
        # Cash / unpriceable / shareless rows: cannot live-reprice. Keep the
        # quantity-shaped row but never pretend the mark is fresh.
        from argosy.services.snapshot_refresh import _PRICEABLE_SYMBOL_RE
        priceable = bool(sym) and bool(_PRICEABLE_SYMBOL_RE.match(sym))
        shares_ok = False
        try:
            shares_ok = float(pos.get("shares") or 0) > 0
        except (TypeError, ValueError):
            shares_ok = False
        if "cash" in at or not priceable or not shares_ok:
            stripped = dict(pos)
            stripped["mark_stale"] = True
            # Cash balances ARE the mark — quantity-shaped cash can keep value.
            # Every other unpriceable stale row must NOT publish last-known money
            # as current: null it and record a loud reprice failure.
            if "cash" in at:
                refreshed.append(stripped)
                continue
            failures.append(
                f"stale_mark_unpriceable:{sym or '-'}@"
                f"{_norm_location(_location_of(pos)) or 'unknown'} "
                f"(valued_as_of={mark_date})"
            )
            stripped["usd_value_k"] = None
            stripped["current_price"] = None
            refreshed.append(stripped)
            continue
        priced = _reprice_position_dict(
            pos, quote_fn=quote_fn,
            fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur, today=ref,
        )
        if priced is None:
            failures.append(
                f"stale_mark_reprice_miss:{sym}@"
                f"{_norm_location(_location_of(pos)) or 'unknown'} "
                f"(valued_as_of={mark_date})"
            )
            if include_stale:
                refreshed.append(pos)
            else:
                stripped = dict(pos)
                stripped["usd_value_k"] = None
                stripped["current_price"] = None
                stripped["mark_stale"] = True
                refreshed.append(stripped)
            continue
        refreshed.append(priced)
    stamped = refreshed

    present = {
        (_symbol_of(p), _norm_location(_location_of(p)))
        for p in stamped
        if _symbol_of(p)
    }
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
      * a policy symbol needs restore but live reprice/FX failed, OR
      * a stale stored mark could not be live-repriced (present OR absent).
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
        return True, (
            "live reprice unavailable for current-money marks — "
            + "; ".join(reprice_failures)
        )

    for sym in policy_symbols:
        if sym in snap_syms:
            # Present — stale-mark refusal is enforced in merge via live
            # reprice (failures recorded on reprice_failures above).
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
    snapshot_date: date | None = None,
) -> TotalBookResult:
    """Single entry point for total/managed books + integrity.

    Durable unmanaged quantities are REPRICED live (managed-book path). A
    missing quote / FX is a real infrastructure failure and marks degraded.
    ``snapshot_date`` stamps missing mark dates so a July position in a
    July-dated snapshot cannot publish as current money on a later day.
    """
    ref = today or date.today()
    policy = load_policy_symbols(session, user_id)
    explicit_policy = load_explicit_policy_symbols(session, user_id)
    load = load_unmanaged_holding_rows(session, user_id, active_only=True)

    # Resolve FX for USD conversion of non-USD durable rows (NVDA is USD).
    fx_nis = fx_usd_nis
    fx_eur = fx_usd_eur
    snap_date = _as_date(snapshot_date)
    if session is not None:
        try:
            from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row
            snap = get_latest_snapshot_row(session, user_id)
            if snap is not None:
                if fx_nis is None:
                    fx_nis = float(snap.fx_usd_nis) if snap.fx_usd_nis else None
                if fx_eur is None:
                    fx_eur = float(snap.fx_usd_eur) if snap.fx_usd_eur else None
                if snap_date is None:
                    snap_date = _as_date(getattr(snap, "snapshot_date", None))
        except Exception:  # noqa: BLE001
            pass

    stamped_positions = stamp_mark_dates(
        snapshot_positions, snapshot_date=snap_date,
    )
    # After stamping from the snapshot as-of, any still-undated mark is
    # unknown — never publish as current money (BLOCKER 6).
    for p in stamped_positions:
        if _position_mark_date(p) is None:
            p["mark_stale"] = True

    reprice_failures: list[str] = []
    # Catch double-counting on the RAW snapshot BEFORE merge drops duplicates.
    degraded = False
    reason: str | None = None
    try:
        books_consistency_check_positions(stamped_positions)
    except AssertionError as exc:
        degraded = True
        reason = f"duplicate snapshot rows: {exc}"

    total, managed = positions_for_books(
        stamped_positions,
        unmanaged_rows=load.rows if load.ok else [],
        policy_symbols=policy,
        today=ref,
        quote_fn=quote_fn,
        fx_usd_nis=fx_nis,
        fx_usd_eur=fx_eur,
        reprice_failures=reprice_failures,
    )
    integ_degraded, integ_reason = assess_total_book_integrity(
        stamped_positions,
        load=load,
        # Only ENFORCED when the operator configured policy rows — never
        # invent an NVDA-must-restore gate from DEFAULT_UNMANAGED_SYMBOLS.
        policy_symbols=explicit_policy,
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
    default_vas = _as_date(valued_as_of) or date.today()

    observed_keys: set[tuple[str, str]] = set()
    for p in stamped:
        if is_managed_position(p, policy_symbols=policy):
            continue
        sym = _symbol_of(p)
        loc = _location_of(p)
        if not sym:
            continue
        observed_keys.add((sym, _norm_location(loc)))
        # A row's observation date is when THAT row was observed — never the
        # feed date of some other account. Prefer per-position stamps.
        row_oas = _as_date(p.get("observed_as_of")) or default_vas
        row_vas = _as_date(p.get("valued_as_of")) or default_vas
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
                valued_as_of=row_vas,
                observed_as_of=row_oas,
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


# Live-book restoration targets (Ariel truncated-book incident, 2026-08-08).
# Used by the operator backfill to self-verify; not a hardcode inside merge.
EXPECTED_RESTORED_POSITION_COUNT = 46
EXPECTED_RESTORED_USD_K = 4047.6
EXPECTED_RESTORED_USD_K_TOL = 0.5


def _position_identity_key(p: Mapping[str, Any] | dict[str, Any]) -> tuple:
    return (
        str(p.get("symbol") or ""),
        _norm_location(str(p.get("location") or "")),
        round(float(p.get("shares") or 0.0), 6),
    )


def books_match_for_restore(
    a: Sequence[Any] | None,
    b: Sequence[Any] | None,
    *,
    tol_usd_k: float = EXPECTED_RESTORED_USD_K_TOL,
) -> bool:
    """True when two books share the same (symbol, location, shares) set + total."""
    da = [dict(_as_mapping(p) or {}) for p in (a or []) if _as_mapping(p)]
    db = [dict(_as_mapping(p) or {}) for p in (b or []) if _as_mapping(p)]
    if len(da) != len(db):
        return False
    ka = sorted(_position_identity_key(p) for p in da)
    kb = sorted(_position_identity_key(p) for p in db)
    if ka != kb:
        return False
    ta = sum(float(p.get("usd_value_k") or 0.0) for p in da)
    tb = sum(float(p.get("usd_value_k") or 0.0) for p in db)
    return abs(ta - tb) <= tol_usd_k


def _require_unmanaged_holdings_schema(session: Any) -> None:
    """Fail closed when migration 0097 tables are absent.

    Restoring the book without ``unmanaged_holdings`` would bring NVDA back
    as MANAGED — the exact sleeve-math distortion this stream removes.
    """
    from sqlalchemy import inspect as sa_inspect

    bind = session.get_bind()
    insp = sa_inspect(bind)
    missing = [
        name for name in ("unmanaged_holdings", "unmanaged_symbol_policy")
        if not insp.has_table(name)
    ]
    if missing:
        raise RuntimeError(
            "prerequisite missing: "
            + ", ".join(missing)
            + " (alembic revision 0097_unmanaged_holdings). "
            "Refusing to restore — policy holdings would return as managed."
        )


def backfill_restored_holdings_book(
    session: Any,
    *,
    user_id: str,
    expected_position_count: int | None = EXPECTED_RESTORED_POSITION_COUNT,
    expected_usd_k: float | None = EXPECTED_RESTORED_USD_K,
    expected_usd_k_tol: float = EXPECTED_RESTORED_USD_K_TOL,
    commit: bool = True,
    actor: str = "operator",
) -> dict[str, Any]:
    """Idempotent one-off restore of the truncated multi-account book.

    Rebuilds the current book from each account's last covering snapshot
    (same mechanism as ingest merge). Safe to re-run: when the latest row
    already matches the reconstruction, returns ``status=noop`` without
    writing. On write, verifies position count / total and rolls back
    (raises) on mismatch.

    Does not touch any DB the caller did not hand it — operators must
    point the session at a COPY, never the live ``db/argosy.db`` casually.

    Fail-closed: refuses to write when ``unmanaged_holdings`` /
    ``unmanaged_symbol_policy`` are missing, or when durable unmanaged sync
    cannot upsert a policy holding.
    """
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
    )

    _require_unmanaged_holdings_schema(session)

    reconstructed = resolve_prior_positions_by_account_coverage(session, user_id)
    n = len(reconstructed)
    total = sum(float(p.get("usd_value_k") or 0.0) for p in reconstructed)
    accounts = sorted({
        location_account_key(_location_of(p))
        for p in reconstructed
        if location_account_key(_location_of(p))
    })

    if expected_position_count is not None and n != expected_position_count:
        raise AssertionError(
            f"restore reconstruction yielded {n} positions, "
            f"expected {expected_position_count}"
        )
    if expected_usd_k is not None and abs(total - expected_usd_k) > expected_usd_k_tol:
        raise AssertionError(
            f"restore reconstruction yielded ${total:.1f}k, "
            f"expected ${expected_usd_k:.1f}k ±{expected_usd_k_tol}"
        )

    latest = get_latest_snapshot_row(session, user_id)
    latest_accounts: frozenset[str] = frozenset()
    if latest is not None:
        try:
            current = json.loads(latest.positions_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            current = []
        latest_accounts = accounts_covered_from_positions(current)
        if books_match_for_restore(current, reconstructed):
            # Honest carry provenance on no-op: the restored row still
            # carries July-dated accounts even when they are now present.
            carried = accounts_carried_provenance(
                reconstructed_accounts=accounts,
                latest_accounts=latest_accounts,
                latest_row=latest,
                reconstructed_positions=reconstructed,
                latest_positions=current,
            )
            return {
                "status": "noop",
                "position_count": n,
                "total_usd_k": round(total, 1),
                "accounts": accounts,
                "accounts_covered": accounts,
                "accounts_carried": carried,
                "latest_snapshot_id": getattr(latest, "id", None),
            }

    # Accounts absent from the pre-restore latest are the ones this restore
    # is carrying forward from earlier covering snapshots.
    accounts_carried = sorted(a for a in accounts if a not in latest_accounts)

    # Stamp management flags; keep per-row observed/valued dates intact.
    policy = load_policy_symbols(session, user_id)
    stamped = stamp_management_flags(reconstructed, policy_symbols=policy)

    snap_dates = [
        _as_date(p.get("observed_as_of")) for p in stamped
        if _as_date(p.get("observed_as_of")) is not None
    ]
    snap_date = max(snap_dates) if snap_dates else date.today()

    cash_balances = 0.0
    for p in stamped:
        at = str(p.get("asset_type") or "").lower()
        if "cash" in at:
            cash_balances += float(p.get("usd_value_k") or 0.0)

    from argosy.state.models import PortfolioSnapshotRow

    # Preserve non-position payload from latest when available.
    alloc = "[]"
    nvda = "[]"
    re_json = "[]"
    pensions = "[]"
    fx_nis = None
    fx_eur = None
    if latest is not None:
        alloc = latest.allocations_json or "[]"
        nvda = latest.nvda_sales_json or "[]"
        re_json = latest.real_estate_json or "[]"
        pensions = latest.pensions_json or "[]"
        fx_nis = latest.fx_usd_nis
        fx_eur = latest.fx_usd_eur

    row = PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=snap_date,
        imported_at=datetime.now(timezone.utc),
        source_path="backfill:last_coverage_restore",
        positions_json=json.dumps(stamped, default=str),
        allocations_json=alloc,
        nvda_sales_json=nvda,
        real_estate_json=re_json,
        pensions_json=pensions,
        totals_json=json.dumps({
            "total_usd_value_k": total,
            "cash_balances_usd_k": cash_balances,
            "accounts_covered": accounts,
            "accounts_carried": accounts_carried,
            "feed_position_count": n,
            "merged_position_count": n,
            "restored_by": actor,
            "restore_kind": "last_coverage",
        }),
        fx_usd_nis=fx_nis,
        fx_usd_eur=fx_eur,
        parse_warnings_json=json.dumps([
            f"BOOK_RESTORE actor={actor} positions={n} "
            f"total_usd_k={total:.1f} accounts={','.join(accounts)} "
            f"carried={','.join(accounts_carried) or 'none'}"
        ]),
    )
    session.add(row)
    # Sync durable unmanaged rows from restored positions, preserving
    # each row's own observed_as_of (never re-date carried quantities).
    sync_result = sync_unmanaged_from_positions(
        session, user_id, stamped, commit=False, valued_as_of=snap_date,
    )
    if sync_result.get("errors", 0) > 0:
        session.rollback()
        raise RuntimeError(
            "unmanaged_holdings sync failed during restore "
            f"(errors={sync_result.get('errors')}; detail={sync_result}). "
            "Refusing to commit — policy holdings must not return as managed."
        )

    # Self-verify before commit.
    written = json.loads(row.positions_json or "[]")
    written_n = len(written)
    written_total = sum(float(p.get("usd_value_k") or 0.0) for p in written)
    if expected_position_count is not None and written_n != expected_position_count:
        session.rollback()
        raise AssertionError(
            f"restore write verification failed: {written_n} positions, "
            f"expected {expected_position_count}"
        )
    if (
        expected_usd_k is not None
        and abs(written_total - expected_usd_k) > expected_usd_k_tol
    ):
        session.rollback()
        raise AssertionError(
            f"restore write verification failed: ${written_total:.1f}k, "
            f"expected ${expected_usd_k:.1f}k"
        )

    if commit:
        session.commit()
        session.refresh(row)
    else:
        session.flush()

    return {
        "status": "restored",
        "position_count": written_n,
        "total_usd_k": round(written_total, 1),
        "accounts": accounts,
        "accounts_covered": accounts,
        "accounts_carried": accounts_carried,
        "snapshot_id": getattr(row, "id", None),
        "snapshot_date": str(snap_date),
    }


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
    accounts_covered: Sequence[str] | None = None,
) -> None:
    """Raise ``SnapshotIngestRejected`` when the write would be destructive.

    Stale-date rule: an incoming feed whose ``snapshot_date`` precedes the
    current book's date must NOT become current. An older observation cannot
    supersede a newer one — quarantine/override is the only path
    (``allow_stale``). Same-date re-imports are allowed.

    Catastrophic-drop rule is scoped to accounts the feed **covers**. A
    Leumi-only feed that omits Schwab is incomplete coverage, not a
    catastrophic wipe of Schwab — per-account merge carries those holdings.
    """
    if latest_row is None:
        return
    if not allow_stale:
        old_date = _as_date(getattr(latest_row, "snapshot_date", None))
        new_date = _as_date(new_snapshot_date)
        # A missing date is not a fresh date. An undated feed must not
        # supersede a dated book (and undated marks never read as current).
        if old_date is not None and new_date is None:
            raise SnapshotIngestRejected(
                "undated_snapshot",
                f"incoming snapshot has no snapshot_date while latest is "
                f"{old_date}; refusing to let an undated feed become current "
                f"(pass allow_stale with a reason to override)",
            )
        if old_date is not None and new_date is not None:
            if new_date < old_date:
                raise SnapshotIngestRejected(
                    "stale_snapshot_date",
                    f"incoming snapshot_date {new_date} precedes "
                    f"latest {old_date}; refusing to let an older feed "
                    f"become current (pass allow_stale with a reason to override)",
                )
    if allow_catastrophic_drop:
        return
    try:
        old_positions = json.loads(getattr(latest_row, "positions_json", None) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        old_positions = []

    covered = {
        location_account_key(a) for a in (accounts_covered or [])
        if location_account_key(a)
    }
    if not covered:
        covered = set(accounts_covered_from_positions(new_positions))

    if covered:
        old_in_covered = [
            p for p in old_positions
            if location_account_key(_location_of(p)) in covered
        ]
        new_in_covered = [
            p for p in (new_positions or [])
            if location_account_key(_location_of(p)) in covered
        ]
    else:
        old_in_covered = old_positions
        new_in_covered = list(new_positions or [])

    old_n = _named_position_count(old_in_covered)
    new_n = _named_position_count(new_in_covered)
    if old_n >= _CATASTROPHIC_MIN_OLD_POSITIONS and new_n < old_n * _CATASTROPHIC_FRACTION:
        raise SnapshotIngestRejected(
            "catastrophic_position_drop",
            f"named positions in covered accounts {sorted(covered) or ['(all)']} "
            f"{old_n} → {new_n} "
            f"(below {_CATASTROPHIC_FRACTION:.0%} retention)",
        )
    old_v = _securities_usd_k(old_in_covered)
    new_v = _securities_usd_k(new_in_covered)
    if old_v >= _CATASTROPHIC_MIN_OLD_USD_K and new_v < old_v * _CATASTROPHIC_FRACTION:
        raise SnapshotIngestRejected(
            "catastrophic_value_drop",
            f"securities usd_k in covered accounts {sorted(covered) or ['(all)']} "
            f"{old_v:.1f} → {new_v:.1f} "
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
    "EXPECTED_RESTORED_POSITION_COUNT",
    "EXPECTED_RESTORED_USD_K",
    "EXPECTED_RESTORED_USD_K_TOL",
    "KNOWN_SYMBOL_RENAMES",
    "MARK_STALE_DAYS",
    "STATUS_ACTIVE",
    "STATUS_RETIRED",
    "QUANTITY_STALE_DAYS",
    "STALE_VALUATION_DAYS",
    "VALUATION_CLOCK_POLICY",
    "AccountMergeResult",
    "SnapshotIngestRejected",
    "TotalBookDegraded",
    "TotalBookResult",
    "UnmanagedLoadResult",
    "accounts_carried_provenance",
    "accounts_covered_from_positions",
    "assess_snapshot_ingest",
    "assess_total_book_integrity",
    "backfill_restored_holdings_book",
    "backfill_unmanaged_from_snapshots",
    "books_consistency_check",
    "books_consistency_check_positions",
    "books_match_for_restore",
    "dedupe_positions_by_symbol_location",
    "derive_accounts_carried_from_dates",
    "ensure_default_unmanaged_policy",
    "has_symbol",
    "implied_nvda_weight_frac",
    "investable_usd_k",
    "is_managed_position",
    "load_explicit_policy_symbols",
    "load_policy_symbols",
    "load_total_and_managed_books",
    "load_total_book",
    "load_unmanaged_holding_rows",
    "position_feed_fingerprint",
    "location_account_key",
    "managed_positions",
    "mark_is_stale",
    "merge_positions_per_account",
    "merge_total_book_positions",
    "normalize_symbol_identity",
    "parse_explicit_managed_flag",
    "parse_positions_json",
    "position_usd_value_k",
    "positions_for_books",
    "quantity_is_stale",
    "resolve_prior_positions_by_account_coverage",
    "retire_unmanaged_account",
    "stamp_management_flags",
    "stamp_mark_dates",
    "symbol_value_usd_k",
    "sync_unmanaged_from_positions",
    "total_positions",
    "tradeable_securities_nis_for_user",
    "unmanaged_row_to_position",
    "valuation_is_stale",
]
