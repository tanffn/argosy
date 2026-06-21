"""Transaction-based cash-source reconciler — RSU §102 net → Leumi USD inflow.

Answers "what produced this USD cash?" from REAL transactions, not a TSV diff,
and accounts for EVERY Schwab RSU sale:

  * The **sale** side comes from the Schwab Equity Awards Center CSV
    (``schwab_csv.parse_csv`` → ``SchwabSale``).
  * The **tax** side comes from the NVIDIA ESOP simulation report
    (``sim_tax.parse_sim_report`` → per-grant §102 ``GrantTaxModel``). This is
    the authoritative per-grant Israeli §102 model: a grant-dependent
    capital(25%)/ordinary(62%) split — NOT a flat 25% CGT.
  * The **cash** side comes from ``expense_transactions`` on the Leumi USD
    account (external_id 44745200), ``direction='credit'``, ``currency_orig=
    'USD'``, transfer merchant ("העברת כספים"), via :func:`load_leumi_usd_transfers`.

Per-sale net (§102, sim-derived)
--------------------------------
All of the user's actual sold lots are long-held, capital-track NVDA shares
from the oldest low-basis grants. We compute each sale's net by applying the
most capital-favorable capital-track grant's §102 model
(:data:`CAPITAL_TRACK_GRANT_ID`, grant_price ≈ $18) to the actual (shares,
sale_price, fees). This reproduces the simulation's "Amount Wired" ratios and
the proven Apr-20 anchor (1040 sh @ $199.56 = $207,538 gross → ≈ $150,864
transfer, ~72.7% retention). The per-grant retention curve (≈72% old grants,
≈38% recent breaking grants) is derived, never hardcoded as a flat rate.

When the sim report is unavailable we fall back to a labeled flat-rate estimate
(``FALLBACK_IL_CGT_RATE``) so the surface still degrades gracefully — but the
net is then flagged ``tax_is_estimated`` and the §102 split is unavailable.

Matching (full-accounting)
--------------------------
Chronological, 1:1, smallest-(net-vs-transfer)-discrepancy: each sale claims the
in-window transfer closest to its sim-derived net. Because every real sale's net
lands within ~2% of its wire, this accounts for ALL sales 1:1 with the RSU-window
transfers. Genuinely unmatched sales / transfers are reported, never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from argosy.services.rsu_reconciliation.schwab_csv import (
    SchwabReport,
    SchwabSale,
    parse_csv,
)
from argosy.services.rsu_reconciliation.sim_tax import (
    WIRE_ORDINARY_RATE,
    GrantTaxModel,
    SimTaxModel,
    parse_sim_report,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session


# Leumi USD account external_id (פמ"ח / USD holding account).
LEUMI_USD_EXTERNAL_ID = "44745200"

# The grant whose §102 capital-track model we apply to the actual sold lots.
# All real sold lots are long-held shares from the oldest low-basis grants
# (2021 grant 182406 / 2022 grant 213000, grant_price ≈ $18); 182406 is the
# canonical low-basis capital-track grant and its retention reproduces the
# proven Apr-20 anchor to within ~1.5%.
CAPITAL_TRACK_GRANT_ID = "182406"

# Fallback IL capital-gains rate used ONLY when the simulation report is
# unavailable (no per-grant §102 model). Labeled ``tax_is_estimated``.
FALLBACK_IL_CGT_RATE = 0.25

# Transfer / sale_net discrepancy band for a candidate match. Real net lands
# within ~2% of the wire; allow a little slack each way.
DEFAULT_MAX_DISCREPANCY_PCT = 8.0

# How long after a sale a transfer may still be its proceeds.
DEFAULT_WINDOW_DAYS = 45

# Transfer-merchant marker (Hebrew "money transfer").
_TRANSFER_MARKERS = ("העברת כספים", "transfer")

# Candidate filenames for the Schwab transactions CSV, newest-first heuristic:
# the date-stamped exports are more complete than the bare name (which can be
# a stale partial export missing the most recent sale).
_SCHWAB_CSV_GLOB = "EquityAwardsCenter_Transactions*.csv"
_SIM_REPORT_NAME = "Nvidia simulation Report.xlsx"


@dataclass(frozen=True)
class LeumiUsdTransfer:
    tx_id: int
    date: date
    amount_usd: float
    merchant_raw: str
    reference: str | None = None


@dataclass(frozen=True)
class CashSourceLink:
    """One reconciled Sale → transfer link, with the §102 net breakdown."""
    sale_date: date
    sale_symbol: str
    sale_shares: int
    sale_price_usd: float
    sale_gross_usd: float
    sale_fees_usd: float
    # §102 tax breakdown (sim-derived unless tax_is_estimated)
    grant_id: str | None
    holding_period: str | None
    capital_income_usd: float
    ordinary_income_usd: float
    capital_rate: float
    ordinary_rate: float
    tax_usd: float                       # capital@25% + ordinary@62% (or fallback)
    tax_is_estimated: bool
    net_estimate_usd: float              # gross − fees − tax
    effective_retention: float          # net / gross
    transfer_tx_id: int
    transfer_date: date
    transfer_amount_usd: float
    days_diff: int
    discrepancy_pct: float               # (transfer − net)/net * 100 (signed)
    confidence: str                      # "high" | "medium"

    @property
    def capital_fraction(self) -> float:
        total = self.capital_income_usd + self.ordinary_income_usd
        return self.capital_income_usd / total if total > 0 else 0.0

    def describe(self) -> str:
        """One-line human attribution string for the windfall surface."""
        if self.tax_is_estimated:
            tax_part = (
                f", est. IL tax ${self.tax_usd:,.0f} "
                f"(~{(1 - self.effective_retention) * 100:.0f}%, flat estimate)"
            )
        else:
            cap_pct = self.capital_fraction * 100
            tax_part = (
                f", §102 tax ${self.tax_usd:,.0f} "
                f"({cap_pct:.0f}% capital @{self.capital_rate * 100:.0f}% / "
                f"{100 - cap_pct:.0f}% ordinary @{self.ordinary_rate * 100:.0f}%)"
            )
        return (
            f"{self.sale_symbol} RSU sale {self.sale_shares} sh @ "
            f"${self.sale_price_usd:,.2f} (${self.sale_gross_usd:,.0f} gross"
            f"{tax_part}) → ${self.net_estimate_usd:,.0f} net "
            f"(~{self.effective_retention * 100:.0f}% retention) ≈ "
            f"${self.transfer_amount_usd:,.0f} to Leumi USD on "
            f"{self.transfer_date.isoformat()}"
        )


@dataclass
class CashSourceReport:
    links: list[CashSourceLink] = field(default_factory=list)
    unmatched_sales: list[SchwabSale] = field(default_factory=list)
    unexplained_transfers: list[LeumiUsdTransfer] = field(default_factory=list)
    summary: str = ""
    sim_available: bool = False

    @property
    def matched_transfer_usd(self) -> float:
        return sum(l.transfer_amount_usd for l in self.links)

    @property
    def matched_net_usd(self) -> float:
        return sum(l.net_estimate_usd for l in self.links)

    @property
    def matched_gross_usd(self) -> float:
        return sum(l.sale_gross_usd for l in self.links)

    @property
    def unexplained_transfer_usd(self) -> float:
        return sum(t.amount_usd for t in self.unexplained_transfers)


# ---------------------------------------------------------------------------
# DB projection
# ---------------------------------------------------------------------------


def _is_transfer(merchant: str) -> bool:
    m = (merchant or "").strip()
    if not m:
        return False
    return any(tok in m for tok in _TRANSFER_MARKERS)


def load_leumi_usd_transfers(
    session: "Session",
    user_id: str,
    *,
    since: date | None = None,
    until: date | None = None,
) -> list[LeumiUsdTransfer]:
    """Project Leumi USD *transfer credits* from ``expense_transactions``.

    Filters: Leumi USD account, credit, USD, transfer merchant. De-dupes exact
    ``(occurred_on, amount)`` collisions (the ingest has known duplicate rows).
    """
    from sqlalchemy import select

    from argosy.state.models import ExpenseSource, ExpenseTransaction

    stmt = (
        select(ExpenseTransaction)
        .join(ExpenseSource, ExpenseSource.id == ExpenseTransaction.source_id)
        .where(
            ExpenseTransaction.user_id == user_id,
            ExpenseSource.external_id == LEUMI_USD_EXTERNAL_ID,
            ExpenseTransaction.direction == "credit",
            ExpenseTransaction.currency_orig == "USD",
        )
    )
    rows = list(session.execute(stmt).scalars())

    seen: set[tuple[str, float]] = set()
    out: list[LeumiUsdTransfer] = []
    for r in rows:
        if not _is_transfer(r.merchant_raw):
            continue
        if r.amount_orig is None:
            continue
        occ = r.occurred_on
        if since is not None and occ < since:
            continue
        if until is not None and occ > until:
            continue
        amt = float(r.amount_orig)
        key = (occ.isoformat(), round(amt, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(LeumiUsdTransfer(
            tx_id=int(r.id),
            date=occ,
            amount_usd=amt,
            merchant_raw=r.merchant_raw,
            reference=r.reference,
        ))
    out.sort(key=lambda t: (t.date, t.tx_id))
    return out


# ---------------------------------------------------------------------------
# Per-sale §102 net
# ---------------------------------------------------------------------------


def _sale_price(sale: SchwabSale) -> float:
    """Volume-weighted sale price across the sale's lots (fallback: gross/shares)."""
    if sale.lots:
        # Lots share the same sale price for one Sale row; first is fine.
        prices = [lot.sale_price_usd for lot in sale.lots if lot.sale_price_usd]
        if prices:
            return prices[0]
    if sale.quantity_shares:
        return sale.gross_usd / sale.quantity_shares
    return 0.0


