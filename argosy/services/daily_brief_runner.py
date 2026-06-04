"""Daily-brief production runner (T4.5).

Single entry point ``generate_daily_brief(user_id, db)`` that:

  1. Loads the current plan context — prefers the user's pending draft
     plan; falls back to the active baseline plan if no draft exists.
  2. Pulls overnight market deltas from FRED + Finnhub + yfinance via
     the existing adapter modules. Each adapter call is wrapped in
     ``track_adapter_call`` so failures surface as ``http_error`` /
     ``exception`` outcomes in the per-decision agent tree.
  3. Composes a one-pager markdown via ``DailyBrieferAgent`` (Sonnet —
     daily summary doesn't need Opus).
  4. Persists:
        - One ``decision_runs`` row with ``decision_kind='daily_brief'``
          and ``notes_json={"brief_date": "YYYY-MM-DD"}`` (the T4.4 UI
          row renderer keys off this).
        - One ``daily_briefs`` row carrying ``content_md`` +
          ``brief_date`` + a back-pointer ``decision_run_id``.

Idempotency: re-running for the same ``brief_date`` (default: today in
``Asia/Jerusalem``) UPDATES the existing row instead of creating a
duplicate. The partial unique index ``uq_daily_briefs_user_date``
enforces this at the DB level too.

Cost cap: ~$1/brief. ``DailyBrieferAgent.max_tokens=4096`` at Sonnet
rates ($15/M output) gives a hard ceiling of ~$0.06/output even
before counting inputs. We additionally validate ``cost_usd`` after
the call and log a warning if the cap is exceeded.

Graceful degradation: adapter failures during input gathering
DO NOT abort the run. The runner produces a brief that mentions
"no overnight data" when adapters are unreachable, so the home page
always has a fresh artifact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from argosy.agents.daily_briefer import DailyBrieferAgent, DailyBriefMarkdown
from argosy.logging import get_logger
from argosy.services.adapter_outcomes import track_adapter_call
from argosy.state import db as db_mod
from argosy.state.models import DailyBrief, DecisionRun, PlanVersion
from argosy.state.queries import get_pending_draft

_log = get_logger("argosy.daily_brief_runner")

# Per-brief soft cost cap. The agent is Sonnet with max_tokens=4096 so
# typical runs are ~$0.05-0.15; the cap is mostly a "we noticed
# something is off" alarm rather than a hard guard.
COST_CAP_USD = 1.0

# Default user timezone for "today's brief_date". Israel time per the
# project owner's location.
DEFAULT_USER_TZ = "Asia/Jerusalem"


# ----------------------------------------------------------------------
# Input gathering — adapter-tracked so failures show in the agent tree.
# ----------------------------------------------------------------------


@dataclass
class _RunnerInputs:
    """Adapter-derived inputs assembled for the briefer agent."""

    plan_label: str
    plan_markdown: str
    tickers: list[str]
    positions_summary: str
    macro_snapshot: dict[str, float]
    news_payload: dict[str, list[dict[str, Any]]]


async def _load_plan_context(user_id: str, db: AsyncSession) -> tuple[str, str]:
    """Return (plan_label, plan_markdown) for the daily brief.

    Preference order:
      1. Pending draft (user is currently iterating on a new plan)
      2. ``role='current'`` plan (the active baseline)
      3. Most recent ``role='superseded'`` (last-known good)
      4. ("(no plan)", "")
    """
    # Pending draft first.
    draft_q = (
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id, PlanVersion.role == "draft")
        .order_by(desc(PlanVersion.imported_at))
        .limit(1)
    )
    draft = (await db.execute(draft_q)).scalar_one_or_none()
    if draft is not None:
        return (
            draft.version_label or f"draft#{draft.id}",
            draft.raw_markdown or "",
        )

    current_q = (
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id, PlanVersion.role == "current")
        .order_by(desc(PlanVersion.imported_at))
        .limit(1)
    )
    current = (await db.execute(current_q)).scalar_one_or_none()
    if current is not None:
        return (
            current.version_label or f"plan#{current.id}",
            current.raw_markdown or "",
        )

    # Any plan at all, last-known.
    any_q = (
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id)
        .order_by(desc(PlanVersion.imported_at))
        .limit(1)
    )
    last = (await db.execute(any_q)).scalar_one_or_none()
    if last is not None:
        return (
            last.version_label or f"plan#{last.id}",
            last.raw_markdown or "",
        )

    return ("(no plan)", "")


async def _gather_macro_snapshot() -> dict[str, float]:
    """Best-effort macro snapshot via FRED. Each series tracked separately."""
    snapshot: dict[str, float] = {}
    try:
        from argosy.adapters import MissingAPIKeyError as AdapterMissingAPIKeyError
        from argosy.adapters.data.fred_adapter import FredAdapter

        fred = FredAdapter()
    except Exception as exc:  # pragma: no cover - import defensive
        _log.warning("daily_brief_runner.fred_import_failed", error=str(exc))
        return snapshot

    series_map = (
        ("vix", "VIXCLS"),
        ("ust_10y", "DGS10"),
        # FRED has no daily USD/ILS series (DEXISUS doesn't exist);
        # CCUSMA02ILM618N is the OECD monthly avg-of-daily rate.
        ("usd_nis", "CCUSMA02ILM618N"),
        ("oil_wti", "DCOILWTICO"),
    )
    for label, series in series_map:
        with track_adapter_call("fred", target=series) as outcome:
            try:
                rows = await fred.get_series(series)
            except AdapterMissingAPIKeyError as exc:
                _log.warning(
                    "daily_brief_runner.fred_missing_key",
                    series=series, reason=str(exc).splitlines()[0],
                )
                outcome.record_http_error(status_code=401, body=str(exc))
                continue
            except Exception as exc:
                _log.warning(
                    "daily_brief_runner.fred_series_failed",
                    series=series, error=str(exc),
                )
                # ``track_adapter_call`` records the exception
                # automatically when the with-body raises; here we
                # caught it ourselves, so log + record explicitly.
                outcome.record_exception(exc)
                continue
            if not rows:
                # outcome stays as "empty" (payload_size=0).
                continue
            for row in reversed(rows):
                val = row.get("value") if isinstance(row, dict) else None
                if val is not None:
                    snapshot[label] = float(val)
                    break
            outcome.set_payload_size_bytes(len(str(rows)))
    return snapshot


async def _gather_news(
    tickers: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Best-effort per-ticker headlines via Finnhub. Capped at 10 tickers."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not tickers:
        return out
    try:
        from datetime import date as _date
        from datetime import timedelta as _td

        from argosy.adapters import MissingAPIKeyError as AdapterMissingAPIKeyError
        from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

        finnhub = FinnhubAdapter()
    except Exception as exc:  # pragma: no cover - import defensive
        _log.warning("daily_brief_runner.finnhub_import_failed", error=str(exc))
        return out

    today_d = _date.today()
    yesterday_d = today_d - _td(days=1)
    for ticker in tickers[:10]:
        with track_adapter_call("finnhub", target=ticker) as outcome:
            try:
                headlines = await finnhub.get_company_news(
                    ticker, start=yesterday_d, end=today_d
                )
            except AdapterMissingAPIKeyError as exc:
                _log.warning(
                    "daily_brief_runner.finnhub_missing_key",
                    reason=str(exc).splitlines()[0],
                )
                outcome.record_http_error(status_code=401, body=str(exc))
                # No key → bail entirely; subsequent tickers will also fail.
                break
            except Exception as exc:
                _log.warning(
                    "daily_brief_runner.finnhub_failed",
                    ticker=ticker, error=str(exc),
                )
                outcome.record_exception(exc)
                continue
            if headlines:
                out[ticker] = headlines
                outcome.set_payload_size_bytes(len(str(headlines)))
    return out


def _load_portfolio_snapshot() -> tuple[list[str], str]:
    """Best-effort portfolio snapshot from the newest TSV under ARGOSY_HOME.

    Returns (tickers, positions_summary). Both empty if no TSV exists
    or parsing fails — the runner gracefully degrades.
    """
    try:
        from argosy.config import get_settings

        settings = get_settings()
        candidates = sorted(
            settings.home.rglob("*.tsv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return ([], "")
        tsv_path = candidates[0]
        from argosy.ingest.tsv import parse_portfolio_tsv

        snapshot = parse_portfolio_tsv(tsv_path)
        tickers = sorted({p.ticker for p in snapshot.positions if p.ticker})
        lines: list[str] = []
        for p in snapshot.positions:
            tk = getattr(p, "ticker", "?")
            qty = getattr(p, "quantity", None)
            val = getattr(p, "market_value", None) or getattr(p, "value", None)
            acct = getattr(p, "account", "")
            lines.append(f"  {tk:<8} qty={qty}  value={val}  acct={acct}")
        return (tickers, "\n".join(lines) or "(no positions)")
    except Exception as exc:  # pragma: no cover - defensive fallback
        _log.warning("daily_brief_runner.tsv_load_failed", error=str(exc))
        return ([], "")


async def _gather_inputs(user_id: str, db: AsyncSession) -> _RunnerInputs:
    plan_label, plan_markdown = await _load_plan_context(user_id, db)
    tickers, positions_summary = _load_portfolio_snapshot()
    macro_snapshot = await _gather_macro_snapshot()
    news_payload = await _gather_news(tickers)
    return _RunnerInputs(
        plan_label=plan_label,
        plan_markdown=plan_markdown,
        tickers=tickers,
        positions_summary=positions_summary,
        macro_snapshot=macro_snapshot,
        news_payload=news_payload,
    )


# ----------------------------------------------------------------------
# Public entry point.
# ----------------------------------------------------------------------


def _today_in(user_tz: str) -> date:
    """Calendar date in the user's timezone."""
    try:
        zone = ZoneInfo(user_tz)
    except Exception:  # pragma: no cover - invalid tz string falls back
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


