"""Generate a refreshed Family Finances Status TSV from Argosy's current state.

Per [[feedback_argosy_generates_tsv]] (2026-05-29): Argosy is the system
that produces this TSV, not the user via an external script. This service
takes the most-recent prior TSV at ``$ARGOSY_EXPENSE_SAMPLES_ROOT`` as
the carry-forward template and refreshes it with current state:

  * **Leumi NIS cash row** -- override with the closing running-balance
    from the most recent ``leumi_osh`` ExpenseStatement.
  * **Leumi USD cash row** -- override with the closing balance from the
    most recent ``leumi_usd`` ExpenseStatement.
  * **Position rows + FX + non-cash structure** -- carry forward
    verbatim from the prior TSV. The XLS-Osh pair flow
    (xls_osh_pair.py) keeps the prior TSV's positions current via the
    monthly XLS upload; this generator is the "now compose the current
    snapshot" step that does NOT require a fresh XLS each time.
  * **snapshot_date** -- updated to today.
  * **Current allocation** -- ``current_pct`` + ``current_k_usd`` +
    ``delta_k`` recomputed from the new totals; ``target_pct`` +
    ``target_k`` preserved verbatim.

The carry-forward sections (real estate, NVDA sales history, pensions)
are user-maintained today; they ride in the prior TSV and pass through
this generator unchanged. UI editors for those sections are deliberate
follow-ups (see SDD wave handover).

This service shares the splice helpers in ``xls_osh_pair`` for the
allocation-block recompute + cash-row construction; the public function
``generate_family_finances_tsv`` is the entry point the route calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.ingest.tsv import parse_portfolio_tsv
from argosy.services.portfolio_ingest.xls_osh_pair import (
    _canonical_tsv_filename,
    _find_most_recent_prior_tsv,
    _get_osh_closing_balance_nis,
    _LEUMI_OSH_PARSER_NAME,
    _recompute_allocation_block,
    _replace_allocation_block,
    _update_header_rows,
)
from argosy.state.models import (
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
)

_log = logging.getLogger(__name__)

_LEUMI_USD_PARSER_NAME = "leumi_usd"


@dataclass
class GeneratorResult:
    """Outcome of a TSV-generation run."""

    tsv_persisted: bool
    persisted_path: Path | None
    snapshot_date: date | None
    leumi_nis_cash: float | None
    leumi_usd_cash: float | None
    warnings: list[str]
    detail: str | None = None


def generate_family_finances_tsv(
    db: Session,
    *,
    user_id: str,
    snapshot_root: Path,
    today: date | None = None,
) -> GeneratorResult:
    """Write a refreshed Family Finances Status TSV to ``snapshot_root``.

    Carry-forward template = the most recent prior TSV at ``snapshot_root``.
    Overrides: snapshot_date, FX (preserved from prior), Leumi NIS + USD
    cash rows, allocation-block currents. Everything else carries through.

    Returns a GeneratorResult describing what happened. Defensive: a
    missing prior TSV returns ``tsv_persisted=False`` with a detail
    message rather than raising -- this is a button-driven flow, no
    silent failures.
    """
    warnings: list[str] = []
    if today is None:
        today = datetime.now(timezone.utc).date()

    prior_tsv = _find_most_recent_prior_tsv(snapshot_root)
    if prior_tsv is None:
        return GeneratorResult(
            tsv_persisted=False,
            persisted_path=None,
            snapshot_date=None,
            leumi_nis_cash=None,
            leumi_usd_cash=None,
            warnings=warnings,
            detail=(
                f"No prior 'Family Finances Status' TSV at {snapshot_root}. "
                f"The generator carries non-cash structure forward from the "
                f"most recent TSV; upload at least one XLS (via the upload "
                f"tile) to seed the scan root, then retry."
            ),
        )

    prior_snapshot = parse_portfolio_tsv(prior_tsv)

    # Pull latest cash balances per bank source. The lookup falls back
    # to older statements if the newest has a glitched extraction.
    nis_lookup = _latest_closing_balance(
        db,
        user_id=user_id,
        parser_name=_LEUMI_OSH_PARSER_NAME,
        raw_row_key="balance",
    )
    usd_lookup = _latest_closing_balance(
        db,
        user_id=user_id,
        parser_name=_LEUMI_USD_PARSER_NAME,
        raw_row_key="balance_usd",
    )
    leumi_nis_cash = nis_lookup.value
    leumi_usd_cash = usd_lookup.value

    # Distinguish 'no statement found' from 'extraction failed on all
    # candidates' so the operator can diagnose. Codex zigzag v2 MINOR
    # (2026-05-29).
    if leumi_nis_cash is None:
        if nis_lookup.statements_scanned == 0:
            warnings.append(
                "No Leumi NIS (Osh) statement found in expense_statements; "
                "Leumi NIS cash row was carried forward from the prior "
                "TSV (may be stale)."
            )
        else:
            warnings.append(
                f"Leumi NIS (Osh) statements found ({nis_lookup.statements_scanned}) "
                f"but closing balance could not be extracted from any of them "
                f"(ids: {nis_lookup.extraction_failed_at}). Check raw_row_json "
                f"for the 'balance' field. NIS cash row carried forward from "
                f"the prior TSV."
            )
    if leumi_usd_cash is None:
        if usd_lookup.statements_scanned == 0:
            warnings.append(
                "No Leumi USD statement found in expense_statements; "
                "Leumi USD cash row was carried forward from the prior "
                "TSV (may be stale)."
            )
        else:
            warnings.append(
                f"Leumi USD statements found ({usd_lookup.statements_scanned}) "
                f"but closing balance could not be extracted from any of them "
                f"(ids: {usd_lookup.extraction_failed_at}). Check raw_row_json "
                f"for the 'balance_usd' field. USD cash row carried forward "
                f"from the prior TSV."
            )
    # Diagnostic warning when fallback fired but we recovered: surface
    # which statements were skipped so the operator can investigate.
    if leumi_nis_cash is not None and nis_lookup.extraction_failed_at:
        warnings.append(
            f"Recovered Leumi NIS cash via fallback (skipped statement ids "
            f"with extraction errors: {nis_lookup.extraction_failed_at})."
        )
    if leumi_usd_cash is not None and usd_lookup.extraction_failed_at:
        warnings.append(
            f"Recovered Leumi USD cash via fallback (skipped statement ids "
            f"with extraction errors: {usd_lookup.extraction_failed_at})."
        )

    fx_usd_nis = prior_snapshot.fx_usd_nis or 3.7
    fx_usd_eur = prior_snapshot.fx_usd_eur or 1.05
    if prior_snapshot.fx_usd_nis is None:
        warnings.append(
            "Prior TSV has no 'USD to NIS:' rate; defaulted to 3.7. "
            "USD-equivalents may be imprecise."
        )

    # Read prior TSV lines verbatim for the splice.
    prior_lines = prior_tsv.read_text(
        encoding="utf-8-sig", errors="ignore",
    ).splitlines()

    # Build the refreshed position block: replace Leumi Cash rows with
    # fresh closing balances, keep every other row verbatim.
    refreshed_lines = _refresh_cash_rows_in_position_block(
        prior_lines=prior_lines,
        leumi_nis_cash=leumi_nis_cash,
        leumi_usd_cash=leumi_usd_cash,
        fx_usd_nis=fx_usd_nis,
    )

    # Update header rows: snapshot_date = today, FX preserved.
    refreshed_lines = _update_header_rows(
        refreshed_lines,
        snapshot_date=today,
        fx_usd_nis=fx_usd_nis,
        fx_usd_eur=fx_usd_eur,
    )

    # Recompute the Current allocation block from the new totals.
    # Locate the position block + extract non-Leumi + new Leumi lines for
    # the recompute helper's by-type aggregation.
    non_leumi_lines, leumi_lines = _split_position_lines(refreshed_lines)
    new_total_usd_k = _compute_total_usd_k(non_leumi_lines + leumi_lines)
    new_alloc_lines = _recompute_allocation_block(
        prior_lines=refreshed_lines,
        prior_allocations=prior_snapshot.allocations,
        new_total_usd_k=new_total_usd_k,
        new_leumi_lines=leumi_lines,
        non_leumi_position_lines=non_leumi_lines,
    )
    # Find where the allocation block lives in refreshed_lines and replace it.
    alloc_start_idx = None
    for i, ln in enumerate(refreshed_lines):
        if "current allocation" in ln.lower():
            alloc_start_idx = i
            break
    if alloc_start_idx is not None:
        head = refreshed_lines[:alloc_start_idx]
        tail = refreshed_lines[alloc_start_idx:]
        tail = _replace_allocation_block(tail, new_alloc_lines)
        refreshed_lines = head + tail

    tsv_text = "\n".join(refreshed_lines) + "\n"

    # Persist under canonical name for today's snapshot_date.
    snapshot_root.mkdir(parents=True, exist_ok=True)
    target_path = snapshot_root / _canonical_tsv_filename(today)
    target_path.write_text(tsv_text, encoding="utf-8")
    _log.info(
        "portfolio_snapshot.generated",
        extra={
            "user_id": user_id,
            "path": str(target_path),
            "leumi_nis_cash": leumi_nis_cash,
            "leumi_usd_cash": leumi_usd_cash,
        },
    )

    return GeneratorResult(
        tsv_persisted=True,
        persisted_path=target_path,
        snapshot_date=today,
        leumi_nis_cash=leumi_nis_cash,
        leumi_usd_cash=leumi_usd_cash,
        warnings=warnings,
        detail=None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _BalanceLookup:
    """Outcome of a closing-balance lookup -- distinguishes 'no statement
    found' from 'statement found but extraction failed' so warnings stay
    diagnosable (codex zigzag v2 MINOR, 2026-05-29).
    """
    value: float | None
    statements_scanned: int
    extraction_failed_at: list[int]  # statement ids where extraction failed


def _latest_closing_balance(
    db: Session,
    *,
    user_id: str,
    parser_name: str,
    raw_row_key: str,
    max_fallback: int = 6,
) -> _BalanceLookup:
    """Return the closing balance from the most recent statement of the
    given parser_name with a valid extraction. Falls back to older
    statements (up to ``max_fallback`` total) if the newest one has no
    transactions, malformed raw_row_json, missing key, or non-numeric
    value. Codex zigzag v2 IMPORTANT (2026-05-29): without the fallback,
    a single glitched statement silently stales the cash row even when
    recoverable data exists in older statements.

    Deterministic ordering via (period_end DESC, id DESC) -- newest
    statement tried first.
    """
    candidates = (
        db.execute(
            select(ExpenseStatement)
            .join(ExpenseSource, ExpenseSource.id == ExpenseStatement.source_id)
            .where(
                ExpenseStatement.user_id == user_id,
                ExpenseStatement.parser_name == parser_name,
            )
            .order_by(
                desc(ExpenseStatement.period_end),
                desc(ExpenseStatement.id),
            )
            .limit(max_fallback)
        )
        .scalars()
        .all()
    )
    failed: list[int] = []
    for stmt in candidates:
        last_txn = (
            db.execute(
                select(ExpenseTransaction)
                .where(ExpenseTransaction.statement_id == stmt.id)
                .order_by(
                    desc(ExpenseTransaction.occurred_on),
                    desc(ExpenseTransaction.id),
                )
                .limit(1)
            )
            .scalar_one_or_none()
        )
        if last_txn is None:
            failed.append(stmt.id)
            continue
        raw_json = last_txn.raw_row_json or "{}"
        try:
            raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        except json.JSONDecodeError:
            failed.append(stmt.id)
            continue
        bal = raw.get(raw_row_key) if isinstance(raw, dict) else None
        if bal is None:
            failed.append(stmt.id)
            continue
        try:
            return _BalanceLookup(
                value=float(str(bal).replace(",", "").strip()),
                statements_scanned=len(candidates),
                extraction_failed_at=failed,
            )
        except (ValueError, AttributeError):
            failed.append(stmt.id)
            continue
    return _BalanceLookup(
        value=None,
        statements_scanned=len(candidates),
        extraction_failed_at=failed,
    )


def _refresh_cash_rows_in_position_block(
    *,
    prior_lines: list[str],
    leumi_nis_cash: float | None,
    leumi_usd_cash: float | None,
    fx_usd_nis: float,
) -> list[str]:
    """Walk the position block; replace each Leumi Cash row's value with
    the latest closing balance. Carry every other row verbatim."""
    out: list[str] = []
    in_position_block = False
    section_terminators = (
        "real estate details", "current allocation",
        "nvda sales history", "pensions",
    )
    for ln in prior_lines:
        joined_lower = ln.lower()
        if "bank account / funds allocation" in joined_lower:
            in_position_block = True
            out.append(ln)
            continue
        if in_position_block and any(t in joined_lower for t in section_terminators):
            in_position_block = False
            out.append(ln)
            continue
        if not in_position_block:
            out.append(ln)
            continue
        cells = ln.split("\t")
        if len(cells) < 11:
            out.append(ln)
            continue
        location = cells[1].strip() if len(cells) > 1 else ""
        currency = cells[2].strip() if len(cells) > 2 else ""
        asset_type = cells[3].strip() if len(cells) > 3 else ""
        if (
            location.lower() == "leumi"
            and asset_type.lower() == "cash"
        ):
            # Override with fresh closing balance per currency.
            if currency == "NIS" and leumi_nis_cash is not None:
                usd_k = (leumi_nis_cash / max(fx_usd_nis, 0.01)) / 1000.0
                cells[9] = f"{leumi_nis_cash:.2f}"
                cells[10] = f"{usd_k:.2f}"
                out.append("\t".join(cells))
                continue
            if currency == "USD" and leumi_usd_cash is not None:
                cells[9] = f"{leumi_usd_cash:.2f}"
                cells[10] = f"{leumi_usd_cash / 1000.0:.2f}"
                out.append("\t".join(cells))
                continue
        out.append(ln)
    return out


def _split_position_lines(
    refreshed_lines: list[str],
) -> tuple[list[str], list[str]]:
    """Split the position block into (non-Leumi, Leumi) lines.

    Helper for _recompute_allocation_block's by-type aggregation.
    Returns lines including the position-block header + Sum row in the
    non-Leumi list (so the by-type aggregator skips them via its own
    location/sum filtering).
    """
    section_terminators = (
        "real estate details", "current allocation",
        "nvda sales history", "pensions",
    )
    non_leumi: list[str] = []
    leumi: list[str] = []
    in_block = False
    for ln in refreshed_lines:
        joined_lower = ln.lower()
        if "bank account / funds allocation" in joined_lower:
            in_block = True
            continue  # don't pass the header row down
        if in_block and any(t in joined_lower for t in section_terminators):
            in_block = False
            continue
        if not in_block:
            continue
        cells = ln.split("\t")
        if len(cells) < 2:
            non_leumi.append(ln)
            continue
        location = cells[1].strip() if len(cells) > 1 else ""
        if "leumi" in location.lower():
            leumi.append(ln)
        else:
            non_leumi.append(ln)
    return non_leumi, leumi


def _compute_total_usd_k(lines: list[str]) -> float:
    """Sum (K) USD Value column (idx 10) across position lines, skipping
    Sum + header rows."""
    total = 0.0
    for ln in lines:
        cells = ln.split("\t")
        if len(cells) <= 10:
            continue
        loc = cells[1].strip() if len(cells) > 1 else ""
        if not loc or "sum" in loc.lower():
            continue
        try:
            v = float(cells[10].replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
        total += v
    return total


__all__ = [
    "GeneratorResult",
    "generate_family_finances_tsv",
]
