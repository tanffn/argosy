"""Functional smoke harness for the plan-refinement decision core.

Hydrates the live derivation graph for user_id='ariel' and runs four
scenarios through run_refinement(), printing a readable trace for each.
"""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

# Enable incremental-plan flag so build_base_graph doesn't raise.
os.environ["ARGOSY_INCREMENTAL_PLAN"] = "1"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings
from argosy.orchestrator.flows.incremental_plan import build_base_graph
from argosy.quality.blast_radius import ChangeRequest, Tier
from argosy.quality.derivation_graph import Node, NodeKind
from argosy.quality.refinement import run_refinement, summary

# ---------------------------------------------------------------------------
# 1. Hydrate the LIVE graph
# ---------------------------------------------------------------------------

USER_ID = "ariel"
DECISION_RUN_ID = 121  # latest decision run (verified: SELECT MAX(id) FROM decision_runs WHERE user_id='ariel')

_url = str(get_settings().database_url).replace("+aiosqlite", "")
_factory = sessionmaker(
    bind=create_engine(_url, connect_args={"check_same_thread": False}),
    expire_on_commit=False,
)

print("=" * 70)
print("ARGOSY REFINEMENT SMOKE HARNESS")
print("=" * 70)
print(f"User: {USER_ID}  decision_run_id={DECISION_RUN_ID}")
print()

with _factory() as _session:
    graph = build_base_graph(_session, USER_ID, decision_run_id=DECISION_RUN_ID)

_input_keys = [k for k in graph.keys() if graph.get(k).kind == NodeKind.INPUT]
_all_keys = graph.keys()

print(f"LIVE GRAPH HYDRATED via build_base_graph (incremental_plan.build_base_graph)")
print(f"  Total nodes: {len(_all_keys)}")
print(f"  INPUT nodes ({len(_input_keys)}): {sorted(_input_keys)}")
print()

# Add retirement.risk_posture as a synthetic INPUT so scenario (b) has a node to target.
# (The live graph has scalar resolver seeds only; risk_posture is a plan-identity policy
# key not yet modelled as a graph node — we inject it so the classifier can see it.)
if "retirement.risk_posture" not in graph.keys():
    graph.add_node(Node(
        key="retirement.risk_posture",
        kind=NodeKind.INPUT,
        value="balanced_growth",
    ))
    print("  [NOTE] 'retirement.risk_posture' not in live graph — injected as INPUT seed"
          " so scenario (b) has a target (policy key, not yet a graph node).")
    print()

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run(name: str, change_requests, *, post_doc=None) -> None:
    print(f"--- {name} ---")
    try:
        decision = run_refinement(graph, change_requests, post_doc=post_doc)
        br = decision.blast_radius
        inv = decision.invariant_report
        print(f"  summary      : {summary(decision)}")
        print(f"  tier         : {decision.tier.value}")
        print(f"  reason       : {decision.reason}")
        print(f"  dirtied_count: {len(br.dirtied_keys)}")
        print(f"  owner_domains: {sorted(br.owner_domains)}")
        print(f"  invariant.ok : {inv.ok if inv is not None else 'N/A (no post_doc)'}")
        print(f"  forced_by_inv: {decision.forced_by_invariant}")
        if inv is not None and not inv.ok:
            codes = [v.code for v in inv.violations]
            print(f"  inv_violations: {codes}")
    except Exception as exc:
        print(f"  ERROR: {exc}")
    print()


