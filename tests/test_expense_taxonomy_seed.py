"""Tests for default expense-category taxonomy seeding."""

from __future__ import annotations

from sqlalchemy.orm import Session

from argosy.services.expense_ingest.taxonomy_seed import (
    DEFAULT_TAXONOMY,
    seed_system_defaults,
    seed_user_categories,
)
from argosy.state.models import ExpenseCategory, User


def test_default_taxonomy_has_required_top_levels():
    slugs = {entry.slug for entry in DEFAULT_TAXONOMY}
    # Top-level rules from the spec §4.2
    for s in ("food.groceries", "dining_out.restaurants",
              "income.salary", "transfers.internal_transfer",
              "investments.broker_buy_us", "uncategorized"):
        assert s in slugs, f"taxonomy missing slug {s}"


def test_food_is_groceries_only():
    """Per user direction: 'restaurants should not be under food'."""
    food_children = {e.slug for e in DEFAULT_TAXONOMY
                     if e.slug.startswith("food.")}
    assert food_children == {"food.groceries"}, food_children


def test_dining_out_is_top_level_with_restaurants():
    do_slugs = {e.slug for e in DEFAULT_TAXONOMY
                if e.slug.startswith("dining_out.")}
    assert "dining_out.restaurants" in do_slugs
    assert "dining_out.takeout" in do_slugs


def test_excluded_categories_marked_correctly():
    by_slug = {e.slug: e for e in DEFAULT_TAXONOMY}
    for s in ("transfers.internal_transfer",
              "investments.broker_buy_us",
              "investments.retirement_contrib",
              "taxes.income_tax_paid"):
        assert by_slug[s].is_excluded_from_spend, f"{s} should be excluded"
    assert by_slug["food.groceries"].is_excluded_from_spend is False


def test_inflow_categories_marked_correctly():
    by_slug = {e.slug: e for e in DEFAULT_TAXONOMY}
    for s in ("income.salary", "income.rsu_vest_proceeds",
              "income.bonus", "income.child_benefit",
              "income.interest_credit", "income.other_recurring_income"):
        assert by_slug[s].is_inflow
    assert by_slug["food.groceries"].is_inflow is False


def test_seed_system_defaults_is_idempotent(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        seed_system_defaults(s)
        s.commit()
        n1 = s.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)).count()
        seed_system_defaults(s)
        s.commit()
        n2 = s.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)).count()
        assert n1 == n2 == len(DEFAULT_TAXONOMY)


def test_seed_user_categories_copies_from_defaults(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        s.add(User(id="ariel", plan="free"))
        seed_system_defaults(s)
        s.commit()
        seed_user_categories(s, "ariel")
        s.commit()
        n_user = s.query(ExpenseCategory).filter_by(user_id="ariel").count()
        n_sys = s.query(ExpenseCategory).filter(
            ExpenseCategory.user_id.is_(None)).count()
        assert n_user == n_sys
        # Re-running is a noop
        seed_user_categories(s, "ariel")
        s.commit()
        assert s.query(ExpenseCategory).filter_by(user_id="ariel").count() == n_user
