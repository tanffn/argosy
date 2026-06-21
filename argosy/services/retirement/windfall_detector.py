"""Windfall detector — reads the TSV diff to find unexplained cash deltas.

Closes Hole #1 from the 2026-05-28 user-guide audit: "How do I tell Argosy
about a 50K bonus / 100K RSU sale?" The answer should not be "you tell us"
— it should be "we noticed and asked."

Trigger gate (per user spec):
  cash_delta_usd_equiv >= $25K   OR   cash_delta_nis >= ₪75K

When the gate fires:
  1. Compute the cash delta vs the prior month's TSV.
  2. Scan the new TSV for share-decreases (status='r' or 'd') and compute
     the dollar value of those sales.
  3. If sales_usd matches cash_delta_usd within 5%: auto-classify as
     "rsu_sale" / "stock_sale". No user dialogue needed.
  4. Otherwise mark as "unclear" — the UI prompts the user via Advisor chat.

The detector returns a ``WindfallEvent`` for downstream consumers
(allocator + UI banner). Returns None when nothing fires.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Windfall flow (added after the 30-gap overhaul).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale


# Cash thresholds in absolute currency units (per user spec).
DEFAULT_THRESHOLD_USD = 25_000.0
DEFAULT_THRESHOLD_NIS = 75_000.0

# Tolerance for matching cash delta to detected sales.
SALE_MATCH_TOLERANCE = 0.05


# TSV column indices (matches update_leumi_tsv.py + the canonical Family
# Finances Status TSV).
COL_STATUS = 0
COL_LOCATION = 1
COL_CURRENCY = 2
COL_TYPE = 3
COL_DETAILS = 4
COL_SYMBOL = 5
COL_SHARES = 6
COL_PRICE = 7
COL_AVG_PRICE = 8
COL_VALUE = 9
COL_KUSD = 10


ClassifiedSource = Literal[
    "rsu_sale",
    "stock_sale",
    "bonus",
    "refund",
    "deposit_unknown",
    "unclear",
]


@dataclass
class Sale:
    """An equity-decrease event detected from the TSV status column."""
    symbol: str
    shares_sold: float
    current_price: float
    value_usd: float


@dataclass
class AllocationLine:
    """One row from the 'Current allocation' table at the bottom of the TSV."""
    asset_class: str
    current_pct: float
    current_k_usd: float
    target_pct: float
    target_k_usd: float
    delta_k_usd: float  # negative = under target (room to add)


@dataclass
class WindfallEvent:
    """A detected cash anomaly that may need allocation."""
    detected_at: datetime
    cash_delta_usd: float
    cash_delta_nis: float
    cash_delta_total_usd_equiv: float
    fx_usd_nis: float
    matching_sales: list[Sale] = field(default_factory=list)
    classified_source: ClassifiedSource = "unclear"
    requires_user_classification: bool = True
    allocation_delta_table: list[AllocationLine] = field(default_factory=list)
    source_tsv: str = ""
    previous_tsv: str | None = None
    # Transaction-based source attribution (real Schwab sale → Leumi transfer
    # links), populated best-effort by ``attribute_cash_source`` when a Schwab
    # CSV + DB session are available. Empty when no link could be established;
    # the surface then degrades to the "unclear" classification above.
    reconciled_source_lines: list[str] = field(default_factory=list)
    reconciled_matched_usd: float = 0.0
    reconciled_unexplained_usd: float = 0.0

    def to_value_with_rationale_dict(self) -> dict[str, ValueWithRationale]:
        """Expose the headline numbers as ValueWithRationale for the UI."""
        return {
            "cash_delta_total_usd_equiv": ValueWithRationale(
                value=round(self.cash_delta_total_usd_equiv, 2),
                unit="USD",
                source_id=None,
                rationale=(
                    f"Sum of USD cash delta (${self.cash_delta_usd:,.0f}) + "
                    f"NIS cash delta (₪{self.cash_delta_nis:,.0f} ≈ "
                    f"${self.cash_delta_nis / max(self.fx_usd_nis, 0.01):,.0f}) "
                    f"between the previous TSV and {Path(self.source_tsv).name}."
                ),
                confidence="high",
            ),
            "classified_source": ValueWithRationale(
                value=self.classified_source,
                unit="enum",
                source_id="argosy_derived",
                rationale=(
                    "Auto-classified by matching cash delta to detected "
                    "equity sales in the new TSV (status='r' or 'd'). "
                    "Matched within 5% → confirmed sale; otherwise unclear."
                ),
                confidence="high" if not self.requires_user_classification else "low",
            ),
            "reconciled_source": ValueWithRationale(
                value=(
                    "; ".join(self.reconciled_source_lines)
                    if self.reconciled_source_lines
                    else "unclear / unexplained residual"
                ),
                unit="text",
                source_id="argosy_derived",
                rationale=(
                    "Transaction-based attribution: each Schwab RSU sale "
                    "(EquityAwardsCenter CSV) taxed per the NVIDIA ESOP §102 "
                    "simulation (grant-dependent capital@25% / ordinary@62% "
                    "split — NOT a flat rate) then linked to the Leumi USD "
                    "transfer it produced (expense_transactions, account 44745200). "
                    f"${self.reconciled_matched_usd:,.0f} of inflow attributed; "
                    f"${self.reconciled_unexplained_usd:,.0f} unexplained "
                    "(salary, FX conversions, or sales outside the CSV window)."
                ),
                confidence="high" if self.reconciled_source_lines else "low",
            ),
        }


def _load_tsv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [row for row in csv.reader(f, delimiter="\t")]


def _parse_num(s: str) -> float:
    try:
        return float(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _find_fx(tsv: list[list[str]]) -> float:
    """Read 'USD to NIS:' header rate (e.g. 2.94161)."""
    for row in tsv[:6]:
        for i, c in enumerate(row):
            if c.strip() == "USD to NIS:" and i + 1 < len(row):
                return _parse_num(row[i + 1])
    return 0.0


def _find_cash_balances(tsv: list[list[str]]) -> tuple[float, float]:
    """Return (leumi_usd_cash, leumi_nis_cash) from the TSV."""
    usd_cash = 0.0
    nis_cash = 0.0
    for row in tsv:
        if len(row) <= COL_KUSD:
            continue
        location = row[COL_LOCATION].strip() if len(row) > COL_LOCATION else ""
        currency = row[COL_CURRENCY].strip() if len(row) > COL_CURRENCY else ""
        asset_type = row[COL_TYPE].strip() if len(row) > COL_TYPE else ""
        if asset_type.lower() != "cash":
            continue
        value = _parse_num(row[COL_VALUE]) if len(row) > COL_VALUE else 0.0
        if "leumi" in location.lower():
            if currency == "USD":
                usd_cash += value
            elif currency == "NIS":
                nis_cash += value
    return usd_cash, nis_cash


def _find_sales(
    tsv: list[list[str]],
    baseline: list[list[str]],
    *,
    fx_usd_nis: float = 3.0,
) -> list[Sale]:
    """Detect share-count decreases between two consecutive TSVs.

    Works for both Leumi rows (status column tracks v/a/r/d) and
    Schwab/Aborad rows (no status — diff shares directly). The status
    column is checked first as a hint, but the underlying mechanism is
    a per-(location, symbol) share-count diff.
    """
    def _is_real_symbol(symbol: str) -> bool:
        """Symbol must look like a ticker (letters + maybe digit/dot/slash).
        Excludes price-like numbers ('35.6') and dashes ('-')."""
        if not symbol or symbol == "-":
            return False
        # Hebrew tickers (מחקה ת"א-200, MSCI World) count
        if re.search(r"[A-Za-z֐-׿]", symbol):
            return True
        return False

    def _holdings_index(rows: list[list[str]]) -> dict[tuple[str, str], tuple[float, float, str]]:
        # key: (location, symbol) -> (shares, price, currency)
        out: dict[tuple[str, str], tuple[float, float, str]] = {}
        for row in rows:
            if len(row) <= COL_KUSD:
                continue
            location = row[COL_LOCATION].strip() if len(row) > COL_LOCATION else ""
            symbol = row[COL_SYMBOL].strip() if len(row) > COL_SYMBOL else ""
            asset_type = row[COL_TYPE].strip() if len(row) > COL_TYPE else ""
            currency = row[COL_CURRENCY].strip() if len(row) > COL_CURRENCY else ""
            if not location or not _is_real_symbol(symbol) or asset_type.lower() == "cash":
                continue
            # Real-estate rows have shares=value; skip them
            if asset_type.lower().startswith("real estate"):
                continue
            shares = _parse_num(row[COL_SHARES])
            price = _parse_num(row[COL_PRICE])
            out[(location, symbol)] = (shares, price, currency)
        return out

    baseline_idx = _holdings_index(baseline)
    current_idx = _holdings_index(tsv)

    sales: list[Sale] = []
    for key, (baseline_shares, _baseline_price, baseline_curr) in baseline_idx.items():
        current_shares = current_idx.get(key, (0.0, 0.0, baseline_curr))[0]
        shares_sold = baseline_shares - current_shares
        if shares_sold <= 0:
            continue
        # Use the CURRENT price (most recent) for the sale-value estimate
        # if the holding still exists; otherwise fall back to baseline.
        current_price = current_idx.get(key, (0, 0, ""))[1] or _baseline_price
        sale_value_local = shares_sold * current_price
        if baseline_curr == "USD":
            value_usd = sale_value_local
        elif baseline_curr == "NIS":
            value_usd = sale_value_local / max(fx_usd_nis, 0.01)
        else:
            value_usd = sale_value_local  # treat unknown as USD-equivalent
        sales.append(Sale(
            symbol=key[1],
            shares_sold=shares_sold,
            current_price=current_price,
            value_usd=value_usd,
        ))
    return sales


def _find_allocation_table(tsv: list[list[str]]) -> list[AllocationLine]:
    """Parse the 'Current allocation:' block at the bottom of the TSV.

    Schema: each data row has `row[1]=asset_class`, `row[2]=pct%`,
    `row[3]=current_k_usd`, `row[4]=target_pct%`, `row[5]=target_k_usd`,
    `row[6]=delta_k_usd`.

    Terminators (in order checked):
      - "Grand Total" row → break
      - Row that started populating data but column-2 lacks "%" → break
        (e.g. "Jan" from NVDA sales history with shares instead of pct)
      - "NVDA Sales History" or "Pensions" section markers → break
    """
    out: list[AllocationLine] = []
    in_block = False
    started_parsing = False  # only break on bad-shape rows after seeing one good row
    for row in tsv:
        if not row:
            continue
        joined = " ".join(c.strip() for c in row[:8]).lower()
        if not in_block:
            if "current allocation" in joined:
                in_block = True
            continue
        # New section markers always terminate the table
        if any(marker in joined for marker in (
            "nvda sales history", "pensions/saving", "real estate details",
        )):
            break
        asset_class = row[1].strip() if len(row) > 1 else ""
        # Grand Total terminates
        if asset_class.lower() == "grand total":
            break
        # Skip header row + blank rows + label-only rows like "Type"
        if asset_class.lower() in ("", "type", "month"):
            if started_parsing:
                # If we already parsed at least one row, a blank/Type/Month
                # marks the table end.
                if asset_class.lower() in ("month",):
                    break
                # blank in the middle: tolerate (continue)
                continue
            continue
        # Data row must have at least 7 columns with a % in col 2
        if len(row) < 7:
            if started_parsing:
                break
            continue
        third_col = row[2].strip() if len(row) > 2 else ""
        if "%" not in third_col:
            if started_parsing:
                break
            continue
        try:
            current_pct = _parse_num(third_col.rstrip("%")) / 100.0
            current_k = _parse_num(row[3])
            target_pct = _parse_num(row[4].rstrip("%")) / 100.0
            target_k = _parse_num(row[5])
            delta_k = _parse_num(row[6])
        except (IndexError, ValueError):
            if started_parsing:
                break
            continue
        out.append(AllocationLine(
            asset_class=asset_class,
            current_pct=current_pct,
            current_k_usd=current_k,
            target_pct=target_pct,
            target_k_usd=target_k,
            delta_k_usd=delta_k,
        ))
        started_parsing = True
    return out


def _classify_source(
    cash_delta_usd_equiv: float,
    sales: list[Sale],
    tolerance: float = SALE_MATCH_TOLERANCE,
) -> tuple[ClassifiedSource, bool]:
    """Return (classification, requires_user_classification).

    Logic:
      - If a single sale matches the cash delta within tolerance → confident
        rsu_sale (NVDA) or stock_sale.
      - If multiple sales sum to within tolerance → stock_sale.
      - Otherwise → unclear (user needs to classify).
    """
    if not sales or cash_delta_usd_equiv <= 0:
        return "unclear", True

    total_sales_usd = sum(s.value_usd for s in sales)
    if total_sales_usd <= 0:
        return "unclear", True

    diff_pct = abs(cash_delta_usd_equiv - total_sales_usd) / max(
        cash_delta_usd_equiv, 1.0,
    )
    if diff_pct > tolerance:
        return "unclear", True

    # Match — figure out what KIND of sale
    if any(s.symbol == "NVDA" for s in sales):
        return "rsu_sale", False
    return "stock_sale", False


def detect_windfall(
    current_tsv_path: Path,
    previous_tsv_path: Path | None,
    *,
    threshold_usd: float = DEFAULT_THRESHOLD_USD,
    threshold_nis: float = DEFAULT_THRESHOLD_NIS,
) -> WindfallEvent | None:
    """Compare two consecutive TSVs and return a WindfallEvent if a cash
    delta breaches either threshold. Returns None otherwise."""
    if not current_tsv_path.exists():
        return None
    if previous_tsv_path is None or not previous_tsv_path.exists():
        return None

    current = _load_tsv(current_tsv_path)
    previous = _load_tsv(previous_tsv_path)

    cur_usd, cur_nis = _find_cash_balances(current)
    prev_usd, prev_nis = _find_cash_balances(previous)
    fx = _find_fx(current) or _find_fx(previous) or 3.0

    cash_delta_usd = cur_usd - prev_usd
    cash_delta_nis = cur_nis - prev_nis
    cash_delta_total_usd_equiv = (
        cash_delta_usd + (cash_delta_nis / max(fx, 0.01))
    )

    # Gate
    if (
        cash_delta_usd < threshold_usd
        and cash_delta_nis < threshold_nis
    ):
        return None

    # TSV-DIFF SALE ATTRIBUTION IS NEUTRALIZED BY DEFAULT.
    # The TSV is a hand-maintained OUTPUT, not a transaction source. Diffing two
    # months' holdings by (location, exact-symbol, fixed-column) fabricated phantom
    # sales whenever a row's ticker/columns shifted between snapshots — e.g. a
    # BRK.B->BRK/B relabel + a leading status-flag column made a GROWN position
    # (150->185) read as "sold 150". Cash-source attribution must come from the
    # real inputs (broker RSU sales + bank cash transactions), via the
    # rsu_reconciliation pipeline — not this diff. Until that linkage lands we
    # still surface the (reliable) cash DELTA, but assert NO sale source.
    # Set ARGOSY_WINDFALL_TSV_SALE_DIFF=1 to re-enable the legacy diff.
    import os as _os
    if _os.environ.get("ARGOSY_WINDFALL_TSV_SALE_DIFF", "0").strip().lower() in {"1", "true", "on", "yes"}:
        sales = _find_sales(current, previous, fx_usd_nis=fx)
        classified, needs_user = _classify_source(cash_delta_total_usd_equiv, sales)
    else:
        sales = []
        classified, needs_user = "unclear", True

    allocation_table = _find_allocation_table(current)

    return WindfallEvent(
        detected_at=datetime.now(),
        cash_delta_usd=round(cash_delta_usd, 2),
        cash_delta_nis=round(cash_delta_nis, 2),
        cash_delta_total_usd_equiv=round(cash_delta_total_usd_equiv, 2),
        fx_usd_nis=fx,
        matching_sales=sales,
        classified_source=classified,
        requires_user_classification=needs_user,
        allocation_delta_table=allocation_table,
        source_tsv=str(current_tsv_path),
        previous_tsv=str(previous_tsv_path),
    )


def attribute_cash_source(
    event: WindfallEvent,
    schwab_csv_path: Path,
    session,
    user_id: str,
    *,
    since: "date | None" = None,
    until: "date | None" = None,
) -> WindfallEvent:
    """Best-effort: attach transaction-based source attribution to a windfall.

    Runs the cash-source reconciler (Schwab RSU sales → Leumi USD transfer
    credits, taxed per the NVIDIA ESOP §102 simulation) and folds the result
    onto ``event`` in place:

      * ``reconciled_source_lines`` — one human attribution line per linked
        sale (e.g. "NVDA RSU sale 1040 sh @ $199.56 ($207,538 gross, §102 tax
        $56,649 (91% capital @25% / 9% ordinary @50%)) → $150,889 net (~73%
        retention) ≈ $150,864 to Leumi USD on 2026-04-29").
      * ``reconciled_matched_usd`` / ``reconciled_unexplained_usd`` — the
        attributed vs. residual cash totals.
      * When at least one sale links AND the classifier was "unclear", the
        event is upgraded to ``rsu_sale`` (real-transaction evidence beats the
        neutralized TSV-diff classifier) and ``requires_user_classification``
        is cleared.

    ``since`` / ``until`` bound both the Schwab sales and the Leumi transfers
    so the residual reflects only the relevant window. When omitted, defaults
    to the calendar year of ``event.detected_at`` (Jan 1 → detection date) so
    pre-window historical transfers don't inflate the "unexplained" residual.

    Degrades silently to the unchanged event (still "unclear") on any error —
    missing CSV, no DB, no link. Never raises. The cash *delta* itself is
    untouched; only the source attribution is augmented.
    """
    if since is None:
        since = date(event.detected_at.year, 1, 1)
    if until is None:
        until = event.detected_at.date()
    try:
        from argosy.services.cash_source_reconciler import reconcile_from_csv

        report = reconcile_from_csv(
            schwab_csv_path, session, user_id, since=since, until=until
        )
    except Exception:  # noqa: BLE001 — attribution is best-effort
        return event

    if report.links:
        event.reconciled_source_lines = [l.describe() for l in report.links]
        event.reconciled_matched_usd = round(report.matched_transfer_usd, 2)
        event.reconciled_unexplained_usd = round(
            report.unexplained_transfer_usd, 2
        )
        if event.classified_source == "unclear":
            # Real transactions linked a sale → upgrade off the neutral default.
            has_nvda = any(l.sale_symbol == "NVDA" for l in report.links)
            event.classified_source = "rsu_sale" if has_nvda else "stock_sale"
            event.requires_user_classification = False
    else:
        event.reconciled_source_lines = []
        event.reconciled_matched_usd = 0.0
        event.reconciled_unexplained_usd = round(
            report.unexplained_transfer_usd, 2
        )
    return event
