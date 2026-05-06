"""Service layer for plan distillation.

Wraps PlanDistillerAgent + persistence logic. Used by:
  - argosy.api.routes.intake (baseline upload happy-path)
  - argosy.orchestrator.loops.plan_watcher (file-change re-distill)
  - the future "Re-distill" UI button

User-edit preservation: each PlanDistillate item carries a ``user_edited``
flag. When ``preserve_user_edits=True`` (the default), re-distillation
merges fresh agent output with prior user-edited items, keeping the
user's value. ``preserve_user_edits=False`` drops user edits (force-refresh).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from argosy.agents.plan_distiller import PlanDistillerAgent
from argosy.agents.plan_distiller_render import render_distillate
from argosy.agents.plan_distiller_types import PlanDistillate
from argosy.logging import get_logger
from argosy.state.models import PlanVersion

log = get_logger(__name__)


@dataclass
class DistillResult:
    plan_version_id: int
    distillate: PlanDistillate
    source_hash: str
    user_edits_preserved: int


def _make_agent(user_id: str) -> PlanDistillerAgent:
    """Indirection point so tests can monkeypatch.

    BaseAgent requires user_id; thread it through from the service caller.
    """
    return PlanDistillerAgent(user_id=user_id)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def distill_baseline_plan(
    session: Session,
    *,
    plan_version_id: int,
    user_id: str,
    preserve_user_edits: bool = True,
) -> DistillResult:
    """Run the distiller against a plan_versions row and persist the result.

    Args:
        plan_version_id: must exist and have role='baseline'.
        user_id: stamped on agent_reports rows.
        preserve_user_edits: if True (default), prior user-edited items
            survive re-distillation. If False, agent output wins.

    Returns:
        DistillResult with the parsed PlanDistillate and side-effects
        already persisted on the row.
    """
    pv = session.get(PlanVersion, plan_version_id)
    if pv is None:
        raise ValueError(f"plan_version {plan_version_id} not found")
    if pv.role != "baseline":
        raise ValueError(
            f"plan_version {plan_version_id} role={pv.role!r}, expected 'baseline'"
        )

    # Capture prior user-edits before agent call.
    prior_edits: dict[str, dict[str, dict]] = {}
    if preserve_user_edits and pv.distillate_json:
        prior = json.loads(pv.distillate_json)
        for category in ("goals", "principles", "decision_rules", "targets", "constraints"):
            for item in prior.get(category) or []:
                if item.get("user_edited"):
                    prior_edits.setdefault(category, {})[item["label"]] = item

    # Run the agent.
    agent = _make_agent(user_id)
    result = agent.run_sync(
        plan_label=pv.version_label or "Imported plan",
        plan_markdown=pv.raw_markdown,
    )
    fresh: PlanDistillate = result.output  # type: ignore[attr-defined]

    # Merge user edits back in.
    edits_preserved = 0
    if prior_edits:
        for category, by_label in prior_edits.items():
            items = getattr(fresh, category)
            new_items = []
            for fresh_item in items:
                edit = by_label.get(fresh_item.label)
                if edit is None:
                    new_items.append(fresh_item)
                    continue
                # Apply user edit on top of the fresh item.
                # Merge at the dict level and re-validate via pydantic so
                # that string dates from JSON are coerced back to date objects.
                fresh_dict = fresh_item.model_dump()
                item_model_fields = type(fresh_item).model_fields
                for k, v in edit.items():
                    if k in item_model_fields and k != "label":
                        fresh_dict[k] = v
                merged = type(fresh_item).model_validate(fresh_dict)
                new_items.append(merged)
                edits_preserved += 1
            setattr(fresh, category, new_items)

    # Persist.
    pv.distillate_json = fresh.model_dump_json()
    pv.distillate_rendered = render_distillate(fresh)
    pv.source_hash = _sha256(pv.raw_markdown)
    pv.distilled_at = datetime.now(timezone.utc)
    session.commit()

    log.info(
        "plan_distiller.persisted",
        plan_version_id=plan_version_id,
        user_id=user_id,
        edits_preserved=edits_preserved,
    )

    return DistillResult(
        plan_version_id=plan_version_id,
        distillate=fresh,
        source_hash=pv.source_hash,
        user_edits_preserved=edits_preserved,
    )


def set_distillate_item_user_edit(
    session: Session,
    *,
    plan_version_id: int,
    category: str,
    item_label: str,
    new_value: dict[str, Any],
) -> None:
    """Apply a user edit to one item of the distillate.

    The category must be one of: goals, principles, decision_rules,
    targets, constraints. The item is matched by ``label``. Sets
    ``user_edited=True`` on the item; merges in any keys from ``new_value``.
    """
    valid = {"goals", "principles", "decision_rules", "targets", "constraints"}
    if category not in valid:
        raise ValueError(f"category {category!r} not in {valid}")

    pv = session.get(PlanVersion, plan_version_id)
    if pv is None or pv.distillate_json is None:
        raise ValueError(f"plan_version {plan_version_id} has no distillate")

    payload = json.loads(pv.distillate_json)
    items = payload.get(category) or []
    for item in items:
        if item.get("label") == item_label:
            item.update(new_value)
            item["user_edited"] = True
            break
    else:
        raise ValueError(f"no item with label={item_label!r} in {category}")

    payload[category] = items
    pv.distillate_json = json.dumps(payload)
    # Re-render markdown view from the edited payload.
    pv.distillate_rendered = render_distillate(PlanDistillate.model_validate(payload))
    session.commit()


__all__ = [
    "DistillResult",
    "distill_baseline_plan",
    "set_distillate_item_user_edit",
]
