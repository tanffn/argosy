"""Follow disputed discovery decisions without granting trade authority.

The daily directive owns this queue. Research refresh is not resolution: only
a subsequently reviewed allocation can close a task. Durable rows survive an
accepted/expired trade sheet and are swept when the scheduler next runs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select

from argosy.services.order_sheet import PendingResearch
from argosy.state.models import AllocationResearchTask, ScanState


def _key(tickers):
    return hashlib.sha256("|".join(sorted(tickers)).encode()).hexdigest()


def pending_tasks(db, user_id):
    return list(db.scalars(select(AllocationResearchTask).where(
        AllocationResearchTask.user_id == user_id,
        AllocationResearchTask.status == "pending",
    )).all())


def research_context(db, user_id):
    return [
        {**json.loads(row.payload_json), "issue_key": row.issue_key,
         "next_review_date": row.next_review_date.isoformat(),
         "last_error": row.last_error,
         "research_result": json.loads(row.result_json) if row.result_json else None}
        for row in pending_tasks(db, user_id)
    ]


def _is_fund(ticker):
    from argosy.services.instrument_reference import lookup, STRUCT_ETF, STRUCT_REIT, STRUCT_BOND
    ref = lookup(ticker)
    return ref is not None and ref.structure in {STRUCT_ETF, STRUCT_REIT, STRUCT_BOND}


def fund_research_evidence(tasks):
    """Fund alternatives retain their own source; never manufacture radar grades."""
    evidence = {}
    def sources(result):
        yield result
        for previous in result.get("prior_results", []):
            yield from sources(previous)
    for task in tasks:
        for result in sources(task.get("research_result") or {}):
            for ticker, item in (result.get("tickers") or {}).items():
                if item.get("kind") == "fund_vehicle" and _is_fund(ticker):
                    candidate = {**item, "as_of": result.get("as_of")}
                    if str(candidate["as_of"] or "") >= str(evidence.get(ticker, {}).get("as_of") or ""):
                        evidence[ticker] = candidate
    return evidence


def fund_comparison_grounded(comparison, evidence, now, freshness_days):
    if comparison is None or not evidence or not evidence.get("report_id"):
        return False
    try:
        fresh = datetime.fromisoformat(str(evidence["as_of"]))
        fresh = fresh.replace(tzinfo=UTC) if fresh.tzinfo is None else fresh
        observed = comparison.evidence_fresh_as_of
        observed = observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed
        return (0 <= (now - fresh).total_seconds() <= freshness_days * 86400
            and abs((observed - fresh).total_seconds()) <= 1
            and comparison.research_verdict == evidence.get("verdict")
            and comparison.research_conviction == evidence.get("conviction"))
    except (ValueError, TypeError, KeyError):
        return False


def _archive_lifecycle(row):
    """Keep prior states inline before terminal transitions or key reuse."""
    payload = json.loads(row.payload_json or "{}")
    history = payload.pop("lifecycle_history", [])
    history.append({
        "status": row.status, "source_fingerprint": row.source_fingerprint,
        "next_review_date": row.next_review_date, "updated_at": row.updated_at,
        "last_attempted_at": row.last_attempted_at, "last_error": row.last_error,
        "result_json": row.result_json, "payload": payload,
    })
    row.payload_json = json.dumps({**payload, "lifecycle_history": history}, default=str)


def sync_sheet_research(db, sheet, fingerprint):
    """Participates in the sheet-write transaction; no separate commit."""
    now = datetime.now(UTC)
    pending = {_key(item.tickers): item for item in sheet.pending_research}
    compared = {row.ticker for row in sheet.candidate_comparisons}
    still_pending = {s for item in sheet.pending_research for s in item.tickers}
    previous = list(pending_tasks(db, sheet.user_id))
    # Snapshot predecessor state before superseding groups. Splits/merges inherit
    # the oldest due date and evidence/error history, never a clean future clock.
    predecessors = [
        {"issue_key": row.issue_key, "tickers": json.loads(row.payload_json)["tickers"],
         "next_review_date": row.next_review_date, "last_attempted_at": row.last_attempted_at,
         "attempted_at_history": research_attempt_times(row),
         "last_error": row.last_error, "result_json": row.result_json}
        for row in previous
    ]
    for row in previous:
        old = PendingResearch.model_validate_json(row.payload_json)
        if row.issue_key not in pending and set(old.tickers) <= (compared | still_pending):
            resolved_tickers = set(old.tickers) - still_pending
            if resolved_tickers and not _grounded_resolution(db, sheet, resolved_tickers, now):
                raise ValueError("Research resolution requires current sourced comparisons and completed independent review")
            _archive_lifecycle(row)
            row.status = "superseded" if set(old.tickers) & still_pending else "resolved"
            row.updated_at = now
            row.source_fingerprint = fingerprint
    for issue_key, item in pending.items():
        inherited = [p for p in predecessors if p["issue_key"] != issue_key and set(p["tickers"]) & set(item.tickers)]
        row = db.get(AllocationResearchTask, (sheet.user_id, issue_key))
        if row is None:
            row = AllocationResearchTask(user_id=sheet.user_id, issue_key=issue_key)
            db.add(row)
            row.next_review_date = item.next_review_date
        elif row.status == "pending":
            # Repeated composition must not keep postponing an overdue issue.
            row.next_review_date = min(row.next_review_date, item.next_review_date)
        else:
            _archive_lifecycle(row)
            row.next_review_date = item.next_review_date
            row.last_attempted_at = None
            row.result_json = None
            row.last_error = None
        row.status = "pending"
        payload = item.model_dump(mode="json")
        prior_history = json.loads(row.payload_json or "{}").get("prior_tasks", [])
        lifecycle_history = json.loads(row.payload_json or "{}").get("lifecycle_history", [])
        if lifecycle_history:
            payload["lifecycle_history"] = lifecycle_history
        if prior_history:
            payload["prior_tasks"] = prior_history
        if inherited:
            row.next_review_date = min(row.next_review_date, *(p["next_review_date"] for p in inherited))
            payload["prior_tasks"] = prior_history + inherited
            errors = [p["last_error"] for p in inherited if p["last_error"]]
            if errors:
                row.last_error = "\n".join(errors)
            if not row.result_json:
                row.result_json = json.dumps({"prior_results": [
                    json.loads(p["result_json"]) for p in inherited if p["result_json"]
                ]})
        row.payload_json = json.dumps(payload, default=str)
        row.source_fingerprint = fingerprint
        row.updated_at = now


def _grounded_resolution(db, sheet, tickers, now):
    review = getattr(sheet, "review_resolution", None)
    if not review or not review.one_voice or review.reviewers_ran != review.reviewers_expected or not review.reviewers_ran:
        return False
    comparisons = {row.ticker: row for row in sheet.candidate_comparisons}
    fund_evidence = fund_research_evidence(research_context(db, sheet.user_id))
    for ticker in tickers:
        if _is_fund(ticker):
            if not fund_comparison_grounded(comparisons.get(ticker), fund_evidence.get(ticker), now, sheet.freshness_days):
                return False
            continue
        scan = db.get(ScanState, (sheet.user_id, ticker))
        comparison = comparisons.get(ticker)
        if scan is None or comparison is None or not scan.last_fleet_at:
            return False
        fresh = scan.last_fleet_at.replace(tzinfo=UTC) if scan.last_fleet_at.tzinfo is None else scan.last_fleet_at
        grade = json.loads(scan.fleet_json or "{}")
        observed = comparison.evidence_fresh_as_of
        observed = observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed
        if (not 0 <= (now - fresh).total_seconds() <= sheet.freshness_days * 86400
                or abs((observed - fresh).total_seconds()) > 1
                or comparison.research_verdict != grade.get("verdict")
                or comparison.research_conviction != grade.get("conviction")):
            return False
    return True


async def _research_fund(ticker, item, user_id):
    from argosy.agents.fund_vehicle_analyst import FundVehicleAnalystAgent
    from argosy.services.decision_funnel.fund_vehicle_decision import _build_fund_context, _load_domain_knowledge
    from argosy.services.agent_report_persistence import persist_agent_report_async
    from argosy.services.order_sheet_facts import collect_execution_facts
    from argosy.api.routes.portfolio import _load_current_doc_and_holdings
    doc, _, _ = await asyncio.to_thread(_load_current_doc_and_holdings, user_id)
    facts, failures = await asyncio.to_thread(collect_execution_facts, [ticker], doc=doc)
    if failures or ticker not in facts:
        raise ValueError(f"{ticker}: fund research facts unavailable: {failures}")
    context = await _build_fund_context(ticker, user_id=user_id)
    context["research_question"] = item.research_question + "\nMissing evidence: " + item.missing_evidence
    context["research_reserve_usd"] = item.reserved_usd
    context["live_execution_evidence"] = facts[ticker].evidence.model_dump(mode="json")
    context["domicile_country"] = facts[ticker].evidence.incorporation_country
    report = await FundVehicleAnalystAgent(user_id=user_id).run(
        ticker=ticker, fund_context=context, domain_knowledge=_load_domain_knowledge(),
    )
    report_id = await persist_agent_report_async(report)
    output = report.output
    conviction = str(getattr(output.conviction, "value", output.conviction)).upper()
    if not report_id or output.verdict.upper() not in {"HOLD", "TRIM", "SELL"} or conviction not in {"HIGH", "MEDIUM", "MED", "LOW"}:
        raise ValueError(f"{ticker}: fund analyst did not return a durable supported review")
    return {"kind": "fund_vehicle", "ticker": ticker, "verdict": output.verdict.upper(),
            "conviction": "MED" if conviction == "MEDIUM" else conviction,
            "report_id": report_id, "thesis_md": output.reasoning_md,
            "cites": output.cited_sources, "facts": context["live_execution_evidence"]}


def _research(item, user_id, *, completed=None, checkpoint=None):
    from argosy.services.discovery_grader import grade_discovery_ticker

    async def work():
        # Named evidence collection, not the top-K screen cache. Independent
        # raw-source analysts run again; the grader addresses the actual dispute.
        result = dict(completed or {})
        for ticker in item.tickers:
            if ticker in result:
                continue
            if _is_fund(ticker):
                result[ticker] = await _research_fund(ticker, item, user_id)
            else:
                pick = await grade_discovery_ticker(
                    user_id, {"ticker": ticker},
                    research_question=item.research_question + "\nMissing evidence: " + item.missing_evidence,
                )
                if pick is None:
                    raise RuntimeError(f"{ticker}: insufficient analyst quorum for pending research")
                result[ticker] = asdict(pick)
            if checkpoint:
                checkpoint(result)
        return result

    return asyncio.run(work())


def research_attempt_times(row):
    """Carry attempt history across lifecycle transitions AND group splits/merges."""
    from argosy.services.decision_readiness import utc
    stamps = set()
    def collect(payload):
        values = list(payload.get("attempted_at_history", []))
        if payload.get("last_attempted_at"):
            values.append(payload["last_attempted_at"])
        stamps.update(utc(datetime.fromisoformat(str(value))).isoformat() for value in values)
        for child in [*payload.get("lifecycle_history", []), *payload.get("prior_tasks", [])]:
            collect(child)
        if isinstance(payload.get("payload"), dict):
            collect(payload["payload"])
    collect(json.loads(row.payload_json or "{}"))
    if row.last_attempted_at:
        stamps.add(utc(row.last_attempted_at).isoformat())
    return sorted(stamps)


def research_retry_state(row, now):
    """Use durable attempt history and the scheduler's existing retry bounds."""
    from argosy.services.decision_readiness import MAX_SLOT_ATTEMPTS, RETRY_DELAY
    stamps = research_attempt_times(row)
    attempts = sum(datetime.fromisoformat(s).date() == now.date() for s in stamps)
    retry_at = datetime.fromisoformat(stamps[-1]) + RETRY_DELAY if stamps else now
    return {"attempts_today": attempts, "max_attempts": MAX_SLOT_ATTEMPTS,
            "automatic_retry": bool(row.last_error) and attempts < MAX_SLOT_ATTEMPTS,
            "next_retry_at": retry_at.isoformat() if attempts < MAX_SLOT_ATTEMPTS else None}


