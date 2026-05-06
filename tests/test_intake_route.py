"""Intake API route tests."""

from __future__ import annotations

import io
import json

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.intake import IntakeAgent
from argosy.agents.intake_extractor import IntakeExtractorAgent
from argosy.api.routes.intake import (
    reset_intake_agent_factory,
    reset_intake_extractor_factory,
    set_intake_agent_factory,
    set_intake_extractor_factory,
)
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    PlanVersion,
    User,
    UserContext,
)


_CANNED = {
    "stage": "stage_1",
    "question_for_user": "What is your country of tax residence?",
    "context_updates": [],
    "stage_complete": False,
    "next_stage": None,
    "confidence": "MEDIUM",
    "cited_sources": [],
    "notes_for_orchestrator": "",
}


def _factory(user_id: str):
    class _M(IntakeAgent):
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(_CANNED),
                tokens_in=80,
                tokens_out=120,
                model=self.model,
            )
    return _M(user_id=user_id)


@pytest.mark.asyncio
async def test_intake_status_default_stage_1(engine: None, client: AsyncClient) -> None:
    res = await client.get("/api/intake/status", params={"user_id": "ariel"})
    assert res.status_code == 200
    body = res.json()
    assert body["current_stage"] == "stage_1"


@pytest.mark.asyncio
async def test_intake_turn_returns_question(engine: None, client: AsyncClient) -> None:
    set_intake_agent_factory(_factory)
    try:
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(UserContext(user_id="ariel", current_stage="stage_1"))
            await session.commit()

        res = await client.post(
            "/api/intake/turn",
            json={"user_id": "ariel", "last_user_message": ""},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["stage"] == "stage_1"
        assert "tax residence" in body["question_for_user"]
        assert body["stage_complete"] is False
    finally:
        reset_intake_agent_factory()


# ----------------------------------------------------------------------
# /upload
# ----------------------------------------------------------------------


_UPLOAD_FIXTURE_MD = """# Sample Wealth Plan v1.0

## Identity
- Tax residency: Israel
- Family: spouse + two children

## Goals
- Retirement: 2032
- Annual income target: 600k NIS
"""


_CANNED_EXTRACTION = {
    "tax_residency": {
        "value": "israel",
        "source_excerpt": "Tax residency: Israel",
        "confidence": "HIGH",
    },
    "citizenship": None,
    "family": {
        "value": "spouse plus two children",
        "source_excerpt": "spouse + two children",
        "confidence": "HIGH",
    },
    "employment": None,
    "retirement_target_year": {
        "value": "2032",
        "source_excerpt": "Retirement: 2032",
        "confidence": "HIGH",
    },
    "target_annual_income": {
        "value": "600k NIS",
        "source_excerpt": "Annual income target: 600k NIS",
        "confidence": "MEDIUM",
    },
    "near_term_spending": None,
    "primary_brokers": None,
    "bank_diversification_preference": None,
    "risk_tolerance": None,
    "constraints_other": [],
    "identity_yaml": "tax_residency: israel\nfamily: spouse plus two children\n",
    "goals_yaml": "retirement_target_year: 2032\ntarget_annual_income: 600k NIS\n",
    "constraints_yaml": "",
    "fields_extracted": [
        "tax_residency",
        "family",
        "retirement_target_year",
        "target_annual_income",
    ],
    "fields_missing": [
        "citizenship",
        "employment",
        "near_term_spending",
        "primary_brokers",
        "risk_tolerance",
    ],
    "confidence": "HIGH",
    "notes": "Plan v1.0 - clear on identity & top goals.",
}


def _extractor_factory(canned: dict):
    def _make(user_id: str):
        class _E(IntakeExtractorAgent):
            async def _call_model(self, *, system: str, user: str) -> ModelCall:
                return ModelCall(
                    text=json.dumps(canned),
                    tokens_in=1500,
                    tokens_out=800,
                    model=self.model,
                )
        return _E(user_id=user_id)

    return _make


@pytest.mark.asyncio
async def test_upload_creates_plan_version_and_merges_context(
    engine: None, client: AsyncClient
) -> None:
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        # Pre-create a user_context with one user-typed identity field that
        # must NOT be overwritten by the extractor.
        async with db_mod.get_session() as session:
            session.add(User(id="ariel"))
            session.add(
                UserContext(
                    user_id="ariel",
                    identity_yaml="tax_residency: united_states\n",
                    current_stage="stage_1",
                )
            )
            await session.commit()

        files = {
            "file": (
                "plan.md",
                io.BytesIO(_UPLOAD_FIXTURE_MD.encode("utf-8")),
                "text/markdown",
            )
        }
        data = {"user_id": "ariel"}
        res = await client.post("/api/intake/upload", data=data, files=files)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["plan_version_id"] >= 1
        assert body["intake_session_id"]
        assert "tax_residency" in body["fields_extracted"]
        assert "citizenship" in body["fields_missing"]
        assert body["confidence"] == "HIGH"
        assert "extracted" in body["summary_for_user"].lower()

        # plan_versions row created with the raw markdown.
        async with db_mod.get_session() as session:
            pv = (
                await session.execute(
                    select(PlanVersion).where(PlanVersion.user_id == "ariel")
                )
            ).scalar_one()
            assert pv.raw_markdown == _UPLOAD_FIXTURE_MD
            assert pv.source_path == "plan.md"
            assert pv.version_label.startswith("from_intake_upload_")

            # user_context: identity_yaml was merged additively. The pre-existing
            # tax_residency=united_states must STILL be there (existing wins),
            # while the new family field must have been added.
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == "ariel")
                )
            ).scalar_one()
            identity = yaml.safe_load(ctx.identity_yaml)
            assert identity["tax_residency"] == "united_states"  # preserved
            assert identity["family"].startswith("spouse")  # added
            goals = yaml.safe_load(ctx.goals_yaml)
            assert goals["retirement_target_year"] == 2032
            assert ctx.intake_session_id == body["intake_session_id"]

            # agent_reports row stamped with the same intake_session_id.
            ar_rows = (
                await session.execute(
                    select(AgentReportRow).where(
                        AgentReportRow.user_id == "ariel",
                        AgentReportRow.agent_role == "intake_extractor",
                    )
                )
            ).scalars().all()
            assert len(ar_rows) == 1
            assert ar_rows[0].intake_session_id == body["intake_session_id"]
            assert ar_rows[0].decision_id is None
    finally:
        reset_intake_extractor_factory()


