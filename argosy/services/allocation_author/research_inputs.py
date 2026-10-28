"""Bounded, attributable subscription evidence for author and blind reviewers."""
from datetime import UTC, date, datetime, timedelta
import json

from sqlalchemy import select, func
from argosy.services.research_inputs import GUIDANCE
from argosy.state.research_models import ResearchItem, ResearchSource


def collect_allocation_research(db, user_id, *, now=None, limit=12):
    now = now or datetime.now(UTC)
    source_filter = (ResearchSource.user_id == user_id, ResearchSource.enabled.is_(True))
    source_count = db.scalar(select(func.count()).select_from(ResearchSource).where(*source_filter))
    sources = list(db.scalars(select(ResearchSource).where(*source_filter)
                             .order_by(ResearchSource.priority.desc(), ResearchSource.id).limit(32)))
    by_id = {s.id: s for s in sources}
    rows = []
    query_truncated = []
    coverage = []
    for source in sources:
        query = select(ResearchItem).where(ResearchItem.user_id == user_id,
            ResearchItem.source_id == source.id, ResearchItem.observed_at <= now,
            ResearchItem.observed_at >= now - timedelta(days=7)
        ).order_by(ResearchItem.observed_at.desc(), ResearchItem.id)
        latest = db.scalar(query.limit(1))
        reviewed = list(db.scalars(query.where(ResearchItem.status.in_(['analyzed', 'analyzed_partial']),
            ResearchItem.analyzed_at <= now).limit(21)))
        if len(reviewed) > 20:
            query_truncated.append(source.id)
        rows.extend(reviewed[:20])
        coverage.append(dict(source=source.name[:120], last_polled_at=str(source.last_polled_at),
            last_error=str(source.last_error or '')[:150],
            latest_item_id=latest.id if latest else None,
            latest_status=latest.status if latest else 'no_recent_item'))
    items = []
    excluded_stated_dates = 0
    for row in sorted(rows, key=lambda r: (r.observed_at, r.id), reverse=True):
        if row.status not in {'analyzed', 'analyzed_partial'} or not row.analyzed_at:
            continue
        analyzed = row.analyzed_at.replace(tzinfo=UTC) if row.analyzed_at.tzinfo is None else row.analyzed_at
        published = row.published_at
        if published is not None:
            published = published.replace(tzinfo=UTC) if published.tzinfo is None else published
        if analyzed > now or (published and not now - timedelta(days=7) <= published <= now):
            continue
        payload = json.loads(row.analysis_json or '{}')
        if (payload.get('evidence_review') or {}).get('status') != 'approved':
            continue
        stated_date = (payload.get('capture') or {}).get('source_date')
        if stated_date:
            try:
                source_day = date.fromisoformat(str(stated_date)[:10])
                if not (now - timedelta(days=7)).date() <= source_day <= now.date():
                    excluded_stated_dates += 1
                    continue
            except ValueError:
                excluded_stated_dates += 1
                continue
        synthesis = payload.get('synthesis') or {}
        # Source interpretation is explicitly separate from independently
        # verified market facts and from the current allocation's judgment.
        items.append(dict(item_id=row.id, source_id='research:' + row.id, source=by_id[row.source_id].name,
            title=row.title, url=row.url, published_at=str(row.published_at or 'unknown'),
            observed_at=str(row.observed_at), analyzed_at=str(row.analyzed_at),
            assessment_id=payload.get('assessment_id'), coverage=payload.get('capture'),
            synthesis={k: v for k, v in synthesis.items()
                       if k in {'executive_summary', 'credible_takeaways', 'challenged_claims',
                                'bottom_line', 'market_outlook', 'ticker_recommendations'}},
            claims=(payload.get('claims') or {}).get('claims', [])[:12],
            skeptic_findings=(payload.get('skeptic') or {}).get('findings', [])[:5]))
    # Bound each source document while retaining identifiers for full reads.
    def bounded(value, depth=0):
        if depth > 6: return '[nested content omitted]'
        if isinstance(value, str): return value[:1200]
        if isinstance(value, list): return [bounded(v, depth+1) for v in value[:12]]
        if isinstance(value, dict): return {str(k)[:80]: bounded(v, depth+1) for k, v in list(value.items())[:20]}
        return value
    selected = []
    for item in items[:max(0, min(limit, 12))]:
        limited = bounded(item)
        if len(json.dumps(limited)) > 4000:
            limited = {k: limited[k] for k in ('item_id', 'source_id', 'source', 'title',
                       'url', 'published_at', 'observed_at', 'analyzed_at', 'assessment_id')}
            review = bounded(item).get('synthesis') or {}
            limited['review_summary'] = {
                'executive_summary': str(review.get('executive_summary') or '')[:1000],
                'bottom_line': str(review.get('bottom_line') or '')[:1600],
                'credible_takeaways': [str(v)[:240] for v in review.get('credible_takeaways', [])[:3]],
                'ticker_dispositions': [{k: str(r.get(k) or '')[:180]
                    for k in ('ticker', 'disposition', 'rationale')}
                    for r in review.get('ticker_recommendations', [])[:12]
                    if isinstance(r, dict)],
            }
            limited['truncated'] = 'Bounded summary and dispositions; read the saved item for full claims/evidence.'
        selected.append(limited)
    result = dict(guidance=GUIDANCE, as_of=now.isoformat(), coverage=coverage,
                items=selected, omitted_reviewed_items=max(0, len(items)-len(selected)),
                query_truncated_source_ids=query_truncated,
                omitted_sources=max(0, source_count-len(sources)),
                excluded_stale_or_future_source_dates=excluded_stated_dates)
    while result['items'] and len(json.dumps(result)) > 60000:
        result['items'].pop()
        result['omitted_reviewed_items'] += 1
    while result['coverage'] and len(json.dumps(result)) > 60000:
        result['coverage'].pop()
        result['omitted_sources'] += 1
    return result
