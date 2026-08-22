"""Household operating-cash floor — the shekels that are never deployable."""

from __future__ import annotations

import pytest

from argosy.services import operating_reserve as opr


class TestFloorConversion:
    def test_floor_is_thirty_thousand_shekels(self):
        assert opr.OPERATING_FLOOR_ILS == 30_000.0

    def test_converts_at_the_supplied_rate(self):
        assert opr.operating_floor_usd(2.9910) == pytest.approx(10_030.09, abs=0.01)
        assert opr.operating_floor_usd(3.50) == pytest.approx(8_571.43, abs=0.01)

    @pytest.mark.parametrize("bad", [None, 0.0, -1.0])
    def test_missing_rate_withholds_NOTHING_rather_than_guessing(self, bad):
        """Guessing a rate would silently under-reserve the family's expense
        money. Withholding zero is visible in the plan and in the caveat."""
        assert opr.operating_floor_usd(bad) == 0.0


class TestSplit:
    def test_takes_the_floor_off_the_top(self):
        deployable, held = opr.apply_operating_floor(
            cash_total_usd=100_000.0, floor_usd=10_030.09)
        assert (deployable, held) == (89_969.91, 10_030.09)
        assert deployable + held == pytest.approx(100_000.0)

    def test_floor_larger_than_cash_withholds_everything(self):
        assert opr.apply_operating_floor(
            cash_total_usd=5_000.0, floor_usd=10_030.09) == (0.0, 5_000.0)

    def test_zero_floor_is_a_no_op(self):
        assert opr.apply_operating_floor(
            cash_total_usd=100_000.0, floor_usd=0.0) == (100_000.0, 0.0)

    def test_negative_inputs_clamp(self):
        assert opr.apply_operating_floor(
            cash_total_usd=-5.0, floor_usd=-5.0) == (0.0, 0.0)


class TestLabel:
    def test_names_both_currencies(self):
        text = opr.labeled_operating_floor(10_030.09, 2.9910)
        assert "10,030.09" in text and "30,000" in text and "2.9910" in text

    def test_says_so_loudly_when_no_rate(self):
        text = opr.labeled_operating_floor(0.0, None)
        assert "could NOT be withheld" in text


class TestAppliesOnlyToInferredCash:
    """The bug caught by the deploy-cash route tests on 2026-08-22.

    First implementation applied the floor unconditionally, so an explicit
    "deploy $10,000" request returned a $0 plan — the ILS 30k floor swallowed
    the whole amount. If the caller NAMED a figure they have already decided it
    is spare; withholding the buffer again double-counts it.
    """

    def _plan(self, **kw):
        from datetime import date

        from argosy.services.deployment_advisor import assemble_deployment_plan

        return assemble_deployment_plan(
            doc=None, holdings={}, deploy_amount_usd=10_000.0,
            as_of=date(2026, 8, 22), usd_ils=2.9910, **kw)

    def test_explicit_amount_is_untouched(self):
        plan = self._plan(cash_is_inferred=False)
        assert plan.operating_floor_usd == 0.0
        assert plan.deploy_amount_usd == 10_000.0

    def test_default_is_explicit_so_existing_callers_are_unaffected(self):
        assert self._plan().operating_floor_usd == 0.0

    def test_inferred_amount_has_the_floor_withheld(self):
        plan = self._plan(cash_is_inferred=True)
        assert plan.operating_floor_usd == pytest.approx(10_000.0)
        assert plan.deploy_amount_usd == 0.0

    def test_conservation_holds_on_a_realistic_balance(self):
        from datetime import date

        from argosy.services.deployment_advisor import assemble_deployment_plan

        plan = assemble_deployment_plan(
            doc=None, holdings={}, deploy_amount_usd=112_960.99,
            as_of=date(2026, 8, 22), usd_ils=2.9910, cash_is_inferred=True)
        assert plan.operating_floor_usd == pytest.approx(10_030.09, abs=0.01)
        assert plan.deploy_amount_usd + plan.operating_floor_usd == pytest.approx(
            plan.cash_total_usd, abs=0.01)
