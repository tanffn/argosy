from __future__ import annotations

from copy import deepcopy
from datetime import date
import json

import pytest

from evals.fleet_calibration import lab, run_suite
from evals.fleet_calibration.market_outcomes import add_months, compare_horizons


def test_controls_always_first_and_unknown_is_not_silently_skipped():
    assert [p["case_id"] for p in lab.select_cases(["boot_2017", "nlf_synthetic"])] == [
        "nlf_synthetic", "omk_synthetic", "boot_2017"]
    with pytest.raises(ValueError, match="Unknown cases"):
        lab.select_cases(["not_a_case"])


def test_real_frozen_packet_preflight_and_answer_key_isolation():
    packets = lab.select_cases(["boot_2017"])
    assert all(row["errors"] for row in lab.preflight(packets))  # Legacy receipts are unbound.
    packet = deepcopy(packets[-1])
    before = run_suite.build_trader_inputs(packet)
    packet.update(real="ANSWER KEY SECRET", resolution={"outcome": "future winner"},
                  expected_classes=["sell"], future_prices=[100, 1000])
    assert run_suite.build_trader_inputs(packet) == before
    packet["sources"][0]["date"] = "2099-01-01"
    assert lab.preflight([packet])[0]["errors"]


def test_real_previous_control_run_rechecks_recorded_violations_and_tampering():
    # Immutable real LLM receipts, not mocked reviewer booleans.
    path = run_suite.RUNS_DIR / "2026-09-11-daily-control-pair.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    packets = {p["case_id"]: p for p in lab.select_cases([])}
    for result in doc["results"]:
        if result.get("status") != "ok":
            continue
        packet = packets[result["case_id"]]
        if result["case_id"] == "nlf_synthetic":
            assert lab.qualification(result, packet) == "review verification failed"
        else:
            assert lab.qualification(result, packet) == "Sourcing receipt is not bound to this frozen case"
        broken = deepcopy(result)
        del broken["agent_pipeline"]["sanitizer"]
        assert lab.qualification(broken, packet)
        broken = deepcopy(result)
        broken["packet_snapshot"]["positions"] = "edited after the decision"
        assert lab.qualification(broken, packet)


def test_missing_controls_and_packet_tampering_never_certify():
    packet = lab.select_cases(["boot_2017"])[-1]
    doc = {"results": [], "lab_manifest": {"cases": [
        {"packet": packet, "packet_sha256": lab.digest(packet)}]}}
    report = lab.summarize(doc)
    assert not report["controls_passed"]
    assert not report["historical_interpretation_allowed"]
    assert report["status"] == "incomplete"
    packet["positions"] = "tampered"
    assert lab.summarize(doc)["cases"][0]["exclusion_reason"] == "Frozen packet hash mismatch"


def test_write_once(tmp_path):
    path = tmp_path / "run.json"
    lab.save_new(path, {"first": True})
    with pytest.raises(FileExistsError):
        lab.save_new(path, {"overwrite": True})
    assert json.loads(path.read_text()) == {"first": True}


def test_fixed_horizons_never_use_freeze_close_or_future_horizon_prices():
    freeze = date(2022, 1, 3)
    subject = {freeze: 1, date(2022, 1, 4): 100, date(2022, 4, 1): 50,
               date(2022, 7, 1): 120, date(2022, 7, 4): 999,
               date(2023, 1, 3): 150, date(2024, 1, 3): 200}
    benchmark = {d: 100 for d in subject}
    rows = compare_horizons(freeze=freeze, subject=subject, benchmark=benchmark, as_of=date(2024, 1, 3))
    assert rows[0]["entry_date"] == "2022-01-04"
    assert rows[0]["exit_date"] == "2022-07-01"
    assert rows[0]["subject_return_pct"] == pytest.approx(20)
    assert rows[0]["subject_max_close_drawdown_pct"] == -50
    assert rows[0]["after_tax_return_pct"] is None
    assert [r["months"] for r in rows] == [6, 12, 24]
    assert rows[-1]["subject_return_pct"] == 100


def test_missing_delisted_prices_and_unmatured_horizons_stay_unknown():
    freeze = date(2022, 1, 3)
    subject = {date(2022, 1, 4): 100, date(2022, 2, 1): 1}
    benchmark = {**subject, date(2022, 7, 1): 100}
    rows = compare_horizons(freeze=freeze, subject=subject, benchmark=benchmark, as_of=date(2022, 8, 1))
    assert [r["status"] for r in rows] == ["missing_price_data", "not_due", "not_due"]
    assert all(r["subject_return_pct"] is None for r in rows)


