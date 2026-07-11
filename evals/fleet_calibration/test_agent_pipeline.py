from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _packet() -> dict:
    return {
        "case_id": "masked_case",
        "_file": "masked_case.json",
        "alias": "Masked Systems",
        "category": "A",
        "grading": "F1_lenient",
        "real": "Secret Corp @ 2020-01-01 ($10)",
        "freeze_date": "2020-01-01",
        "rescale_factor": 2.5,
        "synthetic": False,
        "expected_classes": ["buy"],
        "positioned": False,
        "positions": "No existing position in Masked Systems.",
        "contamination_terms": ["Secret Corp", "SECR", "Famous Product"],
        "resolution": {
            "price_at_freeze_real": 10.0,
            "price_at_horizon_real": 30.0,
            "benchmark_return_pct": 200.0,
        },
        "sources": [
            {
                "fact": "Revenue grew 40%",
                "url": "https://example.test/filing",
                "date": "2019-12-20",
            }
        ],
        "analyst_reports": [
            {"agent_role": "fundamentals", "report": "Masked revenue grew 40%."},
            {"agent_role": "news", "report": "A generic product launched."},
        ],
    }


def _classifier_receipt() -> dict:
    return {
        "stage": 1,
        "agent_role": "calibration_classifier_sourcing",
        "output": {
            "category": "A",
            "grading": "F1_lenient",
            "freeze_date": "2020-01-01",
            "classification_rationale": "Pre-freeze evidence fits entry recognition.",
            "sourced_facts": [
                {
                    "fact": "Revenue grew 40%",
                    "url": "https://example.test/filing",
                    "publication_date": "2019-12-20",
                }
            ],
            "confidence": "HIGH",
        },
    }


def _sanitizer_receipt() -> dict:
    return {
        "stage": 2,
        "agent_role": "calibration_sanitizer",
        "output": {
            "safe_to_run": True,
            "checks": [
                {"check": "alias", "verdict": "pass", "evidence": "masked"},
                {
                    "check": "absolute_figure_rescaling",
                    "verdict": "pass",
                    "evidence": "raw-to-masked figures reconcile",
                },
                {
                    "check": "relative_dates",
                    "verdict": "pass",
                    "evidence": "relative labels only",
                },
                {
                    "check": "macro_event_genericization",
                    "verdict": "pass",
                    "evidence": "no named macro event",
                },
            ],
            "leaked_terms": [],
            "summary": "All sanitizer checks pass.",
            "confidence": "HIGH",
        },
    }


def test_classifier_receipt_preserves_packet_construction_provenance() -> None:
    from agent_pipeline import build_classifier_receipt

    receipt = build_classifier_receipt(_packet())

    assert receipt == {
        "stage": 1,
        "agent_role": "classifier_data_sourcing",
        "case_id": "masked_case",
        "packet_file": "masked_case.json",
        "category": "A",
        "grading": "F1_lenient",
        "freeze_date": "2020-01-01",
        "sources": _packet()["sources"],
        "status": "persisted_fixture",
    }


def test_classifier_agent_contract_requires_dated_primary_sources() -> None:
    from agent_pipeline import CalibrationClassifierSourcingAgent

    agent = CalibrationClassifierSourcingAgent.__new__(
        CalibrationClassifierSourcingAgent
    )
    system, user = agent.build_prompt(
        case_brief={
            "company": "Secret Corp",
            "candidate_window": "before the first major re-rating",
            "research_question": "Is this an entry-recognition or trap case?",
        }
    )

    assert "publication date" in system
    assert "primary source" in system
    assert "Secret Corp" in user
    assert "expected_classes" not in user
    assert "eventual outcome" in system


def test_classifier_receipt_uses_persisted_agent_output_when_present() -> None:
    from agent_pipeline import build_classifier_receipt

    packet = _packet()
    packet["classifier_receipt"] = {
        "agent_role": "calibration_classifier_sourcing",
        "model": "claude-opus-4-8",
        "output": {"category": "A", "freeze_date": "2020-01-01"},
    }

    receipt = build_classifier_receipt(packet)

    assert receipt["stage"] == 1
    assert receipt["status"] == "agent_sourced"
    assert receipt["agent_receipt"] == packet["classifier_receipt"]


