"""``StateObserverLoop`` — daily 17:00 IDT state-observer cron (Spec B commit #7).

Wires the four sibling services that Spec B commits #1-#6 already landed
into one :class:`CadenceLoop`:

  1. :func:`argosy.services.state_snapshot.collect_state_snapshot`
     — assemble the six-section state dict for ``user_id``.
  2. :func:`argosy.services.state_snapshot.persist_state_snapshot`
     — INSERT the row + return the ``StateSnapshot`` ORM object.
  3. :func:`argosy.services.state_diff.compute_full_diff`
     — diff current vs (plan baseline, prior snapshot).
  4. :class:`argosy.agents.state_observer.StateObserverAgent`
     — Opus 4.7 emergent flagger.
  5. :func:`argosy.services.state_observer_flag_writer.write_observer_flags`
     — persist the validated candidates with the dedup_key contract.

Run-level cool-off (spec §4.4)
==============================

A per-user cool-off of :data:`MIN_RUN_INTERVAL_MINUTES` (default 360 = 6h
per spec §4.4) blocks redundant re-runs from the daily cron + on-demand
triggers (snapshot upload, plan re-synthesis). The cool-off is driven by
the most recent ``state_snapshots`` row's ``created_at`` — no separate
lock table is needed because the snapshot table itself records both the
attempt + its timestamp.

The backfill script (Spec B commit #5) bypasses cool-off via
``force=True``; production triggers never set ``force``.

Same-code-path contract: the daily cron fires :meth:`tick` exactly the
same way as the manual ``Run now`` path that goes through ``JobRegistry``.
On-demand triggers go through :func:`run_state_observer_now` which is a
thin wrapper that calls :meth:`tick` with ``trigger_reason`` plumbed
into the snapshot's ``source_versions``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

_log = get_logger("argosy.loops.state_observer")


# Default cron + tz — kept in sync with ``cadences.state_observer`` in
# ``argosy/agent_settings.py``. Aligned with ``news_daily`` at 17:00 IDT
# so the observer reads a fully-settled state (Tel Aviv market closed,
# news pipeline classified the day's signals).
_DEFAULT_CRON = "0 17 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

# Spec §4.4 — per-user cool-off between successful observer runs. 6h is
# the spec default; tunable for tests via the constructor.
MIN_RUN_INTERVAL_MINUTES: int = 360


TriggerReason = Literal[
    "daily_cron", "snapshot_upload", "plan_resynthesis", "backfill", "manual",
]


def state_observer_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row the registry surfaces.

    ``source_kind='monitor'`` — the observer surfaces flags on the Red-Flag
    Strip, same family as the deterministic monitor detectors.
    """
    return JobMetadata(
        name="state_observer_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 17:00 IDT",
        source_kind="monitor",
        description=(
            "Daily state observer — assembles the six-section snapshot, "
            "diffs vs plan baseline + prior snapshot, runs the Opus state-"
            "observer agent, persists emergent flag candidates. 6h cool-off "
            "between successful runs."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level sync session factory cache — mirrors news_daily.py's
# pattern. The state-observer pipeline uses sync DB calls (snapshot
# collectors + flag writer all expect a sync ``Session``).
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Return the cached sync ``sessionmaker`` bound to the configured DB.

    Lazy + cached so import-time has no side effects and we don't churn an
    engine + connection pool every 17:00 IDT. Rebuilds when ``db_file``
    changes (test reloads of settings).
    """
    global _DEFAULT_SESSION_FACTORY

    import sqlalchemy as sa

    from argosy.config import get_settings

    settings = get_settings()
    db_file = str(settings.db_file)

    if _DEFAULT_SESSION_FACTORY is not None:
        cached_key, cached_factory = _DEFAULT_SESSION_FACTORY
        if cached_key == db_file:
            return cached_factory

    sync_url = f"sqlite:///{db_file}"
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _DEFAULT_SESSION_FACTORY = (db_file, factory)
    return factory


def _reset_default_session_factory_cache() -> None:
    """Test hook — clear the cached sessionmaker so a subsequent call
    rebuilds from the current settings."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


def _diffs_to_payload(full_diff: Any) -> dict[str, list[dict[str, Any]]]:
    """Convert a ``FullDiff`` dataclass (or already-dict payload) into the
    agent's input shape.

    The agent's ``_post_validate_output`` accepts the field-path either
    under the ``path`` key (from ``FieldDiff`` via ``dataclasses.asdict``)
    or as a bare string. We always emit the dict form so the audit-trail
    fields (``baseline_value`` / ``current_value`` / ``magnitude``) flow
    through to the prompt.
    """
    if full_diff is None:
        return {"vs_plan": [], "vs_prior": []}
    if isinstance(full_diff, dict):
        # Already in dict form — coerce inner items to dicts too.
        out: dict[str, list[dict[str, Any]]] = {"vs_plan": [], "vs_prior": []}
        for side in ("vs_plan", "vs_prior"):
            for entry in full_diff.get(side) or []:
                if is_dataclass(entry):
                    out[side].append(asdict(entry))
                elif isinstance(entry, dict):
                    out[side].append(dict(entry))
                else:
                    out[side].append({"path": str(entry)})
        return out
    # FullDiff dataclass
    return {
        "vs_plan": [asdict(d) for d in (getattr(full_diff, "vs_plan", None) or [])],
        "vs_prior": [asdict(d) for d in (getattr(full_diff, "vs_prior", None) or [])],
    }


class StateObserverLoop(CadenceLoop):
    """Daily state-observer cadence loop.

    Constructor injection points so tests can drive the loop without
    touching the DB / SDK / live LLM:

    * ``schedule``         — overrides the cron/tz.
    * ``user_id``          — single-tenant for now (defaults ``"ariel"``).
    * ``session_factory``  — sync ``sessionmaker``; default builds from
                              ``get_settings().db_file``.
    * ``collect_fn``       — overrides ``collect_state_snapshot``.
    * ``persist_fn``       — overrides ``persist_state_snapshot``.
    * ``diff_fn``          — overrides ``compute_full_diff``.
    * ``agent_factory``    — overrides ``StateObserverAgent`` construction.
    * ``write_fn``         — overrides ``write_observer_flags``.
    * ``now_fn``           — overrides :func:`_utcnow` (tests pin time).
    * ``min_run_interval_minutes`` — overrides the spec §4.4 cool-off.
    """

    name = "state_observer_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        collect_fn: Callable[..., dict[str, Any]] | None = None,
        persist_fn: Callable[..., Any] | None = None,
        diff_fn: Callable[..., Any] | None = None,
        agent_factory: Callable[[], Any] | None = None,
        write_fn: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        min_run_interval_minutes: int = MIN_RUN_INTERVAL_MINUTES,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        if min_run_interval_minutes < 0:
            raise ValueError(
                "min_run_interval_minutes must be >= 0; "
                f"got {min_run_interval_minutes!r}"
            )
        self.user_id = user_id
        self._session_factory = session_factory
        self._collect_fn = collect_fn
        self._persist_fn = persist_fn
        self._diff_fn = diff_fn
        self._agent_factory = agent_factory
        self._write_fn = write_fn
        self._now_fn = now_fn or _utcnow
        self._min_run_interval_minutes = min_run_interval_minutes
        #: Surfaced by the ``RegisteredScheduler`` adapter even on the
        #: exception path so partial progress (snapshot persisted but
        #: agent crashed) is observable.
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        trigger_reason: TriggerReason = "daily_cron",
        force: bool = False,
    ) -> dict | None:
        """Run the full snapshot → diff → observe → write pipeline once.

        Returns:
          A summary dict with keys ``snapshot_id``, ``candidates_emitted``,
          ``flags_written``, ``flags_deduplicated``, ``flags_tombstoned``,
          ``trigger_reason``, ``skipped_reason`` (only on cool-off skips).
          Returns the same dict on success and on cool-off skip — the
          caller distinguishes via ``skipped_reason``.

        Cool-off (spec §4.4):
          If a successful run happened within ``min_run_interval_minutes``
          of ``now`` for this user, returns
          ``{"skipped_reason": "cool_off", ...}`` without doing any work.
          Pass ``force=True`` to bypass — used by the backfill script /
          manual ``Run now``-with-override flows.
        """
        self.last_output_summary = None

        run_at = (now or self._now_fn)()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)

        _log.info(
            "state_observer.tick.start",
            user_id=self.user_id,
            run_at=run_at.isoformat(),
            trigger_reason=trigger_reason,
            force=force,
        )

        # The sync session work runs in a thread so the async scheduler
        # loop isn't blocked by DB I/O and so the LLM call (which uses
        # asyncio.run internally inside BaseAgent) doesn't collide with
        # the running event loop.
        summary = await asyncio.to_thread(
            self._run_pipeline_sync,
            run_at=run_at,
            trigger_reason=trigger_reason,
            force=force,
        )
        self.last_output_summary = summary
        _log.info(
            "state_observer.tick.done",
            user_id=self.user_id,
            **{k: v for k, v in summary.items() if k != "errors"},
        )
        return summary

    # ------------------------------------------------------------------
    # Sync body — one Session crosses snapshot persist + flag write.
    # ------------------------------------------------------------------

    def _run_pipeline_sync(
        self,
        *,
        run_at: datetime,
        trigger_reason: TriggerReason,
        force: bool,
    ) -> dict[str, Any]:
        factory = self._session_factory or _build_default_session_factory()
        session = factory()
        try:
            # ----- Cool-off check (spec §4.4) -----------------------
            if not force:
                cool_off_skip = self._cool_off_check(
                    session, run_at=run_at,
                )
                if cool_off_skip is not None:
                    return cool_off_skip

            # ----- 1. Collect snapshot ------------------------------
            collect_fn = self._collect_fn or self._default_collect_fn()
            collected = collect_fn(
                session,
                self.user_id,
                as_of=None,
                trigger_reason=trigger_reason,
            )
            # ``collect_state_snapshot`` returns
            # ``{"state": {...}, "source_versions": {...}}``.
            state_dict = collected.get("state", {}) if isinstance(collected, dict) else {}
            source_versions = (
                collected.get("source_versions", {})
                if isinstance(collected, dict) else {}
            )

            # ----- 2. Persist snapshot ------------------------------
            snapshot_date = self._extract_snapshot_date(state_dict, run_at)
            persist_fn = self._persist_fn or self._default_persist_fn()
            snapshot_row = persist_fn(
                session,
                self.user_id,
                snapshot_date,
                state_dict,
                source_versions,
            )
            snapshot_id = int(getattr(snapshot_row, "id", 0) or 0)

            # ----- 3. Plan baseline + prior snapshot ----------------
            plan_baseline = self._extract_plan_baseline(state_dict)
            prior_state = self._load_prior_snapshot_state(
                session, snapshot_id=snapshot_id,
            )

            # ----- 4. Diff ------------------------------------------
            diff_fn = self._diff_fn or self._default_diff_fn()
            full_diff = diff_fn(
                state_dict,
                plan_baseline,
                prior_state,
            )

            # ----- 5. Agent -----------------------------------------
            agent = (
                self._agent_factory or self._default_agent_factory()
            )()
            agent_input_diff = _diffs_to_payload(full_diff)
            report = self._run_agent_sync(
                agent=agent,
                state_dict=state_dict,
                full_diff=agent_input_diff,
                plan_baseline=plan_baseline,
                prior_snapshot=prior_state,
                trigger_reason=trigger_reason,
                snapshot_date=snapshot_date,
                source_versions=source_versions,
            )
            candidates = list(
                getattr(getattr(report, "output", None), "flag_candidates", [])
                or []
            )

            # ----- 6. Write flags -----------------------------------
            write_fn = self._write_fn or self._default_write_fn()
            write_summary = write_fn(
                session,
                self.user_id,
                candidates,
                snapshot_id=snapshot_id,
                now=run_at,
            )

            return {
                "snapshot_id": snapshot_id,
                "candidates_emitted": len(candidates),
                "flags_written": int(
                    getattr(write_summary, "written_count", 0)
                ),
                "flags_deduplicated": int(
                    getattr(write_summary, "deduplicated_count", 0)
                ),
                "flags_tombstoned": int(
                    getattr(write_summary, "tombstoned_count", 0)
                ),
                "trigger_reason": trigger_reason,
                "skipped_reason": None,
            }
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cool_off_check(
        self, session: Session, *, run_at: datetime,
    ) -> dict[str, Any] | None:
        """Return a skip-summary dict iff a recent successful run blocks
        this one; otherwise ``None``.

        The most recent ``state_snapshots`` row IS the proxy for "did we
        recently observe?" — the loop persists exactly one row per
        successful run.
        """
        if self._min_run_interval_minutes <= 0:
            return None

        from argosy.services.state_snapshot import get_latest_state_snapshot

        latest = get_latest_state_snapshot(session, self.user_id)
        if latest is None:
            return None
        last_at = getattr(latest, "created_at", None)
        if last_at is None:
            return None
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        delta = run_at - last_at
        if delta < timedelta(minutes=self._min_run_interval_minutes):
            _log.info(
                "state_observer.tick.skipped_cool_off",
                user_id=self.user_id,
                last_run_at=last_at.isoformat(),
                cool_off_minutes=self._min_run_interval_minutes,
                elapsed_seconds=int(delta.total_seconds()),
            )
            return {
                "snapshot_id": int(getattr(latest, "id", 0) or 0),
                "candidates_emitted": 0,
                "flags_written": 0,
                "flags_deduplicated": 0,
                "flags_tombstoned": 0,
                "trigger_reason": None,
                "skipped_reason": "cool_off",
            }
        return None

    @staticmethod
    def _extract_snapshot_date(
        state_dict: dict[str, Any], run_at: datetime,
    ) -> date:
        """Pull the date from ``state.metadata.snapshot_date`` (ISO),
        falling back to ``run_at.date()``."""
        meta = state_dict.get("metadata") if isinstance(state_dict, dict) else None
        if isinstance(meta, dict):
            raw = meta.get("snapshot_date")
            if isinstance(raw, str):
                try:
                    return date.fromisoformat(raw)
                except ValueError:
                    pass
        return run_at.date()

    @staticmethod
    def _extract_plan_baseline(
        state_dict: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Build the plan-baseline dict the diff service expects.

        ``compute_full_diff`` accepts a six-section-shaped dict (typically
        ``{"plan_inputs": ..., "portfolio": {"allocations": [...]}}``) so
        the cross-section comparator map can pair current-state fields
        against their plan baselines. We hand back the current state's
        own ``plan_inputs`` block (anchoring at the plan the snapshot
        sourced from) plus the portfolio allocations subtree so target_pct
        / target_k_usd pairs surface as deviations.
        """
        if not isinstance(state_dict, dict):
            return None
        plan_inputs = state_dict.get("plan_inputs")
        if not isinstance(plan_inputs, dict) or not plan_inputs:
            return None
        baseline: dict[str, Any] = {"plan_inputs": plan_inputs}
        portfolio = state_dict.get("portfolio")
        if isinstance(portfolio, dict) and portfolio:
            allocations = portfolio.get("allocations")
            if allocations:
                baseline["portfolio"] = {"allocations": allocations}
        return baseline

    def _load_prior_snapshot_state(
        self, session: Session, *, snapshot_id: int,
    ) -> dict[str, Any] | None:
        """Return the immediately-prior snapshot's state dict, or ``None``.

        We just persisted ``snapshot_id`` for ``self.user_id`` — the
        "prior" snapshot is the next-newest row that ISN'T this one.
        """
        from argosy.state.models import StateSnapshot
        import json

        import sqlalchemy as sa

        stmt = (
            sa.select(StateSnapshot)
            .where(StateSnapshot.user_id == self.user_id)
            .where(StateSnapshot.id != snapshot_id)
            .order_by(sa.desc(StateSnapshot.snapshot_date), sa.desc(StateSnapshot.id))
            .limit(1)
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        try:
            return json.loads(row.state_json)
        except (TypeError, ValueError):
            return None

    def _run_agent_sync(
        self,
        *,
        agent: Any,
        state_dict: dict[str, Any],
        full_diff: dict[str, list[dict[str, Any]]],
        plan_baseline: dict[str, Any] | None,
        prior_snapshot: dict[str, Any] | None,
        trigger_reason: str,
        snapshot_date: date,
        source_versions: dict[str, Any],
    ) -> Any:
        """Drive ``agent.run`` from a sync context.

        ``StateObserverAgent.run`` is a coroutine; we wrap it in
        :func:`asyncio.run` because the surrounding sync body is itself
        running inside :func:`asyncio.to_thread` (no event loop in this
        thread).
        """
        gaps = []
        if isinstance(source_versions, dict):
            raw = source_versions.get("historical_replay_gaps")
            if isinstance(raw, list):
                gaps = [str(g) for g in raw]

        coro = agent.run(
            plan_summary=_render_plan_summary(plan_baseline),
            current_state=state_dict,
            full_diff=full_diff,
            plan_baseline=plan_baseline,
            prior_snapshot=prior_snapshot,
            user_notes="",
            user_id=self.user_id,
            snapshot_date=snapshot_date.isoformat(),
            trigger_reason=trigger_reason,
            historical_replay_gaps=gaps,
        )
        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Default function-pointers — kept as methods so subclasses / tests
    # can monkey-patch a single hook rather than touching the constructor.
    # ------------------------------------------------------------------

    @staticmethod
    def _default_collect_fn() -> Callable[..., dict[str, Any]]:
        from argosy.services.state_snapshot import collect_state_snapshot
        return collect_state_snapshot

    @staticmethod
    def _default_persist_fn() -> Callable[..., Any]:
        from argosy.services.state_snapshot import persist_state_snapshot
        return persist_state_snapshot

    @staticmethod
    def _default_diff_fn() -> Callable[..., Any]:
        from argosy.services.state_diff import compute_full_diff
        return compute_full_diff

    def _default_agent_factory(self) -> Callable[[], Any]:
        from argosy.agents.state_observer import StateObserverAgent

        user_id = self.user_id
        return lambda: StateObserverAgent(user_id=user_id)

    @staticmethod
    def _default_write_fn() -> Callable[..., Any]:
        from argosy.services.state_observer_flag_writer import (
            write_observer_flags,
        )
        return write_observer_flags


def _render_plan_summary(plan_baseline: dict[str, Any] | None) -> str:
    """Minimal plain-text rendering of the plan baseline.

    The observer's system prompt asks for "the plan summary the user's
    plan assumed" — for the daily-cron path we synthesize a one-paragraph
    summary directly from ``plan_inputs``. A future iteration may pull
    the LLM-generated plan-rationale paragraph from the live plan_draft.
    """
    if not isinstance(plan_baseline, dict):
        return "(no active plan baseline available)"
    pi = plan_baseline.get("plan_inputs")
    if not isinstance(pi, dict) or not pi:
        return "(plan baseline empty)"
    bits: list[str] = []
    for k in (
        "assumed_fx_usd_nis",
        "assumed_mu_nominal_annual",
        "assumed_sigma_annual",
        "assumed_inflation_annual",
        "assumed_retirement_age",
        "assumed_marginal_tax_rate",
        "assumed_monthly_expenses_nis",
    ):
        v = pi.get(k)
        if v is not None:
            bits.append(f"{k}={v}")
    return "; ".join(bits) or "(plan baseline has no assumed_* fields)"


# ---------------------------------------------------------------------------
# On-demand trigger entry point (spec §7.3)
# ---------------------------------------------------------------------------


async def run_state_observer_now(
    user_id: str,
    *,
    trigger_reason: TriggerReason = "manual",
    force: bool = False,
    loop: StateObserverLoop | None = None,
) -> dict | None:
    """Service-level entry point for on-demand observer runs.

    Two consumers per spec §7.3:

      * Snapshot upload — call with ``trigger_reason="snapshot_upload"``.
      * Plan re-synthesis — call with ``trigger_reason="plan_resynthesis"``.

    Both go through the same :meth:`StateObserverLoop.tick` body as the
    daily cron — there is no parallel "manual" code path.

    Args:
      user_id: the tenant whose observer to fire.
      trigger_reason: stamped into the snapshot's ``source_versions`` for
        traceability. Defaults to ``"manual"`` (suitable for the admin-UI
        ``Run now`` button).
      force: bypass the §4.4 cool-off. The daily cron + on-demand triggers
        should leave this False; the backfill script + manual-override
        flows set it True.
      loop: inject a pre-built loop (tests + the JobRegistry's
        already-registered instance). Defaults to constructing a fresh
        :class:`StateObserverLoop` with default settings.

    Returns:
      The :meth:`tick` summary dict. ``skipped_reason='cool_off'`` when
      the cool-off blocked the run.
    """
    loop_to_use = loop or StateObserverLoop(user_id=user_id)
    return await loop_to_use.tick(
        trigger_reason=trigger_reason,
        force=force,
    )


__all__ = [
    "MIN_RUN_INTERVAL_MINUTES",
    "StateObserverLoop",
    "TriggerReason",
    "run_state_observer_now",
    "state_observer_metadata",
]
