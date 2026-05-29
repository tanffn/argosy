"""End-to-end ingest pipeline. Idempotent on user_files.id.

Order:
  1. Sniff format → dispatch to the right parser.
  2. Register/get source from parser hint (or infer from Leumi headers).
  3. Persist statement (idempotent on user, source, period).
  4. Persist transactions (content-hash dedup).
  5. Correlate bank ↔ card statements (mark is_card_payment, link).
  6. Resolve categories (cascade — refunds skipped).
  7. Match refunds to prior debits (inherit category).
  8. Seed user categories on first run for this user.

Returns IngestResult with statement_id and counts so callers (REST, CLI)
can render a useful response.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.category_resolver import (
    resolve_categories_for_user,
)
from argosy.services.expense_ingest.correlator import correlate_for_user
from argosy.services.expense_ingest.parsers import (
    leumi_osh as p_leumi, leumi_usd as p_leumi_usd,
    isracard as p_isra, max as p_max,
    cal as p_cal, amex as p_amex, diners as p_diners,
)
from argosy.services.expense_ingest.persistence import (
    persist_statement, persist_transactions,
)
from argosy.services.expense_ingest.refund_matcher import match_refunds_for_user
from argosy.services.expense_ingest.registry import register_or_get_source
from argosy.services.expense_ingest.sniff import detect_format
from argosy.services.expense_ingest.taxonomy_seed import (
    seed_system_defaults, seed_user_categories,
)
from argosy.services.expense_ingest.types import (
    ParseResult, ParserName, SourceHint,
)
from argosy.state.models import ExpenseCategory, UserFile

# Discount stub may not yet exist as p_discount — handle absence gracefully.
try:
    from argosy.services.expense_ingest.parsers import discount as p_discount
except ImportError:
    p_discount = None

PARSER_VERSIONS = {
    ParserName.LEUMI_OSH: p_leumi.PARSER_VERSION,
    ParserName.LEUMI_USD: p_leumi_usd.PARSER_VERSION,
    ParserName.ISRACARD:  p_isra.PARSER_VERSION,
    ParserName.MAX:       p_max.PARSER_VERSION,
}

PARSER_DISPATCH = {
    ParserName.LEUMI_OSH: p_leumi.parse,
    ParserName.LEUMI_USD: p_leumi_usd.parse,
    ParserName.ISRACARD:  p_isra.parse,
    ParserName.MAX:       p_max.parse,
    ParserName.CAL:       p_cal.parse,
    ParserName.AMEX:      p_amex.parse,
    ParserName.DINERS:    p_diners.parse,
}
if p_discount is not None:
    PARSER_DISPATCH[ParserName.DISCOUNT] = p_discount.parse


@dataclass
class IngestResult:
    statement_id: int
    transactions_inserted: int
    correlations_made: int
    categories_resolved: int
    refunds_matched: int
    parser_name: str


def _ensure_categories_seeded(session: Session, user_id: str) -> None:
    """First time this user ingests → ensure system defaults + per-user copy."""
    has_user_rows = session.query(ExpenseCategory).filter_by(
        user_id=user_id
    ).first() is not None
    if has_user_rows:
        return
    has_sys_rows = session.query(ExpenseCategory).filter(
        ExpenseCategory.user_id.is_(None)
    ).first() is not None
    if not has_sys_rows:
        seed_system_defaults(session)
        session.flush()
    seed_user_categories(session, user_id)
    session.flush()


_LEUMI_EXPECTED_ACCTS: frozenset[str] = frozenset({
    "44745280",  # NIS current account (Osh)
    "44745200",  # USD brokerage/holding (פמ"ח)
})


def _leumi_source_hint_assert(result: ParseResult) -> SourceHint:
    """Leumi parser fills source_hint with the account number it pulled
    from the HTML header. This guard keeps the single-user simplification
    (we know which Leumi accounts are Ariel's) but BLOWS UP if a
    statement for a different account is fed in — single-user-but-
    trip-wired (per spec §4 Bug 3 / user sign-off Q6:C).

    Both ``LEUMI_OSH`` and ``LEUMI_USD`` parsers route through this
    asserter — same household, two accounts (NIS Osh + USD פמ"ח),
    same trip-wire.
    """
    if result.source_hint is None:
        raise ValueError("Leumi parser did not produce a source_hint")
    parsed_acct = result.source_hint.external_id
    if parsed_acct not in _LEUMI_EXPECTED_ACCTS:
        raise ValueError(
            f"Leumi account mismatch: expected one of "
            f"{sorted(_LEUMI_EXPECTED_ACCTS)}, got {parsed_acct!r}"
        )
    return result.source_hint


def ingest_user_file(
    session: Session, user_id: str, file_id: int,
    *, last4_hint: str | None = None,
) -> IngestResult:
    """Run the full ingest pipeline for one already-cataloged user file."""
    file = session.get(UserFile, file_id)
    if file is None or file.user_id != user_id:
        raise ValueError(f"UserFile {file_id} not found for user {user_id}")

    _ensure_categories_seeded(session, user_id)

    parser_name = detect_format(Path(file.storage_path))
    parser_fn = PARSER_DISPATCH[parser_name]
    try:
        if parser_name == ParserName.MAX:
            result = parser_fn(Path(file.storage_path), last4_hint=last4_hint)
        else:
            result = parser_fn(Path(file.storage_path))
    except Exception as e:
        try:
            from argosy.api.events import publish_event_threadsafe
            publish_event_threadsafe(
                "expense.statement.failed",
                {"user_id": user_id, "file_id": file.id, "parse_error": str(e)},
            )
        except Exception:
            pass
        raise

    if parser_name in (ParserName.LEUMI_OSH, ParserName.LEUMI_USD):
        hint = _leumi_source_hint_assert(result)
    else:
        hint = result.source_hint
        if hint is None:
            raise ValueError(
                f"Parser {parser_name.value} did not produce a source_hint"
            )
    src = register_or_get_source(session, user_id, hint)
    session.flush()

    stmt = persist_statement(
        session, user_id, src.id, file.id, result,
        parser_name, PARSER_VERSIONS.get(parser_name, "0.0.0"),
    )
    inserted = persist_transactions(
        session, stmt, src.id, user_id, result.transactions
    )

    correlations = correlate_for_user(session, user_id)
    resolved = resolve_categories_for_user(session, user_id)
    refunds = match_refunds_for_user(session, user_id)

    # Bidirectional XLS-Osh pair hook (codex zigzag 2026-05-29 #8 -- prefer
    # explicit Leumi-Osh-only hook over a global SQLAlchemy after_insert
    # event). If a Leumi-bank statement just landed and there's a pending
    # Leumi portfolio XLS waiting for cash, resolve the pair now.
    if parser_name == ParserName.LEUMI_OSH:
        try:
            from argosy.services.portfolio_ingest.xls_osh_pair import (
                try_resolve_pending_on_osh_arrival,
            )
            import os
            from pathlib import Path
            env_root = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
            if env_root:
                snapshot_root = Path(env_root)
            else:
                from argosy.config import get_settings
                snapshot_root = get_settings().home / "snapshots"
            try_resolve_pending_on_osh_arrival(
                db=session,
                statement_id=stmt.id,
                snapshot_root=snapshot_root,
            )
        except Exception as exc:  # noqa: BLE001 -- never fail ingest
            import logging
            logging.getLogger(__name__).warning(
                "xls_osh_pair.osh_hook_failed",
                extra={"statement_id": stmt.id, "error": str(exc)},
            )

    try:
        from argosy.api.events import publish_event_threadsafe
        publish_event_threadsafe(
            "expense.statement.parsed",
            {
                "user_id": user_id,
                "statement_id": stmt.id,
                "source_id": src.id,
                "parsed_total_nis": float(stmt.parsed_total_nis),
                "status": stmt.status,
            },
        )
    except Exception:
        pass     # Best-effort — never fail the ingest because of telemetry

    return IngestResult(
        statement_id=stmt.id,
        transactions_inserted=inserted,
        correlations_made=correlations,
        categories_resolved=resolved,
        refunds_matched=refunds,
        parser_name=parser_name.value,
    )
