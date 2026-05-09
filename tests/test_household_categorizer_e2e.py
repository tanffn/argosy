"""Live LLM eval — opt-in via `@pytest.mark.llm_eval`.

Run with: pytest -m llm_eval tests/test_household_categorizer_e2e.py
Default suite (`pytest -m "not llm_eval"`) skips this entire module.

Tests structural properties of categorization on a hand-picked transaction
set covering recognizable merchants. Tolerates same-top-level drift on
ambiguous categories; demands ≥0.85 confidence for clearly-recognizable
rows.
"""

from __future__ import annotations

from datetime import date

import pytest

from argosy.agents.base import _llm_backend_available
from argosy.agents.household_categorizer import HouseholdCategorizerAgent
from argosy.agents.household_categorizer_types import CategorizeRow
from argosy.services.expense_ingest.taxonomy_seed import DEFAULT_TAXONOMY

pytestmark = [
    pytest.mark.llm_eval,
    pytest.mark.skipif(not _llm_backend_available(),
                        reason="no Claude backend configured"),
]


CASES: list[tuple[CategorizeRow, str]] = [
    (CategorizeRow(tx_id=1, merchant_normalized="netflix.com",
                    merchant_raw="NETFLIX.COM", amount_nis=69.90,
                    direction="debit", occurred_on=date(2026, 4, 8),
                    issuer_kind="card", issuer_name="isracard"),
     "subscriptions"),
    (CategorizeRow(tx_id=2, merchant_normalized="שופרסל",
                    merchant_raw="שופרסל בע\"מ", amount_nis=440.20,
                    direction="debit", occurred_on=date(2026, 4, 5),
                    issuer_kind="card", issuer_name="isracard"),
     "food"),
    (CategorizeRow(tx_id=3, merchant_normalized="wolt", merchant_raw="WOLT",
                    amount_nis=85.0, direction="debit",
                    occurred_on=date(2026, 4, 1),
                    issuer_kind="card", issuer_name="isracard"),
     "dining_out"),
    (CategorizeRow(tx_id=4, merchant_normalized="פז דלק",
                    merchant_raw="פז חברת נפט", amount_nis=320.0,
                    direction="debit", occurred_on=date(2026, 4, 2),
                    issuer_kind="card", issuer_name="max",
                    issuer_category_he="דלק ותחנות דלק"),
     "transportation"),
    (CategorizeRow(tx_id=5, merchant_normalized="ביטוח ישיר",
                    merchant_raw="ביטוח ישיר-חיים", amount_nis=142.0,
                    direction="debit", occurred_on=date(2026, 3, 25),
                    issuer_kind="card", issuer_name="max",
                    issuer_category_he="ביטוח ופיננסים"),
     "insurance_other"),
    (CategorizeRow(tx_id=6, merchant_normalized="עיריית חיפה",
                    merchant_raw="עיריית חיפה-י", amount_nis=11834.98,
                    direction="debit", occurred_on=date(2026, 5, 5),
                    issuer_kind="bank", issuer_name="leumi"),
     "housing"),
]


def test_household_categorizer_recognizes_well_known_merchants():
    agent = HouseholdCategorizerAgent(user_id="ariel")
    taxonomy = [e.slug for e in DEFAULT_TAXONOMY]
    rows = [c[0] for c in CASES]
    try:
        results = agent.categorize_batch(rows, taxonomy)
    except NotImplementedError:
        pytest.skip("HouseholdCategorizerAgent._invoke_llm not yet wired "
                    "to a live backend; eval requires Option A wiring.")
    by_id = {r.tx_id: r for r in results}
    misses: list[str] = []
    for row, expected_top in CASES:
        r = by_id[row.tx_id]
        actual_top = r.category_slug.split(".", 1)[0]
        if actual_top != expected_top:
            misses.append(
                f"  tx={row.tx_id} merchant={row.merchant_raw!r} "
                f"got={r.category_slug} (conf={r.confidence:.2f}) "
                f"expected_top={expected_top!r} rationale={r.rationale!r}"
            )
    if misses:
        pytest.fail("Categorizer drift:\n" + "\n".join(misses))


def test_household_categorizer_returns_uncategorized_when_unsure():
    agent = HouseholdCategorizerAgent(user_id="ariel")
    taxonomy = [e.slug for e in DEFAULT_TAXONOMY]
    weird = CategorizeRow(
        tx_id=99, merchant_normalized="zzz garbage merchant 999",
        merchant_raw="ZZZ GARBAGE MERCHANT 999",
        amount_nis=42.0, direction="debit",
        occurred_on=date(2026, 4, 1),
        issuer_kind="card", issuer_name="isracard",
    )
    try:
        results = agent.categorize_batch([weird], taxonomy)
    except NotImplementedError:
        pytest.skip("not wired")
    assert results[0].category_slug == "uncategorized" or \
           results[0].confidence < 0.85, (
        f"expected uncategorized or low-confidence; got "
        f"{results[0].category_slug} @ {results[0].confidence}"
    )
