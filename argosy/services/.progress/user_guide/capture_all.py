"""Capture screenshots of every primary page + audit the end-to-end flow.

Walks the entire site (not just /retirement), logs every console/network
issue, and probes for the data-input flow:
  - How do you add a $50K bonus?
  - How do you upload a monthly bank statement?
  - How do you update credit-card expenses?
  - What's the Plan ↔ Retirement relationship?
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Request, sync_playwright

OUT_DIR = Path("D:/Projects/financial-advisor/ui/public/user-guide/site-tour")
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUDIT = Path("D:/Projects/financial-advisor/ui/public/user-guide/full_site_audit.json")

PAGES = [
    ("home", "/"),
    ("advisor", "/advisor"),
    ("portfolio", "/portfolio"),
    ("expenses", "/expenses"),
    ("plan", "/plan"),
    ("retirement", "/retirement"),
    ("decide", "/decide"),
    ("proposals", "/proposals"),
    ("argonaut", "/argonaut"),
    ("agents", "/agents"),
    ("files", "/files"),
    ("audit", "/audit"),
    ("domain-kb", "/domain-kb"),
    ("settings", "/settings"),
    # Detail surfaces — not in nav, reached by click-through. IDs are
    # hard-coded against known-existing rows in the dev DB; if those
    # rows are pruned the screenshots will show the "not found" state
    # which is still informative for the user-guide.
    ("positions", "/positions"),
    ("decisions-detail", "/decisions/32"),
    ("fleet-review-list", "/fleet-review"),
    ("fleet-review-detail", "/fleet-review/12"),
    ("onboarding", "/onboarding"),
]

per_page: dict[str, dict] = {}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900},
        )

        for label, path in PAGES:
            errors: list[dict] = []
            warns: list[dict] = []
            failed: list[dict] = []

            page = ctx.new_page()
            page.on("console", lambda m: (
                errors.append({"text": m.text, "loc": str(m.location)})
                if m.type == "error"
                else warns.append({"text": m.text, "loc": str(m.location)})
                if m.type == "warning"
                else None
            ))
            page.on("requestfailed", lambda r: failed.append({
                "url": r.url, "failure": r.failure or "",
            }))

            url = f"http://localhost:1337{path}"
            print(f"\n[{label}] {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            except Exception as e:
                print(f"  goto failed: {e}")
                per_page[label] = {
                    "path": path, "loaded": False, "error": str(e),
                }
                page.close()
                continue

            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass  # tolerate slow loads
            time.sleep(1.5)

            # Top + full
            try:
                page.screenshot(
                    path=str(OUT_DIR / f"{slugify(label)}-top.png"),
                    full_page=False,
                )
                page.screenshot(
                    path=str(OUT_DIR / f"{slugify(label)}-full.png"),
                    full_page=True,
                )
            except Exception as e:
                print(f"  screenshot failed: {e}")

            # Probe for inputs / upload widgets / forms
            input_count = page.locator("input").count()
            textarea_count = page.locator("textarea").count()
            file_input_count = page.locator('input[type="file"]').count()
            button_count = page.locator("button").count()
            select_count = page.locator("select").count()
            form_count = page.locator("form").count()

            # Scrape the visible button labels — most call-to-action surfaces
            buttons = []
            for i in range(min(button_count, 20)):
                try:
                    t = page.locator("button").nth(i).inner_text(timeout=500).strip()
                    if t and len(t) < 60:
                        buttons.append(t)
                except Exception:
                    continue

            per_page[label] = {
                "path": path,
                "loaded": True,
                "input_count": input_count,
                "textarea_count": textarea_count,
                "file_input_count": file_input_count,
                "button_count": button_count,
                "select_count": select_count,
                "form_count": form_count,
                "buttons_seen": buttons,
                "console_errors": errors,
                "console_warnings": warns[:10],  # cap noise
                "failed_requests": failed,
            }
            print(f"  inputs={input_count} textareas={textarea_count} "
                  f"file_inputs={file_input_count} buttons={button_count} "
                  f"selects={select_count} forms={form_count}")
            print(f"  errors={len(errors)} warns={len(warns)} failed_reqs={len(failed)}")

            page.close()

        ctx.close()
        browser.close()

    AUDIT.write_text(json.dumps(per_page, indent=2), encoding="utf-8")
    print(f"\n\naudit written to: {AUDIT}")


if __name__ == "__main__":
    main()
