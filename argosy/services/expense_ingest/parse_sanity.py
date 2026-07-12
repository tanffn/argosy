"""Post-parse sanity gate — fail loudly on nonsense before any persist.

Runs after a parser returns a ``ParseResult`` and BEFORE ``persist_statement``.
Hard violations raise ``ParseSanityError`` so the orchestrator's existing
failure path fires (no statement row, no transactions, event published).
Soft findings are returned as warnings that still allow persist.

Format-agnostic: thresholds come from ``ExpensesIngestSanityConfig``; no
per-issuer special-casing. Installment rows keep their original purchase
date — the past window (default 14 months) covers 12-month plans with margin.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from argosy.config import ExpensesIngestSanityConfig
from argosy.services.expense_ingest.types import ParseResult


# Hebrew letters (incl. presentation forms) + ASCII alnum for mojibake ratio.
_HEBREW_RE = re.compile(r"[\u0590-\u05FF\uFB1D-\uFB4F]")
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")

# Known issuer tx-type Hebrew strings that map to a non-default type (or the
# explicit "רגילה" regular). Anything else in raw_row['tx_type_he'] that still
# landed as tx_type='regular' is treated as an unmapped default — soft warn.
_KNOWN_TX_TYPE_HE = frozenset({
    "רגילה",
    "הוראת קבע",
    "תשלומים",
    "זיכוי",
    "חיוב חודשי",
    "חיוב עסקות מיידי",
})


class ParseSanityError(Exception):
    """Hard post-parse sanity failure — ingest must not persist anything."""

    def __init__(self, violations: list[str]):
        if not violations:
            raise ValueError("ParseSanityError requires a non-empty violations list")
        self.violations = list(violations)
        preview = "; ".join(self.violations[:3])
        super().__init__(f"parse sanity failed: {preview}")


@dataclass
class SanityReport:
    """Soft findings only — hard ones raise ``ParseSanityError`` instead."""

    warnings: list[str] = field(default_factory=list)


def _add_months(d: date, months: int) -> date:
    """Calendar-aware month shift (negative = past). Clamps day to month length."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _is_bad_amount(value: float | None, cap: float) -> str | None:
    if value is None:
        return None
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "inf"
    if abs(value) > cap:
        return f"abs={abs(value):.2f} > cap={cap:g}"
    return None


def _merchant_is_blank(merchant: str) -> bool:
    return not (merchant or "").strip()


def _merchant_is_mojibake(merchant: str) -> bool:
    s = merchant or ""
    if "\ufffd" in s:
        return True
    non_space = [c for c in s if not c.isspace()]
    if not non_space:
        return False  # blank handled separately
    good = sum(
        1 for c in non_space
        if _HEBREW_RE.fullmatch(c) or _ASCII_ALNUM_RE.fullmatch(c)
    )
    return (good / len(non_space)) < 0.50


def _total_tolerance(declared: float, cfg: ExpensesIngestSanityConfig) -> float:
    return max(cfg.total_tolerance_nis, cfg.total_tolerance_pct / 100.0 * abs(declared))


