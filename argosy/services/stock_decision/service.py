"""Orchestration for per-stock research -> verdict.

``research_bundle`` assembles the fetched data for one ticker from a set of
best-effort fetchers (news / fundamentals / sentiment / technical / price / plan
thesis). Each fetcher is injected and isolated: a failure or empty result simply
leaves that field ABSENT, and the decision agent lowers its confidence + records
the gap rather than guessing.

``decide_holdings`` runs the review over a book: a cheap ``triage`` gate picks
which names warrant deep research (tiering — a full fetch+decide on every ticker
is what timed the fleet out), then each survivor gets a bundle + a verdict. HOLD
verdicts are returned but are NOT actionable — the caller surfaces only
``actionable_verdicts`` so an intact thesis stays silent.

All collaborators are injectable so the orchestration is unit-tested without a
live LLM or network; the live wiring (real analyst fetchers, inbox sink) is a thin
adapter over this core.
"""
from __future__ import annotations

from typing import Any, Callable

from argosy.agents.stock_decision import (
    StockDecisionOutput,
    decide_stock,
    is_actionable,
)
from argosy.logging import get_logger

log = get_logger(__name__)

_SEVERITY_BY_VERDICT = {"SELL": "critical", "TRIM": "warning", "BUY": "info"}

# A fetcher maps a ticker -> a short text summary (or None when unavailable).
Fetcher = Callable[[str], "str | None"]


def research_bundle(ticker: str, *, fetchers: dict[str, Fetcher]) -> dict[str, Any]:
    """Assemble the fetched research bundle for ``ticker``. Best-effort per field:
    a fetcher that returns falsy or raises leaves its field out of the bundle."""
    bundle: dict[str, Any] = {}
    for field, fn in (fetchers or {}).items():
        try:
            value = fn(ticker)
        except Exception:  # noqa: BLE001 — one bad source must not blank the bundle
            value = None
        if value:
            bundle[field] = value
    return bundle


def decide_holdings(
    holdings: dict[str, float],
    *,
    fetchers: dict[str, Fetcher],
    context_of: Callable[[str, float], str],
    triage: Callable[[str, float], bool] | None = None,
    decide: Callable[..., StockDecisionOutput] = decide_stock,
    user_id: str = "ariel",
) -> list[StockDecisionOutput]:
    """Review a book (``ticker -> usd``): triage to the material names, fetch a
    bundle for each, and reach a verdict. Returns ALL verdicts (incl. HOLD) — the
    caller filters with ``actionable_verdicts`` so HOLDs stay silent.

    ``triage`` is the tiering gate: return False to skip a name's (expensive) deep
    research. ``None`` researches every holding.
    """
    verdicts: list[StockDecisionOutput] = []
    for ticker, usd in (holdings or {}).items():
        if triage is not None and not triage(ticker, float(usd)):
            continue
        bundle = research_bundle(ticker, fetchers=fetchers)
        verdicts.append(
            decide(ticker, context=context_of(ticker, float(usd)),
                   bundle=bundle, user_id=user_id)
        )
    return verdicts


def actionable_verdicts(
    verdicts: list[StockDecisionOutput],
) -> list[StockDecisionOutput]:
    """Only the verdicts that need the client (BUY/SELL/TRIM). HOLD is dropped —
    thesis intact, no action, so it never becomes inbox noise."""
    return [v for v in verdicts if is_actionable(v.verdict)]


def write_stock_decision_proposal(db: Any, user_id: str, v: StockDecisionOutput) -> Any:
    """Persist an actionable verdict as an open ActionProposal (the inbox 'note'
    sink). Idempotent per (user, ticker) via dedup_key; a collision is swallowed."""
    import json
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.exc import IntegrityError

    from argosy.state.models import ActionProposal

    now = datetime.now(timezone.utc)
    evidence = "\n".join(f"- {e}" for e in (v.evidence or []))
    gaps = ", ".join(v.data_gaps or [])
    rationale = v.reason + (f"\n\nEvidence:\n{evidence}" if evidence else "")
    rationale += f"\n\n_Data gaps: {gaps}_" if gaps else ""
    row = ActionProposal(
        user_id=user_id,
        summary=f"{v.verdict.title()} {v.ticker} — {v.reason[:100]}",
        rationale_md=rationale,
        suggested_payload=json.dumps({
            "ticker": v.ticker, "verdict": v.verdict, "confidence": v.confidence,
            "evidence": list(v.evidence or []), "data_gaps": list(v.data_gaps or []),
        }),
        severity=_SEVERITY_BY_VERDICT.get(v.verdict.upper(), "info"),
        surfaced_at=now,
        expires_at=now + timedelta(days=14),
        status="open",
        kind="stock_decision",
        dedup_key=f"stock_decision:{user_id}:{v.ticker.upper()}",
        execution_state="proposed",
    )
    db.add(row)
    try:
        db.commit()
        return row
    except IntegrityError:  # an open peer already holds the dedup slot
        db.rollback()
        return None


def run_holdings_review(
    db: Any,
    user_id: str,
    *,
    min_position_usd: float = 5_000.0,
    holdings: dict[str, float] | None = None,
    fetchers: dict[str, Fetcher] | None = None,
    decide: Callable[..., StockDecisionOutput] = decide_stock,
    sink: Callable[[StockDecisionOutput], Any] | None = None,
) -> dict[str, Any]:
    """Review the book per-name and act: triage to material positions, fetch fresh
    data, decide, and write ONLY actionable verdicts to the inbox. HOLD verdicts
    are logged for audit and stay silent. Returns a summary + the full verdict list.

    All collaborators default to the live wiring but are injectable for tests.
    """
    if holdings is None:
        from argosy.api.routes.portfolio import _load_current_doc_and_holdings

        _doc, holdings, _cash = _load_current_doc_and_holdings(user_id)
    if fetchers is None:
        from argosy.services.stock_decision.fetchers import default_fetchers

        fetchers = default_fetchers(db, user_id)
    if sink is None:
        def sink(v: StockDecisionOutput) -> Any:
            return write_stock_decision_proposal(db, user_id, v)

    verdicts = decide_holdings(
        holdings, fetchers=fetchers,
        context_of=lambda t, usd: f"held ${usd:,.0f}",
        triage=lambda t, usd: usd >= min_position_usd,
        decide=decide, user_id=user_id,
    )
    surfaced = actionable_verdicts(verdicts)
    written = 0
    for v in surfaced:
        try:
            if sink(v) is not None:
                written += 1
        except Exception as exc:  # noqa: BLE001 — one bad write must not abort the review
            log.warning("stock_decision.sink_failed", ticker=v.ticker, err=str(exc)[:120])
    for v in verdicts:
        if not is_actionable(v.verdict):
            log.info("stock_decision.hold", ticker=v.ticker, reason=(v.reason or "")[:120])
    return {
        "reviewed": len(verdicts),
        "actionable": len(surfaced),
        "written": written,
        "verdicts": verdicts,
    }


__all__ = [
    "research_bundle", "decide_holdings", "actionable_verdicts", "Fetcher",
    "run_holdings_review", "write_stock_decision_proposal",
]
