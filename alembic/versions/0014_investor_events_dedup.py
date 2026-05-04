"""phase4 (investor events): unique_key + dedup constraint.

Revision ID: 0014_investor_events_dedup
Revises: 0013_pensions_to_dict_shape
Create Date: 2026-05-04

Without dedup, the daily-brief loop pulls Form 4 / 13F / news /
CapitolTrades / TipRanks rows on every tick (lookback windows of
14-90 days). The same insider trade with accession ``0001-25-000002``
landed in 30 consecutive ticks, the table held 30 identical rows;
the home brief signal-bullet picker (``ORDER BY occurred_at DESC``)
still picked the right row, but the table grew unboundedly.

Fix: adapter-specific natural keys baked into a ``unique_key`` column,
plus a ``UniqueConstraint(user_id, source, unique_key)``. Writers use
``INSERT ... ON CONFLICT DO NOTHING`` (SQLite + Postgres dialects) so
duplicate ticks are no-ops.

Backfill: existing rows on production DBs are rare (Phase 4 just
shipped), but we still backfill ``unique_key`` from ``payload_json``
during ``upgrade()`` so the unique constraint applies cleanly. The
backfill mirrors the keying logic in
``argosy.state.queries._unique_key`` — kept in lockstep there.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_investor_events_dedup"
down_revision: str | Sequence[str] | None = "0013_pensions_to_dict_shape"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def _backfill_unique_key(source: str, payload_json: str, ticker: str | None,
                         occurred_at_iso: str | None, headline: str) -> str:
    """Reproduce the keying logic from ``argosy.state.queries._unique_key``.

    Kept in this migration as a self-contained helper so the upgrade
    path doesn't import from app code (which is allowed but discouraged
    for migrations). If the keying logic changes, update both sides.
    """
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except (TypeError, ValueError):
        payload = {}
    t = (ticker or "").upper()
    occ = (occurred_at_iso or "").strip()

    if source == "sec_form4":
        accession = (payload.get("accession_number") or
                     payload.get("accession") or "")
        if accession:
            return f"{t}:{accession}"
    if source == "sec_13f":
        cik = str(payload.get("cik") or "")
        accession = (payload.get("accession_number") or
                     payload.get("accession") or "")
        if accession:
            return f"{cik}:{accession}"
    if source == "capitoltrades":
        trade_id = str(payload.get("trade_id") or payload.get("id") or "")
        if trade_id:
            return f"{t}:{trade_id}"
        politician = str(payload.get("politician_name") or "")
        return f"{t}:{occ}:{politician}"
    if source == "tipranks":
        return f"{t}:{occ}"
    if source == "news":
        url = str(payload.get("url") or "")
        if url:
            return f"{t}:{url}"
        title = str(payload.get("headline") or payload.get("title") or "")
        return f"{t}:{_hash(title)}"
    # Unknown source — fall back to hashing the headline so dedup at
    # least catches verbatim repeats.
    return f"{t}:{_hash(headline or '')}"


def upgrade() -> None:
    # 1. Add the column nullable so the backfill UPDATE doesn't fight
    #    the NOT NULL default. (SQLite ALTER TABLE ADD COLUMN can't
    #    provide an expression default; we set rows explicitly.)
    with op.batch_alter_table("investor_events") as batch:
        batch.add_column(
            sa.Column(
                "unique_key",
                sa.String(length=128),
                nullable=True,
                server_default="",
            )
        )

    # 2. Backfill unique_key for every existing row.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, source, payload_json, ticker, occurred_at, headline "
            "FROM investor_events"
        )
    ).fetchall()
    for row in rows:
        rid = row[0]
        source = row[1] or ""
        payload_json = row[2] or ""
        ticker = row[3]
        occurred_at = row[4]
        headline = row[5] or ""
        occ_iso = occurred_at.isoformat() if hasattr(occurred_at, "isoformat") \
            else (str(occurred_at) if occurred_at else "")
        uk = _backfill_unique_key(source, payload_json, ticker, occ_iso, headline)
        # Salt with row id when uk is empty so the constraint still
        # applies — empty + empty would collide across distinct empty
        # rows, which we don't want.
        if not uk:
            uk = f"row:{rid}"
        bind.execute(
            sa.text(
                "UPDATE investor_events SET unique_key = :uk WHERE id = :rid"
            ),
            {"uk": uk[:128], "rid": rid},
        )

    # 3. Tighten to NOT NULL + add the unique constraint.
    with op.batch_alter_table("investor_events") as batch:
        batch.alter_column(
            "unique_key",
            existing_type=sa.String(length=128),
            nullable=False,
            server_default="",
        )
        batch.create_unique_constraint(
            "uq_investor_events_user_source_uniquekey",
            ["user_id", "source", "unique_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("investor_events") as batch:
        batch.drop_constraint(
            "uq_investor_events_user_source_uniquekey", type_="unique"
        )
        batch.drop_column("unique_key")
