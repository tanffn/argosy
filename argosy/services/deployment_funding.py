"""Cash FUNDING breakdown for the deploy surface — WHERE the money sits.

Live incident (2026-07-06): /deploy-cash told the client HOW MUCH to deploy
($170,980 detected idle cash) but not WHERE it sits. The pool was Leumi USD
$144,941 + Leumi NIS ~₪58,945 (~$20,040) + Schwab USD $5,893; the client
executed every fill from Leumi USD alone, went ~$16.4k negative, and nearly
sold $20k of fresh positions to cover — when a simple NIS→USD conversion was
the fix.

This module derives, purely from the latest portfolio snapshot (no LLM):

* the CASH FUNDING TABLE — one row per (account, currency): local balance +
  USD equivalent at snapshot FX; and
* REQUIRED ACTIONS — deterministic strings the client must do BEFORE (or
  instead of) blindly filling from one account: "Convert ~NIS X → USD at
  Leumi", "Wire $Y from Schwab", and — post-incident — "convert to cover" when
  an account balance is already NEGATIVE.

Cash rows are identified by the SAME classifier the deployable-cash total
uses (``allocation_engine.is_cash_position``), and each row's USD equivalent
is the same ``usd_value_k * 1000`` the total sums — so the funding table sums
to the deployable number shown to the client BY CONSTRUCTION. When the deploy
amount is an explicit override (``cash_usd=...``) that differs from the pool,
both numbers are shown and the difference labelled, never silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.services.allocation_engine import is_cash_position

# Balances/differences below this are display noise, not funding facts.
_EPSILON_USD = 0.01


@dataclass(frozen=True)
class FundingRow:
    """Cash in ONE account+currency bucket."""

    account: str  # snapshot `location`, e.g. "Leumi", "schwab 876"
    currency: str  # e.g. "USD", "NIS"
    balance: float  # in `currency` (local units)
    usd_equiv: float  # snapshot USD (usd_value_k * 1000 — same as the total)


@dataclass(frozen=True)
class FundingBreakdown:
    rows: tuple[FundingRow, ...]
    total_usd: float  # sum of rows' usd_equiv == tradeable_holdings cash
    required_actions: tuple[str, ...]
    note: str = ""  # non-empty only when total != deploy amount


def _fmt_usd(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_local(currency: str, v: float) -> str:
    return f"{currency} {v:,.0f}"


def derive_cash_funding(
    snapshot,
    deploy_amount_usd: float,
    *,
    discovery_reserve_usd: float = 0.0,
) -> FundingBreakdown:
    """Derive the funding table + required actions from a PortfolioSnapshot.

    Pure derivation — no LLM, no live calls. ``deploy_amount_usd`` is the
    amount the deploy surface is telling the client to place (the detected
    idle cash by default, or an explicit override).
    """
    fx_usd_nis = getattr(snapshot, "fx_usd_nis", None)

    # --- funding table: one row per (account, currency), cash rows only ----
    buckets: dict[tuple[str, str], list[float]] = {}
    order: list[tuple[str, str]] = []
    for p in getattr(snapshot, "positions", []) or []:
        if not is_cash_position(p):
            continue
        account = (getattr(p, "location", "") or "").strip() or "(unknown account)"
        currency = (getattr(p, "currency", "") or "").strip().upper() or "USD"
        usd = float(getattr(p, "usd_value_k", None) or 0.0) * 1000.0
        local = getattr(p, "current_value_local", None)
        balance = float(local) if local is not None else usd
        key = (account, currency)
        if key not in buckets:
            buckets[key] = [0.0, 0.0]
            order.append(key)
        buckets[key][0] += balance
        buckets[key][1] += usd

    rows = tuple(
        FundingRow(
            account=acct, currency=cur,
            balance=round(buckets[(acct, cur)][0], 2),
            usd_equiv=round(buckets[(acct, cur)][1], 2),
        )
        for acct, cur in order
    )
    total_usd = round(sum(r.usd_equiv for r in rows), 2)

    # --- required actions (deterministic) -----------------------------------
    actions: list[str] = []

    # (a) An account already NEGATIVE (the live incident's post-fill state):
    # cover it FIRST — from same-account other-currency cash when available.
    for r in rows:
        if r.usd_equiv >= -_EPSILON_USD:
            continue
        cover_srcs = [
            s for s in rows
            if s.account == r.account and s.currency != r.currency
            and s.usd_equiv > _EPSILON_USD
        ]
        deficit = -r.usd_equiv
        if cover_srcs:
            src = max(cover_srcs, key=lambda s: s.usd_equiv)
            local_needed = ""
            if src.currency == "NIS" and fx_usd_nis:
                local_needed = f" (~NIS {deficit * float(fx_usd_nis):,.0f})"
            actions.append(
                f"{r.account} {r.currency} balance is NEGATIVE "
                f"({_fmt_usd(r.usd_equiv)}): convert {src.currency} to cover "
                f"{_fmt_usd(r.usd_equiv)}{local_needed} at {r.account} before "
                f"anything else."
            )
        else:
            actions.append(
                f"{r.account} {r.currency} balance is NEGATIVE "
                f"({_fmt_usd(r.usd_equiv)}): fund it (transfer/convert from "
                f"another account) before anything else."
            )

    # (b) Shortfall vs the single largest USD account: the client cannot fill
    # the whole deploy from one account — say exactly which conversions/wires
    # assemble the rest. Fires whenever largest-USD-account < deploy amount.
    usd_rows = [r for r in rows if r.currency == "USD" and r.usd_equiv > _EPSILON_USD]
    largest = max(usd_rows, key=lambda r: r.usd_equiv, default=None)
    largest_usd = largest.usd_equiv if largest is not None else 0.0
    if deploy_amount_usd > _EPSILON_USD and largest_usd < deploy_amount_usd - _EPSILON_USD:
        for r in rows:
            if r.usd_equiv <= _EPSILON_USD:
                continue
            if r.currency != "USD":
                local_txt = _fmt_local(r.currency, r.balance)
                actions.append(
                    f"Convert ~{local_txt} -> USD (~{_fmt_usd(r.usd_equiv)}) at "
                    f"{r.account} before executing (fills settle T+2)."
                )
            elif largest is None or r.account != largest.account:
                actions.append(
                    f"Wire {_fmt_usd(r.usd_equiv)} from {r.account} (or execute "
                    f"that portion of the buys from {r.account} directly)."
                )
        if largest is not None:
            actions.append(
                f"Largest single USD account ({largest.account}) holds only "
                f"{_fmt_usd(largest_usd)} of the {_fmt_usd(deploy_amount_usd)} "
                f"deploy — do NOT fill everything from it."
            )

    # --- reconcile funding pool vs deploy amount ----------------------------
    note = ""
    diff = round(total_usd - float(deploy_amount_usd), 2)
    if abs(diff) > _EPSILON_USD:
        note = (
            f"Funding pool ({_fmt_usd(total_usd)} across "
            f"{len(rows)} cash balance(s)) differs from the deploy amount "
            f"({_fmt_usd(float(deploy_amount_usd))}) by {_fmt_usd(diff)} — "
            f"the deploy amount is an explicit override or the snapshot moved; "
            f"both are shown."
        )

    if discovery_reserve_usd and float(discovery_reserve_usd) > _EPSILON_USD:
        from argosy.services.discovery_reserve import labeled_exclusion

        excl = labeled_exclusion(float(discovery_reserve_usd))
        note = f"{note} {excl}".strip() if note else excl

    return FundingBreakdown(
        rows=rows, total_usd=total_usd,
        required_actions=tuple(actions), note=note,
    )


__all__ = ["FundingRow", "FundingBreakdown", "derive_cash_funding"]
