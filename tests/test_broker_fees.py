"""Commission arithmetic — pinned against REAL charges from the Leumi tape.

Every expected value in the first class below was taken from an actual line in
the household's 2025 annual report or H1-2026 statements, not invented. If a
change to broker_fees breaks these, the model has stopped describing reality.
"""

from __future__ import annotations

import pytest

from argosy.services import broker_fees as bf


class TestAgainstRealCharges:
    """Reproduce charges Leumi actually levied under the 0.07% / $7 terms."""

    # The 0.07% window schedule, as it stood 23/12/2025-19/08/2026. Minimum is
    # ILS 20.35 == $7 at the ~2.91 ILS/USD implied by the statements.
    H1_2026 = bf.FeeSchedule(
        venue="leumi", instrument_class="foreign_securities", currency="ILS",
        rate=0.0007, minimum=20.35, maximum_pct_of_ticket=0.30,
    )

    @pytest.mark.parametrize(
        "ticket,expected,rule",
        [
            # Percentage binding — straight 0.07% of the ticket.
            (38_492.64, 26.94, "rate"),
            (157_732.72, 110.41, "rate"),
            (69_437.25, 48.61, "rate"),
            (111_495.03, 78.05, "rate"),
            # Floor binding — all charged the ILS 20.35 minimum in H1-2026.
            (22_238.55, 20.35, "minimum"),
            (20_136.78, 20.35, "minimum"),
            (17_442.00, 20.35, "minimum"),
            (11_376.25, 20.35, "minimum"),
        ],
    )
    def test_matches_the_statement(self, ticket, expected, rule):
        est = bf.estimate(ticket, self.H1_2026)
        assert est.commission == pytest.approx(expected, abs=0.02)
        assert est.binding_rule == rule

    # The 2025 schedule: 0.09% and a floor of ~ILS 26. Distinct from H1_2026 —
    # an earlier draft of this test applied the 2026 floor to a 2025 trade and
    # got the binding rule wrong, which is exactly the confusion worth pinning.
    FY_2025 = bf.FeeSchedule(
        venue="leumi", instrument_class="foreign_securities", currency="ILS",
        rate=0.0009, minimum=25.90, maximum_pct_of_ticket=0.30,
    )

    def test_the_2025_floor_explains_the_editas_charge(self):
        """EDITAS, 21/07/25: ILS 274.77 of proceeds, ILS 26.84 charged = 9.77%.
        The 30% cap does NOT bind here (30% of 274.77 is 82.43) — the floor
        does. No real trade on this tape ever hit the 30% cap."""
        est = bf.estimate(274.77, self.FY_2025)
        assert est.binding_rule == "minimum"
        assert est.commission == pytest.approx(26.84, abs=1.0)
        assert est.below_breakeven is True
        assert est.cost_fraction > 0.09

    def test_the_30_percent_cap_binds_only_on_a_truly_tiny_ticket(self):
        """Synthetic — no observed trade was small enough. At ILS 50 the 30%
        cap (15.00) is below the ILS 20.35 floor, so the cap must win."""
        est = bf.estimate(50.0, self.H1_2026)
        assert est.binding_rule == "maximum_pct_of_ticket"
        assert est.commission == pytest.approx(15.0, abs=0.01)
        assert est.below_breakeven is True


class TestSchedule:
    def test_percentage_binds_above_breakeven(self):
        est = bf.estimate(200_000.0, bf.LEUMI_FOREIGN)
        assert est.binding_rule == "rate"
        assert est.commission == pytest.approx(120.0)
        assert est.below_breakeven is False

    def test_floor_binds_below_breakeven(self):
        est = bf.estimate(10_000.0, bf.LEUMI_FOREIGN)
        assert est.binding_rule == "minimum"
        assert est.commission == 25.0
        assert est.below_breakeven is True
        assert est.cost_bps == pytest.approx(25.0)

    def test_breakeven_is_where_the_two_rules_meet(self):
        be = bf.breakeven_ticket(bf.LEUMI_FOREIGN)
        assert be == pytest.approx(41_666.67, abs=0.01)
        assert bf.estimate(be + 1, bf.LEUMI_FOREIGN).binding_rule == "rate"
        assert bf.estimate(be - 1, bf.LEUMI_FOREIGN).binding_rule == "minimum"

    def test_absolute_maximum_caps_a_huge_ticket(self):
        est = bf.estimate(50_000_000.0, bf.LEUMI_FOREIGN)
        assert est.binding_rule == "maximum"
        assert est.commission == 7500.0

    def test_zero_ticket_is_free_and_not_flagged(self):
        est = bf.estimate(0.0, bf.LEUMI_FOREIGN)
        assert est.commission == 0.0
        assert est.below_breakeven is False

    def test_israeli_schedule_has_no_dispute_and_a_low_breakeven(self):
        assert bf.LEUMI_ISRAELI.disputed_minimum is None
        assert bf.estimate(50_000.0, bf.LEUMI_ISRAELI).note == ""
        assert bf.breakeven_ticket(bf.LEUMI_ISRAELI) == pytest.approx(10_000.0)


class TestTheDisputedMinimum:
    """The 20/08/2026 change must be modelled conservatively and stay loud."""

    def test_foreign_schedule_models_the_conservative_25(self):
        assert bf.LEUMI_FOREIGN.minimum == 25.0
        assert bf.LEUMI_FOREIGN.disputed_minimum == 7.0

    def test_every_foreign_estimate_carries_the_dispute_note(self):
        assert "DISPUTED" in bf.estimate(5_000.0, bf.LEUMI_FOREIGN).note
        assert "DISPUTED" in bf.estimate(500_000.0, bf.LEUMI_FOREIGN).note

    def test_the_dispute_doubles_the_h1_2026_bill(self):
        """The finding that makes this worth modelling: at 0.06% with a $25
        floor, 29 of 30 foreign trades on the real H1 tape pay the flat floor,
        so the 'improved' rate is decorative. Reproduce that here."""
        # Real H1-2026 foreign tickets, in ILS, from the statements.
        tape_ils = [
            38492.64, 22334.59, 36450.44, 69437.25, 52261.46, 43280.85,
            78582.47, 157732.72, 76626.08, 65004.97, 66173.56, 111495.03,
            38766.65, 49287.82, 26340.32, 11376.25, 34040.97, 47858.86,
            60859.08, 22238.55, 20136.78, 23907.16, 17442.00, 26822.43,
            34831.30, 37790.48, 9034.48, 15989.79, 82103.76, 59048.15,
        ]
        actual_ils = 1132.13 - 21.58 - 23.18 - 13.79   # less the 3 TASE trades
        new = bf.FeeSchedule(
            venue="leumi", instrument_class="foreign_securities", currency="ILS",
            rate=0.0006, minimum=25 * 2.91, maximum_pct_of_ticket=0.30,
        )
        ests = [bf.estimate(t, new) for t in tape_ils]
        floored = sum(1 for e in ests if e.below_breakeven)
        total = sum(e.commission for e in ests)
        assert floored == 29, f"expected 29 of 30 floored, got {floored}"
        assert total > 2 * actual_ils, (
            f"disputed reading should more than double the bill: "
            f"{total:.2f} vs {actual_ils:.2f}"
        )


class TestDescribe:
    def test_flags_the_floor_and_names_the_breakeven(self):
        text = bf.describe(bf.estimate(10_000.0, bf.LEUMI_FOREIGN))
        assert "per-trade floor" in text
        assert "41,667" in text

    def test_stays_quiet_when_the_rate_binds(self):
        text = bf.describe(bf.estimate(200_000.0, bf.LEUMI_FOREIGN))
        assert "floor" not in text
