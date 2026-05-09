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
    leumi_osh as p_leumi, isracard as p_isra, max as p_max,
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
    ParserName.ISRACARD:  p_isra.PARSER_VERSION,
    ParserName.MAX:       p_max.PARSER_VERSION,
}

PARSER_DISPATCH = {
    ParserName.LEUMI_OSH: p_leumi.parse,
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


def _leumi_source_hint(result: ParseResult) -> SourceHint:
    """Leumi parser doesn't fill source_hint; derive a default. For multi-tenant
    we'd parse the account number from the HTML header — left as a follow-up
    (Task EX2+); EX1 single-user uses a stable placeholder.
    """
    return SourceHint(
        kind="bank", issuer="leumi",
        external_id="44745280",          # TODO: extract from HTML header
        display_name="Leumi current account",
    )


def ingest_user_file(
    session: Session, user_id: str, file_id: int,
) -> IngestResult:
    """Run the full ingest pipeline for one already-cataloged user file."""
    file = session.get(UserFile, file_id)
    if file is None or file.user_id != user_id:
        raise ValueError(f"UserFile {file_id} not found for user {user_id}")

    _ensure_categories_seeded(session, user_id)

    parser_name = detect_format(Path(file.storage_path))
    parser_fn = PARSER_DISPATCH[parser_name]
    try:
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

    hint = result.source_hint or _leumi_source_hint(result)
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
