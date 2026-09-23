import socket

import httpx
import pytest

from argosy.services import domain_sources as sources


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a: [(2, 1, 6, '', ('8.8.8.8', 443))])


@pytest.mark.asyncio
async def test_capture_preserves_bytes_provenance_and_text(tmp_path, public_dns):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text='<html><script>ignore</script><p>Article 12</p></html>', headers={'content-type': 'text/html'}))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await sources.capture_source('https://example.com/rules', root=tmp_path, client=client)
    assert result['status'] == 'captured'
    assert result['text'] == 'Article 12'
    assert (tmp_path / (result['sha256'] + '.source')).exists()
    assert (tmp_path / (result['receipt_id'] + '.json')).exists()
    assert 'verification' not in result


@pytest.mark.asyncio
async def test_sec_capture_uses_shared_identity_and_pacing_on_each_hop(tmp_path, public_dns, monkeypatch):
    from argosy.adapters.data import sec_13f_adapter, sec_rate_limit
    monkeypatch.setattr(sec_13f_adapter, '_default_headers', lambda: {'User-Agent': 'Argosy test contact@example.com'})
    slots, requests = [], []
    async def reserve(**kwargs):
        slots.append(kwargs)
    monkeypatch.setattr(sec_rate_limit, 'wait_for_sec_request_slot', reserve)
    def transport(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(302, headers={'location': 'https://www.sec.gov/final'})
        if len(requests) == 2:
            return httpx.Response(302, headers={'location': 'https://sec.gov.example.com/final'})
        return httpx.Response(200, text='Public text')
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await sources.capture_source('https://www.sec.gov/start', root=tmp_path, client=client)
    assert result['status'] == 'captured'
    assert len(slots) == 2
    assert all(s['interval_seconds'] == sec_rate_limit.MIN_SEC_REQUEST_INTERVAL_SECONDS for s in slots)
    assert [r.headers['User-Agent'] for r in requests] == [
        'Argosy test contact@example.com', 'Argosy test contact@example.com',
        'Argosy knowledge-source verification']
    assert all(r.url.host == '8.8.8.8' and 'cookie' not in r.headers for r in requests)
    assert all(r.headers['accept-encoding'] == 'gzip, deflate' for r in requests)


@pytest.mark.asyncio
async def test_compressed_source_negotiation_keeps_decoded_byte_limit(tmp_path, public_dns, monkeypatch):
    import gzip
    monkeypatch.setattr(sources, 'MAX_BYTES', 64)
    def transport(req):
        assert 'gzip' in req.headers['accept-encoding']
        return httpx.Response(200, content=gzip.compress(b'x' * 128),
                              headers={'content-encoding': 'gzip', 'content-type': 'text/plain'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await sources.capture_source('https://example.com/rule', root=tmp_path, client=client)
    assert result['status'] == 'unavailable'
    assert 'byte limit' in result['error']


@pytest.mark.asyncio
async def test_identical_fetches_at_same_time_have_distinct_receipts(tmp_path, public_dns, monkeypatch):
    from datetime import datetime, timezone
    class FixedClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(sources, 'datetime', FixedClock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text='same'))) as client:
        first = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
        second = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert first['sha256'] == second['sha256']
    assert first['captured_at'] == second['captured_at']
    assert first['fetch_id'] != second['fetch_id']
    assert first['receipt_id'] != second['receipt_id']
    assert first['fetch_receipt_id'] != second['fetch_receipt_id']
    assert len(list(tmp_path.glob('*.json'))) == 4


@pytest.mark.asyncio
async def test_cancelled_extraction_retains_fetch_receipt(tmp_path, public_dns, monkeypatch):
    import asyncio
    import json
    def cancel(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(sources, 'extract_text', cancel)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text='raw source'))) as client:
        with pytest.raises(asyncio.CancelledError):
            await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    receipt = json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))
    assert receipt['url'] == 'https://example.com/'
    assert receipt['fetch_status'] == 'captured' and receipt['extraction_status'] == 'pending'
    assert (tmp_path / (receipt['sha256'] + '.source')).read_bytes() == b'raw source'


@pytest.mark.asyncio
async def test_image_only_pdf_needs_native_fallback(tmp_path, public_dns):
    import io
    from pypdf import PdfWriter
    writer, stream = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=stream.getvalue(), headers={'content-type': 'application/pdf'}))) as client:
        receipt = await sources.capture_source('https://example.com/rules.pdf', root=tmp_path, client=client)
    assert receipt['fetch_status'] == 'captured'
    assert receipt['extraction_status'] == 'unavailable'


