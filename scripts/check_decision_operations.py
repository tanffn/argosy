"""Check the RUNNING backend's durable daily-analysis receipts, not mocks.

Run with .venv/Scripts/python.exe scripts/check_decision_operations.py
Exit 0: daily analysis current; 1: incomplete/blocked; 2: cannot check.
This is operational evidence, not certification of investment performance
or of a validated order sheet. Safe to run repeatedly; GETs only, no LLM cost.
"""
from __future__ import annotations

import argparse
import json
from urllib.error import URLError
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        with urlopen(args.url.rstrip("/") + "/api/health/decisions", timeout=15) as response:
            report = json.load(response)
        print(json.dumps(report, indent=2))
        return 0 if report.get("status") == "ready" else 1
    except (URLError, TimeoutError, ValueError) as exc:
        print(json.dumps({"status": "unknown", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
