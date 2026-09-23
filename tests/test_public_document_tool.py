from __future__ import annotations

import asyncio
import io
import json
import socket

import httpx
import pytest

from argosy.services import public_document_tool as docs


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    async def __aiter__(self):
        yield self.raw


def install_transport(monkeypatch, respond):
    client_type = httpx.AsyncClient
    seen = []

    def client(**kwargs):
        assert kwargs == dict(verify=True, trust_env=False, follow_redirects=False, auth=None, timeout=20)
        def wrapped(request):
            seen.append(request)
            return respond(request)
        # Even an inherited credential cannot escape the explicit send(auth=None).
        return client_type(**{**kwargs, "auth": ("private", "secret")},
                           cookies={"secret": "cookie"}, transport=httpx.MockTransport(wrapped))

    monkeypatch.setattr(docs.httpx, "AsyncClient", client)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a:
        [(2, 1, 6, "", ("127.0.0.1" if host == "localhost" else "8.8.8.8", 443))])
    return seen


def packet(result):
    return json.loads(result["content"][0]["text"])


def pdf_bytes(count):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for number in range(count):
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 10 Td (Page {number + 1}) Tj ET".encode())
        page[NameObject("/Contents")] = stream
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 21])
async def test_real_isolated_pdf_parser_handles_short_and_bounded_documents(count):
    result = await docs.parse_document(pdf_bytes(count), "application/pdf")
    assert result["inspected_pdf_pages"] == list(range(1, min(count, 20) + 1))
    assert result["more_pdf_pages"] == (count > 20)
    assert "Page 1" in result["text"]
    assert "Page 21" not in result["text"]


@pytest.mark.asyncio
async def test_real_parser_text_budget_and_timeout_reaps_child(monkeypatch):
    result = await docs.parse_document(b"x" * 30000, "text/plain")
    assert len(result["text"]) == 20000 and result["text_truncated"]
    processes = []
    popen = docs.subprocess.Popen
    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(docs.subprocess, "Popen", record)
    monkeypatch.setattr(docs, "PARSE_SECONDS", 0.001)
    with pytest.raises(TimeoutError):
        await docs.parse_document(b"text", "text/plain")
    assert processes and processes[0].poll() is not None


@pytest.mark.asyncio
async def test_capture_has_no_auth_and_saves_bytes_before_extraction(tmp_path, monkeypatch):
    raw = pdf_bytes(2)
    seen = install_transport(monkeypatch, lambda req: httpx.Response(200,
        stream=Body(raw), headers={"content-type": "application/pdf"}))
    result = packet(await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/document.pdf"}))
    assert result["status"] == "captured" and result["fact_verification"] == "not_performed"
    assert result["inspected_pdf_pages"] == [1, 2]
    assert (tmp_path / (result["sha256"] + ".source")).read_bytes() == raw
    assert (tmp_path / (result["receipt_id"] + ".json")).exists()
    assert (tmp_path / (result["fetch_receipt_id"] + ".json")).exists()
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].extensions["sni_hostname"] == "issuer.example"
    assert seen[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["https://localhost/private", "https://issuer.example/" + "a" * 2050,
                                      "https://issuer.example/path\n", "https://user:secret@issuer.example/"])
