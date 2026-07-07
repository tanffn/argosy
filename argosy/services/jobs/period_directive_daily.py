"""``PeriodDirectiveDailyJob`` — the daily proactive "your move" push.

Doctrine (SDD §1.6): Argosy is a proactive expert agency — the client having to
ASK "should I deploy this cash?" is a failure of the primary path. This job makes
the deploy advice PUSH: every evening (19:00 IDT, after the review chain) it
surfaces ONE inbox action proposal when — and only when — there is something
worth doing.

Be-smart contract (three stages, strictly ordered by cost):

1. **Triage** — deterministic + cheap, no LLM. The existing plan-target-gap
   detector (``detect_unallocated_cash_overage``) decides "is there anything to
   decide": idle cash below the plan-target threshold → quiet skip (a skipped
   day is a SUCCESS state). An already-open directive whose cash figure is
   within ±10% of today's → nothing new to say → quiet skip.
2. **Compose** — the fleet AUTHORS the money decision, only when triage fired:
   the SAME ``authored_allocation`` path ``/deploy-cash`` uses, fed the SAME
   holistic packet (``assemble_author_packet`` — real NVDA look-through, sleeve
   gaps, market regime). Determinism never picks instruments here: if the
   author is unavailable / rejected, the run records a degraded summary and
   writes NOTHING — retry next day, never a deterministic fallback allocation.
3. **Sink** — ONE ``allocate`` action proposal (dedup per user, refreshed in
   place on collision — the ``deploy_team_flag`` sink pattern), plus
   auto-supersede: when cash drops back under threshold, the standing open
   directive leaves the client's checklist (``supersede_cleared_flags``
   pattern — resolved items never punt back to the client).

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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger
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
    import sqlalchemy as sa

    from argosy.config import get_settings

    db_file = str(get_settings().db_file)
    if _SESSION_FACTORY is not None and _SESSION_FACTORY[0] == db_file:
        return _SESSION_FACTORY[1]
    engine = sa.create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
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
            "Proactive 'your move' push: cheap deterministic triage (idle cash vs "
            "plan-target threshold, open-directive staleness) decides IF there is "
            "anything to decide; only then the deployment-author fleet composes the "
            "allocation and ONE 'allocate' inbox proposal is written (refreshed in "
            "place, auto-superseded when cash falls back under threshold). A quiet "
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


def compose_authored_directive(db: Session, *, user_id: str, excess_usd: float):
    """The compose seam: the SAME author path ``/deploy-cash`` wires — the fleet
    authors, determinism only verifies. Returns an ``AuthorOutcome`` or ``None``
    when the author path cannot run at all (no plan / kill-switch off)."""
    from argosy.api.routes.portfolio import _load_current_doc_and_holdings
    from argosy.config import get_settings
    from argosy.services.allocation_author.packet_assembly import assemble_author_packet
    from argosy.services.allocation_author.reliable import authored_allocation

    if not get_settings().deployment_author_enabled:
        _log.warning("period_directive_daily.author_disabled")
        return None
    doc, holdings, snap_cash = _load_current_doc_and_holdings(user_id)
    if doc is None:
        _log.warning("period_directive_daily.no_current_plan", user_id=user_id)
        return None
    packet = assemble_author_packet(
        db, user_id=user_id, doc=doc, holdings_usd=holdings,
        cash_usd=snap_cash, deployable_usd=excess_usd,
    )
    return authored_allocation(packet, user_id=user_id)


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
    db: Session, user_id: str, *, excess_usd: float, proposal: Any,
) -> int | None:
    """Write the ONE directive row; on a dedup collision the OPEN row is
    REFRESHED IN PLACE (the deploy_team_flag sink pattern) so the inbox always
    shows TODAY's move, never a stale amount. Returns the row id."""
    from sqlalchemy.exc import IntegrityError

    from argosy.state.models import ActionProposal

    now = datetime.now(timezone.utc)
    buys = list(getattr(proposal, "buys", []) or [])
    top = ", ".join(
        f"{b.symbol} {_fmt_usd(b.amount_usd)}" for b in buys[:3]
    )
    summary = (
        f"Deploy ~{_fmt_usd(excess_usd)} idle cash: {top}"
        f"{', …' if len(buys) > 3 else ''} — full plan in the deploy tool"
    )
    buy_lines = "\n".join(
        f"- **{b.symbol}** {_fmt_usd(b.amount_usd)}"
        + (f" — {b.sleeve}" if getattr(b, "sleeve", "") else "")
        for b in buys
    )
    rationale_md = (
        "Your idle cash sits above the plan-target threshold; the deployment "
        "author composed this period's move:\n\n" + buy_lines
        + ("\n\n" + (getattr(proposal, "rationale", "") or "")).rstrip()
        + "\n\nNothing was executed — review the full plan (sizing, funding, "
        "estate notes) in the deploy tool."
    )
    suggested_payload = json.dumps({
        "excess_usd": round(float(excess_usd), 2),
        "buys": [
            {"symbol": b.symbol, "amount_usd": round(float(b.amount_usd), 2),
             "sleeve": getattr(b, "sleeve", "")}
            for b in buys
        ],
    })
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
        _log.info(
            "period_directive_daily.proposal_refreshed", proposal_id=existing.id,
        )
        return existing.id


