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


def _tranche():
    return BreachTranche(
        nvda_current_pct=57.0, nvda_cap_pct=13.0, over_cap_pct=44.0,
        total_over_cap_nis=4_000_000.0, n_quarters=8, tranche_nis=500_000.0,
    )


def test_broken_thesis_accelerates_to_cap(monkeypatch):
    """A BROKEN thesis (critical) drops the routine glide pace and accelerates the
    trim to the cap NOW — the full over-cap amount, not one quarter's tranche."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(
        nps, "_load_nvda_thesis_flags",
        lambda *a, **k: [{"kind": "thesis_monitor_broken", "severity": "critical"}],
    )

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.status == "sell_due"
    assert v.category == "thesis-break"
    # Accelerated: the FULL over-cap sale to the cap, not the /8 policy tranche.
    assert v.tranche_nis == 4_000_000.0
    assert "thesis" in v.headline.lower()


def test_weakened_thesis_holds_glide_pace_with_review_note(monkeypatch):
    """A WEAKENED thesis does NOT resize the trim — it holds the glide pace and
    surfaces a review note (no acting on a soft single signal)."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(
        nps, "_load_nvda_thesis_flags",
        lambda *a, **k: [{"kind": "thesis_monitor_weakened", "severity": "warning"}],
    )

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.category == "policy"
    assert v.tranche_nis == 500_000.0  # unchanged glide pace
    assert any("weakened" in n.lower() or "review" in n.lower() for n in v.notes)


def test_no_thesis_flags_is_policy(monkeypatch):
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.category == "policy"
    assert v.tranche_nis == 500_000.0


def test_risk_budget_drives_when_it_exceeds_the_policy_pace(monkeypatch):
    """When a plausible NVDA drawdown would breach the FI floor, the risk-budget
    sale (sized to restore the floor) exceeds the routine glide tranche and drives
    the recommendation."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])
    # W=10M, NVDA=6M, floor=8M, 40% shock => risk-budget sale = 1M > 500k policy.
    monkeypatch.setattr(
        nps, "_resolve_risk_budget_inputs",
        lambda *a, **k: (10_000_000.0, 6_000_000.0, 8_000_000.0),
    )

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.category == "risk-budget"
    assert abs(v.tranche_nis - 1_000_000.0) <= 1.0
    assert "retirement" in v.headline.lower() or "floor" in v.headline.lower()


def test_thesis_break_beats_risk_budget_when_larger(monkeypatch):
    """Precedence + magnitude: a broken thesis (accelerate fully to cap = 4M) beats
    a smaller risk-budget sale (1M)."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(
        nps, "_load_nvda_thesis_flags",
        lambda *a, **k: [{"kind": "thesis_monitor_broken", "severity": "critical"}],
    )
    monkeypatch.setattr(
        nps, "_resolve_risk_budget_inputs",
        lambda *a, **k: (10_000_000.0, 6_000_000.0, 8_000_000.0),
    )

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.category == "thesis-break"
    assert v.tranche_nis == 4_000_000.0


def test_risk_budget_fires_even_when_within_cap(monkeypatch):
    """Risk-budget is independent of the concentration cap: an UNDER-cap NVDA
    position that a 40% shock would push below the FI floor still triggers a sell
    (compute_breach_tranche returns None, but we must not stop at no_action)."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: None)
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])
    # W=9M, NVDA=1M (small — within cap), floor=8.8M, 40% shock => risk sale = 500k.
    monkeypatch.setattr(
        nps, "_resolve_risk_budget_inputs",
        lambda *a, **k: (9_000_000.0, 1_000_000.0, 8_800_000.0),
    )

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.status == "sell_due"
    assert v.category == "risk-budget"
    assert abs(v.tranche_nis - 500_000.0) <= 1.0


def test_within_cap_and_no_risk_breach_is_no_action(monkeypatch):
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: None)
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])
    monkeypatch.setattr(nps, "_resolve_risk_budget_inputs", lambda *a, **k: None)

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)
    assert v.status == "no_action"


def test_catchup_drives_when_behind_schedule(monkeypatch):
    """Behind the glide schedule (fewer tranches executed than waypoints due) → the
    catch-up sale (missed tranches) drives when it exceeds the single policy pace."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])
    monkeypatch.setattr(nps, "_resolve_risk_budget_inputs", lambda *a, **k: None)
    # 3 waypoints due, 1 executed => 2 missed * 500k = 1M > 500k policy pace.
    monkeypatch.setattr(nps, "_resolve_catchup_inputs", lambda *a, **k: (3, 1))

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)
    assert v.category == "catch-up"
    assert v.tranche_nis == 1_000_000.0


def test_funnel_concurrence_adds_a_note(monkeypatch):
    """When the daily decision funnel also flags NVDA to reduce, the unified sell
    surfaces that concurrence (the bridge) — strengthening the case, not competing."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])
    monkeypatch.setattr(nps, "_resolve_risk_budget_inputs", lambda *a, **k: None)
    monkeypatch.setattr(nps, "_funnel_concurs_reduce", lambda *a, **k: True)

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)
    assert any("funnel" in n.lower() for n in v.notes)


def test_no_risk_budget_inputs_falls_back_to_policy(monkeypatch):
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: [])
    monkeypatch.setattr(nps, "_resolve_risk_budget_inputs", lambda *a, **k: None)

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.category == "policy"
    assert v.tranche_nis == 500_000.0
    # A genuine "no flags" read carries no unverified caveat.
    assert not any("verif" in n.lower() for n in v.notes)


def test_thesis_load_failure_is_flagged_unverified_not_silently_clean(monkeypatch):
    """If the thesis-flag read FAILS, 'unknown' must not masquerade as 'clear': hold
    the policy pace (never over-sell on no signal) but SURFACE that the thesis state
    could not be verified — so a real break isn't silently downgraded unnoticed."""
    monkeypatch.setattr(nps, "compute_breach_tranche", lambda *a, **k: _tranche())
    monkeypatch.setattr(nps, "_load_nvda_thesis_flags", lambda *a, **k: None)

    v = assess_nvda_policy_sell(session=object(), user_id="ariel", today=_TODAY)

    assert v.category == "policy"
    assert v.tranche_nis == 500_000.0
    assert any("verif" in n.lower() for n in v.notes)
