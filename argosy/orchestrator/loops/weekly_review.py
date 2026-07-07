"""Weekly Review loop (SDD §5.1, Phase 3).

Runs at the configured cron (default `0 18 * * SUN`). Performs a full
T3-style plan-critique re-pass against the latest portfolio snapshot
and flags anything RED for the user. No auto-action; the user reviews
and decides whether to act.

The loop is deliberately thin: a fresh `PlanCritiqueAgent.run(...)`
against the latest plan + portfolio snapshot. The result is a new row
in `plan_critiques`. RED findings are surfaced via the
`weekly_review.flagged` WebSocket event so the dashboard can highlight
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import desc, select

from argosy.agents.plan_critique import PlanCritiqueAgent, PlanCritiqueReport
from argosy.api.events import publish_event
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import PlanCritique, PlanVersion

_log = get_logger("argosy.loops.weekly_review")


@dataclass
class WeeklyReviewInputs:
    """Inputs to one weekly-review run, gathered before the LLM call."""

    user_id: str
    plan_label: str
    plan_markdown: str
    plan_version_id: int | None
    snapshot_label: str
    snapshot_summary: str


class WeeklyReviewLoop(CadenceLoop):
    """Plan-critique re-pass + RED-flagging loop.

    Wired into the scheduler when `cadences.weekly_review.enabled` is true.
    """

    name = "weekly_review"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        plan_critique_factory: Callable[[], PlanCritiqueAgent] | None = None,
        gather_inputs: Callable[[str], "WeeklyReviewInputs | Any"] | None = None,
        reconcile_fn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._critique_factory = plan_critique_factory or (
            lambda: PlanCritiqueAgent(user_id=user_id)
        )
        self._gather = gather_inputs or _default_gather_inputs
        # Critique reconcile loop (FIND -> CORRECT -> RE-VERIFY). Injectable
        # for tests; None resolves to the module-level ``_default_reconcile``
        # at TICK time (late-bound so the conftest live-LLM guard can patch
        # it). Gated at tick time by ARGOSY_CRITIQUE_RECONCILE (default ON).
        self._reconcile = reconcile_fn

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        run_at = (now or _utcnow)()
        inputs = await self._maybe_async(self._gather(self.user_id))
        if not isinstance(inputs, WeeklyReviewInputs):
            raise TypeError(
                f"gather_inputs must return WeeklyReviewInputs, got {type(inputs)!r}"
            )

        if not inputs.plan_markdown.strip():
            _log.warning("weekly_review.no_plan", user_id=self.user_id)
            return

        agent = self._critique_factory()
        report = await agent.run(
            plan_label=inputs.plan_label,
            plan_markdown=inputs.plan_markdown,
            snapshot_label=inputs.snapshot_label,
            snapshot_summary=inputs.snapshot_summary,
            user_context_yaml="",
            domain_kb_files={},
        )
        out: PlanCritiqueReport = report.output  # type: ignore[assignment]

        # Persist the critique
        critique_id: int | None = None
        if inputs.plan_version_id is not None:
            async with db_mod.get_session() as session:
                row = PlanCritique(
                    user_id=self.user_id,
                    plan_version_id=inputs.plan_version_id,
                    critique_json=out.model_dump_json(),
                    model=report.model,
                )
                session.add(row)
                await session.commit()
                critique_id = row.id

        red = [f for f in out.findings if f.severity == "RED"]
        try:
            await publish_event(
                "weekly_review.flagged",
                {
                    "user_id": self.user_id,
                    "run_at": run_at.isoformat(),
                    "red_count": len(red),
                    "findings": [
                        {
                            "plan_item_ref": f.plan_item_ref,
                            "severity": f.severity,
                            "topic": f.topic,
                            "summary": f.summary,
                        }
                        for f in red
                    ],
                },
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("weekly_review.publish_failed")

        # Critique reconcile loop — feed the findings back into the fleet
        # (FIND -> CORRECT -> RE-VERIFY). Runs AFTER the flagged event so the
        # dashboard sees the fresh critique immediately (the reconcile makes
        # two more LLM calls and can take minutes). Cost-bounded inside the
        # service (max 1 closer + 1 re-verify per critique). Fail-soft: a
        # reconcile error never loses the critique row that already landed.
        if inputs.plan_version_id is not None and _reconcile_enabled():
            try:
                _reconcile = self._reconcile or _default_reconcile
                await _reconcile(
                    user_id=self.user_id,
                    plan_version_id=inputs.plan_version_id,
                    plan_label=inputs.plan_label,
                    plan_markdown=inputs.plan_markdown,
                    report=out,
                    source_critique_id=critique_id,
                )
            except Exception:  # noqa: BLE001 — loud log, critique row survives
                _log.exception(
                    "weekly_review.reconcile_failed", user_id=self.user_id
                )

    @staticmethod
    async def _maybe_async(value: Any) -> Any:
        if hasattr(value, "__await__"):
            return await value
        return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reconcile_enabled() -> bool:
    """ARGOSY_CRITIQUE_RECONCILE knob (default ON — nothing hidden)."""
    try:
        from argosy.config import get_settings

        return bool(getattr(get_settings(), "critique_reconcile", True))
    except Exception:  # noqa: BLE001 — settings load must never kill the loop
        return True


async def _default_reconcile(**kwargs: Any) -> Any:
    from argosy.services.critique_reconcile import reconcile_critique

    return await reconcile_critique(**kwargs)


async def _default_gather_inputs(user_id: str) -> WeeklyReviewInputs:
    """Pulls the latest plan + last daily-brief summary for the snapshot."""
    plan_label = "(no plan imported)"
    plan_markdown = ""
    plan_version_id: int | None = None
    snapshot_label = "weekly_review:auto"
    snapshot_summary = ""

    async with db_mod.get_session() as session:
        plan = (
            await session.execute(
                select(PlanVersion)
                .where(PlanVersion.user_id == user_id)
                .order_by(desc(PlanVersion.imported_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is not None:
            plan_label = plan.version_label or f"plan_version_id={plan.id}"
            # Graph-authored plans carry no raw_markdown — render the
            # canonical export so the critique reviews the real plan
            # instead of silently no-op'ing (`weekly_review.no_plan`).
            from argosy.services.plan_export import plan_markdown_for_review

            plan_markdown = await session.run_sync(
                lambda s: plan_markdown_for_review(s, user_id=user_id, plan=plan)
            )
            plan_version_id = plan.id

    return WeeklyReviewInputs(
        user_id=user_id,
        plan_label=plan_label,
        plan_markdown=plan_markdown,
        plan_version_id=plan_version_id,
        snapshot_label=snapshot_label,
        snapshot_summary=snapshot_summary,
    )


__all__ = ["WeeklyReviewInputs", "WeeklyReviewLoop"]
