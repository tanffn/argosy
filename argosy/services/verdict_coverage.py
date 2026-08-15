"""All-holdings verdict-COVERAGE layer.

Gap this closes: the daily ``thesis_monitor`` reasons on INDIVIDUAL STOCKS only
(``default_individual_holdings`` skips every row whose
``instrument_reference`` structure is not ``Stock`` — ETFs / funds / bonds /
REITs are exempt). So most of the book carries NO ongoing per-symbol verdict:
the deconcentration ETFs, the bond sleeve, the REITs, and the durable unmanaged
NVDA can all sit with no standing verdict and nobody notices.

This module makes that gap VISIBLE and closes it, mirroring the pure-seam style
of ``verdict_triggers``:

  1. :func:`holdings_coverage_report` — a CHEAP, deterministic, READ-ONLY sweep.
     It enumerates EVERY held symbol from the CONSERVED current book
     (``current_book.load_current_book().total`` — ETFs, funds, bonds, single
     stocks, AND the durable unmanaged NVDA; it excludes only cash and physical
     real-estate rows), classifies each symbol's coverage against the settled
     verdict registry (``COVERED`` / ``STALE`` / ``UNCOVERED`` by verdict age),
     and returns a per-symbol record + totals. THIS REPORT is the key
     deliverable — "which holdings lack a verdict" is now inspectable so nothing
     silently falls through.

  2. :func:`ensure_coverage` — for holdings that are ``UNCOVERED`` or ``STALE``,
     ESCALATE a fleet re-verdict via the SAME seam thesis_monitor /
     verdict_triggers use (``run_deep_decision``). Best-effort per symbol,
     cost-aware (a ``limit`` cap per run so one sweep never fires the fleet on
     dozens of symbols — the most-overdue are processed first, the rest next
     run), idempotent within a run. It does NOT change what the fleet decides.

ETFs / funds ARE included: the fleet CAN return a hold/trim/sell verdict for an
ETF holding on fund-fit / concentration / vehicle grounds. Where the FULL A2
vehicle-selection judgment (fee / tracking-error comparison) is required and its
metadata is data-blocked, that limitation is recorded HONESTLY on the report
item and threaded into the escalation reason — we never fabricate a
fee-equivalence verdict.

No schema change: this reads existing ``verdicts`` + the current book only
(alembic head stays 0100). Read-only report; the escalation is the only
side effect and it is delegated to the injected ``decide_fn``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.current_book import CurrentBook, load_current_book
from argosy.services.verdict_registry import get_settled_verdict

log = get_logger("argosy.services.verdict_coverage")

CoverageStatus = Literal["covered", "stale", "uncovered"]

# Default freshness horizon: a settled verdict older than this is STALE.
DEFAULT_MAX_AGE_DAYS = 90
# Default per-sweep fleet escalation cap (cost guard).
DEFAULT_LIMIT = 3

# Structures whose FULL A2 vehicle-selection judgment (fee / tracking-error /
# domicile-equivalence) needs vehicle metadata that is currently data-blocked.
# A verdict on these is a fund-fit / concentration / vehicle judgment only — we
# record that limitation honestly rather than fabricate a fee-equivalence call.
_VEHICLE_STRUCTURES = frozenset({"etf", "fund", "reit", "bond"})

# Persistent per-symbol coverage-check COOLDOWN marker (reuses the action-ledger
# dedup mechanism — no schema change, alembic head stays 0100). A ``checked``
# marker (fleet re-verdict completed OR the thesis was re-affirmed/defended) is
# a legitimate "covered again" for ``max_age_days``; a ``retry`` marker (a
# genuine fleet FAILURE — error / quorum_failed / infra-block) suppresses the
# symbol for a SHORT window so a failing name never hogs a capped slot every
# sweep, then retries. ``monitor_flags.kind`` has a closed CHECK enum, so — like
# ``verdict_triggers`` — we write to ``action_proposals`` (kind ``note_only``).
COVERAGE_MARKER_KIND = "note_only"
COVERAGE_CHECK_DEDUP_PREFIX = "holdings_coverage_checked"
# A ``retry`` marker currently shares the ``checked`` dedup prefix (the state is
# carried in the payload), but the ``retry`` prefix is reserved and ALSO excluded
# at the user-facing read sites so the store can split later without leaking.
COVERAGE_RETRY_DEDUP_PREFIX = "holdings_coverage_retry"
# Short retry cooldown (days) for a genuine fleet failure — long enough that a
# failing name yields its slot to other uncovered names next sweep, short enough
# that it is retried well before the full staleness horizon.
RETRY_COOLDOWN_DAYS = 2

# DEFECT A — the cooldown store is INTERNAL bookkeeping and must NEVER surface to
# the user. These markers are written as ``action_proposals`` rows (reusing the
# dedup mechanism, no schema change), but the proposal list, the inbox, and the
# email digest all read every ``status='open'`` row with no expiry/prefix filter,
# so a raw marker would become user-visible inbox/digest chatter (and crowd out
# real actions even after cooldown expiry). Both user-facing read sites
# (``action_proposals.list_open_action_proposals`` and
# ``email_digest``) filter out any dedup key with one of these prefixes via
# :func:`is_coverage_marker_dedup_key`; the cooldown logic here still queries the
# same rows by ``dedup_key`` + ``expires_at``, so lookups keep working.
COVERAGE_MARKER_DEDUP_PREFIXES: tuple[str, ...] = (
    COVERAGE_CHECK_DEDUP_PREFIX,
    COVERAGE_RETRY_DEDUP_PREFIX,
)


def is_coverage_marker_dedup_key(dedup_key: str | None) -> bool:
    """True for an internal holdings-coverage cooldown marker dedup key.

    The user-facing proposal/inbox/digest read sites use this to EXCLUDE the
    coverage bookkeeping markers so they never surface to the user."""
    key = dedup_key or ""
    return any(key.startswith(f"{p}:") for p in COVERAGE_MARKER_DEDUP_PREFIXES)


def coverage_marker_sql_exclusion(dedup_key_column: Any) -> Any:
    """A SQLAlchemy boolean condition that EXCLUDES coverage-marker rows while
    KEEPING rows with a NULL dedup_key (genuine proposals often have none).

    Pushed into the email-digest queries so the internal markers never consume a
    ``LIMIT`` slot and never inflate the open-proposal count / has_any_activity
    (DEFECT A). ``NULL NOT LIKE`` is NULL (would wrongly drop NULL-dedup rows), so
    NULL is admitted explicitly.

    The prefixes contain ``_``, which is a single-char WILDCARD in SQL LIKE — an
    unescaped ``holdings_coverage_checked:%`` would also exclude legit keys like
    ``holdingsXcoverageYchecked:...``. So we escape ``_`` / ``%`` / ``\`` in the
    literal and pass ``escape='\\'``; only the trailing ``:%`` stays a wildcard."""
    from sqlalchemy import or_

    def _lit(p: str) -> str:
        # Escape LIKE metacharacters in the literal prefix; ':' + trailing '%'
        # remain the intended match.
        return p.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")

    like_any_marker = or_(
        *[
            dedup_key_column.like(f"{_lit(p)}:%", escape="\\")
            for p in COVERAGE_MARKER_DEDUP_PREFIXES
        ]
    )
    return or_(dedup_key_column.is_(None), ~like_any_marker)


# DEFECT B — coverage completeness is decided by GROUND TRUTH, not the outcome
# status envelope. The envelope does NOT prove a verdict was persisted:
# ``_record_settled_verdict`` (flow.py:847) SWALLOWS every registry-write failure
# yet still lets ``approved`` / ``trader_hold`` return — so a status-allowlist
# would write a ``checked`` marker and false-cover a holding for the full
# ``max_age_days`` with ZERO settled verdicts. Conversely ``us_situs_floor``
# WRITES a settled actionable verdict (flow.py:805) BEFORE the deterministic floor
# blocks the proposal (deep_decision.py:378) and never retracts it — a status rule
# would wrongly retry a holding that DOES now have a fresh verdict.
#
# So "did this attempt complete a re-verdict?" is answered by the registry itself,
# keyed on the settled row's IDENTITY (id + updated_at), captured BEFORE decide_fn:
#   covered/checked IFF
#     (a) blocked_by == "verdict_defended" — the re-affirm path CONFIRMS the
#         existing standing verdict and writes NO new row, so it stays a status
#         special-case (deep_decision.py:98-110); OR
#     (b) a settled verdict row now exists AND it is NEW-OR-CHANGED vs the
#         pre-capture:
#           * ``pre is None`` — none before, one now; OR
#           * ``current.id != pre_id`` — the supersede/insert path (write_verdict
#             clears the prior settled row and inserts a NEW higher id,
#             verdict_registry.py:196-220); OR
#           * ``current.updated_at > pre_updated_at`` — the IN-PLACE refresh path
#             (write_verdict UPDATES the existing same-(subject,run) row in place,
#             keeping the SAME id and only bumping ``updated_at`` at
#             verdict_registry.py:167-194). Keying on id alone would MISS this →
#             a genuine re-verdict would wrongly retry forever.
#   else → retry (short cooldown), regardless of the status envelope.
#
# This collapses the fragile blocked_by taxonomy into one truth AND catches both
# write paths: trader_hold/approved with a good write → new-or-changed row →
# covered; trader_hold whose write FAILED (swallowed) → row byte-identical (same
# id, same updated_at) → NOT complete → retry (no 90-day false-cover);
# us_situs_floor → wrote a row → covered (self-corrects); every no-verdict block /
# infra error / quorum → unchanged row → retry.
#
# Failure direction is SAFE: if the fleet's committed row is somehow not yet
# visible, the holding is marked retry (a redundant re-fire next sweep on a FRESH
# session that WILL see it), never false-covered — so it converges, never loops.


def _is_verdict_defended(outcome: Any) -> bool:
    """True only for the explicit re-affirm envelope (writes no new row)."""
    if isinstance(outcome, dict):
        blocked_by = outcome.get("blocked_by")
    else:
        blocked_by = getattr(outcome, "blocked_by", None)
    return str(blocked_by or "").strip().lower() == "verdict_defended"


def _settled_verdict_identity(
    session: Session, *, user_id: str, subject: str
) -> tuple[int | None, datetime | None]:
    """Identity ``(id, updated_at)`` of the subject's CURRENT settled verdict, or
    ``(None, None)`` if none / on read failure. Captured before AND after decide_fn
    to detect a re-verdict via EITHER a new id (insert/supersede) or a bumped
    updated_at (in-place same-run refresh)."""
    try:
        v = get_settled_verdict(session, user_id=user_id, subject=subject)
    except Exception as exc:  # noqa: BLE001 — best-effort identity read
        log.warning("verdict_coverage.settled_identity_read_failed", subject=subject, error=str(exc)[:160])
        return (None, None)
    if v is None:
        return (None, None)
    vid = int(v.id) if v.id is not None else None
    return (vid, getattr(v, "updated_at", None))


def _updated_after(current: datetime | None, prior: datetime | None) -> bool:
    """``current > prior`` tolerant of tz-naive/aware mismatch (SQLite stores
    naive). Missing values → False."""
    if current is None or prior is None:
        return False
    try:
        return current > prior
    except TypeError:
        try:
            return current.replace(tzinfo=None) > prior.replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            return False


def _coverage_attempt_completed(
    session: Session,
    *,
    user_id: str,
    subject: str,
    pre_id: int | None,
    pre_updated_at: datetime | None,
    outcome: Any,
) -> bool:
    """GROUND-TRUTH completeness keyed on settled-verdict IDENTITY (see the DEFECT
    B note above).

    True IFF (a) the outcome is a ``verdict_defended`` re-affirm, OR (b) the
    subject's settled verdict is NEW-OR-CHANGED vs the ``(pre_id, pre_updated_at)``
    captured before decide_fn — a fresh id (insert/supersede) OR a bumped
    updated_at (in-place same-run refresh). Ends the coverage read transaction
    first (``commit``) so the SELECT observes the row the fleet committed on its
    OWN connection before ``asyncio.run`` returned. Any read/commit failure →
    False (retry), never a false covered."""
    if _is_verdict_defended(outcome):
        return True

    # End the current read txn so a separate connection's committed write is
    # visible (SQLite/most engines only refresh the snapshot on a new txn).
    try:
        session.commit()
    except Exception:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False

    cur_id, cur_updated_at = _settled_verdict_identity(session, user_id=user_id, subject=subject)
    if cur_id is None:
        return False  # no settled row now → not complete
    if pre_id is None:
        return True  # none before, one now
    if cur_id != pre_id:
        return True  # supersede/insert path — a fresh row id
    return _updated_after(cur_updated_at, pre_updated_at)  # in-place refresh path


# ---------------------------------------------------------------------------
# Result records (deterministic, JSON-friendly)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HeldSymbol:
    """One enumerated, tradeable held symbol (cash / real-estate excluded)."""

    symbol: str
    structure: str  # stock / etf / reit / bond / cash / unknown
    usd_value_k: float


@dataclass(frozen=True)
class CoverageItem:
    """Per-symbol coverage classification."""

    symbol: str
    structure: str
    coverage_status: CoverageStatus
    verdict_id: int | None
    verdict_age_days: float | None
    usd_value_k: float
    next_validation: str | None = None
    a2_metadata_limited: bool = False
    a2_note: str | None = None
    # Cooldown: a persistent coverage-check marker suppresses re-escalation.
    # ``in_cooldown`` True + status "covered" == re-checked/defended recently;
    # ``in_cooldown`` True + status uncovered/stale == a failed attempt cooling
    # down before retry (reported honestly, never faked to covered).
    in_cooldown: bool = False
    cooldown_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "structure": self.structure,
            "coverage_status": self.coverage_status,
            "verdict_id": self.verdict_id,
            "verdict_age_days": self.verdict_age_days,
            "usd_value_k": self.usd_value_k,
            "next_validation": self.next_validation,
            "a2_metadata_limited": self.a2_metadata_limited,
            "a2_note": self.a2_note,
            "in_cooldown": self.in_cooldown,
            "cooldown_until": self.cooldown_until,
        }


@dataclass(frozen=True)
class CoverageReport:
    """Full all-holdings coverage snapshot. Deterministic + read-only."""

    generated_at: str
    max_age_days: int
    items: list[CoverageItem] = field(default_factory=list)

    @property
    def covered(self) -> list[CoverageItem]:
        return [i for i in self.items if i.coverage_status == "covered"]

    @property
    def stale(self) -> list[CoverageItem]:
        return [i for i in self.items if i.coverage_status == "stale"]

    @property
    def uncovered(self) -> list[CoverageItem]:
        return [i for i in self.items if i.coverage_status == "uncovered"]

    @property
    def totals(self) -> dict[str, int]:
        return {
            "held": len(self.items),
            "covered": len(self.covered),
            "stale": len(self.stale),
            "uncovered": len(self.uncovered),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "max_age_days": self.max_age_days,
            "totals": self.totals,
            "items": [i.to_dict() for i in self.items],
        }


# ---------------------------------------------------------------------------
# Enumeration + classification (deterministic, side-effect free)
# ---------------------------------------------------------------------------
def _structure_for(symbol: str, details: str) -> str:
    """Lower-cased instrument STRUCTURE (stock/etf/reit/bond/cash) or
    ``"unknown"`` when the symbol isn't in the curated reference."""
    try:
        from argosy.services import instrument_reference as iref

        ref = iref.lookup(symbol, details)
    except Exception as exc:  # noqa: BLE001 — a lookup miss is UNKNOWN, never fatal
        log.warning("verdict_coverage.structure_lookup_failed", symbol=symbol, error=str(exc)[:160])
        return "unknown"
    if ref is None:
        return "unknown"
    return str(getattr(ref, "structure", "") or "unknown").strip().lower() or "unknown"


