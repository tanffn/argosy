"""/deploy-cash wiring for the fleet-authors pivot: behind deployment_author_enabled
the route attaches `authored` (accepted → primary; unavailable/rejected → degraded,
tiers are the labelled fallback). The author itself is stubbed — no LLM here."""
from __future__ import annotations

from fastapi.testclient import TestClient

from argosy.api.main import create_app
from argosy.services.allocation_author.flow import AuthorOutcome
from argosy.services.allocation_author.proposal import AllocationProposal, Buy
from argosy.services.allocation_author.verifier import GateReport, GateStatus


def _doc():
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, TargetAllocationDoc,
    )
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=30.0, fi_pct=10.0,
        provenance="test",
        classes=[AllocationClassDoc(
            label="Ex-US developed", snapshot_category="ex_us",
            sigma_class="ex_us", target_pct=100.0,
            instruments=[AllocationInstrument(symbol="EXUS", role="primary",
                                              weight_within_class_pct=100.0,
                                              rationale="", domicile="IE")],
            agreement="", rationale="", dissent="")],
        glide=[],
    )


def _patch_doc(monkeypatch):
    import argosy.api.routes.portfolio as portfolio
    monkeypatch.setattr(
        portfolio, "_load_current_doc_and_holdings",
        lambda user_id: (_doc(), {"NVDA": 600_000.0, "SCHD": 264_000.0}, 0.0),
    )


def _enable(monkeypatch):
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_AUTHOR_ENABLED", "1")
    from argosy.config import get_settings
    get_settings.cache_clear()


def test_authored_absent_when_flag_off(monkeypatch):
    _patch_doc(monkeypatch)
    from argosy.config import get_settings
    get_settings.cache_clear()  # ensure default (off)
    client = TestClient(create_app())
    body = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 180000}).json()
    assert body.get("authored") is None
    get_settings.cache_clear()


def test_authored_accepted_is_primary(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    def fake_author(packet, **kw):
        return AuthorOutcome(
            status="accepted",
            proposal=AllocationProposal(
                cash_to_deploy=180_000.0,
                buys=[Buy(symbol="EXUS", amount_usd=180_000.0, sleeve="ex-US",
                          claimed_us_weight=0.0, justification="true ex-US diversifier")],
                rationale="Directed to genuine ex-US on a concentrated book.",
            ),
            report=GateReport(status=GateStatus.ACCEPT, failures=[]),
            attempts=1,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation", fake_author
    )
    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 180000})
    assert resp.status_code == 200, resp.text
    a = resp.json()["authored"]
    assert a["status"] == "accepted" and a["degraded"] is False
    assert a["buys"][0]["symbol"] == "EXUS"
    from argosy.config import get_settings
    get_settings.cache_clear()


def test_authored_unavailable_is_degraded(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    def fake_author(packet, **kw):
        return AuthorOutcome(status="unavailable", proposal=None, report=None, attempts=1)

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation", fake_author
    )
    client = TestClient(create_app())
    resp = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 180000})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    a = body["authored"]
    assert a["status"] == "unavailable" and a["degraded"] is True
    assert any("degraded" in n.lower() for n in a["notes"])
    # The deterministic tiers remain as the labelled fallback.
    assert body["tiers"]
    from argosy.config import get_settings
    get_settings.cache_clear()


def test_packet_carries_no_tax_reserve_field(monkeypatch):
    _patch_doc(monkeypatch)
    _enable(monkeypatch)

    captured = {}

    def fake_author(packet, **kw):
        captured["packet"] = packet
        return AuthorOutcome(
            status="accepted",
            proposal=AllocationProposal(cash_to_deploy=180_000.0,
                                        buys=[Buy(symbol="EXUS", amount_usd=180_000.0)]),
            report=GateReport(status=GateStatus.ACCEPT, failures=[]),
            attempts=1,
        )

    monkeypatch.setattr(
        "argosy.services.allocation_author.reliable.authored_allocation", fake_author
    )
    client = TestClient(create_app())
    a = client.get("/api/portfolio/deploy-cash", params={"cash_usd": 180000}).json()["authored"]
    # No tax-reserve concept anywhere: not in the packet, not in the DTO.
    assert "cgt_liability_usd" not in captured["packet"]
    assert "cash_reserved_for_tax" not in a
    from argosy.config import get_settings
    get_settings.cache_clear()
