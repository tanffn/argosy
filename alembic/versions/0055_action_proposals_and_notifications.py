"""action_proposals + notification_{subscriptions,preferences,dispatch_ledger}.

Revision ID: 0055_action_proposals_and_notifications
Revises: 0054_life_events_cashflow_shape
Create Date: 2026-05-30

Spec E (last-mile delivery layer) commit #1 — four coupled tables for
the action-proposal + notification-fanout layer.

See ``docs/superpowers/specs/2026-05-29-last-mile-delivery-design.md``:

* §1 — ``action_proposals`` table (the action ledger).
* §2.2.1 — capability-boundary enforcement; the ``execution_state``
  column is the structural defense (codex BLOCKER #1 integration in
  spec text).  In this commit's simplified shape the enum tracks
  SUGGESTION -> USER ACTION transitions:
  ``proposed`` / ``accepted_pending_user_action`` / ``dismissed``.
  There is NO auto-execute path in this codebase; the column gates UI
  state transitions only — money still moves only via the existing
  ``proposals -> action_engine -> orders`` pipeline.
* §1.5 — partial UNIQUE on ``(user_id, dedup_key) WHERE status='open'``
  (write-orchestrated tombstone pattern matching Spec B's pattern from
  migration 0049).  When a proposal moves to ``accepted|rejected|
  expired`` the dedup_key is released and the same key can re-fire.
* §3 — notification_subscriptions + notification_preferences +
  notification_dispatch_ledger.
* §9 — commit table row #1.
* Appendix A — full DDLs.

**Migration numbering note** — the spec text calls this "migration
0050".  Sprints A/B/C/D consumed 0048-0054 between the spec being
written and this commit, so this is migration **0055**.  Semantic
content matches Spec E §1 + §3 + Appendix A unchanged.

**Schema shape vs the full spec.**  The spec's Appendix A enumerates
nine tables across the whole 9-commit sprint; this commit lands the
four that the rest of the sprint depends on (action_proposals +
notification_subscriptions + notification_preferences +
notification_dispatch_ledger).  The remaining five tables
(``action_proposal_history``, ``action_proposer_cooldowns``,
``pending_digest_entries``, ``replan_dispatch_log``,
``inferred_life_event_findings``) land in their respective sprint
commits (per-commit migrations matching the per-commit code drop).

Four tables in this migration:

1. **action_proposals** — one row per system-proposed action.
   Discriminator: ``kind`` (allocate / repatriate_currency /
   rebalance / replan_full / add_life_event_phase /
   update_plan_assumption / set_watchlist / note_only).  FKs back to
   ``monitor_flags`` (Spec B, ON DELETE SET NULL — losing the source
   flag does NOT cascade away an already-surfaced proposal) and to
   ``state_snapshots`` (Spec B, same).  ``source_inferred_event_id``
   is plain INTEGER (NO FK constraint) because the
   ``inferred_life_event_findings`` table lands in commit #5; we
   reserve the column shape now so the writer can populate it without
   another migration.  Partial UNIQUE on (user_id, dedup_key) WHERE
   status='open' AND dedup_key IS NOT NULL — same pattern as
   ``ix_monitor_flags_observer_dedup`` in migration 0049.

2. **notification_subscriptions** — one row per (user, channel,
   endpoint) push subscription.  Channels: web_push / email / in_app.
   ``p256dh`` + ``auth`` are nullable because they're web-push-only
   crypto material (irrelevant for email / in-app channels); the
   application layer enforces "non-NULL when channel=web_push", NOT
   the DB.  Per codex spec §3.4 we don't want over-constrained DDL
   that breaks legitimate edge cases.  Status enum tracks the 410-Gone
   lifecycle (``gone`` set when the push endpoint returns 410).

3. **notification_preferences** — per-(user, channel, severity, kind)
   enable matrix.  This is the granular-matrix shape from the
   simplified Spec E commit #1 prompt — one row per cell in the
   channel x severity x kind cube.  ``kind`` is permissive TEXT (no
   CHECK enum) so adding new MonitorFlag.kind + action_proposal.kind
   families doesn't require a CHECK-relaxation migration; the writer
   contract validates kind shape.

4. **notification_dispatch_ledger** — one row per dispatch attempt.
   ``notification_id`` is the deterministic cross-channel dedup key
   (e.g. ``f"{kind}|{ref_id}|{channel}|{severity}|{utc_day}"``).
   UNIQUE(user_id, notification_id, channel) enforces idempotent
   re-dispatch at the DB layer — a retry attempt for the same
   notification on the same channel is rejected by the DB regardless
   of whether the application-level dedup check missed.  The
   uniqueness scope INCLUDES ``user_id`` per codex BLOCKER (Spec E #1
   review): the writer convention for ``notification_id`` doesn't
   include the user (it's just ``kind|ref_id|channel|severity|day``),
   so without the user scope two tenants emitting identical
   deterministic notification ids on the same channel would collide
   and one would lose its audit row.  Argosy is single-user today
   but explicitly multi-tenant-ready per SDD §12.5; scoping at the
   DB layer pins this invariant before any tenant fanout happens.

SQLite version requirement: ``json_valid`` (used in the
``suggested_payload`` CHECK) needs SQLite >= 3.38 — already an Argosy
baseline (see ``argosy/config.py``, plus migrations 0049 / 0050).
Partial-index WHERE clauses are SQLite-supported and exercised in
migrations 0040 / 0043 / 0047 / 0048 / 0049 / 0050.

Downgrade
=========
Drops all four tables + their indexes in reverse dependency order
(ledger -> preferences -> subscriptions -> proposals).  No data
preservation: the tables are fresh in this migration; there is no
legacy shape to reconstitute on downgrade.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_action_proposals_and_notifications"
down_revision: str | None = "0054_life_events_cashflow_shape"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Spec §1.3 — eight action-proposal kinds.  Extending the enum is a
# CHECK-relaxation migration (same shape as 0049's monitor_flags.kind).
_VALID_PROPOSAL_KINDS: tuple[str, ...] = (
    "allocate",
    "repatriate_currency",
    "rebalance",
    "replan_full",
    "add_life_event_phase",
    "update_plan_assumption",
    "set_watchlist",
    "note_only",
)

# Three severity bands shared across action_proposals + notification
# tables.  Matches MonitorFlag.severity from migration 0043 / 0049.
_VALID_SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")

# Action-proposal lifecycle.  ``open`` is the active state; the
# partial-UNIQUE dedup index only fires while status='open'.
_VALID_PROPOSAL_STATUSES: tuple[str, ...] = (
    "open",
    "accepted",
    "deferred",
    "rejected",
    "superseded",
)

# Capability-boundary enum (codex BLOCKER #1 integration per spec
# §2.2.1).  The simplified Spec E #1 prompt's three-value enum tracks
# SUGGESTION -> USER ACTION transitions only — no auto-execute path
# exists in this codebase.
_VALID_EXECUTION_STATES: tuple[str, ...] = (
    "proposed",
    "accepted_pending_user_action",
    "dismissed",
)

# Notification channels — web push + email + in-app WebSocket.  SMS /
# WhatsApp / Telegram are explicitly out of v1 scope per spec §Non-goals.
_VALID_CHANNELS: tuple[str, ...] = ("web_push", "email", "in_app")

# Subscription lifecycle.  ``gone`` is set when the push endpoint
# returns HTTP 410 (browser uninstalled SW / user revoked permission).
_VALID_SUBSCRIPTION_STATUSES: tuple[str, ...] = ("active", "gone")

# Dispatch outcomes.  ``sent`` = delivered to the channel; ``failed`` =
# channel returned non-2xx (logged in error_message); ``skipped`` =
# preference matrix or dedup gate rejected before attempt.
_VALID_DISPATCH_STATUSES: tuple[str, ...] = ("sent", "failed", "skipped")


def _quoted_csv(values: Sequence[str]) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. action_proposals — the action ledger.
    # ------------------------------------------------------------------
    op.create_table(
        "action_proposals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # FK to Spec B's monitor_flags — losing the source flag (e.g. a
        # housekeeping sweep that drops acknowledged flags) must NOT
        # cascade away an already-surfaced proposal.  ON DELETE SET NULL.
        sa.Column(
            "source_flag_id",
            sa.Integer,
            sa.ForeignKey("monitor_flags.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # FK to Spec B's state_snapshots — same reasoning.
        sa.Column(
            "source_observation_id",
            sa.Integer,
            sa.ForeignKey("state_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Forward-looking column for the commit #5
        # inferred_life_event_findings FK.  NO FK constraint today —
        # the referenced table doesn't exist yet.  Writer code in
        # commit #5 populates this; readers tolerate NULL.
        sa.Column(
            "source_inferred_event_id",
            sa.Integer,
            nullable=True,
        ),
        # LLM-generated 1-2 sentence summary; persisted so notification
        # rendering doesn't re-call the LLM (spec §1.2).  Per the prompt,
        # nominal length cap ~200 chars but enforced at the writer layer
        # (Pydantic, not DB CHECK — varchar limits in SQLite are advisory).
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("rationale_md", sa.Text, nullable=False),
        # Structured payload (per-kind Pydantic schema in §1.4).  Loud-
        # error contract: json_valid CHECK at the DB layer is the floor
        # under the writer-side Pydantic validation.
        sa.Column("suggested_payload", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        # Surfacing + expiry timestamps.  expires_at is NOT NULL per the
        # spec note (surfaced_at + 7 days typical for critical, +30 days
        # for non-critical — the writer computes the cushion).
        sa.Column(
            "surfaced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Lifecycle.  Default 'open' — the active state that the
        # partial-UNIQUE dedup index keys off.
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "decided_by_user_note",
            sa.Text,
            nullable=True,
        ),
        # Discriminator for proposal UX + payload shape (spec §1.3).
        sa.Column("kind", sa.Text, nullable=False),
        # Tombstone-pattern dedup key (spec §1.5).  Nullable: a
        # proposal without a deterministic dedup contract just falls
        # outside the uniqueness scope.  Partial UNIQUE index below
        # only fires when status='open' AND dedup_key IS NOT NULL.
        sa.Column("dedup_key", sa.Text, nullable=True),
        # Capability-boundary enforcement (codex BLOCKER #1 / spec
        # §2.2.1).  Three-value enum tracking SUGGESTION -> USER ACTION
        # transitions only.  Default 'proposed': every row written by
        # the proposer starts here; the Accept handler advances to
        # 'accepted_pending_user_action' (still NOT an executable
        # state — money moves via a separate downstream pipeline);
        # 'dismissed' is the terminal state for rejected / expired
        # proposals.
        sa.Column(
            "execution_state",
            sa.Text,
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        # ----- CHECK constraints (declared inline; SQLite materialises
        # them into the CREATE TABLE statement so a future
        # batch-rebuild round-trips them faithfully). -----
        sa.CheckConstraint(
            "kind IN (" + _quoted_csv(_VALID_PROPOSAL_KINDS) + ")",
            name="ck_action_proposals_kind",
        ),
        sa.CheckConstraint(
            "severity IN (" + _quoted_csv(_VALID_SEVERITIES) + ")",
            name="ck_action_proposals_severity",
        ),
        sa.CheckConstraint(
            "status IN (" + _quoted_csv(_VALID_PROPOSAL_STATUSES) + ")",
            name="ck_action_proposals_status",
        ),
        sa.CheckConstraint(
            "execution_state IN ("
            + _quoted_csv(_VALID_EXECUTION_STATES)
            + ")",
            name="ck_action_proposals_execution_state",
        ),
        sa.CheckConstraint(
            "json_valid(suggested_payload)",
            name="ck_action_proposals_suggested_payload_json_valid",
        ),
    )

    # Hot-path index: per-user open queue, severity-then-recent ordering.
    op.create_index(
        "ix_action_proposals_user_status_surfaced",
        "action_proposals",
        ["user_id", "status", sa.text("surfaced_at DESC")],
    )

    # Per-(user, kind, status) lookup — feeds the /proposals UI kind
    # filter and the dedup precheck.
    op.create_index(
        "ix_action_proposals_user_kind_status",
        "action_proposals",
        ["user_id", "kind", "status"],
    )

    # Partial UNIQUE for the write-orchestrated tombstone pattern.
    # Mirror of ix_monitor_flags_observer_dedup from migration 0049 —
    # same uniqueness-only-while-active semantics.  When a proposal
    # transitions out of status='open' the dedup_key is "released" and
    # a fresh proposal with the same key can fire.
    #
    # Two-clause predicate:
    #   * status = 'open'         — only active rows enforce uniqueness
    #   * dedup_key IS NOT NULL   — proposals without a natural dedup
    #                                key fall outside the scope (legacy /
    #                                manual writes coexist).
    #
    # Per spec §1.5 the predicate is STRICT — no time-based clause
    # (SQLite forbids non-deterministic functions in partial-index
    # predicates).  Expiry is handled by the housekeeping loop (spec
    # §1.6) which transitions expires_at-passed rows to status='expired'.
    #
    # NOTE: spec §1.5 includes 'expired' as a separate status; this
    # commit's simplified status enum (5 values) does NOT distinguish
    # 'expired' from 'rejected' — the housekeeping loop's writer is
    # responsible for the transition.  When commit #2 lands the
    # housekeeping loop, a CHECK-relaxation migration extends the enum
    # to add 'expired' + 'customized_accepted' as documented in spec
    # §1.2.  This commit only ships the 5 statuses the action-proposer
    # writer needs.
    op.create_index(
        "ix_action_proposals_dedup_open",
        "action_proposals",
        ["user_id", "dedup_key"],
        unique=True,
        sqlite_where=sa.text(
            "status = 'open' AND dedup_key IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "status = 'open' AND dedup_key IS NOT NULL"
        ),
    )

    # Housekeeping-loop hot-path: "give me all open proposals due to
    # expire."  Partial WHERE keeps the index tiny (most rows are not
    # open).
    op.create_index(
        "ix_action_proposals_expires_open",
        "action_proposals",
        ["expires_at"],
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )

    # ------------------------------------------------------------------
    # 2. notification_subscriptions — push subscription ledger.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_subscriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text, nullable=False),
        # Endpoint shape varies by channel: web push URL (https:// to a
        # browser-vendor push service) / email address / in_app
        # WebSocket channel id.  Application layer validates shape;
        # DB enforces only the (user_id, channel, endpoint) uniqueness
        # below + the channel enum CHECK.
        sa.Column("endpoint", sa.Text, nullable=False),
        # Web-push crypto material — nullable because email / in_app
        # subscriptions have no equivalent (codex spec §3.4: validate
        # via app, NOT DB constraint).
        sa.Column("p256dh", sa.Text, nullable=True),
        sa.Column("auth", sa.Text, nullable=True),
        sa.Column(
            "subscribed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.CheckConstraint(
            "channel IN (" + _quoted_csv(_VALID_CHANNELS) + ")",
            name="ck_notification_subscriptions_channel",
        ),
        sa.CheckConstraint(
            "status IN ("
            + _quoted_csv(_VALID_SUBSCRIPTION_STATUSES)
            + ")",
            name="ck_notification_subscriptions_status",
        ),
        sa.UniqueConstraint(
            "user_id",
            "channel",
            "endpoint",
            name="uq_notification_subscriptions_user_channel_endpoint",
        ),
    )

    # Active-subs lookup (the dispatcher fans out to active rows only).
    op.create_index(
        "ix_notification_subscriptions_user_active",
        "notification_subscriptions",
        ["user_id"],
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )

    # ------------------------------------------------------------------
    # 3. notification_preferences — per-cell enable matrix.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        # ``kind`` is permissive TEXT — no CHECK enum.  The dispatcher
        # matches kinds against MonitorFlag.kind families (state_observer_*
        # / drift / etc.) plus the eight action_proposal kinds (see
        # _VALID_PROPOSAL_KINDS above).  CHECK-enforcing it here would
        # require a CHECK-relaxation migration every time a new flag
        # family lands; the writer contract validates kind shape.
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column(
            "enabled",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "channel IN (" + _quoted_csv(_VALID_CHANNELS) + ")",
            name="ck_notification_preferences_channel",
        ),
        sa.CheckConstraint(
            "severity IN (" + _quoted_csv(_VALID_SEVERITIES) + ")",
            name="ck_notification_preferences_severity",
        ),
        sa.CheckConstraint(
            "enabled IN (0, 1)",
            name="ck_notification_preferences_enabled_bool",
        ),
        sa.UniqueConstraint(
            "user_id",
            "channel",
            "severity",
            "kind",
            name="uq_notification_preferences_user_cell",
        ),
    )

    # ------------------------------------------------------------------
    # 4. notification_dispatch_ledger — dispatch attempts + idempotency.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_dispatch_ledger",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Deterministic cross-channel dedup key.  Writer convention:
        # f"{kind}|{ref_id}|{channel}|{severity}|{utc_day}".  UNIQUE
        # (notification_id, channel) below enforces re-dispatch
        # idempotency at the DB layer.
        sa.Column("notification_id", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        # FK back to the subscription that received the dispatch.  ON
        # DELETE SET NULL — losing the subscription (user opted out)
        # must not cascade away the dispatch audit row.
        sa.Column(
            "subscription_id",
            sa.Integer,
            sa.ForeignKey(
                "notification_subscriptions.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.CheckConstraint(
            "channel IN (" + _quoted_csv(_VALID_CHANNELS) + ")",
            name="ck_notification_dispatch_ledger_channel",
        ),
        sa.CheckConstraint(
            "status IN ("
            + _quoted_csv(_VALID_DISPATCH_STATUSES)
            + ")",
            name="ck_notification_dispatch_ledger_status",
        ),
        # Codex BLOCKER (Spec E #1 review): scope uniqueness to
        # user_id.  The writer's deterministic notification_id is
        # ``f"{kind}|{ref_id}|{channel}|{severity}|{utc_day}"`` —
        # NOT user-namespaced — so two tenants emitting identical
        # ids on the same channel would otherwise collide at the DB
        # layer and one tenant's audit row would be lost.  Argosy is
        # single-user today (Ariel + Noga) but explicitly multi-
        # tenant-ready per SDD §12.5; pinning the invariant at DDL
        # time matches the project-wide pattern.
        sa.UniqueConstraint(
            "user_id",
            "notification_id",
            "channel",
            name="uq_notification_dispatch_ledger_user_notification_channel",
        ),
    )

    # Audit / debug index — "show me this user's recent dispatch
    # attempts, newest first."  Feeds the future /admin/notifications
    # debug page (out of v1 scope per spec §3.6).
    op.create_index(
        "ix_notification_dispatch_user_dispatched",
        "notification_dispatch_ledger",
        ["user_id", sa.text("dispatched_at DESC")],
    )


def downgrade() -> None:
    # Reverse dependency order: ledger first (FK to subscriptions),
    # then preferences (no inbound FKs), then subscriptions (FK target),
    # then proposals (no inbound FKs among the four).

    op.drop_index(
        "ix_notification_dispatch_user_dispatched",
        table_name="notification_dispatch_ledger",
    )
    op.drop_table("notification_dispatch_ledger")

    op.drop_table("notification_preferences")

    op.drop_index(
        "ix_notification_subscriptions_user_active",
        table_name="notification_subscriptions",
    )
    op.drop_table("notification_subscriptions")

    op.drop_index(
        "ix_action_proposals_expires_open",
        table_name="action_proposals",
    )
    op.drop_index(
        "ix_action_proposals_dedup_open",
        table_name="action_proposals",
    )
    op.drop_index(
        "ix_action_proposals_user_kind_status",
        table_name="action_proposals",
    )
    op.drop_index(
        "ix_action_proposals_user_status_surfaced",
        table_name="action_proposals",
    )
    op.drop_table("action_proposals")
