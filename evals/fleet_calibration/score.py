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

LONG_ACTIONS_UNPOSITIONED = {"buy"}
LONG_ACTIONS_POSITIONED = {"buy", "hold"}


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


def grade_point(result: dict, packet: dict) -> dict:
    action = result.get("action")
    expected = packet.get("expected_classes", [])
    grading = packet.get("grading")
    rationale = result.get("rationale_summary") or ""
    positioned = packet.get("positioned", False)

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
    }


def main() -> None:
    run_path = Path(sys.argv[1])
    if not run_path.is_absolute():
        run_path = HERE / run_path
    run_doc = json.loads(run_path.read_text(encoding="utf-8"))
    packets = load_packet_index()

    rows = []
    disqualified = []
    for r in run_doc["results"]:
        cid = r["case_id"]
        pkt = packets.get(cid)
        status = r.get("status")
        if status == "disqualified_temporal":
            disqualified.append((cid, "temporal", "; ".join(r.get("temporal_violations", []))))
            continue
        if status != "ok" or pkt is None:
            disqualified.append((cid, "error", r.get("error", "packet missing")))
            continue
        audit = r.get("output_audit", {})
        if audit.get("contaminated"):
            disqualified.append((cid, "contaminated", ", ".join(audit["contamination_hits"])))
            continue
        g = grade_point(r, pkt)
        rows.append({**r, **g, "packet": pkt})

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
        for cid, why, detail in disqualified:
            lines.append(f"- {cid}: {why} — {detail}")
        lines.append("")
    lines.append("## Per-point detail (section 2b three-column + clock)")
    for row in sorted(rows, key=lambda r: (r["category"], r["case_id"])):
        audit = row.get("output_audit", {})
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
        av = f"{row['acted_return_pct']:+.0f}% vs benchmark {row['benchmark_return_pct']:+.0f}%" \
            if row["acted_return_pct"] is not None else "n/a"
        lines.append(f"- **Agent score:** {row['score']} ({'in class' if row['in_class'] else 'OUT of class'}); acted {av}")
        if row["notes"]:
            lines.append(f"- notes: {'; '.join(row['notes'])}")
        lines.append("")

    report_path = run_path.with_name(run_path.stem + "_report.md")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"report -> {report_path}")
    print("\n".join(lines[:60]))


if __name__ == "__main__":
    main()
