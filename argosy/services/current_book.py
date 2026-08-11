"""Canonical current-book accessor — the ONE way every money surface loads
the user's current holdings.

Before this, ~20 surfaces each re-implemented four things, each subtly wrong
(Sol BLOCK rounds 1-5):

  (a) HEAD-PICK — some ordered by ``id.desc()``, some by ``snapshot_date`` —
      so a backfilled/restore row with a higher id but older import could make
      one surface publish a different book than the plan / dashboard.
  (b) STALENESS — some passed ``today=snapshot_date`` to the book loader,
      backdating every mark's age to zero so a weeks-stale book read as fresh.
  (c) RAW READ — some read ``positions_json`` directly, bypassing conservation
      + the durable-unmanaged restore (understating when Schwab NVDA was
      omitted) and the reprice/degrade path.
  (d) CONFIDENCE — some hard-coded ``HIGH`` regardless of staleness/degrade.

This module centralizes all four. A surface asks ONCE via
:func:`load_current_book` and gets a conserved, REAL-today, confidence-aware
book plus helpers that stamp the right confidence, so the four bugs cannot be
re-introduced per-surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from argosy.logging import get_logger
from argosy.services.holding_books import (
    TotalBookResult,
    UnmanagedLoadResult,
    load_total_book,
    parse_positions_json,
    symbol_value_usd_k,
)

log = get_logger(__name__)

HIGH = "HIGH"
MEDIUM = "MEDIUM"


def _to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CurrentBook:
    """The user's canonical current book + integrity + confidence.

    ``snapshot is None`` means the user has no snapshot at all — callers must
    treat that as PENDING (never zero). ``degraded`` means the book could not
    publish current-money marks (a policy holding unrestorable, a hard-stale
    unrepriceable mark, a conservation break) — callers must return
    unavailable, never a HIGH figure.
    """

    snapshot: Any
    result: TotalBookResult
    snapshot_id: int | None
    snapshot_date: date | None
    fx_usd_nis: float | None
    fx_usd_eur: float | None
    # Spine gate (Phase 3c). ``validated`` = the head snapshot carries a PASS
    # integrity verdict (same-snapshot/user/seq, content-hash matched). Set on
    # every load from the validated-snapshot predicate; carried on the book so
    # every surface that flows through ``load_current_book`` sees it. In WARN
    # (default) it is INFORMATIONAL only — behavior is unchanged.
    #
    # ``load_current_book`` is the ONLY sanctioned constructor — it always sets
    # ``validated`` explicitly from the predicate. The ``True`` default exists
    # solely so the confidence-helper unit tests (and any future in-process
    # constructor) mean "assume validated (warn-safe)" and never accidentally
    # trip the enforce refusal. This IS a fail-OPEN default by design (an
    # unset flag must never lock a money surface out); the flip side is that a
    # future production constructor which forgets to set ``validated`` would
    # silently be treated as validated — so DO NOT add another constructor,
    # route new book loads through ``load_current_book``.
    validated: bool = True
    validation_reason: str = ""

    # --- book views -------------------------------------------------------
    @property
    def total(self) -> list[dict[str, Any]]:
        return self.result.total

    @property
    def managed(self) -> list[dict[str, Any]]:
        return self.result.managed

    @property
    def degraded(self) -> bool:
        return self.result.degraded

    @property
    def degrade_reason(self) -> str | None:
        return self.result.degrade_reason

    @property
    def stale_marks(self) -> tuple[str, ...]:
        return self.result.stale_marks

    @property
    def is_empty(self) -> bool:
        return self.snapshot is None

    # --- confidence -------------------------------------------------------
    def book_confidence(self) -> str:
        """``HIGH`` only when NO mark in the book is soft-stale.

        Use for any figure whose value spans more than one symbol — net worth,
        US-situs estate, or the DENOMINATOR of a ratio (e.g. NVDA weight is
        NVDA ÷ tradeable book, so a stale AAPL still degrades it).
        """
        return MEDIUM if self.result.stale_marks else HIGH

    def symbol_confidence(self, symbol: str) -> str:
        """``HIGH`` unless THIS symbol's own mark is soft-stale.

        Use for a single-symbol ABSOLUTE value (e.g. NVDA value in NIS): it may
        stay HIGH when only some other symbol is stale.
        """
        return MEDIUM if self.result.is_mark_stale(symbol) else HIGH

    def stale_note(self) -> str:
        """Human suffix for a downgraded ``source_locator`` (empty if fresh)."""
        if not self.result.stale_marks:
            return ""
        return (
            f" [STALE MARK — soft-stale marks in book "
            f"({', '.join(self.result.stale_marks)}); confidence downgraded "
            f"from HIGH]"
        )

    # --- money helpers ----------------------------------------------------
    def symbol_usd_k(self, symbol: str) -> float:
        return symbol_value_usd_k(self.total, symbol)


def _empty_result() -> TotalBookResult:
    return TotalBookResult(
        total=[], managed=[],
        load=UnmanagedLoadResult(rows=[], ok=True),
        degraded=False, degrade_reason=None, stale_marks=(),
    )


def load_current_book(
    session: Any,
    user_id: str,
    *,
    today: date | None = None,
    quote_fn: Any | None = None,
    fx_usd_nis: float | None = None,
    fx_usd_eur: float | None = None,
) -> CurrentBook:
    """Load the user's canonical current book — the single money-surface entry.

    * HEAD-PICK via the ONE accessor ``get_latest_snapshot_row``
      (``imported_at DESC, id DESC``) — every surface agrees on the head.
    * REAL-today staleness: ``today`` defaults to ``date.today()`` inside the
      book loader; it is NEVER backdated to the snapshot's own date.
    * Full conservation + durable-unmanaged (Schwab NVDA) restore + reprice.
    * ``stale_marks`` / ``degraded`` for the confidence helpers.

    ``snapshot is None`` (empty book) is returned as a non-degraded empty book;
    callers must treat it as pending.
    """
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

    snap = get_latest_snapshot_row(session, user_id)
    if snap is None:
        return CurrentBook(
            snapshot=None, result=_empty_result(), snapshot_id=None,
            snapshot_date=None, fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur,
            validated=True, validation_reason="empty book (no snapshot)",
        )
    raw = parse_positions_json(snap.positions_json)
    # SPINE GATE (Phase 3c, spec §2A) — WARN-FIRST, DEFAULT-OFF. Consult the
    # validated-snapshot predicate on EVERY load and carry the flag on the book.
    # When the head snapshot lacks a PASS integrity verdict (or its content hash
    # no longer matches), log ``spine_gate.would_refuse`` and set
    # ``validated=False`` — but DO NOT change behavior: the book is still built
    # and returned exactly as before. Promotion to an actual refusal lives in the
    # money-critical surfaces behind ``settings.spine_gate_enforce`` (default
    # False). This absorbs the prior env-only ``ARGOSY_SPINE_GATE`` log seam.
    book_validated = True
    validation_reason = ""
    try:
        from argosy.services.spine.validated_snapshot import is_snapshot_validated

        book_validated = is_snapshot_validated(session, user_id=user_id, snapshot=snap)
        if not book_validated:
            validation_reason = "head snapshot has no PASS integrity verdict"
            log.warning(
                "current_book.spine_gate.would_refuse",
                user_id=user_id,
                snapshot_id=getattr(snap, "id", None),
                reason=validation_reason,
            )
    except Exception as exc:  # noqa: BLE001 — the gate must NEVER break a load
        # Fail-open in WARN: an unverifiable gate must not degrade a working load.
        book_validated = True
        validation_reason = ""
        log.warning(
            "current_book.spine_gate.error",
            snapshot_id=getattr(snap, "id", None),
            err=str(exc)[:160],
        )
    result = load_total_book(
        session, user_id, raw,
        snapshot_date=getattr(snap, "snapshot_date", None),
        today=today,  # None -> real date.today() inside load_total_book
        quote_fn=quote_fn,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
    )
    return CurrentBook(
        snapshot=snap,
        result=result,
        snapshot_id=getattr(snap, "id", None),
        snapshot_date=getattr(snap, "snapshot_date", None),
        fx_usd_nis=(
            fx_usd_nis if fx_usd_nis is not None
            else _to_float(getattr(snap, "fx_usd_nis", None))
        ),
        fx_usd_eur=(
            fx_usd_eur if fx_usd_eur is not None
            else _to_float(getattr(snap, "fx_usd_eur", None))
        ),
        validated=book_validated,
        validation_reason=validation_reason,
    )


__all__ = ["CurrentBook", "load_current_book", "HIGH", "MEDIUM"]
