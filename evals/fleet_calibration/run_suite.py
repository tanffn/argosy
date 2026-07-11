"""Fleet-calibration benchmark runner.

Spec: docs/design/fleet_calibration_benchmark.md (protocol section 1, report
section 2b, implementation notes section 4). Lifted from the proven ad-hoc
harness in tmp/fleet_timemachine/ (run_game.py / run_ttcf.py, 2026-07-09/10).

- Loads packet fixtures from evals/fleet_calibration/packets/*.json.
- TEMPORAL-INTEGRITY AUDIT (pre-run): refuses to run or score any point whose
  packet cites a source dated AFTER its freeze date (synthetics exempt).
- Runs each point through the production TraderAgent (long_hold, T2 -> Opus),
  SEQUENTIALLY (real CLI calls), retrying a failed point once.
- Persists raw results incrementally to runs/<date>.json after EVERY point
  (durable side-effect BEFORE any print — cp1252 discipline).
- OUTPUT CONTAMINATION AUDIT (post-run, per point): (a) grep for the packet's
  contamination_terms (hit => disqualified); (b) calendar-year scan + global
  real-world-name scan (hits => 'suspect' flags for manual adjudication).

No DB writes: BaseAgent.run() does not persist.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import traceback
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

PACKETS_DIR = HERE / "packets"
RUNS_DIR = HERE / "runs"
CLASSIFIER_RECEIPTS_DIR = HERE / "classifier_receipts"

from agent_pipeline import (  # noqa: E402
    run_grading,
    run_replay_pipeline,
    run_review,
    run_sanitizer,
    verify_classifier,
    verify_sanitizer,
)

# ---------------------------------------------------------------------------
# Prompt scaffolding — verbatim from the proven harness (tmp/fleet_timemachine)
# ---------------------------------------------------------------------------
USER_CONSTRAINTS = (
    "Long-horizon investor (5+ year intended hold). Goal: maximize long-term "
    "wealth and earliest safe financial independence. The portfolio has a "
    "'high-potential' sleeve (up to ~5% of portfolio total, ~1-2% per "
    "position) reserved for asymmetric growth candidates; investable cash is "
    "available. Volatility tolerance is high. No leverage, no options.\n\n"
    "DATA PROVENANCE NOTE: this consult evaluates a candidate supplied by a "
    "research service under a MASKED NAME. The ticker and company name are "
    "aliases, and all absolute dollar figures (revenue, market cap, cash, "
    "share price) have been scaled by an undisclosed constant — but every "
    "ratio, growth rate, margin, and valuation multiple is exact and "
    "internally consistent. Evaluate strictly on the data provided; do not "
    "attempt to identify the real company, and do not treat the masked name "
    "as a data-quality defect (the masking is intentional and the figures "
    "are audited)."
)

# v77 exit-discipline rule (scripts/apply_no_price_exit_rule.py), with ONE
# benchmark-required change: the scar sentence is GENERICIZED (production text
# names PLTR + its $8->$16->10x path — that ticker/outcome is itself a case
# under test here, so quoting it verbatim would leak an answer into the prompt
# and false-flag the contamination audit). Semantics preserved.
# Attached when the fixture sets constraints_extra == "exit_rule".
EXIT_RULE = (
    "\n\nEXIT DISCIPLINE (client directive — the client's scar: a prior "
    "high-potential position was held through its trough, then sold purely "
    "on price after it doubled, missing the subsequent 10x+): PRICE APPRECIATION "
    "ALONE IS NEVER AN EXIT TRIGGER in this sleeve, in either lane. A "
    "position doubling triggers a thesis RE-DERIVATION (did a milestone "
    "land? has the cap-math ceiling moved?), never an automatic trim. The "
    "only sanctioned trims: (a) the position's recorded thesis falsifier "
    "fires, or (b) the sleeve breaches its plan cap — then rebalance "
    "mechanically back to cap while KEEPING the position. Slot sizing "
    "(~0.5-1%, accepted 100% loss) already does the risk work; "
    "profit-taking is never needed for safety."
)

# The clock requirement (spec section 2b, owner-specified 2026-07-10).
CLOCK_RULE = (
    "\n\nREPORTING REQUIREMENT (fleet standard): inside the "
    "**Recommendation:** section, explicitly record: (1) FALSIFIERS — the "
    "specific, checkable conditions that would kill this thesis; and (2) THE "
    "CLOCK — the NEXT VALIDATION POINT (the dated or estimated upcoming "
    "event where the thesis gets tested: an earnings print, a product "
    "launch, a contract renewal) and the expected re-rating horizon as an "
    "honest band (e.g. '3-6 months', '2-3 years'). A hard-to-estimate "
    "horizon is NOT optional to report: if it is genuinely unestimable, say "
    "so explicitly — that itself lowers conviction."
)

# ---------------------------------------------------------------------------
# Audits
# ---------------------------------------------------------------------------
# Global real-world-name scan (spec protocol step 2d). Hits are 'suspect'
# flags for manual adjudication; per-packet contamination_terms auto-disqualify.
GLOBAL_SUSPECT_TERMS = [
    "Palantir", "PLTR", "Karp", "Gotham", "Foundry",
    "Nvidia", "NVDA", "Jensen", "Huang", "CUDA", "Hopper", "H100", "A100",
    "ChatGPT", "OpenAI", "GPT-4", "Microsoft", "Azure",
    "AMD", "Ryzen", "EPYC", "Lisa Su", "Radeon", "Xilinx", "Instinct",
    "MI300", "MI308", "Intel",
    "GoPro", "GPRO", "Karma drone", "Hero camera",
    "Tattooed Chef", "TTCF", "C3.ai", "Siebel",
    "Meta ", "Facebook", "Zuckerberg", "Instagram", "WhatsApp", "TikTok",
    "Netflix", "NFLX", "Qwikster", "Hastings", "Disney",
    "Amazon", "AMZN", "AWS", "Bezos",
    "Arista", "ANET", "Cisco", "CSCO",
    "Nikola", "NKLA", "Milton",
    "Beyond Meat", "BYND", "Impossible Foods",
    "ContextLogic", "Peloton", "PTON",
    "Zoom", "Carvana", "CVNA",
    "COVID", "coronavirus", "Ukraine",
]
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def temporal_audit(packet: dict) -> list[str]:
    """Return violations: sources dated after the freeze date."""
    if packet.get("synthetic"):
        return []
    freeze = packet.get("freeze_date")
    violations: list[str] = []
    if not freeze:
        violations.append("missing freeze_date on a non-synthetic packet")
        return violations
    freeze_d = date.fromisoformat(freeze)
    sources = packet.get("sources") or []
    if not sources:
        violations.append("non-synthetic packet has no sources")
    for s in sources:
        d = s.get("date")
        if not d:
            violations.append(f"source missing date: {s.get('fact', '?')[:80]}")
            continue
        if date.fromisoformat(d) > freeze_d:
            violations.append(
                f"source dated {d} > freeze {freeze}: {s.get('fact', '?')[:80]}"
            )
    return violations


def packet_year_audit(packet: dict) -> list[str]:
    """No calendar years may appear inside the analyst_reports (decontamination c)."""
    text = json.dumps(packet.get("analyst_reports", []), ensure_ascii=False)
    return sorted({m.group(0) for m in YEAR_RE.finditer(text)})


def output_audit(packet: dict, response_text: str) -> dict:
    """Contamination check on the fleet's output.

    Terms that appear in the PROMPT we sent (packet text, positions,
    constraints) are prompt echoes, not training-data leakage — excluded.
    """
    prompt_text = (
        json.dumps(packet.get("analyst_reports", []), ensure_ascii=False)
        + " " + (packet.get("positions") or "")
        + " " + build_constraints(packet)
    )
    hits: list[str] = []
    for term in packet.get("contamination_terms", []):
        pat = r"(?i)\b" + re.escape(term) + r"\b"
        if re.search(pat, response_text) and not re.search(pat, prompt_text):
            hits.append(term)
    suspects: list[str] = []
    for term in GLOBAL_SUSPECT_TERMS:
        pat = r"(?i)\b" + re.escape(term.strip()) + r"\b"
        if re.search(pat, response_text) and not re.search(pat, prompt_text):
            suspects.append(term.strip())
    years = sorted({m.group(0) for m in YEAR_RE.finditer(response_text)})
    return {
        "contamination_hits": hits,
        "suspect_terms": suspects,
        "calendar_years_in_output": years,
        "contaminated": bool(hits),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def load_packets(only: list[str] | None) -> list[dict]:
    packets = []
    for p in sorted(PACKETS_DIR.glob("*.json")):
        pkt = json.loads(p.read_text(encoding="utf-8"))
        pkt["_file"] = p.name
        if only and pkt["case_id"] not in only:
            continue
        packets.append(pkt)
    if only:
        order = {cid: i for i, cid in enumerate(only)}
        packets.sort(key=lambda pk: order.get(pk["case_id"], 999))
    else:
        packets.sort(key=lambda pk: (pk.get("run_order", 50), pk["case_id"]))
    return packets


def ensure_output_path_writable(out_path: Path, *, dry_run: bool) -> None:
    """Refuse to mutate a run once its score report exists."""
    if dry_run:
        return
    report_path = out_path.with_name(out_path.stem + "_report.md")
    if report_path.exists():
        raise RuntimeError(
            f"scored run is immutable: {out_path} has report {report_path.name}; "
            "choose a new --out path"
        )


def dry_run_exit_code(run_doc: dict[str, Any]) -> int:
    return (
        2
        if any(
            result.get("status") == "dry_blocked_classifier"
            for result in run_doc.get("results", [])
        )
        else 0
    )


def build_constraints(packet: dict) -> str:
    c = USER_CONSTRAINTS
    if packet.get("constraints_extra") == "exit_rule":
        c += EXIT_RULE
    c += CLOCK_RULE
    return c


async def run_point(packet: dict) -> dict:
    from argosy.agents.trader import TraderAgent  # deferred: import cost

    agent = TraderAgent(user_id="ariel", tier="T2")
    report = await agent.run(
        analyst_reports=packet["analyst_reports"],
        debate_outcome={},
        positions_snapshot=packet["positions"],
        user_constraints=build_constraints(packet),
        tier="T2",
        mode="long_hold",
        ticker=packet["alias"],
    )
    out = report.output.model_dump()
    return {
        "status": "ok",
        "model": report.model,
        "action": out.get("action"),
        "confidence": out.get("confidence"),
        "size": out.get("size_shares_or_currency"),
        "size_units": out.get("size_units"),
        "rationale_summary": out.get("rationale_summary"),
        "cited_sources": out.get("cited_sources"),
        "tokens_in": report.tokens_in,
        "tokens_out": report.tokens_out,
        # Full replay trail for an INDEPENDENT auditor (owner requirement
        # 2026-07-11): the exact constraints string the trader received and
        # the complete raw model output — so a checker agent can verify the
        # reasoning used only packet facts (no real names, no post-freeze
        # knowledge) without trusting the summary. The packet itself is the
        # committed fixture; together these three reproduce the whole call.
        "constraints_rendered": build_constraints(packet),
        "response_raw": report.response_text,
        "output_full": out,
    }


def load_persisted_result(out_path: Path, case_id: str) -> dict[str, Any]:
    """Reload one result from the durable replay trail."""
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    return next(r for r in reversed(doc["results"]) if r["case_id"] == case_id)


def load_classifier_receipt(
    packet: dict[str, Any],
    *,
    receipts_dir: Path = CLASSIFIER_RECEIPTS_DIR,
) -> dict[str, Any] | None:
    path = receipts_dir / f"{packet['case_id']}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def classifier_receipt_preflight(
    packet: dict[str, Any],
    receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    if receipt is None:
        return {"ok": False, "mismatches": [{"field": "receipt", "actual": "missing"}]}
    return verify_classifier(packet, receipt)


def supersede_prior_attempts(run_doc: dict[str, Any], case_id: str) -> None:
    """Close failed prior attempts before appending a retry for the same point."""
    for result in run_doc.get("results", []):
        if (
            result.get("case_id") == case_id
            and result.get("status") not in {
                "ok",
                "disqualified_temporal",
                "superseded_retry",
            }
        ):
            result["superseded_status"] = result.get("status")
            result["status"] = "superseded_retry"


async def execute_live_point(
    packet: dict[str, Any],
    base: dict[str, Any],
    *,
    run_doc: dict[str, Any],
    out_path: Path,
    persist: Callable[[], None],
    constraints_rendered: str | None = None,
    classifier_receipt: dict[str, Any] | None = None,
    sanitizer_runner: Callable[
        [dict[str, Any], str], Awaitable[dict[str, Any]]
    ] = run_sanitizer,
    trader_runner: Callable[
        [dict[str, Any]], Awaitable[dict[str, Any]]
    ] = run_point,
    review_runner: Callable[
        [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]
    ] = run_review,
    grading_runner: Callable[
        [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]
    ] = run_grading,
) -> dict[str, Any]:
    """Run stages 1-5 with a durable reload at every agent boundary."""
    rendered = constraints_rendered or build_constraints(packet)
    base["packet_snapshot"] = {
        "case_id": packet["case_id"],
        "alias": packet["alias"],
        "analyst_reports": packet["analyst_reports"],
        "positions": packet["positions"],
        "constraints_rendered": rendered,
        "freeze_date": packet.get("freeze_date"),
        "sources": packet.get("sources") or [],
        "trader_call": {
            "debate_outcome": {},
            "tier": "T2",
            "mode": "long_hold",
            "ticker": packet["alias"],
        },
    }
    base["agent_pipeline"] = {}
    classifier = dict(classifier_receipt or {
        "stage": 1,
        "agent_role": "calibration_classifier_sourcing",
        "status": "missing",
        "output": {},
    })
    classifier["verification"] = verify_classifier(packet, classifier)
    base["agent_pipeline"]["classifier_data_sourcing"] = classifier
    supersede_prior_attempts(run_doc, packet["case_id"])
    run_doc["results"].append(base)
    persist()
    if not classifier["verification"]["ok"]:
        base["status"] = "disqualified_classifier"
        persist()
        return base

    packet_with_receipt = dict(packet)
    packet_with_receipt["_classifier_receipt"] = classifier
    sanitizer = await sanitizer_runner(packet_with_receipt, rendered)
    sanitizer["verification"] = verify_sanitizer(packet, sanitizer)
    base["agent_pipeline"]["sanitizer"] = sanitizer
    persist()

    if not sanitizer["verification"]["ok"]:
        base["status"] = "disqualified_sanitizer"
        persist()
        return base

    result: dict[str, Any] | None = None
    for attempt in (1, 2):
        try:
            result = await trader_runner(packet)
            break
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "error",
                "attempt": attempt,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
            }
    assert result is not None
    base.update(result)
    if result.get("status") != "ok":
        persist()
        return base

    response_text = result.get("response_raw") or (
        (result.get("rationale_summary") or "")
        + " "
        + " ".join(result.get("cited_sources") or [])
    )
    base["output_audit"] = output_audit(packet, response_text)
    persist()

    def reload_replay() -> dict[str, Any]:
        return load_persisted_result(out_path, packet["case_id"])

    def persist_stage(stage: str, receipt: dict[str, Any]) -> None:
        base["agent_pipeline"][stage] = receipt
        persist()

    await run_replay_pipeline(
        packet,
        load_replay=reload_replay,
        persist_stage=persist_stage,
        review_runner=review_runner,
        grading_runner=grading_runner,
    )
    return base


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated case_ids", default=None)
    ap.add_argument("--dry-run", action="store_true", help="audits only, no LLM calls")
    ap.add_argument("--out", default=None, help="runs file path (default runs/<date>.json)")
    args = ap.parse_args()

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    packets = load_packets(only)
    if not packets:
        print("no packets matched", flush=True)
        return

    out_path = (
        Path(args.out)
        if args.out
        else RUNS_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    )
    ensure_output_path_writable(out_path, dry_run=args.dry_run)
    RUNS_DIR.mkdir(exist_ok=True)
    run_doc: dict = {
        "started": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": 1,
        "results": [],
    }
    if out_path.exists() and not args.dry_run:
        run_doc = json.loads(out_path.read_text(encoding="utf-8"))
        run_doc.setdefault("pipeline_version", 1)
        done = {r["case_id"] for r in run_doc["results"] if r.get("status") in ("ok", "disqualified_temporal")}
        packets = [p for p in packets if p["case_id"] not in done]
        print(f"resuming: {len(done)} points already in {out_path.name}", flush=True)

    def persist() -> None:
        if args.dry_run:
            return  # audits only — never touch the runs file
        out_path.write_text(
            json.dumps(run_doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    for pkt in packets:
        cid = pkt["case_id"]
        violations = temporal_audit(pkt)
        pkt_years = packet_year_audit(pkt)
        base = {
            "case_id": cid,
            "alias": pkt["alias"],
            "category": pkt["category"],
            "grading": pkt.get("grading"),
            "real": pkt["real"],
            "freeze_date": pkt.get("freeze_date"),
            "temporal_violations": violations,
            "packet_calendar_years": pkt_years,
        }
        classifier_receipt = load_classifier_receipt(pkt)
        base["classifier_receipt_available"] = classifier_receipt is not None
        classifier_preflight = classifier_receipt_preflight(pkt, classifier_receipt)
        base["classifier_receipt_preflight"] = classifier_preflight
        if violations:
            base["status"] = "disqualified_temporal"
            run_doc["results"].append(base)
            persist()
            print(f"{cid}: DISQUALIFIED (temporal) - {violations[0]}", flush=True)
            continue
        if pkt_years:
            print(f"{cid}: WARNING calendar years in packet: {pkt_years}", flush=True)
        if args.dry_run:
            base["status"] = (
                "dry_ok" if classifier_preflight["ok"]
                else "dry_blocked_classifier"
            )
            run_doc["results"].append(base)
            suffix = (
                "audits pass (dry run)"
                if classifier_preflight["ok"]
                else "BLOCKED (dry run) - classifier receipt missing or invalid"
            )
            print(f"{cid}: {suffix}", flush=True)
            continue

        print(f"running {cid} ({pkt['category']}/{pkt.get('grading')})...", flush=True)
        try:
            await execute_live_point(
                pkt,
                base,
                run_doc=run_doc,
                out_path=out_path,
                persist=persist,
                classifier_receipt=classifier_receipt,
            )
        except Exception as exc:  # noqa: BLE001
            if not any(r is base for r in run_doc["results"]):
                run_doc["results"].append(base)
            base.update({
                "status": "pipeline_error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:],
            })
            persist()
        flag = ""
        if base.get("output_audit", {}).get("contaminated"):
            flag = " [CONTAMINATED]"
        elif base.get("output_audit", {}).get("suspect_terms"):
            flag = f" [suspect: {base['output_audit']['suspect_terms']}]"
        print(
            f"  {cid}: {base.get('action', base.get('error'))} "
            f"conf={base.get('confidence')}{flag}",
            flush=True,
        )

    run_doc["finished"] = datetime.now(timezone.utc).isoformat()
    persist()
    print(f"done -> {out_path}", flush=True)
    if args.dry_run:
        raise SystemExit(dry_run_exit_code(run_doc))


if __name__ == "__main__":
    asyncio.run(main())
