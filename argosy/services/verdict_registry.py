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

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import ActionProposal, Verdict

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
) -> Verdict:
    """Insert a verdict row; if ``settled``, supersede any prior settled row.

    Typed triggers are validated (unknown kinds raise). Idempotent on the
    same run id: a second write for the same ``source_decision_run_id`` on
    the same subject refreshes the standing row in place.
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
    return row


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
