"""Prepare immutable stage-1 classifier/data-sourcing receipts.

This is a packet-construction command, not part of suite scoring. Real calls
require an explicit reviewer-coordination acknowledgement. The suite later
loads these receipts and never re-runs classification with later knowledge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_pipeline import (
    CalibrationClassifierSourcingAgent,
    build_classifier_input,
    run_classifier_sourcing,
    verify_classifier,
)
from run_suite import CLASSIFIER_RECEIPTS_DIR, load_packets


async def _run_classifier(packet: dict[str, Any]) -> dict[str, Any]:
    agent = CalibrationClassifierSourcingAgent()
    if packet.get("synthetic"):
        agent.claude_code_allowed_tools = ()
    return await run_classifier_sourcing(
        build_classifier_input(packet),
        agent=agent,
    )


async def prepare_classifier_receipts(
    packets: list[dict[str, Any]],
    *,
    receipts_dir: Path,
    dry_run: bool,
    classifier_runner: Callable[
        [dict[str, Any]], Awaitable[dict[str, Any]]
    ] = _run_classifier,
) -> dict[str, list[str]]:
    """Create one immutable receipt per packet; dry-run is call/write free.

    Existing receipts are skip-and-report (never overwritten). Returns
    ``{"prepared": [...], "skipped_existing": [...], "rejected": [...]}``.
    Rejected burns (verification failed) are not written.
    """
    prepared: list[str] = []
    skipped_existing: list[str] = []
    rejected: list[str] = []
    if not dry_run:
        receipts_dir.mkdir(parents=True, exist_ok=True)
    for packet in packets:
        case_id = packet["case_id"]
        out_path = receipts_dir / f"{case_id}.json"
        if out_path.exists():
            skipped_existing.append(case_id)
            continue
        if dry_run:
            prepared.append(case_id)
            continue
        receipt = await classifier_runner(packet)
        receipt["verification"] = verify_classifier(packet, receipt)
        if not receipt["verification"]["ok"]:
            rejected.append(case_id)
            print(
                f"{case_id}: REJECTED verification "
                f"{receipt['verification']['mismatches']}",
                flush=True,
            )
            continue
        tmp_path = out_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(out_path)
        prepared.append(case_id)
    return {
        "prepared": prepared,
        "skipped_existing": skipped_existing,
        "rejected": rejected,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="comma-separated case_ids")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reviewer-approved",
        action="store_true",
        help="required for real LLM calls after resident-reviewer coordination",
    )
    parser.add_argument(
        "--receipts-dir",
        type=Path,
        default=CLASSIFIER_RECEIPTS_DIR,
    )
    args = parser.parse_args()
    if not args.dry_run and not args.reviewer_approved:
        raise SystemExit(
            "real classifier calls require --reviewer-approved after coordination"
        )
    only = [part.strip() for part in args.only.split(",")] if args.only else None
    packets = load_packets(only)
    result = await prepare_classifier_receipts(
        packets,
        receipts_dir=args.receipts_dir,
        dry_run=args.dry_run,
    )
    mode = "planned" if args.dry_run else "persisted"
    for case_id in result["prepared"]:
        print(f"{case_id}: classifier receipt {mode}", flush=True)
    for case_id in result["skipped_existing"]:
        print(f"{case_id}: skipped existing receipt (immutable)", flush=True)
    for case_id in result.get("rejected") or []:
        print(f"{case_id}: classifier receipt rejected (not written)", flush=True)
    if result.get("rejected"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