def test_classifier_input_has_source_manifest_but_no_outcome_answer_key() -> None:
    from agent_pipeline import build_classifier_input

    payload = build_classifier_input(_packet())
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["subject"] == "Secret Corp"
    assert payload["freeze_date"] == "2020-01-01"
    assert payload["source_manifest"] == _packet()["sources"]
    assert "expected_classes" not in encoded
    assert "benchmark_return_pct" not in encoded
    assert "$10" not in encoded


def test_classifier_verifier_rejects_missing_or_post_freeze_sources() -> None:
    from agent_pipeline import verify_classifier

    missing = _classifier_receipt()
    missing["output"]["sourced_facts"] = []
    missing_result = verify_classifier(_packet(), missing)
    assert missing_result["ok"] is False
    assert any(m["field"] == "sourced_facts" for m in missing_result["mismatches"])

    future = _classifier_receipt()
    future["output"]["sourced_facts"][0]["publication_date"] = "2020-01-02"
    future_result = verify_classifier(_packet(), future)
    assert future_result["ok"] is False
    assert any(
        m["field"] == "sourced_facts[0].publication_date"
        for m in future_result["mismatches"]
    )


def test_stage_receipt_verifiers_reject_wrong_role_or_schema() -> None:
    from agent_pipeline import verify_classifier, verify_sanitizer

    wrong_classifier = _classifier_receipt()
    wrong_classifier["agent_role"] = "not_the_classifier"
    assert verify_classifier(_packet(), wrong_classifier)["ok"] is False

    wrong_sanitizer = _sanitizer_receipt()
    wrong_sanitizer["stage"] = 99
    assert verify_sanitizer(_packet(), wrong_sanitizer)["ok"] is False


def test_sanitizer_input_is_blind_to_outcome_but_has_exact_trader_payload() -> None:
    from agent_pipeline import CalibrationSanitizerAgent, build_sanitizer_input

    packet = _packet()
    sourced_facts = [
        {
            "fact": "Unscaled revenue was $100M",
            "url": "https://example.test/filing",
            "publication_date": "2019-12-20",
        }
    ]
    packet["_classifier_receipt"] = {
        "output": {"sourced_facts": sourced_facts}
    }
    payload = build_sanitizer_input(packet, "EXACT CONSTRAINTS")
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["candidate_payload"] == {
        "alias": "Masked Systems",
        "analyst_reports": _packet()["analyst_reports"],
        "positions": "No existing position in Masked Systems.",
        "constraints_rendered": "EXACT CONSTRAINTS",
    }
    assert payload["forbidden_terms"] == ["Secret Corp", "SECR", "Famous Product"]
    assert payload["source_manifest"] == sourced_facts
    assert "Secret Corp @ 2020-01-01" not in encoded
    assert "expected_classes" not in encoded
    assert "benchmark_return_pct" not in encoded
    assert CalibrationSanitizerAgent.claude_code_allowed_tools == ()


def test_review_input_reads_replay_without_answer_key() -> None:
    from agent_pipeline import build_review_input

    replay = {
        "case_id": "masked_case",
        "packet_snapshot": {
            "alias": "Persisted Alias",
            "analyst_reports": [{"agent_role": "fundamentals", "report": "persisted"}],
            "positions": "Persisted positions",
            "freeze_date": "2020-01-01",
            "sources": _packet()["sources"],
        },
        "constraints_rendered": "EXACT CONSTRAINTS",
        "response_raw": '{"action":"buy"}',
        "output_full": {"action": "buy", "rationale_summary": "Packet-grounded rationale"},
        "output_audit": {"contaminated": False},
        "agent_pipeline": {"sanitizer": {"safe_to_run": True}},
    }
    payload = build_review_input(_packet(), replay)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["replay"]["response_raw"] == '{"action":"buy"}'
    assert payload["replay"]["constraints_rendered"] == "EXACT CONSTRAINTS"
    assert payload["packet"]["analyst_reports"] == [
        {"agent_role": "fundamentals", "report": "persisted"}
    ]
    assert payload["packet"]["alias"] == "Persisted Alias"
    assert "Secret Corp @ 2020-01-01" not in encoded
    assert "expected_classes" not in encoded
    assert "benchmark_return_pct" not in encoded


