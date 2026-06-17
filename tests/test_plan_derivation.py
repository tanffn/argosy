"""Lock the codex-zigzag-confirmed derived numbers (2026-06-18) against fixed inputs."""
from argosy.services.plan_derivation import (
    derive_fi_margin_liquid, derive_nvda_deconcentration,
)

# Current-state inputs (FFS 12-Jun-26 + resolver).
NVDA_SH, NVDA_PX, NVDA_W = 11471, 200.14, 0.6251889
CAP, TARGET_W = 0.13, 0.12
LIQUID_NW, FI_TOTAL_CAP = 11687925.80, 11836133.33
NET_WORTH = 11954153.15  # investable basis — the WRONG basis for FI


def test_nvda_deconcentration_matches_locked_values():
    d = derive_nvda_deconcentration(
        nvda_sh=NVDA_SH, nvda_px_usd=NVDA_PX, nvda_weight=NVDA_W,
        target_w=TARGET_W, cap=CAP,
    )
    # strict <=12% -> retain 2,201, sell 9,270 (codex zigzag confirmed)
    assert d["nvda_target_sh"].value == 2201
    assert d["nvda_sell_sh"].value == 9270
    assert d["nvda_cap_breach_x"].value == 4.81
    # retained shares must be UNDER the 12% target (not over)
    book = NVDA_SH * NVDA_PX / NVDA_W
    assert d["nvda_target_sh"].value * NVDA_PX / book <= TARGET_W


def test_fi_margin_uses_liquid_basis_and_is_negative():
    d = derive_fi_margin_liquid(
        liquid_nw_nis=LIQUID_NW, fi_total_capital_nis=FI_TOTAL_CAP,
    )
    assert round(d.value) == -148208          # FI NOT met on the honest basis
    assert d.value < 0


def test_investable_basis_would_overstate_fi_the_codex_block():
    # Using the investable basis instead of liquid flips the sign (+118K) — the BLOCK.
    wrong = derive_fi_margin_liquid(
        liquid_nw_nis=NET_WORTH, fi_total_capital_nis=FI_TOTAL_CAP,
    )
    assert round(wrong.value) == 118020       # the overstatement we must NOT report