async def test_redirect_constraints_apply_before_second_request(tmp_path, monkeypatch, location):
    seen = install_transport(monkeypatch, lambda req: httpx.Response(302, headers={"location": location}))
    result = packet(await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/start"}))
    assert result["status"] == "unavailable" and len(seen) == 1


@pytest.mark.asyncio
async def test_compression_is_refused_without_decoding(tmp_path, monkeypatch):
    install_transport(monkeypatch, lambda req: httpx.Response(200, stream=Body(b"not-even-gzip"),
        headers={"content-encoding": "gzip"}))
    result = packet(await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/"}))
    assert "Compressed response refused" in result["error"]


@pytest.mark.asyncio
async def test_failed_attempts_and_concurrent_calls_share_budget(tmp_path, monkeypatch):
    seen = install_transport(monkeypatch, lambda req: httpx.Response(403))
    reader = docs.PublicDocumentReader(tmp_path)
    results = await asyncio.gather(*(reader.read({"url": "https://issuer.example/"}) for _ in range(5)))
    assert reader.calls == 3 and len(seen) == 3
    assert sum("budget exhausted" in packet(r).get("error", "") for r in results) == 2


@pytest.mark.asyncio
async def test_deadline_and_cancelled_parser_preserve_honest_receipts(tmp_path, monkeypatch):
    install_transport(monkeypatch, lambda req: httpx.Response(200, stream=Body(b"public text"),
        headers={"content-type": "text/plain"}))
    async def cancel(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(docs, "parse_document", cancel)
    with pytest.raises(asyncio.CancelledError):
        await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/"})
    receipts = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert any(r["extraction_status"] == "pending" for r in receipts)
    assert any(r["status"] == "cancelled" for r in receipts)
    assert len(list(tmp_path.glob("*.source"))) == 1


@pytest.mark.asyncio
async def test_storage_is_atomic_and_quota_checked_across_writers(tmp_path, monkeypatch):
    receipt = {"url": "https://issuer.example/", "status": "captured"}
    ids = await asyncio.gather(*(asyncio.to_thread(docs.save_artifacts, tmp_path, receipt, b"same") for _ in range(5)))
    assert len(set(ids)) == 1 and len(list(tmp_path.glob("*.source"))) == 1
    assert not list(tmp_path.glob(".capture-*"))
    monkeypatch.setattr(docs, "MAX_DISK_BYTES", 1)
    with pytest.raises(ValueError, match="quota"):
        docs.save_artifacts(tmp_path, {**receipt, "new": True}, b"different")


@pytest.mark.asyncio
async def test_terminal_receipt_reservation_cannot_be_consumed_by_another_fetch(tmp_path, monkeypatch):
    import sqlite3
    receipt = {"fetch_id": "one", "status": "pending"}
    docs.save_artifacts(tmp_path, receipt, b"raw", reserve_terminal=True)
    used = sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
    monkeypatch.setattr(docs, "MAX_DISK_BYTES", used + docs.TERMINAL_RECEIPT_BYTES + 10000)
    with pytest.raises(ValueError, match="quota"):
        docs.save_artifacts(tmp_path, {"fetch_id": "two"}, b"new", reserve_terminal=True)
    docs.save_artifacts(tmp_path, {**receipt, "status": "captured", "text": "x" * 20000}, terminal=True)
    with sqlite3.connect(tmp_path / "quota.sqlite") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_raw_bytes_published_before_fetch_receipt(tmp_path, monkeypatch):
    published = []
    real = docs._atomic_publish
    def publish(path, data):
        published.append(path.suffix)
        return real(path, data)
    monkeypatch.setattr(docs, "_atomic_publish", publish)
    docs.save_artifacts(tmp_path, {"fetch_id": "x"}, b"source")
    assert published == [".source", ".json"]


@pytest.mark.asyncio
async def test_filesystem_errors_never_reveal_private_paths(tmp_path, monkeypatch):
    async def fetch(url):
        raise OSError("Access denied C:/Users/private-person/secret")
    def save(*args, **kwargs):
        raise OSError("Access denied C:/Users/private-person/secret")
    monkeypatch.setattr(docs, "_fetch", fetch)
    monkeypatch.setattr(docs, "save_artifacts", save)
    result = await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/"})
    assert "private-person" not in str(result)
    assert packet(result)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_failed_cancel_receipt_does_not_replace_cancellation(tmp_path, monkeypatch):
    async def fetch(url):
        raise asyncio.CancelledError()
    def save(*args, **kwargs):
        raise OSError("Disk unavailable")
    monkeypatch.setattr(docs, "_fetch", fetch)
    monkeypatch.setattr(docs, "save_artifacts", save)
    with pytest.raises(asyncio.CancelledError):
        await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/"})


@pytest.mark.asyncio
async def test_whole_fetch_deadline_bounds_slow_stream(tmp_path, monkeypatch):
    async def fetch(url):
        await asyncio.sleep(10)
    monkeypatch.setattr(docs, "_fetch", fetch)
    monkeypatch.setattr(docs, "FETCH_SECONDS", 0.001)
    result = packet(await docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/"}))
    assert result["error"] == "Public document operation timed out"


@pytest.mark.asyncio
@pytest.mark.parametrize("block_phase", ["pending", "terminal"])
async def test_cancellation_settles_publication_before_terminal_cleanup(tmp_path, monkeypatch, block_phase):
    import sqlite3
    import threading
    entered, release = threading.Event(), threading.Event()
    saved = []
    real = docs.save_artifacts
    def save(root, receipt, raw=None, **kwargs):
        selected = kwargs.get("reserve_terminal") if block_phase == "pending" else receipt["status"] == "captured"
        if selected:
            entered.set()
            assert release.wait(5)
        result = real(root, receipt, raw, **kwargs)
        saved.append(receipt["status"])
        if block_phase == "terminal" and receipt["status"] == "captured":
            # A competing publisher runs before the cancelled caller resumes.
            real(root, {"fetch_id": "competitor", "status": "unavailable"}, b"other source")
            # No remaining quota may be needed for a second terminal receipt.
            monkeypatch.setattr(docs, "MAX_DISK_BYTES", 1)
        return result
    async def fetch(url):
        return b"source", "text/plain", url
    async def parse(*args):
        return {"text": "source", "inspected_pdf_pages": [], "more_pdf_pages": False, "text_truncated": False}
    monkeypatch.setattr(docs, "save_artifacts", save)
    monkeypatch.setattr(docs, "_fetch", fetch)
    monkeypatch.setattr(docs, "parse_document", parse)
    task = asyncio.create_task(docs.PublicDocumentReader(tmp_path).read({"url": "https://issuer.example/"}))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved[-1] == ("cancelled" if block_phase == "pending" else "captured")
    if block_phase == "terminal":
        assert "cancelled" not in saved
    assert saved[0] == "unavailable"  # pending fetch receipt
    with sqlite3.connect(tmp_path / "quota.sqlite") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unicode_terminal_envelope_fits_reservation_near_quota(tmp_path, monkeypatch):
    extracted = await docs.parse_document(("\U0001f680" * 20000).encode(), "text/plain")
    assert extracted["text_truncated"]
    assert len(json.dumps(extracted, ensure_ascii=True).encode()) <= 120000
    url = "https://issuer.example/" + "a" * 1900
    pending = {"fetch_id": "unicode", "url": url, "final_url": url, "content_type": "x" * 256,
               "status": "unavailable", "fetch_status": "captured", "extraction_status": "pending"}
    fetch_id = docs.save_artifacts(tmp_path, pending, b"raw", reserve_terminal=True)
    used = sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
    monkeypatch.setattr(docs, "MAX_DISK_BYTES", used + docs.TERMINAL_RECEIPT_BYTES + 30000)
    docs.save_artifacts(tmp_path, {"fetch_id": "other"}, b"y" * 20000)
    complete = {**pending, **extracted, "status": "captured", "fetch_receipt_id": fetch_id}
    final_id = docs.save_artifacts(tmp_path, complete, terminal=True)
    assert (tmp_path / (final_id + ".json")).stat().st_size <= docs.TERMINAL_RECEIPT_BYTES
    with pytest.raises(ValueError, match="reserved byte budget"):
        docs.save_artifacts(tmp_path, {"text": "\U0001f680" * 20000}, terminal=True)


@pytest.mark.parametrize("url", ["http://issuer.example/", "file:///secret", "https://u:p@issuer.example/",
    "https://issuer.example:444/", "https://issuer.example/#fragment", "https://issuer.example/%0a", None])
def test_url_validation(url):
    with pytest.raises(ValueError):
        docs.normalize_public_url(url)
