"""Gap tracker tests — fresh / stale / missing classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argosy.agents.gap_tracker import (
    STAGE_FIELDS,
    STAGE_REQUIRED_FIELDS,
    all_fields,
    field_by_path,
    gap_status,
    gaps_for_prompt,
    pick_gap_driven_target,
)


def test_stage_required_fields_matches_stage_fields_paths() -> None:
    """The legacy `STAGE_REQUIRED_FIELDS` shape must stay byte-for-byte
    compatible with the new `STAGE_FIELDS` projection — the existing
    /api/intake/turn auto-advance path still imports the older symbol."""
    for stage, fields in STAGE_FIELDS.items():
        assert STAGE_REQUIRED_FIELDS[stage] == [f.path for f in fields]
    # And no surprise stages.
    assert set(STAGE_REQUIRED_FIELDS.keys()) == set(STAGE_FIELDS.keys())


def test_all_fields_dedupes_and_orders_by_stage() -> None:
    fs = all_fields()
    paths = [f.path for f in fs]
    assert paths.count("identity.tax_residency") == 1
    # Stage_1 fields appear before stage_3 fields.
    s1_idx = paths.index("identity.tax_residency")
    s3_idx = paths.index("identity.bank_accounts")
    assert s1_idx < s3_idx


def test_field_by_path_returns_correct_spec() -> None:
    f = field_by_path("identity.tax_residency")
    assert f is not None
    assert f.label == "Tax residency"
    assert f.section == "identity"
    assert f.freshness == "one_shot"

    assert field_by_path("nonsense.path") is None


def test_gap_status_all_missing_when_yamls_empty() -> None:
    s = gap_status(
        identity_yaml="", goals_yaml="", constraints_yaml=""
    )
    assert len(s.fresh) == 0
    assert len(s.stale) == 0
    # Every catalogued field is missing.
    assert {f.path for f in s.missing} == {f.path for f in all_fields()}


def test_gap_status_marks_answered_no_timestamp_as_fresh() -> None:
    """Answered field with no audit timestamp -> fresh (we don't go stale
    unless we can prove the value is old)."""
    identity = "tax_residency: israel\n"
    s = gap_status(
        identity_yaml=identity,
        goals_yaml="",
        constraints_yaml="",
        last_updated_per_field=None,  # nothing pinned
    )
    fresh_paths = {f.path for f in s.fresh}
    assert "identity.tax_residency" in fresh_paths


def test_gap_status_marks_field_stale_after_window() -> None:
    """A monthly-freshness field whose last_updated is > 33 days ago
    must land in `stale`, not `fresh`."""
    now = datetime(2026, 5, 1, tzinfo=UTC)
    ago = now - timedelta(days=60)
    identity = "bank_accounts:\n  - {bank: leumi, balance_nis: 100000}\n"
    s = gap_status(
        identity_yaml=identity,
        goals_yaml="",
        constraints_yaml="",
        last_updated_per_field={"identity.bank_accounts": ago},
        now=now,
    )
    stale_paths = {f.path for f, _ in s.stale}
    assert "identity.bank_accounts" in stale_paths
    fresh_paths = {f.path for f in s.fresh}
    assert "identity.bank_accounts" not in fresh_paths


def test_gap_status_one_shot_field_never_stales() -> None:
    """tax_residency is one_shot — even with a 5-year-old timestamp it
    stays fresh (these don't change unless the user reports a life event)."""
    now = datetime(2026, 5, 1, tzinfo=UTC)
    long_ago = now - timedelta(days=5 * 365)
    s = gap_status(
        identity_yaml="tax_residency: israel\n",
        goals_yaml="",
        constraints_yaml="",
        last_updated_per_field={"identity.tax_residency": long_ago},
        now=now,
    )
    assert any(f.path == "identity.tax_residency" for f in s.fresh)
    assert all(f.path != "identity.tax_residency" for f, _ in s.stale)


def test_gap_status_recent_monthly_field_is_fresh() -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    recent = now - timedelta(days=10)
    s = gap_status(
        identity_yaml="bank_accounts:\n  - {bank: x, balance_nis: 1}\n",
        goals_yaml="",
        constraints_yaml="",
        last_updated_per_field={"identity.bank_accounts": recent},
        now=now,
    )
    assert any(f.path == "identity.bank_accounts" for f in s.fresh)


def test_gap_status_skips_spouse_when_unmarried() -> None:
    """When marital_status is 'single', spouse fields auto-resolve as
    fresh (we don't keep nagging unmarried users for spouse data)."""
    s = gap_status(
        identity_yaml="marital_status: single\ntax_residency: israel\n",
        goals_yaml="",
        constraints_yaml="",
    )
    fresh_paths = {f.path for f in s.fresh}
    assert "identity.spouse_citizenship" in fresh_paths
    assert "identity.spouse_tax_residency" in fresh_paths


def test_gaps_for_prompt_combines_missing_and_stale() -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    s = gap_status(
        identity_yaml="bank_accounts:\n  - {bank: x, balance_nis: 1}\ntax_residency: israel\n",
        goals_yaml="",
        constraints_yaml="",
        last_updated_per_field={"identity.bank_accounts": now - timedelta(days=120)},
        now=now,
    )
    answered, still_needed = gaps_for_prompt(s)
    # Bank balance is stale -> must appear in still_needed.
    assert "identity.bank_accounts" in still_needed
    # tax_residency is one-shot fresh.
    assert "identity.tax_residency" in answered


def test_pick_gap_driven_target_prefers_missing_over_stale() -> None:
    """When both a missing and a stale gap exist, missing should win."""
    now = datetime(2026, 5, 1, tzinfo=UTC)
    # tax_residency missing; bank_accounts stale.
    s = gap_status(
        identity_yaml="bank_accounts:\n  - {bank: x, balance_nis: 1}\n",
        goals_yaml="",
        constraints_yaml="",
        last_updated_per_field={"identity.bank_accounts": now - timedelta(days=120)},
        now=now,
    )
    target = pick_gap_driven_target(s)
    assert target is not None
    # tax_residency is priority 1 missing -> should be chosen.
    assert target.path == "identity.tax_residency"


def test_pick_gap_driven_target_returns_none_when_clean() -> None:
    """All catalogued fields filled and fresh -> no target."""
    # Build a YAML that touches every field. Easiest: assert via a
    # synthesized status with empty missing and stale.
    from argosy.agents.gap_tracker import GapStatus

    s = GapStatus(fresh=list(all_fields()), missing=[], stale=[])
    assert pick_gap_driven_target(s) is None


def test_freshness_window_quarterly_boundary() -> None:
    """A quarterly field at exactly 90 days is still fresh; at 100 days is
    stale (window is 95 days per gap_tracker._FRESHNESS_WINDOWS)."""
    # We don't have a quarterly field in the catalog at the moment, but
    # build one synthetically and exercise _is_stale via the public API.
    from argosy.agents.gap_tracker import FieldSpec, _is_stale

    spec = FieldSpec(
        path="identity.dummy",
        label="dummy",
        section="identity",
        freshness="quarterly",
        priority=2,
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    assert _is_stale(spec, now - timedelta(days=90), now) is False
    assert _is_stale(spec, now - timedelta(days=100), now) is True
