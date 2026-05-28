"""Open the generated guide in headless Chromium and screenshot it.

Sanity-check that the HTML renders cleanly + the screenshots reference paths
resolve. Also captures a thumbnail for sharing.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

GUIDE = Path("D:/Projects/financial-advisor/ui/public/user-guide/index.html").resolve()
OUT = Path("D:/Projects/financial-advisor/ui/public/user-guide/guide-thumbnail.png")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()

    errors = []
    failed = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("requestfailed", lambda r: failed.append((r.url, r.failure)))

    file_url = "file:///" + str(GUIDE).replace("\\", "/")
    print("opening:", file_url)
    page.goto(file_url, wait_until="networkidle", timeout=15_000)
    page.wait_for_timeout(1500)

    # Hero shot
    page.screenshot(path=str(OUT), full_page=False)
    # Mid-document
    page.evaluate("window.scrollTo(0, 2000)")
    page.wait_for_timeout(500)
    page.screenshot(
        path=str(OUT.with_name("guide-mid.png")),
        full_page=False,
    )

    print(f"\nconsole errors: {len(errors)}")
    for e in errors: print(f"  - {e}")
    print(f"failed requests: {len(failed)}")
    for url, reason in failed: print(f"  - {url}: {reason}")

    ctx.close()
    browser.close()

print(f"\nthumbnail: {OUT}")
