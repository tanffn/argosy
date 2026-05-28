"""Identify the URL returning 404 on /argonaut."""
from playwright.sync_api import sync_playwright

failed = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.on("response", lambda r: failed.append((r.url, r.status))
            if r.status >= 400 else None)
    page.goto("http://localhost:1337/argonaut", wait_until="networkidle", timeout=15_000)
    page.wait_for_timeout(2000)
    ctx.close()
    browser.close()

print("404+ responses on /argonaut:")
for url, status in failed:
    print(f"  {status} {url}")
