"""Fix C — the conflict-gate ruin boundaries are IPS-owned, not code constants.

The stress PARAMETERS (sigma/inflation) are the scenario; the P(ruin) FAIL/WARN
boundaries are a risk-tolerance JUDGMENT that belongs to the plan/IPS. They now
resolve through the hybrid reference (per-user override -> shipped provisional
default), with the prior in-code values kept only as a fail-safe fallback.
"""
import argosy.services.retirement.reference as R
from argosy.services.retirement.safety_gates import (
    _FALLBACK_CONFLICT_RUIN_FAIL,
    _FALLBACK_CONFLICT_RUIN_WARN,
    _conflict_ruin_thresholds,
)


def test_shipped_default_preserves_prior_boundaries(monkeypatch):
    # No user override -> shipped YAML -> the historical 0.50 / 0.30 (behavior
    # preserved; only the value's HOME moved to the plan/IPS reference).
    monkeypatch.setattr(R, "_load_user_override", lambda *a, **k: None)
    fail, warn = _conflict_ruin_thresholds("ariel", session=None)
    assert (fail, warn) == (0.50, 0.30)


def test_per_user_override_wins(monkeypatch):
    # A stricter IPS risk tolerance from identity_yaml overrides the shipped default.
    overrides = {
        "risk.conflict_ruin_fail_threshold": {"value": 0.40, "source": "ips"},
        "risk.conflict_ruin_warn_threshold": {"value": 0.20, "source": "ips"},
    }
    monkeypatch.setattr(
        R, "_load_user_override", lambda s, u, key: overrides.get(key)
    )
    fail, warn = _conflict_ruin_thresholds("ariel", session=None)
    assert (fail, warn) == (0.40, 0.20)


def test_warn_clamped_below_fail(monkeypatch):
    # A mis-set pair (warn > fail) must not invert the verdict ordering.
    overrides = {
        "risk.conflict_ruin_fail_threshold": {"value": 0.30, "source": "ips"},
        "risk.conflict_ruin_warn_threshold": {"value": 0.60, "source": "ips"},
    }
    monkeypatch.setattr(
        R, "_load_user_override", lambda s, u, key: overrides.get(key)
    )
    fail, warn = _conflict_ruin_thresholds("ariel", session=None)
    assert fail == 0.30
    assert warn == 0.30  # clamped down to fail


def test_unresolvable_key_falls_back_to_in_code_constants(monkeypatch):
    # If the reference can't resolve (missing key), fall back to the prior
    # constants — a fail-safe default, never a harder boundary.
    monkeypatch.setattr(R, "_load_user_override", lambda *a, **k: None)

    def _boom(key, **kw):
        raise R.ResolveError(key)

    monkeypatch.setattr(R, "resolve", _boom)
    fail, warn = _conflict_ruin_thresholds("ariel", session=None)
    assert fail == _FALLBACK_CONFLICT_RUIN_FAIL
    assert warn == _FALLBACK_CONFLICT_RUIN_WARN
