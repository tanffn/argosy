"""Deterministic revisit-trigger EVALUATOR + firing (escalates to a re-verdict).

Gap this closes: the fleet AUTHORS typed ``revisit_triggers`` alongside every
settled verdict (``argosy.agents.trader.RevisitTrigger`` →
``verdict_registry.write_verdict``), and ``verdict_registry.evaluate_triggers``
already sweeps them cheaply — but the ONLY consumer today is the daily
``verdict_trigger_daily`` loop, which merely writes a needs-confirm *unlock*
inbox row. Nothing ever ESCALATES a tripped trigger to a fresh re-verdict.

This module adds exactly that, mirroring ``thesis_monitor``'s pure-seam style:

  1. :func:`evaluate_standing_verdict_triggers` — a CHEAP, deterministic,
     side-effect-free sweep. For every STANDING verdict it classifies each typed
     trigger as ``tripped`` / ``not_tripped`` / ``unevaluable`` WITHOUT an LLM.
       * ``dated_event``    — tripped when ``now.date() >= date``.
       * ``price_below``    — tripped when ``quote_fn(subject) <= price``.
       * ``price_above``    — tripped when ``quote_fn(subject) >= price``.
       * ``metric_condition`` — evaluated against ``macro_fn(subject, metric)``;
         when the metric feed is absent the trigger is UNEVALUABLE (recorded
         honestly), NEVER silently treated as tripped or not-tripped.
     ``quote_fn`` / ``macro_fn`` are injected seams — a missing seam (or a seam
     that returns ``None``) makes the relevant trigger UNEVALUABLE, so the pure
     evaluator never touches the network by itself.

  2. :func:`fire_tripped_triggers` — for each verdict with >=1 TRIPPED trigger,
     escalate ONCE to the SAME re-evaluation path the thesis monitor uses
     (``run_deep_decision`` — the full per-ticker fleet), citing the tripped
     trigger as the ``cited_new_facts`` that clears the pushback gate. Idempotent
     via a ``verdict_trigger_fired`` MonitorFlag keyed on the verdict id: once a
     verdict has escalated, later sweeps skip it until a NEW verdict (new id)
     supersedes it. Best-effort per subject — one symbol's failure never aborts
     the sweep.

The MonitorFlag marker is also the SPINE record ("revisit_trigger fired →
re-verdict"): the re-verdict, if it settles, is captured by the existing
``fleet_recording`` hook, so we only MARK the firing here — no double-record.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.verdict_registry import _loads_list, _norm_subject
from argosy.state.models import ActionProposal, Verdict

log = get_logger("argosy.services.verdict_triggers")

# QuoteFn: subject -> last price (or None when no quote is available).
QuoteFn = Callable[[str], float | None]
# MacroFn: (subject, metric) -> metric value (or None when the feed is absent).
MacroFn = Callable[[str, str], float | None]

# Idempotency + spine-record marker: a ``note_only`` ActionProposal keyed on the
# verdict id (a NEW verdict = new id = escalatable again). This row IS the ledger
# record "revisit_trigger fired -> re-verdict"; ``monitor_flags.kind`` has a
# closed CHECK enum, so we reuse the action ledger (no migration, head stays
# 0100) — the same table + kind the unlock path already writes.
FIRED_MARKER_KIND = "note_only"
FIRED_DEDUP_PREFIX = "verdict_trigger_fired"
_FIRED_TTL_DAYS = 365  # standing until the verdict is superseded

TriggerStatus = Literal["tripped", "not_tripped", "unevaluable"]


# ---------------------------------------------------------------------------
# Result records (deterministic, JSON-friendly)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TriggerEval:
    """One classified trigger."""

    trigger: dict[str, Any]
    status: TriggerStatus
    evidence: str = ""
    reason: str = ""  # populated for ``unevaluable``

    @property
    def kind(self) -> str:
        return str(self.trigger.get("kind") or "")


@dataclass(frozen=True)
class VerdictTriggerResult:
    """Per-verdict sweep outcome. ``checked`` counts every typed trigger seen."""

    subject: str
    verdict_id: int
    checked: int
    tripped: list[TriggerEval] = field(default_factory=list)
    unevaluable: list[TriggerEval] = field(default_factory=list)

    @property
    def has_trip(self) -> bool:
        return bool(self.tripped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "verdict_id": self.verdict_id,
            "checked": self.checked,
            "tripped": [t.trigger for t in self.tripped],
            "unevaluable": [t.trigger for t in self.unevaluable],
        }


@dataclass(frozen=True)
class FiredEscalation:
    """Record of one escalation the firing step performed (or skipped)."""

    subject: str
    verdict_id: int
    triggers: list[dict[str, Any]]
    reason: str
    escalated: bool
    outcome: Any = None  # decide_fn return value (best-effort)
    error: str | None = None


# ---------------------------------------------------------------------------
# 1) Deterministic evaluator — READ + CLASSIFY only, no side effects.
# ---------------------------------------------------------------------------
def _eval_price(
    trigger: dict[str, Any], *, subject: str, quote_fn: QuoteFn | None, below: bool
) -> TriggerEval:
    thr = trigger.get("price")
    if thr is None:
        return TriggerEval(trigger, "unevaluable", reason="trigger missing price")
    if quote_fn is None:
        return TriggerEval(trigger, "unevaluable", reason="no quote feed injected")
    try:
        px = quote_fn(subject)
    except Exception as exc:  # noqa: BLE001 — a feed error is UNEVALUABLE, not false
        return TriggerEval(trigger, "unevaluable", reason=f"quote error: {exc}")
    if px is None:
        return TriggerEval(trigger, "unevaluable", reason="no quote available")
    # A non-numeric stored quote OR threshold is UNEVALUABLE — never a raise
    # (which would abort the whole sweep) and never a wrong trip. The registry
    # validates only ``kind``, so the evaluator must not trust price/value.
    try:
        px_f = float(px)
        thr_f = float(thr)
    except (TypeError, ValueError):
        return TriggerEval(
            trigger, "unevaluable",
            reason=f"non-numeric quote/threshold (px={px!r}, price={thr!r})",
        )
    hit = px_f <= thr_f if below else px_f >= thr_f
    if not hit:
        return TriggerEval(trigger, "not_tripped")
    rel = "below" if below else "above"
    kind = "price_below" if below else "price_above"
    op = "<=" if below else ">="
    evidence = (
        f"{subject} price ${px_f:g} {rel} ${thr_f:g} "
        f"({kind}:{thr} tripped, {px_f:g} {op} {thr_f:g})"
    )
    return TriggerEval(trigger, "tripped", evidence=evidence)


def _eval_dated(trigger: dict[str, Any], *, subject: str, today: date) -> TriggerEval:
    d_raw = trigger.get("date")
    if not d_raw:
        return TriggerEval(trigger, "unevaluable", reason="trigger missing date")
    # STRICT parse: no first-10-char slicing (that silently accepts
    # "2026-08-10garbage" and would falsely trip). Reject trailing garbage —
    # accept a bare ISO date or a full ISO datetime, nothing else.
    raw = str(d_raw).strip()
    d: date | None = None
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        try:
            d = datetime.fromisoformat(raw).date()
        except ValueError:
            d = None
    if d is None:
        return TriggerEval(
            trigger, "unevaluable", reason=f"unparseable date {d_raw!r}"
        )
    if today < d:
        return TriggerEval(trigger, "not_tripped")
    label = trigger.get("label") or trigger.get("event") or d.isoformat()
    evidence = (
        f"{subject} dated_event '{label}' due {d.isoformat()} "
        f"(today={today.isoformat()})"
    )
    return TriggerEval(trigger, "tripped", evidence=evidence)


def _eval_metric(
    trigger: dict[str, Any], *, subject: str, macro_fn: MacroFn | None
) -> TriggerEval:
    metric = str(trigger.get("metric") or "").strip()
    thr = trigger.get("value")
    op = str(trigger.get("op") or ">=")
    if not metric or thr is None:
        return TriggerEval(
            trigger, "unevaluable", reason="trigger missing metric/value"
        )
    if macro_fn is None:
        return TriggerEval(
            trigger, "unevaluable", reason="no metric/macro feed injected"
        )
    try:
        val = macro_fn(subject, metric)
    except Exception as exc:  # noqa: BLE001 — feed error is UNEVALUABLE
        return TriggerEval(trigger, "unevaluable", reason=f"metric error: {exc}")
    if val is None:
        # Honest: the required metric has no feed -> UNEVALUABLE, never false.
        return TriggerEval(
            trigger, "unevaluable", reason=f"metric {metric!r} not available"
        )
    # Non-numeric stored metric value / threshold -> UNEVALUABLE, never a raise.
    try:
        val_f = float(val)
        thr_f = float(thr)
    except (TypeError, ValueError):
        return TriggerEval(
            trigger, "unevaluable",
            reason=f"non-numeric metric/threshold (value={val!r}, threshold={thr!r})",
        )
    # An UNKNOWN operator is UNEVALUABLE — it must NEVER fall through to
    # equality (op='INVALID' with equal operands would falsely trip).
    if op in (">=", "gte", "gt_eq"):
        hit = val_f >= thr_f
    elif op in (">", "gt"):
        hit = val_f > thr_f
    elif op in ("<=", "lte", "lt_eq"):
        hit = val_f <= thr_f
    elif op in ("<", "lt"):
        hit = val_f < thr_f
    elif op in ("==", "=", "eq"):
        hit = val_f == thr_f
    else:
        return TriggerEval(
            trigger, "unevaluable", reason=f"unknown operator {op!r}"
        )
    if not hit:
        return TriggerEval(trigger, "not_tripped")
    evidence = f"{subject} metric {metric}={val_f:g} {op} {thr_f:g} (metric_condition tripped)"
    return TriggerEval(trigger, "tripped", evidence=evidence)


def _classify(
    trigger: dict[str, Any],
    *,
    subject: str,
    today: date,
    quote_fn: QuoteFn | None,
    macro_fn: MacroFn | None,
) -> TriggerEval:
    kind: str = str(trigger.get("kind") or "")
    if kind == "price_below":
        return _eval_price(trigger, subject=subject, quote_fn=quote_fn, below=True)
    if kind == "price_above":
        return _eval_price(trigger, subject=subject, quote_fn=quote_fn, below=False)
    if kind == "dated_event":
        return _eval_dated(trigger, subject=subject, today=today)
    if kind == "metric_condition":
        return _eval_metric(trigger, subject=subject, macro_fn=macro_fn)
    return TriggerEval(trigger, "unevaluable", reason=f"unknown trigger kind {kind!r}")


def evaluate_standing_verdict_triggers(
    session: Session,
    user_id: str,
    *,
    now: datetime,
    quote_fn: QuoteFn | None = None,
    macro_fn: MacroFn | None = None,
) -> list[VerdictTriggerResult]:
    """Cheap deterministic sweep over STANDING verdicts. READ-ONLY.

    For each settled verdict, classify every typed ``revisit_trigger`` as
    tripped / not_tripped / unevaluable. Returns one :class:`VerdictTriggerResult`
    per verdict (including verdicts with zero triggers → ``checked=0``, empty
    lists). Deterministic + side-effect free: it only reads rows and classifies.
    """
    # Honor the documented ``now.date() >= trigger-date`` contract: use the
    # caller's reference date as-is. A UTC re-projection would make
    # 2026-08-10 00:30+03:00 evaluate as Aug 9 and miss an Aug-10 event.
    today = now.date()
    rows = session.execute(
        select(Verdict).where(
            Verdict.user_id == user_id,
            Verdict.settled.is_(True),
        )
    ).scalars().all()

    results: list[VerdictTriggerResult] = []
    for row in rows:
        subject = row.subject
        triggers = [t for t in _loads_list(row.revisit_triggers_json) if isinstance(t, dict)]
        tripped: list[TriggerEval] = []
        unevaluable: list[TriggerEval] = []
        for trig in triggers:
            ev = _classify(
                trig, subject=subject, today=today, quote_fn=quote_fn, macro_fn=macro_fn
            )
            if ev.status == "tripped":
                tripped.append(ev)
            elif ev.status == "unevaluable":
                unevaluable.append(ev)
        results.append(
            VerdictTriggerResult(
                subject=subject,
                verdict_id=int(row.id),
                checked=len(triggers),
                tripped=tripped,
                unevaluable=unevaluable,
            )
        )
    return results


# ---------------------------------------------------------------------------
# 2) Firing — escalate a tripped verdict to a full re-verdict (idempotent).
# ---------------------------------------------------------------------------
def _fired_dedup_key(subject: str, verdict_id: int) -> str:
    return f"{FIRED_DEDUP_PREFIX}:{_norm_subject(subject)}:{verdict_id}"


def _already_fired(
    session: Session, *, user_id: str, subject: str, verdict_id: int
) -> bool:
    """A HELD claim / completed fire for this exact verdict id already exists ->
    this verdict already escalated (idempotent until a new verdict, with a new
    id, supersedes it). A RELEASED claim (deleted, or the ``rejected`` expiry
    fallback) is retry-eligible, so only live ``open`` markers count here."""
    dedup = _fired_dedup_key(subject, verdict_id)
    existing = session.execute(
        select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == dedup,
            ActionProposal.status == "open",
        ).limit(1)
    ).scalar_one_or_none()
    return existing is not None


# Completion classification (defect #1): ``run_deep_decision`` NEVER raises — an
# analyst/flow/quorum failure returns a structured non-completing outcome. Only a
# genuine settle keeps the fired marker; a non-completing outcome releases the
# lease so the subject retries next sweep.
_NONCOMPLETING_STATUS = frozenset({"error", "quorum_failed"})
_NONCOMPLETING_BLOCKED_BY = frozenset(
    {"open_error", "analysts_error", "flow_error"}
)


def _outcome_field(outcome: Any, field_name: str) -> Any:
    if outcome is None:
        return None
    if isinstance(outcome, dict):
        return outcome.get(field_name)
    return getattr(outcome, field_name, None)


def _is_completing(outcome: Any) -> bool:
    """True when ``decide_fn`` ran the fleet to a genuine re-verdict (approved,
    or blocked-by-a-real-decision). False for infra failures — status in
    {error, quorum_failed} or a ``blocked`` whose ``blocked_by`` is an infra
    error (open_error / analysts_error / flow_error). An outcome with no
    recognizable ``status`` (e.g. a test seam returning a plain marker) is
    treated as completing — the seam ran and returned."""
    status = str(_outcome_field(outcome, "status") or "").strip()
    if status in _NONCOMPLETING_STATUS:
        return False
    if status == "blocked":
        blocked_by = str(_outcome_field(outcome, "blocked_by") or "").strip()
        if blocked_by in _NONCOMPLETING_BLOCKED_BY:
            return False
    return True


def _claim_marker(
    session: Session,
    *,
    user_id: str,
    subject: str,
    verdict_id: int,
    triggers: list[dict[str, Any]],
    reason: str,
    now: datetime,
) -> ActionProposal | None:
    """ATOMIC CLAIM (defect #2): INSERT the dedup marker FIRST, before firing.

    The partial-unique(dedup_key, status='open') index arbitrates concurrency —
    exactly one concurrent sweep wins. The loser's INSERT raises IntegrityError;
    we roll back to the SAVEPOINT (session stays usable) and return ``None`` so
    the caller skips this subject this sweep. The claim starts life-cycled as
    ``firing`` (in payload) — :func:`_finalize_claim` marks it ``fired`` on a
    completing outcome; :func:`_release_claim` deletes it on failure."""
    payload = {
        "subject": _norm_subject(subject),
        "verdict_id": verdict_id,
        "triggers": triggers,
        "reason": reason,
        "kind": "verdict_trigger_fired",
        "state": "firing",
        "source": "verdict_trigger_sweep",
    }
    row = ActionProposal(
        user_id=user_id,
        summary=f"revisit trigger fired -> re-verdict: {_norm_subject(subject)}",
        rationale_md=(
            f"Deterministic verdict-trigger sweep fired for settled verdict "
            f"#{verdict_id} on {_norm_subject(subject)} and escalated a full "
            f"re-verdict.\n\n**Reason:** {reason}\n\n"
            f"**Triggers:** `{json.dumps(triggers)}`"
        ),
        suggested_payload=json.dumps(payload, ensure_ascii=False, default=str),
        severity="info",
        surfaced_at=now,
        expires_at=now + timedelta(days=_FIRED_TTL_DAYS),
        status="open",
        kind=FIRED_MARKER_KIND,
        dedup_key=_fired_dedup_key(subject, verdict_id),
        execution_state="proposed",
    )
    try:
        with session.begin_nested():  # SAVEPOINT — a collision never poisons the session
            session.add(row)
            session.flush()
    except IntegrityError:
        # Lost the race: another concurrent sweep already holds the claim.
        return None
    return row


def _finalize_claim(row: ActionProposal) -> None:
    """A completing re-verdict: keep the claim, flip its lifecycle to ``fired``."""
    try:
        payload = json.loads(row.suggested_payload or "{}")
    except (TypeError, ValueError):
        payload = {}
    payload["state"] = "fired"
    row.suggested_payload = json.dumps(payload, ensure_ascii=False, default=str)


def _release_claim(session: Session, row: ActionProposal) -> None:
    """A non-completing outcome / exception: DELETE the claim so the dedup key is
    freed and the subject RETRIES next sweep (the lease is released)."""
    try:
        session.delete(row)
        session.flush()
    except Exception:  # noqa: BLE001 — release is best-effort; expire as fallback
        try:
            row.status = "rejected"
            row.expires_at = row.surfaced_at
        except Exception:  # noqa: BLE001
            pass


def _cited_fact_for(ev: TriggerEval, *, subject: str) -> str:
    """A cited-new-fact string that hits ``verdict_registry._fact_hits_trigger``
    for the tripped trigger, so ``run_deep_decision``'s pushback gate ALLOWS the
    re-run (a tripped typed trigger IS, by construction, the new fact)."""
    trig = ev.trigger
    kind = str(trig.get("kind") or "")
    if kind == "price_below":
        thr = trig.get("price")
        return (
            f"{subject} is now trading at or below ${thr} — recorded revisit "
            f"trigger price_below:{thr} has tripped. {ev.evidence}"
        )
    if kind == "price_above":
        thr = trig.get("price")
        return (
            f"{subject} is now trading at or above ${thr} — recorded revisit "
            f"trigger price_above:{thr} has tripped. {ev.evidence}"
        )
    if kind == "metric_condition":
        metric = str(trig.get("metric") or "")
        label = str(trig.get("label") or "")
        return (
            f"{subject} metric_condition tripped: {metric} {label}. {ev.evidence}"
        )
    # dated_event — evidence carries both the label and the ISO date.
    return ev.evidence or f"{subject} dated_event trigger tripped. {trig.get('date')}"


def _default_decide_fn(
    *, user_id: str, subject: str, cited_new_facts: list[str], reason: str
) -> Any:
    """Default escalation seam: run the SAME full per-ticker fleet the thesis
    monitor / consult path uses, threading the tripped trigger as the cited new
    fact so the pushback gate clears. Async → driven with ``asyncio.run`` inside
    the (already off-thread) sweep worker."""
    import asyncio

    from argosy.decisions.tiers import Tier
    from argosy.services.decision_funnel.deep_decision import run_deep_decision

    funnel_meta = {
        "source": "verdict_trigger_sweep",
        "cited_new_facts": cited_new_facts,
        "revisit_reason": reason,
    }
    return asyncio.run(
        run_deep_decision(
            user_id=user_id,
            ticker=subject,
            tier=Tier.T2,
            consult_mode="long_hold",
            funnel_meta=funnel_meta,
            subject_type="holding",
        )
    )


def fire_tripped_triggers(
    session: Session,
    user_id: str,
    results: list[VerdictTriggerResult],
    *,
    now: datetime,
    decide_fn: Callable[..., Any] | None = None,
) -> list[FiredEscalation]:
    """Escalate each verdict with >=1 tripped trigger to a full re-verdict.

    Idempotent: a ``verdict_trigger_fired`` marker keyed on the verdict id
    guarantees at most one escalation per standing verdict (re-fires only once a
    new verdict supersedes it). Best-effort per subject — one subject's failure
    is captured on its :class:`FiredEscalation` and never aborts the sweep.

    ``decide_fn`` is the injectable firing seam (defaults to
    :func:`_default_decide_fn` → ``run_deep_decision``). It is called with
    ``user_id`` / ``subject`` / ``cited_new_facts`` / ``reason`` keyword args.
    """
    decide = decide_fn or _default_decide_fn
    fired: list[FiredEscalation] = []
    for res in results:
        if not res.has_trip:
            continue
        triggers = [t.trigger for t in res.tripped]
        reason = "; ".join(t.evidence for t in res.tripped if t.evidence) or (
            f"{res.subject}: {len(res.tripped)} revisit trigger(s) tripped"
        )

        def _skip(error: str | None) -> None:
            fired.append(
                FiredEscalation(
                    subject=res.subject,
                    verdict_id=res.verdict_id,
                    triggers=triggers,
                    reason=reason,
                    escalated=False,
                    error=error,
                )
            )

        # Cheap fast-path pre-check (the CLAIM below is the real arbiter).
        try:
            if _already_fired(
                session, user_id=user_id, subject=res.subject, verdict_id=res.verdict_id
            ):
                _skip(None)
                continue
        except Exception as exc:  # noqa: BLE001 — a pre-check hiccup never aborts the sweep
            log.warning(
                "verdict_trigger.precheck_failed",
                subject=res.subject, error=str(exc)[:160],
            )

        # STEP 1 — CLAIM (atomic; loser skips this sweep without double-firing).
        try:
            claim = _claim_marker(
                session,
                user_id=user_id,
                subject=res.subject,
                verdict_id=res.verdict_id,
                triggers=triggers,
                reason=reason,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — one subject never sinks the sweep
            log.warning(
                "verdict_trigger.claim_failed",
                subject=res.subject, verdict_id=res.verdict_id, error=str(exc)[:200],
            )
            _skip(str(exc)[:200])
            continue
        if claim is None:
            # Lost the claim race — another concurrent sweep is handling it.
            _skip(None)
            continue

        # STEP 2 — FIRE (only the claim winner pays the LLM cost).
        try:
            cited = [_cited_fact_for(t, subject=res.subject) for t in res.tripped]
            outcome = decide(
                user_id=user_id,
                subject=res.subject,
                cited_new_facts=cited,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — one subject never sinks the sweep
            # STEP 3a — RELEASE the lease so this subject retries next sweep.
            _release_claim(session, claim)
            log.warning(
                "verdict_trigger.fire_failed",
                user_id=user_id, subject=res.subject,
                verdict_id=res.verdict_id, error=str(exc)[:200],
            )
            _skip(str(exc)[:200])
            continue

        # STEP 3b — RESOLVE by inspecting the outcome (defect #1). A NON-completing
        # outcome (run_deep_decision returns status=error/quorum_failed/… — it
        # never raises) releases the lease; it is NOT a permanent fire.
        if not _is_completing(outcome):
            _release_claim(session, claim)
            status = str(_outcome_field(outcome, "status") or "?")
            log.info(
                "verdict_trigger.fire_non_completing",
                subject=res.subject, verdict_id=res.verdict_id, status=status,
            )
            _skip(f"non_completing:{status}")
            continue

        # Completing re-verdict — keep the claim (mark it fired).
        _finalize_claim(claim)
        log.info(
            "verdict_trigger.fired",
            user_id=user_id, subject=res.subject, verdict_id=res.verdict_id,
            tripped=len(res.tripped), reason=reason[:300],
        )
        fired.append(
            FiredEscalation(
                subject=res.subject,
                verdict_id=res.verdict_id,
                triggers=triggers,
                reason=reason,
                escalated=True,
                outcome=outcome,
            )
        )
    return fired


def sweep_and_fire(
    session: Session,
    user_id: str,
    *,
    now: datetime,
    quote_fn: QuoteFn | None = None,
    macro_fn: MacroFn | None = None,
    decide_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Convenience: evaluate then fire, returning a job-friendly summary.

    Deterministic evaluation + best-effort escalation. Callers own the commit.
    """
    results = evaluate_standing_verdict_triggers(
        session, user_id, now=now, quote_fn=quote_fn, macro_fn=macro_fn
    )
    fired = fire_tripped_triggers(
        session, user_id, results, now=now, decide_fn=decide_fn
    )
    return {
        "verdicts": len(results),
        "tripped_verdicts": sum(1 for r in results if r.has_trip),
        "unevaluable_triggers": sum(len(r.unevaluable) for r in results),
        "escalated": sum(1 for f in fired if f.escalated),
        "skipped_already_fired": sum(
            1 for f in fired if not f.escalated and f.error is None
        ),
        "errors": [f"{f.subject}: {f.error}" for f in fired if f.error],
        "results": [r.to_dict() for r in results if r.has_trip or r.unevaluable],
    }


__all__ = [
    "QuoteFn",
    "MacroFn",
    "TriggerEval",
    "VerdictTriggerResult",
    "FiredEscalation",
    "FIRED_MARKER_KIND",
    "FIRED_DEDUP_PREFIX",
    "evaluate_standing_verdict_triggers",
    "fire_tripped_triggers",
    "sweep_and_fire",
]
