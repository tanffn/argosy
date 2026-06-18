import json
from datetime import datetime, timezone

from argosy.state.models import ChangeRequestRow, DialogueTurnRow, PlanVersion


SEED_USER = "test"  # db_session_with_seeded_user seeds User(id="test")


def _seed_plan(s):
    pv = PlanVersion(user_id=SEED_USER, version_label="t", role="current")
    s.add(pv)
    s.commit()
    s.refresh(pv)
    return pv.id


def test_change_request_row_roundtrip(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    row = ChangeRequestRow(
        plan_id=plan_id,
        target_node_key="swr_pct",
        author="agent:plan_critique",
        kind="set_recipe",
        payload_json=json.dumps({"value": 0.04}),
        rationale="over-conservative",
        status="proposed",
        round_count=0,
        created_at=datetime.now(timezone.utc),
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    assert row.id is not None
    assert row.status == "proposed"


def test_dialogue_turn_row_links_to_change_request(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    cr = ChangeRequestRow(
        plan_id=plan_id, target_node_key="swr_pct", author="user",
        kind="set_recipe", payload_json="{}", rationale="", status="in_dialogue",
        round_count=1, created_at=datetime.now(timezone.utc),
    )
    s.add(cr)
    s.commit()
    s.refresh(cr)
    turn = DialogueTurnRow(
        change_request_id=cr.id, round=1, speaker="A", stance="propose",
        text="change swr_pct", cited_nodes_json=json.dumps(["swr_pct"]),
        created_at=datetime.now(timezone.utc),
    )
    s.add(turn)
    s.commit()
    s.refresh(turn)
    assert turn.change_request_id == cr.id
    assert turn.speaker == "A"
