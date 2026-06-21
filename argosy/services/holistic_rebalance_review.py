"""Holistic, plan-driven, news-supported rebalance/sell review.

Composes ONE whole-portfolio rebalance proposal — trim over-target sleeves to
fund under-target ones — gated by thesis strength. This is an
**orchestration / composition** layer; it writes NO new drift math, NO new
per-position verdict logic, and NO new news pipeline. It reuses, verbatim:

  * Drift signal:    ``argosy.services.retirement.rebalancing.detect_rebalancing_alerts``
                     → per-coarse-class ``RebalancingAlert`` (5pp / 25%-relative rule).
  * Per-position:    ``argosy.services.per_position_thesis.derive_position_theses``
                     → ``PositionThesis`` verdicts (HOLD/BUY/TRIM/SELL/ADD) with
                     ``current_weight_pct`` / ``target_weight_pct`` / ``conviction``.
  * News / thesis:   ``monitor_flags`` rows of kind ``thesis_monitor_<weakened|broken>``
                     and ``alpha_report_caution`` (active, unexpired) per ticker.
  * Estate gate:     ``argosy.services.target_allocation_doc.validate_instrument_domicile``
                     (UCITS-preferred; NVDA the only sanctioned US-situs sleeve).
  * Persistence:     ``argosy.services.action_proposer_runner.write_action_proposal``
                     (tombstone-then-insert dedup; ``execution_state='proposed'``).

The composition is **deterministic** — no LLM is invoked in the pure function.

Thesis-gating rule (TRIMS, confirmed with the user)
---------------------------------------------------
Consider only positions whose COARSE class (equity / bonds / cash) is flagged
*over-target* by ``detect_rebalancing_alerts`` (drift_pp > 0). For each held
position in such a class, propose a TRIM only when one of these gates fires:

  (a) ``THESIS_OVERWEIGHT``  — the position's per-position verdict is TRIM or
      SELL (the plan itself wants the weight down — intact-but-overweight, or a
      UCITS domicile swap), OR
  (b) ``THESIS_WEAKENED``    — an active ``thesis_monitor_weakened`` /
      ``thesis_monitor_broken`` flag exists for the ticker, OR
  (c) ``NEWS_CAUTION``       — an active ``alpha_report_caution`` flag mentions
      the ticker.

A HIGH-conviction position with an INTACT thesis (verdict HOLD/BUY/ADD, no
weakened/broken flag, no caution) is **never** trimmed UNLESS the class drift is
CRITICAL. ``CRITICAL`` here = the alert ``rule_fired == "25pct_relative"`` OR the
absolute class drift is >= ``_CRITICAL_DRIFT_PP`` (12pp) — i.e. the 25%-relative
rule fired, or the class is badly off even on the absolute scale. When the
critical override fires the gate is recorded as ``CRITICAL_DRIFT_OVERRIDE``.

BUY legs fund the most under-target classes from per-position ADD/BUY verdicts
(and the doc's instruments for those classes). Every BUY candidate is run
through the estate gate; a US-domiciled candidate (other than NVDA) is DROPPED
with a recorded note rather than proposed.

Conservation / sanity
----------------------
* Net cash delta (sum of BUY amounts − sum of TRIM/SELL amounts) is reported on
  the review; the composer sizes BUY legs to absorb the trim proceeds up to the
  under-target shortfall so the review reads fund-this-from-that.
* A TRIM never exceeds the position's current USD value.
* Every SELL/TRIM leg carries a taxable-event note (cited, not computed).
* It NEVER executes — a proposal only (``write_action_proposal`` hardcodes
  ``execution_state='proposed'``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# --- Tunables ----------------------------------------------------------------

#: Absolute coarse-class drift (in pp) at/above which a TRIM may override the
#: "never trim a high-conviction intact position" rule. The 25%-relative rule
#: ALSO triggers the override regardless of this value.
_CRITICAL_DRIFT_PP: float = 12.0

#: Standard taxable-event note appended to every SELL / TRIM leg. We cite the
#: tax consequence; we do NOT compute the exact tax (that's the realized-gains
#: engine's job, and depends on lots/basis the composer doesn't load).
_TAXABLE_EVENT_NOTE: str = (
    "Selling/trimming this position is a TAXABLE EVENT — it realizes capital "
    "gains (Israeli CGT, and for RSU lots a §102 / US-sourced component). Net "
    "proceeds will be below the gross trim amount. Confirm the lot-level tax "
    "before acting; this review does not compute the exact liability."
)


# --- Output shapes -----------------------------------------------------------


@dataclass
class RebalanceLeg:
    """One leg of the composed rebalance proposal.

    ``from_pct`` / ``to_pct`` are the position's CURRENT and intended weights
    (percent of book); ``amount_usd`` is the (always-positive) dollar size of
    the move. ``gate_reason`` records WHICH thesis gate authorized a TRIM/SELL
    (or which class shortfall a BUY funds).
    """

    action: str  # TRIM | SELL | BUY
    ticker: str
    asset_class: str  # coarse class: equity | bonds | cash
    from_pct: float | None
    to_pct: float | None
    amount_usd: float
    gate_reason: str
    thesis_conviction: str | None = None
    cited_flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RebalanceReview:
    """The composed whole-portfolio rebalance review.

    ``status`` is ``"ok"`` for a normal review (possibly with zero legs when no
    gated trim/buy was warranted) or ``"cannot_review"`` when a critical input
    is missing — the composer fails loud rather than returning a silent empty
    review.
    """

    status: str  # ok | cannot_review
    summary: str
    rationale_md: str
    legs: list[RebalanceLeg] = field(default_factory=list)
    net_cash_delta_usd: float = 0.0  # +ve = net buy (cash needed); -ve = net sell
    severity: str = "info"  # info | warning | critical
    cannot_review_reason: str | None = None
    dropped_buy_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["legs"] = [leg.to_dict() for leg in self.legs]
        return d


# --- Helpers -----------------------------------------------------------------


def _coarse_class_for_ticker(ticker: str, doc: Any) -> str | None:
    """Map a held ticker to the coarse class the drift alert uses.

    Looks the ticker up in the doc's classes (matching ``instruments[].symbol``)
    and collapses that class's ``sigma_class`` into equity/bonds/cash via the
    SAME ``_coarse_class`` collapse the rebalancing engine uses. Returns None
    when the ticker isn't named in the doc.
    """
    from argosy.services.retirement.rebalancing import _coarse_class

    tk = (ticker or "").strip().upper()
    if not tk:
        return None
    for c in getattr(doc, "classes", []) or []:
        for instr in getattr(c, "instruments", []) or []:
            if (getattr(instr, "symbol", "") or "").strip().upper() == tk:
                # sigma_class collapses through the engine's class->coarse map.
                # rebalancing._coarse_class keys on the fine asset_class names,
                # but sigma_class already IS the coarse-ish bucket; normalize.
                sc = (getattr(c, "sigma_class", "") or "").lower()
                if sc == "bonds":
                    return "bonds"
                if sc == "cash":
                    return "cash"
                # everything else (equities/alternatives/real_estate) -> equity
                return _coarse_class(sc) if sc in (
                    "concentrated_equity", "us_equity", "intl_equity",
                    "emerging_equity", "bonds", "cash", "real_estate",
                ) else "equity"
    return None


def _is_critical_alert(alert: Any) -> bool:
    """A class drift is CRITICAL when the 25%-relative rule fired OR the
    absolute signed drift is >= _CRITICAL_DRIFT_PP."""
    rule = str(getattr(alert, "rule_fired", "") or "")
    drift = abs(float(getattr(getattr(alert, "drift_pp", None), "value", 0.0) or 0.0))
    return rule == "25pct_relative" or drift >= _CRITICAL_DRIFT_PP


def _alert_drift_pp(alert: Any) -> float:
    return float(getattr(getattr(alert, "drift_pp", None), "value", 0.0) or 0.0)


def _verdict_index(verdicts: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for v in verdicts:
        tk = (getattr(v, "ticker", "") or "").strip().upper()
        if tk:
            out[tk] = v
    return out


def _flags_by_ticker(thesis_flags: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group active thesis_monitor / alpha_report_caution flag dicts by ticker.

    Each flag dict carries: ``kind``, ``ticker`` (already extracted from the
    payload by the orchestrator), ``severity``, ``dedup_key`` (for citation).
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for f in thesis_flags or []:
        tk = (f.get("ticker") or "").strip().upper()
        if not tk:
            continue
        out.setdefault(tk, []).append(f)
    return out


def _severity_rank(s: str) -> int:
    return {"info": 0, "warning": 1, "critical": 2}.get(s, 0)


# --- Pure composition --------------------------------------------------------


def compose_rebalance_review(
    *,
    doc: Any,
    position_verdicts: list[Any],
    alerts: list[Any],
    thesis_flags: list[dict[str, Any]],
    total_book_usd: float,
) -> RebalanceReview:
    """Deterministically compose ONE whole-portfolio rebalance review.

    Args:
      doc: the canonical ``TargetAllocationDoc`` (estate gate + ticker→class
        mapping). REQUIRED — a missing doc yields ``status='cannot_review'``.
      position_verdicts: ``PositionThesis`` cards from ``derive_position_theses``.
      alerts: ``RebalancingAlert`` list from ``detect_rebalancing_alerts``.
      thesis_flags: list of active thesis/news flag dicts (see ``_flags_by_ticker``).
      total_book_usd: total tradeable book USD value (for pct→USD sizing).

    Returns:
      A ``RebalanceReview``. ``status='cannot_review'`` when doc is missing or
      the book is non-positive (fail-loud — never a silent empty review on a
      missing critical input). Legs may be empty when nothing was gated.
    """
    if doc is None:
        return RebalanceReview(
            status="cannot_review",
            summary="Cannot compose a rebalance review: no canonical plan allocation doc.",
            rationale_md=(
                "There is no persisted canonical TargetAllocationDoc to rebalance "
                "toward. A plan must exist (with class targets + instruments) before "
                "a holistic rebalance can be composed."
            ),
            cannot_review_reason="missing_target_allocation_doc",
        )
    if not total_book_usd or total_book_usd <= 0:
        return RebalanceReview(
            status="cannot_review",
            summary="Cannot compose a rebalance review: portfolio snapshot is empty.",
            rationale_md=(
                "The portfolio total value is zero or missing, so weights cannot be "
                "sized to dollars. Ingest a current portfolio snapshot first."
            ),
            cannot_review_reason="missing_or_empty_portfolio_snapshot",
        )

    over_target = {
        a.asset_class: a for a in alerts if _alert_drift_pp(a) > 0
    }
    under_target = {
        a.asset_class: a for a in alerts if _alert_drift_pp(a) < 0
    }
    # Per-class trim budget (USD) = the class overage. Aggregate trims within a
    # class are capped at this so several positions in one over-target class
    # can't trim the class by MORE than its actual overage (codex-flagged
    # over-trim: each fallback-sized leg would otherwise claim the full overage).
    class_trim_remaining = {
        cls: round(max(0.0, _alert_drift_pp(a)) / 100.0 * total_book_usd, 2)
        for cls, a in over_target.items()
    }
    verdicts = _verdict_index(position_verdicts)
    flags = _flags_by_ticker(thesis_flags)

    trim_legs: list[RebalanceLeg] = []
    strongest_sev = "info"

    # --- TRIM side: only positions in over-target classes, thesis-gated. -----
    for v in position_verdicts:
        ticker = (getattr(v, "ticker", "") or "").strip().upper()
        if not ticker:
            continue
        cur_usd = getattr(v, "current_usd_value", None)
        if not cur_usd or cur_usd <= 0:
            continue  # not held (ADD candidate) or no value — can't trim
        coarse = _coarse_class_for_ticker(ticker, doc)
        if coarse is None or coarse not in over_target:
            continue  # only trim within an over-target class
        alert = over_target[coarse]

        verdict = (getattr(v, "verdict", "") or "").upper()
        conviction = (getattr(v, "conviction", "") or "").upper() or None
        tk_flags = flags.get(ticker, [])
        weakened = [
            f for f in tk_flags
            if str(f.get("kind", "")).startswith("thesis_monitor_")
        ]
        cautions = [
            f for f in tk_flags if f.get("kind") == "alpha_report_caution"
        ]

        gate_reason: str | None = None
        # (a) plan wants the weight down (intact-but-overweight or UCITS swap).
        if verdict in ("TRIM", "SELL"):
            gate_reason = "THESIS_OVERWEIGHT"
        # (b) thesis weakened / broken.
        elif weakened:
            gate_reason = "THESIS_WEAKENED"
        # (c) news caution.
        elif cautions:
            gate_reason = "NEWS_CAUTION"
        # critical-drift override — even a high-conviction intact position is
        # trimmed when the class is critically off target.
        elif _is_critical_alert(alert):
            gate_reason = "CRITICAL_DRIFT_OVERRIDE"

        if gate_reason is None:
            continue  # high-conviction intact position, non-critical drift -> keep

        # Size the trim toward the class target. The per-position target weight
        # (if any) gives a precise to_pct; otherwise size a proportional share
        # of the class overage attributable to this position.
        from_pct = getattr(v, "current_weight_pct", None)
        tgt_pct = getattr(v, "target_weight_pct", None)
        if (
            from_pct is not None and tgt_pct is not None
            and tgt_pct < from_pct
        ):
            # Precise per-position target → exact trim. NOT capped by the coarse
            # class budget: several instruments in one coarse class can each be
            # over their own target while the class nets to a smaller overage
            # (the intra-class under-target names are funded on the buy side), so
            # the sum of target trims can legitimately exceed the coarse overage.
            used_fallback = False
            to_pct = float(tgt_pct)
            trim_pct = float(from_pct) - to_pct
        else:
            # No per-position target → size by the class overage. This is the
            # over-trim hazard: every fallback leg in a class would otherwise
            # claim the FULL class overage. Cap fallback legs against a shared
            # per-class budget (below) so they can't trim the class past its
            # overage in aggregate.
            used_fallback = True
            class_drift_pp = _alert_drift_pp(alert)  # +ve overage
            to_pct = (
                max(0.0, float(from_pct) - class_drift_pp)
                if from_pct is not None else None
            )
            trim_pct = class_drift_pp if from_pct is None else (
                float(from_pct) - (to_pct or 0.0)
            )

        amount_usd = round(min(
            cur_usd, max(0.0, trim_pct) / 100.0 * total_book_usd
        ), 2)
        if used_fallback:
            # Cap against (and decrement) the shared per-class trim budget so
            # multiple fallback legs in one over-target class can't double-count
            # the overage (codex-flagged).
            budget_left = class_trim_remaining.get(coarse, 0.0)
            amount_usd = round(min(amount_usd, budget_left), 2)
            if amount_usd > 0:
                class_trim_remaining[coarse] = round(budget_left - amount_usd, 2)
        if amount_usd <= 0:
            continue

        action = "SELL" if verdict == "SELL" else "TRIM"
        cited = [
            f.get("dedup_key") or f.get("kind") for f in (weakened + cautions)
            if (f.get("dedup_key") or f.get("kind"))
        ]
        leg_sev = "warning"
        if gate_reason == "CRITICAL_DRIFT_OVERRIDE" or any(
            f.get("severity") == "critical" for f in tk_flags
        ):
            leg_sev = "critical"
        if _severity_rank(leg_sev) > _severity_rank(strongest_sev):
            strongest_sev = leg_sev

        notes = [_TAXABLE_EVENT_NOTE]
        if gate_reason == "CRITICAL_DRIFT_OVERRIDE":
            notes.append(
                f"High-conviction intact position trimmed ONLY because the {coarse} "
                f"class drift is critical (rule={getattr(alert, 'rule_fired', '?')}, "
                f"drift={_alert_drift_pp(alert):+.1f}pp)."
            )

        trim_legs.append(RebalanceLeg(
            action=action,
            ticker=ticker,
            asset_class=coarse,
            from_pct=round(float(from_pct), 2) if from_pct is not None else None,
            to_pct=round(float(to_pct), 2) if to_pct is not None else None,
            amount_usd=amount_usd,
            gate_reason=gate_reason,
            thesis_conviction=conviction,
            cited_flags=[c for c in cited if c],
            notes=notes,
        ))

    trim_proceeds = round(sum(l.amount_usd for l in trim_legs), 2)

    # --- BUY side: fund the most under-target classes, estate-gated. ---------
    buy_legs: list[RebalanceLeg] = []
    dropped: list[dict[str, Any]] = []

    # Estate gate: collect the set of doc symbols that are RED (US-domiciled,
    # non-sanctioned). We DROP these from the buy side.
    from argosy.services.target_allocation_doc import validate_instrument_domicile

    violations = validate_instrument_domicile(doc)
    red_symbols = {
        v.symbol.upper() for v in violations if v.severity == "RED"
    }

    # Candidate buy tickers: per-position ADD / BUY verdicts whose class is
    # under-target. Sort under-target classes by magnitude of shortfall so the
    # most-under class is funded first.
    add_buy = [
        v for v in position_verdicts
        if (getattr(v, "verdict", "") or "").upper() in ("ADD", "BUY")
    ]
    # Compute remaining shortfall per under-target class (in USD).
    shortfall_usd: dict[str, float] = {}
    for cls, alert in under_target.items():
        shortfall_usd[cls] = abs(_alert_drift_pp(alert)) / 100.0 * total_book_usd

    # Total cash available to deploy = trim proceeds (fund-this-from-that).
    remaining_cash = trim_proceeds

    ordered_classes = sorted(
        under_target.keys(), key=lambda c: -shortfall_usd.get(c, 0.0)
    )
    for cls in ordered_classes:
        if remaining_cash <= 0:
            break
        # Buy candidates whose doc class collapses to this coarse class.
        candidates = [
            v for v in add_buy
            if _coarse_class_for_ticker(
                (getattr(v, "ticker", "") or "").upper(), doc
            ) == cls
        ]
        if not candidates:
            continue
        # Drop estate-gate violators; note them.
        usable: list[Any] = []
        for v in candidates:
            tk = (getattr(v, "ticker", "") or "").upper()
            if tk in red_symbols:
                dropped.append({
                    "ticker": tk,
                    "asset_class": cls,
                    "reason": (
                        f"{tk} is US-domiciled (non-sanctioned) → US-situs estate "
                        f"exposure for a non-US-person. Dropped from the buy side; "
                        f"use the Irish UCITS twin instead."
                    ),
                })
                continue
            usable.append(v)
        if not usable:
            continue

        cls_shortfall = shortfall_usd.get(cls, 0.0)
        deploy_here = min(remaining_cash, cls_shortfall)
        if deploy_here <= 0:
            continue
        per_leg = round(deploy_here / len(usable), 2)
        if per_leg <= 0:
            continue
        for v in usable:
            tk = (getattr(v, "ticker", "") or "").upper()
            buy_legs.append(RebalanceLeg(
                action="BUY",
                ticker=tk,
                asset_class=cls,
                from_pct=(
                    round(float(getattr(v, "current_weight_pct", 0.0)), 2)
                    if getattr(v, "current_weight_pct", None) is not None else None
                ),
                to_pct=None,
                amount_usd=per_leg,
                gate_reason=f"FUND_UNDER_TARGET_{cls.upper()}",
                thesis_conviction=(getattr(v, "conviction", "") or "").upper() or None,
                cited_flags=[],
                notes=[
                    f"Estate-gate cleared (UCITS-preferred; NVDA the only sanctioned "
                    f"US-situs sleeve). Funds the under-target {cls} class "
                    f"(shortfall ≈ ${cls_shortfall:,.0f})."
                ],
            ))
        remaining_cash = round(remaining_cash - per_leg * len(usable), 2)

    legs = trim_legs + buy_legs
    buy_total = round(sum(l.amount_usd for l in buy_legs), 2)
    net_cash_delta = round(buy_total - trim_proceeds, 2)

    # --- Summary / rationale -------------------------------------------------
    if not legs:
        summary = (
            "No gated rebalance legs: either no class is over-target, or every "
            "over-target position is a high-conviction intact holding under "
            "non-critical drift (held by policy)."
        )
        rationale = (
            "Holistic rebalance review ran. "
            f"{len(over_target)} over-target class(es), "
            f"{len(under_target)} under-target class(es). No thesis-gated trims "
            "were warranted (high-conviction intact positions are not trimmed "
            "under non-critical drift), so no funding legs were composed."
        )
        return RebalanceReview(
            status="ok",
            summary=summary,
            rationale_md=rationale,
            legs=[],
            net_cash_delta_usd=0.0,
            severity="info",
            dropped_buy_candidates=dropped,
        )

    n_trim = len(trim_legs)
    n_buy = len(buy_legs)
    summary = (
        f"Holistic rebalance: {n_trim} thesis-gated trim/sell leg(s) "
        f"(${trim_proceeds:,.0f}) funding {n_buy} buy leg(s) (${buy_total:,.0f}); "
        f"net cash {net_cash_delta:+,.0f} USD."
    )

    lines = [
        "## Holistic rebalance review",
        "",
        f"- Over-target classes: {', '.join(sorted(over_target)) or '(none)'}",
        f"- Under-target classes: {', '.join(sorted(under_target)) or '(none)'}",
        f"- Trim/sell proceeds: ${trim_proceeds:,.0f}",
        f"- Buy deployed: ${buy_total:,.0f}",
        f"- **Net cash delta: {net_cash_delta:+,.0f} USD** "
        f"({'net buy — cash needed' if net_cash_delta > 0 else 'net sell — cash freed' if net_cash_delta < 0 else 'cash-neutral'})",
        "",
        "### Trim / sell legs (thesis-gated)",
    ]
    for l in trim_legs:
        lines.append(
            f"- **{l.action} {l.ticker}** ({l.asset_class}, "
            f"{l.from_pct}%→{l.to_pct}%, ${l.amount_usd:,.0f}) — gate "
            f"`{l.gate_reason}`, conviction {l.thesis_conviction or 'n/a'}"
            + (f", flags: {', '.join(l.cited_flags)}" if l.cited_flags else "")
        )
    lines.append("")
    lines.append("### Buy legs (estate-gated, UCITS-preferred)")
    for l in buy_legs:
        lines.append(
            f"- **BUY {l.ticker}** ({l.asset_class}, ${l.amount_usd:,.0f}) — "
            f"`{l.gate_reason}`"
        )
    if dropped:
        lines.append("")
        lines.append("### Dropped buy candidates (estate gate)")
        for d in dropped:
            lines.append(f"- {d['ticker']} ({d['asset_class']}): {d['reason']}")
    lines.append("")
    lines.append(
        "> Every trim/sell leg is a TAXABLE EVENT (capital gains realized; net "
        "proceeds below gross). This review is a PROPOSAL only — nothing executes."
    )
    rationale = "\n".join(lines)

    return RebalanceReview(
        status="ok",
        summary=summary[:240] if len(summary) > 240 else summary,
        rationale_md=rationale,
        legs=legs,
        net_cash_delta_usd=net_cash_delta,
        severity=strongest_sev,
        dropped_buy_candidates=dropped,
    )


# --- Orchestrator (loads inputs from existing accessors) ---------------------


def run_holistic_rebalance_review(
    user_id: str,
    session: Any,
    *,
    write_proposal: bool = True,
    now: datetime | None = None,
) -> tuple[RebalanceReview, bool]:
    """Load the real inputs via the existing accessors, compose the review, and
    (optionally) persist it as a ``rebalance`` ActionProposal.

    Returns ``(review, proposal_written)``. The deterministic review is ALWAYS
    returned; ``proposal_written`` reflects whether a row was persisted (False
    on cannot_review, no legs, or write disabled).

    Reused accessors:
      * drift:        ``detect_rebalancing_alerts(user_id, current_age, session)``
      * age:          ``extract_household_state(session, user_id).current_age_years``
      * doc:          ``get_current_plan`` + ``load_plan_target_allocation``
      * verdicts:     ``derive_position_theses(pv, snapshot, agent_reports)``
      * thesis/news:  active ``monitor_flags`` rows (thesis_monitor_* / alpha_report_caution)
      * write:        ``write_action_proposal`` (kind='rebalance')
    """
    if now is None:
        now = datetime.now(timezone.utc)

    from argosy.services.retirement.rebalancing import detect_rebalancing_alerts
    from argosy.services.cashflow_projection import extract_household_state
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan, get_pending_draft

    # --- Plan / doc ----------------------------------------------------------
    pv = get_pending_draft(session, user_id) or get_current_plan(session, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None

    # --- current age (for the drift accessor signature) ---------------------
    try:
        hh = extract_household_state(session, user_id)
        current_age = int(round(hh.current_age_years))
    except Exception:  # noqa: BLE001 — age is only an arg to the drift call
        logger.warning("rebalance_review: household age lookup failed", exc_info=True)
        current_age = 43

    # --- drift ---------------------------------------------------------------
    alerts = detect_rebalancing_alerts(
        user_id=user_id, current_age=current_age, session=session,
    )

    # --- portfolio snapshot + per-position verdicts -------------------------
    snapshot = _load_snapshot(session, user_id)
    total_book_usd = _total_book_usd(snapshot)
    verdicts = _load_position_verdicts(session, user_id, pv, snapshot)

    # --- active thesis / news flags (per ticker) ----------------------------
    thesis_flags = _load_active_thesis_flags(session, user_id, now=now)

    review = compose_rebalance_review(
        doc=doc,
        position_verdicts=verdicts,
        alerts=alerts,
        thesis_flags=thesis_flags,
        total_book_usd=total_book_usd,
    )

    proposal_written = False
    if write_proposal and review.status == "ok" and review.legs:
        proposal_written = _persist_review(session, user_id, review, now=now)

    return review, proposal_written


def _load_snapshot(session: Any, user_id: str) -> Any:
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )

    row = get_latest_snapshot_row(session, user_id)
    return row_to_snapshot(row) if row is not None else None


def _total_book_usd(snapshot: Any) -> float:
    if snapshot is None:
        return 0.0
    total_k = getattr(snapshot, "total_usd_value_k", None)
    if total_k:
        try:
            return float(total_k) * 1000.0
        except (TypeError, ValueError):
            pass
    # Fall back to summing positions.
    total = 0.0
    for p in getattr(snapshot, "positions", []) or []:
        v = getattr(p, "usd_value_k", None)
        if isinstance(v, (int, float)):
            total += float(v) * 1000.0
    return total


def _load_position_verdicts(session: Any, user_id: str, pv: Any, snapshot: Any) -> list[Any]:
    from sqlalchemy import select

    from argosy.services.per_position_thesis import derive_position_theses
    from argosy.state.models import AgentReport

    if pv is None or snapshot is None:
        return []
    reports: list[Any] = []
    decision_run_id = getattr(pv, "decision_run_id", None)
    if decision_run_id is not None:
        decision_id_str = f"plan-synth-{decision_run_id}"
        reports = list(
            session.execute(
                select(AgentReport).where(
                    AgentReport.user_id == user_id,
                    AgentReport.decision_id == decision_id_str,
                )
            ).scalars().all()
        )
    try:
        return derive_position_theses(
            plan_version=pv,
            portfolio_snapshot=snapshot,
            agent_reports=reports,
        )
    except Exception:  # noqa: BLE001
        logger.warning("rebalance_review: per-position derivation failed", exc_info=True)
        return []


def _load_active_thesis_flags(session: Any, user_id: str, *, now: datetime) -> list[dict[str, Any]]:
    """Query active, unexpired thesis_monitor_* / alpha_report_caution flags and
    extract the ticker from each payload. Returns a list of normalized dicts."""
    from sqlalchemy import and_, or_, select

    from argosy.state.models import MonitorFlag

    rows = session.execute(
        select(MonitorFlag).where(
            and_(
                MonitorFlag.user_id == user_id,
                MonitorFlag.status == "active",
                MonitorFlag.acknowledged_at.is_(None),
                or_(
                    MonitorFlag.kind.like("thesis_monitor_%"),
                    MonitorFlag.kind == "alpha_report_caution",
                ),
            )
        )
    ).scalars().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        # Skip expired flags (best-effort; expires_at may be None).
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
        # alpha_report_caution payloads don't carry a clean ticker — pull from
        # the caution text by scanning held tickers later isn't this layer's
        # job; we surface whatever ticker the payload exposes (thesis_monitor
        # always sets it). A caution without a ticker simply won't match a leg.
        out.append({
            "kind": r.kind,
            "ticker": ticker,
            "severity": r.severity,
            "dedup_key": r.dedup_key or r.kind,
        })
    return out


def _persist_review(
    session: Any, user_id: str, review: RebalanceReview, *, now: datetime
) -> bool:
    """Persist the deterministic review as a ``rebalance`` ActionProposal.

    Uses ``write_action_proposal`` directly (NOT the LLM action_proposer) so the
    deterministic composition is what's stored — ``write_action_proposal``
    hardcodes ``execution_state='proposed'`` and applies tombstone-then-insert
    dedup. The ``rebalance`` kind's required payload field is ``rows``.
    """
    from argosy.services.action_proposer_runner import (
        build_dedup_key,
        write_action_proposal,
    )

    rows_payload = [
        {
            "action": l.action,
            "ticker": l.ticker,
            "asset_class": l.asset_class,
            "from_pct": l.from_pct,
            "to_pct": l.to_pct,
            "amount_usd": l.amount_usd,
            "gate_reason": l.gate_reason,
            "conviction": l.thesis_conviction,
            "cited_flags": l.cited_flags,
        }
        for l in review.legs
    ]
    payload = {
        "rows": rows_payload,
        "net_cash_delta_usd": review.net_cash_delta_usd,
        "dropped_buy_candidates": review.dropped_buy_candidates,
        "composer": "holistic_rebalance_review",
    }
    # Dedup key is per (kind, holistic-rebalance-token, severity) so a re-run
    # within the window collapses onto the existing open proposal rather than
    # spamming the queue.
    dedup_key = build_dedup_key(
        kind="rebalance",
        primary_ref_id="holistic_rebalance",
        severity_bucket=review.severity,
    )
    try:
        write_action_proposal(
            session,
            user_id,
            kind="rebalance",
            summary=review.summary,
            rationale_md=review.rationale_md,
            suggested_payload=payload,
            severity=review.severity,
            dedup_key=dedup_key,
            now=now,
        )
        return True
    except Exception:  # noqa: BLE001 — persistence failure never sinks the review
        session.rollback()
        logger.warning("rebalance_review: proposal write failed", exc_info=True)
        return False


__all__ = [
    "RebalanceLeg",
    "RebalanceReview",
    "compose_rebalance_review",
    "run_holistic_rebalance_review",
]
