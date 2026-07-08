"""Annual loop (SDD §5.1, Phase 7).

Cron `0 8 2 1 *` (January 2nd). Surfaces annual prompts to the user:
  - Tax-filing prep
  - W-8BEN refresh prompt
  - Insurance renewal prompt

Triggers a full domain re-verify (calls `DomainRefreshAgent` over every
file regardless of `next_refresh_due`).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from argosy.agents.domain_refresh import (
    DomainRefreshAgent,
    DomainRefreshReport,
    write_back_refresh_results,
)
from argosy.api.events import publish_event
from argosy.config import get_settings
from argosy.execution.audit import record_audit_event
from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule

_log = get_logger("argosy.loops.annual")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AnnualLoop(CadenceLoop):
    """Year-start prompts + full domain re-verify."""

    name = "annual"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        domain_refresh_factory: Callable[[], DomainRefreshAgent] | None = None,
        domain_files_provider: Callable[[], Iterable[dict[str, str]]] | None = None,
        pension_refresh_callable: Callable[[str], Any] | None = None,
        domain_knowledge_root: Path | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._refresh_factory = domain_refresh_factory or (
            lambda: DomainRefreshAgent(user_id=user_id)
        )
        self._files_provider = domain_files_provider or _default_files_provider
        # 2026-07-08 write-back fix: where the frontmatter verification
        # stamps land. Injectable so tests never touch the real
        # `domain_knowledge/` tree; defaults to settings at tick time.
        self._domain_knowledge_root = domain_knowledge_root
        # Phase 3: pluggable pension snapshot job (gemelnet adapter).
        # Defaults to None — when omitted the loop attempts the job
        # lazily and silently no-ops if the user has no `pensions`
        # block. Tests can inject a fake to avoid network access.
        self._pension_refresh: Callable[[str], Any] | None = pension_refresh_callable
        # Per-step outcome side-channel read by RegisteredScheduler
        # (`_safe_output_summary`) so `job_runs.output_summary` records
        # which sub-step failed even when tick() raises. Same contract
        # as NewsDailyJob's multi-stage summary.
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        # Reset the side-channel BEFORE any work so a raise never leaves the
        # adapter reading a PRIOR tick's summary (news_daily precedent).
        self.last_output_summary = None
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("annual.kill_switch_skip")
            return None

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("annual.cost_guard_paused")
            return None

        moment = (now or _utcnow)()

        prompts = [
            {"kind": "tax_filing_prep", "message": "Prepare prior-year tax filing (דוח שנתי)."},
            {
                "kind": "w8ben_refresh",
                "message": "Refresh W-8BEN at Schwab (3-year cycle).",
            },
            {"kind": "insurance_renewal", "message": "Review insurance policy renewals."},
        ]
        for p in prompts:
            try:
                await publish_event(
                    "annual.prompt",
                    {"user_id": self.user_id, "run_at": moment.isoformat(), **p},
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("annual.publish_failed")

        # Full domain re-verify. A sub-step failure here is captured (never
        # swallowed into a green job run) and re-raised at the END of the
        # tick, after the remaining independent steps + the audit event have
        # completed — so RegisteredScheduler closes the `job_runs` row with
        # status='error' (→ /api/jobs health red) while `last_output_summary`
        # still records the partial progress. Previously the exception was
        # logged and dropped, leaving the domain_refresh agent silently dead
        # for days while the annual job reported ok.
        refresh_error: str | None = None
        try:
            files = list(self._files_provider())
        except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
            _log.exception("annual.files_provider_failed")
            files = []
            refresh_error = f"files_provider failed — {type(exc).__name__}: {exc}"

        refresh_summary: str | None = None
        writeback: dict[str, Any] | None = None
        agent_report_id: int | None = None
        discrepancy_count = 0
        if files:
            try:
                agent = self._refresh_factory()
                report = await agent.run(files_due=files)
                refresh_summary = report.output.summary
            except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
                _log.exception("annual.domain_refresh_failed")
                refresh_error = f"{type(exc).__name__}: {exc}"
            else:
                # 2026-07-08 systemic-gap fix: verdicts must land somewhere
                # durable. (1) Stamp `last_verified` / matched `retrieved`
                # dates into the files' frontmatter (never content — a
                # parameter change is a user decision, see (3)); (2) persist
                # the report to agent_reports for auditability; (3) surface
                # changed/outdated parameters as ONE aggregated note_only
                # ActionProposal. Failures here fail the tick LOUD — a
                # silently-dropped verdict is exactly the gap being fixed.
                try:
                    root = (
                        self._domain_knowledge_root
                        or get_settings().domain_knowledge_dir
                    )
                    writeback = write_back_refresh_results(
                        report.output, root=root
                    )
                except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
                    _log.exception("annual.domain_refresh_writeback_failed")
                    refresh_error = f"writeback: {type(exc).__name__}: {exc}"
                try:
                    agent_report_id = await _persist_refresh_report(report)
                except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
                    _log.exception("annual.domain_refresh_persist_failed")
                    refresh_error = f"persist: {type(exc).__name__}: {exc}"
                try:
                    discrepancy_count = await _surface_refresh_discrepancies(
                        user_id=self.user_id,
                        output=report.output,
                        now=moment,
                    )
                except Exception as exc:  # noqa: BLE001 — captured, re-raised at end
                    _log.exception("annual.domain_refresh_discrepancies_failed")
                    refresh_error = (
                        f"discrepancy proposal: {type(exc).__name__}: {exc}"
                    )

        # Phase 3: opportunistic gemelnet pension snapshot.
        # We do NOT bubble exceptions — pensions data is auxiliary; an
        # unreachable MoF site shouldn't fail the annual loop. The outcome
        # is still recorded in the output summary so it is observable.
        pensions_refreshed: int | None = None
        pension_error: str | None = None
        try:
            if self._pension_refresh is not None:
                outcome = self._pension_refresh(self.user_id)
                if hasattr(outcome, "__await__"):
                    outcome = await outcome  # type: ignore[assignment]
                if isinstance(outcome, int):
                    pensions_refreshed = outcome
                elif isinstance(outcome, dict):
                    pensions_refreshed = int(outcome.get("refreshed", 0))
        except Exception as exc:  # noqa: BLE001 — auxiliary; recorded, not raised
            _log.exception("annual.pension_refresh_failed")
            pension_error = f"{type(exc).__name__}: {exc}"

        await record_audit_event(
            user_id=self.user_id,
            event_type="annual.completed",
            entity_type="cadence",
            entity_id="annual",
            payload={
                "now": moment.isoformat(),
                "prompts_count": len(prompts),
                "files_reviewed": len(files),
                "refresh_summary": refresh_summary,
                "pensions_refreshed": pensions_refreshed,
            },
        )

        self.last_output_summary = {
            "prompts_count": len(prompts),
            "files_reviewed": len(files),
            "steps": {
                "prompts": "ok",
                "domain_refresh": (
                    "error" if refresh_error else ("ok" if files else "skipped_no_files")
                ),
                "pension_refresh": (
                    "error"
                    if pension_error
                    else ("ok" if self._pension_refresh is not None else "skipped")
                ),
            },
            "refresh_summary": refresh_summary,
            "domain_refresh_error": refresh_error,
            "domain_refresh_writeback": writeback,
            "domain_refresh_report_id": agent_report_id,
            "domain_refresh_discrepancies": discrepancy_count,
            "pension_refresh_error": pension_error,
            "pensions_refreshed": pensions_refreshed,
        }

        if refresh_error:
            # Fail LOUD so the job run lands not-ok and the existing
            # /api/jobs health derivation surfaces red — no bespoke
            # detector. Partial progress remains readable via
            # `last_output_summary` (RegisteredScheduler exception path).
            raise RuntimeError(
                f"annual: domain_refresh sub-step failed — {refresh_error}"
            )
        return self.last_output_summary


async def _persist_refresh_report(report: Any) -> int | None:
    """Write the refresh run to `agent_reports` (+ output blob).

    Standard cross-cutting-agent persistence pattern (same shape as the
    intake CLI's `_persist_agent_report`): BaseAgent.run() does NOT persist
    for standalone callers, so before this the annual loop's refresh run
    left no auditable row at all.
    """
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow
    from argosy.state.models import AgentReportBlob

    async with db_mod.get_session() as session:
        row = AgentReportRow(
            user_id=report.user_id,
            agent_role=report.agent_role,
            decision_id=report.decision_id,
            prompt_hash=report.prompt_hash,
            response_text=report.response_text,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            cost_usd=report.cost_usd,
            model=report.model,
            confidence=report.confidence.value if report.confidence else None,
            cache_input_tokens=report.cache_input_tokens,
            cache_creation_tokens=report.cache_creation_tokens,
            thinking_tokens=report.thinking_tokens,
            citations_json=report.citations_json,
        )
        session.add(row)
        await session.flush()
        try:
            output_json = report.output.model_dump_json()
        except Exception:  # noqa: BLE001 - defensive serialization fallback
            output_json = json.dumps({"error": "could not serialize output"})
        session.add(
            AgentReportBlob(report_id=row.id, key="output_json", value=output_json)
        )
        await session.commit()
        return row.id


async def _surface_refresh_discrepancies(
    *, user_id: str, output: DomainRefreshReport, now: datetime
) -> int:
    """One aggregated note_only ActionProposal for changed/outdated params.

    The refresh agent NEVER auto-edits file content; any `change_proposed`
    verdict is a decision for the user. Aggregated into ONE open proposal
    (idempotent per dedup_key, refreshed in place) — same pattern as the
    critique-reconcile escalation aggregation. Returns the number of
    discrepancies surfaced (0 → no proposal touched).
    """
    from sqlalchemy import select

    from argosy.state import db as db_mod
    from argosy.state.models import ActionProposal

    discrepancies = [r for r in output.per_file if r.status != "no_change"]
    if not discrepancies:
        return 0

    lines: list[str] = []
    for r in discrepancies:
        detail = (r.note or "").strip() or "(no note)"
        lines.append(f"- **{r.path}** — {detail}")
        if r.diff:
            lines.append(f"  ```diff\n{r.diff.strip()}\n  ```")
    summary = (
        f"Domain-knowledge refresh found {len(discrepancies)} file"
        f"{'s' if len(discrepancies) != 1 else ''} with changed/outdated "
        "parameters"
    )
    rationale_md = (
        "The domain-refresh agent re-verified `domain_knowledge/` against "
        "live sources and reports these parameter discrepancies. Files were "
        "NOT auto-edited — approve the updates (or dismiss) here:\n\n"
        + "\n".join(lines)
    )
    payload = {"discrepancies": [r.model_dump(mode="json") for r in discrepancies]}
    dedup_key = f"domain_refresh_discrepancies:{user_id}"

    async with db_mod.get_session() as session:
        existing = (
            await session.execute(
                select(ActionProposal).where(
                    ActionProposal.dedup_key == dedup_key,
                    ActionProposal.status == "open",
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.summary = summary
            existing.rationale_md = rationale_md
            existing.suggested_payload = json.dumps(payload)
            existing.severity = "warning"
            existing.surfaced_at = now
            existing.expires_at = now + timedelta(days=30)
        else:
            session.add(
                ActionProposal(
                    user_id=user_id,
                    summary=summary,
                    rationale_md=rationale_md,
                    suggested_payload=json.dumps(payload),
                    severity="warning",
                    surfaced_at=now,
                    expires_at=now + timedelta(days=30),
                    status="open",
                    kind="note_only",
                    dedup_key=dedup_key,
                    execution_state="proposed",
                )
            )
        await session.commit()
    return len(discrepancies)


def _default_files_provider() -> list[dict[str, str]]:
    """Walk `domain_knowledge/` and return every `.md` file's content."""
    out: list[dict[str, str]] = []
    settings = get_settings()
    root = settings.domain_knowledge_dir
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*.md")):
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - defensive
            continue
        # Split frontmatter (optional `---\n...\n---` at the top).
        frontmatter = ""
        body = content
        if content.startswith("---\n"):
            end = content.find("\n---\n", 4)
            if end > 0:
                frontmatter = content[4:end]
                body = content[end + 5 :]
        out.append(
            {
                "path": str(p.relative_to(root.parent)),
                "frontmatter": frontmatter,
                "content": body,
            }
        )
    return out


__all__ = ["AnnualLoop"]