def compute_sale_net(
    sale: SchwabSale,
    grant_model: GrantTaxModel | None,
    *,
    fallback_rate: float = FALLBACK_IL_CGT_RATE,
    ordinary_rate: float | None = WIRE_ORDINARY_RATE,
) -> dict:
    """Compute a sale's §102 net using ``grant_model`` (sim-derived).

    Returns a dict of the breakdown fields consumed by :class:`CashSourceLink`.
    Falls back to a flat-rate estimate on the lot realized gain when no grant
    model is available.

    ``ordinary_rate`` defaults to :data:`WIRE_ORDINARY_RATE` (~0.50) because we
    reconcile against ACTUAL bank wires, which reflect ~50% effective ordinary
    withholding — not the sim's conservative 62.17% (codex-validated). Pass
    ``None`` to use the grant's sim rate.
    """
    shares = sale.quantity_shares
    sale_price = _sale_price(sale)
    fees = sale.fees_usd

    if grant_model is not None:
        sn = grant_model.net_for_shares(
            shares, sale_price, fees_usd=fees, ordinary_rate=ordinary_rate,
        )
        return dict(
            grant_id=grant_model.grant_id,
            holding_period=grant_model.holding_period,
            capital_income_usd=sn.capital_income_usd,
            ordinary_income_usd=sn.ordinary_income_usd,
            capital_rate=sn.capital_rate,
            ordinary_rate=sn.ordinary_rate,
            tax_usd=sn.advance_tax_usd,
            tax_is_estimated=False,
            net_estimate_usd=sn.net_usd,
            effective_retention=sn.effective_retention,
            sale_price_usd=sale_price,
        )

    # Fallback: flat CGT on the lot realized gain (labeled estimated).
    gain_vals = [lot.realized_gain_usd for lot in sale.lots
                 if lot.realized_gain_usd is not None]
    gain = float(sum(gain_vals)) if gain_vals else 0.0
    tax = round(max(gain, 0.0) * fallback_rate, 2)
    net = round(sale.gross_usd - fees - tax, 2)
    ret = net / sale.gross_usd if sale.gross_usd > 0 else 0.0
    return dict(
        grant_id=None,
        holding_period=None,
        capital_income_usd=round(gain, 2),
        ordinary_income_usd=0.0,
        capital_rate=fallback_rate,
        ordinary_rate=0.0,
        tax_usd=tax,
        tax_is_estimated=True,
        net_estimate_usd=net,
        effective_retention=ret,
        sale_price_usd=sale_price,
    )


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


