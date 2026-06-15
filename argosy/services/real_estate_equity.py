"""Per-property real-estate net equity, in USD thousands.

The TSV's "Real estate details" section carries a Home row and a Loan row per
property. Net equity = home value − outstanding loan, FX-converted. The rule
below is the codex net-equity review's verdict (see
``tmp_review/codex_realestate_verdict.txt``):

  * read amounts from COL_PRICE (c7) — the "Current Value" column (c9) is
    unreliable (Atlanta's c9 is 0 while c7 holds the real $318k);
  * net_local = home_c7 − abs(loan_c7);
  * convert to USD thousands with rate 1 for USD, the EUR rate for EUR, the
    NIS rate for NIS (rates are "USD to X", so X→USD divides);
  * warn — never silently compute — when a property's data is too ambiguous
    to trust (missing pair, missing FX, zero home value).

This is net-WORTH context for the Portfolio "where is our money" view. It is
deliberately kept OUT of the investable allocation-vs-target (a primary
residence is not investable capital) — per Ariel's decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PropertyEquity:
    name: str
    currency: str
    home_local: float | None
    loan_local: float | None
    net_local: float | None
    net_usd_k: float | None
    warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RealEstateEquity:
    properties: tuple[PropertyEquity, ...]
    total_net_usd_k: float
    warnings: tuple[str, ...] = field(default=())


def _rate_for(currency: str, *, fx_usd_nis: float | None, fx_usd_eur: float | None) -> float | None:
    """USD-per-unit divisor: a 'USD to X' rate converts X→USD by dividing."""
    c = (currency or "").strip().upper()
    if c == "USD":
        return 1.0
    if c == "EUR":
        return fx_usd_eur
    if c == "NIS":
        return fx_usd_nis
    return None


def compute_real_estate_equity(
    real_estate: list,
    *,
    fx_usd_nis: float | None,
    fx_usd_eur: float | None,
    loan_override: dict[str, float] | None = None,
) -> RealEstateEquity:
    """Pair Home/Loan rows by (property name, currency) and compute net equity.

    ``real_estate`` is a list of objects with ``.location`` (property name),
    ``.currency``, ``.role`` ('Home'|'Loan'), ``.value_local`` (from c7).

    ``loan_override`` maps property name → remaining-to-pay that SUPERSEDES the
    snapshot Loan row. This is how the canonical payment ledger
    (``real_estate_ledger``) drives the displayed balance: the snapshot Loan is a
    static, re-import-clobbered figure, so when a property has a payment ledger we
    pass its computed remaining here and ignore the stale snapshot row.
    """
    loan_override = loan_override or {}
    # Group rows by (name, currency).
    by_key: dict[tuple[str, str], dict[str, float | None]] = {}
    order: list[tuple[str, str]] = []
    for r in real_estate:
        name = (getattr(r, "location", "") or "").strip()
        ccy = (getattr(r, "currency", "") or "").strip().upper()
        role = (getattr(r, "role", "") or "").strip().lower()
        if not name or role not in ("home", "loan"):
            continue
        key = (name, ccy)
        if key not in by_key:
            by_key[key] = {"home": None, "loan": None}
            order.append(key)
        by_key[key][role] = getattr(r, "value_local", None)

    properties: list[PropertyEquity] = []
    top_warnings: list[str] = []
    total = 0.0
    for name, ccy in order:
        pair = by_key[(name, ccy)]
        home = pair["home"]
        ledger_remaining = loan_override.get(name)
        loan = ledger_remaining if ledger_remaining is not None else pair["loan"]
        warns: list[str] = []
        if home is None:
            warns.append("missing Home row")
        if loan is None:
            warns.append("missing Loan row (assumed unencumbered)")
        elif ledger_remaining is not None:
            warns.append("remaining-to-pay from payment ledger (not the snapshot)")
        rate = _rate_for(ccy, fx_usd_nis=fx_usd_nis, fx_usd_eur=fx_usd_eur)
        if rate is None or rate == 0:
            warns.append(f"no FX rate for {ccy or '(blank)'}; cannot convert to USD")

        net_local: float | None = None
        net_usd_k: float | None = None
        if home is not None:
            net_local = home - abs(loan) if loan is not None else home
            if rate:
                net_usd_k = round(net_local / rate / 1000.0, 2)
                total += net_usd_k
        else:
            warns.append("home value missing; net equity not computed")

        properties.append(PropertyEquity(
            name=name, currency=ccy, home_local=home, loan_local=loan,
            net_local=net_local, net_usd_k=net_usd_k, warnings=tuple(warns),
        ))

    return RealEstateEquity(
        properties=tuple(properties),
        total_net_usd_k=round(total, 2),
        warnings=tuple(top_warnings),
    )


__all__ = ["PropertyEquity", "RealEstateEquity", "compute_real_estate_equity"]
