"""Apply proposal 69 (Ariel approved 2026-07-10): the discovery dry-powder
earmark becomes a durable annotation on the plan's cash class.

Mirrors the proposal-71 apply: draft derived from current (v75) with ONLY the
cash class changed — a structured ``discovery_reserve`` block + rationale
note — promoted through the gated accept. 1.5% of book (~$59.5k) stays in
cash-equivalents (held SGOV today; IB01 for new parking), instantly
deployable for green-lit discovery buys, replenished first from staged-sell
inflows. Sleeve target 5.68% unchanged. Also flips proposal 68 (gate
widening) to executed — its code change ships in the same commit.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

USER_ID = "ariel"


def main() -> None:
    from argosy.state.models import ActionProposal, PlanVersion

    engine = sa.create_engine(f"sqlite:///{ROOT / 'db' / 'argosy.db'}")
    session = sessionmaker(bind=engine)()

    cur = session.execute(
        sa.select(PlanVersion).where(
            PlanVersion.user_id == USER_ID, PlanVersion.role == "current"
        )
    ).scalar_one()
    assert cur.id == 75, f"expected current v75, found v{cur.id} - abort"

    p69 = session.get(ActionProposal, 69)
    payload69 = json.loads(p69.suggested_payload or "{}")
    reserve_pct = 1.5
    reserve_usd = payload69.get("reserve_usd_at_current_book")
    assert isinstance(reserve_usd, (int, float)) and reserve_usd > 0

    ta = json.loads(cur.target_allocation_json)
    cash = next(c for c in ta["classes"] if "cash" in (c.get("label") or "").lower())
    cash["discovery_reserve"] = {
        "pct_of_book": reserve_pct,
        "usd_at_apply": round(reserve_usd),
        "instruments": ["SGOV (held)", "IB01 (new parking)"],
        "rule": "cash-or-cash-equivalent ONLY; instantly deployable for "
        "green-lit discovery/sleeve buys; never parked in anything that can "
        "fall or takes days to unwind; replenished FIRST from staged-sell "
        "inflows, then paycheck cash, ahead of discretionary top-ups",
        "source_proposal": 69,
        "applied_at": datetime.now(UTC).strftime("%Y-%m-%d"),
    }
    cash["rationale"] = (
        (cash.get("rationale") or "").rstrip()
        + f"\n\nDISCOVERY DRY-POWDER EARMARK (proposal 69, applied "
        f"2026-07-10): {reserve_pct}% of book (~${reserve_usd:,.0f}) of this "
        f"sleeve is earmarked as instantly-deployable reserve for green-lit "
        f"discovery/sleeve buys — cash or T-bill-class only (held SGOV / "
        f"IB01), replenished first from staged-sell inflows. Deployment "
        f"tooling must not treat the earmarked slice as idle/deployable "
        f"general cash."
    ).strip()

    draft = PlanVersion(
        user_id=USER_ID,
        role="draft",
        version_label="dry-powder-earmark-2026-07-10",
        source_path="",
        raw_markdown="",
        imported_at=datetime.now(UTC),
        derived_from_id=cur.id,
        horizon_long_json=cur.horizon_long_json,
        horizon_medium_json=cur.horizon_medium_json,
        horizon_short_json=cur.horizon_short_json,
        horizon_long_md=cur.horizon_long_md,
        horizon_medium_md=cur.horizon_medium_md,
        horizon_short_md=cur.horizon_short_md,
        horizon_long_md_audit=cur.horizon_long_md_audit,
        horizon_medium_md_audit=cur.horizon_medium_md_audit,
        horizon_short_md_audit=cur.horizon_short_md_audit,
        narrative_json=cur.narrative_json,
        sections_json=cur.sections_json,
        target_allocation_json=json.dumps(ta, ensure_ascii=False),
        target_allocation_overrides_json=cur.target_allocation_overrides_json,
    )
    session.add(draft)
    session.commit()
    print(f"draft v{draft.id} created (derived from v{cur.id})")

    req = urllib.request.Request(
        f"http://127.0.0.1:8000/api/plan/draft/{draft.id}/accept?user_id={USER_ID}",
        method="POST",
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        print("accept:", resp.status, resp.read().decode("utf-8", "replace")[:300])
        assert resp.status == 200

    now = datetime.now(UTC)
    p69.status = "executed"
    p69.decided_at = now
    p69.execution_state = "accepted_pending_user_action"
    p69.decided_by_user_note = (
        f"Ariel approved 2026-07-10; applied as plan v{draft.id}: "
        f"discovery_reserve block ({reserve_pct}% ~= ${reserve_usd:,.0f}, "
        "cash/T-bill-class only) on the cash class + rationale note the "
        "deployment tooling reads. No purchase needed - labels held SGOV."
    )
    p68 = session.get(ActionProposal, 68)
    p68.status = "executed"
    p68.decided_at = now
    p68.execution_state = "accepted_pending_user_action"
    p68.decided_by_user_note = (
        "Ariel approved 2026-07-10; applied in code: "
        "discovery_conviction_floor HIGH->MEDIUM "
        "(decision_funnel/policy.py) + radar DEFAULT_CAP_MAX $8B->$30B "
        "(trend_radar.py), tests updated (MEDIUM default pinned; explicit "
        "HIGH floor still restores strict routing)."
    )
    session.commit()
    print(f"proposals 68+69 executed; plan v{draft.id} promoted")


if __name__ == "__main__":
    main()
