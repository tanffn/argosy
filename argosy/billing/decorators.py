"""Entitlement decorators for FastAPI routes.

  @requires_feature("autonomous_mode")
  async def some_route(...) -> ...:
      ...

  @requires_within_quota("monthly_decisions")
  async def run_decision(...) -> ...:
      ...

The decorators inspect `kwargs` (or the route body) for a `user_id`
(falling back to the contextvar from `argosy.tenancy`) and load the
tenant's entitlements. On failure they raise `HTTPException`:

  * 402 Payment Required when a feature is absent.
  * 429 Too Many Requests when a numeric quota is exceeded.

Both exception classes (`EntitlementError`, `QuotaExceededError`) are
also raised in non-route contexts (CLI, agent loops) so callers can
catch them programmatically.
"""

from __future__ import annotations

import inspect
import math
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from fastapi import HTTPException
from sqlalchemy import func, select

from argosy.billing.entitlements import (
    Entitlements,
    feature_required_tier,
)
from argosy.tenancy.context import current_user_id


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


class EntitlementError(RuntimeError):
    """Raised when a tenant lacks the entitlement for a feature."""

    def __init__(self, feature: str, required_tier: str, plan: str) -> None:
        super().__init__(
            f"feature {feature!r} requires plan {required_tier!r} "
            f"(current: {plan!r})"
        )
        self.feature = feature
        self.required_tier = required_tier
        self.plan = plan


class QuotaExceededError(RuntimeError):
    """Raised when a tenant exceeds a numeric monthly quota."""

    def __init__(self, name: str, current: float, limit: float) -> None:
        super().__init__(
            f"quota {name!r} exceeded: current={current}, limit={limit}"
        )
        self.name = name
        self.current = current
        self.limit = limit


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _extract_user_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Best-effort user_id extraction from route args."""
    bound = current_user_id()
    if bound:
        return bound
    candidate = kwargs.get("user_id")
    if isinstance(candidate, str):
        return candidate
    body = kwargs.get("body") or kwargs.get("payload") or kwargs.get("request_body")
    if body is not None and hasattr(body, "user_id"):
        v = getattr(body, "user_id")
        if isinstance(v, str):
            return v
    for arg in args:
        if hasattr(arg, "user_id") and isinstance(getattr(arg, "user_id"), str):
            return getattr(arg, "user_id")
    return None


def _month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    moment = now or datetime.now(timezone.utc)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


# ----------------------------------------------------------------------
# Decorators
# ----------------------------------------------------------------------


def requires_feature(feature: str) -> Callable[[F], F]:
    """Decorator: 402 if the tenant doesn't have `feature`."""

    def decorator(func_in: F) -> F:
        if not inspect.iscoroutinefunction(func_in):
            raise TypeError("requires_feature requires an async function")

        @wraps(func_in)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            user_id = _extract_user_id(args, kwargs)
            if not user_id:
                # No tenant context — let the route's own validation raise.
                return await func_in(*args, **kwargs)
            ent = Entitlements.load(user_id)
            if not ent.has(feature):
                required = feature_required_tier(feature).value
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "feature_not_entitled",
                        "feature": feature,
                        "required_tier": required,
                        "plan": ent.plan.value,
                    },
                )
            return await func_in(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


async def _count_decisions_this_month(user_id: str) -> int:
    """Counts agent_reports rows in the current calendar month."""
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow

    start, end = _month_window()
    async with db_mod.get_session(user_id=user_id) as session:
        stmt = (
            select(func.count(AgentReportRow.id))
            .where(AgentReportRow.user_id == user_id)
            .where(AgentReportRow.created_at >= start)
            .where(AgentReportRow.created_at < end)
        )
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)


async def _spend_this_month_usd(user_id: str) -> float:
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow

    start, end = _month_window()
    async with db_mod.get_session(user_id=user_id) as session:
        stmt = (
            select(func.coalesce(func.sum(AgentReportRow.cost_usd), 0))
            .where(AgentReportRow.user_id == user_id)
            .where(AgentReportRow.created_at >= start)
            .where(AgentReportRow.created_at < end)
        )
        result = await session.execute(stmt)
        return float(result.scalar_one() or 0)


def requires_within_quota(name: str) -> Callable[[F], F]:
    """Decorator: 429 when the tenant has hit a per-month quota.

    Supported names:
      - "monthly_decisions"        → counts agent_reports rows
      - "monthly_claude_spend_usd" → sums agent_reports.cost_usd
    """

    def decorator(func_in: F) -> F:
        if not inspect.iscoroutinefunction(func_in):
            raise TypeError("requires_within_quota requires an async function")

        @wraps(func_in)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            user_id = _extract_user_id(args, kwargs)
            if not user_id:
                return await func_in(*args, **kwargs)
            ent = Entitlements.load(user_id)
            limit = ent.limit(name)
            if math.isinf(limit):
                return await func_in(*args, **kwargs)
            if name == "monthly_decisions":
                current: float = float(await _count_decisions_this_month(user_id))
            elif name == "monthly_claude_spend_usd":
                current = await _spend_this_month_usd(user_id)
            else:
                # Unknown quota name: fail closed with a 500-equivalent.
                raise HTTPException(
                    status_code=500,
                    detail=f"unknown quota: {name!r}",
                )
            if current >= limit:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "quota_exceeded",
                        "quota": name,
                        "current": current,
                        "limit": limit,
                    },
                )
            return await func_in(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


__all__ = [
    "EntitlementError",
    "QuotaExceededError",
    "requires_feature",
    "requires_within_quota",
]
