"""Independent agent stages for the fleet-calibration benchmark.

Stage 1 is the frozen packet's classifier/data-sourcing receipt. Stage 2
sanitizes the exact payload before the production Trader (stage 3) sees it.
Stages 4 and 5 independently review and grade the replay reloaded from disk.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from argosy.agents.base import BaseAgent, ConfidenceBand

OutputT = TypeVar("OutputT", bound=BaseModel)


class SourcedFact(BaseModel):
    fact: str
    url: str
    publication_date: str


class CalibrationClassificationOutput(BaseModel):
    category: Literal["A", "B", "C", "D", "synthetic"]
    grading: Literal[
        "F1_lenient",
        "F2_strict",
        "trap",
        "exit",
        "hold_drawdown",
        "synthetic_winner",
        "synthetic_trap",
        "entry",
        "rederive",
    ]
    freeze_date: str | None
    classification_rationale: str
    sourced_facts: list[SourcedFact]
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class ProtocolCheck(BaseModel):
    check: Literal[
        "alias",
        "absolute_figure_rescaling",
        "relative_dates",
        "macro_event_genericization",
    ]
    verdict: Literal["pass", "fail", "unverifiable", "not_applicable"]
    evidence: str


class CalibrationSanitizerOutput(BaseModel):
    safe_to_run: bool
    checks: list[ProtocolCheck]
    leaked_terms: list[str] = Field(default_factory=list)
    summary: str
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class CalibrationReviewOutput(BaseModel):
    output_clean: bool
    packet_fidelity: bool
    workflow_correct: bool
    reasoning_grounded_score: int = Field(ge=0, le=4)
    grounding_evidence: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    summary: str
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class CalibrationGradingOutput(BaseModel):
    in_expected_class: bool
    score: float = Field(ge=0.0, le=1.0)
    acted_return_pct: float | None = None
    benchmark_return_pct: float | None = None
    rationale: str
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


def build_classifier_input(packet: dict[str, Any]) -> dict[str, Any]:
    """Expose construction evidence without the expected verdict or outcome."""
    subject = str(packet.get("real") or packet["case_id"]).split("@", 1)[0].strip()
    brief: dict[str, Any] = {
        "case_id": packet["case_id"],
        "subject": subject,
        "freeze_date": packet.get("freeze_date"),
        "source_manifest": packet.get("sources") or [],
        "candidate_question": (
            "Classify this freeze point under the benchmark's A/B/C/D/synthetic "
            "taxonomy and select its grading mode using only pre-freeze evidence."
        ),
    }
    # Reconstruction burns for already-frozen packets: lock taxonomy so the
    # agent sources facts for the frozen classification rather than
    # re-litigating hard foresight entries that look trap-like at the print.
    if packet.get("category") is not None and packet.get("grading") is not None:
        brief["frozen_taxonomy"] = {
            "category": packet.get("category"),
            "grading": packet.get("grading"),
            "freeze_date": packet.get("freeze_date"),
        }
        brief["candidate_question"] = (
            "This packet is already frozen. Emit frozen_taxonomy exactly "
            "(category/grading/freeze_date) and gather period-accurate "
            "sourced_facts that support that freeze. Do not re-select taxonomy."
        )
    return brief


def independent_source_manifest(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Build stage-2's rescale/temporal anchor from the frozen packet only.

    Deliberately ignores classifier sourced_facts so a stage-1 error cannot
    supply the proof stage 2 uses for absolute_figure_rescaling.
    """
    manifest: list[dict[str, Any]] = []
    for entry in packet.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        publication = entry.get("publication_date") or entry.get("date")
        fact = entry.get("fact")
        url = entry.get("url")
        if fact is None and url is None and publication is None:
            continue
        row: dict[str, Any] = {
            "fact": fact or "",
            "url": url or "",
            "publication_date": publication or "",
        }
        # Preserve any extra fixture fields (e.g. unscaled figures) as raw
        # evidence without inventing structure the packet did not carry.
        for key, value in entry.items():
            if key in {"fact", "url", "date", "publication_date"}:
                continue
            row[key] = value
        manifest.append(row)
    return manifest


