import hashlib
import json

import pytest
from PIL import Image

from argosy.agents.base import AgentRunError, BaseAgent
from argosy.agents.codex_evidence import prepare_codex_evidence
from argosy.services import codex_public_documents as bridge
from argosy.services.public_document_tool import save_artifacts


class Probe(BaseAgent):
    agent_role = "probe"


def prepare(agent=None, **changes):
    kwargs = dict(system="role", user="question", sources=None, pdf_attachments=None,
                  image_attachments=None, backend="claude_code")
    return prepare_codex_evidence(agent or Probe(user_id="test"), **(kwargs | changes))


@pytest.mark.asyncio
async def test_all_pages_native_images_source_ids_and_cleanup(tmp_path):
    pdf, image = tmp_path / "scan.pdf", tmp_path / "chart.jpg"
    red, blue = Image.new("RGB", (150, 100), "red"), Image.new("RGB", (150, 100), "blue")
    red.save(pdf, save_all=True, append_images=[blue], resolution=72)
    blue.save(image)
    async with prepare(pdf_attachments=[{"path": pdf}], image_attachments=[{"path": image}],
                       sources=[("feed:42", "specific company data")]) as evidence:
        first, second, third = evidence.images
        assert third == image
        assert "scan.pdf" in evidence.user and "images 1–2" in evidence.user
        assert "specific company data" in evidence.user and "feed:42" in evidence.user
        with Image.open(first) as one, Image.open(second) as two:
            assert one.getpixel((20, 20))[0] > 220
            assert two.getpixel((20, 20))[2] > 220
        assert not evidence.mcp_config and not evidence.web_search
    assert not first.exists() and not second.exists()
    assert pdf.exists() and image.exists()


@pytest.mark.asyncio
async def test_no_partial_pdf_or_fabricated_provider_citation(tmp_path):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"truncated")
    with pytest.raises(AgentRunError, match="all PDF pages"):
        async with prepare(pdf_attachments=[{"path": pdf}]):
            pytest.fail("Invalid attachments must abort generation")
    agent = Probe(user_id="test")
    agent.citations_enabled = True
    with pytest.raises(AgentRunError, match="native"):
        async with prepare(agent, sources=[("one", "body")], backend="api_key"):
            pytest.fail("A different provider cannot attest Anthropic citation offsets")


@pytest.mark.asyncio
async def test_research_modes_and_forced_toolless(tmp_path, monkeypatch):
    from argosy.agents import codex_evidence
    from types import SimpleNamespace

    monkeypatch.setattr(codex_evidence, "get_settings", lambda: SimpleNamespace(home=tmp_path))
    agent = Probe(user_id="specific-tenant")
    agent.claude_code_allowed_tools = ("WebSearch",)
    agent.agent_role = "news_reassessment"
    async with prepare(agent, user="Ticker: NVDA\nAs of UTC: today") as evidence:
        assert not evidence.web_search
        assert any('["search_web"]' in option for option in evidence.mcp_config)
    agent.claude_code_allowed_tools = ("WebFetch", "WebSearch")
    agent.claude_code_public_documents = True
    async with prepare(agent) as evidence:
        assert evidence.web_search and any('["read_document"]' in opt for opt in evidence.mcp_config)
        assert evidence.capture_store.name == hashlib.sha256(b"specific-tenant").hexdigest()
        saved = {"status": "captured", "text": "document text"}
        receipt_id = save_artifacts(evidence.capture_store, saved)
        save_artifacts(evidence.capture_store, {"status": "unrelated"})
        (evidence.receipt_index / receipt_id).touch()
        assert evidence.public_document_receipts() == [{**saved, "receipt_id": receipt_id}]
        (evidence.capture_store / f"{receipt_id}.json").write_text("{}")
        with pytest.raises(AgentRunError, match="integrity"):
            evidence.public_document_receipts()
    agent.claude_code_force_toolless = True
    async with prepare(agent) as evidence:
        assert not evidence.mcp_config and not evidence.web_search


@pytest.mark.asyncio
async def test_search_only_mcp_lists_one_tool_and_limits_calls(tmp_path, monkeypatch):
    async def query_results(query):
        assert query == "NVDA stock earnings"
        return [{"url": "https://example.org/filing", "snippet": "not actual filing text"}]
    monkeypatch.setattr(bridge, "search_public_web", query_results)
    server = bridge.make_server("search", tmp_path, tmp_path, "NVDA")
    assert [t.name for t in await server.list_tools()] == ["search_web"]
    for _ in range(3):
        result = await server.call_tool("search_web", {"topic": "earnings"})
        assert not result.isError and "snippet" in result.content[0].text
    exhausted = await server.call_tool("search_web", {"topic": "outlook"})
    assert exhausted.isError


@pytest.mark.asyncio
async def test_document_mcp_indexes_host_receipt(tmp_path, monkeypatch):
    index = tmp_path / "session"
    index.mkdir()
    store = tmp_path / "captures"
    receipt = {"status": "captured", "text": "document body"}
    receipt_id = save_artifacts(store, receipt)

    async def read(self, arguments):
        assert arguments == {"url": "https://example.org/filing"}
        return {"content": [{"type": "text", "text": json.dumps(receipt | {"receipt_id": receipt_id})}],
                "is_error": False}
    monkeypatch.setattr(bridge.PublicDocumentReader, "read", read)
    server = bridge.make_server("documents", store, index)
    assert [t.name for t in await server.list_tools()] == ["read_document"]
    result = await server.call_tool("read_document", {"url": "https://example.org/filing"})
    assert not result.isError and (index / receipt_id).is_file()


@pytest.mark.asyncio
async def test_truncated_jpeg_and_unscoped_search_refused(tmp_path):
    photo = tmp_path / "bad.jpg"
    Image.new("RGB", (200, 200)).save(photo)
    photo.write_bytes(photo.read_bytes()[:-64])
    with pytest.raises(AgentRunError, match="single-frame"):
        async with prepare(image_attachments=[{"path": photo}]):
            pytest.fail("Damaged images must never be passed to the model")
    agent = Probe(user_id="owner")
    agent.agent_role = "news_reassessment"
    agent.claude_code_allowed_tools = ("WebSearch",)
    with pytest.raises(AgentRunError, match="public ticker"):
        async with prepare(agent, user="Ticker: owner@private.example\nAs of UTC: today"):
            pytest.fail("Unscoped searches must not be sent to the web")
