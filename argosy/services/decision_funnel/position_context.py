"""Stage-3 INPUTS — current-position + active-monitor-flag context.

Deterministic plumbing (inputs, not a judgment gate), per the exposure-aware
doctrine: the deep-decision fleet must always know

* whether the client ALREADY HOLDS the candidate ticker (shares / value /
  % of the tradeable book / account), so a BUY is framed as a TOP-UP of an
  existing position and never as "initiate a new position"; and
* every ACTIVE monitor flag on that ticker (e.g. ``thesis_monitor_weakened``)
  with its reason, so a buy-more-vs-weakened-thesis conflict is adjudicated
  explicitly by the fleet instead of silently missed.

Root incident (SOFI, funnel run 2 / proposal 1, 2026-07-07): Stage 1 routed
SOFI as a HELD name on its active ``thesis_monitor_weakened`` flag, but the
Stage-3 fleet ran with an EMPTY ``positions_summary`` and no flag context —
it answered "should we initiate SOFI?" and proposed a $3k starter buy while
the client already held ~$35.5k of SOFI under a weakened-thesis flag.

This module only assembles ALREADY-INGESTED facts (latest portfolio snapshot
+ active ``monitor_flags`` rows) into a text block for the fleet packet. It
never judges the decision.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.decision_funnel.position_context")

# Rationale excerpts are context, not the full artifact — keep the packet lean.
_RATIONALE_EXCERPT_CHARS = 700


def _fmt_usd(value: float) -> str:
    return f"${value:,.0f}"


def _position_lines(session: Session, *, user_id: str, ticker: str) -> list[str]:
    """Held-position lines from the latest snapshot (empty framing when not
    held). Uses the SAME snapshot + weight definition Stage 1 routes on
    (``load_book``) so every stage cites one number."""
    from argosy.services.decision_funnel.book import load_book
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row

    snap = get_latest_snapshot_row(session, user_id)
    positions: list = []
    if snap is not None:
        try:
            positions = json.loads(snap.positions_json or "[]")
        except (json.JSONDecodeError, TypeError):
            positions = []

    rows = [
        p
        for p in positions
        if isinstance(p, dict)
        and (p.get("symbol") or "").strip().upper() == ticker
        and "cash" not in (p.get("asset_type") or "").lower()
    ]
    if not rows:
        return [
            f"CURRENT POSITION — {ticker}: NOT HELD in the latest portfolio "
            "snapshot. A BUY here would INITIATE a new position.",
        ]

    lines = [f"CURRENT POSITION — {ticker} (latest portfolio snapshot):"]
    total_usd = 0.0
    for p in rows:
        try:
            val_usd = float(p.get("usd_value_k") or 0.0) * 1000.0
        except (TypeError, ValueError):
            val_usd = 0.0
        total_usd += val_usd
        try:
            shares = float(p.get("shares") or 0.0)
        except (TypeError, ValueError):
            shares = 0.0
        account = (p.get("location") or "").strip() or "unknown account"
        shares_str = f"{shares:,.4g} shares, " if shares > 0 else ""
        lines.append(f"- HELD: {shares_str}~{_fmt_usd(val_usd)} in {account}.")

    weight_pct = None
    try:
        for h in load_book(session, user_id=user_id):
            if h.ticker.upper() == ticker:
                weight_pct = h.weight_pct
                break
    except Exception:  # noqa: BLE001 — weight is enrichment, not load-bearing
        _log.exception("decision_funnel.position_context_weight_failed", ticker=ticker)
    if weight_pct is not None:
        lines.append(
            f"- {ticker} is ~{weight_pct:.2f}% of the tradeable securities book "
            f"(~{_fmt_usd(total_usd)} total)."
        )

    lines.append(
        f"- FRAMING: the client ALREADY OWNS {ticker}. A BUY is a TOP-UP of "
        "this existing position and a SELL is a trim/exit of it — NEVER frame "
        "or size the decision as 'initiating a new position'."
    )
    return lines


def _flag_matches_ticker(payload: dict, kind: str, ticker: str) -> bool:
    if str(payload.get("ticker") or "").strip().upper() == ticker:
        return True
    primary = str(payload.get("primary_field") or "").strip().upper()
    return primary == f"HOLDING.{ticker}"


def _flag_lines(session: Session, *, user_id: str, ticker: str) -> list[str]:
    """Active monitor flags that reference the ticker, with their reasons."""
    from sqlalchemy import select

    from argosy.state.models import MonitorFlag

    try:
        flags = (
            session.execute(
                select(MonitorFlag).where(
                    MonitorFlag.user_id == user_id,
                    MonitorFlag.status == "active",
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 — enrichment must not crash the packet
        _log.exception("decision_funnel.position_context_flags_failed", ticker=ticker)
        return []

    lines: list[str] = []
    for f in flags:
        try:
            payload = json.loads(f.payload or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if not _flag_matches_ticker(payload, f.kind or "", ticker):
            continue
        surfaced = ""
        if getattr(f, "surfaced_at", None) is not None:
            surfaced = f", surfaced {f.surfaced_at.date().isoformat()}"
        rationale = str(payload.get("rationale_md") or "").strip()
        if len(rationale) > _RATIONALE_EXCERPT_CHARS:
            rationale = rationale[:_RATIONALE_EXCERPT_CHARS].rstrip() + " …"
        status_bit = ""
        if payload.get("thesis_status"):
            status_bit = f" thesis_status={payload['thesis_status']};"
        lines.append(
            f"- {f.kind} (severity={f.severity}{surfaced}):{status_bit} "
            f"{rationale or '(no rationale recorded)'}"
        )

    if not lines:
        return []
    return [
        f"ACTIVE MONITOR FLAGS — {ticker}:",
        *lines,
        "- Any BUY recommendation must EXPLICITLY adjudicate adding exposure "
        "AGAINST these active flags (e.g. buying more of a name under a "
        "weakened-thesis flag) — address the conflict in the verdict, never "
        "ignore it.",
    ]


def build_position_context(session: Session, *, user_id: str, ticker: str) -> str:
    """The full position + active-flag context block for one ticker."""
    t = (ticker or "").strip().upper()
    if not t:
        return ""
    blocks = [_position_lines(session, user_id=user_id, ticker=t)]
    flag_block = _flag_lines(session, user_id=user_id, ticker=t)
    if flag_block:
        blocks.append(flag_block)
    return "\n".join("\n".join(b) for b in blocks if b)


async def position_context_block(*, user_id: str, ticker: str) -> str:
    """Async wrapper for the stage-3 packet (mirrors ``estate_kb`` usage)."""
    from argosy.state import db as db_mod

    async with db_mod.get_session() as session:
        return await session.run_sync(
            lambda s: build_position_context(s, user_id=user_id, ticker=ticker)
        )


__all__ = ["build_position_context", "position_context_block"]
