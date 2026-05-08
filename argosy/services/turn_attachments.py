"""Wave 5 — Advisor chat upload helper.

The advisor chat now accepts file attachments alongside the text message
(text/markdown documents and images). This module:
  - Saves each upload to `<ARGOSY_HOME>/uploads/<user_id>/<turn_uuid>/`
  - Classifies MIME → kind ("text" | "image")
  - Rejects unsupported MIMEs with a 415-friendly exception

Size limits (hardcoded module constants below; not yet plumbed through
`argosy.toml`):
  - per file: 10 MB (`MAX_BYTES_PER_FILE`)
  - per turn: 20 MB total (`MAX_BYTES_PER_TURN`)

Spec: docs/superpowers/specs (Wave 5 inline; SDD §6.14).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel

from argosy.config import get_settings
from argosy.logging import get_logger

log = get_logger(__name__)


# Per-file and per-turn size caps (bytes). See module docstring.
MAX_BYTES_PER_FILE = 10 * 1024 * 1024  # 10 MB
MAX_BYTES_PER_TURN = 20 * 1024 * 1024  # 20 MB


# NTFS forbids these in filenames; ext4/APFS tolerate most but `:` is still
# special on macOS (HFS resource fork). Replace with `_` so an upload from
# a quirky source doesn't 500 on Windows or silently corrupt on macOS.
_ILLEGAL_FILENAME_CHARS = '<>:"|?*\x00'


def _sanitize_filename(name: str) -> str:
    """Strip directory components AND replace OS-illegal chars with `_`."""
    base = os.path.basename(name) or "attachment"
    return "".join("_" if c in _ILLEGAL_FILENAME_CHARS else c for c in base)


_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/json",
    "application/x-yaml",
    "text/yaml",
    "text/csv",
}
_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
}
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".yaml", ".yml", ".json", ".csv"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class Attachment(BaseModel):
    """A single saved upload from a chat turn."""

    kind: Literal["text", "image"]
    path: str
    mime_type: str
    original_name: str
    size_bytes: int


class AttachmentTooLargeError(HTTPException):
    """HTTP 413 — file exceeds size cap."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=413, detail=detail)


class AttachmentUnsupportedError(HTTPException):
    """HTTP 415 — MIME type or extension not allowed."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=415, detail=detail)


def _classify(mime_type: str, original_name: str) -> Literal["text", "image"]:
    """Map MIME + extension → 'text' | 'image'. Raise 415 for anything else."""
    mt = (mime_type or "").lower().strip()
    ext = Path(original_name or "").suffix.lower()

    if mt in _IMAGE_MIMES or mt.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    if mt in _TEXT_MIMES or mt.startswith("text/") or ext in _TEXT_EXTS:
        return "text"
    raise AttachmentUnsupportedError(
        f"unsupported attachment type: mime={mt!r} ext={ext!r}; "
        "Wave 5 accepts text/markdown and images only"
    )


def _uploads_root(user_id: str, turn_uuid: str) -> Path:
    """`<ARGOSY_HOME>/uploads/<user_id>/<turn_uuid>/` — created on demand."""
    home = Path(get_settings().home)
    root = home / "uploads" / user_id / turn_uuid
    root.mkdir(parents=True, exist_ok=True)
    return root


async def save_attachment(
    *, user_id: str, turn_uuid: str, upload: UploadFile,
) -> Attachment:
    """Persist a single FastAPI UploadFile and return its typed Attachment.

    Raises:
        AttachmentTooLargeError: file > MAX_BYTES_PER_FILE
        AttachmentUnsupportedError: MIME/extension not in allowlist
    """
    original_name = upload.filename or "attachment"
    mime_type = upload.content_type or "application/octet-stream"
    kind = _classify(mime_type, original_name)

    # Stream-read so we can short-circuit on size.
    contents = await upload.read()
    size = len(contents)
    if size > MAX_BYTES_PER_FILE:
        raise AttachmentTooLargeError(
            f"attachment {original_name!r} is {size} bytes; cap is {MAX_BYTES_PER_FILE}"
        )

    root = _uploads_root(user_id, turn_uuid)
    # Sanitize filename — strip directory traversal AND OS-illegal chars
    # (`<>:"|?*` are forbidden on NTFS; `:` opens an alternate data stream
    # and silently corrupts the save).
    safe_name = _sanitize_filename(original_name)
    target = root / safe_name
    # If a file with the same name already exists in this turn dir, suffix it.
    if target.exists():
        stem, ext = os.path.splitext(safe_name)
        i = 1
        while True:
            candidate = root / f"{stem}-{i}{ext}"
            if not candidate.exists():
                target = candidate
                break
            i += 1

    target.write_bytes(contents)
    log.info(
        "turn_attachment.saved",
        user_id=user_id,
        turn_uuid=turn_uuid,
        kind=kind,
        size_bytes=size,
        original_name=original_name,
    )
    return Attachment(
        kind=kind,
        path=str(target),
        mime_type=mime_type,
        original_name=original_name,
        size_bytes=size,
    )


async def save_attachments_with_total_cap(
    *, user_id: str, turn_uuid: str, uploads: list[UploadFile],
) -> list[Attachment]:
    """Save many; enforce the per-turn cap on the running total."""
    saved: list[Attachment] = []
    running = 0
    for u in uploads:
        att = await save_attachment(user_id=user_id, turn_uuid=turn_uuid, upload=u)
        running += att.size_bytes
        if running > MAX_BYTES_PER_TURN:
            # Roll back files written so far so a partial-save doesn't leak.
            for prior in saved + [att]:
                try:
                    Path(prior.path).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
            raise AttachmentTooLargeError(
                f"turn attachments total {running} bytes exceeds cap {MAX_BYTES_PER_TURN}"
            )
        saved.append(att)
    return saved


__all__ = [
    "Attachment",
    "AttachmentTooLargeError",
    "AttachmentUnsupportedError",
    "MAX_BYTES_PER_FILE",
    "MAX_BYTES_PER_TURN",
    "save_attachment",
    "save_attachments_with_total_cap",
]
