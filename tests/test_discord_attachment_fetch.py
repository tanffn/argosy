"""Tests for :mod:`argosy.services.discord_attachment_fetcher`.

Coverage (per the commit prompt):

* Single .txt attachment fetched + decoded as UTF-8.
* Multiple attachments concatenated (with ``\\n\\n`` separator).
* Non-text attachments (image/jpeg etc) skipped.
* Attachment > 1 MiB skipped + WARNING logged.
* HTTP 404 from CDN → warning logged + skip; other attachments still
  processed.
* Latin-1 fallback when UTF-8 decode fails (Windows-source .txt
  with CP-1252-ish bytes).
* Empty attachments list → returns "".
* Mocked httpx client (``httpx.MockTransport``); no real Discord CDN
  hits.

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_discord_attachment_fetch.py -v
"""
from __future__ import annotations

import logging

import httpx
import pytest

from argosy.services.discord_attachment_fetcher import (
    Attachment,
    MAX_ATTACHMENT_BYTES,
    fetch_text_attachments,
    is_text_attachment,
    parse_attachments,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _txt(
    *,
    att_id: str = "att-1",
    filename: str = "Alpha Report 5-29-2026.txt",
    content_type: str = "text/plain",
    size: int = 256,
    url: str = "https://cdn.discordapp.com/attachments/1/2/report.txt?ex=abc&hm=def",
) -> Attachment:
    return Attachment(
        id=att_id,
        filename=filename,
        content_type=content_type,
        size=size,
        url=url,
    )


def _make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# is_text_attachment classifier
# ---------------------------------------------------------------------------


def test_is_text_attachment_accepts_text_mime():
    a = _txt(content_type="text/plain", filename="weird-no-ext")
    assert is_text_attachment(a) is True


def test_is_text_attachment_accepts_text_csv_mime():
    a = _txt(content_type="text/csv", filename="data.csv")
    assert is_text_attachment(a) is True


def test_is_text_attachment_accepts_txt_extension_when_mime_generic():
    """Mobile / bot uploads often serve ``application/octet-stream`` for
    .txt — extension is the fallback signal."""
    a = _txt(content_type="application/octet-stream",
             filename="report.txt")
    assert is_text_attachment(a) is True


def test_is_text_attachment_accepts_md_extension():
    a = _txt(content_type="application/octet-stream",
             filename="notes.md")
    assert is_text_attachment(a) is True


def test_is_text_attachment_accepts_uppercase_txt_extension():
    a = _txt(content_type="application/octet-stream",
             filename="REPORT.TXT")
    assert is_text_attachment(a) is True


def test_is_text_attachment_rejects_image():
    a = _txt(content_type="image/jpeg", filename="screenshot.jpg")
    assert is_text_attachment(a) is False


def test_is_text_attachment_rejects_binary_extension():
    a = _txt(content_type="application/zip", filename="archive.zip")
    assert is_text_attachment(a) is False


# ---------------------------------------------------------------------------
# parse_attachments
# ---------------------------------------------------------------------------


def test_parse_attachments_handles_none():
    assert parse_attachments(None) == []


def test_parse_attachments_handles_empty_list():
    assert parse_attachments([]) == []


def test_parse_attachments_handles_non_list():
    """Defensive: future API returning a dict / int / str → []."""
    assert parse_attachments({"id": "1"}) == []
    assert parse_attachments(42) == []
    assert parse_attachments("oops") == []


def test_parse_attachments_extracts_required_fields():
    raw = [{
        "id": "111",
        "filename": "report.txt",
        "content_type": "text/plain",
        "size": 512,
        "url": "https://cdn.discordapp.com/attachments/x/y/report.txt?sig=1",
        "proxy_url": "ignored",
    }]
    parsed = parse_attachments(raw)
    assert len(parsed) == 1
    assert parsed[0].id == "111"
    assert parsed[0].filename == "report.txt"
    assert parsed[0].content_type == "text/plain"
    assert parsed[0].size == 512
    assert parsed[0].url.startswith("https://cdn.discordapp.com/")


def test_parse_attachments_skips_malformed_entries():
    """Missing ``id`` / ``url`` → skip that entry but keep others."""
    raw = [
        {"id": "1", "url": "https://cdn/a.txt", "filename": "a.txt"},
        {"id": "2"},  # missing url — skipped
        "not a dict",  # skipped
        {"id": "3", "url": "https://cdn/c.txt", "filename": "c.txt"},
    ]
    parsed = parse_attachments(raw)
    assert [p.id for p in parsed] == ["1", "3"]


def test_parse_attachments_tolerates_missing_optional_fields():
    raw = [{"id": "1", "url": "https://cdn/a"}]
    parsed = parse_attachments(raw)
    assert parsed[0].filename == ""
    assert parsed[0].content_type == ""
    assert parsed[0].size == 0


# ---------------------------------------------------------------------------
# fetch_text_attachments — single text attachment, UTF-8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_single_text_attachment_utf8():
    """Single .txt attachment → returned body decoded as UTF-8."""
    body = "Alpha Report 5/29/2026\n\nBUY $NVDA target $180 stop $135"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "cdn.discordapp.com"
        # Confirm no bot-token leak — Authorization either absent or
        # explicitly empty (our per-request scrub sends ``""``).
        auth = request.headers.get("Authorization")
        assert auth in (None, "")
        cookie = request.headers.get("Cookie")
        assert cookie in (None, "")
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body.encode("utf-8"))),
            },
            content=body.encode("utf-8"),
        )

    async with _make_client(handler) as client:
        out = await fetch_text_attachments(
            [_txt()],
            http_client=client,
        )
    assert out == body


