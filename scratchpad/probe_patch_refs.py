"""Probe plan 122 for addressable refs + locatable values.

The patch classifier is strict FULL-first: one unaddressable correction sends
the whole run down the full path. So build the corrections against what the
prior draft ACTUALLY contains, and verify before launching.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from argosy.orchestrator.flows.plan_synthesis.orchestrator import (  # noqa: E402
    _load_patch_base_output,
)
from argosy.state.models import PlanVersion  # noqa: E402

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine)()

pv = session.get(PlanVersion, 122)
prior = _load_patch_base_output(pv)
if prior is None:
    raise SystemExit("plan 122 has no structured artifact — patch impossible")

print("=== SECTIONS (section_id, horizon) ===")
for s in prior.sections:
    print(f"   {s.section_id:26} {s.horizon}")

print("\n=== ITEMS per horizon (targets / themes / actions) ===")
for h in ("long", "medium", "short"):
    hz = getattr(prior, h)
    for kind in ("targets", "themes", "actions"):
        for it in getattr(hz, kind, []) or []:
            label = getattr(it, "label", None)
            if label:
                print(f"   {h}.{kind[:-1]}.{label[:52]}")

# What text can the classifier search for occurrences?
blob = " ".join(filter(None, [
    pv.horizon_long_md, pv.horizon_medium_md, pv.horizon_short_md,
    pv.sections_json, pv.narrative_json,
]))

CANDIDATES = [
    "10,386,133", "11,836,133", "311,584", "311,185",
    "54", "45", "55", "46",
    "48,278.73", "48,278", "36,063.70", "36,063",
    "1,557,920", "1,800,000", "4,362,176", "4,500,000",
    "12.0", "13.0", "8,918", "1,462", "10,940", "10,380",
    "264,000", "277,008", "300,000", "10,000,000",
]
print("\n=== VALUE OCCURRENCES in plan 122 rendered surfaces ===")
for v in CANDIDATES:
    n = len(re.findall(re.escape(v), blob))
    print(f"   {v:14} {'HIT x%d' % n if n else '-- absent'}")
