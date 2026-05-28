"""Screenshot + bug-audit driver for the /retirement page.

Launches Chromium via Playwright, navigates to the retirement page, waits
for network idle, then:
  1. Takes a full-page screenshot.
  2. Scrolls to each top-level card and takes a section screenshot.
  3. Captures console errors + failed network requests as a bug log.
  4. Exercises the withdrawal-policy radio selector to confirm interactivity.

Outputs land under ``ui/public/user-guide/screenshots/`` (committed location so
they can be referenced from the HTML user guide).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Request, sync_playwright

OUT_DIR = Path("D:/Projects/financial-advisor/ui/public/user-guide/screenshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)
BUG_LOG = Path("D:/Projects/financial-advisor/ui/public/user-guide/bug_log.json")

URL = "http://localhost:1337/retirement"

console_msgs: list[dict] = []
failed_requests: list[dict] = []


def _on_console(msg: ConsoleMessage) -> None:
    if msg.type in ("error", "warning"):
        console_msgs.append({
            "type": msg.type,
            "text": msg.text,
            "location": str(msg.location) if msg.location else "",
        })


def _on_request_failed(req: Request) -> None:
    failed_requests.append({
        "url": req.url,
        "method": req.method,
        "failure": req.failure or "",
    })


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# Cards we expect to find — in display order. Used both to label
# screenshots and to verify nothing is missing from the rendered page.
EXPECTED_CARDS = [
    "Retirement readiness",
    "Safety gates",
    "Bituach Leumi stipend",
    "Mekadem · Clal kupat pensia",
    "Portfolio volatility (σ)",
    "Withdrawal policy",
    "USD/NIS forecast",
    "Glide path",
    "Rebalancing alerts",
    "Expense phases over life",
    "Healthcare cost curve",
    "Tax calculator",
    "Hishtalmut eligibility",
    "Decumulation order",
    "Lump vs annuity at age 67",
    "Real estate + mortgage",
    "Insurance gaps",
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
            device_scale_factor=1,
        )
        page = ctx.new_page()
        page.on("console", _on_console)
        page.on("requestfailed", _on_request_failed)

        print(f"navigating: {URL}")
        page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        # Wait for network idle — the page fans out 10+ API calls
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as e:
            print(f"  networkidle timeout (continuing): {e}")
        # Extra settle time for recharts animations + lazy state
        time.sleep(2)

        # Full-page screenshot
        full_path = OUT_DIR / "00-full-page.png"
        page.screenshot(path=str(full_path), full_page=True)
        print(f"  saved {full_path.name}")

        # Per-card screenshots — locate each CardTitle text + scroll its
        # parent Card into view, then screenshot just that card.
        rendered: list[str] = []
        missing: list[str] = []
        for i, card_title in enumerate(EXPECTED_CARDS, start=1):
            # Recharts has a re-render quirk; settle a touch each iteration
            try:
                # Find the card by its title (case-sensitive partial match)
                title_locator = page.locator(
                    f'[data-slot="card-title"]:has-text("{card_title}")'
                ).first
                if not title_locator.is_visible(timeout=2_000):
                    # Fall back to plain h-text since card-title may be a styled wrapper
                    title_locator = page.locator(
                        f'text="{card_title}"'
                    ).first
                # Walk up to the nearest Card root
                card_locator = title_locator.locator(
                    'xpath=ancestor::*[contains(@class, "rounded") and contains(@class, "border")][1]'
                ).first
                card_locator.scroll_into_view_if_needed(timeout=5_000)
                time.sleep(0.3)
                slug = f"{i:02d}-{slugify(card_title)}.png"
                card_locator.screenshot(path=str(OUT_DIR / slug))
                rendered.append(card_title)
                print(f"  OK{slug}")
            except Exception as e:
                missing.append(card_title)
                print(f"  FAIL{card_title}: {e}")

        # Interactivity smoke: click the VPW radio + confirm hero verdict
        # text changes
        try:
            hero_pre = page.locator(
                'text=/P\\(SOLVENT|ON TRACK|OFF TRACK|WARN|UNCERTAIN/i'
            ).first.inner_text(timeout=3_000)
            print(f"  hero pre: {hero_pre[:80]!r}")
            page.locator('input[type="radio"][value="vpw"]').check(timeout=5_000)
            time.sleep(3)  # MC re-runs server-side
            hero_post = page.locator(
                'text=/P\\(SOLVENT|ON TRACK|OFF TRACK|WARN|UNCERTAIN/i'
            ).first.inner_text(timeout=3_000)
            print(f"  hero post-VPW: {hero_post[:80]!r}")
            page.screenshot(
                path=str(OUT_DIR / "98-after-vpw-toggle.png"), full_page=False,
            )
        except Exception as e:
            print(f"  policy-toggle smoke failed: {e}")

        # Sources panel open
        try:
            page.locator('button:has-text("Sources")').first.click(timeout=5_000)
            time.sleep(1)
            page.locator('button:has-text("Sources")').first.scroll_into_view_if_needed()
            time.sleep(0.5)
            page.screenshot(
                path=str(OUT_DIR / "99-sources-panel-open.png"), full_page=False,
            )
            print("  OK99-sources-panel-open.png")
        except Exception as e:
            print(f"  sources panel smoke failed: {e}")

        ctx.close()
        browser.close()

    BUG_LOG.write_text(json.dumps({
        "url": URL,
        "rendered_cards": rendered,
        "missing_cards": missing,
        "console_errors": [m for m in console_msgs if m["type"] == "error"],
        "console_warnings": [m for m in console_msgs if m["type"] == "warning"],
        "failed_network_requests": failed_requests,
    }, indent=2), encoding="utf-8")
    print(f"\nbug log: {BUG_LOG}")
    print(f"rendered {len(rendered)}/{len(EXPECTED_CARDS)} cards; "
          f"{len(failed_requests)} failed reqs; "
          f"{sum(1 for m in console_msgs if m['type'] == 'error')} console errors")


if __name__ == "__main__":
    main()