def test_grader_input_receives_answer_key_only_after_review() -> None:
    from agent_pipeline import build_grader_input

    replay = {
        "action": "buy",
        "confidence": "MEDIUM",
        "size": 1.0,
        "size_units": "currency",
        "rationale_summary": "Packet-grounded rationale",
        "agent_pipeline": {
            "review": {
                "output_clean": True,
                "packet_fidelity": True,
                "workflow_correct": True,
                "reasoning_grounded_score": 4,
            }
        },
    }
    payload = build_grader_input(_packet(), replay)

    assert payload["answer_key"]["expected_classes"] == ["buy"]
    assert payload["answer_key"]["resolution"]["benchmark_return_pct"] == 200.0
    assert payload["review"]["reasoning_grounded_score"] == 4
    assert payload["fleet_result"]["action"] == "buy"


@pytest.mark.asyncio
async def test_replay_agents_reload_after_each_persisted_stage() -> None:
    from agent_pipeline import run_replay_pipeline

    persisted = {
        "case_id": "masked_case",
        "action": "buy",
        "agent_pipeline": {"sanitizer": {"output": {"safe_to_run": True}}},
    }
    loads: list[dict] = []
    writes: list[str] = []

    def load_replay() -> dict:
        snapshot = deepcopy(persisted)
        loads.append(snapshot)
        return snapshot

    def persist_stage(stage: str, receipt: dict) -> None:
        persisted.setdefault("agent_pipeline", {})[stage] = receipt
        writes.append(stage)

    async def review_runner(packet: dict, replay: dict) -> dict:
        assert "review" not in replay["agent_pipeline"]
        return {
            "stage": 4,
            "agent_role": "calibration_reviewer",
            "output": {
                "output_clean": True,
                "packet_fidelity": True,
                "workflow_correct": True,
                "reasoning_grounded_score": 4,
                "grounding_evidence": ["fact 1"],
                "violations": [],
                "summary": "clean",
                "confidence": "HIGH",
            },
        }

    async def grading_runner(packet: dict, replay: dict) -> dict:
        assert replay["agent_pipeline"]["review"]["stage"] == 4
        return {
            "stage": 5,
            "agent_role": "calibration_grader",
            "output": {
                "in_expected_class": True,
                "score": 1.0,
                "acted_return_pct": 200.0,
                "benchmark_return_pct": 200.0,
                "rationale": "in class",
                "confidence": "HIGH",
            },
        }

    await run_replay_pipeline(
        _packet(),
        load_replay=load_replay,
        persist_stage=persist_stage,
        review_runner=review_runner,
        grading_runner=grading_runner,
    )

    assert writes == ["review", "grading"]
    assert len(loads) == 2
    assert persisted["agent_pipeline"]["grading"]["verification"]["ok"] is True


def test_grading_verifier_catches_answer_key_and_return_mismatch() -> None:
    from agent_pipeline import verify_grading

    replay = {"action": "buy"}
    receipt = {
        "stage": 5,
        "agent_role": "calibration_grader",
        "output": {
            "in_expected_class": False,
            "score": 0.0,
            "acted_return_pct": 0.0,
            "benchmark_return_pct": 199.0,
            "rationale": "Wrong mechanical claims.",
            "confidence": "HIGH",
        }
    }

    verification = verify_grading(_packet(), replay, receipt)

    assert verification["ok"] is False
    assert verification["expected_in_class"] is True
    assert verification["expected_acted_return_pct"] == 200.0
    assert {m["field"] for m in verification["mismatches"]} == {
        "in_expected_class",
        "acted_return_pct",
        "benchmark_return_pct",
        "score",
    }


