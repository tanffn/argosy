"""Parse-sanity gate: hard violations are fatal before any persist."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from argosy.config import ExpensesIngestSanityConfig, load_expenses_config
from argosy.services.expense_ingest.orchestrator import (
    PARSER_DISPATCH,
    ingest_user_file,
)
from argosy.services.expense_ingest.parse_sanity import (
    ParseSanityError,
    check_parse_sanity,
)
from argosy.services.expense_ingest.types import (
    NormalizedTransaction,
    ParseResult,
    ParserName,
    SourceHint,
    StatementMeta,
)
from argosy.state.models import ExpenseStatement, ExpenseTransaction, User, UserFile

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"
TODAY = date(2026, 7, 12)


def _tx(
    *,
    occurred_on: date = date(2026, 6, 1),
    merchant_raw: str = "סופר פארם",
    amount_nis: float | None = 100.0,
    amount_orig: float | None = None,
    tx_type: str = "regular",
    raw_row: dict | None = None,
) -> NormalizedTransaction:
    return NormalizedTransaction(
        occurred_on=occurred_on,
        merchant_raw=merchant_raw,
        merchant_normalized=merchant_raw.lower(),
        amount_nis=amount_nis,
        amount_orig=amount_orig,
        direction="debit",
        tx_type=tx_type,  # type: ignore[arg-type]
        raw_row=raw_row or {},
    )


def _result(
    txs: list[NormalizedTransaction],
    *,
    declared: float | None = None,
    parsed: float | None = None,
) -> ParseResult:
    if parsed is None:
        parsed = sum((t.amount_nis or 0.0) for t in txs) if txs else 0.0
    if declared is None:
        declared = parsed
    starts = [t.occurred_on for t in txs] or [date(2026, 6, 1)]
    return ParseResult(
        statement=StatementMeta(
            period_start=min(starts),
            period_end=max(starts),
            charge_date=date(2026, 7, 2),
            declared_total_nis=declared,
            parsed_total_nis=parsed,
        ),
        transactions=txs,
        source_hint=SourceHint(
            kind="card", issuer="max", external_id="9999",
        ),
    )


def _shifted_column_result() -> ParseResult:
    """Simulate a Max-format file whose columns slid one place left:
    Excel serial / yyyymmdd values land in amount_nis, merchants look like
    dates. This is what the upload card would surface after the gate fires.
    """
    return _result([
        _tx(
            occurred_on=date(2026, 3, 30),
            merchant_raw="2026-03-30",  # date string in merchant slot
            amount_nis=20260330.0,      # yyyymmdd serial in amount slot
        ),
        _tx(
            occurred_on=date(2026, 3, 25),
            merchant_raw="2026-03-25",
            amount_nis=20260325.0,
        ),
        _tx(
            occurred_on=date(2026, 3, 21),
            merchant_raw="2026-03-21",
            amount_nis=20260321.0,
        ),
    ], declared=654.88, parsed=60780976.0)


# ---------------------------------------------------------------------------
# Unit: each hard check
# ---------------------------------------------------------------------------


def test_hard_shifted_columns_absurd_amounts():
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(_shifted_column_result(), today=TODAY)
    msg = str(ei.value)
    assert msg.startswith("parse sanity failed:")
    assert "amount invalid" in msg
    assert any("amount" in v for v in ei.value.violations)


def test_hard_absurd_amount():
    bad = _result([_tx(amount_nis=250_000.01)])
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(bad, today=TODAY)
    assert any("amount invalid" in v for v in ei.value.violations)


def test_hard_nan_amount():
    tx = _tx(amount_nis=100.0)
    # Bypass pydantic finite-float validation to simulate a corrupted coerce.
    object.__setattr__(tx, "amount_nis", float("nan"))
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(_result([tx]), today=TODAY)
    assert any("NaN" in v for v in ei.value.violations)


def test_hard_blank_merchants_over_threshold():
    # 2/10 = 20% blank > 10% threshold
    txs = [_tx(merchant_raw=f"OK-{i}") for i in range(8)]
    txs += [_tx(merchant_raw="   "), _tx(merchant_raw="")]
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(_result(txs), today=TODAY)
    assert any("blank merchant_raw" in v for v in ei.value.violations)


def test_hard_mojibake_merchants_over_threshold():
    # 3/10 = 30% > 20%
    good = [_tx(merchant_raw=f"סופר-{i}") for i in range(7)]
    bad = [
        _tx(merchant_raw="×ÿ×©×•×¤×¨"),           # Latin-1 mojibake of Hebrew
        _tx(merchant_raw="caf\ufffd\ufffd"),      # U+FFFD replacement chars
        _tx(merchant_raw="!!!@@@###"),            # punctuation-only
    ]
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(_result(good + bad), today=TODAY)
    assert any("mojibake" in v for v in ei.value.violations)


def test_hard_wrong_footer_total():
    # declared=10_000 → tol=max(50, 100)=100; delta=101 is fatal
    txs = [_tx(amount_nis=100.0) for _ in range(10)]
    bad = _result(txs, declared=10_000.0, parsed=9_899.0)
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(bad, today=TODAY)
    assert any("footer total mismatch" in v for v in ei.value.violations)


def test_hard_far_future_date():
    bad = _result([_tx(occurred_on=date(2026, 10, 1))])  # > today+45 from TODAY
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(bad, today=TODAY)
    assert any("date out of range" in v for v in ei.value.violations)


def test_hard_far_past_date():
    # 14 months before 2026-07-12 = 2025-05-12; one day earlier is fatal
    bad = _result([_tx(occurred_on=date(2025, 5, 11))])
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(bad, today=TODAY)
    assert any("date out of range" in v for v in ei.value.violations)


def test_hard_zero_transactions():
    empty = ParseResult(
        statement=StatementMeta(
            period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
            declared_total_nis=None, parsed_total_nis=0.0,
        ),
        transactions=[],
        source_hint=SourceHint(kind="card", issuer="max", external_id="1"),
    )
    with pytest.raises(ParseSanityError) as ei:
        check_parse_sanity(empty, today=TODAY)
    assert ei.value.violations == ["0 transactions after parse"]


# ---------------------------------------------------------------------------
# Soft / boundary
# ---------------------------------------------------------------------------


def test_soft_tolerance_boundary_passes_with_warning():
    # declared=10_000 → tol=100; delta exactly 100 → soft warn, not fatal
    txs = [_tx(amount_nis=100.0) for _ in range(10)]
    ok = _result(txs, declared=10_000.0, parsed=9_900.0)
    report = check_parse_sanity(ok, today=TODAY)
    assert len(report.warnings) == 1
    assert "within tolerance" in report.warnings[0]


def test_soft_unmapped_tx_type_warns():
    txs = [
        _tx(merchant_raw="A", raw_row={"tx_type_he": "רגילה"}),
        _tx(merchant_raw="B", raw_row={"tx_type_he": "עסקת אשראי מוזרה"}),
    ]
    report = check_parse_sanity(_result(txs), today=TODAY)
    assert any("unmapped tx-type" in w for w in report.warnings)


def test_clean_result_no_warnings():
    report = check_parse_sanity(_result([_tx()]), today=TODAY)
    assert report.warnings == []


# ---------------------------------------------------------------------------
# Orchestrator integration: atomicity + event payload + user-visible error
# ---------------------------------------------------------------------------


def _seed_file(s: Session, path: Path) -> int:
    if s.get(User, "ariel") is None:
        s.add(User(id="ariel", plan="free"))
        s.flush()
    f = UserFile(
        user_id="ariel", sha256="s" * 64, original_name=path.name,
        sanitized_name=path.name, mime_type="application/vnd...sheet",
        kind="other", size_bytes=1, storage_path=str(path),
        source="chat_attachment",
    )
    s.add(f)
    s.flush()
    return f.id


def test_atomicity_and_event_payload_on_shifted_columns(alembic_engine_at_head):
    """Failing sanity must persist ZERO statement rows and ZERO tx rows,
    publish expense.statement.failed with violations, and surface a message
    the upload card would render via error=str(exc).
    """
    shifted = _shifted_column_result()
    with pytest.raises(ParseSanityError) as preview:
        check_parse_sanity(shifted, today=TODAY)
    expected_user_error = str(preview.value)

    with Session(alembic_engine_at_head) as s, \
         patch("argosy.services.expense_ingest.category_resolver"
               "._categorize_via_llm", return_value=[]), \
         patch("argosy.api.events.publish_event_threadsafe") as mock_pub, \
         patch.dict(
             PARSER_DISPATCH,
             {ParserName.MAX: lambda path, last4_hint=None: shifted},
         ):
        file_id = _seed_file(s, FIXTURES / "max_minimal.xlsx")
        s.commit()

        with pytest.raises(ParseSanityError) as ei:
            ingest_user_file(s, "ariel", file_id, last4_hint="9999")
        s.rollback()

        assert s.query(ExpenseStatement).count() == 0
        assert s.query(ExpenseTransaction).count() == 0

        assert str(ei.value) == expected_user_error
        # Worked example for the hand-back (upload card renders this string):
        assert expected_user_error.startswith("parse sanity failed:")
        assert "amount invalid" in expected_user_error

        failed_calls = [
            c for c in mock_pub.call_args_list
            if c.args and c.args[0] == "expense.statement.failed"
        ]
        assert len(failed_calls) == 1
        payload = failed_calls[0].args[1]
        assert payload["file_id"] == file_id
        assert payload["parse_error"] == expected_user_error
        assert "violations" in payload
        assert isinstance(payload["violations"], list)
        assert len(payload["violations"]) >= 1
        assert any("amount" in v for v in payload["violations"])


def test_config_defaults_exposed():
    cfg = load_expenses_config(user_id="no-such-user-for-defaults")
    s = cfg.ingest_sanity
    assert isinstance(s, ExpensesIngestSanityConfig)
    assert s.row_amount_cap_nis == 250_000
    assert s.date_past_months == 14
    assert s.date_future_days == 45
    assert s.blank_merchant_pct == 10.0
    assert s.mojibake_pct == 20.0
    assert s.total_tolerance_nis == 50.0
    assert s.total_tolerance_pct == 1.0
