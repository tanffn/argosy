"""Fast synth-evidence iteration harness.

Replays JUST the synthesizer call (Phase 3) against captured Phase 1
analyst reports from a real decision_run. The synth's prompt is
deterministic from its inputs, so we re-run with the current
EVIDENCE DISCIPLINE rubric and check whether the model now emits
numeric facts whose `value` substring-matches the citation `extract`.

Per-iteration: one Opus call (~1-2 min, ~$0.50). Compare to a full
supervised synth (~30 min, ~$15).

Usage:
    .venv/Scripts/python.exe tools/iterate_synth_evidence.py
                              [--decision-run-id 69]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--decision-run-id",
        type=int,
        default=69,
        help="Reuse this run's phase_1 analyst_reports_text as the input.",
    )
    parser.add_argument(
        "--user-id",
        default="ariel",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"SYNTH EVIDENCE ITERATION — decision_run_id={args.decision_run_id}")
    print("=" * 70)

    # ----------------------------------------------------------------
    # Load captured Phase 1 analyst_reports_text from decision_phases.
    # ----------------------------------------------------------------
    import sqlite3
    con = sqlite3.connect("db/argosy.db")
    cur = con.cursor()
    cur.execute(
        "SELECT phase_output_json FROM decision_phases "
        "WHERE decision_run_id = ? AND kind = 'synthesis.phase_1' "
        "ORDER BY seq DESC LIMIT 1",
        (args.decision_run_id,),
    )
    row = cur.fetchone()
    if not row:
        print(f"No phase_1 output for run {args.decision_run_id}.")
        return 1
    phase_1 = json.loads(row[0])
    analyst_reports_text = phase_1["analyst_reports_text"]
    print(f"Loaded {len(analyst_reports_text)} chars of analyst reports.")

    # Pull the baseline + a synthetic debate-outcomes block so the
    # synth has plausible context. Real debates are weight-of-evidence;
    # for evidence-quality testing this is fine.
    cur.execute(
        "SELECT pv.distillate_rendered, pv.raw_markdown "
        "FROM plan_versions pv "
        "WHERE pv.user_id = ? AND pv.role = 'baseline' "
        "ORDER BY pv.id DESC LIMIT 1",
        (args.user_id,),
    )
    bl = cur.fetchone()
    baseline_distillate_md = (bl[0] if bl and bl[0] else "") or "(no baseline)"
    con.close()
    print(f"Loaded baseline distillate ({len(baseline_distillate_md)} chars).")
    print()

    debate_outcomes_text = (
        "=== Debate outcome — long ===\n(replay harness: debates omitted)\n\n"
        "=== Debate outcome — medium ===\n(replay harness: debates omitted)\n\n"
        "=== Debate outcome — short ===\n(replay harness: debates omitted)\n"
    )

    # ----------------------------------------------------------------
    # Fire the synthesizer (live Opus call).
    # ----------------------------------------------------------------
    from argosy.agents.plan_synthesizer import PlanSynthesizerAgent

    agent = PlanSynthesizerAgent(user_id=args.user_id)
    print("Running PlanSynthesizerAgent (live LLM call)...")
    t0 = time.monotonic()
    try:
        result = agent.run_sync(
            baseline_distillate_md=baseline_distillate_md,
            prior_current_md="",
            prior_items_index=[],
            analyst_reports_text=analyst_reports_text,
            debate_outcomes_text=debate_outcomes_text,
            portfolio_snapshot_summary="(replay harness; portfolio context "
                                       "embedded in analyst reports above)",
            recent_fills_summary="(replay harness; no fills)",
        )
        elapsed = time.monotonic() - t0
        synth_output = result.output
        print(f"Synth completed in {elapsed:.1f}s")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"SYNTH CRASHED after {elapsed:.1f}s: "
              f"{type(exc).__name__}: {exc}")
        return 2

    # ----------------------------------------------------------------
    # Save the output for downstream rewriter testing.
    # ----------------------------------------------------------------
    out_path = Path("tmp_review/synth_evidence_iter_output.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        synth_output.model_dump_json(indent=2), encoding="utf-8"
    )
    print(f"Synth output saved to {out_path}")
    print()

    # ----------------------------------------------------------------
    # Validate evidence quality at the content-gate layer.
    # ----------------------------------------------------------------
    from argosy.quality import gate_plan_output
    from argosy.quality.canonical_sections import MVP_COVERAGE_THRESHOLD

    verdict = gate_plan_output(
        horizon_text={"long": "", "medium": "", "short": ""},
        synth=synth_output,
        distillate=None,
        coverage_threshold=MVP_COVERAGE_THRESHOLD,
    )
    print("=" * 70)
    print(f"GATE VERDICT — {verdict.summary()}")
    print("=" * 70)
    for check, vlist in verdict.violations.items():
        marker = "[PASS]" if not vlist else "[FAIL]"
        print(f"  {marker} {check.value:30s} ({len(vlist)} violation(s))")
        if vlist and check.value == "evidence_per_section":
            for v in vlist[:6]:
                print(f"       - {v.detail[:120]}")
            if len(vlist) > 6:
                print(f"       ... and {len(vlist)-6} more")

    print()
    sections_count = len(synth_output.sections) if synth_output.sections else 0
    print(f"sections emitted: {sections_count}")
    if synth_output.sections:
        evid_counts = [
            (s.section_id, len(s.evidence.facts), len(s.evidence.missing_data))
            for s in synth_output.sections
        ]
        for sid, fc, mdc in evid_counts[:8]:
            print(f"  {sid:30s}  facts={fc}  missing={mdc}")

    return 0 if verdict.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
