"""Tests for the typed objects backing the team-sourced Alternatives sleeve."""
from __future__ import annotations

import pytest

from argosy.services.alternatives_types import (
    AlternativesSleeveDecision,
    VerificationEvidence,
    VerificationResult,
    VerifiedAlternativesCandidate,
)


def _green_evidence() -> VerificationEvidence:
    return VerificationEvidence(
        isin_checksum_ok=True,
        isin_prefix="IE",
        domicile_coherent=True,
        registry_hit=True,
        tradeable=None,
        source_url="https://issuer/factsheet",
    )


def test_verification_result_pass_requires_evidence():
    r = VerificationResult(
        symbol="SGLD",
        verified=True,
        severity="GREEN",
        reason="registry + checksum ok",
        evidence=_green_evidence(),
    )
    assert r.verified and r.severity == "GREEN"
    assert r.evidence.registry_hit


def test_verification_result_reject_is_not_verified():
    ev = VerificationEvidence(
        isin_checksum_ok=False,
        isin_prefix="US",
        domicile_coherent=False,
        registry_hit=False,
        tradeable=None,
        source_url=None,
    )
    r = VerificationResult(
        symbol="FAKE",
        verified=False,
        severity="RED",
        reason="bad checksum + US prefix",
        evidence=ev,
    )
    assert not r.verified and r.severity == "RED"


def _verified_candidate(symbol: str = "SGLD") -> VerifiedAlternativesCandidate:
    return VerifiedAlternativesCandidate(
        symbol=symbol,
        name="Invesco Physical Gold ETC",
        asset_class="precious_metals",
        domicile="IE",
        isin="IE00B579F325",
        weight_within_sleeve_pct=100.0,
        conviction="HIGH",
        thesis_md="gold hedge",
        verification=VerificationResult(
            symbol=symbol,
            verified=True,
            severity="GREEN",
            reason="ok",
            evidence=_green_evidence(),
        ),
    )


def test_zero_pct_sleeve_must_carry_no_instruments():
    with pytest.raises(ValueError):
        AlternativesSleeveDecision(
            target_pct=0.0,
            sleeve_sigma=0.0,
            instruments=[_verified_candidate()],
            decision="0_percent",
            rationale_md="x",
        )


def test_nonzero_sleeve_must_carry_instruments():
    with pytest.raises(ValueError):
        AlternativesSleeveDecision(
            target_pct=3.0,
            sleeve_sigma=0.268,
            instruments=[],
            decision="approve",
            rationale_md="x",
        )


def test_valid_approve_decision_round_trips():
    d = AlternativesSleeveDecision(
        target_pct=3.0,
        sleeve_sigma=0.268,
        instruments=[_verified_candidate()],
        decision="approve",
        rationale_md="3% gold diversifier",
    )
    assert d.target_pct == 3.0 and d.instruments[0].symbol == "SGLD"


def test_valid_zero_percent_decision():
    d = AlternativesSleeveDecision(
        target_pct=0.0,
        sleeve_sigma=0.0,
        instruments=[],
        decision="insufficient_data",
        rationale_md="no verified estate-clean candidates",
    )
    assert d.target_pct == 0.0 and not d.instruments
