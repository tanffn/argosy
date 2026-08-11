"""Phase 2 — stance-revision routing (one voice per position).

Phase 1 lets the trader, when it disagrees with a STANDING plan SELL/TRIM
stance, write ``PROPOSED STANCE REVISION:`` + a new-facts justification into
``TraderProposal.rationale_summary`` while STILL emitting ``action="hold"``.
Nothing consumed that label, so the settled verdict stayed a bare HOLD and
silently contradicted the standing SELL.

Ariel's ruling + BINDING escalation rule: ONE VOICE PER POSITION — the fleet may
PROPOSE a revision, a blind gate FILTERS the junk, but reversing a standing
SELL/TRIM on the core is a "sell-vs-hold the core" PATH decision that is ARIEL'S
to make. It is NEVER auto-applied. So Phase 2 SURFACES a worthy revision for a
human decision; it does not move money.

This module is the consumer:

  1. DETECT the label + the new-facts text on the trader rationale, and confirm a
     STANDING SELL/TRIM stance exists for the ticker (``get_stances``).
  2. FILTER — the revision is only worth surfacing if it clears ALL of:
     (a) non-trivial cited facts (not empty / whitespace / punctuation);
     (b) a POSITIVE committed-tripwire hit — the cited facts actually match a
         recorded falsifier / revisit-trigger on the standing SETTLED verdict
         (``verdict_registry.check_pushback_gate`` reason is a hit, NOT merely
         ``allowed`` — the gate allows by default when there is no settled
         verdict, which is no factual basis to overturn a reduction);
     (c) an INDEPENDENT blind re-derivation over the SAME research bundle
         (``stock_decision`` ``decide`` — injectable, NOT shown the trader's
         argument) CONCURS with a keep verdict (HOLD/BUY), not a reduction.
  3. PASS → write a ``revision_proposed`` HoldingReview overlay carrying the new
     facts + the blind concurrence. ``rebuild_stances`` surfaces it as
     ``divergence=True`` with a "approve to move it" note. THE STANCE STAYS the
     plan SELL/TRIM — it only ever moves off the reduction when Ariel APPROVES
     (via the existing proposal overlay: proposal > plan). Phase 2 does NOT
     implement the approval/move; it only surfaces.
  4. FAIL (any filter miss, or any error) → ``revision_rejected`` overlay:
     divergence flagged, standing SELL/TRIM stands, nothing hidden.

FAIL-CLOSED throughout: ANY error / a blind-call exception → REJECTED, the
standing stance is preserved, no exception escapes into the settle path. This
module NEVER writes ``position_stances`` directly (it is a rebuildable
projection); it writes only a HoldingReview overlay and lets ``rebuild_stances``
re-derive. NEITHER outcome auto-moves the stance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from argosy.agents.stock_decision import StockDecisionOutput, decide_stock
from argosy.logging import get_logger
from argosy.services.position_stance import get_stances
from argosy.services.stock_decision.service import (
    record_holding_review,
    research_bundle,
)
from argosy.services.verdict_registry import check_pushback_gate

log = get_logger(__name__)

# The label the trader writes (Phase 1) when it wants to revise a standing stance.
_REVISION_MARKER = "PROPOSED STANCE REVISION:"

# Standing stances a revision may contest (a reduction the fleet wants to stop).
_REDUCE_STANCES = frozenset({"SELL", "TRIM"})

# The blind independent verdicts that CONCUR the reduction should STOP. A
# re-derived SELL/TRIM means the independent pass ALSO wants to reduce → the
# revision is not worth surfacing. ABSTAIN (empty evidence) is NOT a concurrence.
_KEEP_VERDICTS = frozenset({"HOLD", "BUY"})

# The ONLY ``check_pushback_gate`` reasons that count as a POSITIVE tripwire hit
# — a cited new fact actually matched a recorded falsifier/revisit-trigger on the
# STANDING SETTLED VERDICT. ``allowed=True`` with reason ``no_settled_verdict``
# (the gate's default when there is nothing to defend) is NOT a hit: the standing
# SELL comes from the PLAN STANCE, so "no settled verdict to test against" means
# there is no pre-committed factual basis to surface a revision → REJECT.
_POSITIVE_GATE_REASONS = frozenset(
    {"new_fact_hits_falsifier", "new_fact_hits_trigger"}
)

# HoldingReview.outcome markers (String(32) enum, extended WITHOUT a migration —
# precedent: the 'abstained' outcome, models.py HoldingReview). NEITHER moves the
# stance: revision_proposed SURFACES the divergence for Ariel to approve;
# revision_rejected flags a divergence that failed the filter.
OUTCOME_PROPOSED = "revision_proposed"
OUTCOME_REJECTED = "revision_rejected"


def _is_trivial_facts(new_facts_text: str) -> bool:
    """True when the parsed revision cites no substantive new facts — empty, or
    only whitespace/punctuation. A label-only or empty ``PROPOSED STANCE
    REVISION:`` carries no verifiable basis → REJECT."""
    return not re.sub(r"[\s\W_]+", "", new_facts_text or "")


def _max_review_id(db: Any, user_id: str, ticker: str) -> int | None:
    """MAX(holding_reviews.id) for the symbol — a WATERMARK captured BEFORE a
    write so persistence verification can confirm THIS write landed (a row with
    id > watermark), never a stale prior revision_* row. Returns 0 for a genuine
    "no prior rows" (query succeeded), and None on a READ ERROR — a distinct
    sentinel the verifier treats as not-persisted (fail-closed), so a transient
    read failure can't let a stale `id > 0` row masquerade as this run's write."""
    try:
        from sqlalchemy import func, select

        from argosy.state.models import HoldingReview

        t = (ticker or "").strip().upper()
        got = db.execute(
            select(func.max(HoldingReview.id)).where(
                HoldingReview.user_id == user_id, HoldingReview.symbol == t
            )
        ).scalar()
        # Genuine "no prior rows" (query succeeded) → 0 (normal). A read ERROR
        # returns None below — a distinct sentinel the verifier treats as
        # not-persisted (fail-closed), never as "id > 0 accepts any stale row".
        return int(got or 0)
    except Exception as exc:  # noqa: BLE001 — watermark read must not raise
        log.warning("stance_revision.watermark_read_failed", ticker=ticker, err=str(exc)[:160])
        return None


def _review_persisted_after(
    db: Any, user_id: str, ticker: str, watermark: int | None, expected_outcome: str
) -> bool:
    """True IFF a HoldingReview with id > ``watermark`` (i.e. written by THIS
    run, not a stale prior row) carries ``expected_outcome`` for the symbol.

    Fail-closed: if ``watermark`` is None (the pre-write watermark read FAILED),
    we cannot distinguish this run's write from a stale prior row → treat as
    NOT persisted."""
    if watermark is None:
        log.error(
            "stance_revision.verify_no_watermark",
            ticker=(ticker or "").upper(),
            detail="watermark read failed pre-write; cannot confirm persistence (fail-closed)",
        )
        return False
    try:
        from sqlalchemy import select

        from argosy.state.models import HoldingReview

        t = (ticker or "").strip().upper()
        row = (
            db.execute(
                select(HoldingReview)
                .where(
                    HoldingReview.user_id == user_id,
                    HoldingReview.symbol == t,
                    HoldingReview.id > watermark,
                )
                .order_by(HoldingReview.id.desc())
            )
            .scalars()
            .first()
        )
        return row is not None and (row.outcome or "") == expected_outcome
    except Exception as exc:  # noqa: BLE001 — verification must not raise
        log.warning("stance_revision.verify_read_failed", ticker=ticker, err=str(exc)[:160])
        return False


@dataclass(frozen=True)
class StanceRevisionResult:
    """What the router did. ``routed=False`` means no label / no standing
    SELL/TRIM → behaviour unchanged (the HOLD settles as before). NEITHER
    ``surfaced`` nor a reject moves the stance — the stance only leaves the plan
    SELL/TRIM when Ariel approves via the proposal overlay."""

    routed: bool
    surfaced: bool = False
    outcome: str | None = None
    standing_stance: str | None = None
    blind_verdict: str | None = None
    reason: str = ""


def parse_stance_revision(rationale_summary: str | None) -> str | None:
    """Return the new-facts justification text after ``PROPOSED STANCE
    REVISION:``, or None when the label is absent. An empty justification
    returns "" (label present, no facts) — which the trivial-facts filter
    REJECTS (fail-closed)."""
    text = rationale_summary or ""
    idx = text.upper().find(_REVISION_MARKER)
    if idx < 0:
        return None
    return text[idx + len(_REVISION_MARKER):].strip()


def _cited_new_facts(new_facts_text: str) -> list[str]:
    """The cited-new-facts list handed to ``check_pushback_gate`` — mirrors the
    ``funnel_meta['cited_new_facts']`` pattern deep_decision uses. The whole
    blob is the primary fact (maximises substring hits against a falsifier /
    trigger label); non-empty lines are added so a multi-fact justification each
    get a shot."""
    facts = [new_facts_text.strip()] if new_facts_text.strip() else []
    for line in re.split(r"[\n;]+", new_facts_text):
        s = line.strip(" -*\t")
        if s and s not in facts:
            facts.append(s)
    return facts


def _standing_reduce_stance(db: Any, user_id: str, ticker: str) -> str | None:
    """The standing SELL/TRIM stance for ``ticker`` (same read Phase 1 uses),
    or None. Best-effort: any failure → None (no routing)."""
    t = (ticker or "").strip().upper()
    for row in get_stances(db, user_id):
        if (getattr(row, "symbol", "") or "").strip().upper() != t:
            continue
        stance = (getattr(row, "stance", "") or "").strip().upper()
        return stance if stance in _REDUCE_STANCES else None
    return None


def _revision_output(
    ticker: str,
    *,
    verdict: str,
    confidence: str,
    new_facts_text: str,
    surfaced: bool,
    standing_stance: str,
    gate_reason: str,
    blind_verdict: str | None,
) -> StockDecisionOutput:
    """Build the StockDecisionOutput handed to ``record_holding_review`` so the
    overlay carries the new facts + the surface/reject verdict for audit."""
    if surfaced:
        reason = (
            f"Fleet PROPOSES stopping the standing {standing_stance} based on new "
            f"facts; a blind independent re-review concurred (verdict "
            f"{blind_verdict}). The stance remains the plan's {standing_stance} — "
            f"approve this revision to move it. New facts: {new_facts_text}"
        )
    else:
        reason = (
            f"Fleet proposed a stance revision on the standing {standing_stance}; "
            f"blind re-review did NOT confirm the new facts "
            f"({gate_reason or 'independent pass still reduces'}), so the "
            f"standing {standing_stance} stands. New facts: {new_facts_text}"
        )
    return StockDecisionOutput(
        ticker=(ticker or "").upper(),
        verdict=verdict,
        confidence=confidence,  # type: ignore[arg-type]
        reason=reason,
        evidence=[f"proposed stance revision: {new_facts_text}"] if new_facts_text else [],
        data_gaps=[],
    )


def route_stance_revision(
    db: Any,
    *,
    user_id: str,
    ticker: str,
    rationale_summary: str | None,
    decide: Callable[..., StockDecisionOutput] = decide_stock,
    fetchers: dict[str, Callable[[str], "str | None"]] | None = None,
    record: Callable[..., None] = record_holding_review,
) -> StanceRevisionResult:
    """Detect a PROPOSED STANCE REVISION, FILTER it through the blind-review
    machinery, and SURFACE it for Ariel's approval only if worth it. NEITHER
    path moves the stance (see module docstring). FAIL-CLOSED: any error →
    REJECTED, standing stance preserved, no raise.

    ``decide`` is the injectable blind re-derivation seam (default: the live
    ``decide_stock`` LLM agent) so tests stub it without a live call.
    """
    try:
        new_facts_text = parse_stance_revision(rationale_summary)
        if new_facts_text is None:
            return StanceRevisionResult(routed=False, reason="no_revision_label")

        standing = _standing_reduce_stance(db, user_id, ticker)
        if standing is None:
            return StanceRevisionResult(routed=False, reason="no_standing_reduce_stance")

        # From here on we ROUTE. Nothing below MOVES the stance — it either
        # SURFACES a worthy revision (revision_proposed) or flags a rejected
        # divergence (revision_rejected). Either way the plan SELL/TRIM stands.

        # Substantive-facts filter: empty / label-only / punctuation-only → REJECT.
        if _is_trivial_facts(new_facts_text):
            return _reject(
                db, user_id, ticker, new_facts_text, standing,
                reason="empty_or_trivial_facts", blind_verdict=None,
                record=record, gate_reason="the revision cited no substantive new facts",
            )

        # --- Filter 1: mechanical new-facts test against the standing verdict ---
        try:
            gate = check_pushback_gate(
                db,
                user_id=user_id,
                subject=ticker,
                cited_new_facts=_cited_new_facts(new_facts_text),
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed on gate error
            log.warning("stance_revision.gate_failed", ticker=ticker, err=str(exc)[:160])
            return _reject(
                db, user_id, ticker, new_facts_text, standing,
                reason="pushback_gate_error", blind_verdict=None,
                record=record, gate_reason="pushback gate errored (fail-closed)",
            )

        if gate.reason not in _POSITIVE_GATE_REASONS:
            # Require a POSITIVE committed-tripwire hit — NOT merely
            # ``allowed=True``. ``allowed`` is True by DEFAULT when there is no
            # settled verdict to defend (reason ``no_settled_verdict``); that is
            # NOT a factual basis to surface a revision. Only an actual
            # falsifier/trigger hit on the standing settled verdict passes.
            return _reject(
                db, user_id, ticker, new_facts_text, standing,
                reason="pushback_no_tripwire_hit", blind_verdict=None,
                record=record, gate_reason=gate.reason,
            )

        # --- Filter 2: INDEPENDENT blind re-derivation over the same bundle ---
        try:
            if fetchers is None:
                from argosy.services.stock_decision.fetchers import default_fetchers

                fetchers = default_fetchers(db, user_id)
            bundle = research_bundle(ticker, fetchers=fetchers)
            redo = decide(
                ticker,
                context="independent blind re-review of a proposed stance revision",
                bundle=bundle,
                user_id=user_id,
            )
            blind_verdict = (getattr(redo, "verdict", "") or "").strip().upper()
        except Exception as exc:  # noqa: BLE001 — a blind-call failure = rejection
            log.warning("stance_revision.blind_failed", ticker=ticker, err=str(exc)[:160])
            return _reject(
                db, user_id, ticker, new_facts_text, standing,
                reason="blind_call_error", blind_verdict=None,
                record=record, gate_reason="blind re-derivation errored (fail-closed)",
            )

        if blind_verdict not in _KEEP_VERDICTS:
            # Independent pass ALSO reduces (SELL/TRIM) or is inconclusive
            # (ABSTAIN/…) → not a concurrence → not worth surfacing.
            return _reject(
                db, user_id, ticker, new_facts_text, standing,
                reason="blind_did_not_concur", blind_verdict=blind_verdict,
                record=record,
                gate_reason=f"independent blind pass returned {blind_verdict}",
            )

        # --- PASS: all filters cleared → SURFACE (do NOT move the stance) ---
        # TOCTOU: re-read the standing stance immediately before the write. If a
        # rebuild between the first read and now flipped it off SELL/TRIM, there
        # is nothing to surface — abort.
        standing_now = _standing_reduce_stance(db, user_id, ticker)
        if standing_now is None:
            log.info("stance_revision.stance_changed_before_write", ticker=(ticker or "").upper())
            return StanceRevisionResult(
                routed=True, surfaced=False, outcome=None,
                standing_stance=standing, blind_verdict=blind_verdict,
                reason="stance_changed_before_write",
            )

        watermark = _max_review_id(db, user_id, ticker)
        confidence = (getattr(redo, "confidence", "") or "MED").upper() or "MED"
        v = _revision_output(
            ticker, verdict=blind_verdict, confidence=confidence,
            new_facts_text=new_facts_text, surfaced=True,
            standing_stance=standing_now, gate_reason=gate.reason,
            blind_verdict=blind_verdict,
        )
        try:
            record(
                db, user_id, v,
                position_usd=None, elevated_by_flag=False,
                outcome=OUTCOME_PROPOSED,
            )
        except Exception as exc:  # noqa: BLE001 — a write failure must not report surfaced
            log.warning("stance_revision.proposed_write_raised", ticker=ticker, err=str(exc)[:160])

        # Fail-closed persistence verification with a WATERMARK: confirm a NEW
        # row (id > watermark) carries the marker — a swallowed commit failure or
        # a stale prior revision_* row must NOT be read as persisted.
        if not _review_persisted_after(db, user_id, ticker, watermark, OUTCOME_PROPOSED):
            log.error(
                "stance_revision.proposed_not_persisted",
                ticker=(ticker or "").upper(), standing=standing_now,
                detail="revision NOT surfaced — overlay did not persist; standing reduction preserved",
            )
            return _reject(
                db, user_id, ticker, new_facts_text, standing_now,
                reason="proposed_write_not_persisted", blind_verdict=blind_verdict,
                record=record,
                gate_reason="the revision_proposed overlay did not persist (fail-closed)",
            )

        log.info(
            "stance_revision.surfaced",
            ticker=(ticker or "").upper(), standing=standing_now, blind=blind_verdict,
        )
        return StanceRevisionResult(
            routed=True, surfaced=True, outcome=OUTCOME_PROPOSED,
            standing_stance=standing_now, blind_verdict=blind_verdict,
            reason="surfaced_for_approval",
        )
    except Exception as exc:  # noqa: BLE001 — nothing may escape into the settle path
        log.warning("stance_revision.route_failed", ticker=ticker, err=str(exc)[:200])
        return StanceRevisionResult(routed=False, reason="route_error")


def _reject(
    db: Any,
    user_id: str,
    ticker: str,
    new_facts_text: str,
    standing: str,
    *,
    reason: str,
    blind_verdict: str | None,
    record: Callable[..., None],
    gate_reason: str,
) -> StanceRevisionResult:
    """Write the visible ``revision_rejected`` overlay and keep the standing
    SELL/TRIM. The rejected review's verdict mirrors the blind verdict when we
    got one (audit), else the standing stance — either way ``rebuild_stances``
    keeps the plan stance and flags divergence."""
    # Never persist an ABSTAIN / empty verdict on the overlay: ``_latest_reviews``
    # skips ABSTAIN rows, which would silently drop the divergence flag. Fall
    # back to the standing stance for the audit verdict in that case.
    audit_verdict = (
        blind_verdict
        if blind_verdict in {"SELL", "TRIM", "HOLD", "BUY"}
        else standing
    )
    watermark = _max_review_id(db, user_id, ticker)
    v = _revision_output(
        ticker, verdict=audit_verdict, confidence="MED",
        new_facts_text=new_facts_text, surfaced=False,
        standing_stance=standing, gate_reason=gate_reason,
        blind_verdict=blind_verdict,
    )
    try:
        record(
            db, user_id, v,
            position_usd=None, elevated_by_flag=False,
            outcome=OUTCOME_REJECTED,
        )
    except Exception as exc:  # noqa: BLE001 — audit write must never abort routing
        log.warning("stance_revision.reject_write_failed", ticker=ticker, err=str(exc)[:160])
    # Confirm THIS run's divergence audit persisted (watermark — never a stale
    # prior row). The standing SELL is safe regardless (rebuild sees no
    # revision_proposed overlay), but a lost write means the visible divergence
    # flag is gone — log LOUDLY so it's not silent.
    if not _review_persisted_after(db, user_id, ticker, watermark, OUTCOME_REJECTED):
        log.error(
            "stance_revision.rejected_audit_not_persisted",
            ticker=(ticker or "").upper(), standing=standing, reason=reason,
            detail="divergence audit did not persist; standing reduction still preserved",
        )
    log.info(
        "stance_revision.rejected",
        ticker=(ticker or "").upper(), standing=standing, reason=reason,
    )
    return StanceRevisionResult(
        routed=True, surfaced=False, outcome=OUTCOME_REJECTED,
        standing_stance=standing, blind_verdict=blind_verdict, reason=reason,
    )


__all__ = [
    "route_stance_revision",
    "parse_stance_revision",
    "StanceRevisionResult",
    "OUTCOME_PROPOSED",
    "OUTCOME_REJECTED",
]
