"""Domain KB browser routes (SDD §11.1 #8, Phase 7).

Endpoints:
  - GET  /api/domain-kb/tree                        — directory tree
  - GET  /api/domain-kb/file?path=...               — file contents + frontmatter
  - GET  /api/domain-kb/review-queue                — pending refresh proposals
  - POST /api/domain-kb/review/{id}/approve         — approve a pending change
  - POST /api/domain-kb/review/{id}/reject          — reject a pending change

The review queue is backed by a simple in-memory dict for Phase 7
(`_REVIEW_QUEUE` below). Production would move this to a proper table;
the API surface stays the same.

Path safety: all `path` parameters are constrained to under
`${ARGOSY_HOME}/domain_knowledge/` and `..` segments are rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from argosy.config import get_settings

router = APIRouter(prefix="/domain-kb", tags=["domain-kb"])


class TreeNode(BaseModel):
    name: str
    path: str  # relative under domain_knowledge/
    is_dir: bool
    children: list["TreeNode"] = []


TreeNode.model_rebuild()


class FileResponse(BaseModel):
    path: str
    frontmatter: str
    content: str
    raw: str


class ReviewItem(BaseModel):
    id: int
    path: str
    diff: str
    evidence: list[dict[str, Any]] = []
    status: str = "pending"  # pending | approved | rejected
    note: str = ""


class ReviewQueueResponse(BaseModel):
    rows: list[ReviewItem]
    total: int


class ReviewActionResponse(BaseModel):
    status: str
    id: int


# In-memory review queue (Phase 7). Tests reset via `_reset_review_queue`.
_REVIEW_QUEUE: dict[int, ReviewItem] = {}
_NEXT_ID: int = 1


def enqueue_review_item(*, path: str, diff: str, evidence: list[dict[str, Any]]) -> int:
    """Add a refresh proposal to the queue. Returns the new id.

    Called by the `DomainRefreshAgent` flow (or its caller) when status
    is `change_proposed`.
    """
    global _NEXT_ID
    item_id = _NEXT_ID
    _NEXT_ID += 1
    _REVIEW_QUEUE[item_id] = ReviewItem(
        id=item_id, path=path, diff=diff, evidence=evidence, status="pending"
    )
    return item_id


def _reset_review_queue() -> None:
    """Test helper."""
    global _NEXT_ID
    _REVIEW_QUEUE.clear()
    _NEXT_ID = 1


def _safe_resolve(rel_path: str) -> Path:
    """Resolve `rel_path` under domain_knowledge_dir; reject traversal."""
    settings = get_settings()
    root = settings.domain_knowledge_dir.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="path must be under domain_knowledge/"
        ) from exc
    return candidate


def _build_tree(root: Path, base: Path) -> TreeNode:
    rel = root.relative_to(base)
    name = root.name if str(rel) != "." else "domain_knowledge"
    if root.is_file():
        return TreeNode(name=name, path=str(rel).replace("\\", "/"), is_dir=False)
    children = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:  # pragma: no cover - defensive
        entries = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        children.append(_build_tree(entry, base))
    return TreeNode(
        name=name,
        path=str(rel).replace("\\", "/") if str(rel) != "." else "",
        is_dir=True,
        children=children,
    )


@router.get("/tree", response_model=TreeNode)
async def get_tree() -> TreeNode:
    settings = get_settings()
    root = settings.domain_knowledge_dir
    if not root.is_dir():
        return TreeNode(name="domain_knowledge", path="", is_dir=True, children=[])
    return _build_tree(root, root)


@router.get("/file", response_model=FileResponse)
async def get_file(path: str = Query(..., description="Relative path under domain_knowledge/")) -> FileResponse:
    full = _safe_resolve(path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    raw = full.read_text(encoding="utf-8")
    frontmatter = ""
    body = raw
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end > 0:
            frontmatter = raw[4:end]
            body = raw[end + 5 :]
    return FileResponse(path=path, frontmatter=frontmatter, content=body, raw=raw)


@router.get("/review-queue", response_model=ReviewQueueResponse)
async def get_review_queue() -> ReviewQueueResponse:
    rows = sorted(_REVIEW_QUEUE.values(), key=lambda r: r.id)
    pending = [r for r in rows if r.status == "pending"]
    return ReviewQueueResponse(rows=pending, total=len(pending))


@router.post("/review/{item_id}/approve", response_model=ReviewActionResponse)
async def approve_review(item_id: int) -> ReviewActionResponse:
    item = _REVIEW_QUEUE.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    item.status = "approved"
    return ReviewActionResponse(status="approved", id=item_id)


@router.post("/review/{item_id}/reject", response_model=ReviewActionResponse)
async def reject_review(item_id: int) -> ReviewActionResponse:
    item = _REVIEW_QUEUE.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    item.status = "rejected"
    return ReviewActionResponse(status="rejected", id=item_id)


__all__ = ["router", "enqueue_review_item"]
