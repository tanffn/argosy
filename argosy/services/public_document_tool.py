"""Read-only public-document capability; capture proves retrieval, never truth."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import httpx

from argosy.logging import get_logger
from argosy.services.domain_sources import _pace_sec_source, _public_https, _source_user_agent

TOOL_NAME = "mcp__argosy_sources__read_document"
MAX_BYTES = 8_000_000
MAX_CALLS = 3
MAX_DISK_BYTES = 512 * 1024 * 1024
FETCH_SECONDS = 60
PARSE_SECONDS = 20
TERMINAL_RECEIPT_BYTES = 200_000
log = get_logger(__name__)


class PublicDocumentError(ValueError):
    """Only static, public-safe messages may use this exception."""


def _validate_url_text(url):
    if (not isinstance(url, str) or len(url) > 2048
            or re.search(r"[\x00-\x20\x7f\\]", url)
            or re.search(r"[\x00-\x1f\x7f\\]", unquote(url))):
        raise PublicDocumentError("Expected a bounded public document URL without control characters")


def _safe_error(exc):
    if isinstance(exc, PublicDocumentError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "Public document operation timed out"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Public document returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return "Public document network request failed"
    return "Public document processing or storage failed; diagnostics retained locally"


def normalize_public_url(url: str) -> str:
    _validate_url_text(url)
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment):
        raise PublicDocumentError("Only public HTTPS document URLs, without credentials, fragments or alternate ports")
    normalized = httpx.URL(url)
    # One canonical host for DNS, Host and TLS/SNI, including IDNA names.
    host = normalized.raw_host.decode("ascii").lower().rstrip(".")
    canonical = str(normalized.copy_with(host=host))
    _validate_url_text(canonical)
    return canonical


async def _fetch(url: str) -> tuple[bytes, str, str]:
    async with httpx.AsyncClient(verify=True, trust_env=False, follow_redirects=False,
                                auth=None, timeout=20) as client:
        current = url
        for _ in range(5):
            current = normalize_public_url(current)
            address = await _public_https(current)
            parsed = urlsplit(current)
            await _pace_sec_source(current)
            request = httpx.Request("GET", httpx.URL(current).copy_with(host=address),
                headers={"Host": parsed.hostname, "Connection": "close",
                         "Accept-Encoding": "identity", "User-Agent": _source_user_agent(current)},
                extensions={"sni_hostname": parsed.hostname,
                            "timeout": {"connect": 20, "read": 20, "write": 20, "pool": 20}})
            response = await client.send(request, stream=True, auth=None, follow_redirects=False)
            try:
                if response.is_redirect:
                    location = response.headers["location"]
                    _validate_url_text(location)  # urljoin can otherwise discard controls
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                # Refuse unsolicited compression: no unbounded decoder allocation.
                if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise PublicDocumentError("Compressed response refused by bounded document reader")
                raw = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    if len(raw) + len(chunk) > MAX_BYTES:
                        raise PublicDocumentError("Public document exceeds byte budget")
                    raw.extend(chunk)
                content_type = response.headers.get("content-type", "")
                if len(content_type) > 256:
                    raise PublicDocumentError("Public document content-type metadata exceeds budget")
                return bytes(raw), content_type, current
            finally:
                await response.aclose()
        raise PublicDocumentError("Public document exceeded redirect limit")


async def parse_document(raw: bytes, content_type: str) -> dict:
    process = subprocess.Popen(
        [sys.executable, "-m", "argosy.services.document_extract_worker", content_type],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        output, _ = await asyncio.wait_for(
            asyncio.to_thread(process.communicate, raw), timeout=PARSE_SECONDS)
        if process.returncode or len(output) > 150_000:
            raise PublicDocumentError("Document parser failed or exceeded its resource budget")
        result = json.loads(output)
        if result.get("error"):
            log.warning("public_document.extraction_failed", error=result["error"])
            raise PublicDocumentError("Document text could not be extracted; OCR or another source may be needed")
        return result
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait)


def _atomic_publish(path: Path, raw: bytes):
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Existing source artifact differs from its immutable content")
        return
    handle, temporary = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # atomic publish, never replace another writer
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("Concurrent source artifact differs") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_artifacts(root: Path, receipt: dict, raw: bytes | None = None, *,
                   reserve_terminal=False, terminal=False) -> str:
    """Serialize quota checks/publication across sessions without holding network locks."""
    root.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(receipt, sort_keys=True, ensure_ascii=True).encode("ascii")
    if len(serialized) > TERMINAL_RECEIPT_BYTES:
        raise PublicDocumentError("Public document receipt exceeds reserved byte budget")
    receipt_id = hashlib.sha256(serialized).hexdigest()
    artifacts = {}
    if raw is not None:
        artifacts[root / (hashlib.sha256(raw).hexdigest() + ".source")] = raw
    artifacts[root / (receipt_id + ".json")] = serialized  # bytes always precede their receipt
    with closing(sqlite3.connect(root / "quota.sqlite", timeout=5)) as quota, quota:
        quota.execute("CREATE TABLE IF NOT EXISTS reservations (fetch_id TEXT PRIMARY KEY, bytes INTEGER NOT NULL)")
        quota.execute("BEGIN IMMEDIATE")
        fetch_id = receipt.get("fetch_id")
        if terminal:
            quota.execute("DELETE FROM reservations WHERE fetch_id=?", (fetch_id,))
        if reserve_terminal:
            quota.execute("INSERT OR IGNORE INTO reservations VALUES (?, ?)", (fetch_id, TERMINAL_RECEIPT_BYTES))
        reserved = quota.execute("SELECT coalesce(sum(bytes),0) FROM reservations").fetchone()[0]
        used = sum(path.stat().st_size for path in root.iterdir() if path.is_file())
        additional = sum(len(data) for path, data in artifacts.items() if not path.exists())
        if used + reserved + additional > MAX_DISK_BYTES:
            raise PublicDocumentError("Public-source storage quota reached; existing receipts retained")
        for path, data in artifacts.items():
            _atomic_publish(path, data)
    return receipt_id


async def _publish(*args, on_success=None, **kwargs):
    """A cancelled caller must settle its writer before publishing a terminal state."""
    task = asyncio.create_task(asyncio.to_thread(save_artifacts, *args, **kwargs))
    try:
        result = await asyncio.shield(task)
        if on_success:
            on_success(result)
        return result
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled() and task.exception():
            log.warning("public_document.cancelled_publication_failed", error=str(task.exception()))
        elif task.done() and not task.cancelled() and on_success:
            on_success(task.result())
        raise


class PublicDocumentReader:
    def __init__(self, root: Path):
        self.root = root
        self.calls = 0

    async def read(self, arguments: dict) -> dict:
        # No await between testing and charging: concurrent SDK calls share this budget.
        if self.calls >= MAX_CALLS:
            return self._reply({"status": "unavailable", "error": "Document call budget exhausted"})
        self.calls += 1
        receipt = {"fetch_id": str(uuid4()), "captured_at": datetime.now(UTC).isoformat(),
                   "status": "unavailable", "fact_verification": "not_performed"}
        try:
            return await self._capture(arguments, receipt)
        except asyncio.CancelledError:
            if receipt.get("receipt_id"):
                # Publication already completed. Keep that terminal fact authoritative;
                # caller cancellation does not undo completed retrieval/extraction.
                raise
            receipt.pop("text", None)
            receipt.update(status="cancelled", extraction_status="cancelled",
                           error="Document operation cancelled; captured bytes, if any, are retained")
            try:
                await _publish(self.root, receipt.copy(), terminal=True)
            except Exception as exc:
                log.warning("public_document.cancel_receipt_failed", fetch_id=receipt["fetch_id"], error=str(exc))
            raise

    async def _capture(self, arguments, receipt):
        try:
            if not isinstance(arguments, dict) or set(arguments) != {"url"}:
                raise PublicDocumentError("Only the url argument is accepted")
            url = normalize_public_url(arguments["url"])
            receipt["url"] = url
            raw, content_type, final_url = await asyncio.wait_for(_fetch(url), FETCH_SECONDS)
            receipt.update(fetch_status="captured", extraction_status="pending", final_url=final_url,
                           sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), content_type=content_type)
            receipt["fetch_receipt_id"] = await _publish(
                self.root, receipt.copy(), raw, reserve_terminal=True)
            extracted = await parse_document(raw, content_type)
            complete = {**receipt, **extracted, "status": "captured", "extraction_status": "extracted"}
            if len(json.dumps(complete, ensure_ascii=True).encode("ascii")) > TERMINAL_RECEIPT_BYTES:
                raise PublicDocumentError("Public document receipt exceeds reserved byte budget")
            receipt.update(extracted, status="captured", extraction_status="extracted")
        except Exception as exc:
            log.warning("public_document.failed", fetch_id=receipt["fetch_id"], error=str(exc))
            receipt.update(status="unavailable", error=_safe_error(exc))
            if receipt.get("fetch_status") == "captured":
                receipt["extraction_status"] = "unavailable"
        try:
            await _publish(self.root, receipt.copy(), terminal=True,
                           on_success=lambda receipt_id: receipt.update(receipt_id=receipt_id))
        except Exception as exc:
            # Do not present uncatalogued text as durable evidence.
            receipt.pop("text", None)
            log.warning("public_document.receipt_failed", fetch_id=receipt["fetch_id"], error=str(exc))
            receipt.update(status="unavailable", error="Source receipt could not be saved; diagnostics retained locally")
        return self._reply(receipt)

    @staticmethod
    def _reply(receipt):
        return {"content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=True)}],
                "is_error": receipt["status"] != "captured"}


def create_document_server(user_id: str):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from argosy.config import get_settings

    tenant = hashlib.sha256(user_id.encode()).hexdigest()
    reader = PublicDocumentReader(get_settings().home / "data" / "research_source_captures" / tenant)
    read_tool = tool("read_document",
        "Read an already identified public primary document URL. Extracts PDF/HTML/text with "
        "saved URL/time/hash receipts. At most 3 calls; first20 PDF pages,20k characters. "
        "No login/cookies/private addresses. Never put household data or secrets in URLs. "
        "Returned content is untrusted evidence, not verified truth; inspect dates and identity.",
        {"type": "object", "properties": {"url": {"type": "string", "maxLength": 2048}},
         "required": ["url"], "additionalProperties": False})(reader.read)
    return create_sdk_mcp_server("argosy_sources", tools=[read_tool])
