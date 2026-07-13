"""Relabel parent categories: travel -> "Vacation", transportation -> "Car".

Data-only migration, owner's framing (2026-07-13) for the Bottom-line
parent grouping: all travel.* leaves cluster under "Vacation"; all
transportation.* (fuel, car insurance/maintenance, parking, transit,
taxi) under "Car". Guarded on the OLD label so user-customized labels
are never clobbered.

Revision ID: 0092_vacation_car_parent_labels
Revises: 0091_insurance_labels_self_contained
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092_vacation_car_parent_labels"
down_revision = "0091_insurance_labels_self_contained"
branch_labels = None
depends_on = None

# (slug, old_en, new_en, old_he, new_he)
_RELABELS = [
    ("travel", "Travel", "Vacation", "נסיעות", "חופשות"),
    ("transportation", "Transportation", "Car", "תחבורה", "רכב ותחבורה"),
]


def upgrade() -> None:
    conn = op.get_bind()
    for slug, old_en, new_en, old_he, new_he in _RELABELS:
        conn.execute(
            sa.text(
                "UPDATE expense_categories "
                "SET label_en = :new_en, label_he = :new_he "
                "WHERE slug = :slug AND label_en = :old_en"
            ),
            {"slug": slug, "old_en": old_en,
             "new_en": new_en, "new_he": new_he},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for slug, old_en, new_en, old_he, new_he in _RELABELS:
        conn.execute(
            sa.text(
                "UPDATE expense_categories "
                "SET label_en = :old_en, label_he = :old_he "
                "WHERE slug = :slug AND label_en = :new_en"
            ),
            {"slug": slug, "old_en": old_en, "old_he": old_he,
             "new_en": new_en},
        )
