"""Tests for the Spec D commit #4 delta_kind discriminator + 422 contract.

Coverage:
  * Each of the five delta_kind values successfully POSTs with the
    matching per-shape fields populated.
  * Wrong shape rejected with structured 422 (``delta_shape_invalid``).
  * Required-field missing rejected with structured 422.
  * Forbidden-field present rejected with structured 422.
  * (category, delta_kind) interaction matrix from spec §1.4 enforced
    with structured 422 (``delta_kind_not_valid_for_category``).
  * GET /catalog returns the new delta_kind sub-catalog with all 5
    values represented + per-category nudge text.
  * LifeEventOut (DTO) serializes BOTH new + legacy columns.
  * 422 banner contract preserved across the new variants (the body
    carries ``error``, ``delta_kind``, ``allowed_delta_kinds`` /
    ``missing_fields`` / ``forbidden_fields`` so the UI banner can
    render without re-deriving anything).
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _ensure_user(client_with_db, user_id: str = "ariel") -> None:
    from argosy.state.models import User

    SF = client_with_db.app.state.session_factory
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


def _post(client, payload: dict):
    return client.post("/api/life-events", json=payload)


# Per-shape valid-payload templates (Spec D §1.4 interaction matrix).
def _payload_one_shot() -> dict:
    return {
        "user_id": "ariel",
        "category": "asset_event",
        "kind": "inheritance",
        "delta_kind": "one_shot",
        "target_date": "2030-01-15",
        "one_shot_amount_usd": 200000.0,
        "description": "Inheritance from family",
    }


def _payload_recurring() -> dict:
    return {
        "user_id": "ariel",
        "category": "recurring_expense",
        "kind": "new_car",
        "delta_kind": "recurring_every_n_years",
        "target_date": "2027-03-15",  # anchor (column-reused)
        "recurring_amount_usd": -67000.0,
        "recurring_period_years": 5,
        "description": "New car every 5 years",
    }


def _payload_phase_start() -> dict:
    return {
        "user_id": "ariel",
        "category": "family_event",
        "kind": "dependent_leaves",
        "delta_kind": "phase_change_start",
        "phase_start_date": "2034-08-15",
        "monthly_delta_usd": 1500.0,  # positive = expense reduction
        "description": "Kids leave home",
    }


def _payload_phase_end() -> dict:
    return {
        "user_id": "ariel",
        "category": "expense_event",
        "kind": "college",
        "delta_kind": "phase_change_end",
        "phase_start_date": "2030-09-01",
        "phase_end_date": "2034-09-01",
        "monthly_delta_usd": -3333.0,  # negative = expense
        "description": "Kid in college",
    }


def _payload_none() -> dict:
    return {
        "user_id": "ariel",
        "category": "retirement_milestone",
        "kind": "sigma_calibration",
        "delta_kind": "none",
        "description": "Switched sigma model",
    }


# ---------------------------------------------------------------------------
# Happy-path: each delta_kind round-trips POST → 201 → GET
# ---------------------------------------------------------------------------


class TestDeltaKindHappyPath:
    """One test per delta_kind: payload validates, persists, and the
    GET response surfaces the per-shape columns."""

    def test_one_shot_persists(self, client_with_db):
        _ensure_user(client_with_db)
        r = _post(client_with_db, _payload_one_shot())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["delta_kind"] == "one_shot"
        assert body["one_shot_amount_usd"] == 200000.0
        assert body["target_date"] == "2030-01-15"

    def test_recurring_persists(self, client_with_db):
        _ensure_user(client_with_db)
        r = _post(client_with_db, _payload_recurring())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["delta_kind"] == "recurring_every_n_years"
        assert body["recurring_amount_usd"] == -67000.0
        assert body["recurring_period_years"] == 5
        assert body["target_date"] == "2027-03-15"

    def test_phase_change_start_persists(self, client_with_db):
        _ensure_user(client_with_db)
        r = _post(client_with_db, _payload_phase_start())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["delta_kind"] == "phase_change_start"
        assert body["phase_start_date"] == "2034-08-15"
        assert body["monthly_delta_usd"] == 1500.0
        assert body["phase_end_date"] is None

    def test_phase_change_end_persists(self, client_with_db):
        _ensure_user(client_with_db)
        r = _post(client_with_db, _payload_phase_end())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["delta_kind"] == "phase_change_end"
        assert body["phase_start_date"] == "2030-09-01"
        assert body["phase_end_date"] == "2034-09-01"
        assert body["monthly_delta_usd"] == -3333.0

    def test_none_persists(self, client_with_db):
        _ensure_user(client_with_db)
        r = _post(client_with_db, _payload_none())
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["delta_kind"] == "none"
        # All per-shape fields null on a none-kind row.
        assert body["one_shot_amount_usd"] is None
        assert body["recurring_amount_usd"] is None
        assert body["monthly_delta_usd"] is None
        assert body["phase_start_date"] is None


# ---------------------------------------------------------------------------
# Missing-required-field 422s, one per delta_kind
# ---------------------------------------------------------------------------


class TestMissingRequiredField:
    def test_one_shot_missing_amount_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_one_shot()
        del payload["one_shot_amount_usd"]
        r = _post(client_with_db, payload)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["delta_kind"] == "one_shot"
        assert detail["reason"] == "missing_required"
        assert "one_shot_amount_usd" in detail["missing_fields"]

    def test_one_shot_missing_date_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_one_shot()
        del payload["target_date"]
        r = _post(client_with_db, payload)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "missing_required"
        assert "target_date" in detail["missing_fields"]

    def test_recurring_missing_period_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_recurring()
        del payload["recurring_period_years"]
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "missing_required"
        assert "recurring_period_years" in detail["missing_fields"]

    def test_phase_start_missing_start_date_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_phase_start()
        del payload["phase_start_date"]
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert "phase_start_date" in detail["missing_fields"]

    def test_phase_end_missing_end_date_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_phase_end()
        del payload["phase_end_date"]
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "missing_required"
        assert "phase_end_date" in detail["missing_fields"]


# ---------------------------------------------------------------------------
# Forbidden-field-present 422s
# ---------------------------------------------------------------------------


class TestForbiddenFieldPresent:
    def test_one_shot_with_monthly_delta_422(self, client_with_db):
        """one_shot forbids monthly_delta_usd (which belongs to
        phase_change_*).  Spec §3.2."""
        _ensure_user(client_with_db)
        payload = _payload_one_shot()
        payload["monthly_delta_usd"] = -500.0
        r = _post(client_with_db, payload)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "monthly_delta_usd" in detail["forbidden_fields"]

    def test_recurring_with_phase_start_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_recurring()
        payload["phase_start_date"] = "2030-01-01"
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "phase_start_date" in detail["forbidden_fields"]

    def test_phase_start_with_end_date_422(self, client_with_db):
        """phase_change_start is OPEN-ENDED — phase_end_date must NOT
        be sent (use phase_change_end for closed bands).  Spec §1.1 +
        the CHECK constraint."""
        _ensure_user(client_with_db)
        payload = _payload_phase_start()
        payload["phase_end_date"] = "2040-01-01"
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "phase_end_date" in detail["forbidden_fields"]

    def test_phase_end_with_end_before_start_422(self, client_with_db):
        """phase_change_end requires end > start (spec §1.1 CHECK)."""
        _ensure_user(client_with_db)
        payload = _payload_phase_end()
        payload["phase_end_date"] = "2029-01-01"  # < phase_start
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "phase_end_date" in detail["forbidden_fields"]

    def test_none_with_any_per_shape_field_422(self, client_with_db):
        """delta_kind='none' forbids ALL per-shape fields (spec §1.3)."""
        _ensure_user(client_with_db)
        payload = _payload_none()
        payload["one_shot_amount_usd"] = 50000.0
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "one_shot_amount_usd" in detail["forbidden_fields"]


# ---------------------------------------------------------------------------
# (category, delta_kind) interaction matrix 422s (spec §1.4)
# ---------------------------------------------------------------------------


class TestDeltaKindForCategoryMatrix:
    def test_recurring_expense_with_one_shot_422(self, client_with_db):
        """recurring_expense only allows recurring_every_n_years."""
        _ensure_user(client_with_db)
        payload = _payload_one_shot()
        payload["category"] = "recurring_expense"
        payload["kind"] = "new_car"
        r = _post(client_with_db, payload)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_kind_not_valid_for_category"
        assert detail["category"] == "recurring_expense"
        assert detail["delta_kind"] == "one_shot"
        assert "recurring_every_n_years" in detail["allowed_delta_kinds"]

    def test_asset_event_with_phase_change_422(self, client_with_db):
        """asset_event allows only one_shot + none (spec §1.4)."""
        _ensure_user(client_with_db)
        payload = _payload_phase_start()
        payload["category"] = "asset_event"
        payload["kind"] = "home_purchase"
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_kind_not_valid_for_category"
        assert detail["delta_kind"] == "phase_change_start"
        assert set(detail["allowed_delta_kinds"]) == {"one_shot", "none"}

    def test_retirement_milestone_with_recurring_422(self, client_with_db):
        _ensure_user(client_with_db)
        payload = _payload_recurring()
        payload["category"] = "retirement_milestone"
        payload["kind"] = "target_retire_year_change"
        r = _post(client_with_db, payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_kind_not_valid_for_category"
        assert detail["category"] == "retirement_milestone"


# ---------------------------------------------------------------------------
# Catalog endpoint: new delta_kind sub-catalog (Spec D commit #4)
# ---------------------------------------------------------------------------


class TestCatalogDeltaKindRules:
    def test_catalog_carries_delta_kind_rules_per_category(self, client_with_db):
        r = client_with_db.get("/api/life-events/catalog")
        assert r.status_code == 200
        body = r.json()
        assert "delta_kind_rules_by_category" in body
        rules = body["delta_kind_rules_by_category"]
        # All six categories represented.
        assert set(rules.keys()) == {
            "career_event",
            "family_event",
            "asset_event",
            "expense_event",
            "recurring_expense",
            "retirement_milestone",
        }
        # Spot-check the matrix.
        assert "one_shot" in rules["asset_event"]["allowed_delta_kinds"]
        assert (
            "recurring_every_n_years"
            in rules["recurring_expense"]["allowed_delta_kinds"]
        )
        assert (
            rules["recurring_expense"]["default_delta_kind"]
            == "recurring_every_n_years"
        )
        # Every entry carries a non-empty nudge string.
        for _, entry in rules.items():
            assert isinstance(entry["nudge"], str)
            assert len(entry["nudge"]) > 0

    def test_catalog_required_fields_covers_all_five_delta_kinds(
        self, client_with_db
    ):
        r = client_with_db.get("/api/life-events/catalog")
        body = r.json()
        required = body["required_fields_by_delta_kind"]
        forbidden = body["forbidden_fields_by_delta_kind"]
        # All five delta_kinds must be present in both maps.
        expected = {
            "one_shot",
            "recurring_every_n_years",
            "phase_change_start",
            "phase_change_end",
            "none",
        }
        assert set(required.keys()) == expected
        assert set(forbidden.keys()) == expected
        # Sanity: one_shot requires amount + date; recurring requires
        # the period column; phase_change_end requires the end date.
        assert "one_shot_amount_usd" in required["one_shot"]
        assert "recurring_period_years" in required["recurring_every_n_years"]
        assert "phase_end_date" in required["phase_change_end"]
        # none forbids everything per-shape.
        assert "one_shot_amount_usd" in forbidden["none"]
        assert "monthly_delta_usd" in forbidden["none"]
        # one_shot forbids the phase_* fields.
        assert "phase_start_date" in forbidden["one_shot"]
        assert "monthly_delta_usd" in forbidden["one_shot"]

    def test_catalog_preserves_legacy_field_rules(self, client_with_db):
        """Backwards-compat: the original ``field_rules_by_category``
        from sprint #8 must still be present alongside the new
        delta_kind sub-catalog."""
        r = client_with_db.get("/api/life-events/catalog")
        body = r.json()
        assert "field_rules_by_category" in body
        assert (
            body["field_rules_by_category"]["recurring_expense"][
                "supports_recurring_years"
            ]
            is True
        )


# ---------------------------------------------------------------------------
# DTO backwards-compatibility: BOTH new + legacy columns serialized
# ---------------------------------------------------------------------------


class TestUpdateRouteValidation:
    """PUT-level validation parity with POST (codex BLOCKER on Spec D
    commit #4 review — gaps closed)."""

    def test_put_rejects_cross_shape_delta_kind_transition(self, client_with_db):
        """Sending a different delta_kind via PUT must 422 — cross-shape
        transitions in v1 require DELETE + POST."""
        _ensure_user(client_with_db)
        created = _post(client_with_db, _payload_one_shot()).json()
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "delta_kind": "phase_change_start",
            },
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "delta_kind" in detail["forbidden_fields"]

    def test_put_accepts_same_delta_kind_resend(self, client_with_db):
        """Re-sending the SAME delta_kind on PUT is a no-op and must
        not 422."""
        _ensure_user(client_with_db)
        created = _post(client_with_db, _payload_one_shot()).json()
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "delta_kind": "one_shot",
                "one_shot_amount_usd": 250000.0,  # bumping the estimate
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["one_shot_amount_usd"] == 250000.0

    def test_put_forbidden_field_against_row_shape_422(self, client_with_db):
        """PUT that sends a field not allowed by the row's existing
        delta_kind must 422 (e.g. setting monthly_delta_usd on a
        one_shot row)."""
        _ensure_user(client_with_db)
        created = _post(client_with_db, _payload_one_shot()).json()
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "monthly_delta_usd": -500.0,
            },
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "monthly_delta_usd" in detail["forbidden_fields"]

    def test_put_phase_end_invariant_enforced(self, client_with_db):
        """Editing phase_end_date on a phase_change_end row to be
        before the row's phase_start_date must 422."""
        _ensure_user(client_with_db)
        created = _post(client_with_db, _payload_phase_end()).json()
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "phase_end_date": "2029-01-01",  # < row's 2030-09-01
            },
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert detail["reason"] == "forbidden_present"
        assert "phase_end_date" in detail["forbidden_fields"]

    def test_put_phase_end_invariant_with_both_dates_sent(self, client_with_db):
        """A PUT that changes BOTH dates simultaneously still 422s when
        the result violates end>start."""
        _ensure_user(client_with_db)
        created = _post(client_with_db, _payload_phase_end()).json()
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "phase_start_date": "2035-01-01",
                "phase_end_date": "2034-01-01",
            },
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "delta_shape_invalid"
        assert "phase_end_date" in detail["forbidden_fields"]

    def test_put_category_change_revalidates_matrix(self, client_with_db):
        """If the PUT changes the category, the row's existing
        delta_kind must still be in the new category's allowed list."""
        _ensure_user(client_with_db)
        # Start with an asset_event:other_asset_acquired one_shot row.
        payload = _payload_one_shot()
        payload["kind"] = "other_asset_acquired"
        created = _post(client_with_db, payload).json()
        # Try to switch to recurring_expense (only allows
        # recurring_every_n_years) — must 422.
        r = client_with_db.put(
            f"/api/life-events/{created['id']}",
            json={
                "user_id": "ariel",
                "category": "recurring_expense",
                "kind": "new_car",
            },
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "delta_kind_not_valid_for_category"
        assert detail["delta_kind"] == "one_shot"
        assert detail["category"] == "recurring_expense"


class TestDtoBackwardsCompat:
    def test_dto_serializes_new_and_legacy_columns(self, client_with_db):
        _ensure_user(client_with_db)
        # Insert a row with the new shape.
        created = _post(client_with_db, _payload_one_shot()).json()
        # The response carries BOTH the legacy fields AND the new
        # per-shape fields.
        for legacy_key in ("target_date", "amount_usd", "recurring_years"):
            assert legacy_key in created
        for new_key in (
            "delta_kind",
            "one_shot_amount_usd",
            "recurring_amount_usd",
            "recurring_period_years",
            "monthly_delta_usd",
            "phase_start_date",
            "phase_end_date",
            "fx_at_event",
        ):
            assert new_key in created

    def test_legacy_payload_still_accepted(self, client_with_db):
        """Pre-Spec-D payload (no delta_kind, legacy amount_usd +
        target_date) still works — defaults to delta_kind='none' and
        rides along on the row.  Skip the strict per-shape check so
        the existing UI keeps working until commit #5 rewrites it."""
        _ensure_user(client_with_db)
        legacy = {
            "user_id": "ariel",
            "category": "expense_event",
            "kind": "college",
            "target_date": "2032-09-01",
            "amount_usd": 200000,
            "description": "Kid #1 starts college",
        }
        r = _post(client_with_db, legacy)
        assert r.status_code == 201, r.text
        body = r.json()
        # Legacy fields preserved.
        assert body["target_date"] == "2032-09-01"
        assert body["amount_usd"] == 200000.0
        # New columns default to none / null.
        assert body["delta_kind"] == "none"
        assert body["one_shot_amount_usd"] is None

    def test_list_route_returns_dto_with_both_shapes(self, client_with_db):
        _ensure_user(client_with_db)
        _post(client_with_db, _payload_phase_end())
        r = client_with_db.get("/api/life-events?user_id=ariel")
        assert r.status_code == 200
        events = r.json()["events"]
        assert len(events) == 1
        row = events[0]
        # New per-shape columns populated.
        assert row["delta_kind"] == "phase_change_end"
        assert row["phase_end_date"] == "2034-09-01"
        # Legacy columns present (null because the new-shape write
        # didn't populate them).
        assert "amount_usd" in row
        assert "recurring_years" in row
