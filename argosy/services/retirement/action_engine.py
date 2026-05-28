"""Risk-prioritized action engine (MED #24).

Replaces the prior action-items widget (a JSON extractor of plan output)
with a real policy engine that emits concrete, prioritized, time-bounded
actions backed by:
  - Severity (BLOCKER / HIGH / MEDIUM / LOW)
  - Owner (ariel / noga / joint / advisor)
  - Consequence_score (lifetime NPV impact of skipping)
  - Due date

This module is the entry-point for "what do I do this week"; downstream
waves can plug in domain-specific action generators.
"""
from dataclasses import dataclass, field
from typing import Literal


Severity = Literal["BLOCKER", "HIGH", "MEDIUM", "LOW"]
Owner = Literal["ariel", "noga", "joint", "advisor"]


@dataclass(frozen=True)
class PrioritizedAction:
    id: str
    title: str
    rationale: str
    severity: Severity
    due_date: str | None
    owner: Owner
    consequence_score_nis: float  # lifetime NPV impact of skipping
    dependencies: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)


_SEVERITY_ORDER: dict[Severity, int] = {"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def prioritize_actions(actions: list[PrioritizedAction]) -> list[PrioritizedAction]:
    """Sort by severity first, then by consequence_score descending."""
    return sorted(
        actions,
        key=lambda a: (_SEVERITY_ORDER[a.severity], -a.consequence_score_nis),
    )


def make_action(
    *,
    id: str,
    title: str,
    rationale: str,
    severity: Severity,
    owner: Owner = "ariel",
    consequence_score_nis: float = 0.0,
    due_date: str | None = None,
    dependencies: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> PrioritizedAction:
    return PrioritizedAction(
        id=id, title=title, rationale=rationale,
        severity=severity, due_date=due_date, owner=owner,
        consequence_score_nis=consequence_score_nis,
        dependencies=list(dependencies or []),
        source_ids=list(source_ids or []),
    )
