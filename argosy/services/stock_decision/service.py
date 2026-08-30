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

from collections.abc import Callable
from datetime import UTC
from typing import Any

from argosy.agents.stock_decision import (
    StockDecisionOutput,
    decide_stock,
    evidence_field_is_usable,
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
    db: Any, user_id: str, *, now: Any | None = None
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
    from datetime import datetime

    if now is None:
        now = datetime.now(UTC)
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
                    exp = exp.replace(tzinfo=UTC)
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
    redo = decide(
        v.ticker,
        context="independent blind re-review",
        bundle=bundle,
        user_id=user_id,
    )
    return _verdicts_align(v, redo)


def _verdicts_align(
    original: StockDecisionOutput,
    rederived: StockDecisionOutput,
) -> bool:
    """Whether two independently derived verdicts agree directionally."""
    orig = original.verdict.upper()
    again = (rederived.verdict or "").upper()
    if orig in _REDUCE_VERDICTS:
        return again in _REDUCE_VERDICTS
    if orig == "BUY":
        return again == "BUY"
    return False


def write_stock_decision_proposal(db: Any, user_id: str, v: StockDecisionOutput) -> Any:
    """Persist an actionable verdict as an open ActionProposal (the inbox 'note'
    sink). Idempotent per (user, ticker) via dedup_key; a collision is swallowed."""
    import json
    from datetime import datetime, timedelta

    from sqlalchemy.exc import IntegrityError

    from argosy.state.models import ActionProposal

    now = datetime.now(UTC)
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
    # Dedup protects the UI from duplicate cards; it must not freeze the first
    # rationale forever. A fresh confirmed review updates that one open row.
    if hasattr(db, "execute"):
        from sqlalchemy import select

        existing = db.execute(select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == row.dedup_key,
            ActionProposal.status == "open",
        )).scalar_one_or_none()
        if existing is not None:
            for field in (
                "summary",
                "rationale_md",
                "suggested_payload",
                "severity",
                "surfaced_at",
                "expires_at",
            ):
                setattr(existing, field, getattr(row, field))
            existing.execution_state = "proposed"
            db.commit()
            return existing
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


def supersede_stock_decision_proposal(
    db: Any,
    user_id: str,
    ticker: str,
) -> None:
    """Retire a stale open action when a newer reasoned HOLD supersedes it."""
    if db is None or not hasattr(db, "execute"):
        return
    from sqlalchemy import select

    from argosy.state.models import ActionProposal

    row = db.execute(select(ActionProposal).where(
        ActionProposal.user_id == user_id,
        ActionProposal.dedup_key == (
            f"stock_decision:{user_id}:{ticker.strip().upper()}"
        ),
        ActionProposal.status == "open",
    )).scalar_one_or_none()
    if row is None:
        return
    row.status = "superseded"
    row.execution_state = "dismissed"
    db.commit()


