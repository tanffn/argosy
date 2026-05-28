"""Tests for the ValueWithRationale citation primitive (Wave 0 · gap-foundation)."""
import pytest

from argosy.services.retirement.citations import (
    DERIVED,
    ValueWithRationale,
    as_dict,
)


class TestValueWithRationale:
    def test_minimal_construction(self):
        v = ValueWithRationale(
            value=42.0,
            unit="NIS/mo",
            source_id="bituach_leumi_old_age_2026",
            rationale="Single-person base rate at age 67.",
        )
        assert v.value == 42.0
        assert v.unit == "NIS/mo"
        assert v.source_id == "bituach_leumi_old_age_2026"
        assert v.alternatives_considered == []
        assert v.confidence == "medium"
        assert v.as_of_date is None
        assert v.freshness_warning is None

    def test_derived_marker(self):
        v = ValueWithRationale(
            value=0.087,
            unit="probability",
            source_id=DERIVED,
            rationale="Computed from MC: 87 failed paths / 1000.",
        )
        assert v.source_id is None

    def test_as_dict_strips_none_freshness(self):
        v = ValueWithRationale(value=200, unit="ratio", source_id="x", rationale="y")
        d = as_dict(v)
        assert "freshness_warning" not in d  # None values stripped for compact JSON
        assert d["value"] == 200

    def test_as_dict_preserves_freshness_when_set(self):
        v = ValueWithRationale(
            value=200,
            unit="ratio",
            source_id="x",
            rationale="y",
            freshness_warning="Verify with your fund.",
        )
        d = as_dict(v)
        assert d["freshness_warning"] == "Verify with your fund."

    def test_as_dict_strips_empty_alternatives_list(self):
        v = ValueWithRationale(value=1, unit="x", source_id="y", rationale="z")
        d = as_dict(v)
        assert "alternatives_considered" not in d

    def test_as_dict_preserves_populated_alternatives_list(self):
        v = ValueWithRationale(
            value=1,
            unit="x",
            source_id="y",
            rationale="z",
            alternatives_considered=["a", "b"],
        )
        d = as_dict(v)
        assert d["alternatives_considered"] == ["a", "b"]

    def test_as_dict_preserves_none_value_and_source_id(self):
        # value=None means "not enough data"; source_id=None means derived.
        # Both are semantic Nones and must survive serialization.
        v = ValueWithRationale(
            value=None,
            unit="",
            source_id=None,
            rationale="No data available yet",
        )
        d = as_dict(v)
        assert "value" in d and d["value"] is None
        assert "source_id" in d and d["source_id"] is None

    def test_confidence_must_be_one_of_three(self):
        with pytest.raises(ValueError, match="confidence must be one of"):
            ValueWithRationale(
                value=1,
                unit="x",
                source_id=None,
                rationale="y",
                confidence="invalid",  # type: ignore[arg-type]
            )

    def test_alternatives_considered_default_is_independent(self):
        # If the default were a shared list, mutating one would affect the
        # other. The dataclass uses field(default_factory=list) to avoid this.
        v1 = ValueWithRationale(value=1, unit="x", source_id=None, rationale="y")
        v2 = ValueWithRationale(value=2, unit="x", source_id=None, rationale="y")
        v1.alternatives_considered.append("foo")
        assert v2.alternatives_considered == []
