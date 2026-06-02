"""Smoke-test the Phase 0-6 gate pipeline against the persisted v20 plan.

Loads plan_version=20 from the dev DB, reconstructs PlanSynthesisOutput
from its horizon_*_json columns, runs the full gate_plan_output, and
prints a structured verdict.

This is the "what does the gate see right now" report — no LLM cost,
no synth cycle. Shows the user the gate's current verdict on the
canonical fixture so they can decide whether to flip the
ARGOSY_PLAN_GATE_ENFORCE flag.

Usage:
    .venv/Scripts/python.exe tools/smoke_test_gate_against_v20.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root so `from argosy.*` imports resolve.
sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    import sqlite3

    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SynthesisInputs,
    )
    from argosy.quality import gate_plan_output
    from argosy.quality.canonical_sections import MVP_COVERAGE_THRESHOLD

    db_path = Path("db/argosy.db")
    if not db_path.exists():
        print(f"ERROR: db/argosy.db not found at {db_path.absolute()}")
        return 1

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        "SELECT id, role, horizon_long_md, horizon_medium_md, "
        "horizon_short_md, horizon_long_json, horizon_medium_json, "
        "horizon_short_json, synthesis_inputs_json "
        "FROM plan_versions WHERE id = 20"
    )
    row = cur.fetchone()
    if row is None:
        print("ERROR: plan_version=20 not found in db/argosy.db")
        return 1

    (
        pv_id, role,
        long_md, medium_md, short_md,
        long_json, medium_json, short_json,
        inputs_json,
    ) = row

    print("=" * 70)
    print(f"plan_version id={pv_id} role={role}")
    print("=" * 70)
    print(f"horizon_long_md   : {len(long_md or '')} chars")
    print(f"horizon_medium_md : {len(medium_md or '')} chars")
    print(f"horizon_short_md  : {len(short_md or '')} chars")
    print()

    # Reconstruct PlanSynthesisOutput from the persisted JSON columns.
    synth = None
    try:
        synth = PlanSynthesisOutput(
            long=HorizonSection.model_validate_json(long_json),
            medium=HorizonSection.model_validate_json(medium_json),
            short=HorizonSection.model_validate_json(short_json),
            inputs=SynthesisInputs.model_validate_json(
                inputs_json or '{"baseline_id":null,"prior_current_id":null,'
                               '"snapshot_id":null,"fill_ids":[],'
                               '"agent_report_ids":[],"debate_outcome_ids":[],'
                               '"decision_run_id":null}'
            ),
        )
        print("PlanSynthesisOutput reconstructed cleanly from JSON columns.")
    except Exception as exc:
        print(f"NOTE: PlanSynthesisOutput reconstruction failed: {exc}")
        print("Structured-shape checks (3+4+5) will surface this.")
        print()

    # Run the gate.
    horizon_text = {
        "long": long_md or "",
        "medium": medium_md or "",
        "short": short_md or "",
    }
    verdict = gate_plan_output(
        horizon_text=horizon_text,
        synth=synth,
        distillate=None,  # Phase 4 distillate is per-user; not wired here
        coverage_threshold=MVP_COVERAGE_THRESHOLD,
    )

    print()
    print("=" * 70)
    print(f"GATE VERDICT — {verdict.summary()}")
    print("=" * 70)
    print(f"Total violations: {verdict.total_violations}")
    print()

    for check in verdict.violations:
        violations = verdict.for_check(check)
        if not violations:
            print(f"  [PASS]{check.value:30s}  PASS")
            continue
        print(f"  [FAIL]{check.value:30s}  FAIL ({len(violations)} violation(s))")
        for v in violations[:3]:
            detail = v.detail[:100]
            print(f"       - {detail}")
        if len(violations) > 3:
            print(f"       ... and {len(violations) - 3} more")
    print()
    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)
    print()
    print("v20 is the FROZEN FIXTURE — it predates the integration plan.")
    print("It's expected to fail the gate. The point: the gate detects")
    print("the exact symptoms the plan was built to close. A fresh synth")
    print("under the new Phase 1-6 pipeline will produce horizon MD that")
    print("passes history_leak + jargon_leak; section_coverage will turn")
    print("green as the synth model populates the Phase 4 fields.")
    print()
    print("To enable the gate at /accept, set:")
    print("    ARGOSY_PLAN_GATE_ENFORCE=true")
    print()

    con.close()
    return 0 if verdict.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