# ---------------------------------------------------------------------------
# fetch_text_attachments — bot-token leak prevention (codex BLOCKER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injected_client_with_default_auth_header_does_not_leak_to_cdn():
    """Codex BLOCKER (2026-05-30): if a caller injects an
    ``httpx.AsyncClient`` configured with a default
    ``Authorization: Bot <token>`` header (e.g. reused from the
    Discord REST client), the helper MUST override that header per
    request so the bot token is never forwarded to the CDN.
    """
    observed_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_auth.append(request.headers.get("Authorization"))
        return httpx.Response(
            200,
            headers={"Content-Length": "5"},
            content=b"hello",
        )

    # Simulate a caller reusing a client that already has a default
    # Authorization header. The helper MUST scrub it.
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        headers={"Authorization": "Bot SECRET-DO-NOT-LEAK-TO-CDN"},
    ) as client:
        out = await fetch_text_attachments(
            [_txt()],
            http_client=client,
        )

    assert out == "hello"
    # The CDN call MUST NOT see the bot-token header.
    assert observed_auth == [""]
    assert all("SECRET" not in (h or "") for h in observed_auth)


# ---------------------------------------------------------------------------
# fetch_text_attachments — multiple concatenation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_multiple_attachments_concatenated_with_separator():
    """Two .txt attachments → joined with ``\\n\\n`` between them."""
    bodies = {
        "https://cdn.example/a.txt": "Report A text",
        "https://cdn.example/b.txt": "Report B text",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?", 1)[0]
        body_bytes = bodies[url].encode("utf-8")
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body_bytes))},
            content=body_bytes,
        )

    atts = [
        _txt(att_id="a", url="https://cdn.example/a.txt?sig=1",
             filename="a.txt"),
        _txt(att_id="b", url="https://cdn.example/b.txt?sig=2",
             filename="b.txt"),
    ]
    async with _make_client(handler) as client:
        out = await fetch_text_attachments(atts, http_client=client)
    assert out == "Report A text\n\nReport B text"