async def generate_daily_brief(
    user_id: str,
    db: AsyncSession,
    *,
    brief_date: date | None = None,
    user_tz: str = DEFAULT_USER_TZ,
    agent: DailyBrieferAgent | None = None,
) -> DailyBrief:
    """Produce one daily brief for ``user_id`` and persist it.

    Args:
        user_id: the user.
        db: an open ``AsyncSession``. The runner commits the
            ``decision_runs`` row + ``daily_briefs`` row on this session.
        brief_date: the calendar date the brief covers. Defaults to
            today in ``user_tz``.
        user_tz: timezone for the ``brief_date`` default. Defaults to
            ``Asia/Jerusalem``.
        agent: optional pre-built agent (used by tests to inject a
            stubbed ``_call_model``). When None, a fresh
            ``DailyBrieferAgent`` is constructed.

    Returns the persisted ``DailyBrief`` ORM row (with its ``id``,
    ``decision_run_id``, and ``content_md`` populated).

    Idempotency: if a ``daily_briefs`` row with the same
    ``(user_id, brief_date)`` exists, it is UPDATED in place. A new
    ``decision_runs`` row is created on every call so the agent tree
    timeline is honest about how many times we ran.
    """
    target_date = brief_date or _today_in(user_tz)

    # 1. Create the decision_runs row up front so the agent-tree
    #    builder + audit log have a stable id to anchor agent reports
    #    against. status='running'; will be flipped to 'completed' on
    #    success or 'failed' on exception.
    started_at = datetime.now(UTC)
    decision_run = DecisionRun(
        user_id=user_id,
        ticker="(brief)",  # daily-brief is portfolio-wide
        tier="T0",  # tier doesn't apply; T0 = info-only
        decision_kind="daily_brief",
        started_at=started_at,
        status="running",
        notes_json=json.dumps({"brief_date": target_date.isoformat()}),
    )
    db.add(decision_run)
    await db.commit()
    await db.refresh(decision_run)

    try:
        inputs = await _gather_inputs(user_id, db)

        # 2. Compose the markdown via the agent.
        briefer = agent or DailyBrieferAgent(user_id=user_id)
        report = await briefer.run(
            plan_label=inputs.plan_label,
            plan_markdown=inputs.plan_markdown,
            positions_summary=inputs.positions_summary,
            macro_snapshot=inputs.macro_snapshot,
            news_payload=inputs.news_payload,
            decision_id=str(decision_run.id),
        )
        output: DailyBriefMarkdown = report.output

        # 3. Soft cost-cap warning.
        if report.cost_usd > COST_CAP_USD:
            _log.warning(
                "daily_brief_runner.cost_cap_exceeded",
                user_id=user_id,
                cost_usd=report.cost_usd,
                cap_usd=COST_CAP_USD,
                brief_date=target_date.isoformat(),
            )

        # 4. Upsert by (user_id, brief_date).
        existing_q = select(DailyBrief).where(
            DailyBrief.user_id == user_id,
            DailyBrief.brief_date == target_date,
        )
        existing = (await db.execute(existing_q)).scalar_one_or_none()
        run_at = datetime.now(UTC)
        if existing is not None:
            existing.run_at = run_at
            existing.content_md = output.content_md
            existing.summary_text = output.top_line
            existing.decision_run_id = decision_run.id
            row = existing
        else:
            row = DailyBrief(
                user_id=user_id,
                brief_date=target_date,
                run_at=run_at,
                content_md=output.content_md,
                summary_text=output.top_line,
                decision_run_id=decision_run.id,
                # Leave the legacy four-report-JSON columns empty —
                # they're owned by the Phase 2 DailyBriefLoop. T4.5
                # rows are distinguishable by ``brief_date IS NOT NULL``.
            )
            db.add(row)
        await db.commit()
        await db.refresh(row)

        # 5. Mark the decision_run completed.
        decision_run.status = "completed"
        decision_run.finished_at = datetime.now(UTC)
        await db.commit()

        _log.info(
            "daily_brief_runner.persisted",
            user_id=user_id,
            brief_date=target_date.isoformat(),
            brief_id=row.id,
            decision_run_id=decision_run.id,
            cost_usd=report.cost_usd,
        )
        return row

    except Exception as exc:
        # Best-effort flip to failed; never mask the original exception.
        try:
            decision_run.status = "failed"
            decision_run.finished_at = datetime.now(UTC)
            existing_notes = (
                json.loads(decision_run.notes_json)
                if decision_run.notes_json
                else {}
            )
            existing_notes["error"] = f"{type(exc).__name__}: {exc}"
            decision_run.notes_json = json.dumps(existing_notes)
            await db.commit()
        except Exception:  # pragma: no cover - defensive
            _log.exception("daily_brief_runner.failed_marking_failed")
        raise


