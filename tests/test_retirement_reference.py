"""Tests for the hybrid-defaults resolver (Wave 0 · gap-foundation).

Resolver priority:
  1. ``identity_yaml.retirement_reference_overrides.<key>``  (per-user)
  2. Shipped default in ``argosy/data/israel_retirement_reference.yaml``
  3. ResolveError otherwise

Freshness:
  - Shipped default > 12 months past as_of → freshness_warning stamped
  - User override > 18 months past as_of → freshness_warning stamped
  - Intrinsic warnings in the YAML are preserved (win over auto-stamp)
"""
import pytest

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import ResolveError, resolve
from argosy.state.models import User, UserContext


def _seed_user(session, user_id: str = "ariel") -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()


def _seed_user_context_with_overrides(
    session,
    *,
    user_id: str = "ariel",
    overrides: dict | None = None,
) -> None:
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
    identity_yaml = "\n".join(lines) + "\n"
    session.add(
        UserContext(
            user_id=user_id,
            identity_yaml=identity_yaml,
            goals_yaml="",
            constraints_yaml="",
            current_stage="complete",
        )
    )
    session.commit()


class TestResolveShippedDefault:
    def test_returns_shipped_value(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(s)
            v = resolve("mekadem.clal_pensia", user_id="ariel", session=s)
        assert isinstance(v, ValueWithRationale)
        assert v.value == 200
        assert v.unit == "ratio"
        assert v.source_id == "clal_published_table_2026"

    def test_raises_on_unknown_key(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(s)
            with pytest.raises(ResolveError, match="unknown reference key"):
                resolve("nonexistent.key", user_id="ariel", session=s)


class TestResolveUserOverride:
    def test_user_override_takes_precedence(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(
                s,
                overrides={
                    "mekadem.clal_pensia": {
                        "value": 195,
                        "source": "user_intake",
                        "as_of_date": "2026-04",
                    },
                },
            )
            v = resolve("mekadem.clal_pensia", user_id="ariel", session=s)
        assert v.value == 195
        assert v.source_id == "user_intake"
        assert "user" in v.rationale.lower()


class TestFreshnessWarning:
    def test_stale_shipped_default_warns(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(s)
            v = resolve(
                "tax.israeli_cgt_equity",  # as_of 2026-01
                user_id="ariel",
                session=s,
                today="2027-08-01",  # > 12 months later
            )
        assert v.freshness_warning is not None
        assert "month" in v.freshness_warning.lower()

    def test_fresh_shipped_default_no_warning(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(s)
            v = resolve(
                "tax.israeli_cgt_equity",  # as_of 2026-01
                user_id="ariel",
                session=s,
                today="2026-06-01",
            )
        # 5 months old, no intrinsic warning → no stamp
        assert v.freshness_warning is None

    def test_intrinsic_warning_wins_over_auto_stamp(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(s)
            v = resolve(
                "bituach_leumi.single_age_67_base_2026",
                user_id="ariel",
                session=s,
                today="2026-06-01",  # fresh, but the YAML has an intrinsic warning
            )
        assert v.freshness_warning is not None
        assert "indexed annually" in v.freshness_warning.lower()

    def test_user_override_stale_warning_threshold_is_18_months(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context_with_overrides(
                s,
                overrides={
                    "mekadem.clal_pensia": {
                        "value": 195,
                        "source": "user_intake",
                        "as_of_date": "2026-04",
                    },
                },
            )
            # 12mo later — still under user's 18mo threshold
            v_under = resolve(
                "mekadem.clal_pensia", user_id="ariel", session=s, today="2027-04-15",
            )
            assert v_under.freshness_warning is None
            # 20mo later — over threshold
            v_over = resolve(
                "mekadem.clal_pensia", user_id="ariel", session=s, today="2027-12-15",
            )
            assert v_over.freshness_warning is not None
