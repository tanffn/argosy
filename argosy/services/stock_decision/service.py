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


_REDUCE_VERDICTS = frozenset({"SELL", "TRIM"})

# Thesis-monitor flag kinds that ELEVATE a holding past the size triage: a
# deteriorating thesis means the name gets a deep pass regardless of position
# size (a small position can still go to zero while the warning hides in a flag).
_ELEVATING_FLAG_KINDS = ("thesis_monitor_weakened", "thesis_monitor_broken")


def load_elevated_thesis_flags(
    db: Any, user_id: str, *, now: "Any | None" = None
) -> dict[str, dict[str, str]]:
    """Active, unexpired ``thesis_monitor_weakened`` / ``thesis_monitor_broken``
    monitor flags keyed by TICKER (uppercased, from the flag payload — the
    thesis_monitor writes it directly; see ``write_thesis_flag``).

    Each value carries ``kind``, ``status`` (weakened/broken) and ``summary``
    (the flag's rationale — the deteriorating-thesis evidence, fed to the
    decision agent as labelled input). ``broken`` wins over ``weakened`` when a
    ticker somehow carries both. Best-effort: any failure returns ``{}`` so the
    review degrades to plain size triage rather than aborting.
    """
    import json
    from datetime import datetime, timezone

    if now is None:
        now = datetime.now(timezone.utc)
    out: dict[str, dict[str, str]] = {}
    try:
        from sqlalchemy import and_, select

        from argosy.state.models import MonitorFlag

        rows = db.execute(
            select(MonitorFlag).where(
                and_(
                    MonitorFlag.user_id == user_id,
                    MonitorFlag.status == "active",
                    MonitorFlag.acknowledged_at.is_(None),
                    MonitorFlag.kind.in_(_ELEVATING_FLAG_KINDS),
                )
            )
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001 — elevation is additive, never fatal
        log.warning("holdings_review.thesis_flags_load_failed", err=str(exc)[:120])
        return out
    for r in rows:
        # Skip expired flags (best-effort; expires_at may be None / naive).
        exp = getattr(r, "expires_at", None)
        if exp is not None:
            try:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < now:
                    continue
            except Exception:  # noqa: BLE001
                pass
        try:
            payload = json.loads(r.payload or "{}")
        except (TypeError, ValueError):
            payload = {}
        ticker = (payload.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        status = str(payload.get("thesis_status") or r.kind.removeprefix("thesis_monitor_"))
        entry = {
            "kind": r.kind,
            "status": status,
            "summary": (payload.get("rationale_md") or "")[:400],
        }
        prior = out.get(ticker)
        if prior is None or (status == "broken" and prior["status"] != "broken"):
            out[ticker] = entry
    return out


def verify_verdict(
    v: StockDecisionOutput,
    *,
    bundle: dict[str, Any],
    decide: Callable[..., StockDecisionOutput] = decide_stock,
    user_id: str = "ariel",
) -> bool:
    """Blind re-derivation gate for a consequential verdict.

    Independently re-decides from the SAME raw bundle (the re-run is NOT shown the
    original verdict or its reasoning — it re-derives from the evidence) and confirms
    the actionable DIRECTION. Fail-closed: a SELL/TRIM must be re-confirmed as a
    reduce, a BUY as a buy; anything else (incl. a re-derived HOLD) fails the gate,
    so a trade only surfaces when two independent passes agree it is warranted.
    HOLD is not actionable and never reaches this gate. See
    ``feedback_adversarial_review_must_re_derive_blind``.
    """
    if not is_actionable(v.verdict):
        return True
    redo = decide(v.ticker, context="independent blind re-review", bundle=bundle, user_id=user_id)
    orig = v.verdict.upper()
    again = (redo.verdict or "").upper()
    if orig in _REDUCE_VERDICTS:
        return again in _REDUCE_VERDICTS
    if orig == "BUY":
        return again == "BUY"
    return False


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
    except IntegrityError as exc:
        # Expected on a dedup collision (an open peer holds the slot) — but log
        # the actual error: a CHECK-constraint failure looked exactly like a
        # dedup collision here and silently killed this sink from ship day
        # until migration 0077 relaxed the kind CHECK (2026-07-05).
        db.rollback()
        log.warning(
            "stock_decision.proposal_write_skipped",
            ticker=v.ticker, error=str(exc.orig)[:160],
        )
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
    verify: "Callable[[StockDecisionOutput, dict], bool] | None | bool" = None,
    elevated_flags: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Review the book per-name and act: triage to material positions, fetch fresh
    data, decide, blind-verify actionable verdicts, and write ONLY the confirmed
    ones to the inbox. HOLD verdicts are logged for audit and stay silent. Returns
    a summary + the full verdict list.

    Triage is (material by size) OR (active thesis_monitor weakened/broken flag):
    a deteriorating thesis elevates the name into the deep pass regardless of
    position size, and the flag's evidence is appended to the agent's context as
    labelled INPUT — the agent still makes the judgment. ``elevated_flags``
    (ticker -> flag dict) overrides the default DB load for tests.

    ``verify`` gates each actionable verdict (fail-closed on divergence): ``None``
    uses the default blind re-derivation, a callable overrides it, and ``False``
    disables the gate. All collaborators are injectable for tests.
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
    if verify is None:
        def verify(v: StockDecisionOutput, bundle: dict[str, Any]) -> bool:
            return verify_verdict(v, bundle=bundle, decide=decide, user_id=user_id)
    if elevated_flags is None:
        elevated_flags = (
            load_elevated_thesis_flags(db, user_id) if db is not None else {}
        )

    verdicts: list[StockDecisionOutput] = []
    written = 0
    actionable = 0
    held_unverified = 0
    elevated: list[str] = []
    for ticker, usd in (holdings or {}).items():
        flag = elevated_flags.get(ticker.upper())
        if flag is None and float(usd) < min_position_usd:
            continue  # tiering: skip immaterial positions (no expensive research)
        context = f"held ${float(usd):,.0f}"
        if flag is not None:
            # A weakened/broken thesis elevates the name past the size gate —
            # a small position can still go to zero. Auditable escalation.
            elevated.append(ticker.upper())
            log.info(
                "holdings_review.elevated_by_thesis",
                ticker=ticker.upper(), kind=flag.get("kind"),
            )
            context += (
                f"; thesis_monitor status: {flag.get('status')}"
                f" — {flag.get('summary') or '(no rationale recorded)'}"
            )
        bundle = research_bundle(ticker, fetchers=fetchers)
        v = decide(ticker, context=context, bundle=bundle, user_id=user_id)
        verdicts.append(v)
        if not is_actionable(v.verdict):
            log.info("stock_decision.hold", ticker=v.ticker, reason=(v.reason or "")[:120])
            continue
        actionable += 1
        # Fail-closed: a consequential trade must survive a blind re-derivation.
        if verify is not False and not verify(v, bundle):
            held_unverified += 1
            log.info("stock_decision.held_unverified", ticker=v.ticker, verdict=v.verdict)
            continue
        try:
            if sink(v) is not None:
                written += 1
        except Exception as exc:  # noqa: BLE001 — one bad write must not abort the review
            log.warning("stock_decision.sink_failed", ticker=v.ticker, err=str(exc)[:120])
    return {
        "reviewed": len(verdicts),
        "actionable": actionable,
        "written": written,
        "held_unverified": held_unverified,
        "elevated": elevated,
        "verdicts": verdicts,
    }


__all__ = [
    "research_bundle", "decide_holdings", "actionable_verdicts", "Fetcher",
    "run_holdings_review", "write_stock_decision_proposal",
    "load_elevated_thesis_flags",
]
