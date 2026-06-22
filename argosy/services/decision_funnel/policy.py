"""Stage-1 routing policy — the versioned, deterministic thresholds.

Codex's top hardening item: "Stage 1 is a DETERMINISTIC, thresholded policy,
not just a cheap LLM." This module is that policy, single-sourced and
content-hashed so every funnel run records exactly which thresholds it routed
against (D4 observability) and a change is one auditable edit.

The policy encodes:
- HARD TRIGGERS that force a deep review regardless of cooldown (a name must
  EARN a deep review; these are the things that always warrant one).
- MATERIALITY thresholds that route a name only when a signal clears the bar.
- Per-name COOLDOWNS so an unchanged name isn't re-reviewed daily.
- A deterministic AUDIT sample of DROPS to catch false-drops.
- Default NO-OP: anything that fires nothing routes to nothing.

All bands are conservative by design — this is a long-hold book, not a
day-trader's blotter, and Stage 3 is expensive + propose-and-ask.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RoutingPolicy:
    """Deterministic Stage-1 thresholds. Frozen + content-hashed."""

    # --- hard triggers (force a deep review, bypass cooldown) ---
    # Thesis monitor: a BROKEN thesis at any severity, or a WEAKENED thesis at
    # warning OR critical severity, always warrants a fresh decision. (A
    # weakened-at-warning thesis is a real thesis change — routing it is the
    # conservative choice; a false route only costs tokens.)
    route_on_thesis_broken: bool = True
    route_on_thesis_weakened_warning: bool = True
    # Big single-name move (1-month return magnitude, the available price
    # metric from the thesis-monitor price summary) — a large move is a
    # decision trigger whether it's a drawdown or a melt-up.
    big_move_1m_abs_pct: float = 15.0
    # Deep drawdown off the 52-week high.
    big_drawdown_off_high_pct: float = 25.0
    # Earnings within this many days → forced event-driven review.
    earnings_window_days: int = 5
    # Drift of a name's weight above/below its implied target, in percentage
    # points (absolute). Ties to IPS.sell_trigger_drift_pct when available.
    drift_band_pp: float = 5.0

    # --- materiality (route only when the bar is cleared, subject to cooldown) ---
    # A high-materiality single-name news signal routes the name.
    route_on_high_materiality_news: bool = True
    # A risk-off macro read routes the affected sleeve(s), not every name.
    route_sleeve_on_macro_risk_off: bool = True

    # --- cooldowns ---
    # Don't deep-review the same name within this window UNLESS a hard trigger
    # fires. Prevents daily churn on an unchanged thesis.
    cooldown_days: int = 5

    # --- audit of drops ---
    # Deterministically route ~1-in-N dropped names anyway, to catch
    # false-drops (Stage 1 mistakes). 0 disables. The sample is reproducible
    # (hash of day+ticker), not truly random, so a run is replayable.
    audit_drop_one_in: int = 25
    # BLIND drops — a held name with NO signal coverage at all (no thesis flag,
    # no price, no news, no earnings) — are a known false-drop class (stale or
    # missing feeds), so they are audited at a DENSER rate than ordinary drops.
    blind_drop_audit_one_in: int = 8

    # --- concentration caps (fallbacks when IPS is pending) ---
    fallback_general_single_name_cap_pct: float = 10.0
    fallback_nvda_cap_pct: float = 13.0

    # --- discovery-driven new-name candidates ---
    # The discovery funnel's conviction picks feed the decision funnel as
    # new-name BUY candidates. Only the strongest (HIGH conviction + a BUY
    # verdict) earn a deep review — a new name is a higher bar than acting on a
    # held one. Routing them is conservative: deep decision is still
    # propose-and-ask, and (until the funding engine lands) shadow-gated.
    route_discovery_picks: bool = True
    discovery_conviction_floor: str = "HIGH"

    @property
    def version(self) -> str:
        """Short content hash — stamped on every run + snapshot."""
        blob = json.dumps(asdict(self), sort_keys=True)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
        return f"pol-{digest}"

    def to_dict(self) -> dict:
        return {**asdict(self), "version": self.version}


# The default policy the loop uses. Override only via an explicit construction
# in a test or a future per-user tuning layer.
DEFAULT_POLICY = RoutingPolicy()


def audit_hit(one_in: int, *, day: str, ticker: str) -> bool:
    """Deterministic, reproducible 1-in-N sampling (no true randomness, so a
    run replays identically)."""
    if one_in <= 0:
        return False
    h = hashlib.sha256(f"{day}|{ticker.upper()}".encode()).hexdigest()
    return int(h[:8], 16) % one_in == 0


def should_audit_drop(
    policy: RoutingPolicy, *, day: str, ticker: str, blind: bool = False
) -> bool:
    """Whether a dropped name should be audit-re-routed. Blind drops use the
    denser rate."""
    one_in = policy.blind_drop_audit_one_in if blind else policy.audit_drop_one_in
    return audit_hit(one_in, day=day, ticker=ticker)


__all__ = ["RoutingPolicy", "DEFAULT_POLICY", "should_audit_drop", "audit_hit"]