def build_sanitizer_input(
    packet: dict[str, Any], constraints_rendered: str
) -> dict[str, Any]:
    """Return only what an independent sanitizer needs before the fleet test."""
    # Decorrelated from stage 1: never read classifier sourced_facts here.
    raw_sources = list(packet.get("sources") or [])
    return {
        "case_id": packet["case_id"],
        "alias": packet["alias"],
        "rescale_factor": packet.get("rescale_factor"),
        "synthetic": bool(packet.get("synthetic")),
        "candidate_payload": {
            "alias": packet["alias"],
            "analyst_reports": packet["analyst_reports"],
            "positions": packet["positions"],
            "constraints_rendered": constraints_rendered,
        },
        "forbidden_terms": packet.get("contamination_terms") or [],
        "raw_sources": raw_sources,
        "source_manifest": independent_source_manifest(packet),
    }


def build_review_input(
    packet: dict[str, Any], replay: dict[str, Any]
) -> dict[str, Any]:
    """Build a stage-4 input with no outcome/expected-class answer key."""
    pipeline = replay.get("agent_pipeline") or {}
    snapshot = replay.get("packet_snapshot") or {}
    return {
        "case_id": packet["case_id"],
        "freeze_date": snapshot.get("freeze_date"),
        "sources": snapshot.get("sources") or [],
        "packet": {
            "alias": snapshot.get("alias"),
            "analyst_reports": snapshot.get("analyst_reports"),
            "positions": snapshot.get("positions"),
        },
        "replay": {
            "constraints_rendered": replay.get("constraints_rendered"),
            "response_raw": replay.get("response_raw"),
            "output_full": replay.get("output_full"),
            "output_audit": replay.get("output_audit"),
        },
        "sanitizer": pipeline.get("sanitizer"),
    }


def build_grader_input(
    packet: dict[str, Any], replay: dict[str, Any]
) -> dict[str, Any]:
    """Reveal the answer key only to the final grading stage."""
    pipeline = replay.get("agent_pipeline") or {}
    return {
        "case_id": packet["case_id"],
        "category": packet["category"],
        "grading": packet.get("grading"),
        "positioned": bool(packet.get("positioned")),
        "answer_key": {
            "expected_classes": packet.get("expected_classes") or [],
            "resolution": packet.get("resolution"),
        },
        "fleet_result": {
            "action": replay.get("action"),
            "confidence": replay.get("confidence"),
            "size": replay.get("size"),
            "size_units": replay.get("size_units"),
            "rationale_summary": replay.get("rationale_summary"),
        },
        "review": pipeline.get("review"),
    }