def _is_excluded_row(symbol: str) -> bool:
    """True ONLY for a SYMBOL-LESS row (blank or the "-" sentinel) — physical
    real estate, physical/deployable cash, and untickered rows are all
    symbol-less, and there is nothing to record a per-symbol verdict on.

    Crucially we do NOT exclude by raw ``asset_type``: a row with a REAL ticker
    is a listed, priceable SECURITY and MUST be included regardless of its
    free-text asset_type. On the live book, ``O`` and ``IWDP`` carry
    asset_type "Real Estate" (a listed REIT and a listed property ETF), and a
    bond ETF (IBTA) has historically carried asset_type "Cash" — raw asset_type
    is unreliable (see snapshot_refresh.py: "Real Estate" != physical property).
    Structure is resolved from ``instrument_reference`` instead."""
    return not symbol or symbol == "-"


def enumerate_held_symbols(total_rows: list[dict[str, Any]]) -> list[HeldSymbol]:
    """Enumerate EVERY tradeable held symbol from the conserved book's ``total``
    rows — ETFs, funds, bonds, REITs (incl. listed property ETFs / REITs like
    IWDP / O that carry a raw "Real Estate" asset_type), single stocks, and the
    durable unmanaged NVDA. Only SYMBOL-LESS rows (physical real estate /
    physical cash) are excluded. Same symbol summed.

    Deterministic + read-only. Rows with zero/blank value still count (a held
    position with a missing mark must be VISIBLE as uncovered, not dropped)."""
    agg: dict[str, HeldSymbol] = {}
    for row in total_rows or []:
        symbol = str(row.get("symbol") or "").strip().upper()
        if _is_excluded_row(symbol):
            continue
        details = str(row.get("details") or "")
        structure = _structure_for(symbol, details)
        try:
            usd_k = float(row.get("usd_value_k") or 0.0)
        except (TypeError, ValueError):
            usd_k = 0.0
        prior = agg.get(symbol)
        if prior is None:
            agg[symbol] = HeldSymbol(symbol=symbol, structure=structure, usd_value_k=round(usd_k, 4))
        else:
            agg[symbol] = HeldSymbol(
                symbol=symbol,
                structure=prior.structure if prior.structure != "unknown" else structure,
                usd_value_k=round(prior.usd_value_k + usd_k, 4),
            )
    # Largest holding first — the coverage gap on a big position matters most.
    return sorted(agg.values(), key=lambda h: (-h.usd_value_k, h.symbol))


