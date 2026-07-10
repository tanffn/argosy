"""Apply Ariel's anti-early-exit directive (2026-07-10, the PLTR scar):
price appreciation alone is NEVER an exit trigger in the growth sleeve.

Small mandate addendum on the high-growth class (both lanes read it via
sleeve_mandate.py) -> draft v77 -> gated accept. Same pattern as
scripts/apply_growth_sleeve_split.py.
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
    "\n\nEXIT DISCIPLINE (client directive 2026-07-10 — the PLTR lesson: "
    "held at $8, sold at $16 on price, missed the 10x+): PRICE APPRECIATION "
    "ALONE IS NEVER AN EXIT TRIGGER in this sleeve, in either lane. A "
    "position doubling triggers a thesis RE-DERIVATION (did a milestone "
    "land? has the cap-math ceiling moved?), never an automatic trim. The "
    "only sanctioned trims: (a) the position's recorded thesis falsifier "
    "fires, or (b) the sleeve breaches its plan cap — then rebalance "
    "mechanically back to cap while KEEPING the position. Slot sizing "
    "(~0.5-1%, accepted 100% loss) already does the risk work; "
    "profit-taking is never needed for safety."
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
    assert cur.id == 76, f"expected current v76, found v{cur.id} - abort"

    ta = json.loads(cur.target_allocation_json)
    hg = next(
        c for c in ta["classes"] if "high-growth" in (c.get("label") or "").lower()
    )
    assert "EXIT DISCIPLINE" not in (hg.get("rationale") or ""), "already applied"
    hg["rationale"] = (hg.get("rationale") or "").rstrip() + RULE
    for lane in hg.get("lanes", []):
        lane["no_price_exit_rule"] = True

    draft = PlanVersion(
        user_id=USER_ID,
        role="draft",
        version_label="exit-discipline-2026-07-10",
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
    print(f"plan v{draft.id} promoted with the no-price-exit rule")


if __name__ == "__main__":
    main()