def verify_classifier(
    packet: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    output = receipt.get("output") or {}
    expected = {
        "category": packet.get("category"),
        "grading": packet.get("grading"),
        "freeze_date": packet.get("freeze_date"),
    }
    mismatches = [
        {"field": field, "expected": value, "actual": output.get(field)}
        for field, value in expected.items()
        if output.get(field) != value
    ]
    if receipt.get("stage") != 1:
        mismatches.append({
            "field": "stage", "expected": 1, "actual": receipt.get("stage")
        })
    if receipt.get("agent_role") != "calibration_classifier_sourcing":
        mismatches.append({
            "field": "agent_role",
            "expected": "calibration_classifier_sourcing",
            "actual": receipt.get("agent_role"),
        })
    try:
        CalibrationClassificationOutput.model_validate(output)
    except ValidationError as exc:
        mismatches.append({
            "field": "output_schema",
            "expected": "CalibrationClassificationOutput",
            "actual": str(exc),
        })
    if not packet.get("synthetic"):
        sourced_facts = output.get("sourced_facts") or []
        if not sourced_facts:
            mismatches.append({
                "field": "sourced_facts",
                "expected": "at least one pre-freeze sourced fact",
                "actual": sourced_facts,
            })
        freeze_raw = packet.get("freeze_date")
        try:
            freeze_date = date.fromisoformat(freeze_raw) if freeze_raw else None
        except (TypeError, ValueError):
            freeze_date = None
        for index, fact in enumerate(sourced_facts):
            publication_raw = fact.get("publication_date")
            try:
                publication_date = date.fromisoformat(publication_raw)
            except (TypeError, ValueError):
                publication_date = None
            if (
                publication_date is None
                or freeze_date is None
                or publication_date > freeze_date
            ):
                mismatches.append({
                    "field": f"sourced_facts[{index}].publication_date",
                    "expected": f"valid date on/before {freeze_raw}",
                    "actual": publication_raw,
                })
    return {"ok": not mismatches, "mismatches": mismatches}


def verify_sanitizer(
    packet: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    output = receipt.get("output") or {}
    expected_checks = {
        "alias",
        "absolute_figure_rescaling",
        "relative_dates",
        "macro_event_genericization",
    }
    verdicts = {
        check.get("check"): check.get("verdict")
        for check in output.get("checks") or []
    }
    accepted = {"pass", "not_applicable"} if packet.get("synthetic") else {"pass"}
    failures = []
    if receipt.get("stage") != 2:
        failures.append({
            "check": "stage", "expected": 2, "verdict": receipt.get("stage")
        })
    if receipt.get("agent_role") != "calibration_sanitizer":
        failures.append({
            "check": "agent_role",
            "expected": "calibration_sanitizer",
            "verdict": receipt.get("agent_role"),
        })
    try:
        CalibrationSanitizerOutput.model_validate(output)
    except ValidationError as exc:
        failures.append({
            "check": "output_schema",
            "expected": "CalibrationSanitizerOutput",
            "verdict": str(exc),
        })
    for check in sorted(expected_checks):
        verdict = verdicts.get(check, "missing")
        if verdict not in accepted:
            failures.append({"check": check, "verdict": verdict})
    if output.get("leaked_terms"):
        failures.append({
            "check": "leaked_terms",
            "verdict": list(output["leaked_terms"]),
        })
    if output.get("safe_to_run") is not True:
        failures.append({"check": "safe_to_run", "verdict": False})
    return {"ok": not failures, "failures": failures}


def verify_review(receipt: dict[str, Any]) -> dict[str, Any]:
    failures = []
    if receipt.get("stage") != 4:
        failures.append({
            "check": "stage", "expected": 4, "actual": receipt.get("stage")
        })
    if receipt.get("agent_role") != "calibration_reviewer":
        failures.append({
            "check": "agent_role",
            "expected": "calibration_reviewer",
            "actual": receipt.get("agent_role"),
        })
    try:
        CalibrationReviewOutput.model_validate(receipt.get("output") or {})
    except ValidationError as exc:
        failures.append({
            "check": "output_schema",
            "expected": "CalibrationReviewOutput",
            "actual": str(exc),
        })
    return {"ok": not failures, "failures": failures}


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


class _CalibrationAgent(BaseAgent[OutputT], Generic[OutputT]):
    require_citations = False
    use_structured_output = True
    schema_retry_attempts = 2
    max_tokens = 6000

    def __init__(self, *, user_id: str = "ariel", model: str | None = None) -> None:
        super().__init__(user_id=user_id, model=model or "claude-opus-4-8")


class CalibrationClassifierSourcingAgent(
    _CalibrationAgent[CalibrationClassificationOutput]
):
    agent_role = "calibration_classifier_sourcing"
    output_model = CalibrationClassificationOutput
    claude_code_allowed_tools = ("WebSearch", "WebFetch")

    def build_prompt(self, *, case_brief: dict[str, Any]) -> tuple[str, str]:
        system = (
            "You are the classifier and period-accurate data-sourcing agent for "
            "a historical time-machine benchmark. Gather every load-bearing "
            "fact from a primary source where available, recording the source "
            "URL and its publication date. No source may postdate the freeze "
            "date. Do not use retrospective 'why it moved' articles, do not "
            "sanitize the packet, and do not use the eventual outcome as "
            "evidence.\n"
            "Taxonomy: A=entry (should BUY; includes hard foresight / "
            "trap-shaped winners where the contemporaneous print looks soft), "
            "B=trap (should PASS / no position), C=exit (positioned, should "
            "SELL on fired falsifiers), D=hold-through-drawdown, "
            "synthetic=fictional control. Grading modes pair with those "
            "buckets (entry/F1_lenient/F2_strict; trap; exit; "
            "hold_drawdown/rederive; synthetic_winner/synthetic_trap).\n"
            "When the brief includes frozen_taxonomy, this is a reconstruction "
            "burn: emit category/grading/freeze_date EXACTLY as "
            "frozen_taxonomy states and focus on sourced_facts. Do not "
            "re-litigate the taxonomy. When frozen_taxonomy is absent, select "
            "category, grading, and freeze date from pre-freeze evidence only."
        )
        return system, "CASE CONSTRUCTION BRIEF:\n" + _render(case_brief)


class CalibrationSanitizerAgent(
    _CalibrationAgent[CalibrationSanitizerOutput]
):
    agent_role = "calibration_sanitizer"
    output_model = CalibrationSanitizerOutput

    def build_prompt(self, *, payload: dict[str, Any]) -> tuple[str, str]:
        system = (
            "You are the independent sanitizer in a historical time-machine "
            "benchmark. Audit the exact candidate payload before the production "
            "Trader sees it. Check all four protocol dimensions: aliasing, "
            "absolute-figure rescaling, relative dates, and genericized macro "
            "events. Treat forbidden_terms as an answer-key denylist, never as "
            "facts to add. A concrete leak makes safe_to_run=false. Prove "
            "absolute-figure rescaling only from raw_sources / source_manifest "
            "(independently constructed from the frozen packet — never from a "
            "classifier agent's facts). If scaling cannot be independently "
            "proven from that manifest and the masked payload, mark that check "
            "unverifiable rather than inventing proof. You have no tools and "
            "may not fetch or search. For a fully fictional synthetic packet, "
            "mark inapplicable checks not_applicable. Do not identify the "
            "company in your output or infer the eventual outcome."
        )
        return system, "SANITIZER INPUT:\n" + _render(payload)


class CalibrationReviewAgent(_CalibrationAgent[CalibrationReviewOutput]):
    agent_role = "calibration_reviewer"
    output_model = CalibrationReviewOutput

    def build_prompt(self, *, payload: dict[str, Any]) -> tuple[str, str]:
        system = (
            "You are the independent replay reviewer in a historical benchmark. "
            "Audit only the persisted packet and replay supplied below. Verify "
            "output cleanliness, exact packet/constraint fidelity, workflow "
            "correctness, and whether the VISIBLE rationale is grounded step by "
            "step in packet facts. Never claim access to hidden chain-of-thought; "
            "reasoning_grounded_score grades only the written rationale: 0=no "
            "grounding, 1=mostly unsupported, 2=mixed, 3=grounded with minor gaps, "
            "4=fully grounded. You do not know the expected verdict or outcome "
            "and must not grade investment correctness."
        )
        return system, "PERSISTED REPLAY INPUT:\n" + _render(payload)


class CalibrationGradingAgent(_CalibrationAgent[CalibrationGradingOutput]):
    agent_role = "calibration_grader"
    output_model = CalibrationGradingOutput

    def build_prompt(self, *, payload: dict[str, Any]) -> tuple[str, str]:
        system = (
            "You are the final grading agent for a historical benchmark. The "
            "fleet test and independent replay review are complete. Score the "
            "verdict against expected_classes and the recorded grading rule. "
            "Use 1.0 for an in-class verdict. Apply 0.5 only where the benchmark "
            "rule explicitly warrants partial credit (for example, an F1 pass "
            "with falsifiers or a small trap buy with the killing falsifier); "
            "otherwise use 0.0. Compute acted-on return from the supplied "
            "resolution and action. Outcome luck never changes the class grade. "
            "Do not reopen the review agent's cleanliness/workflow judgment."
        )
        return system, "GRADING INPUT:\n" + _render(payload)


def _receipt(stage: int, role: str, report: Any) -> dict[str, Any]:
    return {
        "stage": stage,
        "agent_role": role,
        "model": report.model,
        "tokens_in": report.tokens_in,
        "tokens_out": report.tokens_out,
        "response_raw": report.response_text,
        "output": report.output.model_dump(mode="json"),
    }


async def run_sanitizer(
    packet: dict[str, Any],
    constraints_rendered: str,
    *,
    agent: CalibrationSanitizerAgent | None = None,
) -> dict[str, Any]:
    selected = agent or CalibrationSanitizerAgent()
    report = await selected.run(
        payload=build_sanitizer_input(packet, constraints_rendered)
    )
    return _receipt(2, selected.agent_role, report)


async def run_classifier_sourcing(
    case_brief: dict[str, Any],
    *,
    agent: CalibrationClassifierSourcingAgent | None = None,
) -> dict[str, Any]:
    selected = agent or CalibrationClassifierSourcingAgent()
    report = await selected.run(case_brief=case_brief)
    return _receipt(1, selected.agent_role, report)


async def run_review(
    packet: dict[str, Any],
    replay: dict[str, Any],
    *,
    agent: CalibrationReviewAgent | None = None,
) -> dict[str, Any]:
    selected = agent or CalibrationReviewAgent()
    report = await selected.run(payload=build_review_input(packet, replay))
    return _receipt(4, selected.agent_role, report)


async def run_grading(
    packet: dict[str, Any],
    replay: dict[str, Any],
    *,
    agent: CalibrationGradingAgent | None = None,
) -> dict[str, Any]:
    selected = agent or CalibrationGradingAgent()
    report = await selected.run(payload=build_grader_input(packet, replay))
    return _receipt(5, selected.agent_role, report)


def verify_grading(
    packet: dict[str, Any],
    replay: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Verify the grader's mechanical class/return claims."""
    output = receipt.get("output") or {}
    action = replay.get("action")
    expected_in_class = action in (packet.get("expected_classes") or [])
    resolution = packet.get("resolution") or {}
    expected_benchmark = resolution.get("benchmark_return_pct")
    long_actions = {"buy", "hold"} if packet.get("positioned") else {"buy"}
    expected_acted = (
        expected_benchmark if expected_benchmark is not None and action in long_actions
        else 0.0 if expected_benchmark is not None
        else None
    )

    mismatches: list[dict[str, Any]] = []
    if receipt.get("stage") != 5:
        mismatches.append({
            "field": "stage", "expected": 5, "actual": receipt.get("stage")
        })
    if receipt.get("agent_role") != "calibration_grader":
        mismatches.append({
            "field": "agent_role",
            "expected": "calibration_grader",
            "actual": receipt.get("agent_role"),
        })
    try:
        CalibrationGradingOutput.model_validate(output)
    except ValidationError as exc:
        mismatches.append({
            "field": "output_schema",
            "expected": "CalibrationGradingOutput",
            "actual": str(exc),
        })

    def compare(field: str, expected: Any) -> None:
        actual = output.get(field)
        if isinstance(expected, float) and isinstance(actual, (int, float)):
            matches = abs(float(actual) - expected) <= 1e-6
        else:
            matches = actual == expected
        if not matches:
            mismatches.append(
                {"field": field, "expected": expected, "actual": actual}
            )

    compare("in_expected_class", expected_in_class)
    compare("acted_return_pct", expected_acted)
    compare("benchmark_return_pct", expected_benchmark)
    actual_score = output.get("score")
    partial_credit_allowed = (
        packet.get("grading") == "F1_lenient" and action == "hold"
    ) or (
        packet.get("grading") == "trap" and action == "buy"
    )
    allowed_scores = (
        {1.0} if expected_in_class
        else {0.0, 0.5} if partial_credit_allowed
        else {0.0}
    )
    score_ok = actual_score in allowed_scores
    if not score_ok:
        mismatches.append({
            "field": "score",
            "expected": sorted(allowed_scores),
            "actual": actual_score,
        })
    return {
        "ok": not mismatches,
        "expected_in_class": expected_in_class,
        "expected_acted_return_pct": expected_acted,
        "expected_benchmark_return_pct": expected_benchmark,
        "mismatches": mismatches,
    }


async def run_replay_pipeline(
    packet: dict[str, Any],
    *,
    load_replay: Callable[[], dict[str, Any]],
    persist_stage: Callable[[str, dict[str, Any]], None],
    review_runner: Callable[
        [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]
    ] = run_review,
    grading_runner: Callable[
        [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]
    ] = run_grading,
) -> None:
    """Run stages 4-5, reloading the durable replay before each agent."""
    replay_for_review = load_replay()
    review_receipt = await review_runner(packet, replay_for_review)
    review_receipt["verification"] = verify_review(review_receipt)
    persist_stage("review", review_receipt)

    replay_for_grading = load_replay()
    grading_receipt = await grading_runner(packet, replay_for_grading)
    grading_receipt["verification"] = verify_grading(
        packet, replay_for_grading, grading_receipt
    )
    persist_stage("grading", grading_receipt)


__all__ = [
    "CalibrationClassificationOutput",
    "CalibrationClassifierSourcingAgent",
    "CalibrationGradingOutput",
    "CalibrationGradingAgent",
    "CalibrationReviewAgent",
    "CalibrationReviewOutput",
    "CalibrationSanitizerAgent",
    "CalibrationSanitizerOutput",
    "ProtocolCheck",
    "SourcedFact",
    "build_classifier_input",
    "build_grader_input",
    "build_review_input",
    "build_sanitizer_input",
    "independent_source_manifest",
    "run_classifier_sourcing",
    "run_grading",
    "run_replay_pipeline",
    "run_review",
    "run_sanitizer",
    "verify_classifier",
    "verify_grading",
    "verify_review",
    "verify_sanitizer",
]
