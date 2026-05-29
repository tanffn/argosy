"""state_snapshots + monitor_flags.dedup_key + kind CHECK relaxation.

Revision ID: 0049_state_snapshots_and_monitor_flags
Revises: 0048_job_runs
Create Date: 2026-05-29

Spec B (state-observer) commit #1 — schema groundwork for the general
state-observer agent. See
``docs/superpowers/specs/2026-05-29-state-observer-agent-design.md`` §1.2
(snapshot fields), §4 (flag writer + dedup_key formula + inferred_kind
table), §8 (schema-change summary), Appendix A (state_snapshots DDL).

**Note on the revision number** — the spec text says "Migration 0048".
Sprint A (jobs-registry) shipped first and claimed 0048 for ``job_runs``,
so this revision is renumbered to 0049. The semantic content is
identical to the spec's §8.

Three operations in this migration:

1. **Create ``state_snapshots`` table.** Stores the user's six-section
   ``current_state`` dict (plan_inputs / portfolio / macro /
   cashflow_recent / tax_assumptions / metadata) per spec §1.2 as a
   JSON blob in ``state_json``, with ``source_versions_json`` capturing
   which adapter versions + ``as_of`` timestamps were used. Both JSON
   columns carry a ``json_valid`` CHECK so corrupted writes fail at
   write time, not at LLM-input-assembly time. Idempotent per
   ``(user_id, snapshot_date)`` via UNIQUE constraint so the daily
   cron + on-demand triggers from spec §7.3 can't double-write the
   same calendar day.

2. **Add ``monitor_flags.dedup_key`` column** (TEXT NULL). The
   observer flag-writer (spec §4.2) constructs a deterministic
   ``v1|state_observer|<user_id>|<inferred_kind>|<primary_field>|<deviation_bucket>``
   key and stores it here. A partial UNIQUE index on
   ``(user_id, dedup_key) WHERE dedup_key IS NOT NULL AND
   acknowledged_at IS NULL`` enforces "one active flag per dedup key";
   once the user acknowledges it the same key can re-fire after the
   conditions change. Matches the index-shape precedent from migration
   0047 (expense_review_queue).

3. **Relax the ``monitor_flags.kind`` CHECK** to admit the
   ``state_observer_*`` family. Spec §4.2 enumerates twelve
   inferred-kind suffixes (``fx_observation`` / ``rates_observation``
   / ``equity_observation`` / ``volatility_observation`` /
   ``allocation_observation`` / ``position_observation`` /
   ``concentration_observation`` / ``cash_observation`` /
   ``cashflow_observation`` / ``tax_observation`` /
   ``plan_assumption_observation`` / ``other_observation``). The new
   CHECK is the explicit enumeration of:

     - The three legacy kinds from migration 0043
       (``allocation_drift`` / ``mc_regression`` / ``macro_shift``).
     - The twelve new ``state_observer_<suffix>`` kinds.

   **Design choice — explicit enum, NOT ``LIKE 'state_observer_%'``.**
   The permissive prefix variant is shorter but lets a typo
   (``state_observer_fx_observatioN`` with a capital N) silently land
   in the table; the explicit enum surfaces the typo at write time.
   Spec §4 also explicitly enumerates the same twelve in the partial
   unique index predicate (§4.3 DDL); keeping the CHECK list aligned
   to that predicate prevents two sources of truth from drifting.

   CHECK relaxation on SQLite requires the batch-rebuild pattern
   (drop + recreate the table with the new constraint). We use
   ``op.batch_alter_table`` — same pattern as migration 0047.

**Pre-migration safety preflight (codex BLOCKER #6 from the spec).**
Before relaxing the CHECK, the upgrade scans existing ``monitor_flags``
rows for any ``kind`` value that won't pass the new constraint. If
any are found the migration raises ``RuntimeError`` with the offending
distinct values listed; without this guard the SQLite copy-rename
pattern would silently drop those rows during the table rebuild.

SQLite version requirement: ``json_valid`` requires SQLite >= 3.38
(already an Argosy baseline — see ``argosy/config.py``). Partial-index
WHERE clauses are SQLite-supported (same pattern exercised in
migrations 0040, 0043, 0047, 0048).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_state_snapshots_and_monitor_flags"
down_revision: str | None = "0048_job_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Legacy kinds preserved from migration 0043.
_LEGACY_FLAG_KINDS = (
    "allocation_drift",
    "mc_regression",
    "macro_shift",
)

# New ``state_observer_*`` kinds — the twelve inferred-kind suffixes from
# spec §4.2's mapping table, prefixed with ``state_observer_``.
_OBSERVER_FLAG_KINDS = (
    "state_observer_fx_observation",
    "state_observer_rates_observation",
    "state_observer_equity_observation",
    "state_observer_volatility_observation",
    "state_observer_allocation_observation",
    "state_observer_position_observation",
    "state_observer_concentration_observation",
    "state_observer_cash_observation",
    "state_observer_cashflow_observation",
    "state_observer_tax_observation",
    "state_observer_plan_assumption_observation",
    "state_observer_other_observation",
)

_ALL_FLAG_KINDS = _LEGACY_FLAG_KINDS + _OBSERVER_FLAG_KINDS


def _kinds_sql(kinds: Sequence[str]) -> str:
    return ", ".join(repr(k) for k in kinds)


def _preflight_kinds() -> None:
    """Raise loudly if any existing row would fail the new CHECK.

    Without this guard, the SQLite batch-rebuild pattern would copy
    every passing row and silently DROP rows that violate the new
    constraint — exactly the failure mode codex BLOCKER #6 of the spec
    calls out. Better to surface the unknown values to the operator
    and let them remediate (UPDATE to a known kind, or extend the
    enum) than to lose audit data silently.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("monitor_flags"):
        # Fresh DB; nothing to preflight.
        return

    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT kind FROM monitor_flags "
            "WHERE kind NOT IN (" + _kinds_sql(_ALL_FLAG_KINDS) + ")"
        )
    ).fetchall()
    unknown = sorted(r[0] for r in rows)
    if unknown:
        raise RuntimeError(
            "Migration 0049 preflight failed: monitor_flags contains "
            f"kind values that are not in the new CHECK enum: {unknown}. "
            "Remediate (UPDATE offending rows to a known kind, or "
            "extend _ALL_FLAG_KINDS in this migration) before retrying. "
            "See migration 0049 docstring for the full enum."
        )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. state_snapshots table
    # ------------------------------------------------------------------
    op.create_table(
        "state_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        # The six-section dict from spec §1.2.
        sa.Column("state_json", sa.Text, nullable=False),
        # Adapter versions + as_of timestamps + replay-gap list per spec
        # Appendix A.
        sa.Column("source_versions_json", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "json_valid(state_json)",
            name="ck_state_snapshots_state_json_valid",
        ),
        sa.CheckConstraint(
            "json_valid(source_versions_json)",
            name="ck_state_snapshots_source_versions_json_valid",
        ),
        sa.UniqueConstraint(
            "user_id",
            "snapshot_date",
            name="uq_state_snapshots_user_date",
        ),
    )

    # Covering index for the "give me this user's snapshots, newest first"
    # query (observer's diff service reads the prior snapshot via this
    # path — spec §2.1).
    op.create_index(
        "ix_state_snapshots_user_date",
        "state_snapshots",
        ["user_id", sa.text("snapshot_date DESC")],
    )

    # ------------------------------------------------------------------
    # 2. monitor_flags.dedup_key + CHECK relaxation
    # ------------------------------------------------------------------
    _preflight_kinds()

    # SQLite ALTER cannot DROP+ADD a CHECK constraint in one statement.
    # The batch-rebuild pattern (drop + recreate the table with new
    # constraints applied) is the alembic-blessed path — same shape as
    # migration 0047.
    with op.batch_alter_table("monitor_flags") as batch:
        batch.add_column(sa.Column("dedup_key", sa.Text, nullable=True))
        # Drop the old CHECK from migration 0043 and re-create with the
        # extended enum. Constraint name carried verbatim from 0043 so
        # the downgrade can re-establish the original constraint with
        # the same name.
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_kinds_sql(_ALL_FLAG_KINDS)})",
        )

    # Partial UNIQUE index — observer flag dedup.
    #
    # Spec §4.3 idempotency contract has three branches; each branch
    # is enforced at one of two layers (DB constraint vs writer code):
    #
    #              Writer decision   DB-level enforcement
    #   ---        ---------------   --------------------
    #   (a) active     SKIP write     Strict — partial-unique index
    #                                  rejects duplicate insert; the
    #                                  writer's "skip" is also a
    #                                  safety net so the DB never has
    #                                  to refuse a query.
    #   (b) expired    WRITE          DB allows after writer tomb-
    #                                  stones the expired peer (see
    #                                  branch-(b) section below);
    #                                  without the tombstone the
    #                                  insert would violate the
    #                                  strict index, which is the
    #                                  right failure mode if the
    #                                  writer skipped its preflight.
    #   (c) ack'd      SKIP write     DB ALLOWS (acknowledged_at IS
    #                                  NOT NULL excludes the old
    #                                  row from the index scope, so
    #                                  the DB would accept a new
    #                                  row); the writer chooses to
    #                                  skip per the spec's "re-firing
    #                                  is noise" guidance until the
    #                                  dedup_key changes (typically
    #                                  via deviation_bucket
    #                                  transitions).
    #
    # In short — the DB enforces only (a); (b) and (c) are writer
    # responsibility. The DB constraint is the FLOOR — it prevents
    # the worst case (silent duplicate fires) even if writer code
    # gets a bug.
    #
    # Spec §4.3's SQL puts all three conditions
    # (``dedup_key IS NOT NULL AND acknowledged_at IS NULL AND
    # (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)``) in the
    # partial-index predicate. **That literal SQL is not portable to
    # SQLite** — SQLite explicitly forbids non-deterministic functions
    # (including ``CURRENT_TIMESTAMP``) in partial-index WHERE clauses:
    #   https://www.sqlite.org/partialindex.html
    #   https://sqlite.org/deterministic.html
    # The predicate would be evaluated once at index-maintenance time
    # with ``CURRENT_TIMESTAMP`` captured as a constant — exactly the
    # wrong semantics for "is this flag currently expired?".
    #
    # Resolution — keep the index STRICT, push expiry handling into
    # the writer. The predicate is:
    #
    #     dedup_key IS NOT NULL AND acknowledged_at IS NULL
    #
    # This DB-level constraint enforces "at most one unacknowledged
    # row per dedup_key" — i.e. it handles branch (a) strictly and
    # also strictly-blocks an insert against an unacknowledged-but-
    # expired peer. The flag-writer (sprint commit #6,
    # ``state_observer_flag_writer.py``) handles branch (b) by
    # TOMBSTONING the expired peer first:
    #
    #     UPDATE monitor_flags SET acknowledged_at = CURRENT_TIMESTAMP
    #       WHERE dedup_key = ? AND acknowledged_at IS NULL
    #         AND expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP;
    #     INSERT INTO monitor_flags (..., dedup_key = ?);
    #
    # The tombstone moves the old row out of the partial-index scope
    # (``acknowledged_at IS NULL`` is no longer true), making room
    # for the fresh insert. Branch (c) — already-acknowledged peer —
    # is similarly out of scope at the DB layer; the writer's per-
    # spec decision is to SKIP the write (re-firing is noise until
    # the dedup_key changes), but the DB does not enforce that skip.
    #
    # This is stricter than the spec's literal SQL: branch (a)
    # uniqueness is unconditional regardless of whether ``expires_at``
    # is populated. The writer must explicitly tombstone before
    # re-firing. **Codex Spec-B-1 review round 2 blocker #1** —
    # the earlier "exclude rows with expires_at populated" variant
    # weakened branch (a) when ``expires_at`` was populated but not
    # yet expired, which is the wrong trade-off. **Round 3 NIT** —
    # branch (c) is documented as "writer skips; DB allows" to avoid
    # contradicting the earlier "DB-level enforcement" summary.
    op.create_index(
        "ix_monitor_flags_observer_dedup",
        "monitor_flags",
        ["user_id", "dedup_key"],
        unique=True,
        sqlite_where=sa.text(
            "dedup_key IS NOT NULL AND acknowledged_at IS NULL"
        ),
        postgresql_where=sa.text(
            "dedup_key IS NOT NULL AND acknowledged_at IS NULL"
        ),
    )


