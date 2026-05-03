"""Domain KB browser route tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from argosy.api.routes.domain_kb import _reset_review_queue, enqueue_review_item
from argosy.config import get_settings


@pytest.mark.asyncio
async def test_domain_kb_tree_returns_root(client: AsyncClient) -> None:
    res = await client.get("/api/domain-kb/tree")
    assert res.status_code == 200
    body = res.json()
    assert body["is_dir"] is True
    # name is "domain_knowledge" at the root.
    assert body["name"] == "domain_knowledge"


@pytest.mark.asyncio
async def test_domain_kb_file_reads_content(
    tmp_path: Path, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the loader at a temp dir lets us read a synthetic file."""
    # Place a file in the actual domain_knowledge dir we just rerouted to.
    settings = get_settings()
    target_dir = settings.domain_knowledge_dir / "_test_phase7"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "scratch.md"
    target_file.write_text(
        "---\nlast_verified: 2026-01-01\n---\nbody content\n",
        encoding="utf-8",
    )
    try:
        res = await client.get(
            "/api/domain-kb/file", params={"path": "_test_phase7/scratch.md"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["frontmatter"].startswith("last_verified")
        assert "body content" in body["content"]
    finally:
        target_file.unlink(missing_ok=True)
        try:
            target_dir.rmdir()
        except OSError:
            pass


@pytest.mark.asyncio
async def test_domain_kb_file_rejects_traversal(client: AsyncClient) -> None:
    res = await client.get("/api/domain-kb/file", params={"path": "../../etc/passwd"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_domain_kb_review_queue_lifecycle(client: AsyncClient) -> None:
    _reset_review_queue()
    item_id = enqueue_review_item(
        path="domain_knowledge/tax/israel/cap_gains.md",
        diff="- 25%\n+ 30%",
        evidence=[{"url": "https://taxes.gov.il/", "tier": 1}],
    )

    res = await client.get("/api/domain-kb/review-queue")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["rows"][0]["id"] == item_id
    assert body["rows"][0]["status"] == "pending"

    res = await client.post(f"/api/domain-kb/review/{item_id}/approve")
    assert res.status_code == 200
    assert res.json()["status"] == "approved"

    # Pending list now empty.
    res = await client.get("/api/domain-kb/review-queue")
    assert res.json()["total"] == 0


@pytest.mark.asyncio
async def test_domain_kb_review_reject(client: AsyncClient) -> None:
    _reset_review_queue()
    item_id = enqueue_review_item(path="x.md", diff="", evidence=[])
    res = await client.post(f"/api/domain-kb/review/{item_id}/reject")
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_domain_kb_review_404(client: AsyncClient) -> None:
    _reset_review_queue()
    res = await client.post("/api/domain-kb/review/999/approve")
    assert res.status_code == 404
