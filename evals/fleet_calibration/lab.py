"""Isolated decision-replay artifacts; never publishes recommendations or orders.

Audit: python -m evals.fleet_calibration.lab --only boot_2017 --audit-only
Live:  python -m evals.fleet_calibration.lab --only boot_2017 --out <new.json>
The two fictional controls run first. Historical outcomes are fetched only
AFTER the decisions/reviews are sealed, and never enter the Trader inputs.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from evals.fleet_calibration import run_suite, score

CONTROLS = ("nlf_synthetic", "omk_synthetic")
# Versioned control fixtures, not mutable labels. Changing a control requires
# an explicit benchmark version change; never weaken one mid-run to get green.
CONTROL_DIGESTS_V2 = {
    "nlf_synthetic": "a66b7acbbff1a4d285f631b3fe7f905604b6e11628c8fe599c5504dcbea3c7e0",
    "omk_synthetic": "f760f0d400364aec169347abbbee7127f142c7c8ce5dd1e4996ca01195beafc8",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def select_cases(case_ids: list[str]) -> list[dict]:
    selected = list(dict.fromkeys([*CONTROLS, *case_ids]))
    packets = run_suite.load_packets(selected)
    missing = set(selected) - {p["case_id"] for p in packets}
    if missing:
        raise ValueError(f"Unknown cases: {', '.join(sorted(missing))}")
    return packets


def preflight(packets: list[dict]) -> list[dict]:
    from evals.fleet_calibration.agent_pipeline import verify_classifier
    rows = []
    for packet in packets:
        errors = run_suite.temporal_audit(packet)
        if run_suite.packet_year_audit(packet):
            errors.append("Calendar years remain in masked analyst inputs")
        receipt = run_suite.load_classifier_receipt(packet)
        if receipt is None or not verify_classifier(packet, receipt, require_binding=True)["ok"]:
            errors.append("Missing, invalid or case-unbound immutable sourcing receipt; prepare a bound v2 receipt")
        rows.append({"case_id": packet["case_id"], "errors": errors})
    return rows


def qualification(result: dict, packet: dict) -> str | None:
    if result.get("status") != "ok":
        return result.get("error") or result.get("status") or "missing result"
    try:
        if run_suite.packet_year_audit(packet):
            return "Calendar years remain in masked analyst inputs"
        integrity = score.integrity_disqualification(result, packet)
        if integrity:
            return f"{integrity[0]}: {integrity[1]}"
        reason = score.pipeline_disqualification(result, packet, require_pipeline=True)
        if reason:
            return reason
        from evals.fleet_calibration.agent_pipeline import verify_classifier
        receipt = (result.get("agent_pipeline") or {}).get("classifier_data_sourcing") or {}
        if not verify_classifier(packet, receipt, require_binding=True)["ok"]:
            return "Sourcing receipt is not bound to this frozen case"
        return None
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return f"Invalid replay evidence: {type(exc).__name__}"


def summarize(document: dict) -> dict:
    """Re-derive qualification; don't trust saved reviewer booleans or counters."""
    rows = []
    latest = {r["case_id"]: r for r in document.get("results", [])}
    for entry in document["lab_manifest"]["cases"]:
        packet = entry["packet"]
        result = latest.get(packet["case_id"], {})
        reason = qualification(result, packet)
        if digest(packet) != entry["packet_sha256"]:
            reason = "Frozen packet hash mismatch"
        rows.append({
            "case_id": packet["case_id"], "synthetic": bool(packet.get("synthetic")),
            "action": result.get("action"), "qualified": reason is None,
            "exclusion_reason": reason,
            "matches_expected_class": result.get("action") in packet.get("expected_classes", []),
            "confidence": result.get("confidence"), "size": result.get("size"),
            "size_units": result.get("size_units"),
            "rationale": result.get("rationale_summary"),
            "falsifiers": (result.get("output_full") or {}).get("falsifiers", []),
            "next_validation_point": (result.get("output_full") or {}).get("next_validation_point"),
            "review_violations": (((result.get("agent_pipeline") or {}).get("review") or {}).get("output") or {}).get("violations", []),
            "review_warnings": (((result.get("agent_pipeline") or {}).get("review") or {}).get("output") or {}).get("warnings", []),
            "grading_mismatches": (((result.get("agent_pipeline") or {}).get("grading") or {}).get("verification") or {}).get("mismatches", []),
        })
    controls = {r["case_id"]: r for r in rows if r["case_id"] in CONTROLS}
    manifest_packets = [entry["packet"] for entry in document["lab_manifest"]["cases"]]
    ids = [p["case_id"] for p in manifest_packets]
    result_ids = [r["case_id"] for r in document.get("results", [])]
    control_protocol = (valid_control_protocol(manifest_packets)
                        and result_ids == ids[:len(result_ids)]
                        and len(result_ids) >= 2)
    controls_passed = control_protocol and all(c in controls and controls[c]["qualified"] and
                          controls[c]["matches_expected_class"] for c in CONTROLS)
    return {
        "status": "qualified" if controls_passed and all(r["qualified"] for r in rows) else "incomplete",
        "qualification_protocol": "case_bound_v2",
        "recorded_protocol_version": document["lab_manifest"].get("version", 1),
        "controls_passed": controls_passed,
        "historical_interpretation_allowed": controls_passed,
        "cases": rows,
        "limitations": [
            "Production Trader plus independent benchmark agents, not the full live allocation workflow.",
            "Curated cases test failure modes, not an unbiased sample or proof of investment alpha.",
            "Masking reduces but cannot eliminate model-memory contamination.",
            "Market returns are gross diagnostics, not sized trades or after-tax family performance.",
            "No live recommendations, predictions, approvals, or fills are written by this runner.",
        ],
    }


