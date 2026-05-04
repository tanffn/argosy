"""Daily Brief loop (SDD §5.1, Phase 2).

Runs at the configured cron (default `0 9 * * *` Asia/Jerusalem). Pulls
overnight news + macro state, computes concentration deltas vs plan
targets, asks the plan-critique agent for a one-shot adherence delta,
and writes a `daily_briefs` row. Emits a `daily_brief.ready` WebSocket
event.

The loop is a thin orchestrator: it composes the four analyst agents +
data adapters via dependency injection so tests can mock everything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from argosy.adapters import (
    MissingAPIKeyError as AdapterMissingAPIKeyError,
)
from argosy.adapters import (
    MissingDataSourceError,
)
from argosy.agents.concentration_analyst import ConcentrationAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api.events import publish_event
from argosy.config import get_settings
from argosy.logging import get_logger
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import DailyBrief, PlanCritique, PlanVersion
from argosy.state.queries import record_investor_events

_log = get_logger("argosy.loops.daily_brief")


@dataclass
class DailyBriefInputs:
    """Inputs to one daily-brief run, gathered before the LLM calls.

    Tests construct this directly to avoid touching adapters.

    The investor-event fields (``insider_activity``, ``analyst_signals``,
    ``thirteen_f_watchlist``) carry data pulled from the Phase 4
    adapters; each one degrades to an empty dict when its adapter is
    unreachable or unconfigured. Analyst agents that already accept a
    ``payload`` dict (news / sentiment / concentration) can opt-in to
    consuming this auxiliary context without prompt changes.
    """

    user_id: str
    tickers: list[str]
    news_payload: dict[str, list[dict[str, Any]]]
    macro_snapshot: dict[str, float]
    positions_summary: str
    plan_targets: dict[str, float]
    nvda_shares_sold_ytd: int
    nvda_target_shares_ytd: int
    plan_label: str
    plan_markdown: str
    # Phase 4 — investor-event payloads. Empty by default so existing
    # tests that construct ``DailyBriefInputs(...)`` keep working.
    insider_activity: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    analyst_signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    thirteen_f_watchlist: list[dict[str, Any]] = field(default_factory=list)
    # Per-ticker CapitolTrades (STOCK Act) rows. Mirrors
    # ``insider_activity`` — agents that already accept ``payload`` dicts
    # can opt-in by reading this field.
    capitoltrades_signals: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )


class DailyBriefLoop(CadenceLoop):
    """Wires news + macro + concentration analysts + plan critique into one daily run."""

    name = "daily_brief"

    def __init__(
        self,
        *,
        schedule: LoopSchedule,
        enabled: bool = True,
        user_id: str = "ariel",
        news_agent_factory: Callable[[], NewsAnalystAgent] | None = None,
        macro_agent_factory: Callable[[], MacroAnalystAgent] | None = None,
        concentration_agent_factory: Callable[[], ConcentrationAnalystAgent] | None = None,
        plan_critique_agent_factory: Callable[[], PlanCritiqueAgent] | None = None,
        gather_inputs: Callable[[str], DailyBriefInputs] | None = None,
    ) -> None:
        super().__init__(schedule=schedule, enabled=enabled)
        self.user_id = user_id
        self._news_factory = news_agent_factory or (lambda: NewsAnalystAgent(user_id=user_id))
        self._macro_factory = macro_agent_factory or (lambda: MacroAnalystAgent(user_id=user_id))
        self._concentration_factory = concentration_agent_factory or (
            lambda: ConcentrationAnalystAgent(user_id=user_id)
        )
        self._plan_critique_factory = plan_critique_agent_factory or (
            lambda: PlanCritiqueAgent(user_id=user_id)
        )
        self._gather = gather_inputs or _default_gather_inputs

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> None:
        """Run one Daily Brief end-to-end."""
        run_at = (now or _utcnow)()
        inputs = await self._maybe_async(self._gather(self.user_id))
        if not isinstance(inputs, DailyBriefInputs):  # pragma: no cover - defensive
            raise TypeError(
                f"gather_inputs must return DailyBriefInputs, got {type(inputs)!r}"
            )

        # 1. News digest
        news_agent = self._news_factory()
        news_report = await news_agent.run(
            tickers=inputs.tickers,
            news_payload=inputs.news_payload,
        )

        # 2. Macro regime
        macro_agent = self._macro_factory()
        macro_report = await macro_agent.run(macro_snapshot=inputs.macro_snapshot)

        # 3. Concentration deltas
        conc_agent = self._concentration_factory()
        conc_report = await conc_agent.run(
            positions_summary=inputs.positions_summary,
            plan_targets=inputs.plan_targets,
            nvda_shares_sold_ytd=inputs.nvda_shares_sold_ytd,
            nvda_target_shares_ytd=inputs.nvda_target_shares_ytd,
        )

        # 4. Plan adherence delta (re-run plan-critique with today's snapshot
        # so the home page shows fresh RED/YELLOW/GREEN findings).
        critique_agent = self._plan_critique_factory()
        critique_report = await critique_agent.run(
            plan_label=inputs.plan_label,
            plan_markdown=inputs.plan_markdown,
            snapshot_label=f"daily_brief:{run_at.isoformat()}",
            snapshot_summary=inputs.positions_summary,
            user_context_yaml="",
            domain_kb_files={},
        )

        # 5. Compose summary text from all four reports.
        summary = _compose_summary(
            news=news_report.output,
            macro=macro_report.output,
            concentration=conc_report.output,
            plan=critique_report.output,
        )

        # 6. Persist daily_briefs row
        async with db_mod.get_session() as session:
            row = DailyBrief(
                user_id=self.user_id,
                run_at=run_at,
                summary_text=summary,
                news_report_json=news_report.output.model_dump_json(),
                macro_report_json=macro_report.output.model_dump_json(),
                concentration_report_json=conc_report.output.model_dump_json(),
                plan_delta_json=critique_report.output.model_dump_json(),
            )
            session.add(row)
            await session.commit()
            brief_id = row.id

        _log.info("daily_brief.persisted", brief_id=brief_id, user_id=self.user_id)

        # 7. Emit WebSocket event (best-effort; never crash on a broken pubsub).
        try:
            await publish_event(
                "daily_brief.ready",
                {"brief_id": brief_id, "user_id": self.user_id, "run_at": run_at.isoformat()},
            )
        except Exception:  # pragma: no cover - defensive
            _log.exception("daily_brief.publish_failed")

    @staticmethod
    async def _maybe_async(value: Any) -> Any:
        if hasattr(value, "__await__"):
            return await value
        return value


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _compose_summary(*, news: Any, macro: Any, concentration: Any, plan: Any) -> str:
    parts: list[str] = []
    parts.append("=== DAILY BRIEF ===")
    parts.append(f"News: {getattr(news, 'top_line', '(no news headlines)')}")
    regime = getattr(macro, "regime", None)
    drivers = getattr(macro, "drivers", []) or []
    parts.append(f"Macro regime: {regime}; drivers: {', '.join(drivers) or '(none)'}")
    breaches = getattr(concentration, "breaches", []) or []
    if breaches:
        parts.append(f"Concentration: {len(breaches)} breach(es)")
    else:
        parts.append("Concentration: within caps")
    overall = getattr(plan, "overall_summary", "")
    if overall:
        parts.append(f"Plan delta: {overall}")
    return "\n".join(parts)


async def _default_gather_inputs(user_id: str) -> DailyBriefInputs:
    """Default input gatherer for production runs.

    Best-effort wiring of the real adapters with graceful degradation:
    - Latest TSV at ARGOSY_HOME → tickers + positions_summary
    - Finnhub → per-ticker news payload (skipped if API key missing)
    - FRED + BoI → macro snapshot (skipped on adapter / network error)
    - DB → latest plan_versions row → plan_label + plan_markdown

    Each section degrades independently to an empty payload with a
    structured warning so the LLM agents see "this section is empty"
    rather than fabricated content.
    """
    plan_label = "(no plan imported)"
    plan_markdown = ""
    plan_targets: dict[str, float] = {}
    tickers: list[str] = []
    positions_summary = "(no portfolio snapshot ingested today)"

    async with db_mod.get_session() as session:
        plan = (
            await session.execute(
                select(PlanVersion)
                .where(PlanVersion.user_id == user_id)
                .order_by(desc(PlanVersion.imported_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is not None:
            plan_label = plan.version_label or f"plan_version_id={plan.id}"
            plan_markdown = plan.raw_markdown
        # Latest critique can carry plan-target hints in future phases;
        # for Phase 2 we just record that one was found.
        _ = (
            await session.execute(
                select(PlanCritique)
                .where(PlanCritique.user_id == user_id)
                .order_by(desc(PlanCritique.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

    # 1. Portfolio snapshot from latest TSV under ARGOSY_HOME.
    try:
        tsv_path = _find_latest_tsv()
        if tsv_path is not None:
            from argosy.ingest.tsv import parse_portfolio_tsv

            snapshot = parse_portfolio_tsv(tsv_path)
            tickers = sorted({p.ticker for p in snapshot.positions if p.ticker})
            positions_summary = _summarize_positions(snapshot)
        else:
            _log.warning("daily_brief.no_tsv_found", user_id=user_id)
    except Exception:  # pragma: no cover - defensive (fallback to empty)
        _log.exception("daily_brief.tsv_parse_failed", user_id=user_id)

    # 2. News via Finnhub (best-effort).
    news_payload: dict[str, list[dict[str, Any]]] = {}
    if tickers:
        try:
            from datetime import date as _date
            from datetime import timedelta as _td

            from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

            adapter = FinnhubAdapter()
            today_d = _date.today()
            yesterday_d = today_d - _td(days=1)
            for ticker in tickers[:25]:  # cap to avoid rate-limit blow-up
                try:
                    headlines = await adapter.get_company_news(
                        ticker, start=yesterday_d, end=today_d
                    )
                except Exception:  # pragma: no cover - per-ticker defensive
                    _log.warning("daily_brief.news_per_ticker_failed", ticker=ticker)
                    continue
                if headlines:
                    news_payload[ticker] = headlines
            # Persist news as investor_events so the home brief's signal
            # bullet can pick the most-recent headline alongside Form 4
            # / 13F / TipRanks / CapitolTrades. We persist as a single
            # batch keyed by ticker → list[item].
            if news_payload:
                try:
                    await record_investor_events(user_id, "news", news_payload)
                except Exception:  # pragma: no cover - defensive
                    _log.exception("daily_brief.news_persist_failed")
        except AdapterMissingAPIKeyError as e:
            _log.warning("daily_brief.news_skipped_no_key", reason=str(e).splitlines()[0])
        except Exception:  # pragma: no cover - network/library defensive
            _log.exception("daily_brief.news_fetch_failed")

    # 3a. Insider activity (SEC Form 4) per portfolio ticker.
    # Cap fan-out to 10 tickers per day. SEC has a ~10 req/s limit and
    # each ticker fans out into multiple sub-requests (atom + index +
    # per-filing XML); 10 keeps us comfortably below the cap on a single
    # daily-brief tick and matches the documented Phase 4 design.
    insider_activity: dict[str, list[dict[str, Any]]] = {}
    if tickers:
        try:
            from argosy.adapters.data.sec_form4_adapter import SecForm4Adapter

            form4 = SecForm4Adapter()
            for ticker in tickers[:10]:
                try:
                    rows = await form4.get_recent_form4_for_ticker(ticker, days=30)
                except MissingDataSourceError as e:
                    _log.warning(
                        "daily_brief.form4_skipped",
                        ticker=ticker, reason=str(e).splitlines()[0],
                    )
                    continue
                if rows:
                    insider_activity[ticker] = rows
                    # Persist each Form 4 row so the home brief can pick
                    # the most-recent event without depending on the
                    # 24h kv_cache row staying alive past its TTL.
                    try:
                        await record_investor_events(user_id, "sec_form4", rows)
                    except Exception:  # pragma: no cover - defensive
                        _log.exception(
                            "daily_brief.form4_persist_failed", ticker=ticker
                        )
        except Exception:  # pragma: no cover - defensive
            _log.exception("daily_brief.form4_failed")

    # 3b. Analyst sentiment (TipRanks consensus) per portfolio ticker.
    analyst_signals: dict[str, dict[str, Any]] = {}
    if tickers:
        try:
            from argosy.adapters.data.tipranks_adapter import TipRanksAdapter

            tr = TipRanksAdapter()
            # TipRanks has aggressive rate-limits on the free tier; cap
            # concurrent lookups to a small number.
            for ticker in tickers[:10]:
                try:
                    consensus = await tr.get_analyst_consensus(ticker)
                except MissingDataSourceError as e:
                    _log.warning(
                        "daily_brief.tipranks_skipped",
                        ticker=ticker, reason=str(e).splitlines()[0],
                    )
                    continue
                analyst_signals[ticker] = consensus
            # Persist all collected analyst signals as a single batch —
            # the mapper turns ``{ticker: consensus}`` into one
            # investor_events row per ticker.
            if analyst_signals:
                try:
                    await record_investor_events(
                        user_id, "tipranks", analyst_signals
                    )
                except Exception:  # pragma: no cover - defensive
                    _log.exception("daily_brief.tipranks_persist_failed")
        except Exception:  # pragma: no cover - defensive
            _log.exception("daily_brief.tipranks_failed")

    # 3c. CapitolTrades (STOCK Act disclosures) per portfolio ticker.
    # Cap fan-out at 10 tickers/day — the public site is rate-limited
    # and ten covers a typical concentrated portfolio. Persist each
    # batch into investor_events so the home brief's signal bullet
    # can surface the most-recent politician trade alongside Form 4 /
    # 13F / TipRanks events.
    capitoltrades: dict[str, list[dict[str, Any]]] = {}
    if tickers:
        try:
            from argosy.adapters.data.capitoltrades_adapter import (
                CapitolTradesAdapter,
            )

            ct = CapitolTradesAdapter()
            for ticker in tickers[:10]:
                try:
                    rows = await ct.list_trades_for_ticker(ticker, days=30)
                except MissingDataSourceError as e:
                    _log.warning(
                        "daily_brief.capitoltrades_skipped",
                        ticker=ticker, reason=str(e).splitlines()[0],
                    )
                    continue
                if rows:
                    capitoltrades[ticker] = rows
                    try:
                        await record_investor_events(
                            user_id, "capitoltrades", rows
                        )
                    except Exception:  # pragma: no cover - defensive
                        _log.exception(
                            "daily_brief.capitoltrades_persist_failed",
                            ticker=ticker,
                        )
        except Exception:  # pragma: no cover - defensive
            _log.exception("daily_brief.capitoltrades_failed")

    # 3d. 13F watchlist — pull most-recent filings for filers the user
    # follows. We read the watchlist from identity_yaml (key:
    # ``thirteen_f_watchlist: [<cik>, ...]``); empty by default.
    thirteen_f_watchlist: list[dict[str, Any]] = []
    cik_watchlist = await _resolve_thirteen_f_watchlist(user_id)
    if cik_watchlist:
        try:
            from argosy.adapters.data.sec_13f_adapter import Sec13FAdapter

            sec13f = Sec13FAdapter()
            for cik in cik_watchlist[:10]:
                try:
                    history = await sec13f.get_filer_history(cik, quarters=1)
                except MissingDataSourceError as e:
                    _log.warning(
                        "daily_brief.sec13f_skipped",
                        cik=cik, reason=str(e).splitlines()[0],
                    )
                    continue
                if history:
                    thirteen_f_watchlist.append(
                        {"cik": cik, "filings": history[:1]}
                    )
                    # Persist each filing as one investor_events row so
                    # the home brief picks the most-recent 13F when no
                    # fresher Form 4 / TipRanks signal exists.
                    try:
                        # ``history`` is a list of filing summaries;
                        # tag each with the cik for downstream context.
                        annotated = [
                            {**f, "cik": cik} for f in history[:1]
                            if isinstance(f, dict)
                        ]
                        await record_investor_events(
                            user_id, "sec_13f", annotated
                        )
                    except Exception:  # pragma: no cover - defensive
                        _log.exception(
                            "daily_brief.sec13f_persist_failed", cik=cik
                        )
        except Exception:  # pragma: no cover - defensive
            _log.exception("daily_brief.sec13f_failed")

    # 4. Macro snapshot via FRED + BoI (best-effort).
    macro_snapshot: dict[str, float] = {}
    try:
        from argosy.adapters.data.fred_adapter import FredAdapter

        fred = FredAdapter()
        # VIX, 10Y treasury, USD/NIS, oil. Specific series IDs are stable.
        for label, series in (
            ("vix", "VIXCLS"),
            ("ust_10y", "DGS10"),
            ("usd_nis", "DEXISUS"),
            ("oil_wti", "DCOILWTICO"),
        ):
            try:
                rows = await fred.get_series(series)
                # Take the most recent non-null observation.
                for row in reversed(rows):
                    val = row.get("value") if isinstance(row, dict) else None
                    if val is not None:
                        macro_snapshot[label] = float(val)
                        break
            except Exception:  # pragma: no cover - per-series defensive
                _log.warning("daily_brief.macro_series_failed", series=series)
    except AdapterMissingAPIKeyError as e:
        _log.warning("daily_brief.fred_skipped_no_key", reason=str(e).splitlines()[0])
    except Exception:  # pragma: no cover
        _log.exception("daily_brief.fred_failed")

    # If everything came back empty, log a clear warning so the user
    # understands why the brief reads thin.
    if not tickers and not news_payload and not macro_snapshot:
        _log.warning(
            "daily_brief.empty_payload",
            user_id=user_id,
            hint="No TSV under ARGOSY_HOME and no adapters configured. "
            "Run `argosy ingest tsv <path>` and set FRED/Finnhub keys via "
            "`argosy secrets set ...` to populate the brief.",
        )

    return DailyBriefInputs(
        user_id=user_id,
        tickers=tickers,
        news_payload=news_payload,
        macro_snapshot=macro_snapshot,
        positions_summary=positions_summary,
        plan_targets=plan_targets,
        nvda_shares_sold_ytd=0,
        nvda_target_shares_ytd=0,
        plan_label=plan_label,
        plan_markdown=plan_markdown,
        insider_activity=insider_activity,
        analyst_signals=analyst_signals,
        thirteen_f_watchlist=thirteen_f_watchlist,
        capitoltrades_signals=capitoltrades,
    )


async def _resolve_thirteen_f_watchlist(user_id: str) -> list[str]:
    """Read ``identity.thirteen_f_watchlist`` (list of CIKs) for ``user_id``.

    The watchlist lives in ``UserContext.identity_yaml`` under the key
    ``thirteen_f_watchlist``. Returns an empty list if absent / malformed.
    """
    try:
        import yaml

        from argosy.state.models import UserContext

        async with db_mod.get_session() as session:
            ctx = (
                await session.execute(
                    select(UserContext).where(UserContext.user_id == user_id)
                )
            ).scalar_one_or_none()
            if ctx is None or not ctx.identity_yaml:
                return []
            try:
                identity = yaml.safe_load(ctx.identity_yaml) or {}
            except yaml.YAMLError:
                return []
            if not isinstance(identity, dict):
                return []
            wl = identity.get("thirteen_f_watchlist") or []
            if not isinstance(wl, list):
                return []
            return [str(c).strip() for c in wl if str(c).strip()]
    except Exception:  # pragma: no cover - defensive
        return []


def _find_latest_tsv() -> Any | None:
    """Locate the newest `*.tsv` under ARGOSY_HOME (matches the portfolio route)."""
    settings = get_settings()
    candidates = sorted(
        settings.home.rglob("*.tsv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _summarize_positions(snapshot: Any) -> str:
    """One-line-per-position text summary the LLM can read directly."""
    lines: list[str] = []
    for p in getattr(snapshot, "positions", []) or []:
        ticker = getattr(p, "ticker", "?")
        qty = getattr(p, "quantity", None)
        value = getattr(p, "market_value", None) or getattr(p, "value", None)
        account = getattr(p, "account", "")
        lines.append(f"  {ticker:<8} qty={qty}  value={value}  acct={account}")
    if not lines:
        return "(no positions)"
    return "\n".join(lines)


__all__ = ["DailyBriefInputs", "DailyBriefLoop"]
