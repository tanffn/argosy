"""RAW holdings block for the adversarial codex second opinion.

The codex reviewer re-derives net worth / US-situs estate / NVDA weight from
THESE rows (its own logic, blind to how the pipeline computed them) and flags
any pipeline-claimed number it cannot reproduce. This is the adversarial
contract: independent re-derivation from raw inputs, not consistency-checking
the prose against a shared manifest.

Single-sourced here so the production orchestrator dispatch and any
out-of-flow re-review script feed codex the SAME packet (they previously
mirrored each other line-for-line — a drift hazard).

The packet includes the REAL-ESTATE EQUITY rows (owner-estimate property
values from the ingested owner sheet ``real_estate_json`` + payment-ledger
overrides) because the Wealth Dashboard's "total net worth incl. real estate"
headline is built on them — without these rows that headline is structurally
UNVERIFIABLE to a raw-data auditor (codex draft-73 BLOCKER).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def build_raw_holdings_block(session: "Session", user_id: str) -> str:
    """Assemble the raw-holdings audit packet for ``user_id``'s latest
    snapshot. Returns "" (reviewer degrades gracefully) on any failure."""
    try:
        import json

        from sqlalchemy import select as _select

        from argosy.state.models import PortfolioSnapshotRow

        snap = session.execute(
            _select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is None:
            return ""
        positions = json.loads(snap.positions_json or "[]")
        fx = snap.fx_usd_nis
        lines = [
            f"Snapshot stored FX USD/NIS = {fx} (snapshot id={snap.id}; "
            f"usd_value_k is THOUSANDS of USD).",
            "FX CONVENTION: net worth, US-situs estate exposure, and EVERY "
            "other USD→NIS translation use the Bank of Israel CURRENT daily "
            "representative USD/NIS rate (stated as 'USD/NIS' in the "
            "PIPELINE-CLAIMED HEADLINE NUMBERS block), which may differ "
            "slightly from this snapshot's stored rate. Reproduce net worth "
            "AND the US-situs estate exposure at the BOI current rate — NOT "
            "this snapshot's stored rate — and only flag a USD→NIS figure "
            "(net_worth, us_situs_estate, …) as DIVERGES if it disagrees AT "
            "THE BOI CURRENT RATE. (The US-situs USD basis and instrument "
            "set are identical; a NIS gap that vanishes at the BOI rate is "
            "an FX-convention artifact, not a divergence.)",
            # The `details` cell is often Hebrew (mojibake on a cp1252 hop),
            # which strips the exchange/domicile signal a US-situs
            # classification needs. `instrument_name` is the OBJECTIVE
            # plain-English identity (e.g. "iShares Core S&P 500 (UCITS)",
            # "Schwab US Dividend Equity ETF") from the canonical reference —
            # raw reference data, NOT Argosy's US-situs conclusion — so the
            # reviewer classifies domicile correctly while still re-deriving
            # the US-situs total independently. (Run-114 codex under-counted
            # US-situs by ~$40K because it had only garbled tickers to go on.)
            "symbol | instrument_name | broker_location | currency | "
            "asset_type | usd_value_k | details",
        ]
        from argosy.services.instrument_reference import name_for

        for p in positions:
            if not isinstance(p, dict):
                continue
            _sym = (p.get("symbol") or "").strip()
            _name = name_for(_sym, p.get("details") or "") or "-"
            lines.append(
                f"{_sym or '-'} | {_name} | {p.get('location') or '-'} | "
                f"{p.get('currency') or '-'} | {p.get('asset_type') or '-'} | "
                f"{p.get('usd_value_k')} | {(p.get('details') or '')[:60]}"
            )
        re_lines = _real_estate_lines(session, user_id, snap)
        if re_lines:
            lines += [""] + re_lines
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — reviewer degrades gracefully
        log.warning("raw_holdings_block.build_failed error=%s", exc)
        return ""


def _real_estate_lines(session: "Session", user_id: str, snap) -> list[str]:
    """REAL-ESTATE EQUITY section — the raw rows behind the dashboard's
    'total net worth incl. real estate' basis, explicitly labeled as
    owner-estimate (unaudited). Empty when the snapshot has no rows."""
    try:
        from argosy.services.net_worth_bases import (
            real_estate_equity_for_snapshot,
            real_estate_stub_usd_k,
        )

        eq = real_estate_equity_for_snapshot(
            snapshot=snap, session=session, user_id=user_id
        )
        if eq is None or not eq.properties:
            return []
        stub_k = real_estate_stub_usd_k(snap)
        out = [
            "REAL-ESTATE EQUITY (per-property; OWNER-ESTIMATE values — "
            "UNAUDITED; source: ingested owner sheet real_estate_json on this "
            "snapshot + payment-ledger/impairment overrides where noted):",
            "property | currency | home_value_local | loan_local | "
            "net_equity_usd_k | notes",
        ]
        for p in eq.properties:
            notes = "; ".join(p.warnings) or "-"
            out.append(
                f"{p.name} | {p.currency} | {p.home_local} | {p.loan_local} | "
                f"{p.net_usd_k} | {notes}"
            )
        out.append(
            f"Real-estate net equity total: {eq.total_net_usd_k} USD-k. "
            "BASIS BRIDGE: the Wealth Dashboard 'Total net worth (incl. real "
            "estate)' = investable holdings (position rows above, MINUS the "
            f"legacy real-estate stub rows totalling {round(stub_k, 2)} USD-k "
            "already counted there) + this net-equity total, at the BOI "
            "current USD/NIS rate. The property values are OWNER ESTIMATES "
            "(unaudited) — the incl.-real-estate headline is owner-estimate-"
            "based and is DISTINCT from the audited liquid/investable "
            "figures, which exclude it. Audit the bridge arithmetic; do not "
            "treat the owner estimates themselves as broker-audited."
        )
        return out
    except Exception as exc:  # noqa: BLE001 — packet degrades to positions-only
        log.warning("raw_holdings_block.real_estate_failed error=%s", exc)
        return []


__all__ = ["build_raw_holdings_block"]
