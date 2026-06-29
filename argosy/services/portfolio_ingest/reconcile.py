"""Reconcile an ingested portfolio snapshot against the RAW Leumi source.

The lesson behind this module: internal consistency is not correctness. A
snapshot can sum to 100%, validate as a typed object, and still be wrong —
missing a whole cash currency, carrying a stale/mislabeled position, or
collapsing two securities onto one symbol. The only defense is to diff the
ingested data against the authoritative external source (the bank's own XLS
exports + cash statements) and FAIL LOUD on any mismatch.

This runs on the XLS-pair path (where the raw portfolio XLS is in hand) and is
exposed for a standalone verification sweep. It is deliberately independent of
the synthesis code it checks.
"""
from __future__ import annotations

# Tolerance for per-position USD value drift (price moves between exports);
# qty must match exactly.
_VALUE_K_TOL = 0.5  # $0.5K


def _norm(name: str) -> str:
    return (name or "").strip()


def reconcile_leumi_against_xls(
    *,
    snapshot_positions: list,
    xls_positions: list,
    osh_closing_nis: float | None,
    usd_closing: float | None,
    fx_usd_nis: float,
) -> list[str]:
    """Return a list of discrepancy strings (empty == clean) between the
    ingested snapshot's Leumi section and the raw Leumi sources.

    ``snapshot_positions`` are the parsed snapshot positions (objects with
    .location/.currency/.asset_type/.symbol/.shares/.usd_value_k). ``xls_positions``
    are LeumiPortfolioPosition objects (.security_id/.ticker/.name_he/.quantity/
    .holding_value/.holding_value_currency). The XLS holding value is converted
    to USD at ``fx_usd_nis`` (Leumi exports NIS-denominated values since
    mid-2026) before comparison. Cash balances are the authoritative closing
    balances.
    """
    issues: list[str] = []

    leumi = [
        p for p in snapshot_positions
        if (getattr(p, "location", "") or "").lower().startswith("leumi")
    ]
    leumi_noncash = [
        p for p in leumi
        if (getattr(p, "asset_type", "") or "").lower() != "cash"
    ]

    # 1. Position count: every raw XLS holding must be present.
    if len(leumi_noncash) != len(xls_positions):
        issues.append(
            f"Leumi position count mismatch: snapshot has {len(leumi_noncash)} "
            f"non-cash Leumi rows, raw XLS has {len(xls_positions)}."
        )

    # 2. Per-holding qty + value, matched by quantity+value (robust to the
    #    symbol-label problems this very check guards against).
    snap_by_qty: dict[float, list] = {}
    for p in leumi_noncash:
        snap_by_qty.setdefault(float(getattr(p, "shares", 0) or 0), []).append(p)
    for xp in xls_positions:
        qty = float(getattr(xp, "quantity", 0) or 0)
        # Convert the XLS holding value to USD (NIS-denominated since mid-2026).
        if hasattr(xp, "usd_value"):
            want_usd = float(xp.usd_value(fx_usd_nis) or 0)
        else:  # pragma: no cover - defensive for legacy/plain objects
            raw = float(getattr(xp, "holding_value", 0) or 0)
            ccy = (getattr(xp, "holding_value_currency", "USD") or "USD").upper()
            want_usd = raw / max(fx_usd_nis, 0.01) if ccy == "NIS" else raw
        want_k = want_usd / 1000.0
        cands = snap_by_qty.get(qty, [])
        match = next(
            (p for p in cands
             if abs(float(getattr(p, "usd_value_k", 0) or 0) - want_k) <= _VALUE_K_TOL),
            None,
        )
        if match is None:
            issues.append(
                f"Raw XLS holding not found in snapshot: "
                f"{_norm(getattr(xp, 'ticker', None) or getattr(xp, 'name_he', ''))[:30]} "
                f"qty={qty:g} ~${want_k:.1f}K."
            )

    # 3. Both cash currencies present when the bank reports a balance.
    snap_cash_ccy = {
        (getattr(p, "currency", "") or "").upper()
        for p in leumi if (getattr(p, "asset_type", "") or "").lower() == "cash"
    }
    if osh_closing_nis is not None and "NIS" not in snap_cash_ccy:
        issues.append("Leumi NIS cash row missing from snapshot (Osh reports a balance).")
    if usd_closing is not None and "USD" not in snap_cash_ccy:
        issues.append(
            f"Leumi USD cash row MISSING from snapshot — USD statement reports "
            f"${usd_closing / 1000.0:.1f}K."
        )

    # 4. Symbol collisions: the same symbol on two DIFFERENT Leumi holdings
    #    (distinct quantities) — the STOXX-as-'O' class of bug.
    by_symbol: dict[str, set] = {}
    for p in leumi_noncash:
        sym = (getattr(p, "symbol", "") or "").strip()
        if sym:
            by_symbol.setdefault(sym, set()).add(float(getattr(p, "shares", 0) or 0))
    for sym, qtys in by_symbol.items():
        if len(qtys) > 1:
            issues.append(
                f"Symbol collision: '{sym}' maps to {len(qtys)} distinct Leumi "
                f"holdings (quantities {sorted(qtys)}) — likely a mislabel."
            )

    return issues


__all__ = ["reconcile_leumi_against_xls"]
