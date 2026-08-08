"""Settled-verdict registry + pushback gate + deterministic trigger checker.

Design: docs/handovers/2026-07-11-external-implementer-opens.md §3.B;
memory ``feedback_verdicts_defended_not_reopened``.

Every deep-decision / adjudication writes a settled row. A re-run on a
settled subject must cite a NEW fact that hits a recorded falsifier or
typed revisit trigger — else the entry point returns DEFENDED (standing
verdict, zero agent spawns). The daily trigger checker is a cheap cron
(price / dated_event); a fired trigger UNLOCKS re-evaluation via a
needs-confirm inbox row — it never launches the fleet itself.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import (
    ActionProposal,
    DecisionRun,
    HoldingReview,
    PositionStance,
    Proposal,
    Verdict,
)

_log = get_logger("argosy.services.verdict_registry")

TriggerKind = Literal[
    "price_below",
    "price_above",
    "metric_condition",
    "dated_event",
]

VALID_VERDICTS = frozenset({"BUY", "ADD", "HOLD", "TRIM", "SELL", "WAIT"})
VALID_CONVICTIONS = frozenset({"HIGH", "MED", "LOW", "MEDIUM"})
VALID_TRIGGER_KINDS = frozenset(
    {"price_below", "price_above", "metric_condition", "dated_event"}
)

# Dedup key for unlock inbox rows written when a trigger fires.
UNLOCK_DEDUP_PREFIX = "verdict_revisit_unlocked"


@dataclass(frozen=True)
class PushbackGateResult:
    """Outcome of the pushback / new-facts gate."""

    allowed: bool
    standing: Verdict | None = None
    reason: str = ""
    matched_falsifier: str | None = None
    matched_trigger: dict[str, Any] | None = None

    @property
    def defended(self) -> bool:
        return not self.allowed and self.standing is not None


@dataclass
class FiredTrigger:
    verdict_id: int
    subject: str
    trigger: dict[str, Any]
    evidence: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_subject(subject: str) -> str:
    return (subject or "").strip().upper()


def _norm_conviction(conviction: str) -> str:
    c = (conviction or "MED").strip().upper()
    if c == "MEDIUM":
        return "MED"
    if c not in VALID_CONVICTIONS:
        return "MED"
    return "MED" if c == "MEDIUM" else c


def _norm_verdict(verdict: str) -> str:
    v = (verdict or "").strip().upper()
    if v in ("HOLD/WAIT", "HOLD_WAIT", "WAIT/HOLD"):
        return "WAIT"
    if v not in VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    return v


def _dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def get_settled_verdict(
    session: Session, *, user_id: str, subject: str
) -> Verdict | None:
    """Return the settled standing verdict for ``subject``, or None."""
    subj = _norm_subject(subject)
    if not subj:
        return None
    return session.execute(
        select(Verdict)
        .where(
            Verdict.user_id == user_id,
            Verdict.subject == subj,
            Verdict.settled.is_(True),
        )
        .order_by(Verdict.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def write_verdict(
    session: Session,
    *,
    user_id: str,
    subject: str,
    verdict: str,
    conviction: str,
    falsifiers: list[str] | None = None,
    revisit_triggers: list[dict[str, Any]] | None = None,
    next_validation: date | None = None,
    source_decision_run_id: int | None = None,
    reasoning_md: str = "",
    settled: bool = True,
    entry_price: float | None = None,
) -> Verdict:
    """Insert a verdict row; if ``settled``, supersede any prior settled row.

    Typed triggers are validated (unknown kinds raise). Idempotent on the
    same run id: a second write for the same ``source_decision_run_id`` on
    the same subject refreshes the standing row in place.

    ``entry_price`` — optional snapshot for the fleet-verdict prediction
    ledger. This function NEVER fetches market data (measurement must not
    stall the decision transaction). Callers that have a price pass it;
    otherwise use :func:`register_fleet_prediction_for_verdict` after
    commit with a hard-timeout quote resolver / the backfill command.
    """
    subj = _norm_subject(subject)
    v = _norm_verdict(verdict)
    conv = _norm_conviction(conviction)
    triggers = list(revisit_triggers or [])
    for t in triggers:
        kind = str((t or {}).get("kind") or "")
        if kind not in VALID_TRIGGER_KINDS:
            raise ValueError(f"invalid revisit trigger kind: {kind!r}")

    if settled and source_decision_run_id is not None:
        existing_same_run = session.execute(
            select(Verdict)
            .where(
                Verdict.user_id == user_id,
                Verdict.subject == subj,
                Verdict.source_decision_run_id == source_decision_run_id,
            )
            .order_by(Verdict.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing_same_run is not None:
            existing_same_run.verdict = v
            existing_same_run.conviction = conv
            existing_same_run.falsifiers_json = _dumps(list(falsifiers or []))
            existing_same_run.revisit_triggers_json = _dumps(triggers)
            existing_same_run.next_validation = next_validation
            existing_same_run.reasoning_md = reasoning_md or ""
            existing_same_run.settled = True
            existing_same_run.updated_at = _utcnow()
            session.flush()
            _safe_fleet_prediction_write(
                session,
                user_id=user_id,
                verdict_row=existing_same_run,
                entry_price=entry_price,
            )
            try:
                from argosy.services.home_greeting_cache import mark_home_greeting_dirty

                mark_home_greeting_dirty(user_id, session=session)
            except Exception:  # noqa: BLE001
                pass
            return existing_same_run

    prior = get_settled_verdict(session, user_id=user_id, subject=subj) if settled else None
    # Clear settled on the prior BEFORE inserting the new row so the partial
    # unique index on (user_id, subject) WHERE settled=1 never sees two rows.
    if prior is not None:
        prior.settled = False
        prior.updated_at = _utcnow()
        session.flush()

    row = Verdict(
        user_id=user_id,
        subject=subj,
        verdict=v,
        conviction=conv,
        falsifiers_json=_dumps(list(falsifiers or [])),
        revisit_triggers_json=_dumps(triggers),
        next_validation=next_validation,
        source_decision_run_id=source_decision_run_id,
        settled=settled,
        reasoning_md=reasoning_md or "",
    )
    session.add(row)
    session.flush()

    if prior is not None and prior.id != row.id:
        prior.superseded_by = row.id
        prior.updated_at = _utcnow()
        session.flush()

    _log.info(
        "verdict_registry.written",
        user_id=user_id,
        subject=subj,
        verdict=v,
        conviction=conv,
        settled=settled,
        run_id=source_decision_run_id,
        superseded_prior=prior.id if prior is not None else None,
    )
    _safe_fleet_prediction_write(
        session,
        user_id=user_id,
        verdict_row=row,
        entry_price=entry_price,
    )
    try:
        from argosy.services.home_greeting_cache import mark_home_greeting_dirty

        mark_home_greeting_dirty(user_id, session=session)
    except Exception:  # noqa: BLE001
        pass
    return row


#: Hard ceiling for deferred quote resolution (seconds). Measurement
#: must never hang the decision path; callers run this AFTER commit.
FLEET_QUOTE_TIMEOUT_SECONDS: float = 3.0


def resolve_entry_price_with_timeout(
    subject: str,
    *,
    timeout_seconds: float = FLEET_QUOTE_TIMEOUT_SECONDS,
) -> float | None:
    """Resolve last price off the critical path with a hard timeout.

    Uses a daemon thread + ``join(timeout)`` so returning after the
    deadline does **not** wait for the worker (``ThreadPoolExecutor``
    ``shutdown(wait=True)`` would). A hung provider cannot block the
    decision path indefinitely.
    """
    import threading

    sym = (subject or "").strip().upper()
    if not sym:
        return None

    box: dict[str, float | None] = {"px": None}

    def _fetch() -> None:
        try:
            import yfinance as yf  # type: ignore[import-untyped]

            t = yf.Ticker(sym)
            fast = getattr(t, "fast_info", None)
            if fast is not None:
                px = getattr(fast, "last_price", None)
                if px is None and isinstance(fast, dict):
                    px = fast.get("last_price") or fast.get("lastPrice")
                if px is not None:
                    box["px"] = float(px)
                    return
            info = getattr(t, "info", None) or {}
            if isinstance(info, dict):
                for key in (
                    "currentPrice",
                    "regularMarketPrice",
                    "previousClose",
                ):
                    if info.get(key) is not None:
                        box["px"] = float(info[key])
                        return
        except Exception:  # noqa: BLE001
            box["px"] = None

    worker = threading.Thread(target=_fetch, name=f"fleet-quote-{sym}", daemon=True)
    worker.start()
    worker.join(timeout=timeout_seconds)
    if worker.is_alive():
        _log.warning(
            "verdict_registry.quote_timeout",
            subject=sym,
            timeout_seconds=timeout_seconds,
        )
        return None
    return box["px"]


def register_fleet_prediction_for_verdict(
    session: Session,
    *,
    user_id: str,
    verdict_id: int,
    entry_price: float | None = None,
    resolve_quote: bool = False,
) -> Any:
    """Post-commit ledger registration for a settled verdict.

    Safe to call after the verdict transaction has committed. Optionally
    resolves a quote with :func:`resolve_entry_price_with_timeout` when
    ``resolve_quote=True`` and ``entry_price`` is None. Returns the
    Prediction row, or ``None`` when no entry is available / verdict
    kind is unregistered.
    """
    row = session.get(Verdict, verdict_id)
    if row is None or row.user_id != user_id:
        return None
    px = entry_price
    if px is None and resolve_quote:
        px = resolve_entry_price_with_timeout(row.subject)
    return _maybe_write_fleet_prediction(
        session,
        user_id=user_id,
        verdict_row=row,
        entry_price=px,
    )


def _safe_fleet_prediction_write(
    session: Session,
    *,
    user_id: str,
    verdict_row: Verdict,
    entry_price: float | None,
) -> None:
    """Best-effort prediction write; never fails the verdict transaction.

    Always attempts a durable row — missing entry becomes a pending-entry
    omission visible on the scorecard (iter-2 finding 2).
    """
    try:
        _maybe_write_fleet_prediction(
            session,
            user_id=user_id,
            verdict_row=verdict_row,
            entry_price=entry_price,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "verdict_registry.fleet_prediction_write_failed",
            subject=verdict_row.subject,
            verdict_id=verdict_row.id,
            error=str(exc)[:200],
        )


def _maybe_write_fleet_prediction(
    session: Session,
    *,
    user_id: str,
    verdict_row: Verdict,
    entry_price: float | None,
) -> Any:
    """Idempotent ledger write for settled fleet verdicts (incl. HOLD)."""
    from argosy.services.predictions.writers import write_fleet_verdict_prediction

    triggers = [
        t
        for t in _loads_list(verdict_row.revisit_triggers_json)
        if isinstance(t, dict)
    ]
    return write_fleet_verdict_prediction(
        session,
        user_id,
        verdict_id=verdict_row.id,
        ticker=verdict_row.subject,
        verdict=verdict_row.verdict,
        event_at=verdict_row.created_at or _utcnow(),
        entry_price=entry_price,
        revisit_triggers=triggers,
        next_validation=verdict_row.next_validation,
        conviction=verdict_row.conviction,
        decision_run_id=verdict_row.source_decision_run_id,
    )


def _fact_hits_falsifier(fact: str, falsifier: str) -> bool:
    """Lenient token overlap — majority of falsifier content words in the fact."""
    stop = frozenset({
        "a", "an", "the", "to", "of", "or", "and", "if", "in", "on", "for",
        "with", "by", "at", "is", "be", "as", "that", "this", "from", "would",
        "change", "new", "fact",
    })
    f_words = [
        w for w in re.findall(r"[a-z0-9%$.]+", falsifier.lower()) if w not in stop
    ]
    if not f_words:
        return falsifier.strip().lower() in fact.lower()
    fact_l = fact.lower()
    hits = sum(1 for w in f_words if w in fact_l)
    if len(f_words) == 1:
        return hits == 1
    return hits / len(f_words) > 0.5


def _fact_hits_trigger(fact: str, trigger: dict[str, Any]) -> bool:
    """Cited fact matches a typed trigger (prose or structured keys)."""
    kind = str(trigger.get("kind") or "")
    fact_l = fact.lower()
    if kind == "price_below":
        thr = trigger.get("price")
        if thr is None:
            return False
        # Explicit hit: fact states the threshold was crossed / price is at-or-below.
        if re.search(rf"\b(?:at|below|under|≤|<=)\s*\$?\s*{re.escape(str(thr))}\b", fact_l):
            return True
        if f"price_below:{thr}" in fact_l.replace(" ", ""):
            return True
        return False
    if kind == "price_above":
        thr = trigger.get("price")
        if thr is None:
            return False
        if re.search(rf"\b(?:above|over|≥|>=)\s*\$?\s*{re.escape(str(thr))}\b", fact_l):
            return True
        if f"price_above:{thr}" in fact_l.replace(" ", ""):
            return True
        return False
    if kind == "metric_condition":
        metric = str(trigger.get("metric") or "").lower()
        if metric and metric in fact_l:
            return True
        label = str(trigger.get("label") or "").lower()
        return bool(label) and label in fact_l
    if kind == "dated_event":
        label = str(trigger.get("label") or trigger.get("event") or "").lower()
        if label and label in fact_l:
            return True
        d = trigger.get("date")
        return bool(d) and str(d) in fact
    return False


def check_pushback_gate(
    session: Session,
    *,
    user_id: str,
    subject: str,
    cited_new_facts: list[str] | None = None,
) -> PushbackGateResult:
    """New-facts test against the settled verdict.

    No settled row → allowed (nothing to defend).
    Settled + no matching cited fact → DEFENDED (``allowed=False``).
    Settled + cited fact hits falsifier/trigger → allowed (re-run may proceed).
    """
    standing = get_settled_verdict(session, user_id=user_id, subject=subject)
    if standing is None:
        return PushbackGateResult(allowed=True, reason="no_settled_verdict")

    facts = [f.strip() for f in (cited_new_facts or []) if f and str(f).strip()]
    if not facts:
        return PushbackGateResult(
            allowed=False,
            standing=standing,
            reason=(
                f"DEFENDED: settled {standing.verdict} on {standing.subject} "
                f"(conviction={standing.conviction}); no new fact cited that "
                f"hits a recorded falsifier/trigger"
            ),
        )

    falsifiers = [str(x) for x in _loads_list(standing.falsifiers_json)]
    triggers = [
        t for t in _loads_list(standing.revisit_triggers_json) if isinstance(t, dict)
    ]

    for fact in facts:
        for falsifier in falsifiers:
            if _fact_hits_falsifier(fact, falsifier):
                return PushbackGateResult(
                    allowed=True,
                    standing=standing,
                    reason="new_fact_hits_falsifier",
                    matched_falsifier=falsifier,
                )
        for trigger in triggers:
            if _fact_hits_trigger(fact, trigger):
                return PushbackGateResult(
                    allowed=True,
                    standing=standing,
                    reason="new_fact_hits_trigger",
                    matched_trigger=dict(trigger),
                )

    return PushbackGateResult(
        allowed=False,
        standing=standing,
        reason=(
            f"DEFENDED: settled {standing.verdict} on {standing.subject}; "
            f"cited facts do not hit any recorded falsifier/trigger"
        ),
    )


def evaluate_triggers(
    session: Session,
    *,
    user_id: str,
    quotes: dict[str, float] | None = None,
    metrics: dict[str, dict[str, float]] | None = None,
    today: date | None = None,
) -> list[FiredTrigger]:
    """Cheap deterministic trigger sweep over settled verdicts.

    ``quotes``: ``{SUBJECT: last_price}``.
    ``metrics``: ``{SUBJECT: {metric_name: value}}`` for metric_condition.
    ``today``: calendar date for dated_event (defaults to UTC today).
    """
    today = today or datetime.now(timezone.utc).date()
    quotes = {k.upper(): float(v) for k, v in (quotes or {}).items()}
    metrics = {
        k.upper(): dict(v) for k, v in (metrics or {}).items()
    }
    rows = session.execute(
        select(Verdict).where(
            Verdict.user_id == user_id,
            Verdict.settled.is_(True),
        )
    ).scalars().all()

    fired: list[FiredTrigger] = []
    for row in rows:
        for trigger in _loads_list(row.revisit_triggers_json):
            if not isinstance(trigger, dict):
                continue
            kind = str(trigger.get("kind") or "")
            evidence: str | None = None
            if kind == "price_below":
                px = quotes.get(row.subject)
                thr = trigger.get("price")
                if px is not None and thr is not None and px <= float(thr):
                    evidence = (
                        f"{row.subject} last=${px:.4g} ≤ trigger "
                        f"price_below ${float(thr):.4g}"
                    )
            elif kind == "price_above":
                px = quotes.get(row.subject)
                thr = trigger.get("price")
                if px is not None and thr is not None and px >= float(thr):
                    evidence = (
                        f"{row.subject} last=${px:.4g} ≥ trigger "
                        f"price_above ${float(thr):.4g}"
                    )
            elif kind == "dated_event":
                d_raw = trigger.get("date")
                try:
                    d = date.fromisoformat(str(d_raw)[:10]) if d_raw else None
                except ValueError:
                    d = None
                if d is not None and today >= d:
                    label = trigger.get("label") or trigger.get("event") or d.isoformat()
                    evidence = (
                        f"{row.subject} dated_event '{label}' due "
                        f"{d.isoformat()} (today={today.isoformat()})"
                    )
            elif kind == "metric_condition":
                metric = str(trigger.get("metric") or "")
                op = str(trigger.get("op") or ">=")
                thr = trigger.get("value")
                subj_m = metrics.get(row.subject) or {}
                if metric and thr is not None and metric in subj_m:
                    val = float(subj_m[metric])
                    thr_f = float(thr)
                    ok = (
                        val >= thr_f if op in (">=", "gt_eq", "gte")
                        else val > thr_f if op in (">", "gt")
                        else val <= thr_f if op in ("<=", "lt_eq", "lte")
                        else val < thr_f if op in ("<", "lt")
                        else val == thr_f
                    )
                    if ok:
                        evidence = (
                            f"{row.subject} {metric}={val} {op} {thr_f}"
                        )
            if evidence:
                fired.append(
                    FiredTrigger(
                        verdict_id=row.id,
                        subject=row.subject,
                        trigger=dict(trigger),
                        evidence=evidence,
                    )
                )
    return fired


def write_unlock_inbox_rows(
    session: Session,
    *,
    user_id: str,
    fired: list[FiredTrigger],
    now: datetime | None = None,
) -> list[int]:
    """Upsert needs-confirm ``note_only`` proposals for fired triggers.

    Does NOT launch agents — unlock only. Dedup key
    ``verdict_revisit_unlocked:{subject}:{verdict_id}``.
    """
    from datetime import timedelta

    now = now or _utcnow()
    ids: list[int] = []
    for ft in fired:
        dedup = f"{UNLOCK_DEDUP_PREFIX}:{ft.subject}:{ft.verdict_id}"
        summary = (
            f"revisit unlocked: {ft.subject} — {ft.evidence}"
        )
        rationale = (
            f"Deterministic verdict-trigger checker fired for settled "
            f"verdict #{ft.verdict_id} on {ft.subject}.\n\n"
            f"**Evidence:** {ft.evidence}\n\n"
            f"**Trigger:** `{json.dumps(ft.trigger)}`\n\n"
            "This UNLOCKS re-evaluation — it does not launch the fleet. "
            "Confirm to re-run with the new fact cited."
        )
        existing = session.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.dedup_key == dedup,
                ActionProposal.status == "open",
            )
        ).scalar_one_or_none()
        payload = {
            "verdict_id": ft.verdict_id,
            "subject": ft.subject,
            "trigger": ft.trigger,
            "evidence": ft.evidence,
            "kind": "verdict_revisit_unlocked",
        }
        if existing is not None:
            existing.summary = summary
            existing.rationale_md = rationale
            existing.suggested_payload = json.dumps(payload)
            existing.surfaced_at = now
            existing.expires_at = now + timedelta(days=30)
            session.flush()
            ids.append(existing.id)
            continue
        row = ActionProposal(
            user_id=user_id,
            summary=summary,
            rationale_md=rationale,
            suggested_payload=json.dumps(payload),
            severity="info",
            surfaced_at=now,
            expires_at=now + timedelta(days=30),
            status="open",
            kind="note_only",
            dedup_key=dedup,
            execution_state="proposed",
        )
        session.add(row)
        session.flush()
        ids.append(row.id)
    return ids


# ---------------------------------------------------------------------------
# Structural BUY gates (Item B §3.B items 3-4)
# ---------------------------------------------------------------------------

# Plan sleeves a discovery/tactical BUY may name. "high-potential-ADJACENT"
# (run-166 NOW failure class) is intentionally ABSENT.
KNOWN_BUY_SLEEVES = frozenset({
    "core",
    "international",
    "exus",
    "bonds",
    "cash",
    "gold",
    "alpha",
    "alpha_lane",
    "x10",
    "x10_moonshot",
    "high_growth",
    "high-growth",
    "moonshot",
    "discovery",
    "dry_powder",
})


@dataclass(frozen=True)
class SleeveFitResult:
    ok: bool
    sleeve: str | None
    reason: str = ""


@dataclass(frozen=True)
class BuyStructuralGateResult:
    """Outcome of the additive BUY sleeve/valuation structural gate.

    ``block`` is True only when enforcement is active for that check
    (caller supplied the field, or ``enforce=True``). When soft, failures
    land in ``warnings`` for calibration logging — never hard-block.
    """

    block: bool
    blocked_by: str | None = None
    reason: str = ""
    warnings: tuple[str, ...] = ()


def evaluate_buy_structural_gates(
    *,
    action: str,
    subject: str | None,
    named_sleeve: str | None,
    live_valuation: dict[str, Any] | None,
    enforce: bool = False,
) -> BuyStructuralGateResult:
    """Additive sleeve-fit + blind-valuation gate for BUY/ADD.

    Hard-blocks only when ``enforce`` is True OR the caller supplied the
    corresponding field. Otherwise returns ``block=False`` with
    ``warnings`` describing what would have blocked (calibration path
    while production callers still omit ``funnel_meta``).
    """
    act = (action or "").strip().upper()
    if act not in ("BUY", "ADD"):
        return BuyStructuralGateResult(block=False, reason="not_a_buy")

    sleeve_supplied = bool((named_sleeve or "").strip())
    val = live_valuation if isinstance(live_valuation, dict) else {}
    val_supplied = bool(val)

    sleeve = check_sleeve_fit(
        action=act, named_sleeve=named_sleeve, subject=subject,
    )
    valuation = require_blind_valuation_rederivation(
        action=act, live_inputs=val,
    )

    warnings: list[str] = []

    if not sleeve.ok:
        if enforce or sleeve_supplied:
            return BuyStructuralGateResult(
                block=True,
                blocked_by="sleeve_fit_invalid",
                reason=sleeve.reason,
            )
        warnings.append(f"would_block:sleeve_fit_invalid:{sleeve.reason}")

    if not valuation.ok:
        if enforce or val_supplied:
            return BuyStructuralGateResult(
                block=True,
                blocked_by="valuation_rederivation_failed",
                reason=valuation.reason,
                warnings=tuple(warnings),
            )
        warnings.append(
            f"would_block:valuation_rederivation_failed:{valuation.reason}"
        )

    return BuyStructuralGateResult(
        block=False,
        reason="ok" if not warnings else "soft_pass",
        warnings=tuple(warnings),
    )


def check_sleeve_fit(
    *,
    action: str,
    named_sleeve: str | None,
    subject: str | None = None,
) -> SleeveFitResult:
    """Deterministic sleeve-fit: a BUY must name a hosting plan sleeve.

    Run-166 NOW failure class: approved a "high-potential-ADJACENT" 1% slot
    that exists in no sleeve → structural BLOCK.
    """
    act = (action or "").strip().upper()
    if act not in ("BUY", "ADD"):
        return SleeveFitResult(ok=True, sleeve=named_sleeve, reason="not_a_buy")
    raw = (named_sleeve or "").strip()
    if not raw:
        return SleeveFitResult(
            ok=False,
            sleeve=None,
            reason=(
                f"BUY on {subject or '?'} failed structural sleeve-fit: "
                "no hosting plan sleeve named"
            ),
        )
    key = raw.lower().replace(" ", "_")
    # Explicit reject of the run-166 adjacent fiction.
    if "adjacent" in key or key in ("high-potential-adjacent", "high_potential_adjacent"):
        return SleeveFitResult(
            ok=False,
            sleeve=raw,
            reason=(
                f"BUY on {subject or '?'} failed structural sleeve-fit: "
                f"'{raw}' is not a plan sleeve (run-166 NOW class)"
            ),
        )
    if key not in KNOWN_BUY_SLEEVES and raw.lower() not in KNOWN_BUY_SLEEVES:
        return SleeveFitResult(
            ok=False,
            sleeve=raw,
            reason=(
                f"BUY on {subject or '?'} failed structural sleeve-fit: "
                f"'{raw}' is not a known plan sleeve"
            ),
        )
    return SleeveFitResult(ok=True, sleeve=raw, reason="ok")


@dataclass(frozen=True)
class ValuationRederivationResult:
    ok: bool
    reason: str = ""
    derived: dict[str, Any] = field(default_factory=dict)


def require_blind_valuation_rederivation(
    *,
    action: str,
    live_inputs: dict[str, Any] | None,
    stated_fair_value: float | None = None,
) -> ValuationRederivationResult:
    """BUY runs must carry blind-rederived live valuation inputs.

    Structural floor only: presence of live price + at least one valuation
    anchor (fair_value / pe / fcf). Does not judge whether the BUY is wise.
    """
    act = (action or "").strip().upper()
    if act not in ("BUY", "ADD"):
        return ValuationRederivationResult(ok=True, reason="not_a_buy")
    inputs = dict(live_inputs or {})
    price = inputs.get("price") or inputs.get("last_price") or inputs.get("px")
    if price is None:
        return ValuationRederivationResult(
            ok=False,
            reason="BUY blocked: blind valuation re-derivation missing live price",
        )
    anchor_keys = ("fair_value", "pe", "fcf", "fcf_ttm", "eps", "book_value")
    if not any(inputs.get(k) is not None for k in anchor_keys):
        if stated_fair_value is None:
            return ValuationRederivationResult(
                ok=False,
                reason=(
                    "BUY blocked: blind valuation re-derivation missing "
                    "fair_value/pe/fcf (or equivalent) from live sources"
                ),
            )
    derived = {
        "price": float(price),
        "fair_value": (
            float(stated_fair_value)
            if stated_fair_value is not None
            else (float(inputs["fair_value"]) if inputs.get("fair_value") is not None else None)
        ),
    }
    for k in anchor_keys:
        if inputs.get(k) is not None and k not in derived:
            try:
                derived[k] = float(inputs[k])
            except (TypeError, ValueError):
                derived[k] = inputs[k]
    return ValuationRederivationResult(ok=True, reason="ok", derived=derived)


# ---------------------------------------------------------------------------
# Provenance projection (UX §7 item 1 — additive DTO fields)
# ---------------------------------------------------------------------------

FalsifierState = Literal["armed", "fired", "none_recorded"]


@dataclass(frozen=True)
class VerdictProvenance:
    """Wire-safe provenance block for judgment surfaces.

    ``falsifier_state``:
      * ``armed`` — falsifiers recorded; none have unlocked a revisit
      * ``fired`` — an open unlock inbox row exists for this subject
      * ``none_recorded`` — no falsifiers on the standing verdict (WARNING)
    """

    falsifier_state: FalsifierState
    falsifiers: tuple[str, ...] = ()
    next_validation: str | None = None
    last_fleet_check_at: str | None = None
    verdict_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "falsifier_state": self.falsifier_state,
            "falsifiers": list(self.falsifiers),
            "next_validation": self.next_validation,
            "last_fleet_check_at": self.last_fleet_check_at,
            "verdict_id": self.verdict_id,
        }


_NONE_PROVENANCE = VerdictProvenance(falsifier_state="none_recorded")


def _iso_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _max_iso(*candidates: str | None) -> str | None:
    present = [c for c in candidates if c]
    return max(present) if present else None


def provenance_for_subjects(
    session: Session,
    *,
    user_id: str,
    subjects: list[str] | set[str] | tuple[str, ...],
) -> dict[str, VerdictProvenance]:
    """Batch provenance for judgment surfaces (positions / inbox / watch).

    Always returns an entry per requested subject. Missing registry rows
    still emit ``falsifier_state="none_recorded"`` so the UI never blanks
    the warning — acceptance: "none-recorded is a visible WARNING, not blank."
    """
    wanted = sorted({_norm_subject(s) for s in subjects if _norm_subject(s)})
    if not wanted:
        return {}

    verdicts = list(
        session.execute(
            select(Verdict).where(
                Verdict.user_id == user_id,
                Verdict.subject.in_(wanted),
                Verdict.settled.is_(True),
            )
        ).scalars()
    )
    by_subject: dict[str, Verdict] = {}
    for row in verdicts:
        prior = by_subject.get(row.subject)
        if prior is None or (row.id or 0) > (prior.id or 0):
            by_subject[row.subject] = row

    run_ids = {
        v.source_decision_run_id
        for v in by_subject.values()
        if v.source_decision_run_id is not None
    }
    finished_by_run: dict[int, datetime] = {}
    if run_ids:
        for run in session.execute(
            select(DecisionRun).where(DecisionRun.id.in_(run_ids))
        ).scalars():
            if run.finished_at is not None:
                finished_by_run[run.id] = run.finished_at

    # Open unlock rows → falsifier_state=fired
    fired_subjects: set[str] = set()
    unlock_rows = list(
        session.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.status == "open",
                ActionProposal.dedup_key.like(f"{UNLOCK_DEDUP_PREFIX}:%"),
            )
        ).scalars()
    )
    for row in unlock_rows:
        key = row.dedup_key or ""
        # verdict_revisit_unlocked:{SUBJECT}:{verdict_id}
        parts = key.split(":")
        if len(parts) >= 2:
            subj = _norm_subject(parts[1])
            if subj in wanted:
                fired_subjects.add(subj)

    # Fallback last-check: latest holding_reviews.reviewed_at per symbol
    review_at: dict[str, datetime] = {}
    for sym, reviewed_at in session.execute(
        select(HoldingReview.symbol, sa_func.max(HoldingReview.reviewed_at)).where(
            HoldingReview.user_id == user_id,
            HoldingReview.symbol.in_(wanted),
        ).group_by(HoldingReview.symbol)
    ).all():
        if reviewed_at is not None:
            review_at[_norm_subject(str(sym))] = reviewed_at

    # Fallback: stance falsifiers_json (usually NULL until registry backfills)
    stance_falsifiers: dict[str, list[str]] = {}
    stance_built: dict[str, datetime] = {}
    for stance in session.execute(
        select(PositionStance).where(
            PositionStance.user_id == user_id,
            PositionStance.symbol.in_(wanted),
        )
    ).scalars():
        sym = _norm_subject(stance.symbol)
        stance_falsifiers[sym] = [
            str(x) for x in _loads_list(stance.falsifiers_json) if str(x).strip()
        ]
        if stance.built_at is not None:
            stance_built[sym] = stance.built_at

    # Fallback: latest proposal → decision_run.finished_at for the ticker
    proposal_check: dict[str, datetime] = {}
    proposal_rows = list(
        session.execute(
            select(Proposal).where(
                Proposal.user_id == user_id,
                Proposal.ticker.in_(wanted),
                Proposal.decision_run_id.is_not(None),
            )
        ).scalars()
    )
    prop_run_ids = {
        p.decision_run_id for p in proposal_rows if p.decision_run_id is not None
    }
    prop_finished: dict[int, datetime] = {}
    if prop_run_ids:
        for run in session.execute(
            select(DecisionRun).where(DecisionRun.id.in_(prop_run_ids))
        ).scalars():
            if run.finished_at is not None:
                prop_finished[run.id] = run.finished_at
    for p in proposal_rows:
        sym = _norm_subject(p.ticker)
        fin = prop_finished.get(p.decision_run_id) if p.decision_run_id else None
        if fin is None:
            continue
        prior = proposal_check.get(sym)
        if prior is None or fin > prior:
            proposal_check[sym] = fin

    out: dict[str, VerdictProvenance] = {}
    for subj in wanted:
        row = by_subject.get(subj)
        falsifiers: list[str] = []
        next_val: str | None = None
        verdict_id: int | None = None
        last_check: str | None = None

        if row is not None:
            falsifiers = [
                str(x) for x in _loads_list(row.falsifiers_json) if str(x).strip()
            ]
            next_val = _iso_date(row.next_validation)
            verdict_id = row.id
            if row.source_decision_run_id is not None:
                last_check = _iso_dt(finished_by_run.get(row.source_decision_run_id))
            if last_check is None:
                last_check = _iso_dt(row.updated_at) or _iso_dt(row.created_at)
        elif stance_falsifiers.get(subj):
            falsifiers = list(stance_falsifiers[subj])

        last_check = _max_iso(
            last_check,
            _iso_dt(review_at.get(subj)),
            _iso_dt(proposal_check.get(subj)),
            _iso_dt(stance_built.get(subj)),
        )

        if not falsifiers:
            state: FalsifierState = "none_recorded"
        elif subj in fired_subjects:
            state = "fired"
        else:
            state = "armed"

        out[subj] = VerdictProvenance(
            falsifier_state=state,
            falsifiers=tuple(falsifiers),
            next_validation=next_val,
            last_fleet_check_at=last_check,
            verdict_id=verdict_id,
        )
    return out


def provenance_for_subject(
    session: Session, *, user_id: str, subject: str
) -> VerdictProvenance:
    """Single-subject convenience wrapper around :func:`provenance_for_subjects`."""
    subj = _norm_subject(subject)
    if not subj:
        return _NONE_PROVENANCE
    return provenance_for_subjects(
        session, user_id=user_id, subjects=[subj]
    ).get(subj, _NONE_PROVENANCE)

