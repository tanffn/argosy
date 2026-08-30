"""``PeriodDirectiveDailyJob`` — the daily proactive "your move" push.

Doctrine (SDD §1.6): Argosy is a proactive expert agency — the client having to
ASK "should I deploy this cash?" is a failure of the primary path. This job makes
the deploy advice PUSH: every evening (19:00 IDT, after the review chain) it
surfaces ONE inbox action proposal when — and only when — there is something
worth doing.

Be-smart contract (three stages, strictly ordered by cost):

1. **Triage** — deterministic + cheap, no LLM. Excess plan-target cash OR a
   fresh autonomous buy/sell recommendation means there is something to
   decide. An already-open directive with unchanged funding and source ids is
   a quiet success; no new judgment is invented.
2. **Compose** — call the SAME canonical live ``/deploy-cash`` implementation
   used by the CLI, including author revisions, blind review, current facts,
   after-tax sells and first-class order-sheet validation. Determinism never
   picks instruments here: an unavailable/rejected sheet writes nothing.
3. **Sink** — ONE ``allocate`` action proposal containing the complete validated
   order-sheet schema (dedup per user, refreshed in place). Source proposals
   are cancelled with history once reconciled, leaving one approvable voice.
   When neither cash nor recommendations remain, the standing directive leaves
   the client's checklist.
4. **Materialize after approval** — this job never creates executable rows.
   Accepting the unified action proposal atomically materializes its validated
   lines into awaiting-human trade proposals, with exact custody resolution.

Same-code-path contract: the 19:00 IDT cadence and the manual ``Run now`` both
go through :meth:`tick`. NOTE: it is a ``CadenceLoop``, so
``JobMetadata.long_running`` must be False — that flag is the registry's
LongRunningJob-vs-CadenceLoop discriminator (a mismatch makes
``JobRegistry.register`` raise, leaving the loop on the scheduler but
unrunnable), NOT a "takes a long time" hint.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
from argosy.orchestrator.cost_guard import get_cost_guard
from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.jobs.registry import JobMetadata

_log = get_logger("argosy.jobs.period_directive_daily")

_DEFAULT_CRON = "0 19 * * *"
_DEFAULT_TZ = "Asia/Jerusalem"

# The proposal kind is an EXISTING allowed value of ck_action_proposals_kind
# (migration 0077) — "allocate" is exactly what the directive proposes. A new
# kind would need another CHECK relaxation and buys nothing.
_KIND = "allocate"

# One directive slot per user: the partial-unique open-dedup index makes the
# refresh-in-place idempotent (same pattern as deploy_team_flag's per-symbol key).
_DEDUP_PREFIX = "period_directive"

# An open directive whose cash figure is within this band of today's number is
# still accurate — re-authoring it would burn an LLM run to say the same thing.
STALENESS_TOLERANCE = 0.10

_SESSION_FACTORY: tuple[str, sessionmaker] | None = None


def _build_default_session_factory() -> sessionmaker:
    """Cached sync ``sessionmaker`` bound to the configured DB (rebuilds if the
    db_file changes). Mirrors ``holdings_review`` — the triage/sink services
    need a sync Session; the async get_session yields an AsyncSession they
    cannot consume."""
    global _SESSION_FACTORY
    from argosy.config import get_settings
    from argosy.state.db import create_sync_engine

    db_file = str(get_settings().db_file)
    if _SESSION_FACTORY is not None and _SESSION_FACTORY[0] == db_file:
        return _SESSION_FACTORY[1]
    engine = create_sync_engine(f"sqlite:///{db_file}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _SESSION_FACTORY = (db_file, factory)
    return factory


def period_directive_daily_metadata() -> JobMetadata:
    return JobMetadata(
        name="period_directive_daily",
        schedule_cron=_DEFAULT_CRON,
        schedule_human="Daily 19:00 IDT",
        source_kind="monitor",
        description=(
            "Proactive 'your move' push: cheap deterministic triage (excess cash "
            "or fresh autonomous recommendations, plus directive staleness) decides IF there is "
            "anything to decide; only then the canonical live deploy path authors, "
            "reviews and validates a first-class order sheet. The complete schema is "
            "stored in ONE 'allocate' inbox proposal; accepting it materializes "
            "awaiting-human executable proposals with exact custody "
            "(refreshed in place, auto-superseded below threshold). A quiet "
            "skipped day is a success state. Manual Run now uses the same tick."
        ),
        long_running=False,
    )


def _dedup_key(user_id: str) -> str:
    return f"{_DEDUP_PREFIX}:{user_id}"


def _fmt_usd(amount: float) -> str:
    """Clean, size-proportional round display (never cent precision)."""
    if abs(amount) >= 10_000:
        return f"${amount / 1000:,.0f}k"
    return f"${amount:,.0f}"


def _detect_cash(db: Session, *, user_id: str):
    from argosy.services.unallocated_cash_detector import (
        detect_unallocated_cash_overage,
    )

    return detect_unallocated_cash_overage(db, user_id=user_id)


def _load_recommendations(db: Session, *, user_id: str) -> list[dict[str, Any]]:
    from argosy.services.current_recommendations import (
        load_actionable_recommendations,
    )

    return load_actionable_recommendations(db, user_id=user_id)


def compose_authored_directive(db: Session, *, user_id: str, excess_usd: float):
    """Run the canonical live deploy path and require its order-sheet artifact.

    Calling the route implementation directly is intentional: manual API, CLI
    acceptance and cadence execution cannot silently drift into three allocation
    pipelines. Every argument is explicit so FastAPI ``Query`` defaults never
    leak into this internal call.
    """
    from argosy.api.routes.portfolio import get_deploy_cash

    return get_deploy_cash(
        cash_usd=float(excess_usd),
        user_id=user_id,
        live=True,
        sleeve_pct=5.0,
        use_high_potential=True,
        fleet_review=False,
        include_order_sheet=True,
        allow_sells=True,
        horizon_years_min=1,
        horizon_years_max=5,
        db=db,
    )


def _find_open_directives(db: Session, user_id: str) -> list[Any]:
    from argosy.state.models import ActionProposal

    return (
        db.query(ActionProposal)
        .filter_by(user_id=user_id, kind=_KIND, status="open")
        .filter(ActionProposal.dedup_key.like(f"{_DEDUP_PREFIX}:%"))
        .all()
    )


def _stored_excess_usd(row: Any) -> float | None:
    try:
        payload = json.loads(row.suggested_payload or "{}")
        val = payload.get("excess_usd")
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _stored_source_proposal_ids(row: Any) -> list[int]:
    try:
        payload = json.loads(row.suggested_payload or "{}")
        return sorted({int(value) for value in payload.get("source_proposal_ids", [])})
    except (ValueError, TypeError):
        return []


def _stored_recommendation_keys(row: Any) -> list[str]:
    try:
        payload = json.loads(row.suggested_payload or "{}")
        keys = payload.get("source_recommendation_keys")
        if keys is None:
            # Backward compatibility for directives written before holding-review
            # inputs joined the unified path.
            keys = [
                f"proposal:{int(value)}"
                for value in payload.get("source_proposal_ids", [])
            ]
        return sorted({str(value) for value in keys if str(value)})
    except (ValueError, TypeError):
        return []


def _has_fresh_validated_sheet(row: Any) -> bool:
    """True only for the new schema-bearing directive while its quotes are fresh."""
    try:
        payload = json.loads(row.suggested_payload or "{}")
        if (
            payload.get("artifact_type") != "validated_order_sheet"
            or payload.get("validation_status") != "validated"
        ):
            return False
        from argosy.services.order_sheet import OrderSheet, validate_order_sheet

        sheet = OrderSheet.model_validate(payload["order_sheet"])
        if not validate_order_sheet(sheet).valid:
            return False
        expires_at = sheet.generated_at + timedelta(days=sheet.freshness_days)
        return expires_at >= datetime.now(UTC)
    except (KeyError, TypeError, ValueError):
        return False


def _supersede_open_directives(
    db: Session, user_id: str, *, keep_id: int | None, reason: str,
) -> list[int]:
    """Close open directive rows this loop owns (all of them, or all but the one
    just written). A directive the state no longer supports must DISAPPEAR from
    the client's checklist — same doctrine as ``supersede_cleared_flags``."""
    superseded: list[int] = []
    try:
        for row in _find_open_directives(db, user_id):
            if keep_id is not None and row.id == keep_id:
                continue
            row.status = "superseded"
            superseded.append(row.id)
            _log.info(
                "period_directive_daily.superseded",
                proposal_id=row.id, reason=reason,
            )
        if superseded:
            db.commit()
    except Exception as exc:  # noqa: BLE001 — cleanup is additive/best-effort
        db.rollback()
        _log.warning("period_directive_daily.supersede_failed", error=str(exc)[:160])
    return superseded