def _verdict_age_days(verdict: Any, *, now: datetime) -> float:
    """Age of the settled verdict in days (from ``updated_at``, falling back to
    ``created_at``). Naive stored datetimes are treated as UTC."""
    ref_dt = getattr(verdict, "updated_at", None) or getattr(verdict, "created_at", None)
    if ref_dt is None:
        return math.inf
    if ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=timezone.utc)
    return max(0.0, round((now - ref_dt).total_seconds() / 86400.0, 2))


def _a2_note_for(symbol: str, structure: str) -> tuple[bool, str | None]:
    """Honest A2-limitation record for a pooled vehicle. Returns
    ``(limited, note)`` — a note is attached ONLY for vehicle structures whose
    full fee/tracking-error selection metadata is data-blocked; never for a
    single stock. This does NOT fabricate a verdict — it records that any
    verdict on the vehicle is fund-fit / concentration only."""
    if structure not in _VEHICLE_STRUCTURES:
        return (False, None)
    note = (
        f"A2 vehicle-selection metadata (fee / tracking-error / domicile "
        f"equivalence) is data-blocked for {symbol} ({structure}); a fleet "
        f"verdict here is a fund-fit / concentration / vehicle judgment only, "
        f"NOT a fabricated fee-equivalence verdict."
    )
    return (True, note)


