from __future__ import annotations

import pytest

from argosy.services.allocation_plan import normalize_override_labels


CASH = "Cash & T-bills (incl. ILS tranche)"
SHORT = "Short-duration IG bonds"


def test_fold_short_duration_into_cash_adds_pct_and_drops_key() -> None:
    """Production path: durable overrides still carry Short-duration → must not 400."""
    out = normalize_override_labels({
        CASH: 6.95,
        SHORT: 2.98,
        "Strategic single-stock (NVDA)": 8.0,
    })
    assert SHORT not in out
    assert out[CASH] == pytest.approx(9.93)
    assert out["Strategic single-stock (NVDA)"] == 8.0


def test_fold_short_only_creates_cash_pin() -> None:
    out = normalize_override_labels({SHORT: 2.98})
    assert SHORT not in out
    assert out[CASH] == pytest.approx(2.98)


def test_fold_absent_short_is_noop_for_cash() -> None:
    out = normalize_override_labels({CASH: 6.95})
    assert out == {CASH: 6.95}