def _write_directive_proposal(
    db: Session,
    user_id: str,
    *,
    excess_usd: float,
    source_proposal_ids: list[int],
    source_recommendation_keys: list[str] | None = None,
    sheet: Any,
) -> int | None:
    """Write the ONE schema-bearing directive; on a dedup collision the OPEN row is
    REFRESHED IN PLACE (the deploy_team_flag sink pattern) so the inbox always
    shows TODAY's move, never a stale amount. Returns the row id."""
    from sqlalchemy.exc import IntegrityError

    from argosy.services.order_sheet import OrderAction
    from argosy.services.order_sheet_materializer import order_sheet_fingerprint
    from argosy.state.models import ActionProposal

    now = datetime.now(UTC)
    lines = list(getattr(sheet, "lines", []) or [])
    buys = [
        line for line in lines
        if line.action in (OrderAction.BUY, OrderAction.ADD)
    ]
    top = ", ".join(
        f"{line.symbol} {_fmt_usd(line.notional_usd)}" for line in buys[:3]
    )
    if excess_usd > 0 and (source_recommendation_keys or source_proposal_ids):
        trigger_label = (
            f"~{_fmt_usd(excess_usd)} idle cash plus current recommendations"
        )
    elif excess_usd > 0:
        trigger_label = f"~{_fmt_usd(excess_usd)} idle cash"
    else:
        trigger_label = "current portfolio/market recommendations"
    summary = (
        f"Validated unified order sheet for {trigger_label}: {top}"
        f"{', …' if len(buys) > 3 else ''}"
    )
    order_lines = "\n".join(
        f"- **{line.action.value} {line.symbol}**: "
        f"{line.shares:g} shares / {_fmt_usd(line.notional_usd)} on {line.venue}"
        for line in lines
    )
    rationale_md = (
        "The deployment team reconciled the current portfolio, market, discovery "
        "and funding judgments into this period's single validated artifact:\n\n"
        + order_lines
        + ("\n\n" + (getattr(sheet, "rationale", "") or "")).rstrip()
        + "\n\nNothing was executed. Each line carries its thesis, falsifier, "
        "dated catalyst, live evidence and exact quantity in the attached sheet."
    )
    fingerprint = order_sheet_fingerprint(sheet)
    suggested_payload = json.dumps({
        "artifact_type": "validated_order_sheet",
        "excess_usd": round(float(excess_usd), 2),
        "source_proposal_ids": sorted({int(value) for value in source_proposal_ids}),
        "source_recommendation_keys": sorted(
            {str(value) for value in (source_recommendation_keys or [])}
        ),
        "order_sheet_fingerprint": fingerprint,
        "validation_status": "validated",
        "buys": [
            {
                "symbol": line.symbol,
                "amount_usd": round(float(line.notional_usd), 2),
                "shares": float(line.shares),
                "venue": line.venue,
            }
            for line in buys
        ],
        "order_sheet": sheet.model_dump(mode="json"),
    }, sort_keys=True)
    dedup_key = _dedup_key(user_id)
    row = ActionProposal(
        user_id=user_id,
        summary=summary,
        rationale_md=rationale_md,
        suggested_payload=suggested_payload,
        severity="info",
        surfaced_at=now,
        expires_at=now + timedelta(days=7),  # the daily loop refreshes well before
        status="open",
        kind=_KIND,
        dedup_key=dedup_key,
        execution_state="proposed",
    )
    db.add(row)
    try:
        db.commit()
        _record_surfaced_recommendations(
            db, proposal_id=row.id, sheet=sheet, fingerprint=fingerprint
        )
        _log.info("period_directive_daily.proposal_written", proposal_id=row.id)
        return row.id
    except IntegrityError as exc:
        # Dedup collision → an OPEN directive already holds the slot; refresh it
        # in place (keep the row id + status='open'). Log the real error too —
        # a CHECK failure once masqueraded as a dedup collision (migration 0077).
        db.rollback()
        from argosy.state.models import ActionProposal as _AP

        existing = (
            db.query(_AP).filter_by(dedup_key=dedup_key, status="open").first()
        )
        if existing is None:
            _log.warning(
                "period_directive_daily.proposal_write_failed",
                error=str(getattr(exc, "orig", exc))[:160],
            )
            return None
        existing.summary = summary
        existing.rationale_md = rationale_md
        existing.suggested_payload = suggested_payload
        existing.surfaced_at = now
        existing.expires_at = now + timedelta(days=7)
        db.commit()
        _record_surfaced_recommendations(
            db, proposal_id=existing.id, sheet=sheet, fingerprint=fingerprint
        )
        _log.info(
            "period_directive_daily.proposal_refreshed", proposal_id=existing.id,
        )
        return existing.id