def test_month_end_and_same_day_benchmark_alignment():
    assert add_months(date(2023, 8, 31), 6) == date(2024, 2, 29)
    subject = {date(2022, 1, 4): 50, date(2022, 1, 5): 100, date(2022, 7, 1): 200}
    benchmark = {date(2022, 1, 5): 100, date(2022, 7, 1): 110}
    rows = compare_horizons(freeze=date(2022, 1, 3), subject=subject,
                            benchmark=benchmark, as_of=date(2022, 7, 3))
    assert rows[0]["entry_date"] == "2022-01-05"
    assert rows[0]["excess_return_pp"] == pytest.approx(90)


def test_nan_or_nonpositive_prices_never_score():
    rows = compare_horizons(freeze=date(2022, 1, 3),
        subject={date(2022, 1, 4): 100, date(2022, 7, 1): float("nan")},
        benchmark={date(2022, 1, 4): 100, date(2022, 7, 1): 100}, as_of=date(2022, 7, 3))
    assert rows[0]["status"] == "missing_price_data"


def test_lab_ui_projection_checks_real_artifact_seal(tmp_path):
    import hashlib
    from argosy.services.replay_lab_summary import latest_lab_summary
    source = run_suite.RUNS_DIR / "2026-09-11-daily-control-pair.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["lab_manifest"] = {"cases": [
        {"packet": p, "packet_sha256": lab.digest(p)} for p in lab.select_cases([])]}
    target = tmp_path / "runs" / "lab" / "controls.json"
    target.parent.mkdir(parents=True)
    lab.save_new(target, document)
    lab.save_new(target.with_suffix(".lab.json"), {"run_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    summary = latest_lab_summary(tmp_path)
    assert summary["status"] == "incomplete"
    assert not summary["controls_passed"]  # Original winner review recorded violations.
    document["results"][0]["action"] = "sell"
    target.write_text(json.dumps(document), encoding="utf-8")
    assert latest_lab_summary(tmp_path)["status"] == "invalid"


def test_outcome_fetch_cannot_run_on_unsealed_or_tampered_decision(tmp_path):
    from evals.fleet_calibration.market_outcomes import build_outcomes
    target = tmp_path / "run.json"
    target.write_text("{}", encoding="utf-8")
    target.with_suffix(".lab.json").write_text('{"run_sha256":"not the hash"}', encoding="utf-8")
    with pytest.raises(ValueError, match="changed after"):
        build_outcomes(target, case_id="boot_2017", symbol="BOOT",
                       fetcher=lambda *a: pytest.fail("prices fetched before the seal was checked"))


def test_duplicate_sanitizer_checks_cannot_hide_failure():
    from evals.fleet_calibration.agent_pipeline import verify_sanitizer
    packet = lab.select_cases([])[0]
    checks = [{"check": key, "verdict": "pass", "evidence": "test"} for key in
              ("alias", "absolute_figure_rescaling", "relative_dates", "macro_event_genericization")]
    checks.insert(0, {"check": "alias", "verdict": "fail", "evidence": "leak"})
    receipt = {"stage": 2, "agent_role": "calibration_sanitizer", "output": {
        "safe_to_run": True, "checks": checks, "leaked_terms": [], "summary": "test"}}
    assert not verify_sanitizer(packet, receipt)["ok"]


def test_review_distinguishes_nonblocking_warnings_from_violations():
    from evals.fleet_calibration.agent_pipeline import verify_review
    receipt = {"stage": 4, "agent_role": "calibration_reviewer", "output": {
        "output_clean": True, "packet_fidelity": True, "workflow_correct": True,
        "reasoning_grounded_score": 3, "summary": "test", "warnings": ["style caveat"]}}
    assert verify_review(receipt)["ok"]
    receipt["output"]["violations"] = ["Unsupported actionable price trigger"]
    assert not verify_review(receipt)["ok"]


def test_contradictory_top_level_action_is_not_a_class_match():
    path = run_suite.RUNS_DIR / "2026-09-11-daily-control-pair.json"
    result = next(r for r in json.loads(path.read_text(encoding="utf-8"))["results"]
                  if r["case_id"] == "omk_synthetic")
    result["action"] = "buy"
    packet = lab.select_cases([])[1]
    assert lab.qualification(result, packet) == "Trader output mismatch: action"


def test_synthetic_identity_cannot_acquire_real_returns():
    from evals.fleet_calibration.market_outcomes import validate_identity
    with pytest.raises(ValueError, match="Fictional"):
        validate_identity({"synthetic": True, "real": "BOOT"}, "BOOT")
    with pytest.raises(ValueError, match="identity"):
        validate_identity({"synthetic": False, "real": "BOOT"}, "NVDA")
    with pytest.raises(ValueError, match="identity"):
        validate_identity({"synthetic": False, "real": "Boot Barn (BOOT) @ 2017; beats SPY"}, "SPY")
    validate_identity({"synthetic": False, "real": "Boot Barn (BOOT) @ 2017; beats SPY"}, "BOOT")


def test_case_bound_receipt_cannot_be_swapped_with_another_case():
    from evals.fleet_calibration.agent_pipeline import verify_classifier, classifier_input_digest, build_classifier_input
    packet = lab.select_cases([])[0]
    receipt = run_suite.load_classifier_receipt(packet)
    receipt.update(case_id=packet["case_id"], classifier_input_sha256=classifier_input_digest(build_classifier_input(packet)))
    assert verify_classifier(packet, receipt, require_binding=True)["ok"]
    changed = deepcopy(packet)
    changed["case_id"] = "another_winner"
    assert not verify_classifier(changed, receipt, require_binding=True)["ok"]


def test_control_protocol_rejects_nonfictional_or_reordered_pair():
    packets = lab.select_cases(["boot_2017"])
    assert lab.valid_control_protocol(packets)
    assert not lab.valid_control_protocol(packets[::-1])
    packets[0]["synthetic"] = False
    assert not lab.valid_control_protocol(packets)


def test_malformed_seal_is_visible_invalid_not_a_server_error(tmp_path):
    from argosy.services.replay_lab_summary import latest_lab_summary
    root = tmp_path / "runs" / "lab"
    root.mkdir(parents=True)
    (root / "bad.json").write_text("{}", encoding="utf-8")
    (root / "bad.lab.json").write_text("[]", encoding="utf-8")
    assert latest_lab_summary(tmp_path)["status"] == "invalid"


@pytest.mark.parametrize("field,value", [("output_clean", "false"),
    ("packet_fidelity", 1), ("workflow_correct", "true"),
    ("reasoning_grounded_score", "0"), ("reasoning_grounded_score", True)])
def test_review_never_coerces_evidence(field, value):
    from evals.fleet_calibration.agent_pipeline import verify_review
    output = dict(output_clean=True, packet_fidelity=True, workflow_correct=True,
                  reasoning_grounded_score=3, summary="test")
    output[field] = value
    assert not verify_review(dict(stage=4, agent_role="calibration_reviewer", output=output))["ok"]


@pytest.mark.parametrize("value", [True, "1.0", float("nan"), float("inf")])
def test_grader_returns_are_finite_numbers_not_coerced(value):
    from pydantic import ValidationError
    from evals.fleet_calibration.agent_pipeline import CalibrationGradingOutput
    with pytest.raises(ValidationError):
        CalibrationGradingOutput(in_expected_class=True, score=1.0,
                                 acted_return_pct=value, rationale="test")


def test_control_contents_cannot_be_weakened_with_a_new_manifest_hash():
    packets = lab.select_cases([])
    packets[0]["positions"] = "Changed control portfolio"
    assert not lab.valid_control_protocol(packets)


def test_bare_legacy_identity_is_not_a_symbol_binding():
    from evals.fleet_calibration.market_outcomes import validate_identity
    with pytest.raises(ValueError, match="identity"):
        validate_identity({"synthetic": False, "real": "BOOT"}, "BOOT")


def test_malformed_source_facts_fail_audit_without_crashing():
    packet = lab.select_cases(["boot_2017"])[-1]
    packet["sources"] = [{"fact": {"bad": "shape"}}]
    assert run_suite.temporal_audit(packet)
    from evals.fleet_calibration.agent_pipeline import verify_classifier
    receipt = run_suite.load_classifier_receipt(packet)
    receipt["output"]["sourced_facts"] = [None]
    assert not verify_classifier(packet, receipt)["ok"]
