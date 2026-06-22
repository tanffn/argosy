"""Stage 1 — DETERMINISTIC relevance routing onto the book.

Codex's #1 hardening item: Stage 1 is a deterministic, thresholded policy, NOT
a cheap LLM. It maps the Stage-0 macro read + per-name signals onto the actual
holdings and decides what could be affected. A name must EARN a deep review:

- HARD TRIGGERS force a review regardless of cooldown (thesis broken / weakened
  -critical, big move, deep drawdown, imminent earnings, concentration-cap
  breach, NVDA drift-band breach).
- MATERIALITY (high-materiality single-name news) routes a name UNLESS it is in
  cooldown.
- Otherwise the name DROPS (default NO-OP) — with the reason recorded, never
  silently.
- A deterministic AUDIT sample of drops is re-routed to catch false-drops.
- A risk-off macro read routes the broad-equity SLEEVES (not every name).

This module is pure (no DB / no LLM): the loop builds the per-name signals + the
cooldown map from already-ingested data and the trace tables, then calls
``route``. That keeps the policy unit-testable and replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from argosy.services.decision_funnel.policy import (
    DEFAULT_POLICY,
    RoutingPolicy,
    should_audit_drop,
)

# Sleeve sigma-classes that a BROAD-MARKET risk-off read does NOT route. Only
# broad-equity sleeves move with a market-wide risk-off; alternatives, gold,
# crypto, REITs/real-estate, bonds, cash and explicitly-defensive sleeves do
# not, so routing them on macro risk-off would be noise (codex NIT).
_NON_BROAD_EQUITY_SIGMA = {
    "bonds", "bond", "cash", "alternatives", "alternative", "real_estate",
    "realestate", "reit", "reits", "gold", "crypto", "commodity", "commodities",
    "defensive",
}

if TYPE_CHECKING:  # pragma: no cover
    from argosy.services.decision_funnel.book import BookHolding
    from argosy.services.decision_funnel.stage0_market import MarketRead
    from argosy.services.ips import InvestmentPolicyStatement


@dataclass(frozen=True)
class PerNameSignal:
    """Already-ingested per-name signals the loop assembles for one ticker."""

    ticker: str
    thesis_status: str | None = None  # intact|strengthened|weakened|broken
    thesis_severity: str | None = None  # info|warning|critical
    ret_1m_pct: float | None = None
    off_52w_high_pct: float | None = None  # negative = below high
    earnings_in_days: int | None = None
    high_materiality_news: bool = False
    news_sentiment: str | None = None


@dataclass(frozen=True)
class RoutedCandidate:
    subject: str
    subject_type: str  # "holding" | "sleeve" | "watch"
    triggers: list[str]
    primary_signal: str
    reason: str
    is_audit: bool = False


@dataclass(frozen=True)
class DropRecord:
    subject: str
    subject_type: str
    reason: str
    signal: str = "no_match"


@dataclass(frozen=True)
class RoutingResult:
    routed: list[RoutedCandidate] = field(default_factory=list)
    dropped: list[DropRecord] = field(default_factory=list)


def _cap_for(
    ticker: str, ips: "InvestmentPolicyStatement | None", policy: RoutingPolicy
) -> tuple[float | None, bool]:
    """Return ``(cap_pct, resolved)`` for a ticker.

    ``resolved`` is True only when the cap came from the plan-derived IPS. When
    the IPS is absent OR the relevant cap field is pending, ``resolved`` is
    False and the value is the conservative policy FALLBACK — callers must NOT
    use a pending cap to DROP a name (codex BLOCKER 3); a name above the
    fallback when the real cap is unknown is ROUTED for verification, never
    silently dropped.
    """
    is_nvda = ticker.upper() == "NVDA"
    if ips is not None:
        field = ips.nvda_cap_pct if is_nvda else ips.general_single_name_cap_pct
        if field.value is not None and field.status in ("resolved", "policy_default"):
            return float(field.value), True
    fallback = (
        policy.fallback_nvda_cap_pct if is_nvda
        else policy.fallback_general_single_name_cap_pct
    )
    return fallback, False


def _nvda_target(ips: "InvestmentPolicyStatement | None") -> float | None:
    if ips is not None and ips.nvda_target_pct.value is not None:
        return float(ips.nvda_target_pct.value)
    return None


def _is_blind(sig: PerNameSignal) -> bool:
    """True when a holding has NO signal coverage at all (likely a stale/missing
    feed) — a known false-drop class that gets a denser audit."""
    return (
        sig.thesis_status is None
        and sig.ret_1m_pct is None
        and sig.off_52w_high_pct is None
        and sig.earnings_in_days is None
        and not sig.high_materiality_news
    )


def _hard_triggers(
    h: "BookHolding",
    sig: PerNameSignal,
    ips: "InvestmentPolicyStatement | None",
    policy: RoutingPolicy,
) -> list[str]:
    triggers: list[str] = []
    if sig.thesis_status == "broken" and policy.route_on_thesis_broken:
        triggers.append("thesis_broken")
    if (
        sig.thesis_status == "weakened"
        and sig.thesis_severity in ("warning", "critical")
        and policy.route_on_thesis_weakened_warning
    ):
        triggers.append("thesis_weakened")
    if sig.ret_1m_pct is not None and abs(sig.ret_1m_pct) >= policy.big_move_1m_abs_pct:
        triggers.append("big_move")
    if (
        sig.off_52w_high_pct is not None
        and sig.off_52w_high_pct <= -abs(policy.big_drawdown_off_high_pct)
    ):
        triggers.append("big_drawdown")
    if sig.earnings_in_days is not None and 0 <= sig.earnings_in_days <= policy.earnings_window_days:
        triggers.append("earnings_imminent")
    cap, resolved = _cap_for(h.ticker, ips, policy)
    if cap is not None and h.weight_pct > cap:
        # Known cap breach vs an UNVERIFIED breach (cap pending/absent). Either
        # way we ROUTE — we never DROP on a pending cap.
        triggers.append(
            "concentration_cap_breach" if resolved else "concentration_unverified"
        )
    # Drift band only where a per-name target exists (NVDA today). Non-NVDA
    # per-name drift is a known gap — the plan sets sleeve targets + a NVDA
    # per-name target, not per-name targets for every holding. Sleeve drift is
    # handled at the sleeve level (macro routing + the monthly plan refresh).
    if h.ticker.upper() == "NVDA":
        target = _nvda_target(ips)
        drift_band = (
            float(ips.sell_trigger_drift_pct.value)
            if ips is not None and ips.sell_trigger_drift_pct.value is not None
            else policy.drift_band_pp
        )
        if target is not None and abs(h.weight_pct - target) >= drift_band:
            triggers.append("drift_band_breach")
    return triggers


def _affected_sleeves(
    ips: "InvestmentPolicyStatement | None",
) -> list[str]:
    """Broad-EQUITY sleeves to route on a risk-off macro read (excludes bonds,
    cash, alternatives, gold, crypto, REIT/real-estate, defensive)."""
    if ips is None or not ips.sleeve_targets:
        return ["broad_equity"]
    out = [
        s.label
        for s in ips.sleeve_targets
        if (s.sigma_class or "").lower() not in _NON_BROAD_EQUITY_SIGMA
    ]
    return out or ["broad_equity"]


def route(
    *,
    book: list["BookHolding"],
    market_read: "MarketRead",
    ips: "InvestmentPolicyStatement | None",
    signals: dict[str, PerNameSignal],
    day: str,
    now: datetime,
    last_review_by_ticker: dict[str, datetime] | None = None,
    policy: RoutingPolicy = DEFAULT_POLICY,
) -> RoutingResult:
    """Deterministically route the book. Returns routed + dropped candidates.

    ``now`` is REQUIRED (no wall-clock default) so the routing — and therefore
    the trace — is replay-pure (codex BLOCKER 5).

    Every name is accounted for: a routed candidate carries its trigger(s) +
    reason; a drop carries its reason. The loop persists both via the trace.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Normalise lookup keys to upper-case so a lowercase signal/cooldown key
    # can't silently miss (codex NIT).
    signals = {k.upper(): v for k, v in (signals or {}).items()}
    last_review_by_ticker = {
        k.upper(): v for k, v in (last_review_by_ticker or {}).items()
    }
    # Stage-0 high-materiality news, merged in directly so routing does not
    # depend on the caller having duplicated it into ``signals`` (codex
    # BLOCKER 1). News here is a primary source.
    news_by_ticker: dict[str, str | None] = {}
    for hit in getattr(market_read, "high_materiality_news", []) or []:
        news_by_ticker.setdefault(hit.ticker.upper(), getattr(hit, "sentiment", None))

    routed: list[RoutedCandidate] = []
    dropped: list[DropRecord] = []

    for h in book:
        tk = h.ticker.upper()
        sig = signals.get(tk) or PerNameSignal(ticker=tk)
        # Fold in Stage-0 news even if the caller didn't populate the signal.
        has_news = sig.high_materiality_news or (tk in news_by_ticker)
        news_sentiment = sig.news_sentiment or news_by_ticker.get(tk)

        hard = _hard_triggers(h, sig, ips, policy)
        if hard:
            routed.append(
                RoutedCandidate(
                    subject=tk, subject_type="holding", triggers=hard,
                    primary_signal=hard[0],
                    reason=f"hard trigger(s): {', '.join(hard)}",
                )
            )
            continue

        # High-materiality news BYPASSES cooldown (codex BLOCKER 2): a fresh
        # fraud / guidance-cut / acquisition item must not be suppressed because
        # of a routine review days ago. A false route only costs tokens.
        if has_news and policy.route_on_high_materiality_news:
            routed.append(
                RoutedCandidate(
                    subject=tk, subject_type="holding",
                    triggers=["high_materiality_news"],
                    primary_signal="high_materiality_news",
                    reason=f"high-materiality news (sentiment={news_sentiment})",
                )
            )
            continue

        last = last_review_by_ticker.get(tk)
        in_cooldown = last is not None and (now - last) < timedelta(days=policy.cooldown_days)
        blind = _is_blind(sig) and tk not in news_by_ticker

        if in_cooldown:
            reason = f"cooldown: deep-reviewed {last.date().isoformat()} (< {policy.cooldown_days}d)"
        elif blind:
            reason = "no signal coverage (stale/missing feed) — denser audit"
        else:
            reason = "no material signal"

        if should_audit_drop(policy, day=day, ticker=tk, blind=blind):
            routed.append(
                RoutedCandidate(
                    subject=tk, subject_type="holding", triggers=["audit_sample"],
                    primary_signal="audit_sample",
                    reason=f"audit re-route of a drop ({reason})", is_audit=True,
                )
            )
        else:
            dropped.append(DropRecord(subject=tk, subject_type="holding", reason=reason))

    # Sleeve routing on a risk-off macro read (broad-equity sleeves only).
    if market_read.risk_off and policy.route_sleeve_on_macro_risk_off:
        for label in _affected_sleeves(ips):
            routed.append(
                RoutedCandidate(
                    subject=f"sleeve:{label}"[:64], subject_type="sleeve",
                    triggers=["macro_risk_off"], primary_signal="macro_risk_off",
                    reason=f"risk-off macro read ({market_read.summary})",
                )
            )

    return RoutingResult(routed=routed, dropped=dropped)


__all__ = [
    "PerNameSignal",
    "RoutedCandidate",
    "DropRecord",
    "RoutingResult",
    "route",
]
