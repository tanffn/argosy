"""Apply proposal 71 (Ariel approved Option B, 2026-07-10): split the 5%
high-growth sleeve into a 2% x10-moonshot lane + 3% market-beating-alpha lane.

Mechanics mirror the proposal-67 apply: author a DRAFT plan version derived
from current (v74) with ONLY the high-growth class changed (rationale carries
the two-lane mandate — the funnel reads cls.rationale via sleeve_mandate.py —
plus a structured ``lanes`` list per validate-structured-objects), then
promote through the gated POST /api/plan/draft/{id}/accept, then flip row 71
to executed. Sleeve total stays 5.0%; every other class byte-identical.

Durable side-effects BEFORE prints (cp1252 console).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGOSY_HOME", str(ROOT))
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import urllib.request

USER_ID = "ariel"

ALPHA_LANE_MANDATE = (
    "MARKET-BEATING ALPHA lane (3.0% of the tradeable book): names whose "
    "EXPECTED return beats the market core, admitted by a written "
    "outperformance memo quantified against CSPX and a must-justify test "
    "versus simply holding more IWQU (no lazy quality-compounder overlap). "
    "Cap ceiling $100B. 2-3x cyclical asymmetries (RKT-class) and quality "
    "compounders at fair prices (NOW-class) are admissible; slots ~0.5-1% "
    "with recorded exit_triggers + review_on dates. Verdicts ship with "
    "conviction + falsifiers and are DEFENDED under pushback (client "
    "directive 2026-07-10)."
)


def main() -> None:
    from argosy.state.models import ActionProposal, PlanVersion

    engine = sa.create_engine(f"sqlite:///{ROOT / 'db' / 'argosy.db'}")
    session = sessionmaker(bind=engine)()

    v74 = session.execute(
        sa.select(PlanVersion).where(
            PlanVersion.user_id == USER_ID, PlanVersion.role == "current"
        )
    ).scalar_one()
    assert v74.id == 74, f"expected current plan v74, found v{v74.id} - abort"

    ta = json.loads(v74.target_allocation_json)
    hg = next(
        c for c in ta["classes"] if "high-growth" in (c.get("label") or "").lower()
    )
    assert float(hg["target_pct"]) == 5.0, hg["target_pct"]
    prior_mandate = (hg.get("rationale") or "").strip()

    hg["lanes"] = [
        {
            "name": "x10_moonshot",
            "target_pct": 2.0,
            "mandate": prior_mandate
            or "x10-ASYMMETRY: sub-$30B, credible ~10x cap-math path, "
            "accepted 100% loss, thesis-milestone exit triggers.",
        },
        {
            "name": "market_beating_alpha",
            "target_pct": 3.0,
            "mandate": ALPHA_LANE_MANDATE,
        },
    ]
    hg["rationale"] = (
        "TWO-LANE SLEEVE (proposal 71 applied 2026-07-10, Ariel chose the "
        "split; total 5.0% unchanged).\n\n"
        "LANE 1 - x10 MOONSHOT (2.0% of the tradeable book): "
        + (
            prior_mandate
            or "sub-$30B names with a credible ~10x cap-math path; a 100% "
            "loss is accepted; exit triggers come from the thesis's own "
            "milestones."
        )
        + "\n\nLANE 2 - " + ALPHA_LANE_MANDATE
        + "\n\nFalsifiers (recorded on proposal 71): sleeve <50% funded 90 "
        "days post-split -> the pipeline was the constraint; 12-18mo "
        "alpha-lane excess vs CSPX <=0 -> fold the 3% back into core/IWQU; "
        ">=3x moonshot winner with sizing regret -> adjust the 2/3 ratio, "
        "never revert wholesale."
    )

    draft = PlanVersion(
        user_id=USER_ID,
        role="draft",
        version_label="mandate-split-2026-07-10",
        source_path="",
        raw_markdown="",
        imported_at=datetime.now(UTC),
        derived_from_id=v74.id,
        horizon_long_json=v74.horizon_long_json,
        horizon_medium_json=v74.horizon_medium_json,
        horizon_short_json=v74.horizon_short_json,
        horizon_long_md=v74.horizon_long_md,
        horizon_medium_md=v74.horizon_medium_md,
        horizon_short_md=v74.horizon_short_md,
        horizon_long_md_audit=v74.horizon_long_md_audit,
        horizon_medium_md_audit=v74.horizon_medium_md_audit,
        horizon_short_md_audit=v74.horizon_short_md_audit,
        narrative_json=v74.narrative_json,
        sections_json=v74.sections_json,
        target_allocation_json=json.dumps(ta, ensure_ascii=False),
        target_allocation_overrides_json=v74.target_allocation_overrides_json,
    )
    session.add(draft)
    session.commit()
    draft_id = draft.id
    print(f"draft v{draft_id} created (derived from v74)")

    req = urllib.request.Request(
        f"http://127.0.0.1:8000/api/plan/draft/{draft_id}/accept?user_id={USER_ID}",
        method="POST",
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read().decode("utf-8", "replace")
        print("accept:", resp.status, body[:400])
        assert resp.status == 200

    now = datetime.now(UTC)
    row71 = session.get(ActionProposal, 71)
    row71.status = "executed"
    row71.decided_at = now
    row71.execution_state = "accepted_pending_user_action"
    row71.decided_by_user_note = (
        f"Ariel chose Option B (2026-07-10); applied as plan v{draft_id}: "
        "high-growth sleeve split into 2.0% x10-moonshot + 3.0% "
        "market-beating-alpha lanes (total 5.0% unchanged), mandate + "
        "structured lanes written into the class the funnel reads; "
        "falsifiers carried into the class rationale."
    )
    session.commit()
    print(f"proposal 71 executed; plan v{draft_id} promoted")


if __name__ == "__main__":
    main()
