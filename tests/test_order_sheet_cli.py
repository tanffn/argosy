from __future__ import annotations

import json

from fastapi import FastAPI

from argosy.cli.order_sheet import order_sheet


def test_cli_uses_persisting_e2e_endpoint(monkeypatch, capsys):
    app = FastAPI()
    captured: dict = {}

    @app.post("/api/e2e-proof/run")
    def run(body: dict):
        captured.update(body)
        return {
            "stage": "ready_to_accept",
            "artifact": {"validation": {"valid": True}},
            "lines": [],
        }

    monkeypatch.setattr("argosy.api.main.create_app", lambda: app)
    order_sheet(
        cash_usd=120_000,
        user_id="ariel",
        no_sells=True,
        horizon_min=1,
        horizon_max=5,
    )

    assert captured == {
        "user_id": "ariel",
        "cash_usd": 120_000,
        "allow_sells": False,
        "horizon_years_min": 1,
        "horizon_years_max": 5,
    }
    printed = json.loads(capsys.readouterr().out)
    assert printed["stage"] == "ready_to_accept"
