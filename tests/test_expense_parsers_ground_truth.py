"""Conservation tests: parser output must match the ground-truth oracle.

These tests skip without ARGOSY_EXPENSE_SAMPLES_ROOT. On a developer
machine with the samples present, ALL parametrized cases must pass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.expense_ground_truth import (
    leumi_oracle, isracard_oracle, max_oracle,
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
        debits = sum(t.amount_nis for t in result.transactions
                     if t.direction == "debit")
        credits = sum(t.amount_nis for t in result.transactions
                      if t.direction == "credit")
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
