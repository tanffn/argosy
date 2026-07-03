"""Tests for argosy/quality/plan_node_meta.py — written BEFORE implementation (TDD).

These tests encode the contract that node_meta() must satisfy:
  - known dotted-prefix families resolve to their expected owner_domain + policy_axis
  - a small set of plan-identity keys flag plan_identity_axis=True
  - unknown keys fall through to the conservative default (no explicit owner)
  - validate_owner_coverage returns unmapped keys from a fake graph
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from argosy.quality.plan_node_meta import (
    AuthoringMode,
    HardVerdictSeverity,
    NodeMeta,
    PolicyAxis,
    node_meta,
    validate_owner_coverage,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_node(key: str, kind: str = "derived") -> SimpleNamespace:
    return SimpleNamespace(key=key, kind=SimpleNamespace(value=kind))


def _fake_graph(keys: list[str], kind: str = "derived"):
    nodes = {k: _fake_node(k, kind) for k in keys}
    return SimpleNamespace(
        keys=lambda: list(nodes),
        get=lambda k: nodes[k],
    )


# ---------------------------------------------------------------------------
# PolicyAxis / AuthoringMode / HardVerdictSeverity enum values exist
# ---------------------------------------------------------------------------

class TestEnums:
    def test_policy_axes_exist(self):
        assert set(PolicyAxis) >= {
            PolicyAxis.risk, PolicyAxis.withdrawal, PolicyAxis.tax,
            PolicyAxis.allocation, PolicyAxis.estate, PolicyAxis.concentration,
            PolicyAxis.execution, PolicyAxis.prose,
        }

    def test_authoring_modes_exist(self):
        assert set(AuthoringMode) >= {
            AuthoringMode.deterministic,
            AuthoringMode.owner_authored,
            AuthoringMode.synthesis_authored,
        }

    def test_hard_verdict_severities_exist(self):
        assert set(HardVerdictSeverity) >= {
            HardVerdictSeverity.cosmetic,
            HardVerdictSeverity.localized,
            HardVerdictSeverity.plan_basis,
        }


# ---------------------------------------------------------------------------
# NodeMeta is a frozen dataclass
# ---------------------------------------------------------------------------

class TestNodeMetaDataclass:
    def test_frozen(self):
        meta = node_meta("retirement.swr")
        with pytest.raises((AttributeError, TypeError)):
            meta.owner_domain = "x"  # type: ignore[misc]

    def test_fields_present(self):
        meta = node_meta("retirement.swr")
        assert hasattr(meta, "owner_domain")
        assert hasattr(meta, "policy_axis")
        assert hasattr(meta, "authoring_mode")
        assert hasattr(meta, "boundary_id")
        assert hasattr(meta, "rebuild_boundary")
        assert hasattr(meta, "plan_identity_axis")
        assert hasattr(meta, "hard_verdict_severity")


# ---------------------------------------------------------------------------
# Known-prefix routing
# ---------------------------------------------------------------------------

class TestKnownPrefixes:
    @pytest.mark.parametrize("key", [
        "retirement.swr",
        "retirement.fi_target",
        "retirement.required_real_yield",
        "spend.monthly_baseline",
    ])
    def test_retirement_and_spend_are_withdrawal(self, key):
        meta = node_meta(key)
        assert meta.policy_axis is PolicyAxis.withdrawal, f"{key} should be withdrawal axis"

    @pytest.mark.parametrize("key", [
        "concentration.nvda_weight",
        "concentration.single_stock_cap",
    ])
    def test_concentration_axis(self, key):
        meta = node_meta(key)
        assert meta.policy_axis is PolicyAxis.concentration, f"{key} should be concentration axis"

    @pytest.mark.parametrize("key", [
        "portfolio.equity_weight",
        "allocation.target_weights",
        "sleeve.us_large",
        "sleeve_us.target",
    ])
    def test_allocation_axis(self, key):
        meta = node_meta(key)
        assert meta.policy_axis is PolicyAxis.allocation, f"{key} should be allocation axis"

    @pytest.mark.parametrize("key", [
        "fx.nis_usd_rate",
        "fx.realised_drift",
    ])
    def test_fx_axis(self, key):
        meta = node_meta(key)
        assert meta.policy_axis in (PolicyAxis.tax, PolicyAxis.estate), (
            f"{key} should be tax or estate axis, got {meta.policy_axis}"
        )

    @pytest.mark.parametrize("key", [
        "savings.rsu_monthly",
        "savings.net_salary",
    ])
    def test_savings_axis(self, key):
        meta = node_meta(key)
        # savings feeds forward into withdrawal projections — either withdrawal or execution
        assert meta.policy_axis in (PolicyAxis.withdrawal, PolicyAxis.execution, PolicyAxis.allocation), (
            f"{key}: unexpected axis {meta.policy_axis}"
        )


# ---------------------------------------------------------------------------
# Owner domain matches the _OWNER_BY_PREFIX convention
# ---------------------------------------------------------------------------

class TestOwnerDomain:
    def test_retirement_owner(self):
        assert node_meta("retirement.swr").owner_domain == "withdrawal_sequencer"

    def test_spend_owner(self):
        assert node_meta("spend.monthly_baseline").owner_domain == "household_budget"

    def test_concentration_owner(self):
        assert node_meta("concentration.nvda_weight").owner_domain == "concentration"

    def test_fx_owner(self):
        assert node_meta("fx.nis_usd_rate").owner_domain == "fx"

    def test_savings_owner(self):
        assert node_meta("savings.rsu_monthly").owner_domain == "equity_comp"


# ---------------------------------------------------------------------------
# plan_identity_axis keys
# ---------------------------------------------------------------------------

class TestPlanIdentityAxis:
    @pytest.mark.parametrize("key", [
        "retirement.risk_posture",
        "retirement.objective",
        "retirement.tax_residency",
    ])
    def test_plan_identity_keys_are_flagged(self, key):
        meta = node_meta(key)
        assert meta.plan_identity_axis is True, f"{key} must be plan_identity_axis=True"

    @pytest.mark.parametrize("key", [
        "retirement.swr",
        "spend.monthly_baseline",
        "concentration.nvda_weight",
        "portfolio.equity_weight",
    ])
    def test_non_identity_keys_are_false(self, key):
        meta = node_meta(key)
        assert meta.plan_identity_axis is False, f"{key} must NOT be plan_identity_axis"


# ---------------------------------------------------------------------------
# Conservative default for unknown keys
# ---------------------------------------------------------------------------

class TestUnknownKeyFallback:
    def test_unknown_key_gets_default_owner(self):
        meta = node_meta("zz_unknown.some_value")
        # default owner = _DEFAULT_OWNER_ROLE from ladder_participants
        assert meta.owner_domain == "withdrawal_sequencer"

    def test_unknown_key_authoring_mode_is_synthesis(self):
        meta = node_meta("zz_unknown.some_value")
        assert meta.authoring_mode is AuthoringMode.synthesis_authored

    def test_unknown_key_plan_identity_false(self):
        meta = node_meta("zz_unknown.some_value")
        assert meta.plan_identity_axis is False

    def test_unknown_key_boundary_id_is_default(self):
        meta = node_meta("zz_unknown.some_value")
        # should have SOME boundary_id (non-empty string) so callers can route
        assert isinstance(meta.boundary_id, str) and meta.boundary_id


# ---------------------------------------------------------------------------
# validate_owner_coverage
# ---------------------------------------------------------------------------

class TestValidateOwnerCoverage:
    def test_all_mapped_returns_empty(self):
        g = _fake_graph(["retirement.swr", "spend.monthly_baseline", "concentration.cap"])
        unmapped = validate_owner_coverage(g)
        assert unmapped == []

    def test_unknown_key_is_flagged(self):
        g = _fake_graph(["retirement.swr", "zz_unknown.foo"])
        unmapped = validate_owner_coverage(g)
        assert "zz_unknown.foo" in unmapped

    def test_input_nodes_excluded_from_coverage_check(self):
        """INPUT nodes have no bounded owner by convention (they ARE the source);
        only mutable/authoring nodes need a bounded owner."""
        g = _fake_graph(["zz_unknown.raw_input"], kind="input")
        unmapped = validate_owner_coverage(g)
        assert "zz_unknown.raw_input" not in unmapped

    def test_multiple_unmapped_all_returned(self):
        g = _fake_graph([
            "retirement.swr",
            "mystery.alpha",
            "mystery.beta",
        ])
        unmapped = validate_owner_coverage(g)
        assert set(unmapped) == {"mystery.alpha", "mystery.beta"}

    def test_only_non_input_nodes_checked(self):
        """Mixed graph: INPUT nodes skipped, DERIVED unmapped nodes flagged."""
        nodes = {
            "retirement.swr": _fake_node("retirement.swr", "derived"),
            "raw.ingest": _fake_node("raw.ingest", "input"),
            "orphan.derived": _fake_node("orphan.derived", "derived"),
        }
        g = SimpleNamespace(
            keys=lambda: list(nodes),
            get=lambda k: nodes[k],
        )
        unmapped = validate_owner_coverage(g)
        assert "orphan.derived" in unmapped
        assert "raw.ingest" not in unmapped
        assert "retirement.swr" not in unmapped
