"""migrate identity.pensions list-shape → dict-keyed-by-vehicle.

Revision ID: 0013_pensions_to_dict_shape
Revises: 0012_investor_events
Create Date: 2026-05-04

The Phase 2 gap-tracker (`argosy.agents.gap_tracker.STAGE_FIELDS`)
encodes per-vehicle pension fields under dict-keyed paths like
``identity.pensions.keren_hishtalmut.balance_nis``. The legacy
``identity.pensions`` shape — written by the gemelnet CLI through
Phase 3 — was a flat list of fund dicts, which the gap-tracker's
``_lookup`` walker couldn't traverse (encountering a list at step 1
returned ``None``, marking every per-vehicle field permanently
missing and breaking stage_3 auto-advance for any user with
gemelnet data).

This migration walks every ``user_context`` row, parses
``identity_yaml``, and converts a list-shaped ``pensions`` field into
a dict keyed by canonical vehicle (``keren_hishtalmut`` /
``kupat_gemel`` / ``kupat_pensia``). Each vehicle dict aggregates
``balance_nis`` across funds of that vehicle, preserves the first-seen
``contribution_rate_pct`` / ``employer_match_pct``, and keeps the
fund-level metadata under a ``funds`` list of
``{fund_id, fund_name, last_refreshed_at}``.

Idempotent — if ``pensions`` is already dict-shaped (or absent),
the row is skipped. Downgrade flattens dict back to list shape so
forward and backward migrations are symmetric.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import yaml
from sqlalchemy import text

from alembic import op

revision: str = "0013_pensions_to_dict_shape"
down_revision: str | Sequence[str] | None = "0012_investor_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VEHICLE_KEYS: tuple[str, ...] = (
    "keren_hishtalmut",
    "kupat_gemel",
    "kupat_pensia",
)

_TYPE_ALIASES: dict[str, str] = {
    "keren_hishtalmut": "keren_hishtalmut",
    "kupat_gemel": "kupat_gemel",
    "kupat_pensia": "kupat_pensia",
    "קרן השתלמות": "keren_hishtalmut",
    "hishtalmut": "keren_hishtalmut",
    "קופת גמל": "kupat_gemel",
    "gemel": "kupat_gemel",
    "kupat gemel": "kupat_gemel",
    "פנסיה": "kupat_pensia",
    "קרן פנסיה": "kupat_pensia",
    "pensia": "kupat_pensia",
    "pension": "kupat_pensia",
}


def _vehicle_key(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s in _VEHICLE_KEYS:
        return s
    return _TYPE_ALIASES.get(s) or _TYPE_ALIASES.get(str(raw).strip())


def _list_to_dict(pensions: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in pensions:
        if not isinstance(entry, dict):
            continue
        vk = _vehicle_key(entry.get("type"))
        if vk is None:
            vk = "kupat_gemel"  # safest default — locked-till-retirement
        bucket = out.setdefault(vk, {"funds": []})
        bal = entry.get("balance_nis")
        try:
            bal_f = float(bal) if bal is not None else None
        except (TypeError, ValueError):
            bal_f = None
        if bal_f is not None:
            bucket["balance_nis"] = (bucket.get("balance_nis") or 0.0) + bal_f
        for k in ("contribution_rate_pct", "employer_match_pct"):
            if entry.get(k) is not None and bucket.get(k) is None:
                bucket[k] = entry.get(k)
        fund_record: dict[str, Any] = {
            "fund_id": entry.get("fund_id"),
            "fund_name": entry.get("fund_name"),
        }
        if entry.get("last_refreshed"):
            fund_record["last_refreshed_at"] = entry.get("last_refreshed")
        if entry.get("last_refreshed_at"):
            fund_record["last_refreshed_at"] = entry.get("last_refreshed_at")
        bucket.setdefault("funds", []).append(fund_record)
    return out


def _dict_to_list(pensions: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for vk in _VEHICLE_KEYS:
        bucket = pensions.get(vk)
        if not isinstance(bucket, dict):
            continue
        funds = bucket.get("funds") or []
        if not isinstance(funds, list) or not funds:
            # No fund-level rows: emit a single placeholder so balance
            # survives the round-trip.
            out.append(
                {
                    "type": vk,
                    "balance_nis": bucket.get("balance_nis"),
                    "contribution_rate_pct": bucket.get("contribution_rate_pct"),
                    "employer_match_pct": bucket.get("employer_match_pct"),
                }
            )
            continue
        # Spread the bucket's aggregate balance across the first fund so
        # we don't lose it on round-trip; subsequent funds get balance=None.
        agg_balance = bucket.get("balance_nis")
        for i, f in enumerate(funds):
            if not isinstance(f, dict):
                continue
            row = {
                "type": vk,
                "fund_id": f.get("fund_id"),
                "fund_name": f.get("fund_name"),
                "balance_nis": agg_balance if i == 0 else None,
            }
            if f.get("last_refreshed_at"):
                row["last_refreshed"] = f.get("last_refreshed_at")
            out.append(row)
    return out


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text("SELECT user_id, identity_yaml FROM user_context")
    ).fetchall()
    for row in rows:
        user_id = row[0]
        identity_yaml = row[1] or ""
        if not identity_yaml.strip():
            continue
        try:
            identity = yaml.safe_load(identity_yaml) or {}
        except yaml.YAMLError:
            # Don't clobber unparseable YAML — leave it for manual repair.
            continue
        if not isinstance(identity, dict):
            continue
        pensions = identity.get("pensions")
        if not isinstance(pensions, list):
            continue  # already dict-shaped, or absent → skip
        new_pensions = _list_to_dict(pensions)
        identity["pensions"] = new_pensions
        new_yaml = yaml.safe_dump(identity, allow_unicode=True, sort_keys=False)
        bind.execute(
            text(
                "UPDATE user_context SET identity_yaml = :y WHERE user_id = :u"
            ),
            {"y": new_yaml, "u": user_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text("SELECT user_id, identity_yaml FROM user_context")
    ).fetchall()
    for row in rows:
        user_id = row[0]
        identity_yaml = row[1] or ""
        if not identity_yaml.strip():
            continue
        try:
            identity = yaml.safe_load(identity_yaml) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(identity, dict):
            continue
        pensions = identity.get("pensions")
        if not isinstance(pensions, dict):
            continue  # already list-shaped, or absent → skip
        new_pensions = _dict_to_list(pensions)
        identity["pensions"] = new_pensions
        new_yaml = yaml.safe_dump(identity, allow_unicode=True, sort_keys=False)
        bind.execute(
            text(
                "UPDATE user_context SET identity_yaml = :y WHERE user_id = :u"
            ),
            {"y": new_yaml, "u": user_id},
        )
