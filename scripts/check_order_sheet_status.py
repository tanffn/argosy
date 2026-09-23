"""Read-only live Inbox/API check; does not prove trading or investment results."""
from __future__ import annotations

import argparse
import json
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--ui", default="http://127.0.0.1:1337")
    args = parser.parse_args()
    with urlopen(args.api + "/api/e2e-proof", timeout=30) as response:
        proof = json.load(response)
    with sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)[:300]))
            page.goto(args.ui + "/inbox", wait_until="domcontentloaded", timeout=45000)
            card = page.get_by_test_id("argosy-e2e-run")
            card.wait_for(timeout=45000)
            card.get_by_text(proof["headline"], exact=True).wait_for(timeout=30000)
            approval = card.get_by_role("button", name="Approve plan", exact=True).count()
            text = card.inner_text()
            expected_approval = proof["stage"] == "ready_to_accept"
            findings = []
            if bool(approval) != expected_approval:
                findings.append("Approval control does not match the API stage")
            if proof["stage"] == "no_action":
                if proof["lines"]:
                    findings.append("No-action stage contains executable lines")
                checks = [c for c in proof["checks"] if c["key"] in {"materialized", "telemetry", "fills"}]
                if len(checks) != 3 or any(c.get("applicable", True) or c["passed"] for c in checks):
                    findings.append("No-action execution checks claim completion or failure")
                if f"0 actions · {len(proof['no_action'])} unchanged positions" not in text:
                    findings.append("Unchanged-position count is not rendered")
                # Details are collapsed; inspect DOM text without clicking controls.
                if card.get_by_text("N/A", exact=True).count() != 3:
                    findings.append("Inapplicable checks not rendered as N/A")
            print(json.dumps({
                "stage": proof["stage"], "headline": proof["headline"],
                "action_lines": len(proof["lines"]), "unchanged_positions": len(proof["no_action"]),
                "research_tasks_in_artifact": len(proof.get("pending_research", [])),
                "approval_control_visible": bool(approval), "findings": findings,
                "browser_errors": errors, "execution_proven": False,
            }))
            return int(bool(findings or errors))
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
