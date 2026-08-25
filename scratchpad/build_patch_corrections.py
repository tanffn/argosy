"""Build structured corrections for plan 122 and classify BEFORE launching.

The classifier is strict FULL-first and bounded: one unaddressable correction,
or a union spread over more than MAX_IMPLICATED_GROUPS (2) of the four slice
groups, sends the whole run down the full path.

So: no plan_item_ref (an unresolvable ref forces FULL outright — the no-ref
form falls through to occurrence widening instead), and every wrong_value is a
literal verified to occur in plan 122's rendered surfaces.
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
from sqlalchemy.orm import sessionmaker  # noqa: E402

from argosy.orchestrator.flows.plan_synthesis.orchestrator import (  # noqa: E402
    _load_patch_base_output,
)
from argosy.quality.patch_reachability import (  # noqa: E402
    classify_patch_reachability,
)
from argosy.state.models import PlanVersion  # noqa: E402

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine)()
pv = session.get(PlanVersion, 122)
prior = _load_patch_base_output(pv)

rendered = {
    "long": pv.horizon_long_md or "",
    "medium": pv.horizon_medium_md or "",
    "short": pv.horizon_short_md or "",
}


def corr(index, topic, canonical, wrong, statement):
    return {
        "index": index,
        "severity": "BLOCKER",
        "topic": topic,
        "plan_item_ref": "",          # deliberately empty — see module docstring
        "summary": statement,
        "evidence": [],
        "reconcile_status": "unresolved",
        "canonical_facts": canonical,
        "wrong_values": wrong,
        "required_statement": statement,
        "source": "codex_phase_45",
        "verdict_agent": "codex",
        "source_run_id": 456,
        "source_draft_id": 122,
        "parsed_ref": None,
    }


# Each entry: (label, correction). Classified individually first so the
# per-correction spread is visible, then in combinations.
CANDIDATES = [
    ("FI headline set", corr(
        1, "[C1] FI headline set superseded by the settled basis",
        [["retirement.fi_target_nis", "10,000,000"],
         ["retirement.fi_total_capital_nis", "11,450,000"],
         ["spend.fi_basis_nis", "300,000"]],
        ["10,386,133", "11,836,133", "311,584"],
        "Publish the settled FI basis and perpetuity from their tokens only.",
    )),
    ("FI margin", corr(
        2, "[C1] Capital-sufficiency margin diverges from the canonical",
        [["retirement.fi_margin_signed_nis", "697,318"]],
        ["311,185"],
        "Publish the canonical signed margin from its token only.",
    )),
    ("vest tax", corr(
        3, "[AMBER] Vest tax overstated and mislabelled",
        [["tax.sept_vest_deferred_usd", "36,063.70"]],
        ["48,278.73"],
        "Publish the TaxAnalyst figure and state it is DEFERRED disposal "
        "liability, not at-vest withholding; the action is a verification.",
    )),
    ("FIRE bridge", corr(
        4, "[C2] FIRE bridge published with two different value pairs",
        [["retirement.fire_bridge_nis", "1,800,000"],
         ["retirement.fire_bridge_offmandate_nis", "4,500,000"]],
        ["1,557,920", "4,362,176"],
        "Publish both bridge values from their tokens, together, once.",
    )),
    ("stray cap", corr(
        5, "[C2] Stray non-governing cap literal",
        [["concentration.nvda_cap_pct", "12.0"]],
        ["13.0"],
        "The governing ceiling is the token; delete stray cap literals.",
    )),
]

print("=== per-correction classification ===")
for label, c in CANDIDATES:
    r = classify_patch_reachability(
        corrections=[c], directives=[], prior=prior,
        rendered_surfaces=rendered,
    )
    d = r.decisions[0] if r.decisions else None
    groups = ", ".join(d.implicated_groups) if d else "-"
    print(f"  {label:18} {r.verdict:14} groups=[{groups}]")
    if r.verdict != "PATCH":
        print(f"        reason: {r.reason[:150]}")

print("\n=== combinations (union spread must stay <= 2 groups) ===")
import itertools

best = None
for n in range(len(CANDIDATES), 0, -1):
    for combo in itertools.combinations(CANDIDATES, n):
        r = classify_patch_reachability(
            corrections=[c for _, c in combo], directives=[], prior=prior,
            rendered_surfaces=rendered,
        )
        if r.verdict == "PATCH":
            best = (combo, r)
            break
    if best:
        break

if best:
    combo, r = best
    print(f"  LARGEST PATCHABLE SET ({len(combo)} corrections): "
          f"{', '.join(l for l, _ in combo)}")
    print(f"  verdict: {r.verdict}")
    print(f"  reason : {r.reason}")
else:
    print("  no combination classifies as PATCH")
