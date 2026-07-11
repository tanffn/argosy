# Fleet Calibration Five-Agent Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete item F's first hand-back by separating packet-construction provenance, sanitization, production Trader judgment, replay review, and grading into independently auditable stages.

**Architecture:** The classifier/data-sourcing agent runs at packet-construction time and writes an immutable sidecar receipt; suite scoring loads that receipt and never re-runs classification with later knowledge. The runner then invokes a tool-free independent sanitizer before Trader, persists the complete Trader replay, and makes reviewer and grader agents reload that persisted replay before acting. Deterministic code verifies temporal integrity, stage verdicts, contamination, class matching, and return arithmetic; agents own classification, sanitization, reasoning-integrity review, and grading judgment.

**Tech Stack:** Python 3.12, Pydantic, Argosy `BaseAgent`, asyncio, pytest.

## Global Constraints

- Work only on `feat/opens-2026-07-11`; never merge.
- Never modify existing scored files under `evals/fleet_calibration/runs/`.
- `--dry-run` makes no LLM calls and writes no run artifact.
- No real-LLM scoring in this block; coordinate with the resident reviewer first.
- Every live stage persists before the next stage reads, and writes finish before printing.
- Existing packets remain frozen; classifier receipts are separate immutable artifacts prepared only after reviewer coordination.

---

### Task 1: Stage contracts and blind prompt boundaries

**Files:**
- Create: `evals/fleet_calibration/agent_pipeline.py`
- Modify: `evals/fleet_calibration/test_calibration.py`

**Interfaces:**
- Produces: `build_classifier_receipt(packet) -> dict`
- Produces: `CalibrationSanitizerAgent`, `CalibrationReviewAgent`, `CalibrationGradingAgent`
- Produces: `run_sanitizer(packet, constraints)`, `run_review(packet, replay)`, `run_grading(packet, replay)`

- [ ] Write failing tests proving: the classifier receipt records category/freeze/sources; sanitizer sees exact Trader inputs plus forbidden terms but not `real`/resolution; reviewer sees persisted replay but not expected class/outcome; grader sees expected class/outcome only after review.
- [ ] Run the focused tests and confirm they fail because the pipeline module is absent.
- [ ] Implement Pydantic outputs and three independent `BaseAgent` subclasses using explicit Opus defaults, structured output, no tools, and schema retries.
- [ ] Run the focused tests and confirm green.

### Task 2: Persisted replay orchestration

**Files:**
- Modify: `evals/fleet_calibration/agent_pipeline.py`
- Modify: `evals/fleet_calibration/run_suite.py`
- Modify: `evals/fleet_calibration/test_calibration.py`

**Interfaces:**
- Produces: `run_replay_pipeline(packet, run_doc, out_path, result_index, ...)`
- Consumes: the existing Trader replay fields `constraints_rendered`, `response_raw`, and `output_full`

- [ ] Write failing tests proving stage order and persistence: sanitizer receipt is written before Trader; reviewer reloads the Trader replay from disk; grader reloads the reviewer-enriched replay; a scored run/report path is refused.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Wire sanitizer before `run_point`, then persist Trader output, reload for reviewer, persist, reload for grader, and persist.
- [ ] Preserve deterministic audits as verification metadata and keep `--dry-run` call-free/write-free.
- [ ] Run the focused tests and confirm green.

### Task 3: Agent-backed score report

**Files:**
- Modify: `evals/fleet_calibration/score.py`
- Modify: `evals/fleet_calibration/test_calibration.py`
- Modify: `evals/fleet_calibration/README.md`

**Interfaces:**
- Consumes: `result.agent_pipeline.review` and `result.agent_pipeline.grading`
- Preserves: legacy deterministic scoring for historical runs without pipeline receipts

- [ ] Write failing tests proving new runs use persisted grader scores, review failures disqualify, and historical run documents still score without mutation.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Update scoring/report rendering to show sanitizer, reviewer, and grader receipts while mechanically verifying class and return arithmetic.
- [ ] Document the five stages and the mandatory dry-run/coordination boundary.
- [ ] Run `run_suite.py --dry-run` and the complete non-LLM calibration test file.
- [ ] Commit the logical block and stop for resident review before building case backlog packets.
