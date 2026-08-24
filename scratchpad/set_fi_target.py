"""Record Ariel's settled FI spend basis: ₪300,000/yr (2026-08-24).

His words: "FI target can be a round 300k. you know the real expenses, and just
round it up."

Written into ``goals_yaml`` so the RESOLVER publishes it — a guidance-only
change would have the fleet write ₪300,000 into the prose while
``{{fact:retirement.fi_target_nis}}`` kept resolving to the derived ₪311,584,
manufacturing the two-values-for-one-concept contradiction codex blocks on.

Effect: FI perpetuity = 300,000 / 3.0% = ₪10,000,000 (from ₪10,386,133).
The derived line items stay visible; an explicit adjustment row carries the
−₪11,584 delta and cites the directive, so the basis remains auditable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
import yaml  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

USER_ID = "ariel"
KEY = "fi_target_annual_spend_nis"
VALUE = 300_000.0

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

from argosy.services.fi_methodology import compute_fi_target  # noqa: E402
from argosy.state.models import UserContext  # noqa: E402

before = compute_fi_target(session, user_id=USER_ID)
print(f"BEFORE: spend {before.permanent_annual_spend_nis:,.0f} "
      f"-> FI {before.fi_perpetuity_nis:,.0f} "
      f"(+ reserve {before.finite_liability_reserve_nis:,.0f} "
      f"= {before.fi_total_capital_nis:,.0f})")

ctx = session.execute(
    sa.select(UserContext).where(UserContext.user_id == USER_ID)
).scalar_one_or_none()
if ctx is None:
    raise SystemExit("no UserContext for ariel")

data = yaml.safe_load(ctx.goals_yaml or "") or {}
if not isinstance(data, dict):
    raise SystemExit(f"goals_yaml is not a mapping: {type(data)}")

prior = data.get(KEY)
data[KEY] = VALUE
ctx.goals_yaml = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
session.commit()
print(f"goals_yaml.{KEY}: {prior!r} -> {VALUE:,.0f}")

session.expire_all()
after = compute_fi_target(session, user_id=USER_ID)
print(f"AFTER : spend {after.permanent_annual_spend_nis:,.0f} "
      f"-> FI {after.fi_perpetuity_nis:,.0f} "
      f"(+ reserve {after.finite_liability_reserve_nis:,.0f} "
      f"= {after.fi_total_capital_nis:,.0f})")
print()
print("ITEMISED (must still reconcile to the published basis):")
print(after.itemized_spend_derivation())
