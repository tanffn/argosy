"""Tests for sigma calibration helpers — alternatives sleeve sigma is computed
from the verified instruments' asset classes, not a fixed constant."""
from __future__ import annotations

from argosy.services.retirement.sigma_calibration import compute_alternatives_sigma


def test_alternatives_sigma_linear_blend_gold_btc():
    # gold σ=0.16, BTC σ=0.70; 80/20 blend = 0.8*0.16 + 0.2*0.70 = 0.268
    sigma = compute_alternatives_sigma([("precious_metals", 0.8), ("crypto", 0.2)])
    assert round(sigma, 3) == 0.268


def test_alternatives_sigma_empty_is_zero():
    assert compute_alternatives_sigma([]) == 0.0


def test_alternatives_sigma_all_gold():
    assert round(compute_alternatives_sigma([("precious_metals", 1.0)]), 3) == 0.16


def test_alternatives_sigma_normalises_percent_weights():
    # weights given as within-sleeve percentages (sum 100) yield the same blend
    sigma = compute_alternatives_sigma([("precious_metals", 80.0), ("crypto", 20.0)])
    assert round(sigma, 3) == 0.268


def test_alternatives_sigma_unknown_class_uses_conservative_default():
    # an unmapped class gets the conservative 0.30 default, never silently 0
    assert round(compute_alternatives_sigma([("mystery", 1.0)]), 3) == 0.30
