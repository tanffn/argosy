"""Phase 0 — versioned cross-phase domain contracts (pure; no network/DB).

These value objects are the seams between the allocation engine (1a), the
allocation agent (1b), and the discovery funnel (2). They are defined ONCE here
and imported everywhere downstream, with a canonical candidate fingerprint
(identity, not notional-only) and versioned serialization.
"""
from __future__ import annotations

import pytest

from argosy.services.contracts import (
    CONTRACTS_SCHEMA_VERSION,
    AllocationCandidate,
    AllocationCandidateDTO,
    AllocationLeg,
    EstimatorVerdict,
    ExecutableTask,
    FleetPick,
    ScanState,
    candidate_fingerprint,
    candidate_to_dto,
    deserialize_candidate,
    serialize_candidate,
)


def _leg(symbol="CSPX", usd=1000.0, side="BUY", funding="cash"):
    return AllocationLeg(side=side, symbol=symbol, account_id="ibkr",
                         currency="USD", notional_usd=usd, funding_source=funding)


def _cand(*legs, kind="BUY", horizon="now"):
    return AllocationCandidate(kind=kind, legs=tuple(legs), horizon=horizon)


# --- value objects ---------------------------------------------------------

def test_value_objects_construct_and_total():
    cand = _cand(_leg())
    assert cand.legs[0].symbol == "CSPX"
    assert cand.total_notional_usd == 1000.0
    # a two-leg swap totals the absolute notionals
    swap = _cand(_leg("SCHD", 500.0, side="SELL", funding="trim_proceeds"),
                 _leg("FUSA", 500.0, side="BUY", funding="trim_proceeds"),
                 kind="SWAP")
    assert swap.total_notional_usd == 1000.0


def test_all_six_contracts_exist():
    EstimatorVerdict(ticker="PLTR", go=True, conviction="HIGH",
                     sentiment=0.7, one_line="momentum + fundamentals")
    FleetPick(ticker="PLTR", conviction="HIGH", thesis_md="...",
              verdict="BUY", cites=("src:1",))
    ScanState(user_id="ariel", ticker="PLTR", last_score=0.8)
    ExecutableTask(seq=1, candidate=_cand(_leg()), horizon="now", pace="lump",
                   pace_rationale="", rationale="buy core")


# --- canonical fingerprint -------------------------------------------------

def test_fingerprint_is_identity_not_notional_only():
    a = _cand(_leg("CSPX", 1000.0))
    b = _cand(_leg("CSPX", 1000.0))
    c = _cand(_leg("VUAA", 1000.0))  # same dollars, different ticker
    d = _cand(_leg("CSPX", 1001.0))  # same ticker, different dollars
    assert candidate_fingerprint(a) == candidate_fingerprint(b)
    assert candidate_fingerprint(a) != candidate_fingerprint(c)
    assert candidate_fingerprint(a) != candidate_fingerprint(d)
    # the method on the dataclass agrees with the module function
    assert a.fingerprint() == candidate_fingerprint(a)


def test_fingerprint_total_orderable_with_mixed_none_quantity():
    """codex 1b r2: legs where quantity is None for one and float for another
    must not crash the fingerprint's sort (None vs float is not comparable)."""
    c = AllocationCandidate(kind="SWAP", horizon="now", legs=(
        AllocationLeg("BUY", "CSPX", "ibkr", "USD", 1000.0, "cash", quantity=None),
        AllocationLeg("BUY", "CSPX", "ibkr", "USD", 1000.0, "cash", quantity=10.0),
    ))
    # must not raise, and must be self-consistent
    assert candidate_fingerprint(c) == candidate_fingerprint(c)


def test_fingerprint_is_order_insensitive_across_legs():
    s1 = _cand(_leg("SCHD", 500.0, side="SELL", funding="trim_proceeds"),
               _leg("FUSA", 500.0, side="BUY", funding="trim_proceeds"),
               kind="SWAP")
    s2 = _cand(_leg("FUSA", 500.0, side="BUY", funding="trim_proceeds"),
               _leg("SCHD", 500.0, side="SELL", funding="trim_proceeds"),
               kind="SWAP")
    assert candidate_fingerprint(s1) == candidate_fingerprint(s2)


# --- versioned serialization ----------------------------------------------

def test_serialize_round_trips_and_stamps_version():
    cand = AllocationCandidate(
        kind="SWAP", horizon="this_quarter", est_tax_nis=1234.5,
        surtax_split_suggested=True, rationale="domicile swap",
        cites=("plan_target:FUSA", "replaces:SCHD"),
        legs=(_leg("SCHD", 500.0, side="SELL", funding="trim_proceeds"),
              _leg("FUSA", 500.0, side="BUY", funding="trim_proceeds")))
    blob = serialize_candidate(cand)
    assert blob["schema_version"] == CONTRACTS_SCHEMA_VERSION
    restored = deserialize_candidate(blob)
    assert restored == cand
    assert candidate_fingerprint(restored) == candidate_fingerprint(cand)


def test_deserialize_rejects_future_schema_version():
    cand = _cand(_leg())
    blob = serialize_candidate(cand)
    blob["schema_version"] = CONTRACTS_SCHEMA_VERSION + 1
    with pytest.raises(ValueError):
        deserialize_candidate(blob)


# --- wire DTO --------------------------------------------------------------

def test_candidate_to_dto_preserves_fields():
    cand = AllocationCandidate(
        kind="SWAP", horizon="this_quarter", est_tax_nis=42.0,
        surtax_split_suggested=True, rationale="r", cites=("a", "b"),
        legs=(_leg("SCHD", 500.0, side="SELL", funding="trim_proceeds"),
              _leg("FUSA", 500.0, side="BUY", funding="trim_proceeds")))
    dto = candidate_to_dto(cand)
    assert isinstance(dto, AllocationCandidateDTO)
    dumped = dto.model_dump()
    assert dumped["kind"] == "SWAP"
    assert dumped["surtax_split_suggested"] is True
    assert dumped["cites"] == ["a", "b"]
    assert [l["symbol"] for l in dumped["legs"]] == ["SCHD", "FUSA"]
    assert [l["side"] for l in dumped["legs"]] == ["SELL", "BUY"]
    # DTO faithfully reflects the domain object's legs
    assert dumped["legs"][0]["funding_source"] == "trim_proceeds"
