"""Read-only frozen audit population. No outcome-based selection or order writes."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from structlog.testing import CapturingLogger

from argosy.agents.quick_estimator import QuickEstimatorAgent
from argosy.services.discovery_grader import DiscoveryGraderAgent
from argosy.state.models import AgentReport

TUNING_PROTOCOL = "archived-evidence-delivery-v1"
SEED = "argosy-evidence-delivery-oct23-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def record(row):
    return {"report_id": row.id, "user_id": row.user_id, "role": row.agent_role,
            "decision_run_id": row.decision_id,
            "created_at": row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC).isoformat(),
            "model": row.model, "system_prompt": row.system_prompt, "user_prompt": row.user_prompt,
            "response_text": row.response_text, "sources_json": row.sources_json,
            "prompt_hash": row.prompt_hash, "run_correlation_id": row.run_correlation_id}


def hash_order(row):
    return digest([SEED, row["report_id"]])


def partition(at, boundaries):
    return ("development" if boundaries[0] <= at < boundaries[1] else
            "reserved" if boundaries[1] <= at < boundaries[2] else "outside")


def build_tuning_snapshot(db, *, user_id, packet_date, boundaries):
    if len(boundaries) != 3 or not boundaries[0] < boundaries[1] < boundaries[2]:
        raise ValueError("Three increasing half-open UTC date boundaries required")
    screen_agent = QuickEstimatorAgent(user_id=user_id)
    grade_agent = DiscoveryGraderAgent(user_id=user_id)
    screen_agent._log = CapturingLogger()
    grade_agent._log = CapturingLogger()
    all_rows = list(db.scalars(select(AgentReport).where(
        AgentReport.user_id == user_id,
        AgentReport.agent_role.in_(("quick_estimator", "discovery_grader")),
    ).order_by(AgentReport.created_at, AgentReport.id)))
    first_screen = {}
    first_grade = {}
    current = []
    ledger = []
    raw = {}
    templates = {}
    malformed = []
    grader_rows = []
    for row in all_rows:
        entry = record(row)
        template_id = digest(entry["system_prompt"])
        templates[template_id] = entry.pop("system_prompt")
        entry["system_prompt_id"] = template_id
        entry["record_sha256"] = digest(entry)
        agent = screen_agent if row.agent_role == "quick_estimator" else grade_agent
        try:
            parsed = agent._parse_output(row.response_text).model_dump(mode="json")
            entry["parsed"] = parsed
            entry["ticker"] = parsed["ticker"]
        except Exception as exc:
            match = re.search(r"(?:^|\s)ticker=([A-Za-z0-9.^=-]+)(?:\s|$)", row.user_prompt or "")
            entry.update(parsed=None, ticker=match.group(1).upper() if match else None,
                         parse_error=type(exc).__name__)
            malformed.append(entry["report_id"])
        ledger.append({k: entry.get(k) for k in (
            "report_id", "role", "ticker", "created_at", "record_sha256", "parse_error")})
        if row.agent_role == "quick_estimator":
            if entry["ticker"]:
                first_screen.setdefault(entry["ticker"], entry)
            if "NO fixed 5x or 10x" in (row.system_prompt or ""):
                current.append(entry)
        else:
            if entry["ticker"]:
                first_grade.setdefault(entry["ticker"], entry)
            if row.created_at.date() == packet_date:
                grader_rows.append((row, entry))
        if entry["parsed"] is None:
            raw[str(row.id)] = entry
    for entry in [*first_screen.values(), *current, *first_grade.values()]:
        raw[str(entry["report_id"])] = entry
    firsts = []
    for entry in first_screen.values():
        firsts.append({"report_id": entry["report_id"], "ticker": entry["ticker"],
                       "partition": partition(entry["created_at"][:10], boundaries),
                       "created_at": entry["created_at"], "go": entry["parsed"]["go"] if entry["parsed"] else None})
    packets = []
    for row, entry in grader_rows:
        reports = list(db.scalars(select(AgentReport).where(
            AgentReport.user_id == user_id, AgentReport.decision_id == row.decision_id,
            AgentReport.created_at <= row.created_at,
            AgentReport.agent_role.in_(("fundamentals", "news", "macro", "sentiment")),
        ).order_by(AgentReport.id))) if row.decision_id else []
        evidence = [record(r) for r in reports]
        packets.append({"report_id": row.id, "ticker": entry["ticker"],
                        "decision_run_id": row.decision_id, "created_at": entry["created_at"],
                        "analyst_reports": evidence,
                        "legacy_evidence_characters": sum(min(4000, len(r.response_text)) for r in reports),
                        "complete_field_characters": sum(len(r.response_text) + len(r.sources_json or "") for r in reports),
                        "reports_longer_than_4000": sum(len(r.response_text) > 4000 for r in reports),
                        "reports_with_document_receipts": sum("public_document:" in (r.sources_json or "") for r in reports)})
    unique_packets = {}
    for packet in packets:
        # All rows remain above; repeats do not become independent cases.
        if packet["ticker"]:
            unique_packets.setdefault(packet["ticker"], packet)
    ordered = sorted(unique_packets.values(), key=hash_order)
    selected = [{"report_id": p["report_id"], "ticker": p["ticker"],
                 "partition": "development" if i < 4 else "reserved"} for i, p in enumerate(ordered[:8])]
    old_dev = [raw[str(r["report_id"])] for r in firsts if r["partition"] == "development"]
    old_rejects = sorted([r for r in old_dev if r["parsed"] and r["parsed"]["go"] is False], key=hash_order)[:8]
    old_accepts = sorted([r for r in old_dev if r["parsed"] and r["parsed"]["go"] is True], key=hash_order)[:4]
    current_rejects = [r for r in current if r["parsed"] and r["parsed"]["go"] is False]
    current_accepts = sorted([r for r in current if r["parsed"] and r["parsed"]["go"] is True], key=hash_order)[:6]
    audit_ids = list(dict.fromkeys(r["report_id"] for r in [*current_rejects, *current_accepts, *old_rejects, *old_accepts]))
    return {
        "protocol": TUNING_PROTOCOL, "user_id": user_id, "frozen_at": datetime.now(UTC).isoformat(),
        "selection": {"seed": SEED, "rule": "First archived screen per ticker globally, then half-open UTC date partition; independent hash ordering, no prices/returns accessed",
                      "screen_boundaries": boundaries, "packet_date": str(packet_date),
                      "paired_rule": "Earliest same-day grader per ticker; first4 hash-ranked development, next4 reserved; remaining population retained",
                      "identity_scope": "Ticker-disjoint only; historical corporate identity/share-class changes not independently resolved; do not claim company independence"},
        "templates": templates, "raw_records": raw, "archive_ledger": ledger,
        "first_screens": firsts, "current_policy_screen_ids": [r["report_id"] for r in current],
        "audit_screen_ids": audit_ids, "grader_packets": packets, "paired_selection": selected,
        "counts": {"all_screen_calls": sum(r["role"] == "quick_estimator" for r in ledger),
                   "unique_first_screen_tickers": len(firsts), "malformed_reports": len(malformed),
                   "first_screen_partitions": dict(Counter(r["partition"] for r in firsts)),
                   "current_policy_calls": len(current), "current_policy_investigate": sum(r["parsed"] is not None and r["parsed"]["go"] for r in current),
                   "current_policy_reject": len(current_rejects), "graded_packet_calls": len(packets),
                   "graded_unique_tickers": len(unique_packets), "audit_cases": len(audit_ids)},
        "malformed_report_ids": malformed,
        "limitations": ["Saved call archive only, not all radar opportunities or survivorship-free market coverage.",
                        "Malformed inputs and unknown identity remain explicit; absent reports are not rejections.",
                        "The paired test measures evidence fidelity, not BUY correctness, realized returns or alpha.",
                        "Known issuer names/model memory can contaminate retrospective conclusions; no current retrieval permitted.",
                        "Current mutable scan state is not substituted for missing historical inputs."],
    }