def run_period_directive_daily(
    db: Session,
    user_id: str,
    *,
    detect_fn: Callable[..., Any] | None = None,
    compose_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One triage→compose→sink pass. Returns the rich ``output_summary`` so
    ``job_runs`` tells the whole story (including quiet skips)."""
    detect_fn = detect_fn or _detect_cash
    compose_fn = compose_fn or compose_authored_directive

    # --- Stage 1: TRIAGE (deterministic, no LLM) ---------------------------
    event = detect_fn(db, user_id=user_id)
    if event is None:
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

    excess_usd = float(event.excess_usd)
    open_rows = _find_open_directives(db, user_id)
    for row in open_rows:
        prior = _stored_excess_usd(row)
        if prior and prior > 0 and abs(excess_usd - prior) / prior <= STALENESS_TOLERANCE:
            out = {
                "triggered": False,
                "reason": (
                    f"open directive #{row.id} still accurate "
                    f"(cash within ±{STALENESS_TOLERANCE:.0%}) — nothing new to say"
                ),
                "cash_usd": excess_usd,
                "proposal_id": row.id,
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
    status = getattr(outcome, "status", None)
    proposal = getattr(outcome, "proposal", None)
    if outcome is None or status != "accepted" or proposal is None \
            or not getattr(proposal, "buys", None):
        # Degraded: no authored allocation → NO proposal (never a deterministic
        # fallback allocation on the money path). Any standing directive stays —
        # yesterday's authored move beats nothing.
        out = {
            "triggered": True,
            "reason": (
                f"author unavailable (status={status or 'error'}) — "
                "no proposal written; retrying next run"
            ),
            "cash_usd": excess_usd,
            "proposal_id": None,
            "superseded": [],
            "degraded": True,
        }
        _log.warning("period_directive_daily.degraded", **out)
        return out

    # --- Stage 3: SINK (one row, refresh-in-place, supersede the rest) ------
    proposal_id = _write_directive_proposal(
        db, user_id, excess_usd=excess_usd, proposal=proposal,
    )
    superseded = _supersede_open_directives(
        db, user_id, keep_id=proposal_id,
        reason="replaced by today's authored directive",
    )
    out = {
        "triggered": True,
        "reason": "directive surfaced to the inbox",
        "cash_usd": excess_usd,
        "proposal_id": proposal_id,
        "superseded": superseded,
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
        self._run_fn = run_fn or run_period_directive_daily
        self.last_output_summary: dict[str, Any] | None = None

    async def tick(self, *, now: Callable[[], datetime] | None = None) -> dict | None:
        # The scheduler calls tick(now=clock) — the keyword MUST be accepted
        # (the pending_reevaluation_daily regression).
        self.last_output_summary = None
        run_at = (now or (lambda: datetime.now(timezone.utc)))()
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
