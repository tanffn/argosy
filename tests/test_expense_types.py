"""Pydantic types for the expense-ingest pipeline."""

from datetime import date

import pytest

from argosy.services.expense_ingest.types import (
    NormalizedTransaction, ParseResult, StatementMeta, SourceHint,
    GroundTruth, ParserName,
)


def _row(tx_id: int, merchant: str, amount: float = 100.0,
         direction: str = "debit", issuer: str = "isracard",
         hint: str | None = None):
    return NormalizedTransaction(
        occurred_on=date(2026, 4, 8),
        merchant_raw=merchant,
        merchant_normalized=merchant.lower(),
        amount_nis=amount,
        direction=direction,
        tx_type="regular",
        raw_row={"foo": "bar"},
    )


def test_normalized_transaction_minimal():
    tx = NormalizedTransaction(
        occurred_on=date(2026, 4, 8),
        merchant_raw="NETFLIX.COM",
        merchant_normalized="netflix.com",
        amount_nis=69.90,
        direction="debit",
        tx_type="standing_order",
        raw_row={"foo": "bar"},
    )
    assert tx.amount_nis == 69.90


def test_direction_is_constrained():
    with pytest.raises(Exception):
        NormalizedTransaction(
            occurred_on=date(2026, 4, 8),
            merchant_raw="x", merchant_normalized="x",
            amount_nis=1, direction="something",  # invalid
            tx_type="regular", raw_row={},
        )


def test_tx_type_is_constrained():
    with pytest.raises(Exception):
        NormalizedTransaction(
            occurred_on=date(2026, 4, 8),
            merchant_raw="x", merchant_normalized="x",
            amount_nis=1, direction="debit",
            tx_type="bogus",  # invalid
            raw_row={},
        )


def test_parse_result_round_trip():
    txs = [NormalizedTransaction(
        occurred_on=date(2026, 4, 8),
        merchant_raw="x", merchant_normalized="x",
        amount_nis=10, direction="debit", tx_type="regular",
        raw_row={},
    )]
    pr = ParseResult(
        statement=StatementMeta(
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            charge_date=date(2026, 4, 15),
            declared_total_nis=10, parsed_total_nis=10,
        ),
        transactions=txs,
    )
    assert pr.statement.declared_total_nis == 10
    assert len(pr.transactions) == 1


def test_ground_truth_optional_declared():
    gt = GroundTruth(row_count=5, sum_debits_nis=100, sum_credits_nis=0,
                     declared_total_nis=None)
    assert gt.declared_total_nis is None


def test_parser_name_enum_values():
    assert ParserName.LEUMI_OSH == "leumi_osh"
    assert ParserName.ISRACARD == "isracard"
    assert ParserName.MAX == "max"
