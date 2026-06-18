"""The five Phase-1c persistence tables exist on Base.metadata with the
columns the spec's Data-model section names. We assert columns directly off
the mapper so the test is independent of any migration running."""
from argosy.state.models import (
    PlanNode, PlanEdge, ChangeRequest, DialogueTurn, PropagationEvent,
)


def _cols(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


def test_plan_nodes_columns():
    assert PlanNode.__tablename__ == "plan_nodes"
    assert _cols(PlanNode) >= {
        "id", "plan_id", "node_key", "kind", "value_json", "content",
        "input_hash", "status_validity", "status_flag", "provenance_json",
        "owner", "compute_version", "created_at",
    }


def test_plan_edges_columns():
    assert PlanEdge.__tablename__ == "plan_edges"
    assert _cols(PlanEdge) >= {
        "id", "plan_id", "from_node_key", "to_node_key", "edge_kind", "created_at",
    }


def test_change_requests_columns():
    assert ChangeRequest.__tablename__ == "change_requests"
    assert _cols(ChangeRequest) >= {
        "id", "plan_id", "target_node_key", "author", "kind", "payload_json",
        "rationale", "status", "round_count", "adjudicated_by",
        "terminal_reason", "created_at", "updated_at",
    }


def test_dialogue_turns_columns():
    assert DialogueTurn.__tablename__ == "dialogue_turns"
    assert _cols(DialogueTurn) >= {
        "id", "change_request_id", "round", "speaker", "stance", "text",
        "cited_nodes_json", "created_at",
    }


def test_propagation_events_columns():
    assert PropagationEvent.__tablename__ == "propagation_events"
    assert _cols(PropagationEvent) >= {
        "id", "plan_id", "cycle_id", "trigger_node_key",
        "invalidated_node_keys_json", "recomputed_json",
        "rerendered_surfaces_json", "verification_verdicts_json", "created_at",
    }
