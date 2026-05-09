"""Sanity tests for the parser-independent ground-truth oracle.

These tests skip if ARGOSY_EXPENSE_SAMPLES_ROOT is not set, so CI without
the data passes silently. On a developer machine with the data present
they MUST pass — the oracle is foundational.
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


def _samples_root() -> Path:
    return Path(SAMPLES)


def test_leumi_oracle_runs_on_2026_may():
    p = _samples_root() / "2026" / "Leumi" / "leumi_2026_May_Osh.xls"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = leumi_oracle(p)
    assert gt.row_count > 0
    assert gt.sum_debits_nis > 0
    # Bank statements have no declared footer total
    assert gt.declared_total_nis is None


def test_isracard_oracle_runs_on_card_1266_apr_2026():
    p = _samples_root() / "2026" / "1266" / "1266_04_2026.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = isracard_oracle(p)
    assert gt.row_count > 0
    assert gt.sum_debits_nis > 0
    assert gt.declared_total_nis is not None
    # The footer total must reconcile with our independent column-sum:
    diff = abs(gt.sum_debits_nis - gt.sum_credits_nis - gt.declared_total_nis)
    assert diff < 50.00, (
        f"Isracard oracle: column sums {gt.sum_debits_nis} debit "
        f"{gt.sum_credits_nis} credit do not reconcile to declared "
        f"{gt.declared_total_nis} (diff {diff})"
    )


def test_max_oracle_runs_on_card_6225_apr_2026():
    p = _samples_root() / "2026" / "6225" / "Apr.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = max_oracle(p)
    assert gt.row_count > 0
    assert gt.sum_debits_nis > 0
    assert gt.declared_total_nis is not None
    diff = abs(gt.sum_debits_nis - gt.sum_credits_nis - gt.declared_total_nis)
    assert diff < 50.00


def test_isracard_april_2026_has_known_total():
    """Hard-coded sanity: this exact file's footer total is 3319.44 NIS."""
    p = _samples_root() / "2026" / "1266" / "1266_04_2026.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = isracard_oracle(p)
    assert abs(gt.declared_total_nis - 3319.44) < 0.01, (
        f"declared total drifted from known 3319.44; got {gt.declared_total_nis}"
    )


def test_max_april_2026_has_known_total():
    """Hard-coded sanity: this exact file's footer total is 654.88 NIS."""
    p = _samples_root() / "2026" / "6225" / "Apr.xlsx"
    if not p.exists():
        pytest.skip(f"sample missing: {p}")
    gt = max_oracle(p)
    assert abs(gt.declared_total_nis - 654.88) < 0.01
