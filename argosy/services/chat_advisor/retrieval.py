"""Tenant-scoped, bounded, physically read-only retrieval for chat."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import Integer, case, cast, create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from argosy.services.chat_advisor.contracts import Citation, Principal, ReadResult
from argosy.services.chat_advisor.outbound import OutboundFilter
from argosy.state.models import (
    AgentReport,
    JobRun,
    NewsSignal,
    PlanVersion,
    PortfolioSnapshotRow,
    Proposal,
    UserContext,
    UserFile,
    Verdict,
    YouTubeChannel,
    YouTubeVideo,
)
from argosy.state.research_models import ResearchItem, ResearchSource

MAX_PAGE_SIZE = 50
MAX_DETAIL_CHARS = 24_000  # Leaves room for history/catalog within chat's evidence budget.


def _pagination(total: int, returned: int, limit: int, offset: int) -> dict[str, Any]:
    more = offset + returned < total
    return {"total": total, "returned": returned, "limit": limit, "offset": offset,
            "has_more": more, "next_offset": offset + returned if more else None}


def create_read_only_engine(db_path: str | Path, *, busy_timeout_ms: int = 5_000) -> Engine:
    """Open SQLite with OS-level RO mode and connection-level query_only.

    ``query_only`` is deliberately redundant with ``mode=ro``.  The former
    catches accidental writes even when a test supplies a permissive SQLite
    build; the latter prevents writes at the driver/filesystem boundary.
    """
    path = Path(db_path).resolve()
    encoded_path = quote(path.as_posix(), safe="/:")
    uri = f"sqlite:///file:{encoded_path}?mode=ro&uri=true"
    engine = create_engine(uri, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _read_only(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        finally:
            cursor.close()

    return engine


def _safe_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _iso(value: Any) -> str:
    if value is None:
        return "unknown"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class RetrievalService:
    """Small allowlist of reads; arbitrary SQL and file reads are impossible."""

    TOPICS = frozenset(
        {
            "holdings",
            "plan",
            "constraints",
            "actions",
            "verdicts",
            "recommendations",
            "research",
            "news",
            "discovery",
            "identity",
            "sources",
            "job_health",
            "track_record",
            "cost",
            "reports",
            "documents",
        }
    )

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        engine: Engine | None = None,
        session_factory: Callable[[], Session] | None = None,
        db_resolver: Callable[[Principal], str | Path] | None = None,
        max_page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        self._db_resolver = db_resolver
        if session_factory is None:
            if engine is None:
                if db_path is not None:
                    engine = create_read_only_engine(db_path)
                elif db_resolver is None:
                    # Default routing understands the repository's optional
                    # per-tenant layout and falls back to the legacy shared DB,
                    # whose every query is still user-scoped.
                    from argosy.config import get_settings
                    from argosy.tenancy.database import tenant_db_path

                    settings = get_settings()
                    per_tenant = os.environ.get("ARGOSY_TENANCY", "").lower() in {
                        "per-tenant",
                        "tenant",
                    }
                    self._db_resolver = (
                        (lambda principal: tenant_db_path(principal.household_user_id))
                        if per_tenant
                        else (lambda _principal: settings.db_file)
                    )
            if engine is not None:
                session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self._session_factory = session_factory
        self.engine = engine
        self.max_page_size = min(max(1, int(max_page_size)), MAX_PAGE_SIZE)

    @contextmanager
    def session(self, principal: Principal) -> Iterator[Session]:
        dynamic_engine: Engine | None = None
        if self._session_factory is not None:
            db = self._session_factory()
        elif self._db_resolver is not None:
            path = Path(self._db_resolver(principal))
            if not path.is_file():
                raise FileNotFoundError(
                    f"No tenant database is available for {principal.household_user_id!r}"
                )
            dynamic_engine = create_read_only_engine(path)
            db = Session(dynamic_engine, expire_on_commit=False)
        else:  # pragma: no cover - constructor makes this unreachable
            raise RuntimeError("RetrievalService has no read-only database resolver")
        try:
            # Also protect injected test/session factories.
            if db.bind is not None and db.bind.dialect.name == "sqlite":
                db.connection().exec_driver_sql("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()
            if dynamic_engine is not None:
                dynamic_engine.dispose()

    def read(self, principal: Principal, topic: str, **filters: Any) -> ReadResult:
        normalized = topic.strip().lower().replace("-", "_")
        aliases = {"current_plan": "plan", "inbox": "actions", "health": "job_health"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in self.TOPICS:
            return ReadResult(
                topic=normalized,
                data=None,
                warnings=[
                    f"Unsupported read topic: {normalized}. Available: {', '.join(sorted(self.TOPICS))}."
                ],
            )
        clean_filters = dict(filters)
        if normalized == "identity":
            from argosy.services.chat_advisor.identity import lookup_identity

            queries = [str(clean_filters[key]) for key in ("query", "ticker", "name")
                       if clean_filters.get(key)]
            if not queries:
                return ReadResult("identity", None, warnings=["An issuer name or ticker is needed."])
            try:
                data = lookup_identity(queries)
                return ReadResult("identity", data, [Citation("issuer_directory", "sec", data["verified_at"],
                                                              url=data["source_url"])])
            except Exception as exc:
                return ReadResult("identity", {"queries": queries, "matches": [], "verified": False},
                                  warnings=[f"Identity verification unavailable ({type(exc).__name__}); "
                                            "do not assert the names are the same or different."])
        limit = min(max(1, int(clean_filters.pop("limit", 20))), self.max_page_size)
        offset = max(0, int(clean_filters.pop("offset", 0)))
        detail_offset = max(0, int(clean_filters.pop("detail_offset", 0)))
        if normalized in {"verdicts", "recommendations"}:
            for alias in ("symbol", "instrument"):
                if alias in clean_filters and "ticker" not in clean_filters:
                    clean_filters["ticker"] = clean_filters.pop(alias)
            unsupported = set(clean_filters) - {"ticker", "record_id"}
            if unsupported:
                return ReadResult(normalized, None, warnings=[
                    "That instrument lookup could not be scoped. Please supply the exact ticker; "
                    "no unrelated records were retrieved."])
        method = getattr(self, f"_read_{normalized}")
        try:
            with self.session(principal) as db:
                result = method(
                    db, principal.household_user_id, limit=limit, offset=offset, **clean_filters
                )
                if normalized in {"actions", "research"} and clean_filters.get("record_id") and result.data is not None:
                    # Redact before slicing: otherwise a secret split across
                    # pages could lose its identifying prefix on the next page.
                    encoded = OutboundFilter.redact(json.dumps(result.data, ensure_ascii=False, default=str))
                    if len(encoded) > MAX_DETAIL_CHARS or detail_offset:
                        chunk = encoded[detail_offset:detail_offset + MAX_DETAIL_CHARS]
                        result.data = {
                            "record_id": str(clean_filters["record_id"]), "format": "JSON text excerpt",
                            "detail_excerpt": chunk,
                            "detail_pagination": {"unit": "characters", **_pagination(
                                len(encoded), len(chunk), MAX_DETAIL_CHARS, detail_offset)},
                            "detail_lookup": "Read the same topic and record_id with detail_offset=next_offset to continue. Each read reflects current stored evidence.",
                        }
                return result
        except Exception as exc:  # schema/version/source failures are explicit
            return ReadResult(
                normalized,
                None,
                warnings=[
                    f"The {normalized} source is unavailable ({type(exc).__name__}: {exc}); no healthy/current status can be inferred."
                ],
            )

    def _read_holdings(self, db: Session, user_id: str, **_: Any) -> ReadResult:
        row = db.scalar(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.imported_at.desc())
            .limit(1)
        )
        if row is None:
            return ReadResult(
                "holdings",
                None,
                warnings=["No portfolio snapshot is registered for this household."],
            )
        data = {
            "snapshot_date": _iso(row.snapshot_date),
            "positions": _safe_json(row.positions_json, []),
            "allocations": _safe_json(row.allocations_json, []),
            "totals": _safe_json(row.totals_json, {}),
            "parse_warnings": _safe_json(row.parse_warnings_json, []),
        }
        return ReadResult(
            "holdings", data, [Citation("portfolio_snapshot", str(row.id), _iso(row.imported_at))]
        )

    def _read_plan(self, db: Session, user_id: str, **_: Any) -> ReadResult:
        row = db.scalar(
            select(PlanVersion)
            .where(PlanVersion.user_id == user_id, PlanVersion.role == "current")
            .order_by(PlanVersion.accepted_at.desc(), PlanVersion.id.desc())
            .limit(1)
        )
        if row is None:
            return ReadResult("plan", None, warnings=["No current accepted plan is registered."])
        data = {
            "id": row.id,
            "version_label": row.version_label,
            "role": row.role,
            "accepted_at": _iso(row.accepted_at),
            "long": row.horizon_long_md,
            "medium": row.horizon_medium_md,
            "short": row.horizon_short_md,
            "target_allocation": _safe_json(row.target_allocation_json, None),
        }
        return ReadResult(
            "plan",
            data,
            [
                Citation(
                    "plan_version",
                    str(row.id),
                    _iso(row.accepted_at or row.imported_at),
                    row.version_label,
                )
            ],
        )

    def _read_constraints(self, db: Session, user_id: str, **_: Any) -> ReadResult:
        row = db.get(UserContext, user_id)
        if row is None:
            return ReadResult("constraints", None, warnings=["No household context is registered."])
        return ReadResult(
            "constraints",
            {"constraints_yaml": row.constraints_yaml, "goals_yaml": row.goals_yaml},
            [Citation("user_context", user_id, _iso(row.updated_at))],
        )

    def _read_actions(self, db: Session, user_id: str, *, limit: int, offset: int, **filters: Any) -> ReadResult:
        from argosy.services.chat_advisor.notifications import material_version
        from argosy.services.inbox.service import build_inbox

        feed = build_inbox(db, user_id=user_id)
        record_id = str(filters.get("record_id") or "")
        record_type = str(filters.get("record_type") or "")
        plan_requested = record_id == "current_trade_plan" and record_type in ("", "trade_plan")
        matched = [item for item in feed.items
                   if (not record_type or any(ref.source == record_type for ref in item.source_refs))
                   and (not record_id or not record_type and item.id == record_id
                        or any(ref.ref_id == record_id and (not record_type or ref.source == record_type)
                               for ref in item.source_refs))]
        page = matched[offset:offset + limit]
        citations: list[Citation] = []
        items = []
        for item in page:
            for ref in item.source_refs:
                citations.append(Citation(ref.source, ref.ref_id, feed.generated_at,
                                          material_version(item), label=item.title, category=item.kind))
            row = item.to_dict()
            if not record_id:
                # Keep every item's identity, priority, dates and action, rather
                # than letting one large order artifact truncate later items.
                row["body"] = {key: value for key, value in item.body.items() if key in {
                    "detail", "how_to", "done_when", "status", "blockers", "line_count", "ticker",
                }}
                row["summary_omissions"] = sorted(set(item.body) - set(row["body"]))
                for key, value in row["body"].items():
                    if isinstance(value, str) and len(value) > 2000:
                        row["body"][key] = value[:2000] + " [excerpt; full record available]"
                        row["summary_omissions"].append(f"{key}:remainder")
            items.append(row)
        plan = (feed.trade_plan or {}) if not record_id or matched or plan_requested else {}
        plan_keys = ("as_of", "approval_blocked", "totals", "source", "reserve_usd", "new_cash_usd")
        plan_summary = {key: plan[key] for key in plan_keys if key in plan}
        if plan:
            plan_summary["line_count"] = len(plan.get("lines") or [])
            plan_summary["details_available"] = True
            plan_summary["detail_record_id"] = "current_trade_plan"
        if plan_requested and plan:
            citations.append(Citation("trade_plan", "current_trade_plan", feed.generated_at))
        data = {"queue": "user_action_inbox", "items": items,
                "pagination": _pagination(len(matched), len(page), limit, offset),
                "counts_by_kind": dict(Counter(item.kind for item in matched)),
                "generated_at": feed.generated_at, "liveness": feed.liveness.to_dict(),
                "trade_plan": (plan if plan_requested or record_id and any(item.kind == "order_sheet" for item in page)
                               else plan_summary), "issues": feed.issues,
                "detail_lookup": "Read actions with record_id equal to an item id for full details; current_trade_plan returns the full canonical trade plan."}
        return ReadResult("actions", data, citations, [i["message"] for i in feed.issues])

    def _read_verdicts(
        self, db: Session, user_id: str, *, limit: int, offset: int, **filters: Any
    ) -> ReadResult:
        stmt = select(Verdict).where(Verdict.user_id == user_id)
        if filters.get("record_id") is not None:
            stmt = stmt.where(Verdict.id == int(filters["record_id"]))
        ticker = str(filters.get("ticker") or "").upper().strip()
        if ticker:
            stmt = stmt.where(Verdict.subject == ticker)
        rows = db.scalars(
            stmt.order_by(Verdict.created_at.desc()).offset(offset).limit(limit)
        ).all()
        data = [
            {
                "id": r.id,
                "subject": r.subject,
                "settled": r.settled,
                "verdict": r.verdict,
                "conviction": r.conviction,
                "rationale": r.reasoning_md,
                "falsifiers": _safe_json(r.falsifiers_json, []),
                "next_validation": _iso(r.next_validation),
                "created_at": _iso(r.created_at),
                "superseded_by_id": r.superseded_by,
            }
            for r in rows
        ]
        return ReadResult(
            "verdicts", data, [Citation("verdict", str(r.id), _iso(r.created_at)) for r in rows],
            ([f"No saved verdict matches exact ticker {ticker}. Confirm the intended instrument; "
              "this does not mean its verdict is HOLD or that a fresh review ran."] if ticker and not rows
             else ["Bounded verdict history, not the complete registry."])
        )

    def _read_recommendations(
        self, db: Session, user_id: str, *, limit: int, offset: int, **filters: Any
    ) -> ReadResult:
        stmt = select(Proposal).where(Proposal.user_id == user_id)
        if filters.get("record_id") is not None:
            stmt = stmt.where(Proposal.id == int(filters["record_id"]))
        ticker = str(filters.get("ticker") or "").upper().strip()
        if ticker:
            stmt = stmt.where(Proposal.ticker == ticker)
        rows = db.scalars(
            stmt.order_by(Proposal.updated_at.desc()).offset(offset).limit(limit)
        ).all()
        data = [
            {
                "id": r.id,
                "ticker": r.ticker,
                "action": r.action,
                "amount": float(r.size_shares_or_currency),
                "units": r.size_units,
                "status": r.status,
                "rationale": r.rationale_summary,
                "expires_at": _iso(r.expires_at),
                "updated_at": _iso(r.updated_at),
                "decision_run_id": r.decision_run_id,
            }
            for r in rows
        ]
        return ReadResult(
            "recommendations",
            data,
            [Citation("proposal", str(r.id), _iso(r.updated_at)) for r in rows],
        )

    def _research_query(self, user_id: str, **filters: Any):
        stmt = select(ResearchItem).where(ResearchItem.user_id == user_id)
        unsupported = set(filters) - {"record_id", "source_id", "query", "status"}
        if unsupported:
            raise ValueError(f"Unsupported research filters: {', '.join(sorted(unsupported))}")
        if filters.get("status"):
            stmt = stmt.where(ResearchItem.status == str(filters["status"]))
        if filters.get("record_id") is not None:
            stmt = stmt.where(ResearchItem.id == str(filters["record_id"]))
        source_id = filters.get("source_id")
        if source_id is not None:
            stmt = stmt.where(ResearchItem.source_id == int(source_id))
        query = str(filters.get("query") or "").strip()
        if query:
            stmt = stmt.where(
                (ResearchItem.title.contains(query)) | (ResearchItem.author.contains(query))
                | (ResearchItem.body.contains(query))
            )
        return stmt

    def _research_rows(
        self, db: Session, user_id: str, limit: int, offset: int, **filters: Any
    ) -> list[ResearchItem]:
        stmt = self._research_query(user_id, **filters)
        return list(
            db.scalars(
                stmt.order_by(case((ResearchItem.status == "archived", 1), else_=0),
                              func.coalesce(ResearchItem.published_at, ResearchItem.observed_at).desc(), ResearchItem.id)
                .offset(offset).limit(limit)
            ).all()
        )

    def _read_research(
        self, db: Session, user_id: str, *, limit: int, offset: int, **filters: Any
    ) -> ReadResult:
        rows = self._research_rows(db, user_id, limit, offset, **filters)
        matching = self._research_query(user_id, **filters).subquery()
        counts = dict(db.execute(select(matching.c.status, func.count()).group_by(matching.c.status)).all())
        total = sum(counts.values())
        detail = bool(filters.get("record_id"))
        data = [
            {
                "id": r.id,
                "source_id": r.source_id,
                "title": r.title,
                "author": r.author,
                "url": r.url,
                "published_at": _iso(r.published_at),
                "observed_at": _iso(r.observed_at),
                "status": r.status,
                "capture_coverage": (_safe_json(r.analysis_json, {}) or {}).get("capture"),
                "body": r.body if detail else (r.body or "")[:1500],
                "analysis": _safe_json(r.analysis_json, None) if detail else None,
                "analysis_excerpt": None if detail else (r.analysis_json or "")[:2500],
                "details_available": bool(not detail and (len(r.body or "") > 1500 or r.analysis_json)),
            }
            for r in rows
        ]
        warnings = (
            []
            if rows
            else ["No matching ingested research was found; titles alone are not summarized."]
        )
        return ReadResult(
            "research",
            {"queue": "argosy_research", "owner": "Argosy", "items": data,
             "pagination": _pagination(total, len(rows), limit, offset), "counts_by_status": counts,
             "detail_lookup": "Read research with record_id for the full document and saved analysis."},
            [Citation("research_item", r.id, _iso(r.observed_at), url=r.url or None) for r in rows],
            warnings,
        )

    def _read_news(self, db: Session, user_id: str, *, limit: int, offset: int,
                   **filters: Any) -> ReadResult:
        # Shared table contains private Discord ingestion too; only public feeds
        # may be read without household ownership on the source row.
        stmt = select(NewsSignal).where(NewsSignal.source.in_(
            ("rss", "macro_feed", "yf_earnings", "sec_filing")))
        query = str(filters.get("ticker") or filters.get("query") or "").strip()
        if query:
            stmt = stmt.where(NewsSignal.evidence_excerpt.contains(query)
                              | NewsSignal.parsed_tickers.contains(f'"{query.upper()}"'))
        rows = list(db.scalars(stmt.order_by(NewsSignal.received_at.desc())
                               .offset(offset).limit(limit)))
        research = self._read_research(db, user_id, limit=limit, offset=offset,
                                       **({"query": query} if query else {}))
        data = {
            "checked_at": datetime.now(UTC).isoformat(),
            "coverage": "Recorded public news feeds and this household's research; not a live web search.",
            "news": [{"id": row.id, "source": row.source, "source_ref": row.source_ref,
                      "received_at": _iso(row.received_at),
                      "publication_date": None, "tickers": _safe_json(row.parsed_tickers, []),
                      "excerpt": row.evidence_excerpt, "materiality": row.materiality,
                      "analysis": row.rationale, "analyzed_at": _iso(row.analyzed_at)} for row in rows],
            "research": research.data,
        }
        citations = [Citation("news_signal", str(row.id), _iso(row.received_at),
                              url=row.source_ref if row.source_ref.startswith("https://") else None)
                     for row in rows] + research.citations
        return ReadResult("news", data, citations, [
            "Ingestion time is not publication time. Summarize only the dated evidence actually present; "
            "absence in these bounded cached feeds does not establish that nothing happened today."])

    def _read_discovery(self, db: Session, user_id: str, *, limit: int, offset: int,
                        **filters: Any) -> ReadResult:
        from argosy.state.models import ScanState
        stmt = select(ScanState).where(ScanState.user_id == user_id, ScanState.status == "active")
        if filters.get("ticker"):
            stmt = stmt.where(ScanState.ticker == str(filters["ticker"]).upper())
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(db.scalars(stmt.order_by(ScanState.rank.asc(), ScanState.ticker)
                               .offset(offset).limit(limit)))
        return ReadResult("discovery", {
            "checked_at": datetime.now(UTC).isoformat(),
            "coverage": "Saved discovery research, not executable orders or a new fleet assessment.",
            "candidates": [{"ticker": r.ticker, "rank": r.rank, "score": r.last_score,
                            "screened_at": _iso(r.last_radar_at), "reviewed_at": _iso(r.last_fleet_at),
                            "research": _safe_json(r.fleet_json, None),
                            "estimate": _safe_json(r.estimator_json, None)} for r in rows],
            "pagination": _pagination(total, len(rows), limit, offset),
        }, [Citation("discovery", r.ticker, _iso(r.last_fleet_at or r.last_radar_at),
                     label=f"{r.ticker} discovery review") for r in rows])

    def _read_sources(
        self, db: Session, user_id: str, *, limit: int, offset: int, **_: Any
    ) -> ReadResult:
        mirrored_youtube = (
            select(YouTubeChannel.id)
            .where(
                YouTubeChannel.user_id == user_id,
                ResearchSource.kind == "youtube",
                YouTubeChannel.youtube_channel_id == ResearchSource.reference,
            )
            .exists()
        )
        research_rows = db.scalars(
            select(ResearchSource)
            .where(ResearchSource.user_id == user_id, ~mirrored_youtube)
            .order_by(ResearchSource.kind, func.lower(ResearchSource.name), ResearchSource.id)
            .limit(offset + limit)
        ).all()
        youtube_rows = db.scalars(
            select(YouTubeChannel)
            .where(YouTubeChannel.user_id == user_id)
            .order_by(func.lower(YouTubeChannel.channel_name), YouTubeChannel.id)
            .limit(offset + limit)
        ).all()
        entries = [
            {
                "registry": "research_sources",
                "id": r.id,
                "name": r.name,
                "kind": r.kind,
                "enabled": r.enabled,
                "cadence_hours": r.cadence_hours,
                "last_polled_at": _iso(r.last_polled_at),
                "last_error": r.last_error,
            }
            for r in research_rows
        ]
        for row in youtube_rows:
            item_count = int(
                db.scalar(
                    select(func.count(YouTubeVideo.id)).where(
                        YouTubeVideo.user_id == user_id,
                        YouTubeVideo.channel_id == row.id,
                    )
                )
                or 0
            )
            entries.append(
                {
                    "registry": "youtube_channels",
                    "id": row.id,
                    "name": row.channel_name,
                    "kind": "youtube",
                    "enabled": bool(row.enabled),
                    "last_polled_at": _iso(row.last_polled_at),
                    "last_item_at": _iso(row.last_seen_published_at),
                    "last_error": row.last_error,
                    "items_analyzed": item_count,
                }
            )
        entries.sort(key=lambda row: (str(row["kind"]), str(row["name"]).lower()))
        data = entries[offset : offset + limit]
        research_groups = db.execute(
            select(
                ResearchSource.kind,
                func.count(ResearchSource.id),
                func.sum(cast(ResearchSource.enabled, Integer)),
            )
            .where(ResearchSource.user_id == user_id, ~mirrored_youtube)
            .group_by(ResearchSource.kind)
        ).all()
        youtube_count, youtube_enabled = db.execute(
            select(
                func.count(YouTubeChannel.id),
                func.sum(cast(YouTubeChannel.enabled, Integer)),
            ).where(YouTubeChannel.user_id == user_id)
        ).one()
        by_kind = {str(kind): int(count) for kind, count, _enabled in research_groups}
        by_kind["youtube"] = by_kind.get("youtube", 0) + int(youtube_count or 0)
        registered = sum(by_kind.values())
        enabled = sum(int(value or 0) for _kind, _count, value in research_groups) + int(
            youtube_enabled or 0
        )
        return ReadResult(
            "sources",
            {
                "totals": {
                    "registered": registered,
                    "enabled": enabled,
                    "disabled_or_paused": registered - enabled,
                    "by_kind": by_kind,
                },
                "page": data,
                "page_offset": offset,
                "page_limit": limit,
                "page_count": len(data),
            },
            [
                Citation(str(row["registry"]), str(row["id"]), str(row["last_polled_at"]))
                for row in data
            ],
            [
                "Counts cover Argosy's research-source and YouTube-channel registries only, not unrelated personal subscriptions.",
                "A configured source is not called healthy unless poll or analyzed-item receipts support that conclusion.",
            ],
        )

    def _read_job_health(
        self, db: Session, user_id: str, *, limit: int, offset: int, **_: Any
    ) -> ReadResult:
        latest_ids = select(func.max(JobRun.id)).group_by(JobRun.job_name)
        rows = db.scalars(
            select(JobRun)
            .where(JobRun.id.in_(latest_ids))
            .order_by(JobRun.started_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        data = [
            {
                "id": r.id,
                "job_name": r.job_name,
                "status": r.status,
                "started_at": _iso(r.started_at),
                "finished_at": _iso(r.finished_at),
                "duration_ms": r.duration_ms,
                "skip_reason": r.skip_reason,
                "error": r.error_message,
            }
            for r in rows
        ]
        return ReadResult(
            "job_health",
            data,
            [Citation("job_run", str(r.id), _iso(r.finished_at or r.started_at)) for r in rows],
            [
                "These are latest durable job receipts, not proof that the server is currently reachable.",
                "Operational job metadata is deployment-wide in the legacy shared database; no other household financial records are included.",
            ],
        )

    def _read_track_record(self, db: Session, user_id: str, **_: Any) -> ReadResult:
        try:
            from argosy.services.recommendation_scorecard import build_recommendation_scorecard

            payload = build_recommendation_scorecard(db, user_id=user_id)
        except TypeError:
            payload = build_recommendation_scorecard(db, user_id)
        return ReadResult(
            "track_record",
            payload,
            [Citation("recommendation_scorecard", user_id, datetime.now(UTC).isoformat())],
            [
                "Statistics are projected from the canonical scorecard and are not recomputed by chat."
            ],
        )

    def _read_cost(self, db: Session, user_id: str, **filters: Any) -> ReadResult:
        stmt = select(
            func.coalesce(func.sum(AgentReport.cost_usd), 0),
            func.count(AgentReport.id),
            func.min(AgentReport.created_at),
            func.max(AgentReport.created_at),
        ).where(AgentReport.user_id == user_id)
        requested_start = filters.get("period_start") or filters.get("start") or filters.get("since")
        requested_end = filters.get("period_end") or filters.get("end") or filters.get("until")
        if filters.get("period") and not requested_start and not requested_end:
            return ReadResult(
                "cost",
                None,
                warnings=[
                    "Named cost periods are not inferred. Supply explicit ISO period_start and period_end boundaries."
                ],
            )
        warnings = [
            "This is recorded agent-report cost only; missing provider telemetry is not estimated."
        ]
        try:
            if requested_start:
                stmt = stmt.where(
                    AgentReport.created_at >= datetime.fromisoformat(str(requested_start))
                )
            if requested_end:
                stmt = stmt.where(
                    AgentReport.created_at < datetime.fromisoformat(str(requested_end))
                )
        except ValueError as exc:
            return ReadResult("cost", None, warnings=[f"Invalid ISO cost period: {exc}"])
        if not requested_start and not requested_end:
            warnings.append(
                "No period was supplied; totals cover all recorded history, not a month or billing cycle."
            )
        total, calls, earliest, latest = db.execute(stmt).one()
        data = {
            "cost_usd": float(total),
            "agent_calls": int(calls),
            "period_start": _iso(earliest),
            "period_end": _iso(latest),
            "requested_period_start": requested_start,
            "requested_period_end_exclusive": requested_end,
        }
        return ReadResult(
            "cost",
            data,
            [Citation("agent_reports_aggregate", user_id, _iso(latest))],
            warnings,
        )

    def _read_reports(
        self, db: Session, user_id: str, *, limit: int, offset: int, **filters: Any
    ) -> ReadResult:
        stmt = select(AgentReport).where(AgentReport.user_id == user_id)
        if filters.get("record_id") is not None:
            stmt = stmt.where(AgentReport.id == int(filters["record_id"]))
        role = str(filters.get("role") or "").strip()
        if role:
            stmt = stmt.where(AgentReport.agent_role == role)
        rows = db.scalars(
            stmt.order_by(AgentReport.created_at.desc()).offset(offset).limit(limit)
        ).all()
        data = [
            {
                "id": r.id,
                "role": r.agent_role,
                "decision_id": r.decision_id,
                "response": r.response_text,
                "created_at": _iso(r.created_at),
                "cost_usd": float(r.cost_usd),
            }
            for r in rows
        ]
        return ReadResult(
            "reports", data, [Citation("agent_report", str(r.id), _iso(r.created_at)) for r in rows]
        )

    def _read_documents(
        self, db: Session, user_id: str, *, limit: int, offset: int, **_: Any
    ) -> ReadResult:
        rows = db.scalars(
            select(UserFile)
            .where(UserFile.user_id == user_id, UserFile.deleted_at.is_(None))
            .order_by(UserFile.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        # Catalog metadata only: never dereference storage_path from chat.
        data = [
            {
                "id": r.id,
                "name": r.original_name,
                "mime_type": r.mime_type,
                "kind": r.kind,
                "source": r.source,
                "size_bytes": r.size_bytes,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ]
        return ReadResult(
            "documents",
            data,
            [Citation("user_file", str(r.id), _iso(r.created_at)) for r in rows],
            [
                "Document access is catalog-only; local storage paths and unregistered files are not exposed."
            ],
        )


__all__ = ["MAX_PAGE_SIZE", "RetrievalService", "create_read_only_engine"]
