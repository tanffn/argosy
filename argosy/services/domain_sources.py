"""Capture cited public source bytes for research; never certify their claims.

Only called for trusted, local KB frontmatter. No user records, cookies, credentials,
or model-generated URLs are sent. Redirects are checked just like initial URLs.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
import yaml

MAX_BYTES = 8_000_000
MAX_TEXT = 200_000
MAX_PROMPT_TEXT = 500_000
MAX_SOURCES = 12
MAX_SELECTED_PDF_PAGES = 20


def validate_pdf_pages(pages):
    if pages is not None and (not isinstance(pages, list) or not pages
            or len(pages) > MAX_SELECTED_PDF_PAGES
            or any(type(page) is not int or page < 1 for page in pages)
            or len(set(pages)) != len(pages)):
        raise ValueError('PDF selection requires 1-20 distinct positive one-based page numbers')
    return pages


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def extract_text(raw: bytes, content_type: str, *, pdf_pages=None) -> str:
    validate_pdf_pages(pdf_pages)
    if raw.startswith(b'%PDF-'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        indices = pdf_pages if pdf_pages is not None else range(1, len(reader.pages) + 1)
        if any(number > len(reader.pages) for number in indices):
            raise ValueError('Selected PDF page does not exist')
        pages = [(number, reader.pages[number - 1].extract_text() or '') for number in indices]
        if not any(text.strip() for _, text in pages):
            return ''
        return '\n'.join(f'[PDF page {number}]\n{text}' for number, text in pages)
    if pdf_pages is not None:
        raise ValueError('PDF page selection cannot be applied to a non-PDF source')
    if 'html' in content_type:
        parser = _Text()
        parser.feed(raw.decode('utf-8', errors='replace'))
        return '\n'.join(parser.parts)
    if content_type.startswith('text/') or content_type.split(';')[0] == 'application/json':
        return raw.decode('utf-8', errors='replace')
    raise ValueError('Source is not supported HTML, text, or PDF')


async def _public_https(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Source requires public HTTPS without credentials or alternate ports')
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, 0, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Non-public source address refused')
    return next((row[4][0] for row in addresses if row[0] == socket.AF_INET), addresses[0][4][0])


def _bounded_native_curl(version_output: bytes) -> bool:
    match = re.match(rb'curl (\d+)\.(\d+)\.(\d+)\b', version_output)
    return bool(match and tuple(map(int, match.groups())) >= (8, 20, 0)
                and b'Schannel' in version_output)


@lru_cache(maxsize=1)
def _windows_curl() -> Path | None:
    # Do not launch a similarly named executable from the working directory/PATH.
    if os.name != 'nt' or not os.environ.get('SystemRoot'):
        return None
    executable = Path(os.environ['SystemRoot']) / 'System32' / 'curl.exe'
    if not executable.is_file():
        return None
    version = subprocess.run([str(executable), '--disable', '--version'], capture_output=True,
                             timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    # 8.20 added the decoded-output limit for --compressed; older versions
    # cannot enforce this fallback's temporary-file budget safely.
    return executable if version.returncode == 0 and _bounded_native_curl(version.stdout) else None


def _is_sec_source(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return host == 'sec.gov' or host.endswith('.sec.gov')


def _source_user_agent(url: str) -> str:
    if _is_sec_source(url):
        from argosy.adapters.data.sec_13f_adapter import _default_headers
        return _default_headers()['User-Agent']
    return 'Argosy knowledge-source verification'


async def _pace_sec_source(url: str) -> None:
    if _is_sec_source(url):
        from argosy.adapters.data.sec_rate_limit import (
            MIN_SEC_REQUEST_INTERVAL_SECONDS, wait_for_sec_request_slot,
        )
        await wait_for_sec_request_slot(clock=time.monotonic, sleep=asyncio.sleep,
                                        interval_seconds=MIN_SEC_REQUEST_INTERVAL_SECONDS)


def _native_https(url: str, address: str, executable: Path) -> httpx.Response:
    """Windows TLS fallback with the same pinned destination and no credentials.

    Schannel can reach legacy government servers that reject OpenSSL handshakes.
    Certificate/hostname verification stays enabled; redirects return to the
    caller for a fresh public-address check. No shell or visible console.
    """
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or not ipaddress.ip_address(address).is_global):
        raise ValueError('Native source requires validated public HTTPS')
    pinned = f'[{address}]' if ':' in address else address
    with tempfile.TemporaryDirectory(prefix='argosy-public-source-') as temporary:
        body = Path(temporary) / 'body'
        command = [str(executable), '--disable', '--globoff', '--silent', '--show-error',
                   '--noproxy', '*', '--proto', '=https', '--max-redirs', '0',
                   '--connect-timeout', '15', '--max-time', '25',
                   '--max-filesize', str(MAX_BYTES), '--compressed',
                   '--user-agent', _source_user_agent(url),
                   '--resolve', f'{parsed.hostname}:443:{pinned}',
                   '--output', str(body), '--write-out', '%{json}', '--url', url]
        result = subprocess.run(command, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError(f'Native HTTPS fetch failed ({result.returncode}): '
                             f'{result.stderr.decode("utf-8", errors="replace")[:500]}')
        metadata = json.loads(result.stdout)
        with body.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Source exceeds capture byte limit')
        headers = {'content-type': metadata.get('content_type') or ''}
        if metadata.get('redirect_url'):
            headers['location'] = metadata['redirect_url']
        return httpx.Response(int(metadata['response_code']), content=raw,
                              headers=headers, request=httpx.Request('GET', url))


async def capture_source(url: str, *, root: Path, client: httpx.AsyncClient, pdf_pages=None) -> dict:
    receipt = {'url': url, 'captured_at': datetime.now(UTC).isoformat(),
               'fetch_id': str(uuid4())}
    try:
        if pdf_pages is not None:
            receipt.update(pdf_pages=validate_pdf_pages(pdf_pages), scope='selected_pdf_pages')
        current = url
        for _ in range(5):
            address = await asyncio.wait_for(_public_https(current), timeout=25)
            parsed = urlsplit(current)
            await _pace_sec_source(current)
            # Pin the checked address. Preserve TLS verification/SNI and HTTP Host
            # for the original hostname. A directly constructed Request does not
            # inherit the client's cookie jar. Do not pool by the IP across hosts.
            request = httpx.Request('GET', httpx.URL(current).copy_with(host=address),
                headers={'Host': parsed.hostname, 'Connection': 'close',
                         'Accept': '*/*', 'Accept-Encoding': 'gzip, deflate',
                         'User-Agent': _source_user_agent(current)},
                extensions={'sni_hostname': parsed.hostname,
                            'timeout': {'connect': 25, 'read': 25, 'write': 25, 'pool': 25}})
            try:
                response = await client.send(request, stream=True)
            except httpx.ConnectError as exc:
                executable = _windows_curl()
                if executable is None or not any(term in str(exc).lower() for term in ('ssl', 'tls', 'handshake')):
                    raise
                receipt.setdefault('transport_fallbacks', []).append({
                    'url': current, 'from': 'httpx', 'to': 'windows_native_curl',
                    'reason': str(exc)[:500], 'pinned_address': address,
                })
                await _pace_sec_source(current)
                response = await asyncio.to_thread(_native_https, current, address, executable)
            try:
                if response.is_redirect:
                    current = urljoin(current, response.headers['location'])
                    continue
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ValueError('Source exceeds capture byte limit')
                content_type = response.headers.get('content-type', '')
                break
            finally:
                await response.aclose()
        else:
            raise ValueError('Source exceeded redirect limit')
        raw = bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        blob = root / (digest + '.source')
        try:
            with blob.open('xb') as output:
                output.write(raw)
        except FileExistsError:
            if hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
                raise ValueError('Existing source blob hash mismatch') from None
        receipt.update(fetch_status='captured', final_url=current, sha256=digest,
                       bytes=len(raw), content_type=content_type, extraction_status='pending')
        # Save URL/time/hash BEFORE a potentially failing or cancelled parser.
        receipt['fetch_receipt_id'] = _save_receipt(receipt, root)
        selection = {'pdf_pages': pdf_pages} if pdf_pages is not None else {}
        text = await asyncio.to_thread(extract_text, raw, content_type, **selection)
        if not text.strip():
            raise ValueError('Source has no extractable text; native PDF/OCR still needed')
        receipt.update(status='captured', extraction_status='extracted', text_chars=len(text),
                       truncated=len(text) > MAX_TEXT, text=text[:MAX_TEXT])
    except Exception as exc:
        receipt.update(status='unavailable', error=f'{type(exc).__name__}: {exc}')
        if receipt.get('fetch_status') == 'captured':
            receipt['extraction_status'] = 'unavailable'
    return {**receipt, 'receipt_id': _save_receipt(receipt, root)}


def _save_receipt(receipt: dict, root: Path) -> str:
    """Immutable retrieval/extraction-stage receipts, with per-fetch UUID identity."""
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    receipt_id = hashlib.sha256(serialized.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    target = root / (receipt_id + '.json')
    try:
        with target.open('x', encoding='utf-8') as output:
            output.write(serialized)
    except FileExistsError:
        if target.read_text(encoding='utf-8') != serialized:
            raise ValueError('Existing source receipt hash mismatch') from None
    return receipt_id


async def prepare_sources(item: dict, *, root: Path) -> dict:
    metadata = yaml.safe_load(item.get('frontmatter') or '') or {}
    sources = metadata.get('sources', []) if isinstance(metadata, dict) else []
    urls = list(dict.fromkeys(source['url'] for source in sources
                             if isinstance(source, dict) and isinstance(source.get('url'), str)
                             and source['url'].startswith(('https://', 'http://'))))
    selections = {source['url']: source['pdf_pages'] for source in sources
                  if isinstance(source, dict) and isinstance(source.get('url'), str)
                  and 'pdf_pages' in source}
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False,
                                headers={'User-Agent': 'Argosy knowledge-source verification'}) as client:
        receipts = []
        for offset in range(0, min(len(urls), MAX_SOURCES), 3):
            receipts.extend(await asyncio.gather(*(capture_source(url, root=root, client=client,
                                                                   pdf_pages=selections.get(url))
                                                  for url in urls[offset:offset + 3])))
    remaining = MAX_PROMPT_TEXT
    packets = []
    for receipt in receipts:
        packet = dict(receipt)
        text = packet.get('text', '')
        packet['text'] = text[:remaining]
        packet['prompt_truncated'] = len(text) > remaining
        remaining -= len(packet['text'])
        packets.append(packet)
    return {**item, 'source_packets': packets,
            'source_capture_omitted': urls[MAX_SOURCES:]}