def record_holding_review(
    db: Any,
    user_id: str,
    v: StockDecisionOutput,
    *,
    position_usd: float | None,
    elevated_by_flag: bool,
    outcome: str,
    reviewed_at: Any | None = None,
    verification: StockDecisionOutput | None = None,
    portfolio_weight_pct: float | None = None,
    portfolio_total_usd: float | None = None,
) -> None:
    """Persist one queryable ``holding_reviews`` audit row for a verdict
    ("nothing hidden — reviewed means a queryable row"). Best-effort: an audit
    write failure must never abort the review; no-op when ``db`` is None."""
    if db is None:
        return
    import json
    from datetime import datetime

    try:
        from argosy.state.models import HoldingReview

        db.add(HoldingReview(
            user_id=user_id,
            symbol=(v.ticker or "").upper(),
            reviewed_at=reviewed_at or datetime.now(UTC),
            verdict=(v.verdict or "").upper(),
            confidence=str(v.confidence or "") or None,
            reason=(v.reason or ""),
            evidence_json=json.dumps({
                "evidence": list(v.evidence or []),
                "data_gaps": list(v.data_gaps or []),
                "portfolio_weight_pct": portfolio_weight_pct,
                "portfolio_total_usd": portfolio_total_usd,
                "verification": (
                    {
                        "verdict": verification.verdict,
                        "confidence": verification.confidence,
                        "reason": verification.reason,
                        "evidence": list(verification.evidence or []),
                        "data_gaps": list(verification.data_gaps or []),
                    }
                    if verification is not None
                    else None
                ),
            }),
            position_usd=position_usd,
            elevated_by_flag=bool(elevated_by_flag),
            outcome=outcome,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — audit is additive, never fatal
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning(
            "holdings_review.audit_write_failed",
            ticker=v.ticker, outcome=outcome, err=str(exc)[:160],
        )


def load_x10_sleeve_symbols(db: Any, user_id: str) -> frozenset[str]:
    """Symbols of the current plan's high-growth / x10 (moonshot) sleeve.

    These positions are deliberately SMALL (TEM/OKLO at ~$4.8k) — the exact
    profile the $5k size triage makes review-invisible — yet they are the
    highest-variance theses in the book. They form the triage FLOOR: reviewed
    every pass regardless of size. Best-effort: any failure returns an empty
    set (review degrades to plain size triage)."""
    try:
        from argosy.services.allocation_plan import HIGH_GROWTH_SIGMA_CLASS
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(db, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        if doc is None:
            return frozenset()
        out: set[str] = set()
        for c in doc.classes:
            if getattr(c, "sigma_class", "") != HIGH_GROWTH_SIGMA_CLASS:
                continue
            for inst in getattr(c, "instruments", []) or []:
                sym = (getattr(inst, "symbol", "") or "").strip().upper()
                if sym:
                    out.add(sym)
        return frozenset(out)
    except Exception as exc:  # noqa: BLE001 — floor is additive, never fatal
        log.warning("holdings_review.x10_symbols_load_failed", err=str(exc)[:120])
        return frozenset()


def run_holdings_review(
    db: Any,
    user_id: str,
    *,
    min_position_usd: float = 5_000.0,
    holdings: dict[str, float] | None = None,
    fetchers: dict[str, Fetcher] | None = None,
    decide: Callable[..., StockDecisionOutput] = decide_stock,
    sink: Callable[[StockDecisionOutput], Any] | None = None,
    verify: Callable[[StockDecisionOutput, dict], bool] | None | bool = None,
    elevated_flags: dict[str, dict[str, str]] | None = None,
    always_review: frozenset[str] | set[str] | None = None,
    record: Callable[..., None] | None | bool = None,
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

    ``always_review`` is the triage FLOOR: symbols reviewed regardless of size
    (default: the plan's high-growth / x10 sleeve members — small by design,
    highest-variance theses; the $5k gate made them review-invisible).

    ``record`` writes one queryable ``holding_reviews`` audit row per verdict
    (incl. HOLD — "nothing hidden"): ``None`` uses the default DB writer (no-op
    when ``db`` is None), a callable overrides it, ``False`` disables it.
    Outcomes: proposed / held_unverified / hold / dedup_skipped.
    """
    routed_to_fund_review: list[str] = []
    portfolio_cash_usd = 0.0
    if holdings is None:
        from argosy.api.routes.portfolio import _load_current_doc_and_holdings
        from argosy.services.instrument_reference import STRUCT_STOCK, lookup

        _doc, holdings, _cash = _load_current_doc_and_holdings(user_id)
        portfolio_cash_usd = max(0.0, float(_cash or 0.0))
        all_holdings = dict(holdings or {})
        # This service is the single-company reviewer.  Collective vehicles
        # have a dedicated reviewer (and a scheduled all-holdings coverage
        # sweep) that asks the relevant mandate / domicile / fee / overlap
        # questions.  Sending an index-fund display identity such as
        # ``MSCI WORLD`` through Yahoo company fundamentals only creates slow,
        # meaningless ABSTAINs.  Route by canonical instrument structure; keep
        # unknown symbols on this path so an unclassified real holding remains
        # fail-loud instead of silently disappearing.
        stock_holdings: dict[str, float] = {}
        for symbol, value in (holdings or {}).items():
            ref = lookup(symbol)
            if ref is not None and ref.structure != STRUCT_STOCK:
                routed_to_fund_review.append(symbol.upper())
                continue
            stock_holdings[symbol] = value
        holdings = stock_holdings
    else:
        all_holdings = dict(holdings)
    portfolio_total_usd = (
        sum(max(0.0, float(value or 0.0)) for value in all_holdings.values())
        + portfolio_cash_usd
    )
    if fetchers is None:
        from argosy.services.stock_decision.fetchers import default_fetchers

        fetchers = default_fetchers(db, user_id)
    if sink is None:
        def sink(v: StockDecisionOutput) -> Any:
            return write_stock_decision_proposal(db, user_id, v)
    verification_outputs: dict[str, StockDecisionOutput] = {}
    verification_contexts: dict[str, str] = {}
    if verify is None:
        def verify(v: StockDecisionOutput, bundle: dict[str, Any]) -> bool:
            redo = decide(
                v.ticker,
                context=(
                    verification_contexts.get(v.ticker.upper(), "")
                    + "; role=independent blind re-review"
                ).strip("; "),
                bundle=bundle,
                user_id=user_id,
            )
            verification_outputs[v.ticker.upper()] = redo
            return _verdicts_align(v, redo)
    if elevated_flags is None:
        elevated_flags = (
            load_elevated_thesis_flags(db, user_id) if db is not None else {}
        )
    if always_review is None:
        always_review = (
            load_x10_sleeve_symbols(db, user_id) if db is not None else frozenset()
        )
    if record is None:
        def record(v: StockDecisionOutput, **kw: Any) -> None:
            record_holding_review(db, user_id, v, **kw)
    elif record is False:
        def record(v: StockDecisionOutput, **kw: Any) -> None:
            return None

    verdicts: list[StockDecisionOutput] = []
    written = 0
    actionable = 0
    held_unverified = 0
    elevated: list[str] = []
    evidence_coverage = {
        field: 0
        for field in (
            "news",
            "earnings_calendar",
            "earnings_filing",
            "fundamentals",
            "sentiment",
            "price",
            "thesis",
            "tax",
        )
    }
    evidence_providers: dict[str, int] = {}
    for ticker, usd in (holdings or {}).items():
        flag = elevated_flags.get(ticker.upper())
        floored = ticker.upper() in (always_review or frozenset())
        if flag is None and not floored and float(usd) < min_position_usd:
            continue  # tiering: skip immaterial positions (no expensive research)
        weight_pct = (
            100.0 * float(usd) / portfolio_total_usd
            if portfolio_total_usd > 0
            else None
        )
        context = f"held ${float(usd):,.0f}"
        if weight_pct is not None:
            context += (
                f"; {weight_pct:.2f}% of whole portfolio"
                f" (${portfolio_total_usd:,.0f}, including cash and funds)"
            )
        if flag is None and floored and float(usd) < min_position_usd:
            # Plan x10-sleeve member: small by design, reviewed regardless of
            # size — the position IS the thesis bet, not noise.
            context += "; plan x10-sleeve member (size-floor exempt)"
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
        for field in evidence_coverage:
            value = bundle.get(field)
            if not evidence_field_is_usable(value):
                continue
            evidence_coverage[field] += 1
            text = str(value)
            if text.startswith("source="):
                provider = text.partition(";")[0].removeprefix("source=").strip()
                if provider:
                    key = f"{field}:{provider}"
                    evidence_providers[key] = evidence_providers.get(key, 0) + 1
        v = decide(ticker, context=context, bundle=bundle, user_id=user_id)
        verification_contexts[ticker.upper()] = context
        verdicts.append(v)
        _audit = dict(
            position_usd=float(usd),
            elevated_by_flag=flag is not None,
            portfolio_weight_pct=weight_pct,
            portfolio_total_usd=portfolio_total_usd,
        )
        if (v.verdict or "").upper() == "ABSTAIN":
            log.info(
                "stock_decision.abstained",
                ticker=v.ticker, reason=(v.reason or "")[:120],
            )
            record(v, outcome="abstained", **_audit)
            continue
        if not is_actionable(v.verdict):
            log.info("stock_decision.hold", ticker=v.ticker, reason=(v.reason or "")[:120])
            supersede_stock_decision_proposal(db, user_id, v.ticker)
            record(v, outcome="hold", **_audit)
            continue
        actionable += 1
        # Fail-closed: a consequential trade must survive a blind re-derivation.
        if verify is not False and not verify(v, bundle):
            _audit["verification"] = verification_outputs.get(v.ticker.upper())
            held_unverified += 1
            log.info("stock_decision.held_unverified", ticker=v.ticker, verdict=v.verdict)
            record(v, outcome="held_unverified", **_audit)
            continue
        _audit["verification"] = verification_outputs.get(v.ticker.upper())
        outcome = "dedup_skipped"
        try:
            if sink(v) is not None:
                written += 1
                outcome = "proposed"
        except Exception as exc:  # noqa: BLE001 — one bad write must not abort the review
            log.warning("stock_decision.sink_failed", ticker=v.ticker, err=str(exc)[:120])
        record(v, outcome=outcome, **_audit)
    return {
        "reviewed": len(verdicts),
        "actionable": actionable,
        "written": written,
        "held_unverified": held_unverified,
        "abstained": sum(
            1 for v in verdicts if (v.verdict or "").upper() == "ABSTAIN"
        ),
        "decisions": sum(
            1 for v in verdicts if (v.verdict or "").upper() != "ABSTAIN"
        ),
        "elevated": elevated,
        "evidence_coverage": evidence_coverage,
        "evidence_providers": evidence_providers,
        "routed_to_fund_review": sorted(routed_to_fund_review),
        "portfolio_total_usd": portfolio_total_usd,
        "verdicts": verdicts,
    }


__all__ = [
    "research_bundle", "decide_holdings", "actionable_verdicts", "Fetcher",
    "run_holdings_review", "write_stock_decision_proposal",
    "load_elevated_thesis_flags", "record_holding_review",
    "load_x10_sleeve_symbols",
    "supersede_stock_decision_proposal",
]
