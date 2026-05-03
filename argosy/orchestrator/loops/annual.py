"""Annual loop (SDD §5.1, Phase 7).

Cron `0 8 2 1 *` (January 2nd). Surfaces annual prompts to the user:
  - Tax-filing prep
  - W-8BEN refresh prompt
  - Insurance renewal prompt

Triggers a full domain re-verify (calls `DomainRefreshAgent` over every
file regardless of `next_refresh_due`).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from argosy.agents.domain_refresh import DomainRefreshAgent
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
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._refresh_factory = domain_refresh_factory or (
            lambda: DomainRefreshAgent(user_id=user_id)
        )
        self._files_provider = domain_files_provider or _default_files_provider

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        if os.environ.get("ARGOSY_KILL") == "1":
            _log.info("annual.kill_switch_skip")
            return

        guard = get_cost_guard(user_id=self.user_id)
        if await guard.should_pause_non_routine(loop_name=self.name):
            _log.info("annual.cost_guard_paused")
            return

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

        # Full domain re-verify
        try:
            files = list(self._files_provider())
        except Exception:  # pragma: no cover - defensive
            _log.exception("annual.files_provider_failed")
            files = []

        refresh_summary: str | None = None
        if files:
            try:
                agent = self._refresh_factory()
                report = await agent.run(files_due=files)
                refresh_summary = report.output.summary
            except Exception:  # pragma: no cover - defensive
                _log.exception("annual.domain_refresh_failed")

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
            },
        )


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
