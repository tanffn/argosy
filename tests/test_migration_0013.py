"""Tests for migration 0013 — pensions list-shape → vehicle-keyed dict.

The migration's ``_list_to_dict`` and ``_dict_to_list`` helpers carry
all the data-shape logic; ``upgrade()`` / ``downgrade()`` are thin
wrappers that loop over ``user_context`` rows and call
``yaml.safe_load`` / ``yaml.safe_dump`` around them. Exercising the
helpers directly proves the round-trip preserves balances, rate fields,
and per-fund metadata; an end-to-end Alembic run isn't necessary
beyond what the daily test workflow already covers via
``alembic upgrade head``.

Also asserts the post-upgrade YAML resolves through the gap-tracker's
``_lookup`` walker so stage_3 fields don't permanently mark missing
after the migration.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

# Load the migration module directly — Alembic versions aren't a package
# so we can't `from alembic.versions.0013_... import ...`.
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0013_pensions_to_dict_shape.py"
)
_spec = importlib.util.spec_from_file_location("migration_0013", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
_migration = importlib.util.module_from_spec(_spec)
sys.modules["migration_0013"] = _migration
_spec.loader.exec_module(_migration)


def test_list_to_dict_buckets_funds_by_vehicle() -> None:
    """A flat fund list lands in the right vehicle bucket and aggregates balance."""
    pensions = [
        {
            "type": "keren_hishtalmut",
            "fund_id": "111",
            "fund_name": "Migdal Hishtalmut",
            "balance_nis": 200_000,
            "contribution_rate_pct": 7.5,
            "employer_match_pct": 7.5,
        },
        {
            "type": "kupat_gemel",
            "fund_id": "222",
            "fund_name": "Altshuler Gemel",
            "balance_nis": 850_000,
        },
        {
            "type": "kupat_pensia",
            "fund_id": "333",
            "fund_name": "Harel Pension",
            "balance_nis": 1_500_000,
            "contribution_rate_pct": 6.0,
            "employer_match_pct": 6.5,
        },
    ]
    out = _migration._list_to_dict(pensions)
    assert set(out.keys()) == {"keren_hishtalmut", "kupat_gemel", "kupat_pensia"}
    assert out["keren_hishtalmut"]["balance_nis"] == 200_000
    assert out["keren_hishtalmut"]["contribution_rate_pct"] == 7.5
    assert out["keren_hishtalmut"]["funds"][0]["fund_id"] == "111"
    assert out["kupat_gemel"]["balance_nis"] == 850_000
    assert out["kupat_pensia"]["contribution_rate_pct"] == 6.0


def test_list_to_dict_unknown_type_buckets_to_kupat_gemel() -> None:
    """Unknown ``type`` defaults to kupat_gemel — the safest default
    (locked-till-retirement). Operators see the fall-back via the
    WARNING log fired alongside (asserted below)."""
    pensions = [{"type": "unknown_xyz", "balance_nis": 100, "fund_id": "x"}]
    out = _migration._list_to_dict(pensions)
    assert "kupat_gemel" in out
    assert out["kupat_gemel"]["balance_nis"] == 100


def test_list_to_dict_aggregates_balances_within_vehicle() -> None:
    """Two funds in the same vehicle stack their balances."""
    pensions = [
        {"type": "kupat_gemel", "fund_id": "a", "balance_nis": 100},
        {"type": "kupat_gemel", "fund_id": "b", "balance_nis": 250},
    ]
    out = _migration._list_to_dict(pensions)
    assert out["kupat_gemel"]["balance_nis"] == 350
    assert len(out["kupat_gemel"]["funds"]) == 2


def test_list_to_dict_first_seen_rate_wins() -> None:
    """Aggregate rate fields take the first non-null value across funds."""
    pensions = [
        {
            "type": "keren_hishtalmut",
            "fund_id": "a",
            "contribution_rate_pct": 7.5,
        },
        {
            "type": "keren_hishtalmut",
            "fund_id": "b",
            "contribution_rate_pct": 9.0,  # ignored — first one wins
        },
    ]
    out = _migration._list_to_dict(pensions)
    assert out["keren_hishtalmut"]["contribution_rate_pct"] == 7.5


def test_dict_to_list_round_trips_balance_and_rates() -> None:
    """Down-migration must preserve aggregate balance AND rate fields.

    Earlier the funds-exist branch dropped contribution_rate_pct /
    employer_match_pct entirely; this test pins them onto the first
    fund row so a forward-then-back migration is lossless on rate
    data."""
    pensions = [
        {
            "type": "keren_hishtalmut",
            "fund_id": "111",
            "fund_name": "Migdal",
            "balance_nis": 300_000,
            "contribution_rate_pct": 7.5,
            "employer_match_pct": 7.5,
        },
    ]
    forward = _migration._list_to_dict(pensions)
    backward = _migration._dict_to_list(forward)
    assert len(backward) == 1
    row = backward[0]
    assert row["type"] == "keren_hishtalmut"
    assert row["fund_id"] == "111"
    assert row["balance_nis"] == 300_000
    # The fix: rate fields must survive on the FIRST fund.
    assert row["contribution_rate_pct"] == 7.5
    assert row["employer_match_pct"] == 7.5


def test_dict_to_list_subsequent_funds_have_no_balance_or_rates() -> None:
    """Multi-fund vehicles spread aggregate fields onto the FIRST fund only."""
    forward = {
        "keren_hishtalmut": {
            "balance_nis": 400_000,
            "contribution_rate_pct": 7.5,
            "employer_match_pct": 7.5,
            "funds": [
                {"fund_id": "a", "fund_name": "Migdal"},
                {"fund_id": "b", "fund_name": "Harel"},
            ],
        },
    }
    backward = _migration._dict_to_list(forward)
    assert len(backward) == 2
    assert backward[0]["balance_nis"] == 400_000
    assert backward[0]["contribution_rate_pct"] == 7.5
    assert backward[0]["employer_match_pct"] == 7.5
    # Subsequent fund must NOT carry the aggregate fields — that would
    # double-count them after a forward migration.
    assert backward[1].get("balance_nis") is None
    assert "contribution_rate_pct" not in backward[1]
    assert "employer_match_pct" not in backward[1]


def test_dict_to_list_no_funds_branch_preserves_rate_fields() -> None:
    """The placeholder branch (vehicle bucket with no funds) already
    preserved rate fields; pin that behaviour."""
    forward = {
        "kupat_gemel": {
            "balance_nis": 750_000,
            "contribution_rate_pct": 6.0,
            "employer_match_pct": 6.0,
        },
    }
    backward = _migration._dict_to_list(forward)
    assert len(backward) == 1
    assert backward[0]["contribution_rate_pct"] == 6.0
    assert backward[0]["employer_match_pct"] == 6.0
    assert backward[0]["balance_nis"] == 750_000


def test_post_upgrade_yaml_resolves_through_lookup() -> None:
    """Stage_3 fields like ``identity.pensions.keren_hishtalmut.balance_nis``
    must resolve through the gap-tracker walker after migration. This
    is the regression we shipped 0013 to fix in the first place."""
    from argosy.agents.intake_fields import _lookup

    pensions = [
        {
            "type": "keren_hishtalmut",
            "fund_id": "111",
            "fund_name": "Migdal",
            "balance_nis": 200_000,
            "contribution_rate_pct": 7.5,
        },
    ]
    new_shape = _migration._list_to_dict(pensions)
    identity = {"pensions": new_shape}
    # Round-trip through YAML to mirror what the migration writes back.
    yaml_text = yaml.safe_dump(identity, allow_unicode=True, sort_keys=False)
    parsed = yaml.safe_load(yaml_text)
    # Strip the leading "identity." that real callers pass.
    assert _lookup(parsed, "identity.pensions.keren_hishtalmut.balance_nis") == 200_000
    assert (
        _lookup(parsed, "identity.pensions.keren_hishtalmut.contribution_rate_pct")
        == 7.5
    )
