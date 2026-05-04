"""rename prices_cache → kv_cache.

Revision ID: 0011_rename_prices_cache_to_kv_cache
Revises: 0010_pension_snapshots
Create Date: 2026-05-04

The original ``prices_cache`` table has always been a generic
key/value/TTL store keyed by ``(provider, key)``. It backs the prices
adapters, the gemelnet adapter, the SEC 13F / Form 4 / TipRanks
adapters, and (since Phase 1) UI snapshots like ``advisor_home_brief``.
The ``prices_cache`` name was misleading — every new caller had to
re-explain "this isn't actually a prices table". Renaming to
``kv_cache`` so the schema describes what it is.

The ``CacheKind`` enum selects which physical cache table backs the
call; for ``KvCacheEntry``-backed callers (``PRICES`` and ``UI``), it
is informational only — the namespace comes from the ``provider``
field (the composite PK is ``(provider, key)``), not from ``kind``.
The home-brief endpoint is migrated from ``CacheKind.PRICES`` to
``CacheKind.UI`` in the same change set, with
``provider="advisor_home_brief"`` providing the actual isolation.

Idempotent: the upgrade is a no-op if ``prices_cache`` is already gone
AND ``kv_cache`` already exists (e.g., a fresh
``Base.metadata.create_all`` from tests already created ``kv_cache``
directly). If neither table exists we raise rather than silently
no-op so an operator running the migration on a misconfigured DB sees
a clear failure. The downgrade is symmetric.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "0011_rename_prices_cache_to_kv_cache"
down_revision: Union[str, Sequence[str], None] = "0010_pension_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    tables = _table_names()
    if "prices_cache" in tables and "kv_cache" not in tables:
        op.rename_table("prices_cache", "kv_cache")
    elif "prices_cache" in tables and "kv_cache" in tables:
        # Both present (shouldn't happen in practice, but be safe). Drop
        # the empty prices_cache so we converge.
        op.drop_table("prices_cache")
    elif "kv_cache" in tables:
        # Already migrated (e.g., a fresh ``Base.metadata.create_all``
        # from tests created ``kv_cache`` directly) — nothing to do.
        return
    else:
        # Neither table exists. Don't silently no-op — that lets a
        # misconfigured DB run the rest of the migration chain on top
        # of a missing cache table and fail much later in a much
        # harder-to-diagnose way.
        raise RuntimeError(
            "Migration 0011: neither prices_cache nor kv_cache exists. "
            "Restore from backup or run Base.metadata.create_all() first."
        )


def downgrade() -> None:
    tables = _table_names()
    if "kv_cache" in tables and "prices_cache" not in tables:
        op.rename_table("kv_cache", "prices_cache")
    elif "kv_cache" in tables and "prices_cache" in tables:
        op.drop_table("kv_cache")
