"""Apply the guidance-flip convention to the growth-sleeve exit discipline
(plan v78). Adoption condition met per docs/design/fleet_calibration_benchmark.md:
the TTCF T2 HOLD miss REPRODUCED against the corrected packet in the full
suite run (2026-07-11) — second confirming case. Same draft->gated-accept
pattern as scripts/apply_no_price_exit_rule.py.
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

RULE = (
    "\n\nGUIDANCE-FLIP CONVENTION (adopted 2026-07-11 from the calibration "
    "benchmark; TTCF-class lesson, confirmed twice): when management FLIPS "
    "guidance on a recorded profitability/margin milestone (e.g. withdraws "
    "or reverses a stated path to the thesis's margin pillar), that counts "
    "as the falsifier FIRED - not 'bent, one more print'. The default "
    "action is exit/trim per the thesis, announced as a falsifier exit. "
    "Granting an extra quarter requires an explicit re-derivation showing "
    "why the flip does not break the recorded pillar."
)


def main() -> None:
    from argosy.state.models import PlanVersion

    engine = sa.create_engine(f"sqlite:///{ROOT / 'db' / 'argosy.db'}")
    session = sessionmaker(bind=engine)()
    cur = session.execute(
        sa.select(PlanVersion).where(
            PlanVersion.user_id == USER_ID, PlanVersion.role == "current"
        )
    ).scalar_one()
    assert cur.id == 77, f"expected current v77, found v{cur.id} - abort"

    ta = json.loads(cur.target_allocation_json)
    hg = next(
        c for c in ta["classes"] if "high-growth" in (c.get("label") or "").lower()
    )
    assert "GUIDANCE-FLIP" not in (hg.get("rationale") or ""), "already applied"
    hg["rationale"] = (hg.get("rationale") or "").rstrip() + RULE
    for lane in hg.get("lanes", []):
        lane["guidance_flip_fires_falsifier"] = True

    draft = PlanVersion(
        user_id=USER_ID,
        role="draft",
        version_label="guidance-flip-convention-2026-07-11",
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
    print(f"draft v{draft.id} created")

    req = urllib.request.Request(
        f"http://127.0.0.1:8000/api/plan/draft/{draft.id}/accept?user_id={USER_ID}",
        method="POST",
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        print("accept:", resp.status, resp.read().decode("utf-8", "replace")[:250])
        assert resp.status == 200
    print(f"plan v{draft.id} promoted with the guidance-flip convention")


if __name__ == "__main__":
    main()
