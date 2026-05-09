"""HTTP route tests for /api/expenses/*."""

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


@pytest.fixture()
def expense_client(client_with_db, tmp_path, monkeypatch):
    """client_with_db augmented with:
      - ARGOSY_HOME → tmp_path (so catalog_upload writes to a throw-away dir)
      - a seeded User row so FK-aware sessions don't fail
    """
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import reload_settings
    reload_settings()

    # Seed the 'ariel' user into the test DB so UserFile FK is satisfied even
    # when SQLite FK enforcement is on.
    from argosy.state.models import User
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()

    yield client_with_db

    reload_settings()


def test_upload_max_xlsx_returns_parse_summary(expense_client):
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        with open(FIXTURES / "max_minimal.xlsx", "rb") as f:
            files = {"files": ("max_minimal.xlsx", f.read(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            resp = expense_client.post("/api/expenses/upload",
                                       files=files,
                                       data={"user_id": "ariel"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["filename"] == "max_minimal.xlsx"
    assert r["status"] == "parsed"
    assert r["transactions_inserted"] == 5


def test_upload_unknown_format_returns_failed_status(expense_client):
    files = {"files": ("garbage.bin", b"\x00\x01\x02\x03", "application/octet-stream")}
    resp = expense_client.post("/api/expenses/upload",
                                files=files, data={"user_id": "ariel"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "failed"
    err = body["results"][0]["error"].lower()
    assert "unrecognized" in err or "unknown" in err


def test_upload_multi_file(expense_client):
    with patch("argosy.services.expense_ingest.category_resolver._categorize_via_llm",
               return_value=[]):
        files = [
            ("files", ("max_minimal.xlsx",
                       open(FIXTURES / "max_minimal.xlsx", "rb").read(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("isracard_minimal.xlsx",
                       open(FIXTURES / "isracard_minimal.xlsx", "rb").read(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ]
        resp = expense_client.post("/api/expenses/upload",
                                    files=files, data={"user_id": "ariel"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    assert all(r["status"] == "parsed" for r in body["results"])