def _preflight_downgrade() -> None:
    """Refuse to downgrade if observer-era rows would violate the legacy CHECK.

    Symmetric mirror of the upgrade preflight: if observer code has
    already written ``state_observer_*`` rows to ``monitor_flags``, the
    batch-rebuild on downgrade would silently DROP those rows when the
    legacy three-value CHECK rejects them. Same data-loss failure mode
    as upgrade BLOCKER #6 — guard it the same way.

    Operator remediation: either DELETE the offending rows, UPDATE them
    to a legacy kind (almost never the right call — the rows have
    observer-shaped payloads), or skip the downgrade entirely.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("monitor_flags"):
        return

    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT kind FROM monitor_flags "
            "WHERE kind NOT IN ("
            + _kinds_sql(_LEGACY_FLAG_KINDS)
            + ")"
        )
    ).fetchall()
    blocking = sorted(r[0] for r in rows)
    if blocking:
        raise RuntimeError(
            "Migration 0049 downgrade preflight failed: monitor_flags "
            f"contains kind values that the legacy CHECK rejects: "
            f"{blocking}. Remediate (DELETE or UPDATE to one of "
            f"{list(_LEGACY_FLAG_KINDS)}) before retrying the downgrade. "
            "See migration 0049 downgrade docstring."
        )


def downgrade() -> None:
    # Preflight FIRST — any DDL before this would leave the DB
    # half-downgraded if the preflight then raises (the alembic
    # transaction wrapper does NOT roll back DDL on SQLite, which
    # operates in non-transactional DDL mode per the env logs).
    _preflight_downgrade()

    # Drop the partial unique index so the column drop is safe.
    op.drop_index(
        "ix_monitor_flags_observer_dedup", table_name="monitor_flags"
    )

    # No data-prep needed for ``dedup_key`` itself — it's observer-
    # internal bookkeeping and the writer reconstructs it on re-upgrade
    # from (kind, payload.primary_field) by re-running against the
    # snapshot. Order: drop the new CHECK → install the legacy CHECK →
    # drop the dedup_key column. Doing the column drop AFTER the CHECK
    # swap keeps both constraint operations inside one batch context,
    # avoiding a redundant table rebuild.
    with op.batch_alter_table("monitor_flags") as batch:
        batch.drop_constraint("ck_monitor_flags_kind", type_="check")
        batch.create_check_constraint(
            "ck_monitor_flags_kind",
            f"kind IN ({_kinds_sql(_LEGACY_FLAG_KINDS)})",
        )
        batch.drop_column("dedup_key")

    op.drop_index(
        "ix_state_snapshots_user_date", table_name="state_snapshots"
    )
    op.drop_table("state_snapshots")
