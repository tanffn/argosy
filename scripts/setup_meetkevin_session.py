"""Interactive, isolated Meet Kevin login; store only app auth in the OS keychain.

No Chrome profile import, password inspection, report ingestion or scheduled polling.
The browser makes its normal application requests; this helper issues no API calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

APP_URL = "https://app.meetkevin.com/data/alpha"
API_URL = "https://app-api.meet-kevin.com"
SECRET_KEY = "meetkevin_research_session"


def capture_session(page, context) -> dict | None:
    """Capture only the app's session, never identity-provider or Chrome data."""
    from urllib.parse import urlsplit

    if urlsplit(page.url).hostname != "app.meetkevin.com":
        return None
    token = page.evaluate("sessionStorage.getItem('access_token')")
    if not isinstance(token, str) or not token:
        return None
    return {
        "access_token": token,
        "cookies": context.cookies([APP_URL, API_URL]),
        "captured_at": datetime.now(UTC).isoformat(),
        "origin": "https://app.meetkevin.com",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    if not 30 <= args.timeout_seconds <= 900:
        parser.error("timeout-seconds must be between 30 and 900")
    status_path = Path.home() / ".argosy" / "meetkevin_setup_status.json"

    def status(state: str, **details):
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = status_path.with_suffix(".new")
        temporary.write_text(json.dumps({"state": state, "updated_at": datetime.now(UTC).isoformat(), **details}), encoding="utf-8")
        temporary.replace(status_path)

    from playwright.sync_api import Error, sync_playwright

    from argosy.secrets import set_secret

    status("opening_browser")
    try:
        with sync_playwright() as driver:
            # Fresh ephemeral profile, with no access to the user's normal Chrome.
            browser = driver.chromium.launch(headless=False, args=["--start-maximized"])
            try:
                context = browser.new_context(no_viewport=True)
                page = context.new_page()
                page.goto(APP_URL, wait_until="domcontentloaded", timeout=45000)
                page.bring_to_front()
                status("waiting_for_login", instructions="Sign in directly in the separate browser. No password or cookie export needed.")
                deadline = time.monotonic() + args.timeout_seconds
                while time.monotonic() < deadline and not page.is_closed():
                    try:
                        session = capture_session(page, context)
                    except Error:
                        # Navigation can destroy an execution context mid-check.
                        session = None
                    if session:
                        set_secret(SECRET_KEY, json.dumps(session))
                        status("session_saved", report_access_verified=False,
                               note="App session saved in OS credential manager; browser closes. No report fetched by this helper, no recurring job enabled.")
                        return 0
                    page.wait_for_timeout(1000)  # Local session check; no API polling.
                status("cancelled" if page.is_closed() else "timed_out")
                return 1
            finally:
                browser.close()
    except Exception as exc:
        # No traceback/locals or exception text: auth data must never enter logs.
        status("failed", error_type=type(exc).__name__, note="No recurring job enabled. Check local login setup.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