@pytest.mark.asyncio
async def test_capture_refuses_private_redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a: [(2, 1, 6, '', ('127.0.0.1' if host == 'localhost' else '8.8.8.8', 443))])
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://localhost/secrets'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await sources.capture_source('https://example.com/rules', root=tmp_path, client=client)
    assert result['status'] == 'unavailable'
    assert 'Non-public' in result['error']
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['http://example.com/', 'file:///secrets', 'https://user:password@example.com/', 'https://example.com:8000/'])
async def test_refuses_unsafe_url_before_request(url, tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail('request sent'))) as client:
        result = await sources.capture_source(url, root=tmp_path, client=client)
    assert result['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_byte_limit_and_http_failure_are_not_evidence(tmp_path, public_dns, monkeypatch):
    monkeypatch.setattr(sources, 'MAX_BYTES', 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text='x' * 11))) as client:
        result = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert result['status'] == 'unavailable' and 'byte limit' in result['error']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(403))) as client:
        result = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert result['status'] == 'unavailable' and '403' in result['error']


@pytest.mark.asyncio
async def test_text_truncation_and_cancellation_are_explicit(tmp_path, public_dns, monkeypatch):
    import asyncio
    monkeypatch.setattr(sources, 'MAX_TEXT', 5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text='long source text'))) as client:
        result = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert result['truncated'] and result['text_chars'] == 16 and result['text'] == 'long '
    async def cancel(url):
        raise asyncio.CancelledError()
    monkeypatch.setattr(sources, '_public_https', cancel)
    async with httpx.AsyncClient() as client:
        with pytest.raises(asyncio.CancelledError):
            await sources.capture_source('https://example.com/', root=tmp_path, client=client)


@pytest.mark.asyncio
async def test_connection_pinned_and_cookies_not_sent(tmp_path, public_dns):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text='source', headers={'set-cookie': 'tracking=secret; Path=/'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), cookies={'existing': 'private'}) as client:
        for _ in range(2):
            await sources.capture_source('https://example.com/rules', root=tmp_path, client=client)
    assert all(r.url.host == '8.8.8.8' and r.headers['host'] == 'example.com' for r in requests)
    assert all(r.extensions['sni_hostname'] == 'example.com' for r in requests)
    assert all('cookie' not in r.headers and r.headers['connection'] == 'close' for r in requests)


@pytest.mark.asyncio
async def test_bad_pdf_retains_successfully_fetched_bytes(tmp_path, public_dns):
    raw = b'%PDF- corrupt document'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=raw, headers={'content-type': 'application/pdf'}))) as client:
        result = await sources.capture_source('https://example.com/rules.pdf', root=tmp_path, client=client)
    assert result['status'] == 'unavailable' and result['fetch_status'] == 'captured'
    assert result['extraction_status'] == 'unavailable'
    assert (tmp_path / (result['sha256'] + '.source')).read_bytes() == raw


@pytest.mark.asyncio
async def test_tls_fallback_keeps_public_redirect_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a: [(2, 1, 6, '', ('127.0.0.1' if host == 'localhost' else '8.8.8.8', 443))])
    monkeypatch.setattr(sources, '_windows_curl', lambda: tmp_path / 'curl.exe')
    calls = []
    def native(url, address, executable):
        calls.append((url, address))
        return httpx.Response(302, headers={'location': 'https://localhost/secrets'}, request=httpx.Request('GET', url))
    monkeypatch.setattr(sources, '_native_https', native)
    def fail(request):
        raise httpx.ConnectError('SSL handshake failure')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        receipt = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert receipt['status'] == 'unavailable' and 'Non-public' in receipt['error']
    assert calls == [('https://example.com/', '8.8.8.8')]


@pytest.mark.asyncio
async def test_tls_fallback_captures_with_transport_provenance(tmp_path, public_dns, monkeypatch):
    monkeypatch.setattr(sources, '_windows_curl', lambda: tmp_path / 'curl.exe')
    monkeypatch.setattr(sources, '_native_https', lambda url, address, exe:
                        httpx.Response(200, text='<p>Verified transport, not verified claims</p>', headers={'content-type': 'text/html'}, request=httpx.Request('GET', url)))
    def fail(request):
        raise httpx.ConnectError('SSL handshake failure')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        receipt = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert receipt['status'] == 'captured'
    assert receipt['transport_fallbacks'][0]['pinned_address'] == '8.8.8.8'
    assert receipt['text'] == 'Verified transport, not verified claims'
    assert (tmp_path / (receipt['sha256'] + '.source')).exists()


@pytest.mark.asyncio
async def test_non_tls_connect_failure_does_not_launch_native(tmp_path, public_dns, monkeypatch):
    monkeypatch.setattr(sources, '_windows_curl', lambda: tmp_path / 'curl.exe')
    monkeypatch.setattr(sources, '_native_https', lambda *args: pytest.fail('unexpected fallback'))
    def fail(request):
        raise httpx.ConnectError('Connection refused')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        receipt = await sources.capture_source('https://example.com/', root=tmp_path, client=client)
    assert receipt['status'] == 'unavailable'
    assert 'transport_fallbacks' not in receipt


