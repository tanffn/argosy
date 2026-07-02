"""The NVDA policy-sell assessor — the glide (policy) half of the period directive.

Read-only: it reuses the codex-verified breach-tranche money-math and turns it into
a surface-or-stay-quiet verdict with IC-memo framing. It NEVER writes a proposal or
executes."""
from __future__ import annotations

import datetime as _dt

import argosy.services.nvda_policy_sell as nps
from argosy.services.breach_router import BreachTranche
from argosy.services.nvda_policy_sell import assess_nvda_policy_sell

_TODAY = _dt.date(2026, 7, 2)


def test_sell_due_when_nvda_breaches_cap(monkeypatch):
    tranche = BreachTranche(
        nvda_current_pct=57.0, nvda_cap_pct=13.0, over_cap_pct=44.0,
        total_over_cap_nis=4_000_000.0, n_quarters=8, tranche_nis=500_000.0,
    )
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: tranche)

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.status == "sell_due"
    assert v.category == "policy"
    assert v.tranche_nis == 500_000.0
    assert v.n_quarters == 8
    # The headline names the concentration vs the cap and the paced tranche.
    assert "57" in v.headline and "13" in v.headline
    # A taxable-event note travels with any sell (tax-aware pacing).
    assert v.tax_note


def test_no_action_when_within_cap(monkeypatch):
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: None)

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.status == "no_action"
    assert v.tranche_nis == 0.0
    # The no-action memo is explicit (doing nothing at concentration is a stance).
    assert v.headline
