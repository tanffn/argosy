"""Relabel insurance_other children to self-contained display labels.

Data-only migration. Dashboard rollups render leaf categories FLAT, so
parent-relative labels ("Other", "Home", "Life", "Umbrella") are
unreadable standalone — the owner mistook the bare "Other" row
(insurance_other.other) for the Vacation fold's nested Other. The seed
(`taxonomy_seed.py`) is insert-only, so existing rows need this update.
Guarded on the OLD label so user-customized labels are never clobbered.

Revision ID: 0091_insurance_labels_self_contained
Revises: 0090_expense_source_login_url
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0091_insurance_labels_self_contained"
down_revision = "0090_expense_source_login_url"
branch_labels = None
depends_on = None

# (slug, old_en, new_en, old_he, new_he)
_RELABELS = [
    ("insurance_other.life", "Life", "Life insurance",
     "ביטוח חיים", "ביטוח חיים"),
    ("insurance_other.home", "Home", "Home insurance",
     "ביטוח דירה", "ביטוח דירה"),
    ("insurance_other.umbrella", "Umbrella", "Umbrella insurance",
     "ביטוח-על", "ביטוח-על"),
    ("insurance_other.other", "Other", "Insurance (other)",
     "אחר", "ביטוח אחר"),
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
