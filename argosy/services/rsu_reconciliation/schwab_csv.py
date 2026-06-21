"""Schwab Equity Awards Center CSV parser.

The Schwab Equity Awards Center exports a CSV with a quirky two-tier shape:

  * "Sale" rows carry the gross/net summary of a share sale: ``Date``,
    ``Action='Sale'``, ``Quantity``, ``FeesAndCommissions``, ``Amount``.
  * One or more *RS* sub-rows immediately follow each Sale (``Type='RS'``)
    and break the sale down per-lot/grant: ``Shares``, ``SalePrice``,
    ``VestDate``, ``GrossProceeds``, ``TotalCostBasis``, ``RealizedGainLoss``,
    optionally ``Taxes``.
  * "Forced Disbursement" / "Cash Disbursement" rows: ``Action`` matches,
    ``Amount`` is negative (money leaving Schwab → going to bank). We model
    these as the canonical "money out" event to reconcile against bank.
  * Other actions (Lapse, Deposit, Adjustment, Dividend, Tax Withholding,
    ESPP) are not modelled; their counts are surfaced via
    ``SchwabReport.unparsed_actions`` so callers can see what was skipped.

This module is read-only and pure-Python: ``Decimal`` for money arithmetic,
``float`` only at the dataclass boundary.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchwabSaleLot:
    """One per-lot RS sub-row attached to a Sale."""
    shares: int
    sale_price_usd: float
    vest_date: date | None
    gross_proceeds_usd: float | None
    cost_basis_usd: float | None
    realized_gain_usd: float | None
    taxes_usd: float
    holding_period: str | None = None     # 'LONG TERM' | 'SHORT TERM' | None
    grant_id: str | None = None           # Schwab GrantId on the RS sub-row
    award_date: date | None = None        # AwardDate on the RS sub-row (often blank)


@dataclass(frozen=True)
class SchwabSale:
    """A top-level Sale row plus its RS lot breakdown."""
    date: date
    symbol: str
    quantity_shares: int
    gross_usd: float
    fees_usd: float
    lots: tuple[SchwabSaleLot, ...]
    total_taxes_usd: float
    net_usd: float


@dataclass(frozen=True)
class SchwabDisbursement:
    """A Forced/Cash Disbursement row — money leaving Schwab to the bank."""
    date: date
    amount_usd: float          # positive magnitude
    action: str                # 'Forced Disbursement' | 'Cash Disbursement'


@dataclass(frozen=True)
class SchwabVestEvent:
    """An RSU vest event — restriction lapsed on `date`, shares now
    transferable. Extracted from `Lapse` action rows + their continuation
    sub-row. The paired `Deposit` row that typically follows on T+1 with
    the same AwardId + count is treated as redundant (same data, settle
    date instead of vest date).
    """
    date: date                    # the vest date (restriction lapsed)
    symbol: str                   # 'NVDA'
    grant_id: str                 # Schwab AwardId, e.g. '289173'
    shares_vested: int            # gross qty before tax withholding
    shares_withheld: int          # withheld for taxes
    shares_net: int               # deposited into account
    fmv_per_share_usd: float      # FMV at vest
    award_date: date | None       # original grant date


@dataclass
class SchwabReport:
    sales: list[SchwabSale] = field(default_factory=list)
    disbursements: list[SchwabDisbursement] = field(default_factory=list)
    vest_events: list[SchwabVestEvent] = field(default_factory=list)
    unparsed_actions: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


_DISBURSEMENT_ACTIONS = {"Forced Disbursement", "Cash Disbursement"}


def _money(s: str | None) -> Decimal | None:
    """Parse a Schwab money cell. Strips ``$`` / ``,`` / leading sign space."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    neg = False
    if s.startswith("-"):
        neg = True
        s = s[1:].lstrip()
    if s.startswith("$"):
        s = s[1:]
    s = s.replace(",", "").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def _int(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if not s:
        return None
    try:
        return int(Decimal(s))
    except (InvalidOperation, ValueError):
        return None


def _date(s: str | None) -> date | None:
    """Parse Schwab's MM/DD/YYYY date column."""
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def _f(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def _f0(d: Decimal | None) -> float:
    return 0.0 if d is None else float(d)


# ---------------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------------


def parse_csv(path: Path) -> SchwabReport:
    """Parse a Schwab Equity Awards Center transactions CSV.

    Recognised actions:
      * ``Sale``                — top-level summary row; emits a SchwabSale
        with the immediately following ``Type='RS'`` rows folded in as lots.
      * ``Forced Disbursement`` /
        ``Cash Disbursement``   — emits a SchwabDisbursement.
      * ``Lapse``               — restricted-stock-lapse (vest) event; emits
        a SchwabVestEvent. The continuation sub-row carries `AwardId`,
        `AwardDate`, `FairMarketValuePrice`, `SharesSoldWithheldForTaxes`,
        `NetSharesDeposited`.
      * ``Deposit`` (Description='RS') — settle-side counterpart to a Lapse
        row (typically T+1). Counted as `Deposit` in unparsed_actions for
        visibility but does not emit a separate vest event (would duplicate
        the Lapse). Deposits with non-RS descriptions (e.g. 'ESPP') are
        also counted, not modelled.
      * Anything else           — counted into ``unparsed_actions`` for
        operator visibility (so we don't silently drop them).
    """
    report = SchwabReport()

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    pending_sale: dict | None = None
    pending_lots: list[SchwabSaleLot] = []
    pending_lapse: dict | None = None

    def _flush_sale() -> None:
        """Close out the currently-pending sale (if any) into the report."""
        nonlocal pending_sale, pending_lots
        if pending_sale is None:
            return
        gross = pending_sale["gross"]
        fees = pending_sale["fees"]
        total_taxes = sum((Decimal(str(lot.taxes_usd)) for lot in pending_lots),
                          start=Decimal("0"))
        net = gross - fees - total_taxes
        report.sales.append(SchwabSale(
            date=pending_sale["date"],
            symbol=pending_sale["symbol"],
            quantity_shares=pending_sale["quantity"],
            gross_usd=float(gross),
            fees_usd=float(fees),
            lots=tuple(pending_lots),
            total_taxes_usd=float(total_taxes),
            net_usd=float(net),
        ))
        pending_sale = None
        pending_lots = []

    def _drop_pending_lapse() -> None:
        """Discard the currently-pending Lapse row if its continuation
        sub-row never arrived. Defensive — real CSVs always pair the two,
        but guarding against truncated exports.

        Codex IMPORTANT (commit #7 review): increment an observability
        counter when this happens so a stuck-pipeline state is visible
        rather than silent."""
        nonlocal pending_lapse
        if pending_lapse is not None:
            report.unparsed_actions["Lapse_no_continuation"] = (
                report.unparsed_actions.get("Lapse_no_continuation", 0) + 1
            )
        pending_lapse = None

    for row in rows:
        action = (row.get("Action") or "").strip()
        type_ = (row.get("Type") or "").strip()

        # RS sub-row: attach to the currently-pending sale (if there is one).
        # If there is no pending sale, it's a stray lot row (e.g. a continuation
        # row of a Deposit/Lapse) — skip silently; those events live in
        # unparsed_actions via their parent action row.
        if not action and type_ == "RS":
            if pending_sale is None:
                continue
            shares = _int(row.get("Shares")) or 0
            sale_price = _money(row.get("SalePrice"))
            vest = _date(row.get("VestDate"))
            gross_proceeds = _money(row.get("GrossProceeds"))
            cost_basis = _money(row.get("TotalCostBasis"))
            realized = _money(row.get("RealizedGainLoss"))
            taxes = _money(row.get("Taxes"))
            holding = (row.get("HoldingPeriod") or "").strip() or None
            grant_id = (row.get("GrantId") or "").strip() or None
            award_date = _date(row.get("AwardDate"))
            pending_lots.append(SchwabSaleLot(
                shares=shares,
                sale_price_usd=_f0(sale_price),
                vest_date=vest,
                gross_proceeds_usd=_f(gross_proceeds),
                cost_basis_usd=_f(cost_basis),
                realized_gain_usd=_f(realized),
                taxes_usd=_f0(taxes),
                holding_period=holding,
                grant_id=grant_id,
                award_date=award_date,
            ))
            continue

        # Continuation rows with no action/type. Two cases now:
        # 1. Subordinate detail for a pending Lapse → emit the SchwabVestEvent.
        # 2. Subordinate detail for some other action (Deposit/ESPP/...) —
        #    skip, the parent row counted in unparsed_actions.
        if not action:
            if pending_lapse is not None:
                award_id = (row.get("AwardId") or "").strip()
                award_date = _date(row.get("AwardDate"))
                fmv_per_share = _money(row.get("FairMarketValuePrice"))
                shares_withheld = _int(
                    row.get("SharesSoldWithheldForTaxes")
                ) or 0
                shares_net = _int(row.get("NetSharesDeposited")) or 0
                if award_id and fmv_per_share is not None:
                    report.vest_events.append(SchwabVestEvent(
                        date=pending_lapse["date"],
                        symbol=pending_lapse["symbol"],
                        grant_id=award_id,
                        shares_vested=pending_lapse["quantity"],
                        shares_withheld=shares_withheld,
                        shares_net=shares_net,
                        fmv_per_share_usd=float(fmv_per_share),
                        award_date=award_date,
                    ))
                # Whether or not the sub-row was parseable, the Lapse is
                # done — clear it so the next event starts fresh.
                pending_lapse = None
            continue

        # Any new top-level action ends the previous Sale's lot stream and
        # discards an unmatched Lapse (continuation never came).
        _flush_sale()
        _drop_pending_lapse()

        if action == "Sale":
            pending_sale = {
                "date": _date(row.get("Date")),
                "symbol": (row.get("Symbol") or "").strip(),
                "quantity": _int(row.get("Quantity")) or 0,
                "gross": _money(row.get("Amount")) or Decimal("0"),
                "fees": _money(row.get("FeesAndCommissions")) or Decimal("0"),
            }
            pending_lots = []
            continue

        if action in _DISBURSEMENT_ACTIONS:
            d = _date(row.get("Date"))
            amt = _money(row.get("Amount")) or Decimal("0")
            # Disbursements are written as negatives ('-$207,538.02'); we
            # store the magnitude so downstream code reads it as the credit
            # we expect to see in Leumi.
            report.disbursements.append(SchwabDisbursement(
                date=d,
                amount_usd=float(abs(amt)),
                action=action,
            ))
            continue

        if action == "Lapse":
            # Open a pending Lapse; the next continuation row completes it.
            pending_lapse = {
                "date": _date(row.get("Date")),
                "symbol": (row.get("Symbol") or "").strip(),
                "quantity": _int(row.get("Quantity")) or 0,
            }
            continue

        # Everything else — Deposit, Adjustment, Dividend, Tax Withholding,
        # ESPP, etc. We surface counts so the operator can spot actions we
        # don't yet model. Deposit-paired-with-Lapse is intentionally
        # counted here (we don't emit a duplicate vest event from it).
        report.unparsed_actions[action] = report.unparsed_actions.get(action, 0) + 1

    # Flush any sale at the end of the file.
    _flush_sale()
    _drop_pending_lapse()

    return report
