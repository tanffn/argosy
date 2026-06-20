"""The synth assumption-ledger A7 retention row must render the two DISTINCT
canonical retention rates (at-vest ordinary / capital-track Section-102), never a
single conflated number, and never a hardcoded value on a cold cache."""
from __future__ import annotations

from argosy.orchestrator.flows.plan_synthesis.render import _ledger_rows_with_manifest


class _RV:
    def __init__(self, value, status="resolved"):
        self.value = value
        self.status = status


class _Resolved:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key):
        return self._m.get(key)


def test_a7_retention_row_split_from_canonical_rates():
    resolved = _Resolved({
        "tax.retention_at_vest_pct": _RV(0.50),
        "tax.retention_capital_track_pct": _RV(0.70),
    })
    rows = _ledger_rows_with_manifest(resolved)
    a7 = {r["id"]: r for r in rows}["A7"]
    assert "50%" in a7["value"]
    assert "70%" in a7["value"]
    assert "at-vest" in a7["value"].lower()
    assert "capital" in a7["value"].lower()


def test_a7_cold_cache_is_pending_not_conflated():
    rows = _ledger_rows_with_manifest(None)
    a7 = {r["id"]: r for r in rows}["A7"]
    assert "47%" not in a7["value"]
    assert "pending" in a7["value"].lower()


def test_a7_invalid_rate_leaves_row_pending():
    """A pending / garbage resolver value (0.0, >1, None) must NOT hydrate A7 with
    a false rate — the row stays at the cold-cache pending default."""
    resolved = _Resolved({
        "tax.retention_at_vest_pct": _RV(0.0),
        "tax.retention_capital_track_pct": _RV(0.70),
    })
    rows = _ledger_rows_with_manifest(resolved)
    a7 = {r["id"]: r for r in rows}["A7"]
    assert "pending" in a7["value"].lower()
    assert "0%" not in a7["value"]
