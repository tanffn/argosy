import json

from evals.fleet_calibration import discovery_lab as lab
from evals.fleet_calibration.lab import digest


def test_discovery_partition_is_company_disjoint_and_pinned():
    manifest, dev = lab.load_cases(lab.HERE / "discovery_cases_v1.json", "development")
    _, reserved = lab.load_cases(lab.HERE / "discovery_cases_v1.json", "reserved_evaluation")
    dev_names = {p["market_symbol"] for e, p, _ in dev if e["partition"] != "control"}
    reserved_names = {p["market_symbol"] for e, p, _ in reserved if e["partition"] != "control"}
    assert not dev_names & reserved_names
    for entry, packet, _ in [*dev, *reserved]:
        assert digest(packet) == entry["packet_sha256"]
        assert not packet["positioned"]
    assert manifest["protocol"] == lab.PROTOCOL


def test_real_production_prompts_see_only_masked_frozen_evidence():
    _, cases = lab.load_cases(lab.HERE / "discovery_cases_v1.json", "development")
    for entry, packet, _ in cases:
        screen, grader, screen_inputs, grade_inputs, prompts = lab.agents_and_inputs(entry, packet)
        rendered = json.dumps(prompts)
        assert "expected_classes" not in rendered
        assert "packet_sha256" not in rendered
        assert "resolution" not in rendered
        assert "market_symbol" not in rendered
        assert ("FICTIONAL BENCHMARK CONTROL" if packet["synthetic"] else "FROZEN HISTORICAL REPLAY") in prompts["screen"]["user"]
        assert screen.claude_code_force_toolless and grader.claude_code_force_toolless
        assert "analyst_reports" in grade_inputs
        assert screen_inputs["candidate"]["ticker"] == packet["alias"]


def test_all_source_transformations_rederive_exactly():
    manifest = json.loads((lab.HERE / "discovery_cases_v1.json").read_text(encoding="utf-8"))
    for entry in manifest["cases"]:
        if entry["partition"] == "control":
            continue
        p = json.loads((lab.HERE / "packets" / f"{entry['case_id']}.json").read_text(encoding="utf-8"))
        for m in p["sources"][0]["source_to_masked_mapping"]:
            assert abs(m["raw_value"] * p["rescale_factor"] - m["masked_value"]) < 1e-7
            assert m["publication_date"] <= p["freeze_date"]
            assert m["reporting_period"]


def test_incomplete_controls_do_not_qualify():
    s = lab.summary({"results": [{"entry": {"case_id": "x", "partition": "control", "expected_screen_go": True,
                                            "expected_grade": ["BUY"]}, "status": "preflight"}]})
    assert not s["controls_passed"]
    assert s["status"] == "incomplete"


def test_explicit_integrity_failures_never_pass_with_an_empty_violation_list():
    from evals.fleet_calibration.agent_pipeline import verify_review
    output = {"output_clean": True, "packet_fidelity": True, "workflow_correct": True,
              "reasoning_grounded_score": 3, "grounding_evidence": [], "violations": [],
              "warnings": [], "summary": "Review", "confidence": "MEDIUM"}
    for flag in ("output_clean", "packet_fidelity", "workflow_correct"):
        receipt = {"stage": 4, "agent_role": "calibration_reviewer", "output": {**output, flag: False}}
        assert not verify_review(receipt)["ok"]
    assert verify_review({"stage": 4, "agent_role": "calibration_reviewer", "output": output})["ok"]


def test_business_lead_boundary_changes_no_packets_labels_or_market_prices():
    old, old_cases = lab.load_cases(lab.HERE / "discovery_cases_v1.json", "development")
    new, new_cases = lab.load_cases(lab.HERE / "discovery_cases_v2.json", "development")
    for (a, p, _), (b, q, _) in zip(old_cases, new_cases, strict=True):
        assert p == q
        assert a["expected_grade"] == b["expected_grade"]
        assert a["expected_screen_go"] == b["expected_screen_go"]
        assert b["radar_candidate"]["reasons"] == [p["analyst_reports"][1]["business_description"]]
        _, _, inputs, _, prompts = lab.agents_and_inputs(b, q)
        assert q["analyst_reports"][1]["business_description"] in prompts["screen"]["user"]
        assert "annual_income_statement" not in str(inputs)
        assert "expected_grade" not in str(inputs)
    assert lab.price_input_receipt(old)["cases"] == lab.price_input_receipt(new)["cases"]


def test_both_pinned_manifests_qualify_controls_and_keep_integrity_rejection(monkeypatch):
    monkeypatch.setattr(lab, "qualification", lambda row: row["integrity_ok"])
    for version in (1, 2):
        manifest, cases = lab.load_cases(lab.HERE / f"discovery_cases_v{version}.json", "development")
        rows = [{"entry": entry, "status": "reviewed", "integrity_ok": True,
                 "screen": {"output": {"go": entry["expected_screen_go"]}},
                 "grader": {"output": {"verdict": entry["expected_grade"][0]}}} for entry, _, _ in cases]
        document = {"manifest": manifest, "evaluation_partition": "development", "results": rows[:1]}
        assert lab.summary(document)["cases"][0]["qualified"]  # Does not stop after first control.
        document["results"] = rows
        assert lab.summary(document)["status"] == "qualified"
        assert lab.summary(document)["controls_passed"]
        rows[1]["integrity_ok"] = False
        assert not lab.summary(document)["controls_passed"]
        assert lab.summary(document)["status"] != "qualified"


def test_screen_parsing_accepts_fences_but_raw_format_review_is_separate():
    from argosy.agents.quick_estimator import QuickEstimatorAgent

    agent = QuickEstimatorAgent(user_id="test")
    bare = json.dumps({"ticker": "TEST", "go": True, "conviction": "LOW", "sentiment": 0,
                       "one_line": "Concrete lead with material unknowns; investigate"})
    assert agent._parse_output(bare) == agent._parse_output(f"```json\n{bare}\n```")
