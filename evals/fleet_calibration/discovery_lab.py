"""Versioned, tool-free replay of production discovery screen + synthesis.

This does NOT replay market-universe sourcing, live analyst retrieval or trades.
No production DB writes. Outcomes are fetched only after independent review/seal.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from argosy.agents.quick_estimator import QuickEstimatorAgent
from argosy.services.discovery_grader import DiscoveryGraderAgent
from evals.fleet_calibration import run_suite
from evals.fleet_calibration.agent_pipeline import (
    CalibrationReviewAgent,
    CalibrationSanitizerAgent,
    independent_source_manifest,
    verify_classifier,
    verify_review,
    verify_sanitizer,
)
from evals.fleet_calibration.lab import digest, save_new

HERE = Path(__file__).resolve().parent
PROTOCOL = "discovery_screen_grader_v1"
MANIFEST_SHA256 = "84e18b717c16fe8d6f528b3713a12f2fa56a02737fdb6998d9e7d311670c21bd"
BUSINESS_LEAD_MANIFEST_SHA256 = "6f5c1e154670e1b0e9ae9cc4a63d443c0893bd06a6580aecf03135b384b89574"
PINNED_MANIFESTS = (MANIFEST_SHA256, BUSINESS_LEAD_MANIFEST_SHA256)
TEMPORAL_NOTE = (
    "FROZEN HISTORICAL REPLAY, initiation research only. T0 is the supplied snapshot. "
    "The company and ticker are aliases; dollar amounts are consistently rescaled. "
    "Do not identify the issuer, use model memory/current world events, or infer a calendar year. "
    "Use supplied facts only, relative future windows and scenario assumptions; unknown remains unknown. "
    "A masked name is intentional, not a reason to reject the business."
)
SYNTHETIC_TEMPORAL_NOTE = (
    "FICTIONAL BENCHMARK CONTROL at T0, initiation research only. This company, its ticker, "
    "financials and events are invented; there is no real issuer, original figure set or primary "
    "filing to discover. The reports are stipulated fictional facts, independently checked for "
    "internal consistency. Do not penalize the intentional absence of real filings, live feeds "
    "or calendar dates. Economic facts not supplied remain unknown. Evaluate the business and "
    "valuation as supplied, without identifying any real company or using current-world memory. "
    "Use relative windows only. This does not imply that a BUY is expected."
)


def load_cases(manifest_path: Path, partition: str):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol") != PROTOCOL or digest(manifest) not in PINNED_MANIFESTS:
        raise ValueError("Unsupported discovery replay protocol")
    entries = [e for e in manifest["cases"] if e["partition"] in ("control", partition)]
    if [e["case_id"] for e in entries[:2]] != manifest["control_ids"] or len(entries) < 3:
        raise ValueError("Two pinned discovery controls and at least one historical case required")
    loaded = []
    for entry in entries:
        packet = json.loads((HERE / "packets" / f"{entry['case_id']}.json").read_text(encoding="utf-8"))
        if digest(packet) != entry["packet_sha256"] or packet.get("positioned"):
            raise ValueError("Changed or owned-position packet; discovery replay is initiation-only")
        if manifest.get("screen_contract_version") == 2:
            if entry["radar_candidate"]["reasons"] != [packet["analyst_reports"][1]["business_description"]]:
                raise ValueError("Screen lead must exactly match its frozen business-description source")
        if run_suite.temporal_audit(packet) or run_suite.packet_year_audit(packet):
            raise ValueError(f"Invalid temporal masking: {entry['case_id']}")
        receipt = run_suite.load_classifier_receipt(packet)
        loaded.append((entry, packet, receipt))
    return manifest, loaded


def price_input_receipt(manifest):
    version = 2 if manifest.get("screen_contract_version") == 2 else 1
    receipt = json.loads((HERE / f"discovery_price_inputs_v{version}.json").read_text(encoding="utf-8"))
    if receipt.get("manifest_sha256") != digest(manifest):
        raise ValueError("Input-price receipt is not bound to this frozen manifest")
    for entry in manifest["cases"]:
        if entry["partition"] == "control":
            continue
        matches = [r for r in receipt["cases"] if r["case_id"] == entry["case_id"]]
        if len(matches) != 1 or matches[0]["packet_sha256"] != entry["packet_sha256"]:
            raise ValueError("Missing or mismatched frozen price evidence")
        row = matches[0]
        packet = json.loads((HERE / "packets" / f"{entry['case_id']}.json").read_text(encoding="utf-8"))
        supplied = packet["sources"][1]["market_bar_receipt"]["close"]
        if row["freeze_date"] != packet["freeze_date"] or abs(supplied - row["reconstructed_original_close"]) > .005:
            raise ValueError("Frozen nominal input price disagrees with captured provider evidence")
    return receipt


def agents_and_inputs(entry, packet):
    screen = QuickEstimatorAgent(user_id="ariel")
    grader = DiscoveryGraderAgent(user_id="ariel")
    for agent in (screen, grader):
        agent.claude_code_force_toolless = True
        agent.claude_code_force_isolated = True
    screen_inputs = {"candidate": entry["radar_candidate"]}
    reports = [SimpleNamespace(agent_role=r["agent_role"], response_text=json.dumps(r, ensure_ascii=False))
               for r in packet["analyst_reports"]]
    note = SYNTHETIC_TEMPORAL_NOTE if packet["synthetic"] else TEMPORAL_NOTE + (
        " Quantitative facts were independently sourced to pre-freeze issuer filings; original "
        "identities/URLs are intentionally withheld from the judge but retained for the source auditor. "
        "Treat source age relative to T0, not to the real current calendar. The nominal quote is from "
        "the close at T0, not a live executable fill. This provenance boundary is not missing evidence."
    )
    grade_inputs = {"ticker": packet["alias"], "analyst_reports": reports,
                    "research_question": note,
                    "evidence_limitations": {"replay": "Frozen approved reports, not newly researched analyst calls",
                                              "missing_live_feeds": "No live quotes, news, macro or sentiment may be fetched"}}
    def rendered(agent, inputs):
        system, user = agent.build_prompt(**inputs)
        return {"system": agent.BOILERPLATE_SYSTEM + "\n\n" + system, "user": user}
    # Temporal restrictions are a test boundary, not a modified investment lens.
    # The screen receives them through an explicit candidate reason.
    candidate = dict(screen_inputs["candidate"])
    candidate["reasons"] = [*(candidate.get("reasons") or []), note]
    screen_inputs = {"candidate": candidate}
    prompts = {"screen": rendered(screen, screen_inputs), "grader": rendered(grader, grade_inputs)}
    return screen, grader, screen_inputs, grade_inputs, prompts


def receipt(stage, role, report):
    return {"stage": stage, "agent_role": role, "model": report.model,
            "tokens_in": report.tokens_in, "tokens_out": report.tokens_out,
            "cost_usd": float(report.cost_usd), "response_raw": report.response_text,
            "system_prompt": report.system_prompt, "user_prompt": report.user_prompt,
            "prompt_hash": report.prompt_hash, "output": report.output.model_dump(mode="json")}


def qualification(result):
    if result.get("status") != "reviewed":
        return False
    packet = result["frozen_packet"]
    if digest(packet) != result["entry"]["packet_sha256"]:
        return False
    if not verify_classifier(packet, result.get("classifier") or {}, require_binding=True)["ok"]:
        return False
    if not verify_sanitizer(packet, result.get("sanitizer") or {})["ok"]:
        return False
    if not verify_review(result.get("review") or {})["ok"]:
        return False
    for name in ("screen", "grader"):
        call = result.get(name)
        if name == "grader" and not result["screen"]["output"]["go"]:
            if call is not None:
                return False
            continue
        prompt = result["rendered_prompts"][name]
        if not call or call["system_prompt"] != prompt["system"] or call["user_prompt"] != prompt["user"]:
            return False
        if call["output"].get("ticker") != packet["alias"]:
            return False
    return True


def summary(document):
    manifest = document.get("manifest") or {}
    manifest_ok = digest(manifest) in PINNED_MANIFESTS
    expected_entries = [e for e in manifest.get("cases", [])
                        if e["partition"] in ("control", document.get("evaluation_partition"))]
    actual_ids = [r["entry"]["case_id"] for r in document["results"]]
    order_ok = actual_ids == [e["case_id"] for e in expected_entries[:len(actual_ids)]]
    by_id = {e["case_id"]: e for e in expected_entries}
    cases = []
    for row in document["results"]:
        qualified = (manifest_ok and order_ok and row["entry"] == by_id.get(row["entry"]["case_id"])
                     and qualification(row))
        go = (row.get("screen") or {}).get("output", {}).get("go")
        verdict = (row.get("grader") or {}).get("output", {}).get("verdict")
        expected = row["entry"]
        agrees = go == expected["expected_screen_go"] and verdict in expected["expected_grade"]
        cases.append({"case_id": expected["case_id"], "partition": expected["partition"],
                      "qualified": qualified, "status": row["status"], "screen_go": go,
                      "verdict": verdict, "expected_class_agreement": agrees,
                      "research_verdict_not_order": True})
    controls = [r for r in cases if r["partition"] == "control"]
    passed = len(controls) == 2 and all(r["qualified"] and r["expected_class_agreement"] for r in controls)
    return {"protocol": PROTOCOL, "controls_passed": passed, "cases": cases,
            "status": "qualified" if passed and len(cases) == len(expected_entries)
            and all(r["qualified"] for r in cases) else "incomplete"}


async def run(manifest, cases, out):
    if out.exists() or out.with_suffix(".discovery.json").exists():
        raise ValueError("Discovery replay artifacts are write-once")
    input_prices = price_input_receipt(manifest)
    doc = {"protocol": PROTOCOL, "manifest": manifest, "manifest_sha256": digest(manifest),
           "harness_context_version": 2,
           "evaluation_partition": next(e[0]["partition"] for e in cases if e[0]["partition"] != "control"),
           "input_price_receipt": input_prices, "input_price_receipt_sha256": digest(input_prices),
           "started_at": datetime.now(UTC).isoformat(), "results": []}
    save_new(out, doc)
    def save():
        out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    for entry, packet, classifier in cases:
        row = {"entry": entry, "frozen_packet": packet, "classifier": classifier, "status": "preflight"}
        doc["results"].append(row)
        save()
        print(json.dumps({"case": entry["case_id"], "stage": "preflight"}), flush=True)
        try:
            if not classifier or not verify_classifier(packet, classifier, require_binding=True)["ok"]:
                raise ValueError("Missing or invalid case-bound sourcing receipt")
            screen, grader, screen_inputs, grade_inputs, prompts = agents_and_inputs(entry, packet)
            row["rendered_prompts"] = prompts
            row["frozen_screen_candidate"] = screen_inputs["candidate"]
            row["frozen_analyst_report_adapter"] = [vars(r) for r in grade_inputs["analyst_reports"]]
            sanitizer = CalibrationSanitizerAgent(user_id="ariel")
            sanitizer.claude_code_force_toolless = True
            sanitizer.claude_code_force_isolated = True
            payload = {"protocol": PROTOCOL, "alias": packet["alias"], "synthetic": packet["synthetic"],
                       "rescale_factor": packet.get("rescale_factor"), "candidate_payload": prompts,
                       "forbidden_terms": packet["contamination_terms"], "raw_sources": packet["sources"],
                       "source_manifest": independent_source_manifest(packet)}
            payload["market_bar_evidence"] = [r for r in input_prices["cases"] if r["case_id"] == entry["case_id"]]
            report = await sanitizer.run(payload=payload)
            row["sanitizer"] = receipt(2, sanitizer.agent_role, report)
            save()
            if not verify_sanitizer(packet, row["sanitizer"])["ok"]:
                row["status"] = "disqualified_sanitizer"
                save()
                if entry["partition"] == "control":
                    break
                continue
            print(json.dumps({"case": entry["case_id"], "stage": "production_screen"}), flush=True)
            report = await screen.run(**screen_inputs)
            row["screen"] = receipt(3, screen.agent_role, report)
            save()
            if report.output.go:
                report = await grader.run(**grade_inputs)
                row["grader"] = receipt(3, grader.agent_role, report)
                save()
            reviewer = CalibrationReviewAgent(user_id="ariel")
            reviewer.claude_code_force_toolless = True
            reviewer.claude_code_force_isolated = True
            review_payload = {"protocol": PROTOCOL, "freeze_date": packet.get("freeze_date"),
                              "sources": packet["sources"], "sanitizer": row["sanitizer"],
                              "packet": {"alias": packet["alias"], "analyst_reports": packet["analyst_reports"]},
                              "replay": {"exact_rendered_prompts": prompts, "screen": row["screen"], "grader": row.get("grader")},
                              "semantics": "Production quick screen then research-only discovery grade, not Trader/orders. No expected classes or outcomes are supplied. Screen false legitimately skips the grader. Audit ALL visible reasoning against frozen facts; do not demand execution fields."}
            report = await reviewer.run(payload=review_payload)
            row["review"] = receipt(4, reviewer.agent_role, report)
            row["status"] = "reviewed"
            save()
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            save()
        item = summary(doc)["cases"][-1]
        print(json.dumps(item), flush=True)
        if entry["partition"] == "control" and (not item["qualified"] or not item["expected_class_agreement"]):
            break
    doc["finished_at"] = datetime.now(UTC).isoformat()
    save()
    report = summary(doc)
    # A stopped control run is never qualified just because unattempted cases vanished.
    if len(doc["results"]) != len(cases):
        report["status"] = "incomplete"
        report["unattempted"] = [e[0]["case_id"] for e in cases[len(doc["results"]):]]
    report["run_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
    save_new(out.with_suffix(".discovery.json"), report)
    return report


def outcomes(run_path: Path, out: Path):
    from evals.fleet_calibration.market_outcomes import add_months, compare_horizons, fetch_history
    raw = run_path.read_bytes()
    seal = json.loads(run_path.with_suffix(".discovery.json").read_text(encoding="utf-8"))
    if seal["run_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("Discovery run changed after sealing")
    doc = json.loads(raw)
    qualified = summary(doc)
    if not qualified["controls_passed"]:
        raise ValueError("Discovery controls did not pass; no historical interpretation")
    as_of = datetime.now(UTC).date() - timedelta(days=1)
    results = []
    for row, status in zip(doc["results"], qualified["cases"], strict=True):
        if row["entry"]["partition"] == "control":
            continue
        result = {**status, "horizons": [], "price_evidence": {}}
        results.append(result)
        if not status["qualified"]:
            result["price_status"] = "not_fetched_disqualified"
            continue
        packet = row["frozen_packet"]
        freeze = date.fromisoformat(packet["freeze_date"])
        end = min(as_of, add_months(freeze, 24)) + timedelta(days=1)
        histories = {}
        for symbol in (packet["market_symbol"], "SPY"):
            try:
                histories[symbol] = fetch_history(symbol, freeze + timedelta(days=1), end)
            except Exception as exc:
                histories[symbol] = {}
                result.setdefault("errors", []).append(f"{symbol}: {type(exc).__name__}: {exc}")
        result["horizons"] = compare_horizons(freeze=freeze, subject=histories[packet["market_symbol"]],
                                               benchmark=histories["SPY"], as_of=as_of)
        result["price_evidence"] = {symbol: {str(d): p for d, p in bars.items()} for symbol, bars in histories.items()}
    report = {"protocol": PROTOCOL, "run_sha256": seal["run_sha256"], "as_of": str(as_of),
              "fetched_at": datetime.now(UTC).isoformat(), "cases": results,
              "basis": "If-bought gross adjusted-close returns vs matched SPY, not actual allocations or after-tax P&L",
              "limitations": doc["manifest"]["limitations"]}
    save_new(out, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "discovery_cases_v2.json")
    parser.add_argument("--partition", choices=("development", "reserved_evaluation"), default="development")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--outcomes", type=Path, help="Sealed discovery run to compare; no model calls")
    args = parser.parse_args()
    if args.outcomes:
        if args.out is None:
            parser.error("--out required")
        report = outcomes(args.outcomes, args.out)
        print(json.dumps({"cases": [{**r, "price_evidence": "retained in artifact"} for r in report["cases"]]}, indent=2))
        return 0
    manifest, cases = load_cases(args.manifest, args.partition)
    if args.audit_only:
        result = [{"case_id": e["case_id"], "source_receipt_valid": bool(r) and verify_classifier(p, r, require_binding=True)["ok"]}
                  for e, p, r in cases]
        print(json.dumps(result, indent=2))
        return int(not all(r["source_receipt_valid"] for r in result))
    if args.out is None:
        parser.error("--out required")
    report = asyncio.run(run(manifest, cases, args.out))
    print(json.dumps(report, indent=2))
    return int(report["status"] != "qualified")


if __name__ == "__main__":
    raise SystemExit(main())
