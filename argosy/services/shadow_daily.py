"""Scheduled shadow-ledger reconciliation, with no broker/approval mutations."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from argosy.services.shadow_portfolio import (
    _capture,
    activate,
    append_event,
    digest,
    load_inputs,
    project,
    report,
)
from argosy.state.models import ActionProposal, PortfolioSnapshotRow
from argosy.state.shadow_models import ShadowEvent


def economic_packet(packet):
    return {"date": packet["date"], "fx": packet["fx"], "actions": packet["actions"],
        "marks": packet["marks"], "fx_evidence": packet.get("fx_evidence", {}),
        "cutoff_at": packet.get("cutoff_at"), "contract": packet.get("contract"),
        "unresolved_actions": packet.get("unresolved_actions", [])}


def run_user(factory, user_id, *, now=None, collector=None):
    now = now or datetime.now(UTC)
    with factory() as db:
        seed = activate(db, user_id=user_id, now=now)
        db.commit()
        db.execute(text("BEGIN IMMEDIATE"))
        # Repairs only missing receipts, never resurrects overwritten history or
        # backdates a paper entry. Live publication hooks preserve each revision.
        for proposal in db.scalars(select(ActionProposal).where(ActionProposal.user_id == user_id)):
            _capture(db.connection(), proposal, now=now)
        db.commit()
        seed, events = load_inputs(db, user_id)
        receipts = [json.loads(row.payload_json) for row in events if row.kind == "sheet"]
        old_markets = {payload["date"]: payload for row in events if row.kind == "market"
                       for payload in [json.loads(row.payload_json)]}
        previous_projections = [json.loads(row.payload_json) for row in events if row.kind == "projection"]
        retries = []
        if previous_projections:
            retries = [row["date"] for row in previous_projections[-1]["history"]
                if row.get("status") == "baseline_incomplete" or any(
                    value.get("missing") for value in row.get("books", {}).get("gross", {}).values())][:7]
        fund_observations = {}
        for snap in db.scalars(select(PortfolioSnapshotRow).where(PortfolioSnapshotRow.user_id == user_id,
                PortfolioSnapshotRow.imported_at <= now).order_by(PortfolioSnapshotRow.id.desc()).limit(1000)):
            for position in json.loads(snap.positions_json):
                if (position.get("quote_market") == "TASE_FUND" and position.get("security_id")
                        and str(position.get("price_source") or "").startswith("https://")
                        and position.get("valued_as_of") and position.get("current_price") is not None):
                    key = "TASE:" + str(position["security_id"])
                    fund_observations.setdefault(key, []).append({"price": str(position["current_price"]),
                        "currency": "ILS", "as_of": str(position["valued_as_of"])[:10],
                        "source": position["price_source"], "source_hash": position.get("price_evidence_sha256"),
                        "eligible": True, "contract": "official-fund-nav-ils-v1"})
    # The session/transaction is CLOSED before any HTTP call.
    if collector is None:
        from argosy.services.shadow_market import collect
        collector = collect
    packets, collection = collector(seed, receipts, now=now, existing_days=old_markets,
                                     fund_observations=fund_observations, retry_days=retries)
    with factory() as db:
        for packet in packets:
            from argosy.services.shadow_market import completed_date
            if packet["date"] > completed_date(now).isoformat():
                raise ValueError("Cannot persist a future/incomplete market session")
            if not db.in_transaction():
                db.execute(text("BEGIN IMMEDIATE"))
            prior_row = db.scalar(select(ShadowEvent).where(ShadowEvent.user_id == user_id,
                ShadowEvent.cohort_id == seed["cohort_id"], ShadowEvent.kind == "market",
                ShadowEvent.event_key.like("market:" + packet["date"] + ":%"))
                .order_by(ShadowEvent.id.desc()).limit(1))
            previous = json.loads(prior_row.payload_json) if prior_row else None
            if previous:
                covered = set(packet.get("action_coverage", []))
                packet = {**packet, "marks": {**previous["marks"], **packet["marks"]},
                    "fx": {**previous["fx"], **packet["fx"]},
                    "fx_evidence": {**previous.get("fx_evidence", {}), **packet.get("fx_evidence", {})},
                    "actions": list({row["id"]: row for row in [
                        *[action for action in previous["actions"] if action["instrument"] not in covered],
                        *packet["actions"]]}.values())}
            signature = digest(economic_packet(packet))
            if previous and digest(economic_packet(previous)) == signature:
                db.commit()
                continue
            packet["supersedes_event_id"] = prior_row.id if prior_row else None
            packet["revision"] = (previous.get("revision", 0) if previous else 0) + 1
            append_event(db.connection(), user_id=user_id, cohort_id=seed["cohort_id"],
                event_key="market:" + packet["date"] + ":" + digest([packet["supersedes_event_id"], signature])[:48], kind="market", payload=packet,
                effective_at=datetime.fromisoformat(packet["date"] + "T23:00:00+00:00"))
            db.commit()
        result = project(db, user_id=user_id)
        db.commit()
        projection = report(db, user_id)
        append_event(db.connection(), user_id=user_id, cohort_id=seed["cohort_id"],
            event_key="attempt:" + now.isoformat(), kind="attempt",
            payload={"status": result["status"], "collection": collection}, effective_at=now)
        db.commit()
    return {"user_id": user_id, "cohort_id": seed["cohort_id"], "status": result["status"],
            "collection": collection, "report": projection}


def run_batch(factory, *, now=None):
    with factory() as db:
        users = list(db.scalars(select(PortfolioSnapshotRow.user_id).distinct()))
        attempts = dict(db.execute(select(ShadowEvent.user_id, func.max(ShadowEvent.recorded_at))
            .where(ShadowEvent.kind == "attempt").group_by(ShadowEvent.user_id)).all())
        users.sort(key=lambda user: (attempts.get(user) or datetime.min, user))
        users = users[:10]
    results = []
    for user_id in users:
        try:
            result = run_user(factory, user_id, now=now)
            results.append({key: result[key] for key in ("user_id", "cohort_id", "status", "collection")})
        except Exception as exc:
            results.append({"user_id": user_id, "status": "error", "error": str(exc)[:1000]})
            from argosy.services.shadow_accounting import VERSION
            attempted_at = now or datetime.now(UTC)
            with factory() as db:
                append_event(db.connection(), user_id=user_id, cohort_id=digest([user_id, VERSION]),
                    event_key="attempt-failed:" + attempted_at.isoformat(), kind="attempt",
                    payload={"status": "error", "error": str(exc)[:1000]}, effective_at=attempted_at,
                    now=attempted_at)
                db.commit()
    return {"users": results, "failures": sum(row["status"] == "error" for row in results)}