def test_grading_verifier_forbids_partial_credit_on_strict_points() -> None:
    from agent_pipeline import verify_grading

    packet = _packet()
    packet["grading"] = "F2_strict"
    replay = {"action": "hold"}
    receipt = {
        "stage": 5,
        "agent_role": "calibration_grader",
        "output": {
            "in_expected_class": False,
            "score": 0.5,
            "acted_return_pct": 0.0,
            "benchmark_return_pct": 200.0,
            "rationale": "Unsupported partial credit.",
            "confidence": "HIGH",
        },
    }

    verification = verify_grading(packet, replay, receipt)

    assert verification["ok"] is False
    assert any(m["field"] == "score" for m in verification["mismatches"])


def test_scored_run_path_is_never_reopened(tmp_path: Path) -> None:
    from run_suite import ensure_output_path_writable

    run_path = tmp_path / "2026-07-11.json"
    run_path.write_text('{"results": []}', encoding="utf-8")
    run_path.with_name("2026-07-11_report.md").write_text("# scored", encoding="utf-8")

    with pytest.raises(RuntimeError, match="scored run is immutable"):
        ensure_output_path_writable(run_path, dry_run=False)

    ensure_output_path_writable(run_path, dry_run=True)


def test_existing_score_report_is_never_overwritten(tmp_path: Path) -> None:
    from score import ensure_report_path_writable

    report_path = tmp_path / "run_report.md"
    report_path.write_text("# immutable", encoding="utf-8")

    with pytest.raises(RuntimeError, match="score report is immutable"):
        ensure_report_path_writable(report_path)


@pytest.mark.asyncio
async def test_live_point_persists_each_agent_boundary_before_next_stage(
    tmp_path: Path,
) -> None:
    from run_suite import execute_live_point

    packet = _packet()
    run_doc = {"results": []}
    out_path = tmp_path / "run.json"
    snapshots: list[dict] = []

    def persist() -> None:
        out_path.write_text(json.dumps(run_doc), encoding="utf-8")
        snapshots.append(deepcopy(run_doc))

    classifier_receipt = _classifier_receipt()

    async def sanitizer_runner(packet: dict, constraints: str) -> dict:
        assert snapshots[-1]["results"][0]["agent_pipeline"][
            "classifier_data_sourcing"
        ]["verification"]["ok"] is True
        return _sanitizer_receipt()

    async def trader_runner(packet: dict) -> dict:
        assert snapshots[-1]["results"][0]["agent_pipeline"]["sanitizer"]["stage"] == 2
        return {
            "status": "ok",
            "action": "buy",
            "confidence": "MEDIUM",
            "size": 1.0,
            "size_units": "currency",
            "rationale_summary": "Packet-grounded rationale with falsifier and clock.",
            "constraints_rendered": "EXACT CONSTRAINTS",
            "response_raw": '{"action":"buy"}',
            "output_full": {"action": "buy"},
        }

    async def review_runner(packet: dict, replay: dict) -> dict:
        assert replay["response_raw"] == '{"action":"buy"}'
        assert "review" not in replay["agent_pipeline"]
        return {
            "stage": 4,
            "agent_role": "calibration_reviewer",
            "output": {
                "output_clean": True,
                "packet_fidelity": True,
                "workflow_correct": True,
                "reasoning_grounded_score": 4,
                "grounding_evidence": ["fact 1"],
                "violations": [],
                "summary": "Grounded and clean.",
                "confidence": "HIGH",
            },
        }

    async def grading_runner(packet: dict, replay: dict) -> dict:
        assert replay["agent_pipeline"]["review"]["stage"] == 4
        return {
            "stage": 5,
            "agent_role": "calibration_grader",
            "output": {
                "in_expected_class": True,
                "score": 1.0,
                "acted_return_pct": 200.0,
                "benchmark_return_pct": 200.0,
                "rationale": "In expected class.",
                "confidence": "HIGH",
            },
        }

    result = await execute_live_point(
        packet,
        {
            "case_id": "masked_case",
            "alias": "Masked Systems",
            "category": "A",
            "grading": "F1_lenient",
        },
        run_doc=run_doc,
        out_path=out_path,
        persist=persist,
        classifier_receipt=classifier_receipt,
        sanitizer_runner=sanitizer_runner,
        trader_runner=trader_runner,
        review_runner=review_runner,
        grading_runner=grading_runner,
        constraints_rendered="EXACT CONSTRAINTS",
    )

    assert result["status"] == "ok"
    assert [s["results"][0]["agent_pipeline"].keys() for s in snapshots] == [
        {"classifier_data_sourcing"},
        {"classifier_data_sourcing", "sanitizer"},
        {"classifier_data_sourcing", "sanitizer"},
        {"classifier_data_sourcing", "sanitizer", "review"},
        {"classifier_data_sourcing", "sanitizer", "review", "grading"},
    ]


