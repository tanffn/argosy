"""Tests for the mekadem variance band (Wave 1 · BLOCKER #3)."""
import pytest

from argosy.services.retirement.mekadem import (
    MekademBand,
    get_mekadem_for_fund,
    monthly_annuity_for_band,
)
from argosy.state.models import User, UserContext


def _seed_user(session, user_id: str = "ariel") -> None:
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, plan="free"))
        session.commit()


def _seed_user_context(
    session, *, user_id: str = "ariel", overrides: dict | None = None
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


class TestMekademBand:
    def test_returns_band_with_low_lt_typical_lt_high(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            band = get_mekadem_for_fund(
                "clal_pensia", user_id="ariel", session=s,
            )
        assert isinstance(band, MekademBand)
        assert band.fund_id == "clal_pensia"
        assert band.typical.value == 200
        # Default band width 2.5%
        assert band.low.value == pytest.approx(195.0)
        assert band.high.value == pytest.approx(205.0)
        assert band.low.value < band.typical.value < band.high.value

    def test_band_for_all_three_supported_funds(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            clal = get_mekadem_for_fund("clal_pensia", user_id="ariel", session=s)
            migdal = get_mekadem_for_fund("migdal_pensia", user_id="ariel", session=s)
            menorah = get_mekadem_for_fund("menorah_pensia", user_id="ariel", session=s)
        assert clal.typical.value == 200
        assert migdal.typical.value == 198
        assert menorah.typical.value == 202

    def test_typical_carries_canonical_source_band_carries_derived(
        self, client_with_db
    ):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            band = get_mekadem_for_fund("clal_pensia", user_id="ariel", session=s)
        assert band.typical.source_id == "clal_published_table_2026"
        # Low / high are derived — source_id None per the convention.
        assert band.low.source_id is None
        assert band.high.source_id is None
        # Rationales mention the band methodology
        assert "favorable" in band.low.rationale.lower()
        assert "unfavorable" in band.high.rationale.lower()

    def test_user_override_propagates_into_band(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(
                s,
                overrides={
                    "mekadem.clal_pensia": {
                        "value": 190,
                        "source": "user_intake_2026_q2",
                        "as_of_date": "2026-04",
                    },
                },
            )
            band = get_mekadem_for_fund("clal_pensia", user_id="ariel", session=s)
        assert band.typical.value == 190
        assert band.typical.source_id == "user_intake_2026_q2"
        # Band derived from the user's typical (mekadem rounded to 1 decimal)
        assert band.low.value == pytest.approx(185.25, abs=0.1)
        assert band.high.value == pytest.approx(194.75, abs=0.1)

    def test_unsupported_fund_raises(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            with pytest.raises(ValueError, match="unsupported fund_id"):
                get_mekadem_for_fund(
                    "nonexistent_fund", user_id="ariel", session=s,  # type: ignore[arg-type]
                )

    def test_custom_band_width(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            band = get_mekadem_for_fund(
                "clal_pensia", user_id="ariel", session=s, band_width=0.05,
            )
        # 5% wider: 190 / 200 / 210
        assert band.low.value == pytest.approx(190.0)
        assert band.high.value == pytest.approx(210.0)


class TestMonthlyAnnuityForBand:
    def test_inverts_mekadem_direction(self, client_with_db):
        # balance / mekadem: high mekadem → low annuity.
        # The annuity band's "low" comes from the mekadem band's "high".
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            band = get_mekadem_for_fund("clal_pensia", user_id="ariel", session=s)
        balance_nis = 1_500_000.0  # Hypothetical pension balance
        low, typical, high = monthly_annuity_for_band(
            band, balance_nis=balance_nis,
        )
        # 1_500_000 / 205 = 7317.07 (low annuity from high mekadem)
        # 1_500_000 / 200 = 7500.00 (typical)
        # 1_500_000 / 195 = 7692.31 (high annuity from low mekadem)
        assert low.value == pytest.approx(7317.07, abs=0.01)
        assert typical.value == pytest.approx(7500.00, abs=0.01)
        assert high.value == pytest.approx(7692.31, abs=0.01)
        assert low.value < typical.value < high.value
        # All annuity outputs are in NIS/mo
        assert low.unit == typical.unit == high.unit == "NIS/mo"

    def test_zero_balance_raises(self, client_with_db):
        SF = client_with_db.app.state.session_factory
        with SF() as s:
            _seed_user(s)
            _seed_user_context(s)
            band = get_mekadem_for_fund("clal_pensia", user_id="ariel", session=s)
        with pytest.raises(ValueError, match="balance_nis must be"):
            monthly_annuity_for_band(band, balance_nis=0)