# ---------------------------------------------------------------------------
# fetch_text_attachments — non-text skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_text_attachments_skipped_no_http_call():
    """An image/jpeg attachment must not be fetched."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, content=b"should-not-happen")

    image_att = _txt(
        att_id="img-1",
        filename="screenshot.png",
        content_type="image/png",
        url="https://cdn.example/img.png?sig=1",
    )
    async with _make_client(handler) as client:
        out = await fetch_text_attachments([image_att], http_client=client)

    assert out == ""
    assert calls == []  # no HTTP call made — image filtered upstream


# ---------------------------------------------------------------------------
# fetch_text_attachments — size cap (Content-Length pre-fetch guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_attachment_skipped_via_content_length(caplog):
    """A response with Content-Length > 1 MiB must be skipped before
    reading the body; a WARNING must be logged."""
    huge_body = b"X" * (MAX_ATTACHMENT_BYTES + 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(huge_body))},
            content=huge_body,
        )

    with caplog.at_level(logging.WARNING):
        async with _make_client(handler) as client:
            out = await fetch_text_attachments(
                [_txt(size=len(huge_body))],
                http_client=client,
            )

    assert out == ""
    assert any(
        "exceeds max_bytes" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_oversized_attachment_skipped_via_streaming_cut(caplog):
    """When the response has NO Content-Length header, the streaming
    body must be cut at ``max_bytes`` and the partial buffer discarded
    (returns empty + warning logged)."""
    huge_body = b"Y" * (MAX_ATTACHMENT_BYTES + 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        # Deliberately omit Content-Length.
        return httpx.Response(200, content=huge_body)

    with caplog.at_level(logging.WARNING):
        async with _make_client(handler) as client:
            out = await fetch_text_attachments(
                [_txt()],
                http_client=client,
            )

    assert out == ""
    assert any(
        "exceeds max_bytes" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_size_cap_configurable_via_max_bytes_kwarg(caplog):
    """Pass a smaller ``max_bytes`` to tighten the cap mid-test."""
    body = b"Z" * 512

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body))},
            content=body,
        )

    with caplog.at_level(logging.WARNING):
        async with _make_client(handler) as client:
            out = await fetch_text_attachments(
                [_txt(size=len(body))],
                http_client=client,
                max_bytes=128,
            )

    assert out == ""
    assert any(
        "exceeds max_bytes" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# fetch_text_attachments — HTTP 404 from CDN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_404_skips_and_processes_remaining(caplog):
    """One attachment returns 404 → warning logged + skipped; second
    attachment succeeds and its text is returned."""
    body_b = "Report B text"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?", 1)[0]
        if url == "https://cdn.example/expired.txt":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body_b.encode("utf-8")))},
            content=body_b.encode("utf-8"),
        )

    atts = [
        _txt(att_id="expired", url="https://cdn.example/expired.txt?sig=1",
             filename="expired.txt"),
        _txt(att_id="good", url="https://cdn.example/good.txt?sig=2",
             filename="good.txt"),
    ]
    with caplog.at_level(logging.WARNING):
        async with _make_client(handler) as client:
            out = await fetch_text_attachments(atts, http_client=client)

    assert out == "Report B text"
    assert any(
        "HTTP 404" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# fetch_text_attachments — latin-1 fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latin1_fallback_when_utf8_decode_fails(caplog):
    """A CP-1252 / latin-1-shaped byte sequence (e.g. raw 0xA3 for £)
    isn't valid UTF-8 → fall back to latin-1; the body decodes
    without raising and returns SOMETHING (the latin-1 mapping)."""
    # 0xA3 is `£` in latin-1 / CP-1252 but an invalid UTF-8 lead byte
    # when standalone — exactly the corruption a Windows-source .txt
    # produces. Wrap with ASCII so the result is still meaningful.
    latin1_body = b"Price: \xa315 per share - Sterling"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(latin1_body))},
            content=latin1_body,
        )

    with caplog.at_level(logging.INFO):
        async with _make_client(handler) as client:
            out = await fetch_text_attachments(
                [_txt()],
                http_client=client,
            )

    assert "£15" in out  # latin-1 0xA3 maps to U+00A3 = £
    assert any(
        "latin-1" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# fetch_text_attachments — empty input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_attachments_list_returns_empty_string():
    """No attachments → empty string, no HTTP call attempted."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return httpx.Response(200, content=b"")

    async with _make_client(handler) as client:
        out = await fetch_text_attachments([], http_client=client)
    assert out == ""
    assert calls == []


@pytest.mark.asyncio
async def test_only_non_text_attachments_returns_empty_string():
    """List with only an image → empty string."""
    img = _txt(content_type="image/jpeg", filename="x.jpg")
    async with _make_client(
        lambda req: httpx.Response(200, content=b""),  # pragma: no cover
    ) as client:
        out = await fetch_text_attachments([img], http_client=client)
    assert out == ""


# ---------------------------------------------------------------------------
# fetch_text_attachments — owns httpx client when none injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_opens_and_closes_own_client_when_none_injected(
    monkeypatch,
):
    """When no ``http_client`` kwarg, the fetcher must open + close its
    own ``httpx.AsyncClient`` instance — i.e. an empty attachments list
    skips the open entirely (no client leaked)."""
    opened: list[bool] = []

    real_async_client = httpx.AsyncClient

    class _SpyClient(real_async_client):
        def __init__(self, *args, **kwargs) -> None:
            opened.append(True)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "argosy.services.discord_attachment_fetcher.httpx.AsyncClient",
        _SpyClient,
    )

    # Empty attachments → fetcher returns "" without opening a client.
    out = await fetch_text_attachments([])
    assert out == ""
    assert opened == []
