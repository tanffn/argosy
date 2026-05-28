"""Hybrid-defaults resolver — shipped YAML + per-user identity_yaml override.

Priority order:
  1. ``identity_yaml.retirement_reference_overrides.<key>``  (per-user, intake)
  2. Shipped default in ``argosy/data/israel_retirement_reference.yaml``
  3. ``ResolveError`` if neither.

Freshness:
  - If shipped default's ``as_of_date`` is > 12 months before today, stamp a
    generic "verify with your fund" warning on the returned object (unless
    the YAML already provides one; intrinsic warnings win).
  - User overrides get a similar check at 18 months.
"""
from dataclasses import replace
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale


_REFERENCE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "israel_retirement_reference.yaml"
)


class ResolveError(KeyError):
    """Raised when a reference key is not in shipped YAML or user override."""


@lru_cache(maxsize=4)
def _load_shipped(path_str: str) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path_str).read_text(encoding="utf-8")) or {}
    return raw.get("values", {})


def _build_value(entry: dict[str, Any]) -> ValueWithRationale:
    return ValueWithRationale(
        value=entry.get("value"),
        unit=entry.get("unit", ""),
        source_id=entry.get("source_id"),
        rationale=entry.get("rationale", ""),
        alternatives_considered=list(entry.get("alternatives_considered", [])),
        as_of_date=entry.get("as_of_date"),
        freshness_warning=entry.get("freshness_warning"),
        confidence=entry.get("confidence", "medium"),
    )


def _stamp_freshness(
    v: ValueWithRationale,
    today_iso: str,
    threshold_months: int,
) -> ValueWithRationale:
    if v.freshness_warning:
        return v  # intrinsic warning wins
    if not v.as_of_date:
        return v
    today_d = date.fromisoformat(today_iso)
    asof_str = v.as_of_date
    if len(asof_str) == 7:  # "YYYY-MM"
        asof_str = asof_str + "-01"
    elif len(asof_str) == 4:  # "YYYY"
        asof_str = asof_str + "-01-01"
    asof_d = date.fromisoformat(asof_str)
    months = (today_d.year - asof_d.year) * 12 + (today_d.month - asof_d.month)
    if months > threshold_months:
        return replace(
            v,
            freshness_warning=(
                f"As-of date {v.as_of_date} is > {threshold_months} months old; "
                "verify with your fund / official source."
            ),
        )
    return v


def _load_user_override(
    session: Session, user_id: str, key: str
) -> dict[str, Any] | None:
    """Pull the per-user override block from identity_yaml.

    Schema (in identity_yaml):
      retirement_reference_overrides:
        <key>:
          value: ...
          source: <free-form string for now>
          as_of_date: "YYYY-MM"
          rationale: optional
    """
    from argosy.services.wealth_dashboard import _load_user_context_yaml

    ctx = _load_user_context_yaml(session, user_id) or {}
    overrides = ctx.get("retirement_reference_overrides", {}) or {}
    return overrides.get(key)


def resolve(
    key: str,
    *,
    user_id: str,
    session: Session,
    today: str | None = None,
) -> ValueWithRationale:
    """Resolve a reference value with hybrid defaults.

    Returns a ValueWithRationale stamped with freshness warning if applicable.
    Raises ResolveError if the key is unknown.
    """
    today_iso = today or date.today().isoformat()

    user_override = _load_user_override(session, user_id, key)
    if user_override is not None:
        v = ValueWithRationale(
            value=user_override.get("value"),
            unit=user_override.get("unit", ""),
            source_id=user_override.get("source", "user_intake"),
            rationale=user_override.get(
                "rationale",
                "Provided by user via intake — overrides the shipped Argosy default.",
            ),
            alternatives_considered=[],
            as_of_date=user_override.get("as_of_date"),
            freshness_warning=user_override.get("freshness_warning"),
            confidence=user_override.get("confidence", "high"),
        )
        return _stamp_freshness(v, today_iso, threshold_months=18)

    shipped = _load_shipped(str(_REFERENCE_PATH))
    if key not in shipped:
        raise ResolveError(f"unknown reference key: {key!r}")
    v = _build_value(shipped[key])
    return _stamp_freshness(v, today_iso, threshold_months=12)
