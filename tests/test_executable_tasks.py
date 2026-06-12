"""Phase 1b — the hard reconciliation gate: an agent task may only wrap a
deterministic 1a candidate; the task set must cover the candidate set exactly
(identity + uniqueness + 1:1 coverage), so the agent invents no numbers."""
from __future__ import annotations

import pytest

from argosy.services.allocation_engine import AllocationCandidate, AllocationLeg
from argosy.services.executable_tasks import ExecutableTask, reconcile_or_raise


def _cand(sym, usd):
    return AllocationCandidate(kind="BUY", horizon="now",
        legs=(AllocationLeg(side="BUY", symbol=sym, account_id="ibkr",
              currency="USD", notional_usd=usd, funding_source="cash"),))


def _task(cand, seq=1):
    return ExecutableTask(seq=seq, candidate=cand, horizon="now", pace="lump",
                          pace_rationale="", rationale="buy core", cites=())


def test_reconcile_passes_when_totals_match():
    c = _cand("CSPX", 1000.0)
    reconcile_or_raise([_task(c)], [c])  # no raise


def test_reconcile_raises_on_invented_number():
    c = _cand("CSPX", 1000.0)
    bad = _task(_cand("CSPX", 9999.0))
    with pytest.raises(ValueError):
        reconcile_or_raise([bad], [c])


def test_reconcile_raises_on_same_dollar_different_ticker():
    """notional-only matching is NOT enough — a same-$ different instrument must
    be rejected (identity fingerprint)."""
    c = _cand("CSPX", 1000.0)
    swapped = _task(_cand("VUAA", 1000.0))
    with pytest.raises(ValueError):
        reconcile_or_raise([swapped], [c])


def test_reconcile_raises_on_dropped_candidate():
    c1, c2 = _cand("CSPX", 1000.0), _cand("FUSA", 500.0)
    with pytest.raises(ValueError):
        reconcile_or_raise([_task(c1)], [c1, c2])  # c2 dropped


def test_reconcile_raises_on_duplicated_candidate():
    c = _cand("CSPX", 1000.0)
    with pytest.raises(ValueError):
        reconcile_or_raise([_task(c, 1), _task(c, 2)], [c])  # c used twice


def test_reconcile_rejects_modified_tax_and_quantity():
    """codex 1b #2: the gate's identity must include the material numeric fields
    (est_tax_nis, surtax flag, leg quantity), not just notional — else a task
    that altered tax/quantity would falsely reconcile."""
    real = AllocationCandidate(kind="TRIM", horizon="this_quarter",
        est_tax_nis=100.0, surtax_split_suggested=False,
        legs=(AllocationLeg(side="SELL", symbol="VOO", account_id="ibkr",
              currency="USD", notional_usd=1000.0, funding_source="trim_proceeds",
              quantity=10.0),))
    tampered = AllocationCandidate(kind="TRIM", horizon="this_quarter",
        est_tax_nis=999999.0, surtax_split_suggested=True,
        legs=(AllocationLeg(side="SELL", symbol="VOO", account_id="ibkr",
              currency="USD", notional_usd=1000.0, funding_source="trim_proceeds",
              quantity=999.0),))
    t = ExecutableTask(seq=1, candidate=tampered, horizon="this_quarter",
                       pace="lump", pace_rationale="", rationale="x")
    with pytest.raises(ValueError):
        reconcile_or_raise([t], [real])
