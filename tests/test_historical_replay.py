from __future__ import annotations

import json
from pathlib import Path

from argosy.services.historical_replay import build_historical_replay_summary


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_historical_replay_does_not_certify_incomplete_receipts(tmp_path: Path) -> None:
    root = tmp_path / "fleet"
    for case_id in ("winner", "unsafe_buy", "miss", "future"):
        _write(
            root / "packets" / f"{case_id}.json",
            {
                "case_id": case_id,
                "synthetic": case_id != "future",
                "freeze_date": None if case_id != "future" else "2020-01-01",
                "sources": [] if case_id != "future" else [
                    {"date": "2020-01-02", "fact": "future"}
                ],
            },
        )
    for case_id in ("winner", "unsafe_buy", "miss"):
        _write(root / "classifier_receipts" / f"{case_id}.json", {})

    def result(case_id: str, *, correct: bool, qualified: bool) -> dict:
        return {
            "case_id": case_id,
            "status": "ok",
            "category": "A",
            "grading": "entry",
            "action": "buy" if correct else "hold",
            "confidence": "MEDIUM",
            "size": 10_000.0,
            "size_units": "currency",
            "rationale_summary": "Buy a starter because the asymmetry is favorable.",
            "output_full": {
                "falsifiers": ["Unit economics reverse for two quarters."],
                "next_validation_point": "Next quarterly report, 2-3 months out.",
                "rerating_horizon": "6-12 months.",
            },
            "packet_snapshot": {"expected_classes": ["buy"]},
            "agent_pipeline": {
                "review": {"output": {
                    "output_clean": True,
                    "packet_fidelity": qualified,
                    "workflow_correct": True,
                    "reasoning_grounded_score": 3,
                    "violations": [] if qualified else ["unsafe typed detail"],
                }},
                "grading": {"output": {
                    "score": 1.0 if correct else 0.0,
                    "in_expected_class": correct,
                    "acted_return_pct": 10.0 if correct else 0.0,
                    "benchmark_return_pct": 10.0,
                }},
            },
        }

    run = root / "runs" / "2026-08-30.json"
    _write(run, {"started": "2026-08-30T00:00:00+00:00", "results": [
        result("winner", correct=True, qualified=True),
        result("unsafe_buy", correct=True, qualified=False),
        result("miss", correct=False, qualified=True),
    ]})
    run.with_name("2026-08-30_report.md").write_text("scored", encoding="utf-8")

    summary = build_historical_replay_summary(replay_root=root)

    assert summary["status"] == "scored"
    assert summary["separate_from_forward_live"] is True
    assert summary["coverage"] == {
        "packets": 4,
        "immutable_receipts": 3,
        "replay_ready": 0,
        "missing_receipt": 0,
        "invalid_receipt": 3,
        "temporal_disqualified": 1,
        "executed": 3,
    }
    assert summary["raw_direction"] == {"correct": 2, "total": 3, "rate": 2 / 3}
    assert summary["reviewer_certified"] == {
        "correct": 0,
        "total": 0,
        "rate": None,
        "disqualified": 3,
    }
    winner = next(row for row in summary["cases"] if row["case_id"] == "winner")
    assert winner["size"] == 10_000.0
    assert winner["size_units"] == "currency"
    assert winner["rationale_summary"].startswith("Buy a starter")
    assert winner["falsifiers"] == ["Unit economics reverse for two quarters."]
    assert winner["next_validation_point"].startswith("Next quarterly report")
    assert winner["rerating_horizon"] == "6-12 months."
    assert winner["acted_return_pct"] is None
    assert winner["review_violations"]


def test_real_control_artifacts_reveal_violation_and_remain_explicitly_synthetic(tmp_path: Path) -> None:
    import shutil
    source = Path(__file__).resolve().parents[1] / "evals" / "fleet_calibration"
    shutil.copytree(source / "packets", tmp_path / "packets")
    shutil.copytree(source / "classifier_receipts", tmp_path / "classifier_receipts")
    (tmp_path / "runs").mkdir()
    for name in ("2026-09-11-daily-control-pair.json", "2026-09-11-daily-control-pair_report.md"):
        shutil.copy2(source / "runs" / name, tmp_path / "runs" / name)
    summary = build_historical_replay_summary(replay_root=tmp_path)
    assert summary["run_id"] == "2026-09-11-daily-control-pair"
    assert summary["reviewer_certified"]["total"] == 0  # Legacy receipts lack case/input binding.
    assert summary["evidence_counts"] == {"synthetic": 2, "historical": 0}
    assert all(row["acted_return_pct"] is None for row in summary["cases"])


def test_historical_replay_reports_not_run(tmp_path: Path) -> None:
    summary = build_historical_replay_summary(replay_root=tmp_path)
    assert summary["status"] == "not_run"
    assert summary["cases"] == []
