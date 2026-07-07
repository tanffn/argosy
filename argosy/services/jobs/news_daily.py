"""``NewsDailyJob`` — 17:00 IDT daily news pipeline (Spec A commit #7).

Wraps the two-stage news pipeline as a single :class:`CadenceLoop`:

  Stage 1 — :func:`argosy.services.news_ingest.run_news_ingest`
            (deterministic extractor; no LLM). Per-name RSS fetch covers the
            user's HELD SINGLE STOCKS, resolved from the latest portfolio
            snapshot at TICK time (``resolve_holdings_split``); ETFs/funds
            get index-level/macro treatment only.
  Stage 2 — :func:`argosy.services.news_analyst_runner.run_news_signal_analysis`
            (Opus analyst over batches of ≤20 signals). Gated by a
            deterministic TRIAGE: fires only when Stage 1 persisted new
            signals, or a held single stock crossed the volatility
            threshold (``ARGOSY_NEWS_VOLATILITY_MOVE_PCT``) with unanalyzed
            signals pending. Quiet day → no agent construction, summary
            ``reason='no new signals'``.

Both stages share **one** sync SQLAlchemy ``Session`` so a partial
ingest doesn't leak rows the analyst can't see. ``tick()`` runs the
sync work via :func:`asyncio.to_thread` because the analyst runner uses
:func:`asyncio.run` internally and cannot nest inside a running loop.

Per-stage outcome capture (codex NICE #7): when Stage 2 raises, Stage 1's
counts are still surfaced through ``self.last_output_summary`` — set in
a ``finally`` block so the
:class:`~argosy.services.jobs.registered_scheduler.RegisteredScheduler`
adapter can record "ingest ok, analyze error" in ``job_runs.output_summary``
even though the tick itself re-raised.

Same-code-path contract: the scheduler fires this on the 17:00 cadence
AND the manual ``Run now`` path (commit #4's
``POST /api/jobs/news_daily/run-now``) goes through the same
:meth:`tick` body — no parallel "manual" variant.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from argosy.agents.news_signal_analyst import NewsSignalAnalystAgent
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata
from argosy.services.news_analyst_runner import (
    AnalysisRunResult,
    run_news_signal_analysis,
)
from argosy.services.news_ingest import NewsIngestResult, run_news_ingest
from argosy.state.models import NewsSignal

_log = get_logger("argosy.jobs.news_daily")


# ---------------------------------------------------------------------------
# Tick-time holdings resolution — the book, split single-stocks vs funds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldingsTickers:
    """The user's book split by instrument STRUCTURE for news treatment.

    ``single_stocks`` get per-name headline fetch at full priority (NVDA +
    the moonshot sleeve + any held equity single name). ``funds`` (ETFs /
    index trackers) get index-level/macro treatment ONLY — an ETF's news is
    its market's news, so no per-ETF headline fetch.
    """

    single_stocks: tuple[str, ...] = field(default_factory=tuple)
    funds: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_symbols(self) -> list[str]:
        return list(self.single_stocks) + list(self.funds)


def resolve_holdings_split(session: Session, user_id: str) -> HoldingsTickers:
    """Resolve the CURRENT holdings from the latest portfolio snapshot and
    split single stocks from funds — at TICK time, never construction time
    (positions change daily).

    The single-stock/fund split is derived from the instrument reference's
    ``structure`` axis (``Stock`` vs ``ETF``/``REIT``/``Bond``/``Cash``) —
    the same curated data every allocation surface uses. An instrument the
    reference doesn't know falls back to the snapshot's ``details`` text
    ("Stock, AI" → single stock); with no evidence either way it gets the
    LIGHT (fund/macro) treatment and is logged for curation, never a
    hardcoded ticker list.

    Best-effort: no snapshot → empty split (macro-only day).
    """
    from argosy.services.allocation_engine import is_cash_position
    from argosy.services.instrument_reference import STRUCT_STOCK, lookup
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )

    row = get_latest_snapshot_row(session, user_id)
    if row is None:
        return HoldingsTickers()
    snapshot = row_to_snapshot(row)

    singles: list[str] = []
    funds: list[str] = []
    seen: set[str] = set()
    for p in snapshot.positions or []:
        if is_cash_position(p):
            continue
        sym = (getattr(p, "symbol", "") or "").strip().upper()
        if not sym or sym == "-" or sym in seen:
            continue
        usd_k = getattr(p, "usd_value_k", None) or 0.0
        if not usd_k:
            continue  # stale zero-value row — mirrors tradeable_holdings
        seen.add(sym)
        details = getattr(p, "details", "") or ""
        ref = lookup(sym, details)
        if ref is not None:
            (singles if ref.structure == STRUCT_STOCK else funds).append(sym)
        elif "stock" in details.lower() and sym.isascii():
            singles.append(sym)
        else:
            # Unknown instrument with no single-stock evidence: light
            # treatment (its news is its market's news) + log for curation.
            _log.info(
                "news_daily.holdings.unclassified_light_treatment",
                symbol=sym, details=details[:60],
            )
            funds.append(sym)
    return HoldingsTickers(
        single_stocks=tuple(sorted(singles)), funds=tuple(sorted(funds)),
    )


def _default_price_moves(tickers: list[str]) -> dict[str, float]:
    """Cheap close-over-close move sweep for the held single-stock list.

    Returns ``{ticker: pct_move}`` (signed, e.g. ``-5.2``) for each ticker
    with at least two closes; a ticker whose fetch fails is skipped (logged),
    never fabricated. Mirrors ``speculative_monitor._fetch_history_stats``'s
    best-effort yfinance pattern. No LLM — this feeds the deterministic
    Stage-2 triage gate only.
    """
    out: dict[str, float] = {}
    for t in tickers:
        try:
            import yfinance as yf

            hist = yf.Ticker(t).history(period="7d", auto_adjust=True)
            if hist is None or hist.empty:
                continue
            closes = hist["Close"].dropna()
            if len(closes) < 2:
                continue
            prev, cur = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev:
                out[t] = (cur - prev) / prev * 100.0
        except Exception as exc:  # noqa: BLE001 — best-effort sweep
            _log.warning(
                "news_daily.price_sweep_failed", ticker=t, error=str(exc)[:120],
            )
    return out


# Default cron + tz — kept in sync with cadences.news_daily in
# agent_settings.py. The cadence-config value takes precedence at boot
# time (commit #3b's startup hook will pass `cadences.news_daily` through
# `LoopSchedule.from_config`); this default lets tests construct the job
# without a full AgentSettings round-trip.
_DEFAULT_CRON = "0 17 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"


def news_daily_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row for the registry.

    Imported by ``argosy/api/main.py``'s guarded-import block (already
    present from commit #3b); the registration call happens there.
    """
    return JobMetadata(
        name="news_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 17:00 IDT",
        source_kind="ingest",
        description=(
            "Daily news pipeline — Stage 1 RSS+macro_feed ingest + Stage 2 "
            "Opus analyst classification. Runs at 17:00 IL-local; manual "
            "Run now goes through the same tick body."
        ),
        long_running=False,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Module-level cache for the sync engine + sessionmaker. We build it
# lazily on first use (so import-time has no side effects) and reuse
# across ticks (so we don't churn an engine + connection pool every
# 17:00 IDT). Keyed by db_file path so a settings-reload that points
# at a different DB transparently rebuilds.
_DEFAULT_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Return the cached sync ``sessionmaker`` bound to the configured DB.

    Mirrors the pattern in ``argosy/cli/expenses_admin.py`` — the news
    pipeline services (``run_news_ingest`` + ``run_news_signal_analysis``)
    require a SYNC ``Session``; the async ``argosy.state.db.get_session``
    yields an :class:`AsyncSession` which they cannot consume.

    Lifecycle: the engine + sessionmaker are built on first use and
    reused for the process lifetime. Rebuilds only when ``db_file``
    changes (e.g. a test reloads settings to point at a fresh
    ``tmp_path``). Tests inject their own factory via the constructor.

    Codex review (commit #7) flagged a per-tick rebuild here that
    leaked engines + connection pools at 17:00 IDT every day; this
    cache fixes that.
    """
    global _DEFAULT_SESSION_FACTORY

    import sqlalchemy as sa

    from argosy.config import get_settings

    settings = get_settings()
    db_file = str(settings.db_file)

    if _DEFAULT_SESSION_FACTORY is not None:
        cached_key, cached_factory = _DEFAULT_SESSION_FACTORY
        if cached_key == db_file:
            return cached_factory

    sync_url = f"sqlite:///{db_file}"
    engine = sa.create_engine(
        sync_url, connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _DEFAULT_SESSION_FACTORY = (db_file, factory)
    return factory


def _reset_default_session_factory_cache() -> None:
    """Test hook — clear the cached sessionmaker so a subsequent call
    rebuilds from the current settings. Production code never invokes
    this; pytest fixtures using ``monkeypatch.setenv("ARGOSY_HOME", ...)``
    may need it if they rely on the default factory."""
    global _DEFAULT_SESSION_FACTORY
    _DEFAULT_SESSION_FACTORY = None


class NewsDailyJob(CadenceLoop):
    """Daily news ingest + analyst loop.

    Constructor accepts optional injection points so the unit tests can
    swap in stubs without touching the DB or the SDK:

    * ``schedule``         — overrides the default cron/tz (tests pass
                              an interval-based ``LoopSchedule``).
    * ``session_factory``  — sync ``sessionmaker``; default builds one
                              from ``get_settings().db_file``.
    * ``ingest_fn``        — overrides ``run_news_ingest``.
    * ``analyst_fn``       — overrides ``run_news_signal_analysis``.
    * ``agent_factory``    — overrides ``NewsSignalAnalystAgent``.
    * ``user_holdings``    — ticker symbols threaded into the analyst's
                              materiality context. Default (empty) resolves
                              the user's CURRENT positions from the latest
                              snapshot at tick time.
    * ``tickers``          — explicit per-name RSS fetch list. Default
                              ``None`` resolves held SINGLE STOCKS from the
                              latest snapshot at tick time (ETFs/funds get
                              index-level/macro treatment only).
    * ``holdings_resolver``— overrides ``resolve_holdings_split``.
    * ``price_move_fn``    — overrides the yfinance close-over-close sweep
                              feeding the volatility trigger.
    * ``volatility_move_pct`` — overrides the config threshold
                              (``ARGOSY_NEWS_VOLATILITY_MOVE_PCT``).

    Stage-2 triage (deterministic — decides WHETHER to spend LLM; the
    analyst does all judgment): the analyst batch fires only when Stage 1
    persisted new (non-duplicate) signals, OR a held single stock moved
    beyond the volatility threshold AND unanalyzed signals are pending. A
    quiet day skips agent construction entirely and reports
    ``reason='no new signals'``.
    """

    name = "news_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        ingest_fn: Callable[..., NewsIngestResult] | None = None,
        analyst_fn: Callable[..., AnalysisRunResult] | None = None,
        agent_factory: Callable[[], NewsSignalAnalystAgent] | None = None,
        user_holdings: list[str] | None = None,
        tickers: list[str] | None = None,
        holdings_resolver: Callable[[Session, str], HoldingsTickers] | None = None,
        price_move_fn: Callable[[list[str]], dict[str, float]] | None = None,
        volatility_move_pct: float | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule
            or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._ingest_fn = ingest_fn or run_news_ingest
        self._analyst_fn = analyst_fn or run_news_signal_analysis
        self._agent_factory = agent_factory or (
            lambda: NewsSignalAnalystAgent(user_id=self.user_id)
        )
        self._user_holdings = user_holdings or []
        self._tickers = tickers
        self._holdings_resolver = holdings_resolver or resolve_holdings_split
        self._price_move_fn = price_move_fn or _default_price_moves
        self._volatility_move_pct = volatility_move_pct
        #: Populated in :meth:`tick`'s ``finally`` so the
        #: :class:`RegisteredScheduler` adapter can read partial-progress
        #: results when Stage 2 raises (codex NICE #7).
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(
        self, *, now: Callable[[], datetime] | None = None
    ) -> dict | None:
        """Run Stage 1 ingest + Stage 2 analyst in one sync session.

        Returns the ``output_summary`` dict on success; raises (after
        populating ``self.last_output_summary`` in ``finally``) when
        Stage 2 fails. Stage 1's counts are always recorded — partial
        progress is observable in both the success and the
        Stage-2-failure cases.
        """
        run_at = (now or _utcnow)()
        _log.info("news_daily.tick.start", run_at=run_at.isoformat())

        # Reset the side-channel BEFORE any work — codex review (commit
        # #7) flagged a stale-summary bug where session_factory()
        # raising before the `try/finally` block would leave the
        # adapter reading the PRIOR tick's summary. By clearing here,
        # any unhandled-before-finally raise leaves last_output_summary
        # at None and the adapter records NULL for output_summary.
        self.last_output_summary = None

        factory = self._session_factory or _build_default_session_factory()

        # The sync session work runs in a thread so the analyst runner's
        # internal ``asyncio.run`` doesn't collide with our event loop.
        # Tests that pass a sync ``session_factory`` directly still go
        # through this path — ``asyncio.to_thread`` is a no-op for already
        # synchronous work and keeps the production + test paths
        # identical.
        return await asyncio.to_thread(self._run_stages_sync, factory)

    def _run_stages_sync(
        self, session_factory: Callable[[], Session]
    ) -> dict[str, Any]:
        """Synchronous body — one Session crosses both stages.

        Splitting this out lets tests assert on session sharing (the
        same ``Session`` object is passed to ``ingest_fn`` and
        ``analyst_fn``) without dealing with the async wrapper.
        """
        stage1_result: NewsIngestResult | None = None
        stage2_result: AnalysisRunResult | None = None
        stage1_status = "pending"
        stage2_status = "pending"
        stage1_error: str | None = None
        stage2_error: str | None = None
        tickers_info: dict[str, Any] | None = None
        gate: dict[str, Any] | None = None
        skip_reason: str | None = None

        session = session_factory()
        try:
            # ------------------------------------------------------------
            # Tick-time holdings resolution — positions change daily, so
            # the per-name fetch list comes from the LATEST snapshot at
            # tick time, never construction time. Held single stocks get
            # full per-name RSS priority; ETFs/funds stay index/macro-only.
            # ------------------------------------------------------------
            tickers = self._tickers
            known_tickers: frozenset[str] | None = None
            single_stocks: list[str]
            if tickers is None:
                try:
                    resolved = self._holdings_resolver(session, self.user_id)
                except Exception:  # noqa: BLE001 — best-effort; macro-only day
                    _log.exception("news_daily.holdings_resolve_failed")
                    resolved = HoldingsTickers()
                single_stocks = list(resolved.single_stocks)
                tickers = single_stocks
                tickers_info = {
                    "single_stocks": single_stocks,
                    "funds_light_treatment": len(resolved.funds),
                }
                held = resolved.all_symbols
                if held:
                    # Extend the extractor whitelist with the actual book so
                    # held moonshot names parse as tickers downstream.
                    from argosy.services.news_extractor import (
                        KNOWN_TICKERS_DEFAULT,
                    )

                    known_tickers = frozenset(
                        KNOWN_TICKERS_DEFAULT | {s.upper() for s in held}
                    )
            else:
                single_stocks = list(tickers)
                tickers_info = {
                    "single_stocks": single_stocks,
                    "funds_light_treatment": 0,
                }
            user_holdings = self._user_holdings or (
                resolved.all_symbols if self._tickers is None else single_stocks
            )

            # ------------------------------------------------------------
            # Stage 1 — deterministic ingest. No LLM.
            # ------------------------------------------------------------
            try:
                stage1_result = self._ingest_fn(
                    session, tickers=tickers or None,
                    known_tickers=known_tickers,
                )
                stage1_status = "ok"
                # Commit before Stage 2 so the analyst sees Stage 1's
                # rows even if Stage 2 rolls back.
                session.commit()
            except Exception as exc:
                stage1_status = "error"
                stage1_error = str(exc)
                _log.exception("news_daily.stage1_failed")
                session.rollback()
                # Re-raise: Stage 1 failure means we have nothing for
                # Stage 2 to analyze anyway.
                raise

            # ------------------------------------------------------------
            # Stage-2 triage gate — deterministic, cheap-first. Decides
            # WHETHER to spend LLM; the analyst does all judgment. Fires
            # when Stage 1 persisted anything new, or (only then checked —
            # keep the network sweep off the common path) a held single
            # stock moved beyond the volatility threshold while unanalyzed
            # signals are pending. A quiet day never constructs the agent.
            # ------------------------------------------------------------
            pending = int(
                session.execute(
                    select(func.count())
                    .select_from(NewsSignal)
                    .where(NewsSignal.analyzed_at.is_(None))
                ).scalar_one()
            )
            fire = (stage1_result.persisted or 0) > 0
            reasons: list[str] = ["new_signals"] if fire else []
            vol_moves: dict[str, float] = {}
            if not fire and single_stocks:
                threshold = self._volatility_move_pct
                if threshold is None:
                    from argosy.config import get_settings

                    threshold = get_settings().news_volatility_move_pct
                if threshold > 0:
                    try:
                        moves = self._price_move_fn(single_stocks)
                    except Exception:  # noqa: BLE001 — best-effort sweep
                        _log.exception("news_daily.price_sweep_failed")
                        moves = {}
                    vol_moves = {
                        t: round(m, 2)
                        for t, m in (moves or {}).items()
                        if abs(m) >= threshold
                    }
                    if vol_moves and pending > 0:
                        fire = True
                        reasons.append("volatility_trigger")
            gate = {
                "fired": fire,
                "reasons": reasons,
                "pending_unanalyzed": pending,
                "volatility_moves": vol_moves,
            }

            if not fire:
                stage2_status = "skipped"
                skip_reason = "no new signals"
                if vol_moves:
                    # Moves crossed the threshold but nothing is pending —
                    # the analyst would have zero rows to read.
                    skip_reason = (
                        "no new signals (volatility trigger with no "
                        "unanalyzed signals)"
                    )
                _log.info(
                    "news_daily.stage2_skipped",
                    reason=skip_reason,
                    pending_unanalyzed=pending,
                )
            else:
                # --------------------------------------------------------
                # Stage 2 — Opus analyst over unanalyzed rows.
                # Agent construction lives INSIDE the try block (codex
                # commit #7 review): NewsSignalAnalystAgent.__init__ does
                # SDK setup that can fail (missing API key, network probe,
                # etc.). If agent construction raises, that's a Stage 2
                # failure mode — classifying it as `analyze='pending'`
                # would lie to the operator.
                # --------------------------------------------------------
                try:
                    agent = self._agent_factory()
                    stage2_result = self._analyst_fn(
                        session,
                        agent=agent,
                        user_holdings=user_holdings,
                    )
                    stage2_status = "ok"
                    session.commit()
                except Exception as exc:
                    stage2_status = "error"
                    stage2_error = str(exc)
                    _log.exception("news_daily.stage2_failed")
                    session.rollback()
                    raise
        finally:
            # Populate the side-channel BEFORE closing the session so the
            # adapter reads a complete dict even on the exception path.
            self.last_output_summary = _build_summary(
                stage1_result=stage1_result,
                stage2_result=stage2_result,
                stage1_status=stage1_status,
                stage2_status=stage2_status,
                stage1_error=stage1_error,
                stage2_error=stage2_error,
                tickers_info=tickers_info,
                gate=gate,
                reason=skip_reason,
            )
            session.close()

        _log.info(
            "news_daily.tick.done",
            ingested=stage1_result.persisted if stage1_result else 0,
            analyzed=stage2_result.analyzed if stage2_result else 0,
        )
        return self.last_output_summary


def _build_summary(
    *,
    stage1_result: NewsIngestResult | None,
    stage2_result: AnalysisRunResult | None,
    stage1_status: str,
    stage2_status: str,
    stage1_error: str | None,
    stage2_error: str | None,
    tickers_info: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Render the ``output_summary`` dict in the spec's shape.

    Always returns a dict — even when both stages failed, the operator
    sees ``stages={ingest: <status>, analyze: <status>}`` and an empty
    counts block. This keeps the ``job_runs.output_summary`` column
    queryable: every news_daily row has the same top-level keys.

    ``tickers`` (tick-time snapshot resolution), ``stage2_gate`` (the
    deterministic triage verdict) and ``reason`` (quiet-day explanation,
    e.g. ``"no new signals"``) are present when the smart-intake path
    produced them.
    """
    counts: dict[str, int] = {
        "ingested_fetched": stage1_result.fetched if stage1_result else 0,
        "ingested_persisted": stage1_result.persisted if stage1_result else 0,
        "ingested_duplicates": stage1_result.duplicates if stage1_result else 0,
        "analyzed": stage2_result.analyzed if stage2_result else 0,
        "analyzed_batches": stage2_result.batches if stage2_result else 0,
    }
    stage_errors: dict[str, str] = {}
    if stage1_error:
        stage_errors["ingest"] = stage1_error
    if stage2_error:
        stage_errors["analyze"] = stage2_error

    notes = (
        f"by_source={stage1_result.by_source!r}"
        if stage1_result is not None
        else "no_stage1_result"
    )

    out: dict[str, Any] = {
        "counts": counts,
        "stages": {
            "ingest": stage1_status,
            "analyze": stage2_status,
        },
        "stage_errors": stage_errors,
        "notes": notes,
    }
    if tickers_info is not None:
        out["tickers"] = tickers_info
    if gate is not None:
        out["stage2_gate"] = gate
    if reason is not None:
        out["analyzed"] = counts["analyzed"]
        out["reason"] = reason
    return out


__all__ = [
    "HoldingsTickers",
    "NewsDailyJob",
    "news_daily_metadata",
    "resolve_holdings_split",
]
