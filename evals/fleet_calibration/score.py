"""Score a fleet-calibration run and emit the section-2b report.

Usage:
  .venv/Scripts/python.exe evals/fleet_calibration/score.py runs/<date>.json

Scoring rules (spec section 3, recorded so they're not re-litigated):
- Class match: action in expected_classes => PASS (1.0).
- F1 (foresight) points are graded leniently: a reasoned pass WITH falsifiers
  recorded is a half-fail (0.5), not a full fail.
- A trap-case BUY is a graded fail: 0.5 if bought SMALL with the killing
  falsifier recorded; 0.0 otherwise. (Mechanical proxy flags the candidates;
  the report lists them for manual adjudication.)
- Outcome luck never overrides decision quality (the CVNA rule): returns are
  reported, but the pass/fail grade comes from the expected CLASS.
- MEDIUM-conviction verdicts in the right class are passes; conviction
  calibration (HIGH vs MEDIUM hit rates) is reported separately.

Agent-score column: "if we acted on this verdict we would have returned NN%"
vs the benchmark path (buy-and-hold / ride-to-terminal), from the fixture's
resolution block. Long-action set: buy (unpositioned) / buy+hold (positioned).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKETS_DIR = HERE / "packets"

from agent_pipeline import (  # noqa: E402
    verify_classifier,
    verify_grading,
    verify_review,
    verify_sanitizer,
)
from run_suite import build_constraints, output_audit, temporal_audit  # noqa: E402

LONG_ACTIONS_UNPOSITIONED = {"buy"}
LONG_ACTIONS_POSITIONED = {"buy", "hold"}


def ensure_report_path_writable(report_path: Path) -> None:
    if report_path.exists():
        raise RuntimeError(
            f"score report is immutable: {report_path}; choose an unscored run"
        )


def load_packet_index() -> dict[str, dict]:
    idx = {}
    for p in PACKETS_DIR.glob("*.json"):
        pkt = json.loads(p.read_text(encoding="utf-8"))
        idx[pkt["case_id"]] = pkt
    return idx


def extract_section(rationale: str, *names: str) -> str:
    """Pull the first sentence-sized snippet following any of the given labels."""
    for name in names:
        m = re.search(
            r"(?is)\*\*" + re.escape(name) + r":?\*\*:?\s*(.{0,400}?)(?:\n\n|\Z)",
            rationale,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def find_snippet(rationale: str, pattern: str, width: int = 320) -> str:
    m = re.search(pattern, rationale, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    start = m.start()
    return re.sub(r"\s+", " ", rationale[start : start + width]).strip()


def mentions_falsifiers(rationale: str) -> bool:
    return bool(re.search(r"(?i)falsifier|would kill (the|this) thesis|exit trigger", rationale))


def recompute_pipeline_verifications(
    result: dict, packet: dict
) -> dict[str, dict]:
    pipeline = result.get("agent_pipeline") or {}
    if not pipeline:
        return {}
    required = (
        "classifier_data_sourcing",
        "sanitizer",
        "review",
        "grading",
    )
    if any(stage not in pipeline for stage in required):
        return {}
    return {
        "classifier_data_sourcing": verify_classifier(
            packet, pipeline["classifier_data_sourcing"]
        ),
        "sanitizer": verify_sanitizer(packet, pipeline["sanitizer"]),
        "review": verify_review(pipeline["review"]),
        "grading": verify_grading(packet, result, pipeline["grading"]),
    }


def packet_snapshot_mismatch(result: dict, packet: dict) -> str | None:
    expected_constraints = build_constraints(packet)
    expected = {
        "case_id": packet["case_id"],
        "alias": packet["alias"],
        "analyst_reports": packet["analyst_reports"],
        "positions": packet["positions"],
        "constraints_rendered": expected_constraints,
        "freeze_date": packet.get("freeze_date"),
        "sources": packet.get("sources") or [],
        "trader_call": {
            "debate_outcome": {},
            "tier": "T2",
            "mode": "long_hold",
            "ticker": packet["alias"],
        },
    }
    snapshot = result.get("packet_snapshot") or {}
    for field, value in expected.items():
        if snapshot.get(field) != value:
            return field
    if result.get("constraints_rendered") != expected_constraints:
        return "constraints_rendered"
    return None


def pipeline_disqualification(
    result: dict,
    packet: dict,
    *,
    require_pipeline: bool = False,
) -> str | None:
    """Return why a new five-stage result cannot be scored."""
    pipeline = result.get("agent_pipeline")
    if not pipeline:
        return "pipeline missing" if require_pipeline else None
    for stage in (
        "classifier_data_sourcing",
        "sanitizer",
        "review",
        "grading",
    ):
        if stage not in pipeline:
            return f"pipeline incomplete: missing {stage}"
    replay_required = (
        "packet_snapshot",
        "constraints_rendered",
        "response_raw",
        "output_full",
    )
    missing_replay = [field for field in replay_required if not result.get(field)]
    if missing_replay:
        return f"replay incomplete: missing {', '.join(missing_replay)}"
    snapshot_mismatch = packet_snapshot_mismatch(result, packet)
    if snapshot_mismatch:
        return f"packet snapshot mismatch: {snapshot_mismatch}"
    validations = recompute_pipeline_verifications(result, packet)
    for stage, label in (
        ("classifier_data_sourcing", "classifier"),
        ("sanitizer", "sanitizer"),
        ("review", "review"),
        ("grading", "grading"),
    ):
        verification = validations.get(stage) or {"ok": False}
        if not verification["ok"]:
            return f"{label} verification failed"
    sanitizer = pipeline["sanitizer"].get("output") or {}
    if not sanitizer.get("safe_to_run", False):
        return "sanitizer rejected packet"
    review = pipeline["review"].get("output") or {}
    for field in ("output_clean", "packet_fidelity", "workflow_correct"):
        if not review.get(field, False):
            return f"review failed: {field}"
    return None


def recompute_integrity_audit(result: dict, packet: dict) -> dict:
    response_text = result.get("response_raw") or (
        (result.get("rationale_summary") or "")
        + " "
        + " ".join(result.get("cited_sources") or [])
    )
    return {
        "temporal_violations": temporal_audit(packet),
        "output_audit": output_audit(packet, response_text),
    }


def integrity_disqualification(
    result: dict, packet: dict
) -> tuple[str, str] | None:
    audit = recompute_integrity_audit(result, packet)
    if audit["temporal_violations"]:
        return "temporal", "; ".join(audit["temporal_violations"])
    if audit["output_audit"].get("contaminated"):
        return (
            "contaminated",
            ", ".join(audit["output_audit"].get("contamination_hits") or []),
        )
    return None


def render_pipeline_receipts(
    result: dict, packet: dict | None = None
) -> list[str]:
    pipeline = result.get("agent_pipeline") or {}
    if not pipeline:
        return []
    recomputed = (
        recompute_pipeline_verifications(result, packet) if packet is not None
        else {}
    )
    lines = ["- **Five-stage structured receipts:**"]
    for stage in (
        "classifier_data_sourcing",
        "sanitizer",
        "review",
        "grading",
    ):
        receipt = pipeline.get(stage) or {}
        rendered = {
            key: receipt.get(key)
            for key in (
                "stage",
                "agent_role",
                "model",
                "output",
                "verification",
            )
            if key in receipt
        }
        if stage in recomputed:
            rendered["verification"] = recomputed[stage]
        lines.extend([
            f"  - **{stage}:**",
            "```json",
            json.dumps(rendered, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
        ])
    return lines


def render_disqualified_entry(
    case_id: str,
    reason: str,
    detail: str,
    result: dict,
    packet: dict | None = None,
) -> list[str]:
    lines = [f"### {case_id}", f"- {reason} — {detail}"]
    lines.extend(render_pipeline_receipts(result, packet))
    return lines


def select_latest_results(results: list[dict]) -> list[dict]:
    latest_index = {
        result["case_id"]: index for index, result in enumerate(results)
    }
    return [
        result
        for index, result in enumerate(results)
        if latest_index[result["case_id"]] == index
    ]


def grade_point(result: dict, packet: dict) -> dict:
    action = result.get("action")
    expected = packet.get("expected_classes", [])
    grading = packet.get("grading")
    rationale = result.get("rationale_summary") or ""
    positioned = packet.get("positioned", False)

    pipeline = result.get("agent_pipeline") or {}
    grader = pipeline.get("grading") or {}
    if grader and verify_grading(packet, result, grader)["ok"]:
        authored = grader.get("output") or {}
        acted = authored.get("acted_return_pct")
        # Null-resolution synthetics: coerce grader 0.0 → None so reports show
        # n/a (synthetic) rather than +0%.
        res = packet.get("resolution")
        if (
            (res is None or res.get("benchmark_return_pct") is None)
            and isinstance(acted, (int, float))
            and abs(float(acted)) <= 1e-6
        ):
            acted = None
        return {
            "in_class": bool(authored.get("in_expected_class")),
            "score": float(authored.get("score", 0.0)),
            "notes": [authored["rationale"]] if authored.get("rationale") else [],
            "acted_return_pct": acted,
            "benchmark_return_pct": authored.get("benchmark_return_pct"),
            "falsifiers_snippet": find_snippet(rationale, r"falsifier"),
            "clock_snippet": find_snippet(
                rationale,
                r"THE CLOCK|\bCLOCK\b\s*[:—-]|next validation|validation point",
            ),
            "verdict_line": extract_section(rationale, "Verdict"),
            "grade_source": "calibration_grader",
        }

    in_class = action in expected
    score = 1.0 if in_class else 0.0
    notes = []
    if not in_class:
        if grading == "F1_lenient" and action == "hold" and mentions_falsifiers(rationale):
            score = 0.5
            notes.append("F1 lenient: reasoned pass with falsifiers = half-fail")
        elif grading == "trap" and action == "buy":
            small = bool(re.search(r"(?i)starter|small slot|0\.5|half slot|minimum", rationale))
            if small and mentions_falsifiers(rationale):
                score = 0.5
                notes.append("trap BUY half-credit: small + killing falsifier recorded (verify manually)")
            else:
                notes.append("trap BUY full fail: no small-size-with-falsifier evidence")

    res = packet.get("resolution")
    acted_ret = bench_ret = None
    if res and res.get("benchmark_return_pct") is not None:
        bench_ret = res["benchmark_return_pct"]
        long_actions = LONG_ACTIONS_POSITIONED if positioned else LONG_ACTIONS_UNPOSITIONED
        acted_ret = bench_ret if action in long_actions else 0.0

    return {
        "in_class": in_class,
        "score": score,
        "notes": notes,
        "acted_return_pct": acted_ret,
        "benchmark_return_pct": bench_ret,
        "falsifiers_snippet": find_snippet(rationale, r"falsifier"),
        "clock_snippet": find_snippet(
            rationale,
            r"THE CLOCK|\bCLOCK\b\s*[:—-]|next validation|validation point",
        ),
        "verdict_line": extract_section(rationale, "Verdict"),
        "grade_source": "legacy_deterministic",
    }


def main() -> None:
    run_path = Path(sys.argv[1])
    if not run_path.is_absolute():
        run_path = HERE / run_path
    run_doc = json.loads(run_path.read_text(encoding="utf-8"))
    packets = load_packet_index()
    require_pipeline = run_doc.get("pipeline_version") == 1

    rows = []
    disqualified = []
    for r in select_latest_results(run_doc["results"]):
        cid = r["case_id"]
        pkt = packets.get(cid)
        status = r.get("status")
        if status == "disqualified_temporal":
            disqualified.append((
                cid,
                "temporal",
                "; ".join(r.get("temporal_violations", [])),
                r,
            ))
            continue
        if pkt is None:
            disqualified.append((cid, "error", "packet missing", r))
            continue
        if status != "ok":
            disqualified.append((cid, "error", r.get("error", status), r))
            continue
        integrity_failure = integrity_disqualification(r, pkt)
        if integrity_failure:
            why, detail = integrity_failure
            disqualified.append((cid, why, detail, r, pkt))
            continue
        pipeline_failure = pipeline_disqualification(
            r, pkt, require_pipeline=require_pipeline
        )
        if pipeline_failure:
            disqualified.append((
                cid, "agent_pipeline", pipeline_failure, r, pkt
            ))
            continue
        g = grade_point(r, pkt)
        score_audit = recompute_integrity_audit(r, pkt)["output_audit"]
        from horizon_calibration import score_row as score_horizon_row

        horizon = score_horizon_row(
            rationale=r.get("rationale_summary") or "",
            freeze_date=pkt.get("freeze_date"),
            packet=pkt,
        )
        rows.append({
            **r,
            **g,
            "packet": pkt,
            "score_output_audit": score_audit,
            "horizon_calibration": horizon,
        })

    # --- aggregates ---
    by_cat: dict[str, list] = {}
    for row in rows:
        by_cat.setdefault(row["category"], []).append(row)

    def agg(rws):
        n = len(rws)
        pts = sum(r["score"] for r in rws)
        rets = [(r["acted_return_pct"], r["benchmark_return_pct"]) for r in rws
                if r["acted_return_pct"] is not None]
        acted = sum(a for a, _ in rets) / len(rets) if rets else None
        bench = sum(b for _, b in rets) / len(rets) if rets else None
        return n, pts, acted, bench, len(rets)

    conv: dict[str, list] = {}
    for row in rows:
        conv.setdefault(str(row.get("confidence")), []).append(row["score"])

    lines = [f"# Fleet-calibration report — {run_path.name}", ""]
    lines.append("| Point | Category | Fleet verdict (conv, size) | In class? | Score | Acted vs benchmark |")
    lines.append("|---|---|---|---|---|---|")
    for row in sorted(rows, key=lambda r: (r["category"], r["case_id"])):
        av = f"{row['acted_return_pct']:+.0f}% vs {row['benchmark_return_pct']:+.0f}%" \
            if row["acted_return_pct"] is not None else "n/a (synthetic)"
        lines.append(
            f"| {row['case_id']} | {row['category']}/{row['grading']} "
            f"| {row['action']} ({row['confidence']}, {row['size']} {row['size_units']}) "
            f"| {'Y' if row['in_class'] else 'N'} | {row['score']} | {av} |"
        )
    lines.append("")
    lines.append("## Category subtotals")
    for cat in sorted(by_cat):
        n, pts, acted, bench, nres = agg(by_cat[cat])
        ret = f"; acting on fleet {acted:+.0f}% vs benchmark {bench:+.0f}% (n={nres})" \
            if acted is not None else ""
        lines.append(f"- **{cat}**: {pts}/{n} points{ret}")
    n, pts, acted, bench, nres = agg(rows)
    lines.append(f"- **TOTAL**: {pts}/{n}" + (
        f"; acting on the fleet at every frozen point: {acted:+.0f}% avg vs {bench:+.0f}% benchmark (n={nres})"
        if acted is not None else ""))
    lines.append("")
    lines.append("## Conviction calibration")
    for c in sorted(conv):
        s = conv[c]
        lines.append(f"- {c}: {sum(s)}/{len(s)} ({100 * sum(s) / len(s):.0f}%)")
    lines.append("")
    if disqualified:
        lines.append("## Disqualified / not scored")
        for entry in disqualified:
            cid, why, detail, result, *packet_tail = entry
            packet = packet_tail[0] if packet_tail else packets.get(cid)
            lines.extend(
                render_disqualified_entry(cid, why, detail, result, packet)
            )
        lines.append("")
    lines.append("")
    lines.append("## Horizon calibration (§2b clock band)")
    hz_counts: dict[str, int] = {}
    for row in rows:
        hz = (row.get("horizon_calibration") or {}).get("score") or "no_band"
        hz_counts[hz] = hz_counts.get(hz, 0) + 1
    for label in ("inside", "outside", "unestimable_stated", "not_applicable", "no_band"):
        if label in hz_counts:
            lines.append(f"- {label}: {hz_counts[label]}")
    for row in sorted(rows, key=lambda r: (r["category"], r["case_id"])):
        hz = row.get("horizon_calibration") or {}
        stated = hz.get("stated")
        if isinstance(stated, dict):
            stated_s = (
                f"{stated.get('low_months')}-{stated.get('high_months')} mo "
                f"({stated.get('raw')})"
            )
        else:
            stated_s = stated or "none"
        lines.append(
            f"- {row['case_id']}: {hz.get('score')} "
            f"(stated={stated_s}; actual_months={hz.get('actual_months')})"
        )
    lines.append("")
    lines.append("## Per-point detail (section 2b three-column + clock)")
    for row in sorted(rows, key=lambda r: (r["category"], r["case_id"])):
        audit = row.get("score_output_audit", {})
        lines.append(f"### {row['case_id']} — real: {row['real']}")
        lines.append(f"- **Fleet reasoning:** {row['action']} ({row['confidence']}) — {row['verdict_line']}")
        lines.append(f"  - falsifiers: {row['falsifiers_snippet'] or 'NOT RECORDED'}")
        lines.append(f"  - clock: {row['clock_snippet'] or 'NOT STATED'}")
        lines.append(
            f"- **Reasoning-integrity audit:** temporal OK; "
            f"contamination hits: {audit.get('contamination_hits', [])}; "
            f"suspects: {audit.get('suspect_terms', [])}; "
            f"years in output: {audit.get('calendar_years_in_output', [])}"
        )
        review = (
            (row.get("agent_pipeline") or {}).get("review") or {}
        ).get("output") or {}
        if review:
            lines.append(
                "  - independent replay review: "
                f"clean={review.get('output_clean')}; "
                f"packet_fidelity={review.get('packet_fidelity')}; "
                f"workflow_correct={review.get('workflow_correct')}; "
                f"visible-reasoning grounding={review.get('reasoning_grounded_score')}/4"
            )
        lines.extend(render_pipeline_receipts(row, row["packet"]))
        av = f"{row['acted_return_pct']:+.0f}% vs benchmark {row['benchmark_return_pct']:+.0f}%" \
            if row["acted_return_pct"] is not None else "n/a"
        lines.append(
            f"- **Agent score:** {row['score']} "
            f"({'in class' if row['in_class'] else 'OUT of class'}); acted {av}; "
            f"source={row['grade_source']}"
        )
        if row["notes"]:
            lines.append(f"- notes: {'; '.join(row['notes'])}")
        lines.append("")

    report_path = run_path.with_name(run_path.stem + "_report.md")
    ensure_report_path_writable(report_path)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"report -> {report_path}")
    print("\n".join(lines[:60]))


if __name__ == "__main__":
    main()
