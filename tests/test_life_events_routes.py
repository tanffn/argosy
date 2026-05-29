"""Tests for the /api/life-events routes + loud-error contract.

Sprint commit #8 of the plan/execute/monitor reorg. Codex BLOCKER on
spec #1 §4.1: the 422 response must carry structured detail so the UI
can render a red-banner inline (not be swallowed by a generic error
boundary). Verified at the service layer here; the UI assertion test
lives in ui/__tests__/.
"""
from __future__ import annotations


def _ensure_user(client_with_db, user_id="ariel"):
    from argosy.state.models import User
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


VALID_PAYLOAD = {
    "user_id": "ariel",
    "category": "expense_event",
    "kind": "college",
    "target_date": "2032-09-01",
    "amount_usd": 200000,
    "description": "Kid #1 starts college",
}


class TestCatalog:
    def test_catalog_returns_all_six_categories(self, client_with_db):
        r = client_with_db.get("/api/life-events/catalog")
        assert r.status_code == 200
        body = r.json()
        assert set(body["categories"]) == {
            "career_event",
            "family_event",
            "asset_event",
            "expense_event",
            "recurring_expense",
            "retirement_milestone",
        }

    def test_catalog_kinds_per_category(self, client_with_db):
        r = client_with_db.get("/api/life-events/catalog")
        body = r.json()
        kbc = body["kinds_by_category"]
        # Spot-check a couple of the per-category enums.
        assert "college" in kbc["expense_event"]
        assert "marriage" in kbc["family_event"]
        assert "target_retire_year_change" in kbc["retirement_milestone"]
        # Cross-category check: 'college' is NOT a valid career_event kind.
        assert "college" not in kbc["career_event"]

    def test_catalog_field_rules_drive_ui_visibility(self, client_with_db):
        """Codex IMPORTANT #6: per-category field rules must be in the
        catalog so the UI doesn't hardcode which categories need which
        fields."""
        r = client_with_db.get("/api/life-events/catalog")
        body = r.json()
        rules = body["field_rules_by_category"]
        # asset_event + expense_event + recurring_expense need amount.
        assert rules["asset_event"]["requires_amount"] is True
        assert rules["expense_event"]["requires_amount"] is True
        assert rules["recurring_expense"]["requires_amount"] is True
        # career_event / family_event / retirement_milestone don't.
        assert rules["career_event"]["requires_amount"] is False
        assert rules["family_event"]["requires_amount"] is False
        assert rules["retirement_milestone"]["requires_amount"] is False
        # Only recurring_expense supports recurring_years.
        assert rules["recurring_expense"]["supports_recurring_years"] is True
        for cat in (
            "career_event",
            "family_event",
            "asset_event",
            "expense_event",
            "retirement_milestone",
        ):
            assert rules[cat]["supports_recurring_years"] is False


class TestCreate:
    def test_create_valid_event_returns_201(self, client_with_db):
        _ensure_user(client_with_db)
        r = client_with_db.post("/api/life-events", json=VALID_PAYLOAD)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["category"] == "expense_event"
        assert body["kind"] == "college"
        assert body["amount_usd"] == 200000
        assert body["id"] > 0

    def test_create_bad_category_returns_structured_422(self, client_with_db):
        """Loud-error: unknown category surfaces with a structured body
        the UI banner consumes."""
        _ensure_user(client_with_db)
        payload = {**VALID_PAYLOAD, "category": "not_a_real_category"}
        r = client_with_db.post("/api/life-events", json=payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "category_not_recognized"
        assert detail["input"] == "not_a_real_category"
        assert "expense_event" in detail["valid_categories"]

    def test_create_bad_kind_returns_structured_422(self, client_with_db):
        """Loud-error: kind that exists in some other category is still
        rejected for the chosen category."""
        _ensure_user(client_with_db)
        # 'college' is valid for expense_event but NOT for career_event.
        payload = {**VALID_PAYLOAD, "category": "career_event", "kind": "college"}
        r = client_with_db.post("/api/life-events", json=payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "kind_not_valid_for_category"
        assert detail["input"] == "college"
        # career_event's valid kinds should be in the alternatives.
        assert "job_change" in detail["valid_kinds"]


class TestList:
    def test_list_returns_inserted_events(self, client_with_db):
        _ensure_user(client_with_db)
        client_with_db.post("/api/life-events", json=VALID_PAYLOAD)
        r = client_with_db.get("/api/life-events?user_id=ariel")
        assert r.status_code == 200
        body = r.json()
        assert len(body["events"]) == 1
        assert body["events"][0]["kind"] == "college"

    def test_list_empty_for_unknown_user(self, client_with_db):
        r = client_with_db.get("/api/life-events?user_id=nobody")
        assert r.status_code == 200
        assert r.json()["events"] == []


class TestUpdate:
    def test_update_persists_changes(self, client_with_db):
        _ensure_user(client_with_db)
        created = client_with_db.post("/api/life-events", json=VALID_PAYLOAD).json()
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "amount_usd": 250000,
                "description": "Revised estimate",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["amount_usd"] == 250000
        assert body["description"] == "Revised estimate"


class TestDelete:
    def test_delete_removes_event(self, client_with_db):
        _ensure_user(client_with_db)
        created = client_with_db.post("/api/life-events", json=VALID_PAYLOAD).json()
        r = client_with_db.delete(
            f"/api/life-events/{created['id']}?user_id=ariel",
        )
        assert r.status_code == 204

        # List should now be empty.
        r2 = client_with_db.get("/api/life-events?user_id=ariel")
        assert r2.json()["events"] == []

    def test_delete_unknown_id_returns_404(self, client_with_db):
        r = client_with_db.delete("/api/life-events/9999?user_id=ariel")
        assert r.status_code == 404


class TestKindEnumPolicy:
    """Spec §4.2 — every category has a finite, server-defined kind
    enum. The catalog endpoint exposes it; the UI cannot expand it
    client-side without a backend change."""

    def test_all_six_categories_have_at_least_one_kind(self, client_with_db):
        r = client_with_db.get("/api/life-events/catalog")
        kbc = r.json()["kinds_by_category"]
        for cat in (
            "career_event", "family_event", "asset_event",
            "expense_event", "recurring_expense", "retirement_milestone",
        ):
            assert len(kbc[cat]) >= 1, (
                f"category {cat!r} has no kinds — spec mandates each "
                "category be useful out of the box."
            )
