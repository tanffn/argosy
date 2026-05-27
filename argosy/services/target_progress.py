"""Target-progress — live "current vs target" annotation per plan target.

Pure-Python service that, given a user_id and a plan_versions row, returns
one ``TargetProgress`` per target found in the long / medium / short horizon
JSON payloads. Each row carries a live-computed ``current_value`` (when
computable from snapshot / FX / fills / agent reports) plus a ``status``
classification (AT_TARGET / ABOVE_TARGET / BELOW_TARGET / UNKNOWN) that the
UI uses to render a 🟢/🟡/🔴/⚪ progress strip below each TARGET card.

Units handled today:

  * ``pct_of_portfolio`` / ``pct_of_net_worth`` — % of total USD portfolio
    value for a symbol identified by keywords in the target label
    (NVDA / SGOV / cash / specific tickers).
  * ``usd`` — USD value of a sleeve identified by keywords in the label
    (``defensive``/``sgov`` → SGOV+cash; ``us_situs``/``us-domiciled``/
    ``us domiciled`` → Schwab-located holdings, etc.)
  * ``nis`` — NIS-denominated targets; pulls life-insurance face amount
    when the label hints at it (we don't track life-insurance value in
    the snapshot, so this falls back to UNKNOWN with a clear reason).
  * ``shares`` — share-count targets. When the label says "to sell" we
    pull from concentration agent_report's ``shares_sold_ytd``; when it
    says "ending share count" we pull from current NVDA holdings.
  * ``months`` — runway-style targets; pull from wealth-dashboard math
    (defensive_total_nis / monthly_burn_nis).
  * ``ratio`` — bare ratios (e.g. hedge ratio); UNKNOWN — we don't track
    hedge state in the DB yet.

When a target's unit isn't in this list, or the label doesn't match any
known signature, the row returns ``status=UNKNOWN`` with
``compute_source='not yet computable'``. The UI renders that as the
"(live state pending: synthesis required)" sentence instead of a number.

``direction_is_good`` encodes whether being ABOVE the target is desirable:

  * ``False`` for ceiling-style targets ("NVDA share of portfolio",
    "US-domiciled ETF aggregate ceiling", "share count at gate"). Being
    above the target is bad → 🔴.
  * ``True`` for floor-style targets ("SGOV sleeve floor", "life
    insurance face amount", "shares to sell"). Being above is good → 🟢
    when far above, 🟡 when close-but-below.
  * ``None`` when ambiguous (the ratio target above). UI renders ⚪.

This service has no LLM dependency; it reads the latest
portfolio_snapshots row, the latest household_budget agent_report, and
the concentration agent_report tied to the draft's decision_run_id.
Costs <10ms in practice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.state.models import (
    AgentReport,
    PlanVersion,
    PortfolioSnapshotRow,
    UserContext,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTO — wire shape mirrored by argosy.api.routes.plan.TargetProgress
# (the API layer wraps this in pydantic so the OpenAPI schema is clean).
# ---------------------------------------------------------------------------


# Status thresholds — the relative band where AT_TARGET applies.
AT_TARGET_REL_TOLERANCE = 0.02  # ±2% of target value


@dataclass
class TargetProgress:
    item_id: str
    target_value: float
    target_unit: str
    current_value: float | None
    current_unit: str
    gap_value: float | None
    gap_pct: float | None
    status: str  # "AT_TARGET" | "ABOVE_TARGET" | "BELOW_TARGET" | "UNKNOWN"
    direction_is_good: bool | None
    compute_source: str
    last_observation: str


# ---------------------------------------------------------------------------
# Small helpers — local copies (kept identical to plan.py's slug heuristic).
# ---------------------------------------------------------------------------


def _slug(label: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_")[:40]


def _classify_status(
    *,
    target_value: float,
    current_value: float | None,
) -> tuple[str, float | None, float | None]:
    """Return (status, gap_value, gap_pct).

    AT_TARGET when |current - target| / |target| <= AT_TARGET_REL_TOLERANCE.
    """
    if current_value is None:
        return "UNKNOWN", None, None
    gap = current_value - target_value
    denom = abs(target_value) if target_value != 0 else 1.0
    gap_pct = (gap / denom) * 100.0
    if abs(gap_pct) <= AT_TARGET_REL_TOLERANCE * 100.0:
        return "AT_TARGET", gap, gap_pct
    if gap > 0:
        return "ABOVE_TARGET", gap, gap_pct
    return "BELOW_TARGET", gap, gap_pct


# ---------------------------------------------------------------------------
# DB readers — narrowly scoped so each section degrades on missing data.
# ---------------------------------------------------------------------------


def _latest_snapshot(session: Session, user_id: str) -> PortfolioSnapshotRow | None:
    return session.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(desc(PortfolioSnapshotRow.snapshot_date), desc(PortfolioSnapshotRow.id))
        .limit(1)
    ).scalar_one_or_none()


def _latest_household_budget_payload(session: Session, user_id: str) -> dict[str, Any]:
    """Parse the freshest household_budget agent_report.response_text.

    Returns {} on missing row or parse failure. Tolerates ```json fences``.
    """
    row = session.execute(
        select(AgentReport)
        .where(
            AgentReport.user_id == user_id,
            AgentReport.agent_role == "household_budget",
        )
        .order_by(desc(AgentReport.id))
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not row.response_text:
        return {}
    text = row.response_text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl >= 0:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        lo, hi = text.find("{"), text.rfind("}")
        if lo >= 0 and hi > lo:
            try:
                obj = json.loads(text[lo : hi + 1])
                return obj if isinstance(obj, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}


def _latest_concentration_payload(
    session: Session, user_id: str, decision_run_id: int | None
) -> dict[str, Any]:
    """Best-effort parse of the concentration agent_report for the draft's run.

    Returns {} when no row exists or response_text is unparseable.
    """
    if decision_run_id is None:
        return {}
    decision_id_str = f"plan-synth-{decision_run_id}"
    row = session.execute(
        select(AgentReport)
        .where(
            AgentReport.user_id == user_id,
            AgentReport.decision_id == decision_id_str,
            AgentReport.agent_role == "concentration",
        )
        .order_by(desc(AgentReport.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None or not row.response_text:
        return {}
    text = row.response_text
    brace = text.find("{")
    if brace < 0:
        return {}
    decoder = json.JSONDecoder(strict=False)
    try:
        obj, _ = decoder.raw_decode(text[brace:])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _user_context_fx(session: Session, user_id: str) -> float | None:
    """Return the manual fx_rate.usd_nis the user set, if any."""
    row = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    if row is None or not row.identity_yaml:
        return None
    try:
        import yaml

        data = yaml.safe_load(row.identity_yaml) or {}
    except Exception:  # noqa: BLE001
        return None
    fx = data.get("fx_rate") if isinstance(data, dict) else None
    if isinstance(fx, dict):
        v = fx.get("usd_nis")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Snapshot-derived primitives.
# ---------------------------------------------------------------------------


@dataclass
class _SnapshotMath:
    """Pre-computed numbers we lift off the latest portfolio snapshot.

    All NIS-denominated cash positions are converted via fx_usd_nis when
    available. Schwab-located holdings (heuristic: ``location`` contains
    'schwab') are summed for US-situs targets.
    """

    snapshot_date: str | None
    fx_usd_nis: float | None
    total_usd: float | None
    nvda_usd: float | None
    nvda_shares: float | None
    sgov_usd: float | None
    cash_usd: float | None
    us_situs_usd: float | None
    us_etf_aggregate_usd: float | None  # US-domiciled ETFs excl. NVDA
    positions: list[dict[str, Any]]


def _summarize_snapshot(
    snapshot: PortfolioSnapshotRow | None,
    *,
    fallback_fx: float | None,
) -> _SnapshotMath | None:
    """Roll the snapshot into the handful of scalars used by the target
    matcher below. Returns None when no snapshot exists.
    """
    if snapshot is None:
        return None
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        positions = []
    try:
        totals = json.loads(snapshot.totals_json or "{}")
    except json.JSONDecodeError:
        totals = {}

    total_usd_k = totals.get("total_usd_value_k")
    total_usd = float(total_usd_k) * 1000.0 if total_usd_k is not None else None
    fx = snapshot.fx_usd_nis or fallback_fx

    nvda_usd = 0.0
    nvda_shares: float | None = None
    sgov_usd = 0.0
    cash_usd = 0.0
    us_situs_usd = 0.0
    us_etf_aggregate_usd = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        sym = (p.get("symbol") or "").upper()
        atype = (p.get("asset_type") or "").lower()
        loc = (p.get("location") or "").lower()
        v_k = float(p.get("usd_value_k") or 0.0)
        v_usd = v_k * 1000.0
        if sym == "NVDA":
            nvda_usd += v_usd
            shr = p.get("shares")
            if isinstance(shr, (int, float)):
                nvda_shares = float(shr)
        elif sym == "SGOV":
            sgov_usd += v_usd
        elif atype == "cash" or sym == "-" or sym == "":
            cash_usd += v_usd
        # US-situs heuristic: anything held at Schwab.
        if "schwab" in loc:
            us_situs_usd += v_usd
        # US-domiciled ETF aggregate (excl. NVDA + SGOV — both have their
        # own dedicated targets in the medium-horizon plan; folding them
        # into this aggregate would double-count). Heuristic: ETF assets
        # held at Schwab or with an explicit US-domicile flag.
        if (
            sym not in ("NVDA", "SGOV", "")
            and atype in ("etf", "fund")
            and "schwab" in loc
        ):
            us_etf_aggregate_usd += v_usd

    return _SnapshotMath(
        snapshot_date=(
            snapshot.snapshot_date.isoformat() if snapshot.snapshot_date else None
        ),
        fx_usd_nis=float(fx) if fx else None,
        total_usd=total_usd,
        nvda_usd=nvda_usd if nvda_usd > 0 else None,
        nvda_shares=nvda_shares,
        sgov_usd=sgov_usd if sgov_usd > 0 else None,
        cash_usd=cash_usd if cash_usd > 0 else None,
        us_situs_usd=us_situs_usd if us_situs_usd > 0 else None,
        us_etf_aggregate_usd=(
            us_etf_aggregate_usd if us_etf_aggregate_usd > 0 else None
        ),
        positions=[p for p in positions if isinstance(p, dict)],
    )


# ---------------------------------------------------------------------------
# Per-target classifier.
# ---------------------------------------------------------------------------


# Sentinel reasons for UNKNOWN so the UI can render meaningful tooltips.
_REASON_NO_SNAPSHOT = "no portfolio snapshot"
_REASON_UNRECOGNIZED_LABEL = "label not recognized by classifier"
_REASON_UNSUPPORTED_UNIT = "unit not yet supported"
_REASON_NO_TOTAL = "snapshot has no total_usd_value_k"
_REASON_NO_FX = "no fx_usd_nis available"
_REASON_NO_HOUSEHOLD = "household_budget agent_report missing or unparseable"
_REASON_NO_CONCENTRATION = "concentration agent_report missing for this draft"


def _classify_one_target(
    *,
    item_id: str,
    target: dict[str, Any],
    snapshot_math: _SnapshotMath | None,
    household: dict[str, Any],
    concentration: dict[str, Any],
) -> TargetProgress:
    """Return a TargetProgress for one target dict from a horizon payload."""
    label = (target.get("label") or "").strip()
    label_lc = label.lower()
    unit = (target.get("unit") or "").strip().lower()
    raw_value = target.get("value")
    try:
        target_value = float(raw_value) if raw_value is not None else 0.0
    except (TypeError, ValueError):
        target_value = 0.0

    def _unknown(reason: str) -> TargetProgress:
        return TargetProgress(
            item_id=item_id,
            target_value=target_value,
            target_unit=unit,
            current_value=None,
            current_unit=unit,
            gap_value=None,
            gap_pct=None,
            status="UNKNOWN",
            direction_is_good=None,
            compute_source=reason,
            last_observation=reason,
        )

    # --- pct_of_portfolio / pct_of_net_worth -----------------------------
    if unit in ("pct_of_portfolio", "pct_of_net_worth"):
        if snapshot_math is None:
            return _unknown(_REASON_NO_SNAPSHOT)
        if snapshot_math.total_usd is None or snapshot_math.total_usd <= 0:
            return _unknown(_REASON_NO_TOTAL)

        # Figure out which sleeve this target is about.
        symbol_usd: float | None = None
        symbol_label: str | None = None
        if "nvda" in label_lc:
            symbol_usd = snapshot_math.nvda_usd
            symbol_label = "NVDA"
        elif "sgov" in label_lc:
            symbol_usd = snapshot_math.sgov_usd
            symbol_label = "SGOV"
        elif "us-domiciled" in label_lc or "us domiciled" in label_lc:
            symbol_usd = snapshot_math.us_etf_aggregate_usd
            symbol_label = "US-domiciled ETFs"
        else:
            return _unknown(_REASON_UNRECOGNIZED_LABEL)

        if symbol_usd is None:
            return _unknown(f"no {symbol_label} position in snapshot")
        current_pct = (symbol_usd / snapshot_math.total_usd) * 100.0
        direction_is_good = False  # pct-of-portfolio targets are ceilings
        status, gap, gap_pct = _classify_status(
            target_value=target_value, current_value=current_pct,
        )
        # "shares to sell" inverted-direction case handled by unit=shares
        # block below; pct-of-portfolio is always a ceiling in practice.
        last = (
            f"{symbol_label} {current_pct:.1f}% as of "
            f"{snapshot_math.snapshot_date or 'latest snapshot'}"
        )
        return TargetProgress(
            item_id=item_id,
            target_value=target_value,
            target_unit=unit,
            current_value=round(current_pct, 2),
            current_unit=unit,
            gap_value=round(gap, 2) if gap is not None else None,
            gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
            status=status,
            direction_is_good=direction_is_good,
            compute_source="portfolio_snapshot",
            last_observation=last,
        )

    # --- usd -------------------------------------------------------------
    if unit == "usd":
        if snapshot_math is None:
            return _unknown(_REASON_NO_SNAPSHOT)

        current_usd: float | None = None
        current_label: str | None = None
        direction_is_good: bool | None = None

        if (
            "defensive" in label_lc
            or ("sgov" in label_lc and "floor" in label_lc)
            or ("cash" in label_lc and ("sleeve" in label_lc or "floor" in label_lc))
        ):
            # Sleeve floor — SGOV + cash. Direction: above is good.
            sgov = snapshot_math.sgov_usd or 0.0
            cash = snapshot_math.cash_usd or 0.0
            current_usd = sgov + cash
            current_label = "defensive sleeve (SGOV + cash)"
            direction_is_good = True
        elif "us-domiciled" in label_lc or "us domiciled" in label_lc:
            current_usd = snapshot_math.us_etf_aggregate_usd
            current_label = "US-domiciled ETFs (excl. NVDA)"
            direction_is_good = False  # ceiling
        elif "us-situs" in label_lc or "us situs" in label_lc:
            current_usd = snapshot_math.us_situs_usd
            current_label = "US-situs holdings"
            direction_is_good = False
        elif "re-anchoring" in label_lc or "anchor" in label_lc:
            # Threshold (e.g. "NVDA price re-anchoring trigger" at $210).
            # Read current NVDA spot from the snapshot positions.
            current_price: float | None = None
            for p in snapshot_math.positions:
                if (p.get("symbol") or "").upper() == "NVDA":
                    price = p.get("current_price")
                    if isinstance(price, (int, float)):
                        current_price = float(price)
                        break
            current_usd = current_price
            current_label = "NVDA spot price"
            direction_is_good = None  # ambiguous; it's a threshold trigger
        else:
            return _unknown(_REASON_UNRECOGNIZED_LABEL)

        if current_usd is None:
            return _unknown(f"no value for {current_label}")
        status, gap, gap_pct = _classify_status(
            target_value=target_value, current_value=current_usd,
        )
        last = (
            f"{current_label} ${current_usd:,.0f} as of "
            f"{snapshot_math.snapshot_date or 'latest snapshot'}"
        )
        return TargetProgress(
            item_id=item_id,
            target_value=target_value,
            target_unit=unit,
            current_value=round(current_usd, 2),
            current_unit=unit,
            gap_value=round(gap, 2) if gap is not None else None,
            gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
            status=status,
            direction_is_good=direction_is_good,
            compute_source="portfolio_snapshot",
            last_observation=last,
        )

    # --- nis -------------------------------------------------------------
    if unit == "nis":
        # We don't track life-insurance face amount in the snapshot.
        # Surface as UNKNOWN with a clear hint so the UI shows the
        # "(live state pending: synthesis required)" sentence.
        if "life-insurance" in label_lc or "life insurance" in label_lc:
            return _unknown(
                "life-insurance face amount not tracked in snapshot",
            )
        return _unknown(_REASON_UNRECOGNIZED_LABEL)

    # --- shares ----------------------------------------------------------
    if unit == "shares":
        # Two flavours of share-count targets:
        #   * "shares to sell" / "deconcentration" — pull shares_sold_ytd
        #     from concentration agent_report. Direction: above is good.
        #   * "ending share count" / "share ceiling" — pull current shares
        #     from the snapshot. Direction: BELOW (ceiling) is good.
        if "to sell" in label_lc or "deconcentration" in label_lc:
            if not concentration:
                return _unknown(_REASON_NO_CONCENTRATION)
            pace = concentration.get("nvda_pace") or {}
            sold = pace.get("shares_sold_ytd")
            if not isinstance(sold, (int, float)):
                return _unknown("concentration payload has no shares_sold_ytd")
            current_shares = float(sold)
            direction_is_good = True
            status, gap, gap_pct = _classify_status(
                target_value=target_value, current_value=current_shares,
            )
            return TargetProgress(
                item_id=item_id,
                target_value=target_value,
                target_unit=unit,
                current_value=current_shares,
                current_unit=unit,
                gap_value=round(gap, 2) if gap is not None else None,
                gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
                status=status,
                direction_is_good=direction_is_good,
                compute_source="concentration agent_report.nvda_pace",
                last_observation=(
                    f"NVDA YTD sold: {int(current_shares)} shares "
                    f"(target {int(target_value)})"
                ),
            )

        # "ending share count" / "share ceiling" — current NVDA shares
        # held vs the gate target.
        if (
            "ending share" in label_lc
            or "share count" in label_lc
            or "share ceiling" in label_lc
        ):
            if snapshot_math is None or snapshot_math.nvda_shares is None:
                return _unknown("no NVDA shares in latest snapshot")
            current_shares = snapshot_math.nvda_shares
            direction_is_good = False  # ceiling
            status, gap, gap_pct = _classify_status(
                target_value=target_value, current_value=current_shares,
            )
            return TargetProgress(
                item_id=item_id,
                target_value=target_value,
                target_unit=unit,
                current_value=current_shares,
                current_unit=unit,
                gap_value=round(gap, 2) if gap is not None else None,
                gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
                status=status,
                direction_is_good=direction_is_good,
                compute_source="portfolio_snapshot",
                last_observation=(
                    f"NVDA holdings: {int(current_shares)} shares as of "
                    f"{snapshot_math.snapshot_date or 'latest snapshot'}"
                ),
            )

        return _unknown(_REASON_UNRECOGNIZED_LABEL)

    # --- months ----------------------------------------------------------
    if unit == "months":
        if snapshot_math is None:
            return _unknown(_REASON_NO_SNAPSHOT)
        burn_nis = household.get("monthly_burn_nis") if household else None
        if not isinstance(burn_nis, (int, float)) or burn_nis <= 0:
            return _unknown(_REASON_NO_HOUSEHOLD)
        fx = snapshot_math.fx_usd_nis
        if not fx:
            return _unknown(_REASON_NO_FX)
        sgov = snapshot_math.sgov_usd or 0.0
        cash = snapshot_math.cash_usd or 0.0
        defensive_nis = (sgov + cash) * fx
        current_months = defensive_nis / float(burn_nis)
        direction_is_good = True
        status, gap, gap_pct = _classify_status(
            target_value=target_value, current_value=current_months,
        )
        return TargetProgress(
            item_id=item_id,
            target_value=target_value,
            target_unit=unit,
            current_value=round(current_months, 2),
            current_unit=unit,
            gap_value=round(gap, 2) if gap is not None else None,
            gap_pct=round(gap_pct, 2) if gap_pct is not None else None,
            status=status,
            direction_is_good=direction_is_good,
            compute_source="snapshot + household_budget",
            last_observation=(
                f"{current_months:.1f} months runway "
                f"(defensive ₪{defensive_nis:,.0f} / burn ₪{int(burn_nis):,}/mo)"
            ),
        )

    # --- ratio / unknown -------------------------------------------------
    if unit == "ratio":
        return _unknown("hedge-ratio state not tracked in DB")

    return _unknown(_REASON_UNSUPPORTED_UNIT)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def _walk_targets(plan: PlanVersion) -> list[tuple[str, dict[str, Any]]]:
    """Yield (item_id, target_dict) pairs across all three horizons.

    item_id matches the synthesizer's slug convention so the UI can join
    these rows against DeltaItem.item_id.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for horizon, json_str in (
        ("long", plan.horizon_long_json),
        ("medium", plan.horizon_medium_json),
        ("short", plan.horizon_short_json),
    ):
        if not json_str:
            continue
        try:
            payload = json.loads(json_str)
        except json.JSONDecodeError:
            continue
        for t in payload.get("targets") or []:
            if not isinstance(t, dict):
                continue
            label = t.get("label") or ""
            if not label:
                continue
            synthetic_id = f"{horizon}.targets.{_slug(label)}"
            out.append((synthetic_id, t))
    return out


def compute_target_progress_for_plan(
    session: Session,
    *,
    user_id: str,
    plan: PlanVersion,
) -> list[TargetProgress]:
    """Build a TargetProgress for every target across the plan's horizons.

    Pure-ish: one snapshot read, one household_budget read, one
    concentration read, then per-target math. Each block falls back to
    UNKNOWN cleanly when its source data is missing.
    """
    fallback_fx = _user_context_fx(session, user_id)
    snapshot = _latest_snapshot(session, user_id)
    snapshot_math = _summarize_snapshot(snapshot, fallback_fx=fallback_fx)
    household = _latest_household_budget_payload(session, user_id)
    concentration = _latest_concentration_payload(
        session, user_id, plan.decision_run_id
    )

    out: list[TargetProgress] = []
    for item_id, target in _walk_targets(plan):
        try:
            out.append(
                _classify_one_target(
                    item_id=item_id,
                    target=target,
                    snapshot_math=snapshot_math,
                    household=household,
                    concentration=concentration,
                )
            )
        except Exception as exc:  # noqa: BLE001 — defensive; never crash the route
            logger.warning(
                "target_progress: classifier crashed for item_id=%s err=%s",
                item_id, exc,
            )
            out.append(
                TargetProgress(
                    item_id=item_id,
                    target_value=0.0,
                    target_unit=(target.get("unit") or "").lower(),
                    current_value=None,
                    current_unit=(target.get("unit") or "").lower(),
                    gap_value=None,
                    gap_pct=None,
                    status="UNKNOWN",
                    direction_is_good=None,
                    compute_source="classifier error",
                    last_observation="classifier error",
                )
            )
    return out


__all__ = [
    "AT_TARGET_REL_TOLERANCE",
    "TargetProgress",
    "compute_target_progress_for_plan",
]
