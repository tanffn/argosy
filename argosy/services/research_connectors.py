"""Bounded public RSS/Atom, SEC 13D and 13F-change connectors."""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import ipaddress
import json
import socket
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx


def validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTP(S) source URL without credentials")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Only standard web ports are supported")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Private network sources are not supported")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Cannot resolve the source hostname") from exc
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Private network sources are not supported")
    return url


def fetch_public(url: str, *, max_bytes=4_000_000) -> tuple[bytes, str]:
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(5):
            validate_public_url(url)
            host = urlsplit(url).hostname or ""
            headers = {"User-Agent": "Argosy-Research/1.0", "Accept": "application/rss+xml,application/atom+xml,text/html,*/*"}
            if host == "sec.gov" or host.endswith(".sec.gov"):
                from argosy.adapters.data.sec_13f_adapter import _user_agent
                headers["User-Agent"] = _user_agent()
            with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise ValueError("Source exceeds the bounded download size")
                return bytes(chunks), url
    raise ValueError("Too many source redirects")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr", "h1", "h2", "br"}:
            self.parts.append("\n")
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(html: str) -> str:
    parser = PlainText()
    parser.feed(html)
    return "\n".join(line.strip() for line in " ".join(parser.parts).splitlines() if line.strip())


def parse_date(text):
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(text)
        except (ValueError, TypeError):
            return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def parse_feed(data: bytes, base_url: str) -> list[dict]:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Feed entity declarations are not supported")
    root = ET.fromstring(data)
    entries = root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry")
    if root.tag not in {"rss", "{http://www.w3.org/2005/Atom}feed"} and not entries:
        raise ValueError("URL did not return an RSS or Atom feed")
    result = []
    for entry in entries[:200]:
        fields = {}
        for child in entry:
            name = child.tag.split("}")[-1]
            value = "".join(child.itertext()).strip()
            if name == "link":
                if child.attrib.get("rel", "alternate") != "alternate":
                    continue
                value = child.attrib.get("href", value)
            fields[name] = value
        link = urljoin(base_url, fields.get("link", ""))
        body = fields.get("encoded") or fields.get("content") or fields.get("description") or fields.get("summary") or ""
        result.append({"external_id": fields.get("guid") or fields.get("id") or link,
                       "title": fields.get("title", "Untitled research"), "url": link,
                       "body": plain_text(body), "author": fields.get("creator") or fields.get("author", ""),
                       "published_at": parse_date(fields.get("published") or fields.get("pubDate") or fields.get("updated"))})
    return result


def holdings_changes(current, previous):
    """Compare share counts by CUSIP and security class; exclude option positions."""
    def aggregate(rows):
        out = {}
        for row in rows:
            if row.get("put_call"):
                continue
            key = (str(row.get("cusip") or ""), str(row.get("title_of_class") or ""))
            if not key[0]:
                continue
            entry = out.setdefault(key, {"name": row.get("name"), "shares": 0})
            entry["shares"] += float(row.get("shares") or 0)
        return out
    before, after = aggregate(previous), aggregate(current)
    changes = []
    for key, row in after.items():
        old = before.get(key, {}).get("shares", 0)
        if row["shares"] > old and (not old or row["shares"] / old >= 1.2):
            changes.append({"cusip": key[0], "name": row["name"], "shares_before": old,
                            "shares_after": row["shares"], "kind": "new" if old == 0 else "increased",
                            "limitation": "Delayed disclosure; verify splits, ticker identity and the current thesis. No purchase price inferred."})
    return changes


async def collect_source(source) -> list[dict]:
    import asyncio
    if source.kind == "sec13f":
        from argosy.adapters.data.sec_13f_adapter import Sec13FAdapter
        adapter = Sec13FAdapter()
        filings = await adapter.get_filer_history(source.reference, quarters=2)
        if len(filings) < 2:
            raise ValueError("Two 13F filings are required for a changes comparison")
        accession = lambda row: row.get("accession_number") or row.get("accession")
        current = await adapter.get_filing_holdings(accession(filings[0]))
        previous = await adapter.get_filing_holdings(accession(filings[1]))
        if not current or not previous:
            raise ValueError("13F holdings unavailable; comparison deferred")
        changes = holdings_changes(current, previous)
        return [{"external_id": "13f:" + accession(filings[0]), "title": source.name + " — new and increased 13F positions",
                 "url": filings[0].get("document_url") or filings[0].get("url") or filings[0].get("filing_url") or "https://www.sec.gov/edgar/browse/?CIK=" + source.reference,
                 "published_at": parse_date(filings[0].get("filed_at") or filings[0].get("filing_date")),
                 "body": json.dumps({"manager": source.name, "current_filing": filings[0], "previous_filing": filings[1], "changes": changes}, default=str),
                 "author": source.name}] if changes else []
    url = source.reference
    if source.kind == "sec13d" and url == "all":
        url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SCHEDULE%2013D&owner=include&count=100&output=atom"
    if source.kind not in {"rss", "sec13d"}:
        return []
    data, final_url = await asyncio.to_thread(fetch_public, url)
    return parse_feed(data, final_url)


def expand_document(item) -> str:
    """Read the source document at analysis time, not every daily poll."""
    url = item.url
    if "sec.gov/Archives/" in url and "-index.htm" in url:
        url = url.split("-index.htm")[0] + ".txt"
    data, final_url = fetch_public(url)
    if data.startswith(b"%PDF"):
        import io
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages[:150])
    else:
        text = plain_text(data.decode("utf-8", errors="replace"))
    if len(text.strip()) < 200:
        raise ValueError("Source document is too short; keep the item pending for retry")
    return f"Publisher: {item.author}\nTitle: {item.title}\nOriginal URL: {final_url}\nPublished: {item.published_at or 'unknown'}\n\n{text[:150000]}"