@dataclass(frozen=True)
class _Cooldown:
    state: str  # "checked" | "retry"
    until: datetime


def _cooldown_dedup_key(symbol: str) -> str:
    return f"{COVERAGE_CHECK_DEDUP_PREFIX}:{symbol.upper()}"


def _load_cooldowns(
    session: Session, user_id: str, *, now: datetime
) -> dict[str, _Cooldown]:
    """Load ACTIVE (unexpired, open) per-symbol coverage-check markers.

    A ``checked`` marker means the fleet completed / re-affirmed a verdict; a
    ``retry`` marker means a genuine failure is cooling down. Best-effort — a
    load failure returns ``{}`` and the sweep degrades to verdict-age only."""
    import json

    out: dict[str, _Cooldown] = {}
    try:
        from sqlalchemy import select

        from argosy.state.models import ActionProposal

        rows = session.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.status == "open",
                ActionProposal.dedup_key.like(f"{COVERAGE_CHECK_DEDUP_PREFIX}:%"),
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001 — cooldown input is additive
        log.warning("verdict_coverage.cooldown_load_failed", error=str(exc)[:160])
        return out
    for r in rows:
        key = r.dedup_key or ""
        parts = key.split(":", 1)
        if len(parts) != 2 or not parts[1]:
            continue
        symbol = parts[1].strip().upper()
        until = getattr(r, "expires_at", None)
        if until is None:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if until <= now:
            continue  # expired — no longer suppresses
        try:
            payload = json.loads(r.suggested_payload or "{}")
        except (TypeError, ValueError):
            payload = {}
        state = str(payload.get("state") or "checked")
        prior = out.get(symbol)
        # Keep the LONGER-lived marker if two somehow coexist.
        if prior is None or until > prior.until:
            out[symbol] = _Cooldown(state=state, until=until)
    return out


