"""Read-only summary of the immutable historical fleet-calibration artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_replay_root() -> Path:
    return Path(__file__).resolve().parents[2] / "evals" / "fleet_calibration"


def _latest_scored_run(runs_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for path in runs_dir.glob("*.json"):
        report = path.with_name(path.stem + "_report.md")
        if not report.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("results"), list):
            continue
        candidates.append((report.stat().st_mtime, path, document))
    if not candidates:
        return None
    _, path, document = max(candidates, key=lambda row: row[0])
    return path, document


def _temporal_violation(packet: dict[str, Any]) -> bool:
    if not isinstance(packet, dict) or not isinstance(packet.get("synthetic"), bool):
        return True
    if packet.get("synthetic"):
        return False
    freeze_raw = packet.get("freeze_date")
    sources = packet.get("sources") or []
    if not freeze_raw or not isinstance(sources, list) or not sources:
        return True
    try:
        freeze = datetime.fromisoformat(str(freeze_raw)).date()
    except ValueError:
        return True
    for source in sources:
        if not isinstance(source, dict):
            return True
        try:
            published = datetime.fromisoformat(str(source.get("date"))).date()
        except (TypeError, ValueError):
            return True
        if published > freeze:
            return True
    return False


def _packet_index(root: Path) -> dict[str, dict[str, Any]]:
    packets: dict[str, dict[str, Any]] = {}
    for path in (root / "packets").glob("*.json"):
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
            packets[str(packet["case_id"])] = packet
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
    return packets


def _coverage(root: Path, packets: dict[str, dict[str, Any]]) -> dict[str, int]:
    from evals.fleet_calibration.agent_pipeline import verify_classifier

    receipt_dir = root / "classifier_receipts"
    receipt_ids = {path.stem for folder in (receipt_dir, receipt_dir / "v2") for path in folder.glob("*.json")}
    temporal_ids = {case_id for case_id, packet in packets.items() if _temporal_violation(packet)}
    eligible = (receipt_ids & set(packets)) - temporal_ids
    replay_ready = set()
    for case_id in eligible:
        try:
            receipt_path = receipt_dir / "v2" / f"{case_id}.json"
            if not receipt_path.exists():
                receipt_path = receipt_dir / f"{case_id}.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if verify_classifier(packets[case_id], receipt, require_binding=True)["ok"]:
                replay_ready.add(case_id)
        except (AttributeError, ValueError, KeyError, TypeError, OSError):
            pass  # Invalid receipts are counted, never ready.
    return {
        "packets": len(packets),
        "immutable_receipts": len(receipt_ids),
        "replay_ready": len(replay_ready),
        "missing_receipt": len(set(packets) - receipt_ids - temporal_ids),
        "invalid_receipt": len(eligible - replay_ready),
        "temporal_disqualified": len(temporal_ids),
    }


def build_historical_replay_summary(
    *, replay_root: Path | None = None
) -> dict[str, Any]:
    """Return the newest scored replay, kept separate from forward outcomes."""

    root = replay_root or _default_replay_root()
    from argosy.services.replay_lab_summary import latest_lab_summary
    lab = latest_lab_summary(root)
    packets = _packet_index(root)
    coverage = _coverage(root, packets)
    latest = _latest_scored_run(root / "runs")
    if latest is None:
        return {
            "status": "not_run",
            "separate_from_forward_live": True,
            "coverage": coverage,
            "message": "No immutable scored historical replay is available yet.",
            "cases": [],
            "lab": lab,
        }

    run_path, document = latest
    if any(not isinstance(result, dict) for result in document["results"]):
        return {"status": "invalid", "separate_from_forward_live": True,
                "coverage": coverage, "cases": [], "lab": lab,
                "message": "Replay results are malformed; no investment-test score is available."}
    # Failed/retried attempts remain in the immutable file.  The newest attempt
    # is authoritative; superseded attempts stay auditable but do not double-count.
    latest_by_case: dict[str, dict[str, Any]] = {}
    for result in document.get("results") or []:
        if result.get("case_id"):
            latest_by_case[str(result["case_id"])] = result

    cases: list[dict[str, Any]] = []
    for case_id, result in latest_by_case.items():
        if result.get("status") != "ok":
            continue
        pipeline = result.get("agent_pipeline") or {}
        review = ((pipeline.get("review") or {}).get("output") or {})
        grade = ((pipeline.get("grading") or {}).get("output") or {})
        output_full = result.get("output_full") or {}
        from evals.fleet_calibration.lab import qualification

        packet = packets.get(case_id)
        exclusion_reason = qualification(result, packet) if packet else "Frozen packet missing"
        reviewer_qualified = exclusion_reason is None
        violations = [str(value) for value in (review.get("violations") or [])]
        if exclusion_reason:
            violations.insert(0, exclusion_reason)
        cases.append(
            {
                "case_id": case_id,
                "category": result.get("category"),
                "grading": result.get("grading"),
                "action": result.get("action"),
                "confidence": result.get("confidence"),
                "size": result.get("size"),
                "size_units": result.get("size_units"),
                "rationale_summary": result.get("rationale_summary"),
                "falsifiers": output_full.get("falsifiers") or [],
                "next_validation_point": output_full.get("next_validation_point"),
                "rerating_horizon": output_full.get("rerating_horizon"),
                "expected_actions": packets.get(case_id, {}).get(
                    "expected_classes", []
                ),
                "class_score": grade.get("score"),
                "in_expected_class": grade.get("in_expected_class"),
                "reviewer_qualified": reviewer_qualified,
                "synthetic": bool(packet and packet.get("synthetic")),
                "review_flags": {
                    "output_clean": review.get("output_clean"),
                    "packet_fidelity": review.get("packet_fidelity"),
                    "workflow_correct": review.get("workflow_correct"),
                    "reasoning_grounded_score": review.get("reasoning_grounded_score"),
                },
                "review_violations": violations[:3],
                "review_warnings": review.get("warnings") or [],
                "acted_return_pct": grade.get("acted_return_pct") if reviewer_qualified and not packet.get("synthetic") else None,
                "benchmark_return_pct": grade.get("benchmark_return_pct") if reviewer_qualified and not packet.get("synthetic") else None,
            }
        )

    raw_correct = sum(case.get("in_expected_class") is True for case in cases)
    qualified = [case for case in cases if case["reviewer_qualified"]]
    certified_correct = sum(case.get("in_expected_class") is True for case in qualified)
    generated_at = datetime.fromtimestamp(
        run_path.with_name(run_path.stem + "_report.md").stat().st_mtime,
        tz=timezone.utc,
    ).isoformat()
    return {
        "status": "scored",
        "run_id": run_path.stem,
        "started_at": document.get("started"),
        "scored_at": generated_at,
        "separate_from_forward_live": True,
        "coverage": {**coverage, "executed": len(cases)},
        "raw_direction": {
            "correct": raw_correct,
            "total": len(cases),
            "rate": raw_correct / len(cases) if cases else None,
        },
        "reviewer_certified": {
            "correct": certified_correct,
            "total": len(qualified),
            "rate": certified_correct / len(qualified) if qualified else None,
            "disqualified": len(cases) - len(qualified),
        },
        "message": (
            "Curated replay diagnostics, not proof of investment returns. Synthetic "
            "controls are fictional; historical class matches are not portfolio P&L. "
            "Forward recommendation outcomes remain a separate scorecard."
        ),
        "evidence_counts": {
            "synthetic": sum(case["synthetic"] for case in cases),
            "historical": sum(not case["synthetic"] for case in cases),
        },
        "cases": cases,
        "lab": lab,
    }


__all__ = ["build_historical_replay_summary"]