@pytest.mark.asyncio
async def test_upload_rejects_non_markdown(
    engine: None, client: AsyncClient
) -> None:
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        files = {
            "file": (
                "plan.pdf",
                io.BytesIO(b"%PDF-1.4 not really markdown"),
                "application/pdf",
            )
        }
        res = await client.post(
            "/api/intake/upload",
            data={"user_id": "ariel"},
            files=files,
        )
        assert res.status_code == 400
        assert "Markdown" in res.json()["detail"]
    finally:
        reset_intake_extractor_factory()


# ----------------------------------------------------------------------
# /file-to-text
# ----------------------------------------------------------------------


def _build_xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    from openpyxl import Workbook  # type: ignore[import-untyped]

    wb = Workbook()
    default = wb.active
    wb.remove(default)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_minimal_pdf_bytes(pages: list[str]) -> bytes:
    """Hand-rolled minimal PDF with one text per page."""
    objs: list[bytes] = []

    def _add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog_id = _add(b"")
    pages_id = _add(b"")
    page_kids: list[int] = []
    for txt in pages:
        safe = txt.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        content_obj = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
            + stream
            + b"\nendstream"
        )
        content_id = _add(content_obj)
        page_obj = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("latin-1")
        page_id = _add(page_obj)
        page_kids.append(page_id)
    objs[catalog_id - 1] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")
    )
    kids_str = " ".join(f"{k} 0 R" for k in page_kids)
    objs[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_kids)} >>".encode(
            "latin-1"
        )
    )

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"
    xref_pos = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n".encode("latin-1")
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("latin-1")
    buf += (
        f"trailer << /Size {len(objs) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(buf)


@pytest.mark.asyncio
async def test_file_to_text_md_happy_path(
    engine: None, client: AsyncClient
) -> None:
    src = "# Title\n\n**Bold** plus קרן השתלמות"
    files = {
        "file": ("notes.md", io.BytesIO(src.encode("utf-8")), "text/markdown"),
    }
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["filename"] == "notes.md"
    assert body["extracted_text"] == src
    assert body["warnings"] == []
    assert body["page_or_sheet_count"] == 0


@pytest.mark.asyncio
async def test_file_to_text_csv_happy_path(
    engine: None, client: AsyncClient
) -> None:
    src = "ticker,shares\nAAPL,100\n"
    files = {
        "file": ("p.csv", io.BytesIO(src.encode("utf-8")), "text/csv"),
    }
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["extracted_text"] == src


@pytest.mark.asyncio
async def test_file_to_text_tsv_happy_path(
    engine: None, client: AsyncClient
) -> None:
    src = "ticker\tshares\nAAPL\t100\n"
    files = {
        "file": (
            "p.tsv",
            io.BytesIO(src.encode("utf-8")),
            "text/tab-separated-values",
        ),
    }
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["extracted_text"] == src


@pytest.mark.asyncio
async def test_file_to_text_txt_happy_path(
    engine: None, client: AsyncClient
) -> None:
    src = "Hello world\n"
    files = {
        "file": ("note.txt", io.BytesIO(src.encode("utf-8")), "text/plain"),
    }
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 200, res.text
    assert res.json()["extracted_text"] == src


@pytest.mark.asyncio
async def test_file_to_text_pdf_happy_path(
    engine: None, client: AsyncClient
) -> None:
    pdf = _build_minimal_pdf_bytes(["Pay stub line A", "Pay stub line B"])
    files = {"file": ("stub.pdf", io.BytesIO(pdf), "application/pdf")}
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["page_or_sheet_count"] == 2
    assert "Pay stub line A" in body["extracted_text"]


@pytest.mark.asyncio
async def test_file_to_text_xlsx_happy_path(
    engine: None, client: AsyncClient
) -> None:
    xlsx = _build_xlsx_bytes(
        {"Positions": [["ticker", "shares"], ["AAPL", 100]]}
    )
    files = {
        "file": (
            "broker.xlsx",
            io.BytesIO(xlsx),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["page_or_sheet_count"] == 1
    assert "AAPL,100" in body["extracted_text"]
    assert "## Sheet: Positions" in body["extracted_text"]


@pytest.mark.asyncio
async def test_file_to_text_rejects_unsupported(
    engine: None, client: AsyncClient
) -> None:
    files = {
        "file": ("photo.png", io.BytesIO(b"\x89PNG\r\n"), "image/png"),
    }
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 400
    assert "Unsupported" in res.json()["detail"]


@pytest.mark.asyncio
async def test_file_to_text_rejects_oversize(
    engine: None, client: AsyncClient
) -> None:
    big = b"x" * (5 * 1024 * 1024 + 1)
    files = {"file": ("big.txt", io.BytesIO(big), "text/plain")}
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 413
    assert "too large" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_file_to_text_rejects_empty(
    engine: None, client: AsyncClient
) -> None:
    files = {"file": ("empty.md", io.BytesIO(b""), "text/markdown")}
    res = await client.post("/api/intake/file-to-text", files=files)
    assert res.status_code == 400
    assert "empty" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_creates_user_if_missing(
    engine: None, client: AsyncClient
) -> None:
    """Uploading is the first thing a brand-new user might do — the route
    must auto-create the user + user_context rows."""
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        files = {
            "file": (
                "plan.md",
                io.BytesIO(_UPLOAD_FIXTURE_MD.encode("utf-8")),
                "text/markdown",
            )
        }
        res = await client.post(
            "/api/intake/upload",
            data={"user_id": "newbie"},
            files=files,
        )
        assert res.status_code == 200, res.text

        async with db_mod.get_session() as session:
            user = (
                await session.execute(select(User).where(User.id == "newbie"))
            ).scalar_one_or_none()
            assert user is not None
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == "newbie")
                )
            ).scalar_one()
            assert "tax_residency" in ctx.identity_yaml
    finally:
        reset_intake_extractor_factory()


