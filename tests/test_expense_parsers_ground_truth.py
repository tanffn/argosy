"""Conservation tests: parser output must match the ground-truth oracle.

These tests skip without ARGOSY_EXPENSE_SAMPLES_ROOT. On a developer
machine with the samples present, ALL parametrized cases must pass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.expense_ground_truth import (
    leumi_oracle, isracard_oracle, max_oracle, discount_oracle,
)

SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
pytestmark = pytest.mark.skipif(
    not SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset"
)


def _root() -> Path:
    return Path(SAMPLES)


def _all_existing(*patterns) -> list[Path]:
    out: list[Path] = []
    root = _root()
    for sub in patterns:
        for p in root.glob(sub):
            if p.is_file():
                out.append(p)
    return out


@pytest.fixture(scope="module")
def leumi_samples():
    paths = _all_existing("**/Leumi/leumi_*.xls")
    if not paths:
        pytest.skip("no Leumi samples present")
    return paths


def test_leumi_parser_conservation(leumi_samples):
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    for p in leumi_samples:
        truth = leumi_oracle(p)
        result = parse(p)
        # NIS-only sums (Bug 2 part 1): rows with amount_nis IS NULL (foreign)
        # are excluded — the oracle mirrors this exclusion.
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit" and t.amount_nis is not None)
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit" and t.amount_nis is not None)
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count drift parser={len(result.transactions)} "
            f"oracle={truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00, (
            f"{p.name}: debit sum drift parser={debits} oracle={truth.sum_debits_nis}"
        )
        assert abs(credits - truth.sum_credits_nis) < 1.00, (
            f"{p.name}: credit sum drift parser={credits} oracle={truth.sum_credits_nis}"
        )


@pytest.fixture(scope="module")
def isracard_samples():
    # Both Ariel's card 1266 and Noga's card 0235 use the Isracard format.
    paths = _all_existing("**/1266/*.xlsx") + _all_existing("**/0235/*.xlsx")
    if not paths:
        pytest.skip("no Isracard samples present")
    return paths


def test_isracard_parser_conservation(isracard_samples):
    from argosy.services.expense_ingest.parsers.isracard import parse
    for p in isracard_samples:
        truth = isracard_oracle(p)
        result = parse(p)
        # NIS-only sums (Bug 2 part 1): foreign rows have amount_nis=None.
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit" and t.amount_nis is not None)
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit" and t.amount_nis is not None)
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count drift {len(result.transactions)} vs {truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00, (
            f"{p.name}: debit drift {debits} vs {truth.sum_debits_nis}"
        )
        assert abs(credits - truth.sum_credits_nis) < 1.00, (
            f"{p.name}: credit drift {credits} vs {truth.sum_credits_nis}"
        )
        # Issuer footer reconciliation (within ₪50, looser per spec)
        if truth.declared_total_nis is not None:
            assert abs(result.statement.parsed_total_nis - truth.declared_total_nis) < 50.00, (
                f"{p.name}: parsed total {result.statement.parsed_total_nis} "
                f"vs declared {truth.declared_total_nis}"
            )


@pytest.fixture(scope="module")
def max_samples():
    paths = _all_existing("**/6225/*.xlsx")
    if not paths:
        pytest.skip("no Max samples present")
    return paths


def test_max_parser_conservation(max_samples):
    from argosy.services.expense_ingest.parsers.max import parse
    for p in max_samples:
        truth = max_oracle(p)
        result = parse(p)
        # NIS-only sums (Bug 2 part 1) — currently Max parser pre-converts
        # foreign charges so amount_nis is always non-None, but guard anyway.
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit" and t.amount_nis is not None)
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit" and t.amount_nis is not None)
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count {len(result.transactions)} vs {truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00
        assert abs(credits - truth.sum_credits_nis) < 1.00
        if truth.declared_total_nis is not None:
            assert abs(result.statement.parsed_total_nis
                       - truth.declared_total_nis) < 50.00


@pytest.fixture(scope="module")
def discount_samples():
    paths = _all_existing("**/2923/transaction-details_export_*.xlsx")
    if not paths:
        pytest.skip("no Discount samples present")
    return paths


def test_discount_parser_conservation(discount_samples):
    from argosy.services.expense_ingest.parsers.discount import parse
    for p in discount_samples:
        truth = discount_oracle(p)
        result = parse(p)
        # NIS-only sums (Bug 2 part 1) — Discount parser stores converted NIS,
        # but guard for forward-compat.
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit" and t.amount_nis is not None)
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit" and t.amount_nis is not None)
        assert len(result.transactions) == truth.row_count, (
            f"{p.name}: row count {len(result.transactions)} vs {truth.row_count}"
        )
        assert abs(debits - truth.sum_debits_nis) < 1.00, (
            f"{p.name}: debit sum {debits:.2f} vs oracle {truth.sum_debits_nis:.2f}"
        )
        assert abs(credits - truth.sum_credits_nis) < 1.00, (
            f"{p.name}: credit sum {credits:.2f} vs oracle {truth.sum_credits_nis:.2f}"
        )
