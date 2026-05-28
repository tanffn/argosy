"""Smoke tests for the umbrella /api/retirement/* router (Wave 0).

Wave 0 ships only the sources + reference endpoints; later waves add
projection / safety / ruin endpoints to the same prefix.
"""
import pytest

from argosy.state.models import User, UserContext


def _seed_user_with_override(
    session,
    *,
    user_id: str = "ariel",
    overrides: dict | None = None,
) -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()
    lines = ["date_of_birth: '1982-08-28'"]
    if overrides:
        lines.append("retirement_reference_overrides:")
        for k, sub in overrides.items():
            lines.append(f"  {k}:")
            for sk, sv in sub.items():
                if isinstance(sv, str):
                    lines.append(f"    {sk}: '{sv}'")
                else:
                    lines.append(f"    {sk}: {sv}")
    session.add(
        UserContext(
            user_id=user_id,
            identity_yaml="\n".join(lines) + "\n",
            goals_yaml="",
            constraints_yaml="",
            current_stage="complete",
        )
    )
    session.commit()


class TestSourcesEndpoint:
    def test_get_sources_returns_canonical_registry(self, client_with_db):
        r = client_with_db.get("/api/retirement/sources")
        assert r.status_code == 200
        body = r.json()
        assert "sources" in body
        assert "bituach_leumi_old_age_2026" in body["sources"]
        bl = body["sources"]["bituach_leumi_old_age_2026"]
        assert bl["kind"] == "official"
        assert bl["url"].startswith("https://www.btl.gov.il")

    def test_get_source_by_id(self, client_with_db):
        r = client_with_db.get("/api/retirement/sources/bengen_1994")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "bengen_1994"
        assert body["kind"] == "research"

    def test_get_source_returns_404_for_unknown(self, client_with_db):
        r = client_with_db.get("/api/retirement/sources/nonexistent_xyz")
        assert r.status_code == 404


class TestReferenceEndpoint:
    def test_get_reference_returns_value_with_rationale(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/reference/mekadem.clal_pensia?user_id=ariel",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["value"] == 200
        assert body["unit"] == "ratio"
        assert body["source_id"] == "clal_published_table_2026"

    def test_get_reference_returns_user_override(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(
                s,
                overrides={
                    "mekadem.clal_pensia": {
                        "value": 195,
                        "source": "user_intake",
                        "as_of_date": "2026-04",
                    },
                },
            )
        r = client_with_db.get(
            "/api/retirement/reference/mekadem.clal_pensia?user_id=ariel",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["value"] == 195
        assert body["source_id"] == "user_intake"

    def test_get_reference_returns_404_for_unknown_key(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/reference/nonexistent.key?user_id=ariel",
        )
        assert r.status_code == 404


class TestMekademEndpoint:
    def test_get_mekadem_returns_band(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/mekadem/clal_pensia?user_id=ariel",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["fund_id"] == "clal_pensia"
        assert body["typical"]["value"] == 200
        assert body["low"]["value"] < body["typical"]["value"] < body["high"]["value"]
        # No balance supplied → no annuity band
        assert "annuity_monthly_nis_typical" not in body

    def test_get_mekadem_with_balance_includes_annuity_band(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/mekadem/clal_pensia?user_id=ariel&balance_nis=1500000",
        )
        assert r.status_code == 200
        body = r.json()
        assert "annuity_monthly_nis_typical" in body
        # 1.5M / 200 = 7500
        assert body["annuity_monthly_nis_typical"]["value"] == pytest.approx(7500.0, abs=0.01)

    def test_get_mekadem_404_on_unknown_fund(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/mekadem/bogus_fund?user_id=ariel",
        )
        assert r.status_code == 404


class TestBituachLeumiEndpoint:
    def test_get_bl_stipend_full_history_no_spouse(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/bituach-leumi?user_id=ariel&current_age=43"
            "&contribution_history_years=35&spouse_eligible=false",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["monthly_nis"]["value"] == pytest.approx(2100.0)
        assert body["eligibility_age"]["value"] == 67
        assert len(body["sensitivity_levers"]) == 3

    def test_get_bl_stipend_with_spouse_adds_supplement(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user_with_override(s)
        r = client_with_db.get(
            "/api/retirement/bituach-leumi?user_id=ariel&current_age=43"
            "&contribution_history_years=35&spouse_eligible=true",
        )
        assert r.status_code == 200
        body = r.json()
        assert body["monthly_nis"]["value"] == pytest.approx(3150.0)
        assert body["spouse_supplement_applied"]["value"] == 1