# ---------------------------------------------------------------------------
# Scenario (a) SCALAR change — tax.retention_at_vest_pct with supplies_value=True
# Expected tier: SCOPED_EDIT (T0)
# Why: tax.retention_at_vest_pct is synthesis_authored with owner=withdrawal_sequencer
# (a wired agent), so _detect_missing_owner=False. It only dirties one surface node
# (surface:retention_at_vest_statement, no plan_basis hard verdict, single owner
# domain, fraction 1/31 << 0.34 threshold) → falls through to T0 SCOPED_EDIT.
# Note: fx.usd_nis would be a natural candidate but it dirties concentration.*
# nodes with plan_basis hard verdict severity (concentration.nvda_current_pct
# etc.) → classifier fires "flips a plan-basis hard verdict" → FULL_REBUILD.
# ---------------------------------------------------------------------------
_run(
    "Scenario (a): SCALAR tax.retention_at_vest_pct supplies_value=True [expect SCOPED_EDIT]",
    [ChangeRequest(node_key="tax.retention_at_vest_pct", new_value=0.55, supplies_value=True)],
)

# ---------------------------------------------------------------------------
# Scenario (b) PLAN-IDENTITY — retirement.risk_posture
# Expected tier: FULL_REBUILD (changes_plan_identity_axis=True)
# ---------------------------------------------------------------------------
_run(
    "Scenario (b): PLAN-IDENTITY retirement.risk_posture [expect FULL_REBUILD]",
    [ChangeRequest(node_key="retirement.risk_posture", new_value="conservative", supplies_value=True)],
)

# ---------------------------------------------------------------------------
# Scenario (c) INVARIANT-NET — perturbed post_doc breaks allocation_sum
# We bump the first class target_pct by +5 so total > 100 + 0.5pp tolerance.
# Expected tier: FULL_REBUILD with forced_by_invariant=True.
# ---------------------------------------------------------------------------
from argosy.services.target_allocation_doc import load_plan_target_allocation
from argosy.state.queries import get_current_plan

with _factory() as _s2:
    _pv = get_current_plan(_s2, USER_ID)
    _doc_pre = load_plan_target_allocation(_pv)

if _doc_pre is None:
    print("--- Scenario (c): INVARIANT-NET [SKIPPED — no current plan doc] ---")
    print()
else:
    # Deep-copy, perturb: bump first class target_pct by +5pp → sum ≈ 104.99%
    import copy
    _doc_post = _doc_pre.model_copy(deep=True)
    _doc_post.classes[0].target_pct += 5.0
    _sum_pre = round(sum(c.target_pct for c in _doc_pre.classes), 4)
    _sum_post = round(sum(c.target_pct for c in _doc_post.classes), 4)
    print(f"  [scenario c] pre doc alloc sum={_sum_pre}%  post doc alloc sum={_sum_post}%")
    print()
    _run(
        "Scenario (c): INVARIANT-NET alloc_sum breach [expect FULL_REBUILD forced_by_invariant=True]",
        [ChangeRequest(node_key="fx.usd_nis", new_value=3.10, supplies_value=True)],
        post_doc=_doc_post,
    )

# ---------------------------------------------------------------------------
# Scenario (d) UNSUPPLIED figure — portfolio.liquid_net_worth_nis supplies_value=False
# authoring_mode=owner_authored (portfolio. prefix); supplies_value=False →
# _detect_missing_owner=True → FULL_REBUILD.
# Expected tier: FULL_REBUILD
# ---------------------------------------------------------------------------
_run(
    "Scenario (d): UNSUPPLIED owner_authored portfolio.liquid_net_worth_nis [expect FULL_REBUILD]",
    [ChangeRequest(node_key="portfolio.liquid_net_worth_nis", new_value=None, supplies_value=False)],
)

# ---------------------------------------------------------------------------
# Note on pending scenarios
# ---------------------------------------------------------------------------
print(
    "NOTE: ALLOCATION/SLEEVE-tweak scenarios (spec §7 #2/#3) are PENDING "
    "allocation-in-graph (not yet modelled — the 'allocation' topic-owner "
    "agent stub exists in plan_node_meta._LOCAL_OWNER_EXTENSIONS but has no "
    "wired agent; any sleeve ChangeRequest currently escalates to FULL_REBUILD "
    "via missing_owner_for_changed_node)."
)