@pytest.mark.asyncio
async def test_sanitizer_rejection_prevents_trader_call(tmp_path: Path) -> None:
    from run_suite import execute_live_point

    run_doc = {"results": []}
    out_path = tmp_path / "run.json"

    def persist() -> None:
        out_path.write_text(json.dumps(run_doc), encoding="utf-8")

    classifier_receipt = _classifier_receipt()

    async def sanitizer_runner(packet: dict, constraints: str) -> dict:
        return {"stage": 2, "output": {"safe_to_run": False}}

    async def trader_runner(packet: dict) -> dict:
        raise AssertionError("Trader must not run after sanitizer rejection")

    result = await execute_live_point(
        _packet(),
        {"case_id": "masked_case"},
        run_doc=run_doc,
        out_path=out_path,
        persist=persist,
        classifier_receipt=classifier_receipt,
        sanitizer_runner=sanitizer_runner,
        trader_runner=trader_runner,
        constraints_rendered="EXACT CONSTRAINTS",
    )

    assert result["status"] == "disqualified_sanitizer"
    assert json.loads(out_path.read_text(encoding="utf-8"))["results"][0]["status"] == (
        "disqualified_sanitizer"
    )


@pytest.mark.asyncio
async def test_unverifiable_sanitizer_check_blocks_trader(tmp_path: Path) -> None:
    from run_suite import execute_live_point

    run_doc = {"results": []}
    out_path = tmp_path / "run.json"

    def persist() -> None:
        out_path.write_text(json.dumps(run_doc), encoding="utf-8")

    classifier_receipt = _classifier_receipt()

    async def sanitizer_runner(packet: dict, constraints: str) -> dict:
        receipt = _sanitizer_receipt()
        receipt["output"]["checks"][1]["verdict"] = "unverifiable"
        return receipt

    async def trader_runner(packet: dict) -> dict:
        raise AssertionError("Trader must not run with an unverifiable sanitizer check")

    result = await execute_live_point(
        _packet(),
        {"case_id": "masked_case"},
        run_doc=run_doc,
        out_path=out_path,
        persist=persist,
        classifier_receipt=classifier_receipt,
        sanitizer_runner=sanitizer_runner,
        trader_runner=trader_runner,
        constraints_rendered="EXACT CONSTRAINTS",
    )

    assert result["status"] == "disqualified_sanitizer"


@pytest.mark.asyncio
async def test_missing_construction_classifier_receipt_blocks_live_point(
    tmp_path: Path,
) -> None:
    from run_suite import execute_live_point

    run_doc = {"results": []}
    out_path = tmp_path / "run.json"

    def persist() -> None:
        out_path.write_text(json.dumps(run_doc), encoding="utf-8")

    async def sanitizer_runner(packet: dict, constraints: str) -> dict:
        raise AssertionError("sanitizer must not run without stage-1 receipt")

    result = await execute_live_point(
        _packet(),
        {"case_id": "masked_case"},
        run_doc=run_doc,
        out_path=out_path,
        persist=persist,
        classifier_receipt=None,
        sanitizer_runner=sanitizer_runner,
        constraints_rendered="EXACT CONSTRAINTS",
    )

    assert result["status"] == "disqualified_classifier"
    persisted = json.loads(out_path.read_text(encoding="utf-8"))["results"][0]
    assert persisted["agent_pipeline"]["classifier_data_sourcing"]["status"] == (
        "missing"
    )