# ----------------------------------------------------------------------
# T1.9 — distillation hook on upload happy-path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_upload_triggers_distillation(
    engine: None, client: AsyncClient, monkeypatch
) -> None:
    """After upload, the inserted plan_versions row has distillate_json populated."""
    import io

    from argosy.agents.plan_distiller_types import Goal, PlanDistillate
    from argosy.services import plan_distiller_service as svc

    # Stub the agent so no LLM call happens.
    class _Fake:
        def run_sync(self, **kw):
            payload = PlanDistillate(
                plan_label="Test plan",
                distilled_at_iso="2026-05-05T00:00:00+00:00",
                goals=[Goal(label="retirement_target_year", value="2031")],
            )
            return type(
                "R",
                (),
                {
                    "output": payload,
                    "model": "fake",
                    "tokens_in": 1,
                    "tokens_out": 1,
                    "cost_usd": 0.0,
                },
            )()

    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _Fake())
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        files = {
            "file": (
                "plan.md",
                io.BytesIO(b"# Plan\n\nRetirement: 2031\n"),
                "text/markdown",
            )
        }
        data = {"user_id": "ariel"}
        r = await client.post("/api/intake/upload", files=files, data=data)
        assert r.status_code == 200, r.text
        body = r.json()
        plan_id = body["plan_version_id"]

        # Distillate should now be populated.
        async with db_mod.get_session() as session:
            pv = (
                await session.execute(
                    select(PlanVersion).where(PlanVersion.id == plan_id)
                )
            ).scalar_one_or_none()
            assert pv is not None
            assert pv.distillate_json is not None, "distillate_json should be populated"
            assert "retirement_target_year" in pv.distillate_json
            assert pv.distilled_at is not None
            assert pv.source_hash is not None
            assert pv.role == "baseline"
    finally:
        reset_intake_extractor_factory()


