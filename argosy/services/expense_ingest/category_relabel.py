"""Guarded expense_categories relabel used by data-only alembic revisions.

Both ``label_en`` and ``label_he`` must match the expected old values
before overwrite — a user who customized only one language must not have
the other clobbered.
"""

from __future__ import annotations

import sqlalchemy as sa


def relabel_category(
    conn,
    *,
    slug: str,
    old_en: str,
    new_en: str,
    old_he: str,
    new_he: str,
) -> None:
    conn.execute(
        sa.text(
            "UPDATE expense_categories "
            "SET label_en = :new_en, label_he = :new_he "
            "WHERE slug = :slug "
            "AND label_en = :old_en AND label_he = :old_he"
        ),
        {
            "slug": slug,
            "old_en": old_en,
            "new_en": new_en,
            "old_he": old_he,
            "new_he": new_he,
        },
    )