def test_synthetic_sanitizer_accepts_not_applicable_protocol_checks() -> None:
    from agent_pipeline import verify_sanitizer

    packet = _packet()
    packet["synthetic"] = True
    receipt = _sanitizer_receipt()
    for check in receipt["output"]["checks"][1:]:
        check["verdict"] = "not_applicable"

    assert verify_sanitizer(packet, receipt)["ok"] is True


def test_persisted_replay_loader_uses_latest_duplicate_case(tmp_path: Path) -> None:
    from run_suite import load_persisted_result

    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "results": [
                    {"case_id": "masked_case", "status": "error", "attempt": 1},
                    {"case_id": "masked_case", "status": "ok", "attempt": 2},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_persisted_result(path, "masked_case")["attempt"] == 2


def test_classifier_receipt_loads_from_construction_sidecar(tmp_path: Path) -> None:
    from run_suite import load_classifier_receipt

    receipt = _classifier_receipt()
    (tmp_path / "masked_case.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )

    assert load_classifier_receipt(_packet(), receipts_dir=tmp_path) == receipt


def test_dry_run_exit_code_blocks_missing_classifier_receipts() -> None:
    from run_suite import classifier_receipt_preflight, dry_run_exit_code

    assert dry_run_exit_code({
        "results": [{"status": "dry_ok"}, {"status": "dry_blocked_classifier"}]
    }) == 2
    assert dry_run_exit_code({"results": [{"status": "dry_ok"}]}) == 0
    bad = _classifier_receipt()
    bad["output"]["category"] = "B"
    assert classifier_receipt_preflight(_packet(), bad)["ok"] is False
    assert classifier_receipt_preflight(_packet(), _classifier_receipt())["ok"] is True


@pytest.mark.asyncio
async def test_classifier_preparation_persists_before_returning(
    tmp_path: Path,
) -> None:
    from prepare_classifier import prepare_classifier_receipts

    async def classifier_runner(packet: dict) -> dict:
        return _classifier_receipt()

    prepared = await prepare_classifier_receipts(
        [_packet()],
        receipts_dir=tmp_path,
        dry_run=False,
        classifier_runner=classifier_runner,
    )

    assert prepared == ["masked_case"]
    persisted = json.loads(
        (tmp_path / "masked_case.json").read_text(encoding="utf-8")
    )
    assert persisted["verification"]["ok"] is True


@pytest.mark.asyncio
async def test_classifier_preparation_dry_run_is_call_and_write_free(
    tmp_path: Path,
) -> None:
    from prepare_classifier import prepare_classifier_receipts

    async def classifier_runner(packet: dict) -> dict:
        raise AssertionError("dry-run must not call classifier")

    prepared = await prepare_classifier_receipts(
        [_packet()],
        receipts_dir=tmp_path,
        dry_run=True,
        classifier_runner=classifier_runner,
    )

    assert prepared == ["masked_case"]
    assert list(tmp_path.iterdir()) == []


def test_score_uses_verified_grading_agent_judgment() -> None:
    from score import grade_point

    result = {
        "action": "hold",
        "rationale_summary": "A reasoned pass with a falsifier and next validation point.",
        "agent_pipeline": {
            "review": {
                "output": {
                    "output_clean": True,
                    "packet_fidelity": True,
                    "workflow_correct": True,
                    "reasoning_grounded_score": 4,
                }
            },
            "grading": {
                "stage": 5,
                "agent_role": "calibration_grader",
                "output": {
                    "in_expected_class": False,
                    "score": 0.5,
                    "acted_return_pct": 0.0,
                    "benchmark_return_pct": 200.0,
                    "rationale": "F1 lenient partial credit.",
                    "confidence": "HIGH",
                },
                "verification": {"ok": True},
            },
        },
    }

    grade = grade_point(result, _packet())

    assert grade["score"] == 0.5
    assert grade["in_class"] is False
    assert grade["acted_return_pct"] == 0.0
    assert grade["grade_source"] == "calibration_grader"
    assert grade["notes"] == ["F1 lenient partial credit."]


def test_report_renders_full_structured_stage_receipts() -> None:
    from score import render_pipeline_receipts

    row = {
        "agent_pipeline": {
            "classifier_data_sourcing": _classifier_receipt(),
            "sanitizer": _sanitizer_receipt(),
            "review": {
                "output": {
                    "output_clean": True,
                    "packet_fidelity": True,
                    "workflow_correct": True,
                    "reasoning_grounded_score": 4,
                    "grounding_evidence": ["fact 1"],
                    "violations": [],
                    "summary": "grounded",
                    "confidence": "HIGH",
                }
            },
            "grading": {
                "output": {
                    "in_expected_class": True,
                    "score": 1.0,
                    "acted_return_pct": 200.0,
                    "benchmark_return_pct": 200.0,
                    "rationale": "in class",
                    "confidence": "HIGH",
                },
                "verification": {"ok": True},
            },
        }
    }

    rendered = "\n".join(render_pipeline_receipts(row))

    assert '"sourced_facts"' in rendered
    assert '"checks"' in rendered
    assert '"grounding_evidence"' in rendered
    assert '"rationale": "in class"' in rendered


def test_pipeline_disqualification_revalidates_receipts_at_score_time() -> None:
    from score import pipeline_disqualification
    from run_suite import build_constraints

    pipeline = {
        "classifier_data_sourcing": _classifier_receipt(),
        "sanitizer": _sanitizer_receipt(),
        "review": {
            "stage": 4,
            "agent_role": "calibration_reviewer",
            "output": {
                "output_clean": True,
                "packet_fidelity": True,
                "workflow_correct": True,
                "reasoning_grounded_score": 4,
                "grounding_evidence": ["fact 1"],
                "violations": [],
                "summary": "clean",
                "confidence": "HIGH",
            },
            "verification": {"ok": True},
        },
        "grading": {
            "stage": 5,
            "agent_role": "calibration_grader",
            "output": {
                "in_expected_class": True,
                "score": 1.0,
                "acted_return_pct": 200.0,
                "benchmark_return_pct": 200.0,
                "rationale": "in class",
                "confidence": "HIGH",
            },
            "verification": {"ok": True},
        },
    }
    pipeline["classifier_data_sourcing"]["verification"] = {"ok": True}
    pipeline["sanitizer"]["verification"] = {"ok": True}
    pipeline["review"]["verification"] = {"ok": True}

    tampered = deepcopy(pipeline)
    tampered["classifier_data_sourcing"]["agent_role"] = "not_the_classifier"

    assert pipeline_disqualification(
        {
            "action": "buy",
            "packet_snapshot": {
                "case_id": _packet()["case_id"],
                "alias": _packet()["alias"],
                "analyst_reports": _packet()["analyst_reports"],
                "positions": _packet()["positions"],
                "constraints_rendered": build_constraints(_packet()),
                "freeze_date": _packet()["freeze_date"],
                "sources": _packet()["sources"],
                "trader_call": {
                    "debate_outcome": {},
                    "tier": "T2",
                    "mode": "long_hold",
                    "ticker": _packet()["alias"],
                },
            },
            "constraints_rendered": build_constraints(_packet()),
            "response_raw": '{"action":"buy"}',
            "output_full": {"action": "buy"},
            "agent_pipeline": tampered,
        },
        _packet(),
    ) == "classifier verification failed"


def test_packet_snapshot_validation_covers_constraints_and_trader_call() -> None:
    from run_suite import build_constraints
    from score import packet_snapshot_mismatch

    packet = _packet()
    result = {
        "constraints_rendered": build_constraints(packet),
        "packet_snapshot": {
            "case_id": packet["case_id"],
            "alias": packet["alias"],
            "analyst_reports": packet["analyst_reports"],
            "positions": packet["positions"],
            "constraints_rendered": build_constraints(packet),
            "freeze_date": packet["freeze_date"],
            "sources": packet["sources"],
            "trader_call": {
                "debate_outcome": {},
                "tier": "T2",
                "mode": "long_hold",
                "ticker": packet["alias"],
            },
        },
    }

    assert packet_snapshot_mismatch(result, packet) is None
    result["packet_snapshot"]["trader_call"]["tier"] = "T1"
    assert packet_snapshot_mismatch(result, packet) == "trader_call"


def test_new_pipeline_run_cannot_fall_back_when_receipts_are_missing() -> None:
    from score import pipeline_disqualification

    assert pipeline_disqualification(
        {"action": "buy"},
        _packet(),
        require_pipeline=True,
    ) == "pipeline missing"
    assert pipeline_disqualification(
        {"action": "buy"},
        _packet(),
        require_pipeline=False,
    ) is None


def test_score_time_recomputes_temporal_and_contamination_audits() -> None:
    from score import integrity_disqualification

    future_packet = _packet()
    future_packet["sources"][0]["date"] = "2020-01-02"
    clean_claim = {
        "response_raw": '{"rationale":"clean"}',
        "output_audit": {"contaminated": False},
    }
    assert integrity_disqualification(clean_claim, future_packet)[0] == "temporal"

    contaminated = {
        "response_raw": '{"rationale":"Secret Corp is obvious"}',
        "output_audit": {"contaminated": False},
    }
    assert integrity_disqualification(contaminated, _packet())[0] == "contaminated"


def test_report_recomputes_verification_instead_of_rendering_stale_ok() -> None:
    from score import render_pipeline_receipts

    result = {
        "action": "buy",
        "agent_pipeline": {
            "classifier_data_sourcing": _classifier_receipt(),
            "sanitizer": _sanitizer_receipt(),
            "review": {
                "stage": 4,
                "agent_role": "calibration_reviewer",
                "output": {
                    "output_clean": True,
                    "packet_fidelity": True,
                    "workflow_correct": True,
                    "reasoning_grounded_score": 4,
                    "grounding_evidence": [],
                    "violations": [],
                    "summary": "clean",
                    "confidence": "HIGH",
                },
            },
            "grading": {
                "stage": 5,
                "agent_role": "calibration_grader",
                "output": {
                    "in_expected_class": True,
                    "score": 1.0,
                    "acted_return_pct": 200.0,
                    "benchmark_return_pct": 200.0,
                    "rationale": "in class",
                    "confidence": "HIGH",
                },
            },
        },
    }
    result["agent_pipeline"]["classifier_data_sourcing"]["agent_role"] = "tampered"
    for receipt in result["agent_pipeline"].values():
        receipt["verification"] = {"ok": True}

    rendered = "\n".join(render_pipeline_receipts(result, _packet()))

    assert '"ok": false' in rendered


def test_retry_supersedes_prior_attempt_and_scorer_selects_latest() -> None:
    from run_suite import supersede_prior_attempts
    from score import select_latest_results

    run_doc = {
        "results": [
            {"case_id": "masked_case", "status": "pipeline_error", "attempt": 1}
        ]
    }
    supersede_prior_attempts(run_doc, "masked_case")
    run_doc["results"].append(
        {"case_id": "masked_case", "status": "ok", "attempt": 2}
    )

    assert run_doc["results"][0]["status"] == "superseded_retry"
    assert select_latest_results(run_doc["results"]) == [run_doc["results"][1]]


def test_disqualified_pipeline_report_keeps_structured_receipts() -> None:
    from score import render_disqualified_entry

    result = {
        "case_id": "masked_case",
        "agent_pipeline": {
            "classifier_data_sourcing": _classifier_receipt(),
            "sanitizer": _sanitizer_receipt(),
        },
    }
    rendered = "\n".join(
        render_disqualified_entry(
            "masked_case",
            "agent_pipeline",
            "sanitizer verification failed",
            result,
        )
    )

    assert "sanitizer verification failed" in rendered
    assert '"checks"' in rendered


def test_historical_result_without_agent_pipeline_keeps_legacy_scoring() -> None:
    from score import grade_point, pipeline_disqualification

    result = {
        "action": "buy",
        "rationale_summary": "Legacy rationale with falsifier.",
    }

    assert pipeline_disqualification(
        result, _packet(), require_pipeline=False
    ) is None
    grade = grade_point(result, _packet())
    assert grade["score"] == 1.0
    assert grade["grade_source"] == "legacy_deterministic"
