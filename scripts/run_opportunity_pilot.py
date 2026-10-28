"""Real current-market research pilot; forecasts only, no orders/approvals/fills.

Uses live discovery research plus an independently blind challenger. Raw reports,
prompts, reconciliation, policy version and future evaluation clocks are retained.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from argosy.agents.opportunity_pilot import (  # noqa: E402
    OpportunityChallengerAgent,
    OpportunityEvidenceAgent,
    OpportunityResolutionAgent,
    OpportunitySelectorAgent,
)
from argosy.services.after_tax import (  # noqa: E402
    SaleTaxPolicy,
    TaxLotInput,
    calculate_after_tax_sale,
)
from argosy.services.agent_report_persistence import persist_agent_report_async  # noqa: E402
from argosy.services.discovery_grader import grade_discovery_ticker  # noqa: E402
from argosy.services.high_potential_sleeve import MOONSHOT_MANDATE_VERSION  # noqa: E402
from argosy.services.predictions.writers import write_discovery_evaluation_predictions  # noqa: E402
from argosy.services.recommendation_history import analysis_database_path, read_engine  # noqa: E402
from argosy.state.db import create_sync_engine  # noqa: E402
from argosy.state.models import AgentReport  # noqa: E402


def _json(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _evidence(report):
    return {"report_id": getattr(report, "id", None), "role": report.agent_role, "model": report.model,
            "created_at": report.created_at.isoformat(), "response_text": report.response_text,
            "system_prompt": report.system_prompt, "user_prompt": report.user_prompt,
            "prompt_hash": report.prompt_hash, "sources_json": getattr(report, "sources_json", None)}


def hydrate_report(db, saved, user_id, role, run_id=None):
    row = db.get(AgentReport, saved["report_id"])
    if (row is None or row.user_id != user_id or row.agent_role != role
            or (run_id is not None and str(row.decision_id) != str(run_id))
            or any(getattr(row, k) != saved[k] for k in
                   ("response_text", "prompt_hash", "system_prompt", "user_prompt"))):
        raise ValueError("Parent report ownership, role, run or content mismatch")
    return _evidence(row)


def verified_parent(parent, user_id):
    """Hydrate all model evidence from owned stored reports, never trust embedded IDs."""
    if parent.get("user_id") not in (None, user_id):
        raise ValueError("Parent user mismatch")
    engine = read_engine()
    try:
        with Session(engine) as db:
            db.connection().exec_driver_sql("BEGIN")
            def owned(saved, role, run_id=None):
                return hydrate_report(db, saved, user_id, role, run_id)
            owned(parent["agenda"], "opportunity_pilot_selector")
            for item in parent["results"]:
                run_id = item["decision_run_id"]
                if run_id is None or not str(run_id).strip():
                    raise ValueError("Parent research run ID is required")
                final = owned(item["final_evidence"], "opportunity_pilot_resolution", run_id)
                item["final_view"] = OpportunityResolutionAgent(user_id=user_id)._parse_output(
                    final["response_text"]).model_dump(mode="json")
                item["final_report_id"] = final["report_id"]
                item["grader"] = owned(item["grader"], "discovery_grader", run_id)
                item["raw_reports"] = [owned(r, r["role"], run_id) for r in item["raw_reports"]]
                if any(r["role"] not in ("fundamentals", "news", "macro", "sentiment") for r in item["raw_reports"]):
                    raise ValueError("Unexpected parent analyst role")
            # Legacy sealed pilots predate explicit user_id. Ownership above is
            # established from every stored report, never inferred from CLI defaults.
            parent["user_id"] = user_id
            return parent
    finally:
        engine.dispose()


def sources_for(marker: str, user_id: str) -> tuple[dict, list[dict]]:
    engine = read_engine()
    try:
        with Session(engine) as db:
            db.connection().exec_driver_sql("BEGIN")
            grades = list(db.scalars(select(AgentReport).where(
                AgentReport.user_id == user_id, AgentReport.agent_role == "discovery_grader",
                AgentReport.user_prompt.contains(marker),
            )))
            if len(grades) != 1:
                raise ValueError("Cannot establish exact pilot-to-research lineage")
            run_id = grades[0].decision_id
            reports = [_evidence(r)
                       for r in db.scalars(select(AgentReport).where(
                           AgentReport.user_id == user_id, AgentReport.decision_id == run_id,
                           AgentReport.agent_role.in_(("fundamentals", "news", "macro", "sentiment")),
                       ).order_by(AgentReport.id))]
            if not reports:
                raise ValueError("Primary analyst reports missing from pilot research")
            return {**_evidence(grades[0]), "decision_run_id": run_id}, reports
    finally:
        engine.dispose()


def reports_for_run(run_id: str, user_id: str) -> list[dict]:
    engine = read_engine()
    try:
        with Session(engine) as db:
            return [_evidence(r) for r in db.scalars(select(AgentReport).where(
                AgentReport.user_id == user_id, AgentReport.decision_id == str(run_id),
            ).order_by(AgentReport.id))]
    finally:
        engine.dispose()


def persist_clocks(user_id, pilot_id, ticker, output, context, report_id):
    analysis_database_path()
    engine = create_sync_engine()
    try:
        with Session(engine) as db:
            rows = write_discovery_evaluation_predictions(
                db, user_id, event_key=f"opportunity_pilot:{pilot_id}:{ticker}", ticker=ticker,
                verdict=output.verdict, conviction=output.conviction, event_at=datetime.now(UTC),
                entry_price=None, entry_price_policy="post_decision_bar", mandate_version=MOONSHOT_MANDATE_VERSION,
                estimator_go=None, estimator_conviction=None, estimation="Fleet-selected current research pilot; not a new quick screen",
                thesis=output.model_dump_json(), radar_rank=None, radar_score=None,
                research_context={**context, "pilot_id": pilot_id, "final_report_id": report_id},
            )
            db.commit()
            return [r.id for r in rows]
    finally:
        engine.dispose()


async def run(args):
    database_path = analysis_database_path()
    if args.out.exists() or args.out.with_suffix(".seal.json").exists():
        raise ValueError("Pilot artifacts are write-once; select a new --out")
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
        discovery = client.get("/api/portfolio/discovery", params={"user_id": args.user_id})
        portfolio = client.get("/api/portfolio/snapshot", params={"user_id": args.user_id})
        discovery.raise_for_status()
        portfolio.raise_for_status()
    book = portfolio.json()
    context = {"snapshot_date": book["snapshot_date"], "book_degraded": book.get("book_degraded"),
               "total_snapshot_value_usd_k": book["total_usd_value_k"],
               "positions": [{k: p.get(k) for k in ("symbol", "sleeve", "usd_value_k", "mark_stale")}
                             for p in book["positions"] if p.get("symbol") != "-"],
               "market_data_warnings": book.get("market_data_warnings")}
    candidates = [{"ticker": r["ticker"], "radar_rank": r.get("rank"),
                   "screen": r.get("estimator"),
                   "last_grade": ({"verdict": r["fleet"]["verdict"], "conviction": r["fleet"]["conviction"],
                                   "summary": r["fleet"]["thesis_md"][:1600]} if r.get("fleet") else None)}
                  for r in discovery.json()["candidates"] if r["status"] == "active"]
    pilot_id = str(uuid.uuid4())
    doc = {"kind": "analysis_only_opportunity_pilot", "pilot_id": pilot_id, "user_id": args.user_id,
           "started_at": datetime.now(UTC).isoformat(), "mandate_version": MOONSHOT_MANDATE_VERSION,
           "database_target": str(database_path), "portfolio_context": context,
           "candidate_universe": candidates, "results": [], "status": "running"}
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
    def save():
        args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    agenda = await OpportunitySelectorAgent(user_id=args.user_id).run(payload={
        "as_of": datetime.now(UTC).isoformat(), "portfolio": context, "candidates": candidates,
    })
    agenda_id = await persist_agent_report_async(agenda)
    doc["agenda"] = {**_evidence(agenda), "report_id": agenda_id, **agenda.output.model_dump(mode="json")}
    symbols = [c.ticker.upper() for c in agenda.output.choices]
    if len(set(symbols)) != 3 or not set(symbols) <= {c["ticker"] for c in candidates}:
        raise ValueError("Research agenda must select three distinct active candidates")
    save()
    print(json.dumps({"stage": "agenda_selected", "choices": symbols}), flush=True)

    async def one(choice, row):
        symbol = choice.ticker.upper()
        marker = f"PILOT_RECEIPT:{pilot_id}:{symbol}"
        question = (f"{marker}\nAs of today independently investigate {symbol}: {choice.why_now}. "
                    f"Decisive question: {choice.decisive_question}. "
                    "Fetch dated primary issuer/SEC sources. Explicitly search for material POST-QUARTER "
                    "financing, debt, dilution, acquisitions and guidance updates; quarter-end cash/debt is "
                    "not today's balance sheet. Reconcile per-share/ADS units and all actual profit measures, "
                    "working-capital needs and current valuation. Build independent downside/base/upside "
                    "earnings/cash-flow cases with6/12/24month horizons. No current-price-derived-multiple "
                    "times the same EPS as independent valuation. Compare buy-now versus waiting, the "
                    "benchmark, and concentration overlap. No fixed multiple hurdle and no forced buy. "
                    "Record sources, dates, unresolved facts, falsifiers and next validation. Analysis only.")
        print(json.dumps({"stage": "fleet_research", "ticker": symbol}), flush=True)
        def opened(run_id):
            row.update(decision_run_id=str(run_id), research_marker=marker, research_question=question)
            save()
        pick = await grade_discovery_ticker(args.user_id, {"ticker": symbol}, research_question=question,
                                            on_open=opened)
        if pick is None:
            row.update(status="incomplete", reason="Research quorum failed")
            row["available_reports"] = await asyncio.to_thread(reports_for_run, row["decision_run_id"], args.user_id)
            save()
            return
        grader, reports = await asyncio.to_thread(sources_for, marker, args.user_id)
        run_id = grader["decision_run_id"]
        row.update(decision_run_id=run_id, raw_reports=reports, grader=grader, stage="blind_challenge")
        save()
        raw = {"ticker": symbol, "as_of": datetime.now(UTC).isoformat(),
               "portfolio": context, "raw_analyst_reports": reports}
        print(json.dumps({"stage": "blind_challenge", "ticker": symbol}), flush=True)
        blind = await OpportunityChallengerAgent(user_id=args.user_id).run(payload=raw)
        blind_id = await persist_agent_report_async(blind, decision_id=run_id)
        row.update(blind_report_id=blind_id, blind_evidence={**_evidence(blind), "report_id": blind_id},
                   blind_view=blind.output.model_dump(mode="json"), stage="resolution")
        save()
        final = await OpportunityResolutionAgent(user_id=args.user_id).run(payload={
            **raw, "discovery_grader": {"verdict": pick.verdict, "conviction": pick.conviction,
                                        "thesis": pick.thesis_md, "cites": list(pick.cites)},
            "blind_challenger": blind.output.model_dump(mode="json"),
        })
        final_id = await persist_agent_report_async(final, decision_id=run_id)
        row.update(final_report_id=final_id, final_evidence={**_evidence(final), "report_id": final_id},
                   final_view=final.output.model_dump(mode="json"), stage="clock_writing")
        save()
        if final.output.next_review_date < datetime.now(UTC).date():
            raise ValueError("Final review date precedes this live analysis")
        clocks = await asyncio.to_thread(persist_clocks, args.user_id, pilot_id, symbol, final.output,
                                         {"decision_run_id": run_id, "blind_report_id": blind_id}, final_id)
        row["prediction_ids"] = clocks
        save()
        tax_illustrations = []
        if final.output.reference_price_usd is not None:
            price = final.output.reference_price_usd
            for scenario in final.output.scenarios:
                after_tax = calculate_after_tax_sale(
                    gross_proceeds_usd=scenario.terminal_price_usd,
                    current_price_usd=scenario.terminal_price_usd,
                    lots=[TaxLotInput(lot_id="FICTIONAL_ONE_SHARE", quantity=1, cost_basis_usd=price)],
                    policy=SaleTaxPolicy(effective_tax_rate=.25, authoritative=False,
                                         method="Illustrative25pct_no_loss_credit_constant_FX",
                                         source="Owner-stated25pct illustration; not verified household tax basis"),
                    as_of=datetime.now(UTC), friction_usd=None,
                )
                tax_illustrations.append({**scenario.model_dump(mode="json"),
                    "gross_return_pct": (scenario.terminal_price_usd / price - 1) * 100,
                    "illustrative_net_return_pct": (after_tax.tax.net_proceeds_usd / price - 1) * 100,
                    "tax_layer": after_tax.model_dump(mode="json"),
                    "basis": "FICTIONAL one-share illustration, flat25% gains tax, constant FX, no loss credits; friction unresolved, NOT household after-tax P&L or actionable net proceeds"})
        print(json.dumps({"stage": "completed", "ticker": symbol, "verdict": final.output.verdict}), flush=True)
        row.update(status="completed", stage="completed", tax_illustrations=tax_illustrations)
        save()
    # Bounded real research; each completed name is saved independently.
    semaphore = asyncio.Semaphore(2)
    async def bounded(choice):
        async with semaphore:
            row = {"ticker": choice.ticker, "status": "running", "stage": "research"}
            doc["results"].append(row)
            save()
            try:
                await one(choice, row)
            except Exception as exc:
                row.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
                if row.get("decision_run_id"):
                    row["available_reports"] = await asyncio.to_thread(reports_for_run, row["decision_run_id"], args.user_id)
            save()
    await asyncio.gather(*(bounded(choice) for choice in agenda.output.choices))
    doc["status"] = "completed" if all(r["status"] == "completed" for r in doc["results"]) else "incomplete"
    doc["finished_at"] = datetime.now(UTC).isoformat()
    save()
    with args.out.with_suffix(".seal.json").open("x", encoding="utf-8") as handle:
        json.dump({"sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(), "status": doc["status"]}, handle)
    print(json.dumps({"status": doc["status"], "results": [{"ticker": r["ticker"], "status": r["status"],
                       "view": r.get("final_view")} for r in doc["results"]]}, default=str), flush=True)
    return int(doc["status"] != "completed")


async def followup(args):
    """One evidence-closing round over a sealed pilot; never rewrite its decisions."""
    analysis_database_path()
    parent_bytes = args.follow_up.read_bytes()
    parent_seal = json.loads(args.follow_up.with_suffix(".seal.json").read_text(encoding="utf-8"))
    parent_hash = hashlib.sha256(parent_bytes).hexdigest()
    if parent_seal["sha256"] != parent_hash:
        raise ValueError("Parent pilot seal mismatch")
    parent = json.loads(parent_bytes)
    if parent.get("kind") != "analysis_only_opportunity_pilot" or parent.get("status") != "completed":
        raise ValueError("Follow-up requires a completed original pilot, not another follow-up")
    if datetime.fromisoformat(parent["finished_at"]).date() != datetime.now(UTC).date():
        raise ValueError("A new day requires a fresh pilot price/portfolio snapshot")
    if parent["mandate_version"] != MOONSHOT_MANDATE_VERSION:
        raise ValueError("Parent user/policy mismatch")
    parent = verified_parent(parent, args.user_id)
    cached_evidence = {}
    evidence_cache_hash = None
    if args.reuse_evidence:
        cache_bytes = args.reuse_evidence.read_bytes()
        cache_seal = json.loads(args.reuse_evidence.with_suffix(".seal.json").read_text(encoding="utf-8"))
        evidence_cache_hash = hashlib.sha256(cache_bytes).hexdigest()
        cache = json.loads(cache_bytes)
        if (cache_seal["sha256"] != evidence_cache_hash or cache.get("parent_sha256") != parent_hash
                or cache.get("user_id") != args.user_id or cache.get("mandate_version") != MOONSHOT_MANDATE_VERSION):
            raise ValueError("Evidence cache seal, parent, user or policy mismatch")
        engine = read_engine()
        try:
            with Session(engine) as db:
                for item in cache["results"]:
                    prior = next(r for r in parent["results"] if r["ticker"] == item["ticker"])
                    if item.get("evidence_report"):
                        cached_evidence[item["ticker"]] = hydrate_report(
                            db, item["evidence_report"], args.user_id, "opportunity_pilot_evidence", prior["decision_run_id"])
        finally:
            engine.dispose()
    if args.out.exists() or args.out.with_suffix(".seal.json").exists():
        raise ValueError("Follow-up artifacts are write-once")
    doc = {"kind": "analysis_only_opportunity_followup", "pilot_id": str(uuid.uuid4()),
           "user_id": args.user_id, "parent_artifact": str(args.follow_up), "parent_sha256": parent_hash,
           "reused_evidence_sha256": evidence_cache_hash,
           "mandate_version": MOONSHOT_MANDATE_VERSION, "status": "running", "results": [],
           "started_at": datetime.now(UTC).isoformat(), "portfolio_context": parent["portfolio_context"]}
    def save():
        args.out.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(doc, handle)
    semaphore = asyncio.Semaphore(2)
    async def one(prior):
        async with semaphore:
            ticker = prior["ticker"]
            row = {"ticker": ticker, "decision_run_id": prior["decision_run_id"],
                   "prior_final_report_id": prior["final_report_id"], "stage": "closing_evidence_gaps",
                   "status": "running"}
            doc["results"].append(row)
            save()
            try:
                if not prior["final_view"]["unresolved_questions"]:
                    row.update(status="not_needed", stage="completed")
                    save()
                    return
                if ticker in cached_evidence:
                    evidence_row = cached_evidence[ticker]
                    evidence_output = OpportunityEvidenceAgent(user_id=args.user_id)._parse_output(evidence_row["response_text"])
                else:
                    evidence = await OpportunityEvidenceAgent(user_id=args.user_id).run(payload={
                        "ticker": ticker, "as_of": datetime.now(UTC).isoformat(),
                        "unresolved_questions": prior["final_view"]["unresolved_questions"],
                    })
                    evidence_id = await persist_agent_report_async(evidence, decision_id=prior["decision_run_id"])
                    evidence_row = {**_evidence(evidence), "report_id": evidence_id}
                    evidence_output = evidence.output
                evidence_id = evidence_row["report_id"]
                row.update(evidence_report=evidence_row,
                           stage="blind_challenge")
                save()
                raw = {"ticker": ticker, "as_of": datetime.now(UTC).isoformat(),
                       "portfolio": doc["portfolio_context"], "raw_analyst_reports": prior["raw_reports"],
                       "followup_evidence": evidence_output.model_dump(mode="json"),
                       "followup_retrieval_receipts": evidence_row["sources_json"],
                       "evidence_contract": "Findings are analyst assertions. Verify them against actual retrieval receipts; absence of inspected primary text means unresolved, even if the analyst labels it verified."}
                blind = await OpportunityChallengerAgent(user_id=args.user_id).run(payload=raw)
                blind_id = await persist_agent_report_async(blind, decision_id=prior["decision_run_id"])
                row.update(blind_evidence={**_evidence(blind), "report_id": blind_id}, stage="resolution")
                save()
                final = await OpportunityResolutionAgent(user_id=args.user_id).run(payload={
                    **raw, "discovery_grader": prior["grader"], "prior_final_view": prior["final_view"],
                    "blind_challenger": blind.output.model_dump(mode="json"),
                })
                final_id = await persist_agent_report_async(final, decision_id=prior["decision_run_id"])
                row.update(final_evidence={**_evidence(final), "report_id": final_id},
                           final_view=final.output.model_dump(mode="json"), stage="clock_writing")
                save()
                if final.output.next_review_date < datetime.now(UTC).date():
                    raise ValueError("Review date precedes follow-up")
                row["prediction_ids"] = await asyncio.to_thread(
                    persist_clocks, args.user_id, doc["pilot_id"], ticker, final.output,
                    {"parent_pilot_id": parent["pilot_id"], "parent_sha256": parent_hash,
                     "evidence_report_id": evidence_id, "blind_report_id": blind_id}, final_id)
                row.update(status="completed", stage="completed")
            except Exception as exc:
                row.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            save()
            print(json.dumps({"ticker": ticker, "status": row["status"],
                              "verdict": row.get("final_view", {}).get("verdict")}), flush=True)
    await asyncio.gather(*(one(r) for r in parent["results"]))
    doc["status"] = "completed" if all(r["status"] in ("completed", "not_needed") for r in doc["results"]) else "incomplete"
    doc["finished_at"] = datetime.now(UTC).isoformat()
    save()
    with args.out.with_suffix(".seal.json").open("x", encoding="utf-8") as handle:
        json.dump({"sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(), "status": doc["status"]}, handle)
    return int(doc["status"] != "completed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="ariel")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--follow-up", type=Path, help="One bounded evidence-closing round on a sealed pilot")
    parser.add_argument("--reuse-evidence", type=Path, help="Recover owned evidence from a sealed same-parent follow-up; judgments always run fresh")
    args = parser.parse_args()
    if args.reuse_evidence and not args.follow_up:
        parser.error("--reuse-evidence requires --follow-up")
    existed_before_start = args.out.exists()
    try:
        exit_code = asyncio.run(followup(args) if args.follow_up else run(args))
    except BaseException as exc:
        if not existed_before_start and args.out.exists() and not args.out.with_suffix(".seal.json").exists():
            partial = json.loads(args.out.read_text(encoding="utf-8"))
            partial.update(status="failed", failure=f"{type(exc).__name__}: {exc}",
                           finished_at=datetime.now(UTC).isoformat())
            args.out.write_text(json.dumps(partial, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            with args.out.with_suffix(".seal.json").open("x", encoding="utf-8") as handle:
                json.dump({"sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(), "status": "failed"}, handle)
        raise
    raise SystemExit(exit_code)