def test_native_curl_is_pinned_verified_bounded_and_hidden(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    def run(command, **kwargs):
        assert command[1] == '--disable'
        assert '--globoff' in command
        assert command[command.index('--resolve') + 1] == 'example.com:443:8.8.8.8'
        assert command[command.index('--noproxy') + 1] == '*'
        assert command[command.index('--proto') + 1] == '=https'
        assert command[command.index('--max-filesize') + 1] == str(sources.MAX_BYTES)
        assert command[command.index('--max-redirs') + 1] == '0'
        assert not set(command).intersection({'-k', '--insecure', '-L', '--location', '--netrc', '--cookie'})
        assert kwargs['timeout'] == 30 and not kwargs.get('shell')
        assert kwargs['creationflags'] == getattr(sources.subprocess, 'CREATE_NO_WINDOW', 0)
        Path(command[command.index('--output') + 1]).write_bytes(b'hello')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'response_code': 200, 'content_type': 'text/plain'}).encode(), stderr=b'')
    monkeypatch.setattr(sources.subprocess, 'run', run)
    response = sources._native_https('https://example.com/', '8.8.8.8', tmp_path / 'curl.exe')
    assert response.content == b'hello' and response.status_code == 200


def test_native_refuses_oversize_even_if_curl_does_not(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.setattr(sources, 'MAX_BYTES', 5)
    def run(command, **kwargs):
        Path(command[command.index('--output') + 1]).write_bytes(b'123456')
        return SimpleNamespace(returncode=0, stdout=b'{"response_code":200}', stderr=b'')
    monkeypatch.setattr(sources.subprocess, 'run', run)
    with pytest.raises(ValueError, match='byte limit'):
        sources._native_https('https://example.com/', '8.8.8.8', tmp_path / 'curl.exe')


def test_native_rejects_private_destination_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(sources.subprocess, 'run', lambda *a, **k: pytest.fail('unsafe launch'))
    with pytest.raises(ValueError, match='public HTTPS'):
        sources._native_https('https://example.com/', '127.0.0.1', tmp_path / 'curl.exe')


def test_native_failure_is_not_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(sources.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=60, stdout=b'', stderr=b'certificate verification failed'))
    with pytest.raises(ValueError, match='certificate verification failed'):
        sources._native_https('https://example.com/', '8.8.8.8', tmp_path / 'curl.exe')


@pytest.mark.parametrize('version,valid', [(b'curl 8.21.0 (Windows) Schannel', True),
                                        (b'curl 8.20.0 Schannel', True),
                                        (b'curl 8.19.0 Schannel', False),
                                        (b'curl 8.21.0 OpenSSL', False),
                                        (b'not a version', False)])
def test_native_requires_bounded_decompression_version(version, valid):
    assert sources._bounded_native_curl(version) is valid


def test_selected_pdf_pages_preserve_original_numbers(monkeypatch):
    import pypdf
    class Page:
        def __init__(self, content):
            self.content = content
        def extract_text(self):
            return self.content
    monkeypatch.setattr(pypdf, 'PdfReader', lambda stream: type('Reader', (), {'pages': [Page('one'), Page('two'), Page('three')]})())
    assert sources.extract_text(b'%PDF- test', 'application/pdf', pdf_pages=[3, 1]) == '[PDF page 3]\nthree\n[PDF page 1]\none'
    with pytest.raises(ValueError, match='does not exist'):
        sources.extract_text(b'%PDF- test', 'application/pdf', pdf_pages=[4])


@pytest.mark.parametrize('pages', [[], [0], [-1], [True], ['1'], [1, 1], list(range(1, 22))])
def test_invalid_page_selection_is_rejected(pages):
    with pytest.raises(ValueError, match='page numbers'):
        sources.validate_pdf_pages(pages)


def test_non_pdf_cannot_claim_page_selection():
    with pytest.raises(ValueError, match='non-PDF'):
        sources.extract_text(b'<p>HTML</p>', 'text/html', pdf_pages=[1])


@pytest.mark.asyncio
async def test_selected_capture_saves_complete_raw_source_and_scope(tmp_path, public_dns, monkeypatch):
    import hashlib
    raw = b'%PDF- original complete bytes'
    def extract(data, content_type, *, pdf_pages):
        assert data == raw and pdf_pages == [2]
        return '[PDF page 2]\nSelected evidence'
    monkeypatch.setattr(sources, 'extract_text', extract)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=raw))) as client:
        receipt = await sources.capture_source('https://example.com/law', root=tmp_path, client=client, pdf_pages=[2])
    assert receipt['scope'] == 'selected_pdf_pages' and receipt['pdf_pages'] == [2]
    assert receipt['sha256'] == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / (receipt['sha256'] + '.source')).read_bytes() == raw
    assert not receipt['truncated']  # Complete SELECTED pages, not whole source.


@pytest.mark.asyncio
async def test_prepare_sources_routes_page_selection(tmp_path, monkeypatch):
    calls = []
    async def capture(url, *, root, client, pdf_pages):
        calls.append((url, pdf_pages))
        return {'url': url, 'text': 'selected', 'pdf_pages': pdf_pages}
    monkeypatch.setattr(sources, 'capture_source', capture)
    result = await sources.prepare_sources({'frontmatter': 'sources:\n  - url: https://example.com/law\n    pdf_pages: [2, 3]\n'}, root=tmp_path)
    assert calls == [('https://example.com/law', [2, 3])]
    assert result['source_packets'][0]['pdf_pages'] == [2, 3]