def holdings_coverage_report(
    session: Session,
    user_id: str,
    *,
    now: datetime,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    book_loader: Callable[..., CurrentBook] = load_current_book,
) -> CoverageReport:
    """READ-ONLY, deterministic all-holdings coverage report.

    Enumerates every held symbol from the conserved current book (incl. ETFs /
    funds / bonds / REITs and the durable unmanaged NVDA; excludes cash + real
    estate) and classifies each against its settled verdict:

      * ``COVERED``   — a standing verdict fresh within ``max_age_days``.
      * ``STALE``     — a standing verdict older than ``max_age_days``.
      * ``UNCOVERED`` — no standing verdict at all.

    Never writes; it only reads the book and the verdict registry.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    book = book_loader(session, user_id)
    held = enumerate_held_symbols(getattr(book, "total", []) or [])
    cooldowns = _load_cooldowns(session, user_id, now=now)

    items: list[CoverageItem] = []
    for h in held:
        verdict = get_settled_verdict(session, user_id=user_id, subject=h.symbol)
        a2_limited, a2_note = _a2_note_for(h.symbol, h.structure)
        cd = cooldowns.get(h.symbol)
        in_cd = cd is not None
        cd_until = cd.until.isoformat() if cd is not None else None

        if verdict is None:
            verdict_id: int | None = None
            age_days: float | None = None
            nv_iso: str | None = None
            base_status: CoverageStatus = "uncovered"
        else:
            age = _verdict_age_days(verdict, now=now)
            base_status = "stale" if age > max_age_days else "covered"
            verdict_id = int(verdict.id) if verdict.id is not None else None
            age_days = None if age == math.inf else age
            nv = getattr(verdict, "next_validation", None)
            nv_iso = nv.isoformat() if nv is not None else None

        # A ``checked`` cooldown means the fleet completed / re-affirmed the
        # thesis recently — a legitimate "covered again" (defended-stale
        # converges instead of re-firing). A ``retry`` cooldown does NOT fake
        # coverage — the true uncovered/stale status is kept and reported; it
        # only suppresses re-escalation for the short retry window.
        status = base_status
        if cd is not None and cd.state == "checked" and base_status != "uncovered":
            status = "covered"
        elif cd is not None and cd.state == "checked" and base_status == "uncovered":
            # Completing escalation on a formerly-uncovered name normally leaves
            # a fresh verdict (→ covered above); if the verdict isn't visible yet
            # the marker still records the recent successful check.
            status = "covered"

        items.append(
            CoverageItem(
                symbol=h.symbol,
                structure=h.structure,
                coverage_status=status,
                verdict_id=verdict_id,
                verdict_age_days=age_days,
                usd_value_k=h.usd_value_k,
                next_validation=nv_iso,
                a2_metadata_limited=a2_limited,
                a2_note=a2_note,
                in_cooldown=in_cd,
                cooldown_until=cd_until,
            )
        )
    return CoverageReport(
        generated_at=now.isoformat(),
        max_age_days=max_age_days,
        items=items,
    )


# ---------------------------------------------------------------------------
# Escalation — fire a fleet re-verdict for UNCOVERED / STALE holdings.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageEscalation:
    """Record of one escalation ``ensure_coverage`` performed (or skipped)."""

    symbol: str
    coverage_status: CoverageStatus
    reason: str
    escalated: bool           # completing (fleet re-verdict) OR defended (re-affirmed)
    failed: bool = False      # structured non-completing outcome / exception
    outcome: Any = None
    error: str | None = None


def _write_cooldown_marker(
    session: Session,
    user_id: str,
    symbol: str,
    *,
    state: str,
    now: datetime,
    until: datetime,
    reason: str,
) -> None:
    """Upsert a per-symbol coverage-check cooldown marker (reuses the
    ``action_proposals`` dedup mechanism — no schema change). ``state`` is
    ``checked`` (completing/defended → covered for ``max_age_days``) or
    ``retry`` (failure → short suppression). Best-effort; a write failure never
    aborts the sweep. Flushes (the caller owns the commit)."""
    import json

    from sqlalchemy import select

    from argosy.state.models import ActionProposal

    dedup = _cooldown_dedup_key(symbol)
    payload = json.dumps(
        {"symbol": symbol.upper(), "state": state, "kind": "holdings_coverage_checked",
         "source": "holdings_coverage_sweep", "reason": reason[:400]},
        ensure_ascii=False, default=str,
    )
    try:
        existing = session.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.dedup_key == dedup,
                ActionProposal.status == "open",
            ).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.suggested_payload = payload
            existing.surfaced_at = now
            existing.expires_at = until
            existing.summary = f"coverage {state}: {symbol.upper()}"
            session.flush()
            return
        row = ActionProposal(
            user_id=user_id,
            summary=f"coverage {state}: {symbol.upper()}",
            rationale_md=(
                f"All-holdings coverage sweep {state} marker for {symbol.upper()} "
                f"— suppresses re-escalation until {until.isoformat()}.\n\n{reason}"
            ),
            suggested_payload=payload,
            severity="info",
            surfaced_at=now,
            expires_at=until,
            status="open",
            kind=COVERAGE_MARKER_KIND,
            dedup_key=dedup,
            execution_state="proposed",
        )
        session.add(row)
        session.flush()
    except Exception as exc:  # noqa: BLE001 — marker write must never abort the sweep
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            "verdict_coverage.cooldown_write_failed",
            symbol=symbol, state=state, error=str(exc)[:200],
        )


def _is_collective_instrument(symbol: str, structure: str) -> bool:
    """True when the instrument is a collective vehicle (ETF / fund / REIT /
    bond) that should be routed to the fund-vehicle verdict path instead of
    the per-stock analyst fleet.

    The fund-vehicle path asks the RIGHT questions: domicile, TER, mandate
    fit, NVDA look-through, sleeve overlap.  The equity fleet (fundamentals /
    news / sentiment) asks equity questions (PE, moat, earnings) that are
    undefined for a passive index fund.

    Uses the structure from ``instrument_reference`` (supplied by the
    CoverageItem that identified this as a vehicle structure in
    ``_a2_note_for``). Falls back to the ``_VEHICLE_STRUCTURES`` set so
    the unknown-structure path is safe (unknown → equity fleet, not the
    fund path).
    """
    return str(structure or "").strip().lower() in _VEHICLE_STRUCTURES


def _default_decide_fn(
    *, user_id: str, subject: str, cited_new_facts: list[str], reason: str
) -> Any:
    """Default escalation seam — dispatches to the CORRECT fleet for the
    instrument's structure:

    * COLLECTIVE (ETF / fund / REIT / bond per ``instrument_reference``) →
      ``run_fund_vehicle_decision`` which runs ONE Opus fund-vehicle analyst
      and writes a structured verdict addressing domicile, TER, mandate fit,
      NVDA look-through, and overlap.

    * INDIVIDUAL (Stock, or unknown-structure fallback) → the full per-stock
      analyst fleet (``run_deep_decision``) exactly as before, threading the
      coverage reason as the cited new fact.

    Async → driven with ``asyncio.run`` inside the (already off-thread) sweep
    worker. Never changes what the fleet decides.

    The structure is resolved from ``instrument_reference`` at dispatch time
    so the callable signature stays compatible with all existing ``decide_fn``
    injection sites (tests, thesis_monitor, verdict_trigger sweep).
    """
    import asyncio

    # Resolve structure for routing — best-effort; unknown falls through to
    # the equity fleet (safe default, same as before this dispatch was added).
    structure = _structure_for(subject, "")

    funnel_meta = {
        "source": "holdings_coverage_sweep",
        "cited_new_facts": cited_new_facts,
        "revisit_reason": reason,
    }

    if _is_collective_instrument(subject, structure):
        from argosy.services.decision_funnel.fund_vehicle_decision import (
            run_fund_vehicle_decision,
        )
        log.info(
            "verdict_coverage.dispatching_fund_vehicle",
            symbol=subject, structure=structure,
        )
        return asyncio.run(
            run_fund_vehicle_decision(
                user_id=user_id,
                ticker=subject,
                funnel_meta=funnel_meta,
            )
        )

    from argosy.decisions.tiers import Tier
    from argosy.services.decision_funnel.deep_decision import run_deep_decision

    return asyncio.run(
        run_deep_decision(
            user_id=user_id,
            ticker=subject,
            tier=Tier.T2,
            consult_mode="long_hold",
            funnel_meta=funnel_meta,
            subject_type="holding",
        )
    )


def _overdue_key(item: CoverageItem) -> tuple[int, float]:
    """Sort key: most-overdue first. UNCOVERED (no verdict) is the most overdue
    (rank 0, treated as infinite age); STALE ranks after, oldest first."""
    if item.coverage_status == "uncovered":
        return (0, -math.inf)
    return (1, -(item.verdict_age_days or 0.0))


def ensure_coverage(
    session: Session,
    user_id: str,
    *,
    now: datetime,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    limit: int = DEFAULT_LIMIT,
    decide_fn: Callable[..., Any] | None = None,
    book_loader: Callable[..., CurrentBook] = load_current_book,
    report: CoverageReport | None = None,
) -> dict[str, Any]:
    """Escalate a fleet re-verdict for UNCOVERED / STALE holdings, capped by
    ``limit`` (cost guard), most-overdue first.

    Best-effort per symbol — one symbol's fleet failure is captured on its
    :class:`CoverageEscalation` and never aborts the sweep. Idempotent within a
    run (each symbol is fired at most once) AND across runs via a persistent
    cooldown marker. A fully-covered book is a no-op. ``decide_fn`` (defaults to
    :func:`_default_decide_fn` → ``run_deep_decision``) is called with
    ``user_id`` / ``subject`` / ``cited_new_facts`` / ``reason``. Does NOT change
    what the fleet decides — it only INSPECTS the outcome:

      * a COMPLETED attempt — a ``verdict_defended`` re-affirm OR a FRESH settled
        verdict row now exists (GROUND TRUTH ``_coverage_attempt_completed``, NOT
        the status envelope) — writes a ``checked`` cooldown for ``max_age_days``
        → the symbol is "covered again", so a defended-stale name CONVERGES
        instead of re-firing every sweep and the cap rotates through the book;
      * anything else — no fresh verdict persisted (a swallowed write failure,
        error / quorum_failed / infra-block / any no-verdict block) or an
        exception — is a genuine FAILURE: reported ``escalated=False, failed=True``
        (never faked to covered) and given only a SHORT ``retry`` cooldown so it
        yields its capped slot to other uncovered names, then retries.
    """
    decide = decide_fn or _default_decide_fn
    rep = report or holdings_coverage_report(
        session, user_id, now=now, max_age_days=max_age_days, book_loader=book_loader
    )

    # Candidates: uncovered/stale AND not already suppressed by a live cooldown
    # (a ``checked`` cooldown already reads as covered; this also drops names
    # inside a ``retry`` window so a failing name never hogs a slot every sweep).
    candidates = [
        i for i in rep.items
        if i.coverage_status in ("uncovered", "stale") and not i.in_cooldown
    ]
    candidates.sort(key=_overdue_key)

    checked_until = now + timedelta(days=max_age_days)
    retry_until = now + timedelta(days=RETRY_COOLDOWN_DAYS)

    escalations: list[CoverageEscalation] = []
    seen: set[str] = set()
    fired = 0
    for item in candidates:
        if fired >= limit:
            break
        if item.symbol in seen:  # idempotent within a run
            continue
        seen.add(item.symbol)

        if item.coverage_status == "uncovered":
            reason = (
                f"No standing verdict exists for {item.symbol} "
                f"({item.structure}) — all-holdings coverage gap; fleet "
                f"re-verdict requested to establish a standing verdict."
            )
        else:
            reason = (
                f"Standing verdict #{item.verdict_id} for {item.symbol} is "
                f"{item.verdict_age_days} days old (> {max_age_days}d coverage "
                f"horizon) — re-verification requested."
            )
        cited = [reason]
        if item.a2_metadata_limited and item.a2_note:
            cited.append(item.a2_note)

        fired += 1  # every attempt (success OR failure) consumes a per-run slot
        # Settled-verdict IDENTITY BEFORE decide_fn (DEFECT B ground truth): a
        # genuine re-verdict is detected AFTER the attempt via a new id OR a bumped
        # updated_at. Capture primitives now so no ORM ref survives the commit.
        pre_id, pre_updated_at = _settled_verdict_identity(
            session, user_id=user_id, subject=item.symbol
        )
        # Release the coverage read-txn/locks BEFORE the (long, foreign-writing)
        # fleet call: we must NOT hold a lock across decide_fn, or the fleet's
        # verdict COMMIT on its own connection deadlocks (SQLite single-writer).
        try:
            session.commit()
        except Exception:  # noqa: BLE001
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
        try:
            outcome = decide(
                user_id=user_id,
                subject=item.symbol,
                cited_new_facts=cited,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — one symbol never sinks the sweep
            log.warning(
                "verdict_coverage.escalate_failed",
                symbol=item.symbol, status=item.coverage_status, error=str(exc)[:200],
            )
            # Genuine failure → short retry cooldown (do NOT mark checked).
            _write_cooldown_marker(
                session, user_id, item.symbol, state="retry",
                now=now, until=retry_until, reason=f"exception: {str(exc)[:200]}",
            )
            escalations.append(
                CoverageEscalation(
                    symbol=item.symbol, coverage_status=item.coverage_status,
                    reason=reason, escalated=False, failed=True, error=str(exc)[:200],
                )
            )
            continue

        # GROUND TRUTH (DEFECT B): the attempt COMPLETED a re-verdict iff it was a
        # verdict_defended re-affirm OR the subject's settled verdict is NEW-OR-
        # CHANGED vs the pre-capture (new id from insert/supersede, OR bumped
        # updated_at from an in-place same-run refresh). The status envelope is NOT
        # trusted — a swallowed registry-write returns approved/trader_hold yet
        # leaves the row byte-identical (persists nothing).
        if not _coverage_attempt_completed(
            session, user_id=user_id, subject=item.symbol,
            pre_id=pre_id, pre_updated_at=pre_updated_at, outcome=outcome,
        ):
            status_str = str(
                (outcome or {}).get("status") if isinstance(outcome, dict)
                else getattr(outcome, "status", None)
            )
            log.info(
                "verdict_coverage.escalate_non_completing",
                symbol=item.symbol, status=item.coverage_status, outcome_status=status_str,
            )
            _write_cooldown_marker(
                session, user_id, item.symbol, state="retry",
                now=now, until=retry_until, reason=f"non_completing:{status_str}",
            )
            escalations.append(
                CoverageEscalation(
                    symbol=item.symbol, coverage_status=item.coverage_status,
                    reason=reason, escalated=False, failed=True, outcome=outcome,
                    error=f"non_completing:{status_str}",
                )
            )
            continue

        # Completing (fresh re-verdict) OR defended (re-affirmed) → checked.
        _write_cooldown_marker(
            session, user_id, item.symbol, state="checked",
            now=now, until=checked_until, reason=reason,
        )
        log.info(
            "verdict_coverage.escalated",
            symbol=item.symbol, status=item.coverage_status,
            a2_limited=item.a2_metadata_limited,
        )
        escalations.append(
            CoverageEscalation(
                symbol=item.symbol, coverage_status=item.coverage_status,
                reason=reason, escalated=True, outcome=outcome,
            )
        )

    return {
        "held": rep.totals["held"],
        "covered": rep.totals["covered"],
        "stale": rep.totals["stale"],
        "uncovered": rep.totals["uncovered"],
        "candidates": len(candidates),
        "escalated": sum(1 for e in escalations if e.escalated),
        "failed": sum(1 for e in escalations if e.failed),
        "limit": limit,
        "errors": [f"{e.symbol}: {e.error}" for e in escalations if e.error],
        "escalations": [
            {
                "symbol": e.symbol, "status": e.coverage_status,
                "escalated": e.escalated, "failed": e.failed,
            }
            for e in escalations
        ],
        "report": rep.to_dict(),
    }


__all__ = [
    "CoverageStatus",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_LIMIT",
    "HeldSymbol",
    "CoverageItem",
    "CoverageReport",
    "CoverageEscalation",
    "enumerate_held_symbols",
    "holdings_coverage_report",
    "ensure_coverage",
    "is_coverage_marker_dedup_key",
    "coverage_marker_sql_exclusion",
    "COVERAGE_MARKER_DEDUP_PREFIXES",
]
