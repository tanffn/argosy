"""Closed-loop expectation verifier — deterministic, inviolable-arithmetic floor.

When broker fills are applied to the book
(``argosy.services.snapshot_refresh.apply_fills_to_snapshot``), the resulting
``portfolio_snapshots`` row ARMS closed-loop expectations in its
``parse_warnings``: ``fill-applied:<sym>:<shares>@<price>`` entries,
``cash_overdraft:`` / ``cash_funding_gap:`` entries, caller-supplied
``expectation:`` prose notes, and (for new rows) a machine-readable
``closed_loop_expectations:{json}`` blob. The design intent (SDD §20.4) is
that "the next real ingest verifies the armed expectations" — this module is
the code that actually does that, so verification never depends on a human
remembering.

Three entry points:

* :func:`collect_armed_expectations` — read-only scan. Armed = expectations
  in ``fills-applied:*`` snapshot rows NEWER (by row id) than the newest
  REAL-ingest row. This positional definition is the resolution state: once
  a real ingest lands, the fills rows are older than it and stop being
  armed. No history rewrite, no extra table — resolution outcomes are
  recorded on the NEW ingest row's ``parse_warnings`` (append-only, on the
  new row) and the definition survives re-ingest by construction.
* :func:`verify_and_record_on_ingest` — called from the ingest write-through
  (``portfolio_ingest.xls_osh_pair``) right after a real snapshot row lands.
  Position-quantity expectations within tolerance → RESOLVED (recorded,
  disappears from the client surface); mismatch → a LOUD warning naming
  expected-vs-actual. Estimated prices (the SGOV sale case) verify SHARES
  and note the price difference — price differences NEVER fail (the ingest
  is later than the fill; the market moved).
* :func:`sweep_unverified_expectations` — the daily-loop step: armed
  expectations older than ``STALE_AFTER_DAYS`` with no real ingest since →
  ONE dedup'd needs-info action proposal ("send the current broker export").
  Resolved expectations auto-supersede the proposal (leave the client's
  checklist; stored for audit).

DISCIPLINE: deterministic arithmetic only (quantities, cash, tolerances).
This module never judges whether a trade was GOOD — that is the LLM team's
job; this is the conservation floor.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import ActionProposal, PortfolioSnapshotRow

_log = get_logger("argosy.services.closed_loop")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: parse_warnings prefix for the machine-readable expectations blob written by
#: ``apply_fills_to_snapshot`` (prose entries are kept for humans).
BLOB_PREFIX = "closed_loop_expectations:"

#: Armed expectations older than this with no real ingest → needs-info sweep.
STALE_AFTER_DAYS = 7

#: Share-count tolerance: |actual - expected| <= max(rel * expected, abs).
SHARES_REL_TOL = 0.005
SHARES_ABS_TOL = 0.01

#: Price differences above this (relative) get an informational note. Price
#: NEVER fails a verification — quantities and cash are the arithmetic floor.
PRICE_NOTE_REL = 0.02

_UNVERIFIED_DEDUP = "closed_loop_unverified:{user_id}"
_MISMATCH_DEDUP = "closed_loop_mismatch:{user_id}"

# Snapshot rows Argosy writes itself (never a real broker/TSV ingest).
_SELF_SOURCE_PREFIXES = ("fills-applied:", "self-refresh:")

# ---------------------------------------------------------------------------
# Expectation model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionExpectation:
    """Expected FINAL share count for one (symbol, location, currency).

    ``expected_shares`` is the post-fill book quantity taken from the armed
    snapshot row itself (robust to merges and multi-row chains — the latest
    armed row's book is authoritative). ``None`` means the prose entry could
    not be bound to a position (e.g. an ambiguous symbol) — carried as
    unverifiable, surfaced loudly rather than silently dropped.
    """

    symbol: str
    location: str | None
    currency: str | None
    expected_shares: float | None
    shares_delta: float          # signed: + buy, - sell (from the fill entry)
    price: float | None
    price_estimated: bool
    source_row_id: int
    raw: str


@dataclass(frozen=True)
class CashExpectation:
    """Expected cash reconciliation at (location, currency).

    ``recorded_after_local`` is the post-fill balance the armed row recorded
    (may be negative — a stale-snapshot overdraft). Verification rule: the
    next real ingest must report a NON-NEGATIVE balance at this account;
    the actual-vs-recorded delta is informational (expenses move cash
    between snapshots — equality would be a false gate).
    """

    location: str
    currency: str
    recorded_after_local: float | None
    overdraft: bool
    source_row_id: int
    raw: str


@dataclass(frozen=True)
class ManualExpectation:
    """A free-text ``expectation:`` note — listed, not auto-checkable."""

    text: str
    source_row_id: int


@dataclass
class ArmedExpectations:
    """Everything armed since the last real ingest, deduplicated."""

    positions: list[PositionExpectation] = field(default_factory=list)
    cash: list[CashExpectation] = field(default_factory=list)
    manual: list[ManualExpectation] = field(default_factory=list)
    armed_row_ids: list[int] = field(default_factory=list)
    oldest_armed_at: datetime | None = None
    oldest_armed_date: str | None = None

    @property
    def checkable_count(self) -> int:
        return len(self.positions) + len(self.cash)

    @property
    def empty(self) -> bool:
        return not (self.positions or self.cash or self.manual)


@dataclass
class ClosedLoopVerification:
    """Outcome of one verify-on-ingest run."""

    resolved: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    checked: int = 0

    def reconcile_lines(self) -> list[str]:
        """Lines for the ingest reconcile surface. Mismatches are LOUD."""
        out = [f"CLOSED-LOOP MISMATCH: {m}" for m in self.mismatches]
        out += [f"closed-loop:resolved: {r}" for r in self.resolved]
        out += [f"closed-loop:note: {n}" for n in self.notes]
        out += [f"closed-loop:manual (not auto-checkable): {m}" for m in self.manual]
        return out


# ---------------------------------------------------------------------------
# Parsing — the ACTUAL formats apply_fills_to_snapshot + callers write
# ---------------------------------------------------------------------------

# fill-applied:SPMV:125@114.7
# fill-applied:SELL:SGOV:200@100.45 (price=live-quote estimate; ...)
_FILL_RE = re.compile(
    r"^fill-applied:(?P<side>SELL:)?(?P<sym>[A-Za-z][A-Za-z0-9./-]*)"
    r":(?P<sh>[0-9][0-9,.]*)@(?P<px>[0-9][0-9,.]*)(?P<tail>.*)$"
)

# cash_overdraft:Leumi:USD:-16,434.66 — snapshot cash was stale ...
# cash_funding_gap:Leumi:USD:-16434.66 — CAUSE CONFIRMED ...
_CASH_RE = re.compile(
    r"^cash_(?P<kind>overdraft|funding_gap):(?P<loc>[^:]+):(?P<ccy>[^:]+)"
    r":(?P<bal>-?[0-9][0-9,.]*)"
)

_EXPECTATION_PREFIX = "expectation:"


def _num(s: str) -> float:
    return float(s.replace(",", "").rstrip("."))


@dataclass
class ParsedWarnings:
    """Raw parse of one row's parse_warnings (before position binding)."""

    fills: list[dict[str, Any]] = field(default_factory=list)
    cash: list[dict[str, Any]] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    blob: dict[str, Any] | None = None


def parse_warning_entries(warnings: list[str]) -> ParsedWarnings:
    """Extract structured expectation pieces from one row's parse_warnings.

    When the machine-readable ``closed_loop_expectations:`` blob is present it
    is authoritative for the auto-checkable pieces (the prose fill/cash lines
    are duplicates kept for humans); ``expectation:`` prose notes are still
    collected and union-deduped with the blob's manual list.
    """
    out = ParsedWarnings()
    for w in warnings:
        w = (w or "").strip()
        if not w:
            continue
        if w.startswith(BLOB_PREFIX):
            try:
                blob = json.loads(w[len(BLOB_PREFIX):])
                if isinstance(blob, dict):
                    out.blob = blob
            except (ValueError, TypeError):
                _log.warning("closed_loop.blob_unparseable", entry=w[:120])
            continue
        m = _FILL_RE.match(w)
        if m:
            tail = (m.group("tail") or "").strip()
            sign = -1.0 if m.group("side") else 1.0
            out.fills.append({
                "symbol": m.group("sym").upper(),
                "shares_delta": sign * _num(m.group("sh")),
                "price": _num(m.group("px")),
                "price_estimated": "estimate" in tail.lower(),
                "raw": w,
            })
            continue
        m = _CASH_RE.match(w)
        if m:
            out.cash.append({
                "location": m.group("loc").strip(),
                "currency": m.group("ccy").strip().upper(),
                "recorded_after_local": _num(m.group("bal")),
                "overdraft": True,  # both entry kinds record a funding shortfall
                "raw": w,
            })
            continue
        if w.startswith(_EXPECTATION_PREFIX):
            out.manual.append(w)
    if out.blob is not None:
        # Prose fill/cash lines duplicate the blob — the blob wins.
        out.fills = []
        out.cash = []
        blob_manual = [str(x) for x in (out.blob.get("manual") or [])]
        out.manual = list(dict.fromkeys(blob_manual + out.manual))
    return out


# ---------------------------------------------------------------------------
# Position binding + row scanning
# ---------------------------------------------------------------------------


def _row_positions(row: PortfolioSnapshotRow) -> list[dict[str, Any]]:
    try:
        positions = json.loads(row.positions_json or "[]")
        return [p for p in positions if isinstance(p, dict)]
    except (ValueError, TypeError):
        return []


def _is_cash(p: dict[str, Any]) -> bool:
    return (p.get("asset_type") or "").strip().lower() == "cash"


def _bind_fill_to_position(
    fill: dict[str, Any], positions: list[dict[str, Any]],
) -> tuple[float | None, str | None, str | None]:
    """Resolve a prose fill entry to (expected_final_shares, location, currency)
    using the armed row's own post-fill book.

    The prose format is lossy on location/currency, so bind by symbol:
    prefer the apply-fills default (Leumi/USD), else a unique symbol match,
    else unresolvable (None) — surfaced loudly at verify, never dropped.
    A full SELL leaves no position row: expected 0 at the default account.
    """
    sym = fill["symbol"]
    cands = [
        p for p in positions
        if (p.get("symbol") or "").strip().upper() == sym and not _is_cash(p)
    ]
    default = [
        p for p in cands
        if (p.get("location") or "").strip().lower() == "leumi"
        and (p.get("currency") or "").strip().upper() == "USD"
    ]
    if default:
        p = default[0]
    elif len(cands) == 1:
        p = cands[0]
    elif not cands and fill["shares_delta"] < 0:
        # Fully sold out — the position legitimately left the book.
        return 0.0, "Leumi", "USD"
    else:
        return None, None, None
    return (
        float(p.get("shares") or 0.0),
        (p.get("location") or "").strip() or None,
        (p.get("currency") or "").strip().upper() or None,
    )


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_real_ingest(source_path: str | None) -> bool:
    sp = (source_path or "").strip()
    return bool(sp) and not sp.startswith(_SELF_SOURCE_PREFIXES)


def collect_armed_expectations(
    session: Session,
    *,
    user_id: str = "ariel",
    before_row_id: int | None = None,
) -> ArmedExpectations:
    """Scan ``portfolio_snapshots`` for armed (unverified) expectations.

    Armed = expectations in ``fills-applied:*`` rows with id greater than the
    newest REAL-ingest row's id (and, when ``before_row_id`` is given —
    the verify-on-ingest path — smaller than that new row's id, and relative
    to the newest real ingest BEFORE it). Read-only.
    """
    stmt = (
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(PortfolioSnapshotRow.id)
    )
    if before_row_id is not None:
        stmt = stmt.where(PortfolioSnapshotRow.id < before_row_id)
    rows = list(session.execute(stmt).scalars().all())

    last_real_id = 0
    for r in rows:
        if _is_real_ingest(r.source_path):
            last_real_id = r.id
    armed_rows = [
        r for r in rows
        if r.id > last_real_id
        and (r.source_path or "").startswith("fills-applied:")
    ]

    out = ArmedExpectations()
    # Later rows override earlier ones per key: the latest armed row's book
    # is the expected final state (row chains like deploy → SGOV-sale carry
    # the earlier row's fill lines forward; dedup handles the repetition).
    pos_by_key: dict[tuple[str, str, str], PositionExpectation] = {}
    cash_by_key: dict[tuple[str, str], CashExpectation] = {}
    seen_manual: set[str] = set()

    for row in armed_rows:
        out.armed_row_ids.append(row.id)
        imported = _as_utc(row.imported_at)
        if out.oldest_armed_at is None or (
            imported is not None and imported < out.oldest_armed_at
        ):
            out.oldest_armed_at = imported
            out.oldest_armed_date = (
                row.snapshot_date.isoformat() if row.snapshot_date else None
            )
        try:
            warnings = json.loads(row.parse_warnings_json or "[]")
        except (ValueError, TypeError):
            warnings = []
        parsed = parse_warning_entries([str(w) for w in warnings])
        positions = _row_positions(row)

        if parsed.blob is not None:
            for ep in parsed.blob.get("expected_positions") or []:
                sym = str(ep.get("symbol") or "").upper()
                if not sym:
                    continue
                loc = str(ep.get("location") or "Leumi")
                ccy = str(ep.get("currency") or "USD").upper()
                pos_by_key[(sym, loc.lower(), ccy)] = PositionExpectation(
                    symbol=sym, location=loc, currency=ccy,
                    expected_shares=(
                        float(ep["shares"]) if ep.get("shares") is not None else None
                    ),
                    shares_delta=float(ep.get("shares_delta") or 0.0),
                    price=(
                        float(ep["price"]) if ep.get("price") is not None else None
                    ),
                    price_estimated=bool(ep.get("price_estimated", False)),
                    source_row_id=row.id, raw=BLOB_PREFIX + "…",
                )
            cash_blob = parsed.blob.get("cash")
            if isinstance(cash_blob, dict) and cash_blob.get("location"):
                loc = str(cash_blob["location"])
                ccy = str(cash_blob.get("currency") or "USD").upper()
                after = cash_blob.get("after_local")
                cash_by_key[(loc.lower(), ccy)] = CashExpectation(
                    location=loc, currency=ccy,
                    recorded_after_local=(
                        float(after) if after is not None else None
                    ),
                    overdraft=(after is not None and float(after) < 0),
                    source_row_id=row.id, raw=BLOB_PREFIX + "…",
                )

        for f in parsed.fills:
            expected, loc, ccy = _bind_fill_to_position(f, positions)
            key = (f["symbol"], (loc or "leumi").lower(), (ccy or "USD"))
            pos_by_key[key] = PositionExpectation(
                symbol=f["symbol"], location=loc or "Leumi",
                currency=ccy or "USD",
                expected_shares=expected, shares_delta=f["shares_delta"],
                price=f["price"], price_estimated=f["price_estimated"],
                source_row_id=row.id, raw=f["raw"],
            )
        for c in parsed.cash:
            cash_by_key[(c["location"].lower(), c["currency"])] = CashExpectation(
                location=c["location"], currency=c["currency"],
                recorded_after_local=c["recorded_after_local"],
                overdraft=c["overdraft"], source_row_id=row.id, raw=c["raw"],
            )
        for m in parsed.manual:
            if m not in seen_manual:
                seen_manual.add(m)
                out.manual.append(ManualExpectation(text=m, source_row_id=row.id))

    out.positions = list(pos_by_key.values())
    out.cash = list(cash_by_key.values())
    return out


# ---------------------------------------------------------------------------
# Verification arithmetic (pure)
# ---------------------------------------------------------------------------


def _shares_match(expected: float, actual: float) -> bool:
    tol = max(SHARES_REL_TOL * abs(expected), SHARES_ABS_TOL)
    return abs(actual - expected) <= tol


def _actual_shares(
    positions: list[dict[str, Any]], exp: PositionExpectation,
) -> tuple[float, float | None]:
    """(summed actual shares, a representative ingested price) for the
    expectation's (symbol, location-prefix, currency). Location matches by
    prefix (``leumi`` matches ``Leumi Trade``), same convention as the
    Leumi reconcile gate."""
    total = 0.0
    price: float | None = None
    loc_prefix = (exp.location or "").strip().lower()
    for p in positions:
        if _is_cash(p):
            continue
        if (p.get("symbol") or "").strip().upper() != exp.symbol:
            continue
        if loc_prefix and not (
            (p.get("location") or "").strip().lower().startswith(loc_prefix)
        ):
            continue
        if exp.currency and (
            (p.get("currency") or "").strip().upper() != exp.currency
        ):
            continue
        total += float(p.get("shares") or 0.0)
        if p.get("current_price") is not None:
            price = float(p["current_price"])
    return total, price


def verify_against_positions(
    armed: ArmedExpectations, positions: list[dict[str, Any]],
) -> ClosedLoopVerification:
    """Deterministically check armed expectations against ingested truth."""
    result = ClosedLoopVerification()

    for exp in armed.positions:
        result.checked += 1
        where = f"{exp.location or '?'}/{exp.currency or '?'}"
        if exp.expected_shares is None:
            result.mismatches.append(
                f"unverifiable expectation for {exp.symbol} "
                f"(delta {exp.shares_delta:+g} sh) — could not bind the "
                f"fill entry to a unique position; check the broker export "
                f"manually [{exp.raw}]"
            )
            continue
        actual, actual_price = _actual_shares(positions, exp)
        if _shares_match(exp.expected_shares, actual):
            line = f"{exp.symbol} {exp.expected_shares:g} sh at {where} confirmed"
            if (
                exp.price
                and actual_price
                and abs(actual_price - exp.price) > PRICE_NOTE_REL * exp.price
            ):
                tag = "estimated fill price" if exp.price_estimated else "fill price"
                line += (
                    f" ({tag} {exp.price:g} vs ingested {actual_price:g} — "
                    f"price updated, shares govern)"
                )
            result.resolved.append(line)
        else:
            result.mismatches.append(
                f"expected {exp.symbol} {exp.expected_shares:g} sh at {where}, "
                f"ingest shows {actual:g} sh "
                f"(armed by snapshot row {exp.source_row_id}: {exp.raw})"
            )

    for cexp in armed.cash:
        result.checked += 1
        loc_prefix = cexp.location.strip().lower()
        actuals = [
            float(p.get("current_value_local") or 0.0)
            for p in positions
            if _is_cash(p)
            and (p.get("location") or "").strip().lower().startswith(loc_prefix)
            and (p.get("currency") or "").strip().upper() == cexp.currency
        ]
        where = f"{cexp.location}/{cexp.currency}"
        if not actuals:
            result.mismatches.append(
                f"expected a {where} cash balance to reconcile the recorded "
                f"funding shortfall "
                f"({cexp.recorded_after_local:,.2f}), but the ingest has NO "
                f"cash row at {where}"
            )
            continue
        balance = sum(actuals)
        if balance < 0:
            result.mismatches.append(
                f"{where} cash is STILL NEGATIVE after ingest "
                f"({balance:,.2f}; snapshot had recorded "
                f"{cexp.recorded_after_local:,.2f}) — the broker balance did "
                f"not cover the executed fills"
            )
        else:
            result.resolved.append(
                f"{where} cash reconciled: ingested {balance:,.2f} "
                f"(snapshot had recorded {cexp.recorded_after_local:,.2f} "
                f"post-fill)"
            )

    result.manual = [m.text for m in armed.manual]
    return result


# ---------------------------------------------------------------------------
# Verify-on-ingest (hooked from portfolio_ingest.xls_osh_pair write-through)
# ---------------------------------------------------------------------------


def verify_and_record_on_ingest(
    session: Session,
    *,
    user_id: str,
    new_row: PortfolioSnapshotRow,
    commit: bool = True,
) -> ClosedLoopVerification | None:
    """Verify armed expectations against a freshly-ingested snapshot row.

    Outcome lines are APPENDED to the NEW row's ``parse_warnings_json``
    (audit record; history rows are never modified). With ``commit=True``
    the open closed-loop proposals are also maintained: the "unverified"
    needs-info proposal is superseded (an ingest arrived), and a mismatch
    proposal is written/refreshed or superseded per the result. With
    ``commit=False`` (mid-ingest atomic batch) only the row append + flush
    happens — the daily sweep reconciles the proposals later.

    Returns ``None`` when the new row is not a real ingest or nothing was
    armed. Never raises — callers must not lose an ingest to this gate.
    """
    try:
        if new_row is None or not _is_real_ingest(new_row.source_path):
            return None
        armed = collect_armed_expectations(
            session, user_id=user_id, before_row_id=new_row.id,
        )
        if armed.empty:
            return None
        result = verify_against_positions(armed, _row_positions(new_row))

        lines = result.reconcile_lines()
        try:
            existing = json.loads(new_row.parse_warnings_json or "[]")
        except (ValueError, TypeError):
            existing = []
        new_row.parse_warnings_json = json.dumps(list(existing) + lines)
        if commit:
            session.commit()
        else:
            session.flush()

        if commit:
            _supersede_open(
                session, user_id=user_id,
                dedup_key=_UNVERIFIED_DEDUP.format(user_id=user_id),
                reason="real ingest arrived; expectations verified",
            )
            mismatch_key = _MISMATCH_DEDUP.format(user_id=user_id)
            if result.mismatches:
                _write_or_refresh_proposal(
                    session, user_id=user_id, dedup_key=mismatch_key,
                    severity="warning",
                    summary=(
                        f"{len(result.mismatches)} broker-fill expectation(s) "
                        f"MISMATCHED the ingested portfolio — expected-vs-actual "
                        f"needs your eyes"
                    ),
                    rationale_md=(
                        "The last real ingest was checked against the armed "
                        "closed-loop expectations (deterministic share/cash "
                        "arithmetic):\n\n"
                        + "\n".join(f"- {m}" for m in result.mismatches)
                        + "\n\nResolved alongside:\n"
                        + ("\n".join(f"- {r}" for r in result.resolved) or "- none")
                    ),
                    payload={
                        "kind": "closed_loop_mismatch",
                        "ingest_row_id": new_row.id,
                        "mismatches": result.mismatches,
                        "resolved": result.resolved,
                    },
                )
            else:
                _supersede_open(
                    session, user_id=user_id, dedup_key=mismatch_key,
                    reason="subsequent ingest verified clean",
                )

        _log.info(
            "closed_loop.verified_on_ingest",
            user_id=user_id, ingest_row_id=new_row.id,
            checked=result.checked, resolved=len(result.resolved),
            mismatches=len(result.mismatches), manual=len(result.manual),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — never lose an ingest to the gate
        _log.warning("closed_loop.verify_failed", error=str(exc)[:200])
        return None


# ---------------------------------------------------------------------------
# Daily sweep (called from PendingReevaluationDailyLoop.tick)
# ---------------------------------------------------------------------------


def sweep_unverified_expectations(
    session: Session,
    *,
    user_id: str = "ariel",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Daily check: armed expectations with no real ingest since arming.

    Older than :data:`STALE_AFTER_DAYS` → write ONE dedup'd needs-info
    proposal (refresh-in-place on collision, same pattern as the deploy-team
    flag sink). Nothing armed → auto-supersede any open closed-loop
    proposals so resolved items LEAVE the client's checklist.
    """
    now = _as_utc(now) or datetime.now(UTC)
    armed = collect_armed_expectations(session, user_id=user_id)

    if armed.empty:
        superseded = _supersede_open(
            session, user_id=user_id,
            dedup_key=_UNVERIFIED_DEDUP.format(user_id=user_id),
            reason="no armed expectations remain",
        )
        return {"armed": 0, "proposal": None, "superseded": superseded}

    age_days: float | None = None
    if armed.oldest_armed_at is not None:
        age_days = (now - armed.oldest_armed_at).total_seconds() / 86400.0
    if age_days is None or age_days < STALE_AFTER_DAYS:
        return {
            "armed": armed.checkable_count,
            "proposal": None,
            "age_days": round(age_days, 2) if age_days is not None else None,
        }

    armed_date = armed.oldest_armed_date or "an earlier date"
    n = armed.checkable_count
    detail_lines = [
        f"- {p.symbol}: expected "
        + (f"{p.expected_shares:g} sh" if p.expected_shares is not None
           else f"delta {p.shares_delta:+g} sh (unbound)")
        + f" at {p.location or '?'}/{p.currency or '?'}"
        for p in armed.positions
    ] + [
        f"- cash {c.location}/{c.currency}: recorded post-fill balance "
        f"{c.recorded_after_local:,.2f} awaiting broker reconciliation"
        for c in armed.cash
    ] + [f"- (manual) {m.text}" for m in armed.manual]
    proposal_id = _write_or_refresh_proposal(
        session, user_id=user_id,
        dedup_key=_UNVERIFIED_DEDUP.format(user_id=user_id),
        severity="warning",
        summary=(
            f"{n} broker-fill expectations from {armed_date} are unverified — "
            f"send the current broker export"
        ),
        rationale_md=(
            f"Broker fills were applied to the book on {armed_date} "
            f"(snapshot rows {armed.armed_row_ids}) and armed closed-loop "
            f"expectations, but no real broker/TSV ingest has landed since "
            f"({age_days:.0f} days). Argosy cannot confirm the fills against "
            f"the bank's own record until you upload a current export.\n\n"
            + "\n".join(detail_lines)
        ),
        payload={
            "kind": "closed_loop_unverified",
            "armed_row_ids": armed.armed_row_ids,
            "armed_since": armed_date,
            "expectations": n,
        },
    )
    return {
        "armed": n,
        "proposal": proposal_id,
        "age_days": round(age_days, 2),
    }


# ---------------------------------------------------------------------------
# Proposal sink helpers (dedup refresh-in-place + supersede)
# ---------------------------------------------------------------------------


def _write_or_refresh_proposal(
    session: Session,
    *,
    user_id: str,
    dedup_key: str,
    severity: str,
    summary: str,
    rationale_md: str,
    payload: dict[str, Any],
) -> int | None:
    """Write one ``note_only`` proposal; on the open-dedup collision refresh
    the existing OPEN row in place (same pattern as the deploy-team flag
    sink — the inbox always shows today's state, never a stale count)."""
    from sqlalchemy.exc import IntegrityError

    now = datetime.now(UTC)
    row = ActionProposal(
        user_id=user_id,
        summary=summary,
        rationale_md=rationale_md,
        suggested_payload=json.dumps(payload, default=str),
        severity=severity,
        surfaced_at=now,
        expires_at=now + timedelta(days=30),
        status="open",
        kind="note_only",
        dedup_key=dedup_key,
        execution_state="proposed",
    )
    session.add(row)
    try:
        session.commit()
        return row.id
    except IntegrityError as exc:
        session.rollback()
        existing = (
            session.query(ActionProposal)
            .filter_by(dedup_key=dedup_key, status="open")
            .first()
        )
        if existing is not None:
            existing.summary = summary
            existing.rationale_md = rationale_md
            existing.suggested_payload = json.dumps(payload, default=str)
            existing.severity = severity
            existing.surfaced_at = now
            existing.expires_at = now + timedelta(days=30)
            session.commit()
            _log.info(
                "closed_loop.proposal_refreshed",
                dedup_key=dedup_key, proposal_id=existing.id,
            )
            return existing.id
        _log.warning(
            "closed_loop.proposal_write_skipped",
            dedup_key=dedup_key, error=str(getattr(exc, "orig", exc))[:160],
        )
        return None


def _supersede_open(
    session: Session, *, user_id: str, dedup_key: str, reason: str,
) -> int:
    """Close open closed-loop proposals for a resolved condition — a
    resolved item must LEAVE the client's checklist (stored for audit)."""
    try:
        rows = (
            session.query(ActionProposal)
            .filter_by(user_id=user_id, dedup_key=dedup_key, status="open")
            .all()
        )
        for row in rows:
            row.status = "superseded"
            row.decided_at = datetime.now(UTC)
            row.decided_by_user_note = f"auto-superseded: {reason}"
        if rows:
            session.commit()
            _log.info(
                "closed_loop.proposal_superseded",
                dedup_key=dedup_key, count=len(rows), reason=reason,
            )
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        session.rollback()
        _log.warning("closed_loop.supersede_failed", error=str(exc)[:160])
        return 0


__all__ = [
    "ArmedExpectations",
    "CashExpectation",
    "ClosedLoopVerification",
    "ManualExpectation",
    "PositionExpectation",
    "collect_armed_expectations",
    "parse_warning_entries",
    "sweep_unverified_expectations",
    "verify_against_positions",
    "verify_and_record_on_ingest",
]