@pytest.mark.asyncio
async def test_intake_upload_distillation_failure_is_non_fatal(
    engine: None, client: AsyncClient, monkeypatch
) -> None:
    """If the distiller raises, the upload still succeeds; distillate stays NULL.

    Distillation is a value-add, not a precondition for the upload to
    be useful. The user's plan markdown must still be captured.
    """
    import io

    from argosy.services import plan_distiller_service as svc

    class _Boom:
        def run_sync(self, **kw):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(svc, "_make_agent", lambda user_id: _Boom())
    set_intake_extractor_factory(_extractor_factory(_CANNED_EXTRACTION))
    try:
        files = {
            "file": ("plan.md", io.BytesIO(b"# Plan"), "text/markdown")
        }
        data = {"user_id": "ariel"}
        r = await client.post("/api/intake/upload", files=files, data=data)
        assert r.status_code == 200, r.text
        plan_id = r.json()["plan_version_id"]

        async with db_mod.get_session() as session:
            pv = (
                await session.execute(
                    select(PlanVersion).where(PlanVersion.id == plan_id)
                )
            ).scalar_one_or_none()
            assert pv is not None
            assert pv.distillate_json is None  # distillation failed silently
            assert pv.role == "baseline"
            assert pv.raw_markdown == "# Plan"
    finally:
        reset_intake_extractor_factory()
