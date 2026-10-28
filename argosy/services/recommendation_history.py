"""Read-only inventory and post-decision market diagnostics, not portfolio P&L.

Every saved call remains visible. Publication, shadow status and missing evidence
are separate from investment performance; repeated calls are not independent bets.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import String, create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from argosy.state.models import (
    ActionProposal,
    AgentReport,
    DecisionRun,
    Prediction,
    Proposal,
    Verdict,
)

METHOD = "matched_post_decision_adjusted_close_v1"
HORIZONS = (30, 90, 180, 365)
MAX_GAP_DAYS = 7
_CLOCK_SOURCES = {
    "signal_stream:discovery_evaluation": "discovery_research",
    "signal_stream:order_sheet": "order_sheet_clock",
}


def analysis_database_path() -> Path:
    """These operator tools support the configured shared DB, never split routing."""
    from argosy.config import get_settings
    from argosy.state.db import _per_tenant_mode
    if _per_tenant_mode():
        raise ValueError("Analysis CLI does not support mixed per-tenant routing; no research started")
    path = get_settings().db_file.resolve()
    if not path.is_file():
        raise ValueError("Configured analysis database does not exist")
    return path


def read_engine():
    return create_engine(URL.create("sqlite", database=f"file:{analysis_database_path().as_posix()}",
                                    query={"mode": "ro", "uri": "true"}))


def _object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _stamp(value: datetime) -> str:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat()


def _action(value: str) -> str:
    return {"ADD": "BUY", "TRIM": "SELL"}.get(value.upper(), value.upper())


def _clock_key(prediction, ref):
    event = re.sub(r"\|(30|180|365)d$", "", prediction.message_id or f"id:{prediction.id}")
    return (prediction.source, event, ref.get("mandate_version"),
            json.dumps(ref.get("research_context") or {}, sort_keys=True))


def _grade_output(report):
    from argosy.services.discovery_grader import DiscoveryGraderAgent, FleetGradeOutput
    for blob in report.blobs:
        if blob.key == "output_json":
            try:
                return FleetGradeOutput.model_validate_json(blob.value).model_dump()
            except ValueError:
                pass
    try:
        return DiscoveryGraderAgent(user_id=report.user_id)._parse_output(report.response_text).model_dump()
    except ValueError:
        return {}


def inventory(session: Session, *, user_id: str) -> dict:
    """Inventory original records, with exact lineage deduplication only.

    Rows are not filtered by execution, expiration, or outcome availability.
    Unlinked clocks are not silently promoted to published recommendations.
    """
    rows: list[dict] = []
    by_run: dict[tuple[int, str, str], dict] = {}
    ref_rows: dict[tuple, dict] = {}
    proposals = list(session.scalars(select(Proposal).where(Proposal.user_id == user_id)))
    for p in proposals:
        channel = "shadow" if p.shadow else "manual" if p.source == "manual" else "live_proposal"
        row = {
            "key": f"proposal:{p.id}", "ticker": p.ticker.upper(),
            "action": _action(p.action), "event_at": _stamp(p.created_at),
            "channel": channel, "source": p.source, "status": p.status,
            "shadow": bool(p.shadow), "lineage": [f"proposals:{p.id}"],
            "rationale": p.rationale_summary, "policy_version": "unversioned",
            "decision_run_id": p.decision_run_id,
            "publication_evidence": "proposal_record_not_proof_of_message_delivery",
        }
        rows.append(row)
        ref_rows[("proposal", p.id)] = row
        if p.decision_run_id is not None:
            by_run[(p.decision_run_id, p.ticker.upper(), _action(p.action))] = row

    for v in session.scalars(select(Verdict).where(Verdict.user_id == user_id)):
        key = (v.source_decision_run_id, v.subject.upper(), _action(v.verdict))
        linked = by_run.get(key)
        if linked is not None:
            linked["lineage"].append(f"verdicts:{v.id}")
            ref_rows[("verdict", v.id)] = linked
            continue
        rows.append({
            "key": f"verdict:{v.id}", "ticker": v.subject.upper(),
            "action": _action(v.verdict), "event_at": _stamp(v.created_at),
            "channel": "verdict_only", "source": "deep_decision_verdict",
            "status": "superseded" if v.superseded_by else "recorded",
            "shadow": None, "lineage": [f"verdicts:{v.id}"],
            "rationale": v.reasoning_md, "policy_version": "unversioned",
            "decision_run_id": v.source_decision_run_id,
            "publication_evidence": "not_established",
        })
        ref_rows[("verdict", v.id)] = rows[-1]

    # Include actual surfaced sheets, even when no outcome clocks were written.
    sheet_keys: set[tuple[int, str, str]] = set()
    quote_aliases: dict[str, list[dict]] = defaultdict(list)
    for a in session.scalars(select(ActionProposal).where(ActionProposal.user_id == user_id)):
        payload = _object(a.suggested_payload)
        sheet = payload.get("order_sheet")
        if not isinstance(sheet, dict):
            continue
        for index, line in enumerate(sheet.get("lines") or []):
            if not isinstance(line, dict) or not line.get("symbol") or not line.get("action"):
                continue
            symbol, action = str(line["symbol"]).upper(), _action(str(line["action"]))
            sheet_keys.add((a.id, symbol, action))
            evidence = line.get("evidence") or {}
            provider = re.search(r"\byfinance:([A-Za-z0-9.^=-]+)", str(evidence.get("price_source") or ""))
            if provider:
                quote_aliases[symbol].append({"provider_symbol": provider.group(1),
                                              "lineage": f"action_proposals:{a.id}:line:{index}",
                                              "venue": line.get("venue")})
            rows.append({
                "key": f"surfaced_sheet:{a.id}:{index}", "ticker": symbol,
                "action": action, "event_at": _stamp(a.surfaced_at),
                "channel": "surfaced_order_sheet", "source": "order_sheet",
                "status": a.status, "shadow": False,
                "lineage": [f"action_proposals:{a.id}:line:{index}"],
                "rationale": line.get("thesis"), "policy_version": "unversioned",
                "decision_run_id": None, "venue": line.get("venue"),
                "publication_evidence": "surfaced_at_recorded_not_a_fill",
            })
            ref_rows[("sheet", a.id, symbol, action)] = rows[-1]

    clocks: dict[tuple, dict] = {}
    for p in session.scalars(select(Prediction).where(
        Prediction.user_id == user_id, Prediction.source.in_(tuple(_CLOCK_SOURCES)),
    ).order_by(Prediction.id)):
        if not p.ticker:
            continue
        ref = _object(p.source_ref)
        action = _action(str(ref.get("verdict") or ref.get("action") or "UNKNOWN"))
        ticker = p.ticker.upper()
        if p.source == "signal_stream:order_sheet" and (ref.get("proposal_id"), ticker, action) in sheet_keys:
            continue
        key = _clock_key(p, ref)
        if key in clocks:
            clocks[key]["lineage"].append(f"predictions:{p.id}")
            continue
        row = {
            "key": f"clock_event:{p.id}", "ticker": ticker, "action": action,
            "event_at": _stamp(p.event_at), "channel": _CLOCK_SOURCES[p.source],
            "source": p.source, "status": "recorded", "shadow": None,
            "lineage": [f"predictions:{p.id}"],
            "rationale": ref.get("expectation"),
            "policy_version": ref.get("mandate_version") or "unversioned",
            "decision_run_id": ref.get("decision_run_id"),
            "research_context": ref.get("research_context") or {},
            "clock_event_key": key[1],
            "publication_evidence": "research_only" if p.source.endswith("discovery_evaluation") else "surviving_clock_only",
        }
        clocks[key] = row
        rows.append(row)

    # Preserve unclocked analysis-only grades too. Without exact lineage they
    # remain a separate, potentially overlapping channel, never pooled with clocks.
    from argosy.services.high_potential_sleeve import MOONSHOT_MANDATE_VERSION, X10_SLEEVE_MANDATE
    for report, run in session.execute(select(AgentReport, DecisionRun).join(
        DecisionRun, AgentReport.decision_id == DecisionRun.id.cast(String),
    ).where(AgentReport.user_id == user_id, DecisionRun.user_id == user_id,
            AgentReport.agent_role == "discovery_grader",
            DecisionRun.decision_kind == "discovery_research")):
        output = _grade_output(report)
        rows.append({
            "key": f"research_report:{report.id}", "ticker": run.ticker.upper(),
            "action": _action(str(output.get("verdict") or "UNKNOWN")),
            "event_at": _stamp(run.finished_at or report.created_at),
            "channel": "research_report", "source": "discovery_grader",
            "status": run.status, "shadow": None,
            "lineage": [f"agent_reports:{report.id}", f"decision_runs:{run.id}"],
            "rationale": output.get("thesis_md") or report.response_text,
            "policy_version": MOONSHOT_MANDATE_VERSION if X10_SLEEVE_MANDATE in (report.system_prompt or "") else "unversioned",
            "decision_run_id": run.id, "publication_evidence": "analysis_only_not_an_order",
        })

    # Show saved canonical evaluations alongside (not substituted for) the new
    # post-decision recomputation. Their contracts/entry conventions may differ.
    from argosy.services.predictions.outcomes import authoritative_outcomes
    predictions = list(session.scalars(select(Prediction).where(
        Prediction.user_id == user_id,
        Prediction.source.in_((*_CLOCK_SOURCES, "signal_stream:deep_decision_verdict", "signal_stream:proposal_recommendation")),
    )))
    canonical = authoritative_outcomes(session, predictions)
    for p in predictions:
        ref = _object(p.source_ref)
        if p.source == "signal_stream:deep_decision_verdict":
            row = ref_rows.get(("verdict", ref.get("verdict_id")))
        elif p.source == "signal_stream:proposal_recommendation":
            row = ref_rows.get(("proposal", ref.get("proposal_id")))
        elif p.source == "signal_stream:order_sheet":
            row = ref_rows.get(("sheet", ref.get("proposal_id"), (p.ticker or "").upper(), _action(str(ref.get("action") or "UNKNOWN"))))
            row = row or clocks.get(_clock_key(p, ref))
        else:
            row = clocks.get(_clock_key(p, ref))
        if row is None:
            key = _clock_key(p, ref)
            row = clocks.get(key)
            if row is None:
                row = {"key": f"unlinked_clock:{p.id}", "ticker": (p.ticker or "UNKNOWN").upper(),
                       "action": _action(str(ref.get("action") or ref.get("verdict") or "UNKNOWN")),
                       "event_at": _stamp(p.event_at), "channel": "unlinked_clock",
                       "source": p.source, "status": "parent_not_found", "shadow": None,
                       "lineage": [], "rationale": ref.get("expectation"),
                       "policy_version": ref.get("mandate_version") or "unversioned",
                       "research_context": ref.get("research_context") or {},
                       "publication_evidence": "unknown", "clock_event_key": key[1]}
                clocks[key] = row
                rows.append(row)
            row["lineage"].append(f"predictions:{p.id}")
        selected = canonical.get(p.id)
        outcome = selected[1] if selected else None
        row.setdefault("canonical_saved_outcomes", []).append({
            "prediction_id": p.id, "horizon_days": p.timeframe_days,
            "method": outcome.evaluation_method if outcome else p.evaluation_method,
            "outcome_kind": outcome.outcome_kind if outcome else "not_scored",
            "direction_adjusted_pnl_decimal": float(outcome.pnl_pct) if outcome and outcome.pnl_pct is not None else None,
            "basis": "Original versioned evaluator; not pooled with matched post-decision diagnostics",
        })

    rows.sort(key=lambda r: (r["event_at"], r["key"]))
    # Predeclared first-call cohort per channel/policy/direction. This does not
    # pretend that repeated daily recommendations are independent observations.
    first: dict[tuple, str] = {}
    for row in rows:
        key = (row["channel"], row["policy_version"], row["ticker"], row["action"])
        row["first_call_key"] = first.setdefault(key, row["key"])
        row["first_call_in_cohort"] = first[key] == row["key"]
    return {
        "schema_version": 1, "user_id": user_id, "calls": rows,
        "provider_alias_evidence": dict(quote_aliases),
        "coverage": {"call_events": len(rows), "unique_tickers": len({r["ticker"] for r in rows}),
                     "by_channel": dict(Counter(r["channel"] for r in rows)),
                     "oldest_event_at": rows[0]["event_at"] if rows else None},
        "scope": "All proposals, verdicts, surfaced-sheet lines, surviving discovery/sheet clocks and discovery-research grader reports; not unsaved prose or every notification. Cross-channel representations may overlap and are not pooled.",
    }


def compare_window(*, event_at: str, days: int, subject: dict[date, float],
                   benchmark: dict[date, float], as_of: date,
                   exchange_timezone: str = "America/New_York") -> dict:
    if days not in HORIZONS:
        raise ValueError("Unsupported audit horizon")
    if "T" in event_at or " " in event_at:
        timestamp = datetime.fromisoformat(event_at)
        timestamp = timestamp.replace(tzinfo=timestamp.tzinfo or UTC)
        freeze = timestamp.astimezone(ZoneInfo(exchange_timezone)).date()
    else:
        freeze = date.fromisoformat(event_at)
    target = freeze + timedelta(days=days)
    out = {"horizon_days": days, "target_date": str(target), "status": "not_due",
           "subject_return_pct": None, "benchmark_return_pct": None,
           "after_tax_return_pct": None}
    if target > as_of:
        return out
    def valid(bars):
        return {d: float(p) for d, p in bars.items() if isinstance(p, (float, int))
                and not isinstance(p, bool) and math.isfinite(p) and p > 0 and d <= as_of}
    subject, benchmark = valid(subject), valid(benchmark)
    common = sorted(set(subject) & set(benchmark))
    entries = [d for d in common if freeze < d <= freeze + timedelta(days=MAX_GAP_DAYS)]
    exits = [d for d in common if target - timedelta(days=MAX_GAP_DAYS) <= d <= target]
    if not entries or not exits or exits[-1] <= entries[0]:
        return {**out, "status": "missing_price_data", "reason": "No timely matched post-decision entry/exit; delisted is not zero."}
    start, end = entries[0], exits[-1]
    stock_return = (subject[end] / subject[start] - 1) * 100
    spy_return = (benchmark[end] / benchmark[start] - 1) * 100
    return {**out, "status": "available", "entry_date": str(start), "exit_date": str(end),
            "subject_entry_close": subject[start], "subject_exit_close": subject[end],
            "benchmark_entry_close": benchmark[start], "benchmark_exit_close": benchmark[end],
            "subject_return_pct": stock_return, "benchmark_return_pct": spy_return,
            "buy_vs_spy_pp": stock_return - spy_return,
            "sell_to_spy_vs_hold_pp": spy_return - stock_return,
            "sell_to_zero_yield_cash_vs_hold_pp": -stock_return}


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        if not row["first_call_in_cohort"]:
            continue
        for outcome in row.get("outcomes", []):
            groups[(row["channel"], row["policy_version"], row["action"], outcome["horizon_days"])].append(outcome)
    result = []
    for (channel, version, action, days), outcomes in sorted(groups.items()):
        scored = [o for o in outcomes if o["status"] == "available"]
        def mean(key, sample=scored):
            return sum(o[key] for o in sample) / len(sample) if sample else None
        result.append({"channel": channel, "policy_version": version, "action": action,
                       "horizon_days": days, "first_calls": len(outcomes), "available": len(scored),
                       "status_counts": dict(Counter(o["status"] for o in outcomes)),
                       "mean_underlying_return_pct": mean("subject_return_pct"),
                       "mean_spy_return_pct": mean("benchmark_return_pct"),
                       "mean_buy_excess_pp": mean("buy_vs_spy_pp") if action == "BUY" else None,
                       "mean_sell_to_spy_vs_hold_pp": mean("sell_to_spy_vs_hold_pp") if action == "SELL" else None,
                       "rose": sum(o["subject_return_pct"] > 0 for o in scored),
                       "fell": sum(o["subject_return_pct"] < 0 for o in scored)})
    return result


def seal_inventory(document: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