def save_new(path: Path, document: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False)


def valid_control_protocol(packets: list[dict]) -> bool:
    ids = [p["case_id"] for p in packets]
    return (ids[:2] == list(CONTROLS) and len(ids) == len(set(ids))
            and all(digest({k: v for k, v in p.items() if k != "_file"}) == CONTROL_DIGESTS_V2[p["case_id"]]
                    for p in packets[:2])
            and packets[0].get("synthetic") is True and packets[1].get("synthetic") is True
            and packets[0].get("grading") == "synthetic_winner"
            and packets[1].get("grading") == "synthetic_trap"
            and packets[0].get("expected_classes") == ["buy"]
            and packets[1].get("expected_classes") == ["hold"])


async def run_lab(packets: list[dict], out: Path) -> dict:
    from argosy.config import get_settings
    if not valid_control_protocol(packets):
        raise ValueError("The fictional winner/trap control pair must be first and unique")
    if not get_settings().anthropic.claude_code_isolated:
        raise ValueError("Replay requires isolated CLI settings (no project memory/hooks)")
    if out.exists() or out.with_suffix(".lab.json").exists():
        raise ValueError("Replay artifacts are write-once; choose a new --out")
    errors = preflight(packets)
    if any(row["errors"] for row in errors):
        raise ValueError(json.dumps(errors))
    out.parent.mkdir(parents=True, exist_ok=True)
    document = run_suite.new_pipeline_run_doc(started=datetime.now(timezone.utc).isoformat())
    document["lab_manifest"] = {
        "version": 2, "horizons_months": [6, 12, 24],
        "cases": [{"packet": deepcopy(p), "packet_sha256": digest(p)} for p in packets],
    }
    save_new(out, document)

    def persist() -> None:
        # Only this newly-created, unsealed run is mutable while stages complete.
        out.write_text(json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")

    for packet in packets:
        print(f"Replay {packet['case_id']}...", flush=True)
        base = {key: packet.get(key) for key in ("case_id", "alias", "category", "grading", "freeze_date")}
        try:
            await run_suite.execute_live_point(
                packet, base, run_doc=document, out_path=out, persist=persist,
                classifier_receipt=run_suite.load_classifier_receipt(packet),
            )
        except Exception as exc:
            if not any(r is base for r in document["results"]):
                document["results"].append(base)
            base.update(status="pipeline_error", error=f"{type(exc).__name__}: {exc}")
            persist()
        reason = qualification(base, packet)
        print(f"  {base.get('action', 'no decision')}: {reason or 'qualified'}", flush=True)
        if packet["case_id"] in CONTROLS and (reason or base.get("action") not in packet["expected_classes"]):
            break  # don't interpret historical results when the control lens fails
    document["finished"] = datetime.now(timezone.utc).isoformat()
    persist()
    report = summarize(json.loads(out.read_text(encoding="utf-8")))
    report["run_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
    save_new(out.with_suffix(".lab.json"), report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", required=True, help="Comma-separated existing frozen case IDs; controls always included")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        packets = select_cases([c.strip() for c in args.only.split(",") if c.strip()])
        if args.audit_only:
            report = preflight(packets)
            print(json.dumps(report, indent=2))
            return 2 if any(r["errors"] for r in report) else 0
        if args.out is None:
            parser.error("--out is required for a live run")
        report = asyncio.run(run_lab(packets, args.out))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["status"] == "qualified" else 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