def refresh_due_research(db, user_id, *, now=None, research_fn=None, retry_failed=False):
    """Daily successful research; failed/interrupted work gets bounded recovery."""
    now = now or datetime.now(UTC)
    due = [row for row in pending_tasks(db, user_id) if row.next_review_date <= now.date()]
    out = {"due": len(due), "refreshed": [], "failures": [], "recovery": []}
    for row in due:
        retry = research_retry_state(row, now)
        automatic = (retry["automatic_retry"] and datetime.fromisoformat(retry["next_retry_at"]) <= now)
        if ((row.last_error and not (retry_failed or automatic)) or
                (not row.last_error and retry["attempts_today"] > 0)):
            if row.last_error:
                out["failures"].append(row.last_error)
                out["recovery"].append({"issue_key": row.issue_key, **retry})
            continue
        item = PendingResearch.model_validate_json(row.payload_json)
        input_hash = hashlib.sha256(item.model_dump_json().encode()).hexdigest()
        previous_result = json.loads(row.result_json or "{}")
        saved = previous_result.get("checkpoint", {})
        completed = (saved.get("tickers", {}) if saved.get("input_hash") == input_hash
                     and saved.get("as_of", "")[:10] == now.date().isoformat() else {})
        evidence_as_of = datetime.fromisoformat(saved["as_of"]) if completed else now
        if row.last_error:
            _archive_lifecycle(row)
        row.last_attempted_at = now
        # Persist an interrupted-attempt marker before long model calls.
        row.last_error = "Research attempt unfinished; eligible automatic recovery resumes after the cooldown."
        db.commit()
        def checkpoint(result):
            # Partial evidence is labelled and never promoted as a full task result.
            row.result_json = json.dumps({**previous_result, "checkpoint": {
                "input_hash": input_hash, "as_of": evidence_as_of.isoformat(), "tickers": result}})
            db.commit()
        try:
            result = (research_fn(item, user_id) if research_fn else
                      _research(item, user_id, completed=completed, checkpoint=checkpoint))
            if set(result) != set(item.tickers):
                raise ValueError("Research result does not cover every pending alternative")
            for ticker, pick in result.items():
                if _is_fund(ticker):
                    if pick.get("kind") != "fund_vehicle" or not pick.get("report_id"):
                        raise ValueError(f"{ticker}: missing fund-specific research receipt")
                    continue  # Saved in task evidence, not a moonshot ScanState.
                scan = db.get(ScanState, (user_id, ticker))
                if scan is None or scan.status not in {"active", "dropped"}:
                    raise ValueError(f"{ticker}: missing or quarantined discovery record; research not promoted")
                # Radar absence is not a research failure. An existing pending
                # question survives daily ranking churn; update its evidence,
                # never its radar membership, rank or screening timestamp.
                scan.fleet_json = json.dumps(pick)
                scan.last_fleet_at = evidence_as_of
                scan.updated_at = now
            row.result_json = json.dumps({"as_of": evidence_as_of.isoformat(), "tickers": result})
            row.last_error = None
            row.updated_at = now
            db.commit()
            out["refreshed"].append(row.issue_key)
        except Exception as exc:
            db.rollback()
            row.last_error = f"{', '.join(item.tickers)}: {exc}"
            row.updated_at = now
            db.commit()
            out["failures"].append(row.last_error)
            out["recovery"].append({"issue_key": row.issue_key, **research_retry_state(row, now)})
    return out
