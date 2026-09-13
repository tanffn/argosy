"""Evidence-use receipts and horizon-preserving speaker scorecards."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import re

from sqlalchemy import or_, select

from argosy.services.research_catalog import research_session, digest, utc
from argosy.state.research_models import ResearchItem, ResearchClaim, ResearchEvidenceUse


def record_report_connection(connection, report):
    """All report persistence paths get atomic usage receipts, without extra commits."""
    from sqlalchemy import inspect
    if report.agent_role.startswith("youtube_"):
        return
    prompt = (report.sources_json or "") + "\n" + (report.user_prompt or "")
    ids = set(re.findall(r"research:([a-f0-9]{64})(?=[:\s\"\\]|$)", prompt))
    if not ids or not inspect(connection).has_table("research_evidence_uses"):
        return
    output = (report.response_text or "") + (report.citations_json or "")
    for item in connection.execute(select(ResearchItem.__table__).where(ResearchItem.id.in_(ids), ResearchItem.user_id == report.user_id)).mappings():
        connection.execute(ResearchEvidenceUse.__table__.insert().values(
            id=digest(report.user_id, report.id, item["id"]), user_id=report.user_id, item_id=item["id"],
            report_id=report.id, decision_id=report.decision_id, agent_role=report.agent_role,
            cited=("research:" + item["id"]) in output or bool(item["url"] and item["url"] in output),
            created_at=report.created_at or datetime.now(UTC)))


def index_report(session, report) -> int:
    """Supplied and cited are separate; presence alone is never impact credit."""
    if report.agent_role.startswith("youtube_"):
        return 0  # ingestion is not a downstream investment review
    prompt = (report.sources_json or "") + "\n" + (report.user_prompt or "")
    ids = set(re.findall(r"research:([a-f0-9]{64})(?=[:\s\"\\]|$)", prompt))
    output = report.response_text or ""
    citations = report.citations_json or ""
    verdict = None
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            verdict = parsed.get("verdict") or parsed.get("action")
    except ValueError:
        pass
    count = 0
    for item_id in ids:
        item = session.get(ResearchItem, item_id)
        if item is None or item.user_id != report.user_id:
            continue
        key = digest(report.user_id, report.id, item_id)
        if session.get(ResearchEvidenceUse, key):
            continue
        cited = ("research:" + item_id) in output + citations or bool(item.url and item.url in output + citations)
        session.add(ResearchEvidenceUse(id=key, user_id=report.user_id, item_id=item_id,
                    report_id=report.id, decision_id=report.decision_id, agent_role=report.agent_role,
                    cited=cited, verdict=str(verdict)[:32] if verdict else None, created_at=report.created_at))
        count += 1
    return count


def index_evidence_uses(*, user_id):
    from argosy.state.models import AgentReport
    with research_session() as session:
        rows = session.scalars(select(AgentReport).where(
            AgentReport.user_id == user_id,
            or_(AgentReport.sources_json.contains("research:"), AgentReport.user_prompt.contains("research:")),
        ).order_by(AgentReport.id.desc()).limit(2000)).all()
        count = sum(index_report(session, row) for row in rows)
        session.commit()
        return count


def score_direction(*, direction, subject_return, benchmark_return):
    if direction not in {"bullish", "bearish"}:
        return {"verdict": "unscored", "reason": "No explicit directional call"}
    signed = subject_return if direction == "bullish" else -subject_return
    return {"verdict": "correct" if signed > 0 else "incorrect" if signed < 0 else "inconclusive",
            "subject_return_pct": subject_return * 100, "benchmark_return_pct": benchmark_return * 100,
            "excess_return_pct": (subject_return - benchmark_return) * 100,
            "method": "direction_at_stated_horizon_v1",
            "limitation": "Scores price direction only, not every causal claim or target price in the statement."}


def evaluate_due_claims(*, user_id, now=None, price_fetcher=None):
    from argosy.services.predictions.evaluator import default_price_fetcher
    from argosy.logging import get_logger
    now = now or datetime.now(UTC)
    fetch = price_fetcher or default_price_fetcher
    count = 0
    with research_session() as session:
        rows = session.execute(select(ResearchClaim, ResearchItem).join(ResearchItem).where(
            ResearchClaim.user_id == user_id, ResearchClaim.evaluated_at.is_(None),
            ResearchClaim.due_at < now.replace(hour=0, minute=0, second=0, microsecond=0),
        ).order_by(ResearchClaim.due_at).limit(25)).all()
        for claim, item in rows:
            start = utc(item.published_at or item.observed_at).date() + timedelta(days=1)
            end = utc(claim.due_at).date()
            if not claim.ticker or claim.direction not in {"bullish", "bearish"}:
                outcome = {"verdict": "unscored", "reason": "Requires qualitative review; no measurable ticker-direction call"}
            elif utc(item.observed_at) >= utc(claim.due_at):
                outcome = {"verdict": "unscored", "reason": "Ingested after the forecast horizon; excluded from prospective calibration"}
            else:
                try:
                    bars = sorted((b for b in fetch(claim.ticker, start, end) if start <= b.bar_date <= end and b.close > 0), key=lambda b:b.bar_date)
                    spy = sorted((b for b in fetch("SPY", start, end) if start <= b.bar_date <= end and b.close > 0), key=lambda b:b.bar_date)
                    common = sorted(set(b.bar_date for b in bars) & set(b.bar_date for b in spy))
                    if len(common) < 2 or (end-common[-1]).days > 4 or (common[0]-start).days > 4:
                        continue  # missing data remains pending, never an incorrect call
                    by_day = {b.bar_date:b.close for b in bars}
                    spy_day = {b.bar_date:b.close for b in spy}
                    first, last = common[0], common[-1]
                    outcome = score_direction(direction=claim.direction,
                        subject_return=by_day[last]/by_day[first]-1, benchmark_return=spy_day[last]/spy_day[first]-1)
                    outcome.update({"entry_date": str(first), "exit_date": str(last), "entry_close": by_day[first],
                                    "exit_close": by_day[last], "benchmark": "SPY", "price_provider": "existing EOD adapter"})
                except Exception as exc:
                    get_logger(__name__).warning("research.claim_price_unavailable", claim_id=claim.id, error=str(exc)[:160])
                    continue
            claim.outcome_json = json.dumps(outcome)
            claim.evaluated_at = now
            count += 1
            session.commit()  # Never hold a writer lock while fetching the next price history.
        session.commit()
    return count
