"""Deterministic execution-evidence checker for plan action items.

Why this exists
---------------
Two OVERDUE plan action items ("Sell the June 17 vest → park in SGOV",
"First UCITS DCA tranche" due 2026-07-01) were ALREADY EXECUTED by the
2026-07-06 deploy, but the checklist kept nagging because nothing tied
the book back to the item. This module closes that loop:

* For each open action item it looks for **matching evidence** in the
  book (latest portfolio snapshot positions) and the fills history.
* Evidence found → the item is stamped ``argosy_verified=True`` with a
  plain-language evidence summary and ``argosy_verified_status=
  "looks_executed"`` — and the greeting surfaces it ONCE as a
  needs-confirm ("this looks executed — confirm done").
* **NO auto-ack.** The client confirms through the existing
  ``POST /api/plan/action-items/{item_id}/ack`` endpoint; Argosy only
  provides the evidence. A confirmed (acked) item disappears.

Everything here is deterministic — string matching on the item text +
sums over snapshot positions / fills rows. No LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from argosy.logging import get_logger

log = get_logger("argosy.services.action_item_evidence")

#: Minimum SGOV book value (USD) for "the vest proceeds look parked in
#: SGOV" — well below one net vest tranche (~$80k) but far above dust,
#: so a leftover $200 stub can't satisfy the item.
_SGOV_MIN_USD = 10_000.0

#: Minimum number of DISTINCT plan UCITS instruments that must be held
#: for "the first UCITS tranche looks executed". The Jul-6 deploy bought
#: 8+ UCITS names; requiring 3 keeps the matcher robust to partial books
#: while a single pre-existing UCITS position can't satisfy it alone.
_UCITS_MIN_DISTINCT = 3


@dataclass(frozen=True)
class ActionEvidence:
    """Positive execution evidence for one action item."""

    status: str  # "looks_executed"
    summary: str


def _fmt_usd(v: float) -> str:
    a = abs(float(v))
    if a >= 1_000_000:
        s = f"{a / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"${s}M"
    if a >= 1_000:
        s = f"{a / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"${s}k"
    return f"${a:,.0f}"


def _is_sgov_park_item(text: str) -> bool:
    """The 'sell the vest → park proceeds in SGOV' item."""
    t = text.lower()
    return "sgov" in t and any(w in t for w in ("vest", "park", "sell", "proceeds"))


def _is_ucits_tranche_item(text: str) -> bool:
    """The 'first UCITS DCA tranche' item."""
    t = text.lower()
    return "ucits" in t and any(
        w in t for w in ("dca", "dollar-cost", "tranche", "deploy")
    )


class ActionEvidenceContext:
    """Book + fills + plan-instrument state loaded ONCE per request.

    ``evidence_for(label, detail, dated)`` runs the deterministic
    matchers against that state. Every load path is best-effort — a
    missing snapshot / plan simply yields no evidence (items stay
    overdue), never an exception into the route.
    """

    def __init__(
        self,
        *,
        positions: list[dict[str, Any]],
        snapshot_date: Any,
        ucits_symbols: frozenset[str],
        deploy_snapshot_dates: list[date],
        fills: list[Any],
        nvda_sales: list[dict[str, Any]] | None = None,
        nvda_sales_anchor_year: int | None = None,
    ) -> None:
        self.positions = positions
        self.snapshot_date = snapshot_date
        self.ucits_symbols = ucits_symbols
        self.deploy_snapshot_dates = deploy_snapshot_dates
        self.fills = fills
        #: The latest snapshot's month-granular NVDA sale rows
        #: (``{month, shares, price}``) + the year they anchor to — the
        #: book's own record of whether the SALE half of "sell the vest →
        #: park in SGOV" actually happened.
        self.nvda_sales = nvda_sales or []
        self.nvda_sales_anchor_year = nvda_sales_anchor_year

    # -- loading --------------------------------------------------------------

    @classmethod
    def load(cls, session: Any, user_id: str) -> "ActionEvidenceContext":
        positions: list[dict[str, Any]] = []
        snapshot_date: Any = None
        nvda_sales: list[dict[str, Any]] = []
        nvda_sales_anchor_year: int | None = None
        try:
            from argosy.services.portfolio_snapshot_store import (
                get_latest_snapshot_row,
            )

            row = get_latest_snapshot_row(session, user_id)
            if row is not None and row.positions_json:
                parsed = json.loads(row.positions_json)
                if isinstance(parsed, list):
                    positions = [p for p in parsed if isinstance(p, dict)]
                snapshot_date = getattr(row, "snapshot_date", None)
            if row is not None and getattr(row, "nvda_sales_json", None):
                parsed_sales = json.loads(row.nvda_sales_json)
                if isinstance(parsed_sales, list):
                    nvda_sales = [s for s in parsed_sales if isinstance(s, dict)]
                snap_d = getattr(row, "snapshot_date", None)
                if isinstance(snap_d, datetime):
                    snap_d = snap_d.date()
                if isinstance(snap_d, date):
                    nvda_sales_anchor_year = snap_d.year
        except Exception:  # noqa: BLE001 — evidence is enrichment only
            log.warning("action_item_evidence.snapshot_load_failed", exc_info=True)

        ucits_symbols: set[str] = set()
        try:
            from argosy.services.target_allocation_doc import (
                load_plan_target_allocation,
            )
            from argosy.state.queries import get_current_plan, get_pending_draft

            pv = get_pending_draft(session, user_id) or get_current_plan(
                session, user_id
            )
            doc = load_plan_target_allocation(pv) if pv is not None else None
            for c in getattr(doc, "classes", []) or []:
                for instr in getattr(c, "instruments", []) or []:
                    if (getattr(instr, "domicile", "") or "").strip().upper() == "IE":
                        sym = (getattr(instr, "symbol", "") or "").strip().upper()
                        if sym:
                            ucits_symbols.add(sym)
        except Exception:  # noqa: BLE001
            log.warning("action_item_evidence.doc_load_failed", exc_info=True)

        deploy_dates: list[date] = []
        try:
            from sqlalchemy import select

            from argosy.state.models import PortfolioSnapshotRow

            rows = session.execute(
                select(PortfolioSnapshotRow).where(
                    PortfolioSnapshotRow.user_id == user_id,
                    PortfolioSnapshotRow.source_path.like("fills-applied:%"),
                )
            ).scalars().all()
            for r in rows:
                d = getattr(r, "snapshot_date", None)
                if isinstance(d, datetime):
                    d = d.date()
                if isinstance(d, date):
                    deploy_dates.append(d)
        except Exception:  # noqa: BLE001
            log.warning("action_item_evidence.deploy_rows_load_failed", exc_info=True)

        fills: list[Any] = []
        try:
            from sqlalchemy import select

            from argosy.state.models import Fill

            fills = list(
                session.execute(
                    select(Fill).where(Fill.user_id == user_id)
                ).scalars().all()
            )
        except Exception:  # noqa: BLE001
            log.warning("action_item_evidence.fills_load_failed", exc_info=True)

        return cls(
            positions=positions,
            snapshot_date=snapshot_date,
            ucits_symbols=frozenset(ucits_symbols),
            deploy_snapshot_dates=deploy_dates,
            fills=fills,
            nvda_sales=nvda_sales,
            nvda_sales_anchor_year=nvda_sales_anchor_year,
        )

    # -- book accessors ---------------------------------------------------------

    def _holding_usd(self, symbol: str) -> tuple[float, int]:
        """(total USD value, account count) for ``symbol`` across the book."""
        total = 0.0
        n = 0
        for p in self.positions:
            if (p.get("symbol") or "").strip().upper() != symbol.upper():
                continue
            v_k = p.get("usd_value_k")
            if isinstance(v_k, (int, float)) and v_k > 0:
                total += float(v_k) * 1000.0
                n += 1
        return total, n

    def _held_ucits(self) -> list[str]:
        held: set[str] = set()
        for p in self.positions:
            sym = (p.get("symbol") or "").strip().upper()
            if sym in self.ucits_symbols:
                v_k = p.get("usd_value_k")
                if isinstance(v_k, (int, float)) and v_k > 0:
                    held.add(sym)
        return sorted(held)

    def _buy_fills(self, *, on_or_after: date | None, symbols: frozenset[str] | None) -> list[Any]:
        out: list[Any] = []
        for f in self.fills:
            action = (getattr(f, "action", "") or "").strip().upper()
            qty = float(getattr(f, "quantity", 0) or 0)
            if not (action.startswith("BUY") or qty > 0):
                continue
            tk = (getattr(f, "ticker", "") or "").strip().upper()
            if symbols is not None and tk not in symbols:
                continue
            filled_at = getattr(f, "filled_at", None)
            if on_or_after is not None and filled_at is not None:
                fd = filled_at.date() if isinstance(filled_at, datetime) else filled_at
                if fd < on_or_after:
                    continue
            out.append(f)
        return out

    def _has_deploy_on_or_after(self, due: date | None) -> bool:
        if due is None:
            return bool(self.deploy_snapshot_dates)
        return any(d >= due for d in self.deploy_snapshot_dates)

    def _nvda_sale_on_or_after(self, due: date | None) -> str | None:
        """Evidence the NVDA SALE itself happened on/after ``due``.

        Two sources, either satisfies:

        * a SELL fill for NVDA with ``filled_at`` on/after the due date
          (day-precise — the fills ledger is the canonical source);
        * a ``nvda_sales_json`` row (month-granular ``{month, shares}``)
          whose month resolves on/after the due month in the snapshot's
          anchor year. The approximation is at most one month wide, and
          only on the due date's own month — same convention as
          ``nvda_sales_history._sum_monthly_sales``.

        Returns a human-readable descriptor of the matched evidence, or
        ``None`` when the book shows NO sale — SGOV merely existing
        (a position that may long predate the vest) is NOT execution
        evidence for "sell the vest".
        """
        # (a) day-precise fills ledger.
        for f in self.fills:
            tk = (getattr(f, "ticker", "") or "").strip().upper()
            if tk != "NVDA":
                continue
            action = (getattr(f, "action", "") or "").strip().upper()
            qty = float(getattr(f, "quantity", 0) or 0)
            is_sell = qty < 0 or action.startswith("SELL") or action.startswith("SOLD")
            if action.startswith("BUY") and qty >= 0:
                is_sell = False
            if not is_sell:
                continue
            filled_at = getattr(f, "filled_at", None)
            fd = filled_at.date() if isinstance(filled_at, datetime) else filled_at
            if due is not None:
                if not isinstance(fd, date) or fd < due:
                    continue
            shares = int(round(abs(qty)))
            when = f" on {fd.isoformat()}" if isinstance(fd, date) else ""
            return f"an NVDA sale of {shares} shares filled{when}"

        # (b) month-granular snapshot sale rows.
        from argosy.services.nvda_sales_history import _month_index

        for s in self.nvda_sales:
            month = s.get("month")
            shares = s.get("shares")
            if not month or not shares:
                continue
            m_idx = _month_index(str(month))
            if m_idx is None:
                continue
            if due is not None:
                anchor = self.nvda_sales_anchor_year
                if anchor is None or anchor < due.year:
                    continue
                if anchor == due.year and m_idx < due.month:
                    continue
            return f"the book records an NVDA sale of {shares} shares in {month}"
        return None

    # -- matchers ---------------------------------------------------------------

    def _sgov_evidence(self, due: date | None) -> ActionEvidence | None:
        # The item is "sell the vest → park in SGOV". Its evidence must
        # include the SALE itself — an SGOV position alone can predate
        # the vest by years and proves nothing about execution.
        sale = self._nvda_sale_on_or_after(due)
        if sale is None:
            return None
        total, n_accounts = self._holding_usd("SGOV")
        sgov_buys = self._buy_fills(
            on_or_after=due, symbols=frozenset({"SGOV"})
        )
        if total < _SGOV_MIN_USD and not sgov_buys:
            return None
        as_of = f" as of {self.snapshot_date}" if self.snapshot_date else ""
        parts = [
            sale,
            f"SGOV is held at {_fmt_usd(total)} across "
            f"{n_accounts} account(s){as_of}",
        ]
        if sgov_buys:
            parts.append(f"{len(sgov_buys)} SGOV buy fill(s) on record")
        return ActionEvidence(
            status="looks_executed",
            summary=(
                "; ".join(parts)
                + " — the vest proceeds look parked in SGOV. Confirm to close "
                "this item."
            ),
        )

    def _ucits_evidence(self, due: date | None) -> ActionEvidence | None:
        held = self._held_ucits()
        ucits_buys = self._buy_fills(on_or_after=due, symbols=self.ucits_symbols)
        deploy_applied = self._has_deploy_on_or_after(due)
        if len(held) < _UCITS_MIN_DISTINCT:
            return None
        if not deploy_applied and not ucits_buys:
            # UCITS held, but nothing shows a tranche EXECUTED on/after the
            # due date — pre-existing positions alone don't satisfy the item.
            return None
        as_of = f" as of {self.snapshot_date}" if self.snapshot_date else ""
        shown = ", ".join(held[:6]) + (" …" if len(held) > 6 else "")
        src = (
            f"{len(ucits_buys)} UCITS buy fill(s) on record"
            if ucits_buys
            else "a broker-fills deploy was applied to the book"
        )
        return ActionEvidence(
            status="looks_executed",
            summary=(
                f"The book holds {len(held)} plan UCITS instruments "
                f"({shown}){as_of} and {src} on/after the due date — the "
                "UCITS tranche looks executed. Confirm to close this item."
            ),
        )

    def evidence_for(
        self, *, label: str, detail: str, dated: date | None
    ) -> ActionEvidence | None:
        """Deterministic evidence lookup for one action item; ``None``
        when no matcher fires or the matched evidence is absent."""
        text = f"{label} {detail}"
        try:
            if _is_sgov_park_item(text):
                return self._sgov_evidence(dated)
            if _is_ucits_tranche_item(text):
                return self._ucits_evidence(dated)
        except Exception:  # noqa: BLE001 — evidence must never sink the checklist
            log.warning("action_item_evidence.matcher_failed", exc_info=True)
        return None


def looks_executed_unconfirmed_items(
    session: Any, user_id: str, *, today: date | None = None
) -> list[Any]:
    """Open action items with POSITIVE execution evidence, not yet
    confirmed by the client — the greeting's needs-confirm source.

    Returns the ``ActionItem`` pydantic rows from the plan route's
    collector (evidence-stamped, unacknowledged only). Best-effort:
    any failure returns an empty list.
    """
    try:
        from argosy.api.routes.plan import _collect_action_items, _load_action_acks
        from argosy.state.queries import get_current_plan, get_pending_draft

        pv = get_pending_draft(session, user_id) or get_current_plan(
            session, user_id
        )
        if pv is None:
            return []
        today_d = today or datetime.now(timezone.utc).date()
        items = _collect_action_items(
            pv,
            today=today_d,
            window_days=14,
            acked=_load_action_acks(session, user_id),
            evidence_ctx=ActionEvidenceContext.load(session, user_id),
        )
        return [
            it for it in items
            if it.argosy_verified is True
            and it.argosy_verified_status == "looks_executed"
            and not it.acknowledged
        ]
    except Exception:  # noqa: BLE001 — greeting enrichment only
        log.warning(
            "action_item_evidence.greeting_lookup_failed", exc_info=True
        )
        return []


def overdue_unexecuted_items(
    session: Any, user_id: str, *, today: date | None = None
) -> list[Any]:
    """Open action items PAST their due date with NO execution evidence
    — the greeting's needs-action source.

    The complement of :func:`looks_executed_unconfirmed_items`:
    ``status == "OVERDUE"`` and ``argosy_verified is not True`` (same
    demotion rule as the action-items route's ``overdue_count``) and
    not acknowledged. An item Argosy can verify as looks-executed is
    NEVER also overdue here — it surfaces as a needs-confirm instead.
    Best-effort: any failure returns an empty list.
    """
    try:
        from argosy.api.routes.plan import _collect_action_items, _load_action_acks
        from argosy.state.queries import get_current_plan, get_pending_draft

        pv = get_pending_draft(session, user_id) or get_current_plan(
            session, user_id
        )
        if pv is None:
            return []
        today_d = today or datetime.now(timezone.utc).date()
        items = _collect_action_items(
            pv,
            today=today_d,
            window_days=14,
            acked=_load_action_acks(session, user_id),
            evidence_ctx=ActionEvidenceContext.load(session, user_id),
        )
        return [
            it for it in items
            if it.status == "OVERDUE"
            and it.argosy_verified is not True
            and not it.acknowledged
        ]
    except Exception:  # noqa: BLE001 — greeting enrichment only
        log.warning(
            "action_item_evidence.overdue_lookup_failed", exc_info=True
        )
        return []


__all__ = [
    "ActionEvidence",
    "ActionEvidenceContext",
    "looks_executed_unconfirmed_items",
    "overdue_unexecuted_items",
]
