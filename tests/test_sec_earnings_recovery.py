"""Exercise SEC earnings through its actual request/receipt recovery seams."""
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

from argosy.services.sec_earnings_monitor import SecEarningsFetcher
from argosy.services.jobs.recovery_policy import ProviderCooldownError, recovery_window


@pytest.mark.real_seam
def test_sec_fetcher_retries_real_http_and_both_response_formats():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            failed = calls.count(self.path) == 1
            self.send_response(503 if failed else 200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(timeout=3) as client:
            fetcher = SecEarningsFetcher(client=client)
            base = f"http://127.0.0.1:{server.server_port}"
            assert fetcher._get_json(base + "/json") == {"ok": True}
            assert json.loads(fetcher._get_text(base + "/text")) == {"ok": True}
        assert calls == ["/json", "/json", "/text", "/text"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize("status", [403, 429])
def test_access_failure_and_long_cooldown_stop_following_tickers(status):
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(status, headers={"Retry-After": "3600"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetcher = SecEarningsFetcher(client=client)
        with pytest.raises(httpx.HTTPStatusError):
            fetcher._get_json("https://data.sec.gov/first")
        with pytest.raises(httpx.HTTPStatusError if status == 403 else ProviderCooldownError):
            fetcher._get_text("https://data.sec.gov/second")
    assert len(calls) == 1


def test_malformed_json_is_not_transport_retried():
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text="not-json")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            SecEarningsFetcher(client=client)._get_json("https://data.sec.gov/test")
    assert len(calls) == 1


def test_per_ticker_failure_metadata_drives_bounded_job_recovery():
    def row(errors):
        return SimpleNamespace(output_summary=json.dumps({"errors": errors}),
                               finished_at=datetime.now(UTC))
    transient = {"ticker": "AMD", "failure": {"code": "timeout", "retryable": True}}
    assert recovery_window(row([transient]), 3)["max_attempts"] == 6
    assert recovery_window(row([transient, {"error": "parse failure"}]), 3)["eligible"] is False
    denied = {"ticker": "AMD", "failure": {"code": "authentication", "retryable": False}}
    assert recovery_window(row([denied]), 1)["eligible"] is False