def _record_surfaced_recommendations(
    db: Session,
    *,
    proposal_id: int,
    sheet: Any,
    fingerprint: str,
) -> None:
    """Start outcome clocks when advice is shown, not only when it is bought."""

    try:
        from argosy.services.predictions.writers import (
            write_order_sheet_predictions,
        )

        for line in list(getattr(sheet, "lines", []) or []):
            write_order_sheet_predictions(
                db,
                sheet.user_id,
                fingerprint=fingerprint,
                proposal_id=proposal_id,
                ticker=line.symbol,
                action=line.action.value,
                event_at=sheet.generated_at,
                due_at=datetime.combine(
                    line.expectation.due_date,
                    time(23, 59, 59),
                    tzinfo=UTC,
                ),
                entry_price=line.evidence.price_usd,
                expectation=line.expectation.statement,
                success_measure=line.expectation.success_measure,
                stance_source=line.stance_source,
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - calibration cannot hide the advice
        db.rollback()
        _log.warning(
            "period_directive_daily.prediction_write_failed",
            proposal_id=proposal_id,
            error=str(exc)[:160],
        )


def _link_team_telemetry(
    db: Session,
    user_id: str,
    *,
    directive_proposal_id: int,
    started_at: datetime,
) -> list[int]:
    """Attach exact standalone author/reviewer rows to the durable artifact."""
    from sqlalchemy import select

    from argosy.state.models import ActionProposal, AgentReport

    reports = list(
        db.execute(
            select(AgentReport)
            .where(
                AgentReport.user_id == user_id,
                AgentReport.agent_role.in_(
                    ("deployment_author", "deployment_reviewer")
                ),
                AgentReport.created_at >= started_at,
            )
            .order_by(AgentReport.id)
        ).scalars().all()
    )
    if not reports:
        return []
    row = db.get(ActionProposal, directive_proposal_id)
    if row is None:
        return []
    payload = json.loads(row.suggested_payload or "{}")
    payload["team_agent_report_ids"] = [int(report.id) for report in reports]
    payload["team_decision_ids"] = sorted(
        {str(report.decision_id) for report in reports if report.decision_id}
    )
    payload["team_cost_usd"] = round(
        sum(float(report.cost_usd) for report in reports), 6
    )
    row.suggested_payload = json.dumps(payload, sort_keys=True)
    db.commit()
    return payload["team_agent_report_ids"]


def _materialize_validated_sheet(
    db: Session,
    sheet: Any,
    *,
    funding_account_id: str,
    materialize_fn: Callable[..., list[Any]] | None = None,
) -> list[int]:
    """Create awaiting-human execution rows using exact, evidenced accounts.

    Buy custody is explicit configuration. Sell custody is derived only when
    the live lots table names exactly one account for the symbol; ambiguity is
    surfaced instead of guessed.
    """
    from sqlalchemy import select

    from argosy.services.order_sheet import OrderAction
    from argosy.services.order_sheet_materializer import materialize_order_sheet
    from argosy.state.models import Lot

    sell_accounts: dict[str, str] = {}
    for line in sheet.lines:
        if line.action not in (OrderAction.SELL, OrderAction.TRIM):
            continue
        accounts = set(
            db.execute(
                select(Lot.account_id).where(
                    Lot.user_id == sheet.user_id,
                    Lot.ticker == line.symbol,
                    Lot.quantity > 0,
                )
            ).scalars().all()
        )
        accounts = {account.strip() for account in accounts if account.strip()}
        if len(accounts) != 1:
            raise ValueError(
                f"{line.symbol}: sell custody must resolve to exactly one account; "
                f"found {sorted(accounts)}"
            )
        sell_accounts[line.symbol] = accounts.pop()

    writer = materialize_fn or materialize_order_sheet
    rows = writer(
        db,
        sheet,
        funding_account_id=funding_account_id,
        sell_accounts_by_symbol=sell_accounts,
    )
    db.commit()
    return [int(row.id) for row in rows]


def _consume_component_recommendations(
    db: Session,
    user_id: str,
    *,
    source_proposal_ids: list[int],
    directive_proposal_id: int | None,
) -> list[int]:
    """Make the unified directive the sole approvable voice.

    The source proposals remain in the immutable audit trail, but can no
    longer be approved independently after the deployment author has
    reconciled them into one order sheet. The directive payload stores their
    ids, allowing the daily refresh to keep carrying the same inputs while
    that directive remains open.
    """
    if directive_proposal_id is None or not source_proposal_ids:
        return []

    from sqlalchemy import select

    from argosy.services.current_recommendations import AUTONOMOUS_PROPOSAL_SOURCES
    from argosy.state.models import Proposal, ProposalHistory

    rows = db.execute(
        select(Proposal).where(
            Proposal.user_id == user_id,
            Proposal.id.in_(source_proposal_ids),
            Proposal.status == "awaiting_human",
            Proposal.source.in_(AUTONOMOUS_PROPOSAL_SOURCES),
        )
    ).scalars().all()
    consumed: list[int] = []
    now = datetime.now(UTC)
    for row in rows:
        row.status = "cancelled"
        row.updated_at = now
        consumed.append(int(row.id))
        db.add(ProposalHistory(
            proposal_id=row.id,
            status="cancelled",
            transitioned_at=now,
            transitioned_by="period_directive_daily",
            note=(
                "Reconciled into unified action proposal "
                f"#{directive_proposal_id}; component is no longer independently approvable."
            ),
        ))
    if consumed:
        db.commit()
    return sorted(consumed)


def run_period_directive_daily(
    db: Session,
    user_id: str,
    *,
    detect_fn: Callable[..., Any] | None = None,
    recommendations_fn: Callable[..., list[dict[str, Any]]] | None = None,
    compose_fn: Callable[..., Any] | None = None,
    funding_account_id: str | None = None,
    materialize_fn: Callable[..., list[Any]] | None = None,
) -> dict[str, Any]:
    """One triage→compose→sink pass. Returns the rich ``output_summary`` so
    ``job_runs`` tells the whole story (including quiet skips)."""
    run_started_at = datetime.now(UTC)
    detect_fn = detect_fn or _detect_cash
    recommendations_fn = recommendations_fn or _load_recommendations
    compose_fn = compose_fn or compose_authored_directive

    # --- Stage 1: TRIAGE (deterministic, no LLM) ---------------------------
    event = detect_fn(db, user_id=user_id)
    recommendations = recommendations_fn(db, user_id=user_id)
    from argosy.services.current_recommendations import (
        recommendation_ids,
        recommendation_keys,
    )

    source_proposal_ids = recommendation_ids(recommendations)
    source_recommendation_keys = recommendation_keys(recommendations)
    if event is None and not source_recommendation_keys:
        # Below threshold (or no/stale snapshot) → quiet success. A standing
        # open directive is now moot — it leaves the checklist.
        superseded = _supersede_open_directives(
            db, user_id, keep_id=None,
            reason="idle cash back below plan-target threshold",
        )
        out = {
            "triggered": False,
            "reason": "idle cash below plan-target threshold — nothing to decide",
            "cash_usd": None,
            "proposal_id": None,
            "superseded": superseded,
        }
        _log.info("period_directive_daily.quiet_skip", **out)
        return out

    excess_usd = float(event.excess_usd) if event is not None else 0.0
    open_rows = _find_open_directives(db, user_id)
    for row in open_rows:
        prior = _stored_excess_usd(row)
        prior_source_keys = _stored_recommendation_keys(row)
        cash_still_accurate = bool(
            prior is not None
            and (
                (prior == 0.0 and excess_usd == 0.0)
                or (
                    prior > 0
                    and abs(excess_usd - prior) / prior <= STALENESS_TOLERANCE
                )
            )
        )
        if (
            cash_still_accurate
            and prior_source_keys == source_recommendation_keys
            and _has_fresh_validated_sheet(row)
        ):
            out = {
                "triggered": False,
                "reason": (
                    f"open directive #{row.id} still accurate "
                    "(funding and source recommendations unchanged) — "
                    "nothing new to say"
                ),
                "cash_usd": excess_usd,
                "proposal_id": row.id,
                "source_proposal_ids": source_proposal_ids,
                "source_recommendation_keys": source_recommendation_keys,
                "superseded": [],
            }
            _log.info("period_directive_daily.quiet_skip", **out)
            return out

    # --- Stage 2: COMPOSE (the fleet authors; determinism never allocates) --
    try:
        outcome = compose_fn(db, user_id=user_id, excess_usd=excess_usd)
    except Exception as exc:  # noqa: BLE001 — fail quiet-but-logged, retry next day
        _log.warning("period_directive_daily.compose_failed", error=str(exc)[:200])
        outcome = None
    authored = getattr(outcome, "authored", None)
    authored_status = getattr(authored, "status", None)
    artifact = getattr(outcome, "order_sheet", None)
    artifact_status = getattr(artifact, "status", None)
    sheet = getattr(artifact, "sheet", None)
    if (
        outcome is None
        or authored_status != "accepted"
        or artifact_status != "validated"
        or sheet is None
    ):
        # Degraded: no validated artifact → NO directive (never a prose or
        # deterministic fallback on the money path). A standing directive stays
        # only if it still passes today's schema/freshness rules; an invalid old
        # artifact must not linger as the UI's blocked "current" trade plan.
        prior_valid = next(
            (
                row
                for row in _find_open_directives(db, user_id)
                if _has_fresh_validated_sheet(row)
            ),
            None,
        )
        superseded = _supersede_open_directives(
            db,
            user_id,
            keep_id=(prior_valid.id if prior_valid is not None else None),
            reason="current artifact failed today's validation and replacement failed",
        )
        failures = list(getattr(artifact, "failures", []) or [])
        out = {
            "triggered": True,
            "reason": (
                "validated order sheet unavailable "
                f"(author={authored_status or 'error'}, "
                f"artifact={artifact_status or 'missing'}) — "
                "no directive written; retrying next run"
            ),
            "cash_usd": excess_usd,
            "proposal_id": None,
            "superseded": superseded,
            "artifact_failures": failures,
            "status": "degraded",
            "degraded": True,
        }
        _log.warning("period_directive_daily.degraded", **out)
        return out

    # --- Stage 3: SINK (one row, refresh-in-place, supersede the rest) ------
    proposal_id = _write_directive_proposal(
        db,
        user_id,
        excess_usd=excess_usd,
        source_proposal_ids=source_proposal_ids,
        source_recommendation_keys=source_recommendation_keys,
        sheet=sheet,
    )
    if proposal_id is not None:
        _link_team_telemetry(
            db,
            user_id,
            directive_proposal_id=proposal_id,
            started_at=run_started_at,
        )
    consumed_source_ids = _consume_component_recommendations(
        db,
        user_id,
        source_proposal_ids=source_proposal_ids,
        directive_proposal_id=proposal_id,
    )
    superseded = _supersede_open_directives(
        db, user_id, keep_id=proposal_id,
        reason="replaced by today's authored directive",
    )
    from argosy.services.order_sheet_materializer import order_sheet_fingerprint

    # The scheduler authors/surfaces only. Executable proposal rows are staged
    # atomically when the user accepts this unified action proposal.
    materialized_ids: list[int] = []
    materialization_status = "awaiting_unified_approval"

    out = {
        "triggered": True,
        "reason": "validated order sheet surfaced to the inbox",
        "cash_usd": excess_usd,
        "proposal_id": proposal_id,
        "source_proposal_ids": source_proposal_ids,
        "source_recommendation_keys": source_recommendation_keys,
        "consumed_source_proposal_ids": consumed_source_ids,
        "superseded": superseded,
        "order_sheet_fingerprint": order_sheet_fingerprint(sheet),
        "order_lines": len(sheet.lines),
        "materialization_status": materialization_status,
        "materialized_proposal_ids": materialized_ids,
    }
    _log.info("period_directive_daily.surfaced", **out)
    return out


class PeriodDirectiveDailyJob(CadenceLoop):
    """Daily proactive triage→compose→sink loop."""

    name = "period_directive_daily"

    def __init__(
        self,
        *,
        schedule: LoopSchedule | None = None,
        enabled: bool = True,
        user_id: str = "ariel",
        session_factory: sessionmaker | Callable[[], Session] | None = None,
        run_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            schedule=schedule or LoopSchedule(cron=_DEFAULT_CRON, timezone=_DEFAULT_TZ),
            enabled=enabled,
        )
        self.user_id = user_id
        self._session_factory = session_factory
        self._cost_guard_enabled = run_fn is None
        self._run_fn = run_fn or run_period_directive_daily
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        # The scheduler calls tick(now=clock) — the keyword MUST be accepted
        # (the pending_reevaluation_daily regression).
        self.last_output_summary = None
        if self._cost_guard_enabled and await get_cost_guard(
            user_id=self.user_id
        ).should_pause_non_routine(loop_name=self.name):
            out = {"status": "paused", "reason": "cost_cap"}
            self.last_output_summary = out
            _log.info(
                "period_directive_daily.cost_guard_paused",
                user_id=self.user_id,
            )
            return out
        run_at = (now or (lambda: datetime.now(UTC)))()
        _log.info("period_directive_daily.tick.start", run_at=run_at.isoformat())

        def _work() -> dict[str, Any]:
            factory = self._session_factory or _build_default_session_factory()
            session = factory()
            try:
                return self._run_fn(session, self.user_id)
            finally:
                session.close()

        out = await asyncio.to_thread(_work)
        self.last_output_summary = out
        _log.info("period_directive_daily.tick.done", **{
            k: out.get(k) for k in ("triggered", "reason", "proposal_id")
        })
        return out


__all__ = [
    "PeriodDirectiveDailyJob",
    "compose_authored_directive",
    "period_directive_daily_metadata",
    "run_period_directive_daily",
]
