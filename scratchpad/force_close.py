"""Force-close draft 125 as role='current'. Owner-authorised.

Ariel, 2026-08-24: "you can force a plan as it is. I don't care! we need to
CLOSE THIS!"

This is a FORCED promotion, not an approval. The distinction is recorded on the
plan so nobody — including a future me — mistakes one for the other.

Preconditions met before running (Sol's conditions, sol_close_it.md):
  * uses run 470's final draft (125), never the fallback 124;
  * the four estate defects struck deterministically (no LLM);
  * LEAKAGE CLEAN on the assembled artifact — never overridden;
  * override record persisted with authorities, reasons and execution holds.

Overridden, each inspected and judged document-hygiene rather than a money
error:
  * codex BLOCK / whole_artifact_reader BLOCK / fund_manager rejected;
  * fi_fx_shock_sufficiency — same-sentence FX-qualifier matcher; eight
    phrasings already qualified, it surfaces a new variant each pass;
  * event_currency_consistency — a tax event denominated in both NIS and USD;
  * ips_allocation_sum — structured medium IPS declares 2 sleeves (17.1%).

NOT overridden: leakage. It passes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

DRAFT_ID = 125
USER_ID = "ariel"

engine = sa.create_engine(f"sqlite:///{(ROOT / 'db' / 'argosy.db').as_posix()}")
session = sessionmaker(bind=engine, expire_on_commit=False)()

from argosy.state.models import PlanVersion  # noqa: E402

pv = session.get(PlanVersion, DRAFT_ID)
if pv is None or pv.user_id != USER_ID:
    raise SystemExit(f"draft {DRAFT_ID} missing/cross-tenant")

COLS = [
    "horizon_long_md", "horizon_medium_md", "horizon_short_md",
    "sections_json", "narrative_json", "target_allocation_json",
]
body = "".join(getattr(pv, c, "") or "" for c in COLS)
sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

prior_current = session.execute(
    sa.select(PlanVersion.id).where(
        PlanVersion.user_id == USER_ID, PlanVersion.role == "current"
    )
).scalar()

override = {
    "acceptance_override": {
        "forced": True,
        "is_approval": False,
        "authorised_by": "ariel",
        "authorisation_quote": (
            "you can force a plan as it is. I don't care! we need to CLOSE THIS!"
        ),
        "authorised_at_utc": datetime.now(timezone.utc).isoformat(),
        "draft_id": DRAFT_ID,
        "decision_run_id": pv.decision_run_id,
        "artifact_sha256": sha,
        "prior_current_plan_id": prior_current,
        "authorities_overridden": {
            "codex_phase_45": "BLOCK — findings judged document-consistency, not money errors",
            "whole_artifact_reader_phase_55": "BLOCK — same class",
            "fund_manager": "rejected — lead reason was the cap divergence, since resolved to the settled 13.0",
            "deterministic_gate": [
                "fi_fx_shock_sufficiency — same-sentence FX-qualifier matcher; "
                "8 phrasings qualified, a new variant surfaces each pass",
                "event_currency_consistency — a tax event denominated in both NIS and USD",
                "ips_allocation_sum — structured medium IPS declares 2 sleeves (17.1%)",
            ],
        },
        "not_overridden": {
            "leakage": "CLEAN on the assembled artifact — never overridden",
        },
        "deterministic_fixes_applied": [
            "struck stale US-situs subtotal $3,029.1k and derived ~$1.19M tax estimate; "
            "publish only {{fact:concentration.us_situs_estate_exposure_nis}} "
            "(canonical ILS 9,127,060; AVUV/REET/VHT are US-domiciled and already included)",
            "removed untraceable NIS-native literal ILS 68,403",
            "qualified 8 affirmative FI-sufficiency claims with the current USD/NIS mark",
            "canonical FI verdict string fixed at source in plan_numeric_resolver",
            "assumption A13 superseded FI basis ILS 311,584 -> ILS 300,000",
            "target_allocation_doc.nvda_cap_pct 12.0 -> 13.0 (settled adjudication)",
        ],
        "execution_holds": [
            "NO NVDA order until the trustee/Schwab lot ledger is refreshed with per-lot cost basis",
            "NO NVDA tranche until a deterministic share cap is published",
            "the counsel brief must use the domicile-classified canonical estate set, not the plan's prose",
            "moonshot buys remain subject to the allocation verifier",
            "SGOV disposes into IB01, never IBTA",
        ],
        "rollback": f"atomic rollback to plan {prior_current} via the plan rollback route",
    }
}

try:
    existing = json.loads(pv.synthesis_inputs_json or "{}")
except (ValueError, TypeError):
    existing = {}
if not isinstance(existing, dict):
    existing = {}
existing.update(override)
pv.synthesis_inputs_json = json.dumps(existing, ensure_ascii=False)
session.commit()

print("=== OVERRIDE RECORD PERSISTED ===")
print(f"  draft            : {DRAFT_ID}")
print(f"  artifact sha256  : {sha[:32]}...")
print(f"  prior current    : plan {prior_current}")
print(f"  forced           : True   (is_approval: False)")
print(f"  leakage          : NOT overridden (clean)")
print(f"  execution holds  : {len(override['acceptance_override']['execution_holds'])}")

session.expire_all()
pv = session.get(PlanVersion, DRAFT_ID)
rec = json.loads(pv.synthesis_inputs_json).get("acceptance_override", {})
print()
print("=== VERIFY ===")
print(f"  record readable  : {bool(rec)}")
print(f"  sha matches      : {rec.get('artifact_sha256') == sha}")
print(f"  role (unchanged) : {pv.role}")
