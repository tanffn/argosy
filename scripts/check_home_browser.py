"""Read-only, headless check of Home's real browser-to-API connection."""
import json
import argparse
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:1337")
    parser.add_argument("--exercise-retry", action="store_true", help="Inject one browser-local 503, then retry against the real API (does not affect server state).")
    args = parser.parse_args()
    url = args.url
    with sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        page = browser.new_page()
        failures = []
        responses = []
        errors = []
        page.on("requestfailed", lambda request: failures.append({"url": request.url, "error": request.failure}))
        page.on("response", lambda response: responses.append({"url": response.url, "status": response.status}) if "/api/inbox" in response.url or "/api/home/greeting" in response.url else None)
        page.on("pageerror", lambda error: errors.append(str(error)[:300]))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        panel = page.locator('[data-slot="home-actions"]')
        panel.wait_for(timeout=30000)
        page.wait_for_function("""() => {
          const panel = document.querySelector('[data-slot="home-actions"]');
          return panel && !panel.textContent.includes('Checking your next actions');
        }""", timeout=30000)
        body = panel.inner_text()
        failed = "could not be refreshed" in body
        recovered = None
        if args.exercise_retry and not failed:
            # Deliberate local fault injection tests failure presentation; the
            # initial load and retry both still exercise the actual server.
            page.route("**/api/inbox?*", lambda route: route.fulfill(status=503, body="Temporary test outage"))
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            panel.get_by_text("The action list could not be refreshed.", exact=False).wait_for(timeout=30000)
            stale_list_cleared = panel.locator('ol[aria-label="Prioritized next actions"]').count() == 0
            page.unroute("**/api/inbox?*")
            panel.get_by_role("button", name="Retry now").click()
            page.wait_for_function("""() => !document.querySelector('[data-slot="home-actions"]').textContent.includes('could not be refreshed')""", timeout=30000)
            recovered = stale_list_cleared and "could not be refreshed" not in panel.inner_text()
        page.wait_for_function("""() => document.querySelector('[data-slot="fm-greeting"]')?.textContent.includes('Portfolio') || document.body.textContent.includes('Plan alignment')""", timeout=30000)
        healthy_reads = all(any(part in response["url"] and response["status"] == 200 for response in responses) for part in ("/api/inbox", "/api/home/greeting"))
        print(json.dumps({"url": url, "action_list_failed": failed, "has_action_list": panel.locator('ol[aria-label="Prioritized next actions"]').count() > 0, "both_reads_ok": healthy_reads, "injected_failure_recovered": recovered, "issues": panel.get_by_role('alert').all_text_contents(), "responses": responses, "request_failures": failures[:12], "page_errors": errors[:5]}, ensure_ascii=True))
        browser.close()
        return 1 if failed or errors or not healthy_reads or recovered is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