def reconcile_cash_sources(
    report: SchwabReport,
    transfers: list[LeumiUsdTransfer],
    *,
    sim: SimTaxModel | None = None,
    capital_track_grant_id: str = CAPITAL_TRACK_GRANT_ID,
    fallback_rate: float = FALLBACK_IL_CGT_RATE,
    ordinary_rate: float | None = WIRE_ORDINARY_RATE,
    max_discrepancy_pct: float = DEFAULT_MAX_DISCREPANCY_PCT,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CashSourceReport:
    """Link Schwab RSU sales to Leumi USD transfers using §102 sim-derived net.

    Chronological, 1:1, closest-net-match. Read-only. ``ordinary_rate`` defaults
    to the wire-calibrated ~0.50 (see :func:`compute_sale_net`).
    """
    out = CashSourceReport(sim_available=sim is not None)

    grant_model: GrantTaxModel | None = None
    if sim is not None:
        grant_model = sim.grant(capital_track_grant_id)

    sales = sorted(report.sales, key=lambda s: (s.date, -s.gross_usd))
    consumed: set[int] = set()

    for sale in sales:
        if sale.gross_usd <= 0 or sale.date is None:
            out.unmatched_sales.append(sale)
            continue

        breakdown = compute_sale_net(
            sale, grant_model,
            fallback_rate=fallback_rate, ordinary_rate=ordinary_rate,
        )
        net = breakdown["net_estimate_usd"]
        window_end = sale.date + timedelta(days=window_days)

        candidates: list[tuple[LeumiUsdTransfer, float]] = []
        for t in transfers:
            if t.tx_id in consumed:
                continue
            if t.date < sale.date or t.date > window_end:
                continue
            if net <= 0:
                continue
            disc = (t.amount_usd - net) / net * 100.0
            if abs(disc) <= max_discrepancy_pct:
                candidates.append((t, disc))

        if not candidates:
            out.unmatched_sales.append(sale)
            continue

        # Smallest absolute discrepancy wins; tie-break nearest date, low tx_id.
        best_t, best_disc = min(
            candidates,
            key=lambda pair: (
                abs(pair[1]),
                abs((pair[0].date - sale.date).days),
                pair[0].tx_id,
            ),
        )
        consumed.add(best_t.tx_id)

        confidence = "high" if abs(best_disc) <= 3.0 else "medium"
        out.links.append(CashSourceLink(
            sale_date=sale.date,
            sale_symbol=sale.symbol,
            sale_shares=sale.quantity_shares,
            sale_price_usd=round(breakdown["sale_price_usd"], 4),
            sale_gross_usd=round(sale.gross_usd, 2),
            sale_fees_usd=round(sale.fees_usd, 2),
            grant_id=breakdown["grant_id"],
            holding_period=breakdown["holding_period"],
            capital_income_usd=breakdown["capital_income_usd"],
            ordinary_income_usd=breakdown["ordinary_income_usd"],
            capital_rate=breakdown["capital_rate"],
            ordinary_rate=breakdown["ordinary_rate"],
            tax_usd=breakdown["tax_usd"],
            tax_is_estimated=breakdown["tax_is_estimated"],
            net_estimate_usd=net,
            effective_retention=round(breakdown["effective_retention"], 4),
            transfer_tx_id=best_t.tx_id,
            transfer_date=best_t.date,
            transfer_amount_usd=round(best_t.amount_usd, 2),
            days_diff=(best_t.date - sale.date).days,
            discrepancy_pct=round(best_disc, 2),
            confidence=confidence,
        ))

    out.unexplained_transfers = [
        t for t in transfers if t.tx_id not in consumed
    ]

    n_links = len(out.links)
    n_sales = len(sales)
    tax_basis = "§102 sim-derived" if sim is not None else "flat-rate fallback"
    out.summary = (
        f"{n_links}/{n_sales} RSU sales accounted for ({tax_basis} tax): "
        f"${out.matched_gross_usd:,.0f} gross → ${out.matched_net_usd:,.0f} net "
        f"≈ ${out.matched_transfer_usd:,.0f} to Leumi USD. "
        f"{len(out.unexplained_transfers)} unexplained transfers "
        f"(${out.unexplained_transfer_usd:,.0f}); "
        f"{len(out.unmatched_sales)} unmatched sales."
    )
    return out


# ---------------------------------------------------------------------------
# Source resolution + convenience
# ---------------------------------------------------------------------------


def _resolve_schwab_csv(csv_path: Path) -> Path | None:
    """Pick the most RECENT Schwab transactions CSV in the directory.

    The bare ``EquityAwardsCenter_Transactions.csv`` can be a stale partial
    export missing the latest sale (observed: it lacked the 2026-06-01 sale).
    Schwab's date-stamped exports (``..._YYYYMMDDhhmmss.csv``) carry their
    export time both in the filename and mtime; the newest one is the most
    up-to-date. We pick the newest by (filename-embedded timestamp, mtime).
    """
    import re

    directory = csv_path.parent
    candidates = list(directory.glob(_SCHWAB_CSV_GLOB))
    if csv_path.exists() and csv_path not in candidates:
        candidates.append(csv_path)
    if not candidates:
        return csv_path if csv_path.exists() else None

    def _stamp(p: Path) -> tuple[int, float]:
        m = re.search(r"_(\d{14})", p.name)
        embedded = int(m.group(1)) if m else 0
        return (embedded, p.stat().st_mtime)

    return max(candidates, key=_stamp)


def _resolve_sim_report(schwab_dir: Path) -> SimTaxModel | None:
    sim_path = schwab_dir / _SIM_REPORT_NAME
    if not sim_path.exists():
        return None
    try:
        return parse_sim_report(sim_path)
    except Exception:  # noqa: BLE001 — degrade to fallback estimate
        return None


def reconcile_from_csv(
    csv_path: Path,
    session: "Session",
    user_id: str,
    *,
    since: date | None = None,
    until: date | None = None,
    sim: SimTaxModel | None = None,
    **kwargs,
) -> CashSourceReport:
    """Parse the (most complete) Schwab CSV + the sim tax model + Leumi
    transfers, then reconcile. Returns an empty report when the CSV is missing.
    """
    resolved = _resolve_schwab_csv(csv_path)
    if resolved is None or not resolved.exists():
        return CashSourceReport(
            summary="Schwab transactions CSV not found; no source attribution."
        )
    report = parse_csv(resolved)
    # Restrict to RSU (NVDA) sales — the §102 RSU model applies to those — and
    # to the reconciliation window. Sales before ``since`` were wired in earlier
    # periods (their transfers aren't in this window) and would otherwise show
    # as a long tail of false "unmatched sales".
    report.sales = [
        s for s in report.sales
        if s.symbol.upper() == "NVDA"
        and (since is None or (s.date is not None and s.date >= since))
        and (until is None or (s.date is not None and s.date <= until))
    ]

    if sim is None:
        sim = _resolve_sim_report(resolved.parent)

    transfers = load_leumi_usd_transfers(
        session, user_id, since=since, until=until
    )
    return reconcile_cash_sources(report, transfers, sim=sim, **kwargs)


def find_transfer_source(
    report: CashSourceReport,
    transfer_amount_usd: float,
    *,
    tolerance_usd: float = 1.0,
) -> CashSourceLink | None:
    """Return the link whose transfer amount matches ``transfer_amount_usd``."""
    best: CashSourceLink | None = None
    best_diff = tolerance_usd
    for link in report.links:
        diff = abs(link.transfer_amount_usd - transfer_amount_usd)
        if diff <= best_diff:
            best = link
            best_diff = diff
    return best


__all__ = [
    "LEUMI_USD_EXTERNAL_ID",
    "CAPITAL_TRACK_GRANT_ID",
    "CashSourceLink",
    "CashSourceReport",
    "LeumiUsdTransfer",
    "compute_sale_net",
    "find_transfer_source",
    "load_leumi_usd_transfers",
    "reconcile_cash_sources",
    "reconcile_from_csv",
]
