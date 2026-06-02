"""Fast rewriter-iteration harness.

The supervised synth #66-68 loop took ~30 min per cycle to test
one rewriter prompt edit. This harness short-circuits that: load a
frozen ``PlanSynthesisOutput`` fixture, run JUST the rewriter +
invariant validator + simulated gate, print a structured report.

Each iteration is ONE Opus call (~30-60s), not 30 min. The fixture
exercises all the structured fields the validator preserves (targets
with source_section, deltas, speculative_candidates, inputs
provenance) plus prose-laden labels that test the rewriter's
scrubbing prowess.

Usage:
    .venv/Scripts/python.exe tools/iterate_rewriter.py [--fixture v20]
                                                       [--save-output FILE]

Fixtures available:
    v20  — loads plan_version=20 horizons from db/argosy.db
           (the canonical jargon-heavy fixture)

When ``--save-output FILE`` is given, the rewriter output is saved
so the next iteration can diff against it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Project root on sys.path so `from argosy.*` resolves.
sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_v20_synth() -> "PlanSynthesisOutput":
    """Reconstruct a PlanSynthesisOutput from plan_version=20."""
    import sqlite3

    from argosy.agents.plan_synthesizer_types import (
        HorizonSection,
        PlanSynthesisOutput,
        SynthesisInputs,
    )

    con = sqlite3.connect("db/argosy.db")
    cur = con.cursor()
    cur.execute(
        "SELECT horizon_long_json, horizon_medium_json, horizon_short_json, "
        "       synthesis_inputs_json "
        "FROM plan_versions WHERE id = 20"
    )
    long_j, medium_j, short_j, inputs_j = cur.fetchone()
    con.close()

    return PlanSynthesisOutput(
        long=HorizonSection.model_validate_json(long_j),
        medium=HorizonSection.model_validate_json(medium_j),
        short=HorizonSection.model_validate_json(short_j),
        inputs=SynthesisInputs.model_validate_json(
            inputs_j or '{"baseline_id":null,"prior_current_id":null,'
                        '"snapshot_id":null,"fill_ids":[],'
                        '"agent_report_ids":[],"debate_outcome_ids":[],'
                        '"decision_run_id":null}'
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        default="v20",
        help=(
            "Fixture: 'v20' loads plan_versions.id=20; any other "
            "value is treated as a path to a PlanSynthesisOutput "
            "JSON file (e.g. decision_phases.phase_output_json)."
        ),
    )
    parser.add_argument(
        "--save-output",
        type=Path,
        default=Path("tmp_review/rewriter_iter_output.json"),
        help="Where to save the rewriter's output for diff comparison.",
    )
    parser.add_argument(
        "--user-id",
        default="ariel",
        help="user_id for BaseAgent init (controls SDK + cost telemetry).",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"REWRITER ITERATION HARNESS — fixture={args.fixture}")
    print("=" * 70)

    # ----------------------------------------------------------------
    # Load fixture.
    # ----------------------------------------------------------------
    t0 = time.monotonic()
    if args.fixture == "v20":
        synth_input = _load_v20_synth()
    else:
        from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput
        path = Path(args.fixture)
        if not path.exists():
            raise SystemExit(f"fixture file not found: {path}")
        synth_input = PlanSynthesisOutput.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    load_s = time.monotonic() - t0
    print(f"Fixture loaded in {load_s:.2f}s")
    print(f"  long.targets    : {len(synth_input.long.targets)}")
    print(f"  medium.targets  : {len(synth_input.medium.targets)}")
    print(f"  short.targets   : {len(synth_input.short.targets)}")
    print(f"  long.themes     : {len(synth_input.long.themes)}")
    print(f"  medium.themes   : {len(synth_input.medium.themes)}")
    print(f"  short.themes    : {len(synth_input.short.themes)}")
    print(f"  long.actions    : {len(synth_input.long.actions)}")
    print(f"  medium.actions  : {len(synth_input.medium.actions)}")
    print(f"  short.actions   : {len(synth_input.short.actions)}")
    print()

    # ----------------------------------------------------------------
    # Fire the rewriter (live LLM call — Opus, ~30-60s).
    # ----------------------------------------------------------------
    from argosy.agents.plan_language_rewriter import PlanLanguageRewriter

    print("Running PlanLanguageRewriter (live LLM call)...")
    t1 = time.monotonic()
    rewriter = PlanLanguageRewriter(user_id=args.user_id)
    try:
        result = rewriter.run_sync(synth_output=synth_input, decision_id=0)
        rewriter_s = time.monotonic() - t1
        print(f"Rewriter completed in {rewriter_s:.1f}s")
        rewritten = result.output
    except Exception as exc:
        rewriter_s = time.monotonic() - t1
        print(f"REWRITER CRASHED after {rewriter_s:.1f}s: "
              f"{type(exc).__name__}: {exc}")
        return 2

    # ----------------------------------------------------------------
    # Save output (for diff against next iteration).
    # ----------------------------------------------------------------
    if args.save_output:
        args.save_output.parent.mkdir(parents=True, exist_ok=True)
        args.save_output.write_text(
            rewritten.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"Saved rewriter output to {args.save_output}")
    print()

    # ----------------------------------------------------------------
    # Mirror the orchestrator's wrapper: force-preserve structured
    # subtrees before validation. This is what production sees.
    # ----------------------------------------------------------------
    from argosy.orchestrator.flows.plan_synthesis.orchestrator import (
        _force_preserve_structured_fields,
    )
    from argosy.quality.rewriter_invariants import (
        validate_rewriter_invariants,
    )

    rewritten = _force_preserve_structured_fields(
        before=synth_input, after=rewritten
    )
    violations = validate_rewriter_invariants(
        before=synth_input, after=rewritten
    )
    print("=" * 70)
    print(f"INVARIANT VALIDATOR — {len(violations)} violation(s)")
    print("=" * 70)
    if violations:
        # Classify as the orchestrator would.
        structural = [
            v for v in violations
            if (
                "rewriter changed" in v.detail
                or "rewriter modified" in v.detail
                or "subtree modified" in v.detail
                or "preserved field" in v.detail
                or "(provenance)" in v.detail
            )
        ]
        prose = [v for v in violations if v not in structural]
        print(f"  structural (would abort): {len(structural)}")
        print(f"  prose-only (would warn) : {len(prose)}")
        print()
        if structural:
            print("STRUCTURAL VIOLATIONS (these MUST be fixed):")
            for v in structural[:10]:
                print(f"  - locator: {v.locator}")
                print(f"    detail : {v.detail[:120]}")
            if len(structural) > 10:
                print(f"  ... and {len(structural)-10} more")
            print()
        if prose and not structural:
            print("PROSE VIOLATIONS (would log + ship in production):")
            for v in prose[:6]:
                print(f"  - locator: {v.locator}")
                print(f"    detail : {v.detail[:120]}")
            if len(prose) > 6:
                print(f"  ... and {len(prose)-6} more")

    # ----------------------------------------------------------------
    # Run the full Phase 0 gate on the rewritten output's markdown.
    # ----------------------------------------------------------------
    from argosy.orchestrator.flows.plan_synthesis.render import (
        _horizon_md_user,
    )
    from argosy.quality import gate_plan_output
    from argosy.quality.canonical_sections import MVP_COVERAGE_THRESHOLD

    horizon_text = {
        "long": _horizon_md_user(rewritten.long),
        "medium": _horizon_md_user(rewritten.medium),
        "short": _horizon_md_user(rewritten.short),
    }
    verdict = gate_plan_output(
        horizon_text=horizon_text,
        synth=rewritten,
        distillate=None,
        coverage_threshold=MVP_COVERAGE_THRESHOLD,
    )
    print()
    print("=" * 70)
    print(f"PHASE 0 GATE — {verdict.summary()}")
    print("=" * 70)
    print(f"Total violations: {verdict.total_violations}")
    for check, vlist in verdict.violations.items():
        marker = "[PASS]" if not vlist else "[FAIL]"
        print(f"  {marker} {check.value:30s} ({len(vlist)} violation(s))")
        if vlist:
            for v in vlist[:3]:
                print(f"       - {v.detail[:100]}")
            if len(vlist) > 3:
                print(f"       ... and {len(vlist)-3} more")

    print()
    total_s = time.monotonic() - t0
    print(f"Total iteration time: {total_s:.1f}s")
    print()
    if not violations and verdict.passes:
        print("REWRITER CLEAN — gate passes against the rewriter output. "
              "Safe to fire a full supervised synth to confirm end-to-end.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
