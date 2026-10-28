from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import httpx
import pytest

from argosy.services.jobs.recovery_policy import recovery_window
from argosy.services.jobs.transport_failure import (
    classify_transport_failure, retry_after_seconds, retry_data_read,
)


@pytest.mark.parametrize("status,retryable", [(408, True), (429, True), (500, True),
    (502, True), (503, True), (504, True), (400, False), (401, False),
    (403, False), (404, False), (501, False)])
def test_http_classification_and_bounded_attempts(status, retryable):
    calls, sleeps = [], []
    def read():
        calls.append(1)
        raise HTTPError("https://source.invalid", status, "unavailable", {}, None)
    with pytest.raises(HTTPError):
        retry_data_read(read, sleep=sleeps.append)
    assert len(calls) == (3 if retryable else 1)
    assert sleeps == ([1, 3] if retryable else [])


def test_retry_after_http_date_and_long_delays():
    now = datetime.now(UTC).replace(microsecond=0)
    assert retry_after_seconds(format_datetime(now + timedelta(seconds=20)), now=now) == 20
    for value in ("garbage", "nan", "inf"):
        assert retry_after_seconds(value) is None
    calls, sleeps = [], []
    def read():
        calls.append(1)
        raise HTTPError("https://source.invalid", 429, "wait", {"Retry-After": "3600"}, None)
    with pytest.raises(HTTPError):
        retry_data_read(read, sleep=sleeps.append)
    assert len(calls) == 1
    assert sleeps == []


def test_wrapped_httpx_timeout_and_permanent_failure():
    try:
        try:
            raise httpx.ReadTimeout("timed out")
        except httpx.ReadTimeout as exc:
            raise RuntimeError("adapter failed") from exc
    except RuntimeError as exc:
        assert classify_transport_failure(exc).retryable
    assert not classify_transport_failure(ValueError("HTTP 503 mentioned in bad data")).retryable


def test_real_http_read_recovers_from_503_without_duplicate_success():
    """Real sockets + urllib exception/header semantics, not a mocked fetch seam."""
    calls, sleeps = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(1)
            self.send_response(503 if len(calls) < 3 else 200)
            self.send_header("Retry-After", "2")
            self.end_headers()
            self.wfile.write(b'{"results": []}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def read():
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
            return json.load(response)
    try:
        assert retry_data_read(read, sleep=sleeps.append) == {"results": []}
        assert len(calls) == 3
        assert sleeps == [2, 3]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_durable_policy_later_probes_and_mixed_failures():
    now = datetime.now(UTC)
    failure = {"code": "upstream_unavailable", "retryable": True, "retry_after_seconds": 9000}
    row = SimpleNamespace(started_at=now, finished_at=now,
        output_summary=json.dumps({"streams": {"source": {"status": "error", "failure": failure}}}))
    policy = recovery_window(row, 3)
    assert policy["eligible"]
    assert policy["max_attempts"] == 6
    assert datetime.fromisoformat(policy["next_retry_at"]) == now + timedelta(seconds=9000)
    assert not recovery_window(row, 6)["eligible"]
    row.output_summary = json.dumps({"streams": {
        "a": {"status": "error", "failure": failure},
        "b": {"status": "error", "error": "parser bug"},
    }})
    assert not recovery_window(row, 3)["eligible"]
    row.output_summary = json.dumps({"failure": {"code": "authentication", "retryable": False}})
    assert not recovery_window(row, 1)["eligible"]


@pytest.mark.asyncio
async def test_async_retry_uses_same_headers_and_no_auth_retry():
    from argosy.services.jobs.retry import retry_transient
    calls, sleeps = [], []
    async def sleep(delay):
        sleeps.append(delay)
    async def read():
        calls.append(1)
        if len(calls) == 1:
            raise HTTPError("https://source.invalid", 503, "wait", {"Retry-After": "4"}, None)
        return "ok"
    assert await retry_transient(read, sleep=sleep, jitter_min_s=1, jitter_max_s=1) == "ok"
    assert sleeps == [4]
    async def denied():
        raise HTTPError("https://source.invalid", 403, "denied", {}, None)
    with pytest.raises(HTTPError):
        await retry_transient(denied, sleep=sleep)
    assert sleeps == [4]