def check_parse_sanity(
    result: ParseResult,
    *,
    config: ExpensesIngestSanityConfig | None = None,
    today: date | None = None,
) -> SanityReport:
    """Validate a ``ParseResult``. Raise ``ParseSanityError`` on hard violations.

    Returns soft warnings (in-tolerance total delta, unmapped tx-type strings)
    when all hard checks pass.
    """
    cfg = config or ExpensesIngestSanityConfig()
    today = today or date.today()
    violations: list[str] = []
    warnings: list[str] = []

    txs = result.transactions
    if not txs:
        raise ParseSanityError(["0 transactions after parse"])

    # Date plausibility (reviewer redesign 2026-07-12): anchoring the past
    # window on TODAY fatals legitimate backfills of old statements (live:
    # the 2025 Leumi sample). Garbage (shifted columns) shows up as absurd
    # absolute years or an impossible SPAN, not as honest age:
    #   - absolute floor 2000-01-01 (column-shift dates land in 1900/2093)
    #   - future ceiling today + date_future_days (real rows can't be ahead)
    #   - span cap: newest-oldest <= date_past_months (covers 12-month
    #     installment carryover inside one statement window)
    past_floor = date(2000, 1, 1)
    future_ceil = today + timedelta(days=cfg.date_future_days)
    # The span cap encodes STATEMENT-WINDOW semantics (one billing cycle +
    # installment carryover). Rolling/range exports (result.rolling: bank
    # ranges, 90-day card pulls) have arbitrary honest spans — a full-year
    # Leumi export is legitimate — so they get floor+ceiling only.
    if result.rolling:
        span_floor = past_floor
    else:
        _dates = [tx.occurred_on for tx in result.transactions]
        span_floor = _add_months(max(_dates), -cfg.date_past_months)

    blank_n = 0
    mojibake_n = 0
    unmapped_n = 0
    amount_hits: list[str] = []
    date_hits: list[str] = []

    for i, tx in enumerate(txs):
        if (tx.occurred_on < past_floor or tx.occurred_on > future_ceil
                or tx.occurred_on < span_floor):
            lo = max(past_floor, span_floor)
            date_hits.append(
                f"row[{i}] occurred_on={tx.occurred_on.isoformat()} "
                f"outside [{lo.isoformat()}, {future_ceil.isoformat()}]"
            )

        for field_name, amt in (("amount_nis", tx.amount_nis),
                                ("amount_orig", tx.amount_orig)):
            reason = _is_bad_amount(amt, cfg.row_amount_cap_nis)
            if reason is not None:
                amount_hits.append(f"row[{i}] {field_name} {reason}")

        if _merchant_is_blank(tx.merchant_raw):
            blank_n += 1
        elif _merchant_is_mojibake(tx.merchant_raw):
            mojibake_n += 1

        raw_type = ""
        if isinstance(tx.raw_row, dict):
            raw = tx.raw_row.get("tx_type_he")
            if raw is not None and not (isinstance(raw, float) and math.isnan(raw)):
                raw_type = str(raw).strip()
        if (
            raw_type
            and raw_type not in _KNOWN_TX_TYPE_HE
            and tx.tx_type == "regular"
        ):
            unmapped_n += 1

    if date_hits:
        preview = date_hits[0]
        extra = f" (+{len(date_hits) - 1} more)" if len(date_hits) > 1 else ""
        violations.append(f"date out of range: {preview}{extra}")

    if amount_hits:
        preview = amount_hits[0]
        extra = f" (+{len(amount_hits) - 1} more)" if len(amount_hits) > 1 else ""
        violations.append(f"amount invalid: {preview}{extra}")

    n = len(txs)
    blank_pct = 100.0 * blank_n / n
    if blank_pct > cfg.blank_merchant_pct:
        violations.append(
            f"blank merchant_raw on {blank_n}/{n} rows "
            f"({blank_pct:.1f}% > {cfg.blank_merchant_pct:g}%)"
        )

    mojibake_pct = 100.0 * mojibake_n / n
    if mojibake_pct > cfg.mojibake_pct:
        violations.append(
            f"mojibake merchant_raw on {mojibake_n}/{n} rows "
            f"({mojibake_pct:.1f}% > {cfg.mojibake_pct:g}%)"
        )

    declared = result.statement.declared_total_nis
    parsed = result.statement.parsed_total_nis
    if declared is not None:
        if math.isnan(declared) or math.isinf(declared):
            violations.append(
                f"declared_total_nis is not finite ({declared!r})"
            )
        else:
            delta = abs(declared - parsed)
            tol = _total_tolerance(declared, cfg)
            if delta > tol:
                violations.append(
                    f"footer total mismatch: declared={declared:g} "
                    f"parsed={parsed:g} |delta|={delta:g} > "
                    f"tolerance={tol:g}"
                )
            elif delta > 0:
                warnings.append(
                    f"footer total within tolerance: declared={declared:g} "
                    f"parsed={parsed:g} |delta|={delta:g} "
                    f"(tolerance={tol:g})"
                )

    if unmapped_n:
        warnings.append(
            f"unmapped tx-type string defaulted to 'regular' on "
            f"{unmapped_n}/{n} row(s)"
        )

    if violations:
        raise ParseSanityError(violations)

    return SanityReport(warnings=warnings)


__all__ = [
    "ParseSanityError",
    "SanityReport",
    "check_parse_sanity",
]