# ----------------------------------------------------------------------
# Scheduler hook — gated by ARGOSY_DAILY_BRIEF_ENABLED.
# ----------------------------------------------------------------------


def is_enabled_for_runtime() -> bool:
    """Whether the T4.5 background scheduler should fire in this process.

    Gated by:
      - ARGOSY_DAILY_BRIEF_ENABLED=1 explicit opt-in (default: off)
      - pytest detection — if PYTEST_CURRENT_TEST is set, always off so
        importing the API in tests never fires a real run.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return os.environ.get("ARGOSY_DAILY_BRIEF_ENABLED", "").strip() == "1"


async def _sleep_until_07_local(user_tz: str = DEFAULT_USER_TZ) -> None:
    """Sleep until the next 07:00 in ``user_tz``."""
    import asyncio

    try:
        zone = ZoneInfo(user_tz)
    except Exception:  # pragma: no cover
        zone = ZoneInfo("UTC")
    now_local = datetime.now(zone)
    target = now_local.replace(hour=7, minute=0, second=0, microsecond=0)
    if target <= now_local:
        # Already past 07:00 today → schedule tomorrow.
        from datetime import timedelta as _td

        target = target + _td(days=1)
    wait_secs = max(1.0, (target - now_local).total_seconds())
    await asyncio.sleep(wait_secs)


async def background_loop(
    *,
    user_id: str = "ariel",
    user_tz: str = DEFAULT_USER_TZ,
) -> None:
    """Forever-loop that fires ``generate_daily_brief`` at 07:00 local.

    Wired from ``argosy.api.main.create_app`` behind the
    ``ARGOSY_DAILY_BRIEF_ENABLED`` gate. The loop is intentionally
    simple — one user per process. Multi-tenant fan-out is a future
    concern that lives upstream of this function.
    """
    while True:
        try:
            await _sleep_until_07_local(user_tz)
            async with db_mod.get_session() as session:
                await generate_daily_brief(user_id, session, user_tz=user_tz)
            # Fleet self-review — daily sweep fires alongside the brief
            # so accumulated architectural / behavioural anomalies
            # surface every morning.  Gated by the SAME enable flag
            # (we're already inside background_loop, which only runs
            # under ARGOSY_DAILY_BRIEF_ENABLED=1).  Failures are
            # logged + swallowed; the brief loop NEVER breaks because
            # of an observability surface.
            try:
                await _run_daily_self_review(user_id)
            except Exception:  # pragma: no cover - defensive
                _log.exception("fleet_self_review.daily_sweep_failed")
            # EX2 — daily anomaly-detection backstop. Same gate
            # semantics: the call only fires when
            # ARGOSY_ANOMALY_DETECTION_ENABLED=1 AND the process isn't
            # pytest. Spawns its own daemon thread + sync session.
            # Failures are swallowed inside schedule_anomaly_check —
            # the brief loop NEVER breaks because of an anomaly hook.
            try:
                from argosy.services.anomaly_runner import (
                    schedule_anomaly_check,
                )
                schedule_anomaly_check(
                    user_id=user_id,
                    triggered_by="daily",
                )
            except Exception:  # pragma: no cover - defensive
                _log.exception("anomaly_runner.daily_backstop_failed")
        except Exception:  # pragma: no cover - defensive: NEVER break the loop
            _log.exception("daily_brief_runner.background_tick_failed")


async def _run_daily_self_review(user_id: str) -> None:
    """Fire the daily fleet self-review sweep on a worker thread.

    The detectors are sync (sqlalchemy.orm.Session) so we run them
    via ``asyncio.to_thread`` to keep the asyncio loop responsive.
    """
    import asyncio
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.services.fleet_self_review_runner import (
        generate_fleet_self_review,
    )

    settings = get_settings()
    sync_url = settings.database_url.replace("+aiosqlite", "")
    engine = create_engine(sync_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def _run() -> None:
        db = SessionLocal()
        try:
            generate_fleet_self_review(
                db,
                user_id=user_id,
                scope_kind="daily",
                decision_run_id=None,
            )
        finally:
            db.close()
            engine.dispose()

    await asyncio.to_thread(_run)


__all__ = [
    "COST_CAP_USD",
    "DEFAULT_USER_TZ",
    "background_loop",
    "generate_daily_brief",
    "is_enabled_for_runtime",
]
