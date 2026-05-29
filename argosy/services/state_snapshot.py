"""State-snapshot collector + persister (Spec B commit #2).

Reads the user's full ``current_state`` into a single diff-able,
JSON-serialisable dict (six top-level sections per spec §1.2) and
persists it to the ``state_snapshots`` table (migration 0049 / ORM
``argosy.state.models.StateSnapshot``).

Public API
==========

* ``collect_state_snapshot(session, user_id, *, as_of=None)`` -- pure
  read; assembles the six-section dict.
* ``persist_state_snapshot(session, user_id, snapshot_date, state,
  source_versions)`` -- INSERT into ``state_snapshots``. Raises
  ``IntegrityError`` on ``(user_id, snapshot_date)`` collision so the
  caller decides whether to skip or update.
* ``get_latest_state_snapshot(session, user_id)`` -- newest row for the
  user (or ``None``).
* ``get_state_snapshot_by_date(session, user_id, snapshot_date)`` --
  exact-match lookup.
* ``state_snapshot_to_dict(row)`` -- ORM row → JSON-shaped dict
  (re-parses both JSON columns).

Six-section schema (§1.2)
=========================

Every section key is ALWAYS present, even when its underlying source
is missing -- the diff service (sibling commit #3) relies on this
invariant to distinguish "section was there, now gone" from "section
never existed."

Time-travel (§1.4)
==================

* ``as_of=None`` (default) -- live read. Every adapter sees today's
  date; ``source_versions['historical_replay_gaps']`` stays ``[]``.
* ``as_of=<past date>`` -- historical replay. Each source is asked
  for data at that date; if the source can't be reconstructed AND
  it's not a critical input, we record the gap in
  ``historical_replay_gaps`` and use the freshest available value.
  If a CRITICAL source (e.g. the plan version that was active on
  ``as_of``) can't be reconstructed, we raise ``StateReplayError``
  rather than silently producing a partial snapshot. The downstream
  observer agent downgrades severity one band on any field whose
  source is listed in ``historical_replay_gaps`` (spec §1.4 last
  paragraph).

Codex review focus (per task brief)
===================================

1. Multi-source assembly is read-only against persisted rows; the
   only DB write is the ``persist_state_snapshot`` call which adds
   ONE row. No cross-table mutation. Read isolation: each helper
   accepts the same ``session`` so a long-running collect sees one
   consistent SQLAlchemy unit-of-work (single connection in SQLite
   WAL mode -- per CLAUDE.md, no concurrent writers per user).

2. Time-travel plumbing: every collector helper accepts an
   ``as_of`` kwarg. Where the underlying service does NOT support
   one (BoI / FRED historical APIs aren't called from here in v1 to
   avoid a network dependency from a pure-read service), the helper
   records the gap explicitly and falls back to the live value
   (matching spec §1.4's "best-effort macro" stance).

3. JSON serialisation: ``_json_safe`` walks every emitted value and
   converts ``Decimal`` → ``float`` and ``datetime``/``date`` →
   ISO-8601 string. The state dict is round-tripped through
   ``json.dumps`` at persist time so any non-serialisable value
   raises ``TypeError`` immediately rather than silently corrupting
   the row.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.state.models import (
    NewsSignal,
    PlanVersion,
    PortfolioSnapshotRow,
    StateSnapshot,
)


# Codebase pins per spec Appendix A. Stamped on every snapshot so a
# future replay knows what code produced it; bumped explicitly when the
# underlying module changes meaningfully.
_BOI_CLIENT_VERSION = "2026-05-09"
_CASHFLOW_PROJECTION_VERSION = "2026-05-27"
_SCHEMA_MIGRATION_HEAD = "0049"
_NEWS_LOOKBACK_DAYS = 7


class StateReplayError(RuntimeError):
    """Raised when a CRITICAL source for a historical replay cannot be
    reconstructed.

    Examples of "critical": the plan_version that was active on the
    requested ``as_of`` date doesn't exist in the DB (no current/draft
    plan_version row with ``imported_at <= as_of``). Non-critical
    gaps (e.g. live FRED data for a 6-months-ago snapshot) are
    recorded in ``historical_replay_gaps`` and the collector
    continues.
    """


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Recursively convert ``value`` to a JSON-serialisable shape.

    Whitelist-only conversion (codex IMPORTANT integration):
    ``Decimal`` → ``float``; ``date``/``datetime`` → ISO-8601 string;
    nested dicts / lists / tuples / sets walked recursively. Anything
    that's already a primitive (str / int / float / bool / None)
    passes through unchanged. Unknown types raise ``TypeError`` so
    that a stale-DTO leak surfaces at COLLECT time, not silently
    smuggled through a ``str()`` fallback into the persisted JSON.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    raise TypeError(
        f"state_snapshot._json_safe: refusing to serialise unsupported "
        f"type {type(value).__name__!r} (value={value!r}). Whitelist: "
        f"None / bool / int / float / str / Decimal / date / datetime "
        f"/ dict / list / tuple / set / frozenset."
    )


# ---------------------------------------------------------------------------
# Section assemblers
# ---------------------------------------------------------------------------


def _collect_plan_inputs(
    session: Session,
    user_id: str,
    *,
    as_of: date | None,
    gaps: list[str],
) -> tuple[dict[str, Any], int | None]:
    """Build the ``plan_inputs`` section + return the source plan_version_id.

    Live mode (``as_of=None``): use the user's role='current' plan.
    Falls back to role='draft' when no current exists yet, then to
    role='baseline'. Returns ``({}, None)`` only when the user has no
    plan_version rows at all.

    Historical mode: pick the newest plan_version with
    ``imported_at <= as_of``. If none exists, raise
    ``StateReplayError`` (per spec §1.4 -- a plan with no baseline
    on the replay date is a critical gap, not a soft fallback).
    """
    from argosy.services import cashflow_projection as cf
    from argosy.services.wealth_dashboard import _load_user_context_yaml

    plan_row = _pick_plan_version(session, user_id, as_of=as_of)
    if plan_row is None:
        if as_of is not None:
            raise StateReplayError(
                f"No plan_version exists for user={user_id!r} on or before "
                f"as_of={as_of.isoformat()}; cannot reconstruct plan_inputs."
            )
        return {}, None

    # Synthesis assumptions live in ``synthesis_inputs_json`` (set by
    # the plan-synthesizer orchestrator on draft/current/superseded
    # rows). Baseline rows stash the distillate in ``distillate_json``
    # but the same fields aren't guaranteed -- so we tolerate missing
    # values and emit ``None`` rather than crashing.
    assumptions: dict[str, Any] = {}
    if plan_row.synthesis_inputs_json:
        try:
            assumptions = json.loads(plan_row.synthesis_inputs_json) or {}
        except json.JSONDecodeError:
            assumptions = {}

    ctx = _load_user_context_yaml(session, user_id)
    # Time-travel plumbing (codex BLOCKER #1 integration):
    #   - extract_household_state HAS a ``today`` kwarg; pass as_of so
    #     the household NIS spend / age / portfolio-NIS conversion
    #     reflects the historical date.
    #   - extract_pension_state does NOT accept a historical anchor
    #     (it reads CURRENT identity_yaml + the latest user context);
    #     for as_of != today we record an explicit replay gap rather
    #     than silently leaking present-day pension contributions.
    pension_state = cf.extract_pension_state(session, user_id)
    if as_of is not None:
        gaps.append(
            "plan_inputs.pension_state: extract_pension_state has no "
            f"as_of plumbing (live values used for as_of={as_of.isoformat()})"
        )
    household = cf.extract_household_state(session, user_id, today=as_of)
    if as_of is not None:
        # _latest_household_budget_report + _latest_snapshot inside
        # extract_household_state aren't time-traveled either; log
        # the household components that escape historical reads.
        gaps.append(
            "plan_inputs.household_budget: extract_household_state uses "
            "latest household_budget agent_report (no as_of filter); "
            f"historical replay for as_of={as_of.isoformat()} may include "
            "post-as_of revisions."
        )

    # Target allocation: dig out of identity_yaml's
    # ``allocation_target`` block (the structure synthesis writes).
    # Default to {} when absent so the field is always-present.
    target_alloc = {}
    raw_target = ctx.get("allocation_target") if isinstance(ctx, dict) else None
    if isinstance(raw_target, dict):
        for k, v in raw_target.items():
            try:
                target_alloc[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    # Severance fold-in is documented in extract_pension_state; expose
    # the combined NIS contribution rate as the assumed monthly income
    # mirror so the diff service can compare against realized income.
    monthly_income_nis = (
        pension_state.kupat_pensia_contribution_monthly_nis
        + pension_state.keren_hishtalmut_contribution_monthly_nis
    )

    plan_inputs = {
        "assumed_fx_usd_nis": float(household.fx_usd_nis),
        "assumed_mu_nominal_annual": _as_float(
            assumptions.get("mu_nominal_annual"), cf.DEFAULT_MU_NOMINAL_ANNUAL,
        ),
        "assumed_sigma_annual": _as_float(
            assumptions.get("sigma_annual"), cf.DEFAULT_SIGMA_ANNUAL,
        ),
        "assumed_inflation_annual": _as_float(
            assumptions.get("inflation_annual"), cf.DEFAULT_INFLATION_ANNUAL,
        ),
        "assumed_retirement_age": _as_float(
            assumptions.get("retirement_age"),
            _as_float(ctx.get("retirement_age") if isinstance(ctx, dict) else None,
                      67.0),
        ),
        "assumed_marginal_tax_rate": _as_float(
            assumptions.get("marginal_tax_rate"), cf.DEFAULT_TAX_RATE,
        ),
        "assumed_monthly_expenses_nis": float(household.monthly_expenses_nis),
        "assumed_monthly_income_nis": float(monthly_income_nis),
        "assumed_withdrawal_policy": str(
            assumptions.get("withdrawal_policy")
            or (ctx.get("withdrawal_policy") if isinstance(ctx, dict) else None)
            or "constant_real"
        ),
        "assumed_target_allocation": target_alloc,
        "plan_version_id": int(plan_row.id),
        "plan_version_role": str(plan_row.role or ""),
        "plan_version_label": str(plan_row.version_label or ""),
    }

    # Replay gap: assumptions block missing for historical row.
    if as_of is not None and not assumptions:
        gaps.append(
            f"plan_inputs.assumptions: plan_version id={plan_row.id} has no "
            f"synthesis_inputs_json (historical replay used defaults)."
        )

    return plan_inputs, int(plan_row.id)


def _pick_plan_version(
    session: Session,
    user_id: str,
    *,
    as_of: date | None,
) -> PlanVersion | None:
    """Pick the appropriate ``PlanVersion`` for the snapshot.

    Live (``as_of=None``): prefer role='current', then 'draft', then
    newest 'baseline'.

    Historical (``as_of`` set): newest row with
    ``imported_at <= as_of`` regardless of role (the historical state
    saw whatever plan was alive on that date).
    """
    if as_of is not None:
        # imported_at is a DateTime; compare against end-of-day on as_of.
        cutoff = datetime.combine(as_of, datetime.max.time())
        return session.execute(
            select(PlanVersion)
            .where(
                PlanVersion.user_id == user_id,
                PlanVersion.imported_at <= cutoff,
            )
            .order_by(desc(PlanVersion.imported_at))
            .limit(1)
        ).scalar_one_or_none()

    # Live: walk the role preference.
    for role in ("current", "draft", "baseline"):
        row = session.execute(
            select(PlanVersion)
            .where(
                PlanVersion.user_id == user_id,
                PlanVersion.role == role,
            )
            .order_by(desc(PlanVersion.imported_at))
            .limit(1)
        ).scalar_one_or_none()
        if row is not None:
            return row
    return None


def _collect_portfolio(
    session: Session,
    user_id: str,
    *,
    as_of: date | None,
    gaps: list[str],
) -> dict[str, Any]:
    """Build the ``portfolio`` section from the freshest persisted
    ``portfolio_snapshots`` row.

    Returns ``{}`` (NOT a missing key) when the table is empty for
    this user, so the diff service can detect "section is present but
    has no data" vs "section gone" -- per spec §1.2.

    Historical mode: pick the newest row with
    ``snapshot_date <= as_of`` (or ``imported_at <= as_of`` when
    ``snapshot_date`` is NULL, which the legacy ingest path tolerates).
    """
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )

    if as_of is None:
        row = get_latest_snapshot_row(session, user_id)
    else:
        # Historical (codex IMPORTANT #1 integration): prefer
        # ``snapshot_date`` -- the *business* date the portfolio is
        # claimed to be valid for -- when it's set; fall back to
        # ``imported_at`` only when snapshot_date is NULL (legacy
        # ingest path that didn't fill it). This avoids excluding a
        # valid past row that was imported LATER than as_of, and
        # avoids picking up rows whose business date is past as_of
        # just because imported_at happens to be earlier.
        from sqlalchemy import and_, or_
        cutoff = datetime.combine(as_of, datetime.max.time())
        row = session.execute(
            select(PortfolioSnapshotRow)
            .where(
                PortfolioSnapshotRow.user_id == user_id,
                or_(
                    and_(
                        PortfolioSnapshotRow.snapshot_date.is_not(None),
                        PortfolioSnapshotRow.snapshot_date <= as_of,
                    ),
                    and_(
                        PortfolioSnapshotRow.snapshot_date.is_(None),
                        PortfolioSnapshotRow.imported_at <= cutoff,
                    ),
                ),
            )
            # Sort by snapshot_date when present (newest business
            # date wins), tiebreak on imported_at then id for
            # determinism.
            .order_by(
                desc(PortfolioSnapshotRow.snapshot_date),
                desc(PortfolioSnapshotRow.imported_at),
                desc(PortfolioSnapshotRow.id),
            )
            .limit(1)
        ).scalar_one_or_none()

    if row is None:
        if as_of is not None:
            gaps.append(
                "portfolio: no portfolio_snapshots row on or before "
                f"as_of={as_of.isoformat()}"
            )
        return {}

    snap = row_to_snapshot(row)
    fx = float(row.fx_usd_nis or 0.0)
    # Prefer the persisted ``totals_json`` (written at ingest time
    # with the snapshot's own bookkeeping numbers) over re-computing
    # from positions -- positions may be sparse / dropped during
    # parser revisions while totals_json captures the snapshot's
    # claimed bottom line. Fall back to the position sum when
    # totals_json is missing or unparseable.
    total_usd_k = float(snap.total_usd_value_k)
    cash_usd_k = float(snap.cash_balances_usd_k())
    if row.totals_json:
        try:
            totals = json.loads(row.totals_json)
        except json.JSONDecodeError:
            totals = {}
        if isinstance(totals, dict):
            stored_total = totals.get("total_usd_value_k")
            if stored_total is not None:
                try:
                    total_usd_k = float(stored_total)
                except (TypeError, ValueError):
                    pass
            stored_cash = totals.get("cash_balances_usd_k")
            if stored_cash is not None:
                try:
                    cash_usd_k = float(stored_cash)
                except (TypeError, ValueError):
                    pass
    total_usd = total_usd_k * 1000.0
    cash_usd = cash_usd_k * 1000.0

    # Positions: keep only the fields the diff service / observer
    # actually need to pair against the plan. Strip raw_line / parser
    # crumbs to keep token budget low (spec §2.5).
    positions: list[dict[str, Any]] = []
    for p in snap.positions:
        shares = _as_float(p.shares, 0.0)
        value_usd = _as_float(p.usd_value_k, 0.0) * 1000.0
        value_nis = value_usd * fx if fx else 0.0
        positions.append({
            "ticker": str(p.symbol or "").strip() or None,
            "shares": shares,
            "value_usd": value_usd,
            "value_nis": value_nis,
            "asset_class": str(p.asset_type or "").strip() or None,
            "currency": str(p.currency or "").strip() or None,
        })

    allocations: list[dict[str, Any]] = []
    for a in snap.allocations:
        cur_k = _as_float(a.usd_value_k, 0.0)
        tar_k = _as_float(a.target_k, 0.0)
        cur_pct = _as_float(a.pct, 0.0)
        tar_pct = _as_float(a.target_pct, 0.0)
        allocations.append({
            "category": str(a.category or "").strip(),
            "current_pct": cur_pct,
            "target_pct": tar_pct,
            "current_k_usd": cur_k,
            "target_k_usd": tar_k,
        })

    # Top concentration: largest single-position % of total. Falls
    # back to 0.0 when total is zero (avoids ZeroDivisionError; the
    # diff service treats 0.0 vs 0.0 as no-deviation).
    top_concentration_pct = 0.0
    if total_usd > 0 and positions:
        top_val = max((p["value_usd"] or 0.0) for p in positions)
        top_concentration_pct = top_val / total_usd

    # Unallocated cash: delegated to the existing detector (which
    # already encodes the "cash above the target sleeve" rule).
    # Fallback to cash_usd when the detector isn't reachable.
    unallocated_cash_usd = _detect_unallocated_cash(session, user_id, cash_usd)

    return {
        "total_value_usd": total_usd,
        "cash_balances_usd": cash_usd,
        "positions": positions,
        "allocations": allocations,
        "top_concentration_pct": top_concentration_pct,
        "unallocated_cash_usd": unallocated_cash_usd,
        "snapshot_date": (
            row.snapshot_date.isoformat()
            if isinstance(row.snapshot_date, date) else None
        ),
        "fx_usd_nis_at_snapshot": fx or None,
    }


def _detect_unallocated_cash(
    session: Session, user_id: str, fallback_cash_usd: float,
) -> float:
    """Best-effort wrapper around the unallocated-cash detector.

    The detector module's signature has evolved across waves; we
    catch import / call failures and fall back to the raw cash
    balance so the snapshot never aborts on a stale wiring.
    """
    try:
        from argosy.services.unallocated_cash_detector import (  # type: ignore[attr-defined]
            detect_unallocated_cash,
        )
    except (ImportError, AttributeError):
        return float(fallback_cash_usd)
    try:
        result = detect_unallocated_cash(session, user_id)
    except Exception:  # noqa: BLE001 -- defensive; never crash collect
        return float(fallback_cash_usd)
    # Detector may return a dataclass / float / dict; pull the USD
    # number out best-effort.
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, dict):
        for k in ("unallocated_cash_usd", "amount_usd", "usd"):
            if k in result:
                try:
                    return float(result[k])
                except (TypeError, ValueError):
                    pass
    for attr in ("unallocated_cash_usd", "amount_usd", "usd"):
        if hasattr(result, attr):
            try:
                return float(getattr(result, attr))
            except (TypeError, ValueError):
                pass
    return float(fallback_cash_usd)


def _collect_macro(
    *,
    as_of: date | None,
    gaps: list[str],
    boi_adapter: Any | None,
    fred_adapter: Any | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the ``macro`` section.

    Adapters are passed in (not imported here at module level) so
    tests can stub the network surface without monkeypatching. The
    helpers below catch ``Exception`` per-field so one adapter going
    down doesn't take the whole section with it -- the offending
    field is recorded as a gap and emitted as ``None``.

    Historical mode caveat (spec §1.4): the BoI / FRED adapters DO
    have historical APIs (``fetch_range``, FRED series with
    ``start``/``end``), but in v1 we don't reach for them from the
    collector to keep the snapshot a read-only DB operation. A
    historical ``as_of`` falls back to today's spot AND records each
    field as a replay gap; the downstream observer downgrades
    severity one band on those fields. Backfill script (spec commit
    #5) can override this by passing already-fetched historical
    values.
    """
    historical = as_of is not None

    fx_spot = None
    fx_as_of = None
    if boi_adapter is not None:
        try:
            fx_payload = _maybe_await(boi_adapter.get_usd_nis(on_or_before=as_of))
            if isinstance(fx_payload, dict):
                fx_spot = _as_float(fx_payload.get("rate"), None)
                fx_as_of = fx_payload.get("as_of")
        except Exception as exc:  # noqa: BLE001 -- soft-fail
            gaps.append(f"macro.fx_usd_nis_spot: {type(exc).__name__}: {exc}")
    elif historical:
        gaps.append("macro.fx_usd_nis_spot: no boi_adapter and as_of is historical")
    else:
        gaps.append("macro.fx_usd_nis_spot: boi_adapter not provided")

    # The 30d average is best-effort -- if we have an adapter that
    # supports range fetches we could compute it; v1 leaves it None
    # and records the gap. The observer prompt notes "smoothed
    # average not available."
    fx_30d_avg = None
    gaps.append("macro.fx_usd_nis_30d_avg: 30d range fetch not implemented in v1")

    # FRED series. _fetch_fred_latest swallows errors per-field.
    fed_funds_rate = _fetch_fred_latest(fred_adapter, "DFF", gaps,
                                        field="macro.fed_funds_rate_pct")
    treasury_10y = _fetch_fred_latest(fred_adapter, "DGS10", gaps,
                                      field="macro.treasury_10y_yield_pct")
    sp500 = _fetch_fred_latest(fred_adapter, "SP500", gaps,
                               field="macro.sp500_index")
    nasdaq = _fetch_fred_latest(fred_adapter, "NASDAQCOM", gaps,
                                field="macro.nasdaq_index")
    vix = _fetch_fred_latest(fred_adapter, "VIXCLS", gaps, field="macro.vix")

    # Per-field replay gaps: when as_of is set, every macro value
    # was not actually time-traveled -- the live adapter call
    # returned today's value. Record so the observer downgrades.
    if historical:
        for f in ("fed_funds_rate_pct", "treasury_10y_yield_pct",
                  "sp500_index", "nasdaq_index", "vix"):
            gaps.append(
                f"macro.{f}: historical replay used live adapter "
                f"(today's value, not as_of={as_of.isoformat()})"
            )

    return {
        "fx_usd_nis_spot": fx_spot,
        "fx_usd_nis_30d_avg": fx_30d_avg,
        "fed_funds_rate_pct": fed_funds_rate,
        "treasury_10y_yield_pct": treasury_10y,
        "sp500_index": sp500,
        "sp500_30d_return_pct": None,  # 30d window not computed in v1
        "nasdaq_index": nasdaq,
        "nasdaq_30d_return_pct": None,
        "vix": vix,
        "fx_as_of": fx_as_of,
        "recent_high_materiality_news": [],  # populated below
        "recent_news_summary": {},
    }


def _maybe_await(coro_or_value: Any) -> Any:
    """If ``coro_or_value`` is a coroutine, drive it on a one-shot
    event loop and return the result; otherwise return as-is.

    Lets us call adapters that may be sync (tests) or async
    (production) from a sync collector without forcing the caller
    into asyncio land. Tests typically stub the adapter with a
    SimpleNamespace whose ``get_usd_nis`` returns a dict directly --
    we handle both.
    """
    import asyncio
    import inspect

    if inspect.iscoroutine(coro_or_value):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Nested-loop case (rare from collector). Spawn a new
                # loop on a worker thread.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, coro_or_value).result()
        except RuntimeError:
            pass
        return asyncio.run(coro_or_value)
    return coro_or_value


def _fetch_fred_latest(
    fred_adapter: Any | None,
    series_id: str,
    gaps: list[str],
    *,
    field: str,
) -> float | None:
    """Pull the most-recent non-null value from a FRED series.

    Defensive: any exception is captured, the field is recorded as a
    gap, and ``None`` is returned. The observer downgrades severity
    on flags touching None-valued fields.
    """
    if fred_adapter is None:
        gaps.append(f"{field}: fred_adapter not provided")
        return None
    try:
        rows = _maybe_await(fred_adapter.get_series(series_id))
    except Exception as exc:  # noqa: BLE001 -- soft-fail
        gaps.append(f"{field}: {type(exc).__name__}: {exc}")
        return None
    if not rows:
        gaps.append(f"{field}: empty series from FRED")
        return None
    for row in reversed(rows):
        val = row.get("value") if isinstance(row, dict) else None
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    gaps.append(f"{field}: all values in series were null")
    return None


def _collect_news_signals(
    session: Session,
    *,
    as_of: date | None,
    lookback_days: int = _NEWS_LOOKBACK_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull last-N-days high-materiality news signals.

    Returns ``(rows, summary)``:
      - rows: per spec §1.2 -- list of dicts the observer reads.
      - summary: keyword_counts / sentiment_dist / source_trust_dist
        derived stats so the LLM doesn't have to count manually.
    """
    anchor = datetime.combine(as_of, datetime.max.time()) if as_of else datetime.now(
        timezone.utc
    )
    # Filter naively on received_at -- spec §1.4 says historical
    # replay uses ``received_at <= as_of``.
    cutoff_low = anchor.timestamp() - lookback_days * 86400

    q = session.execute(
        select(NewsSignal)
        .where(NewsSignal.materiality == "high")
        .where(NewsSignal.received_at <= anchor)
        .order_by(desc(NewsSignal.received_at))
        .limit(200)
    ).scalars().all()

    rows: list[dict[str, Any]] = []
    keyword_counts: dict[str, int] = {}
    sentiment_dist: dict[str, int] = {}
    trust_dist: dict[str, int] = {}

    for s in q:
        ts = s.received_at
        if ts is None:
            continue
        if isinstance(ts, datetime):
            if ts.timestamp() < cutoff_low:
                continue
        try:
            tickers = json.loads(s.parsed_tickers or "[]")
        except json.JSONDecodeError:
            tickers = []
        try:
            kws = json.loads(s.event_keywords or "[]")
        except json.JSONDecodeError:
            kws = []
        rows.append({
            "news_signal_id": int(s.id),
            "source": str(s.source or ""),
            "parsed_tickers": [str(t) for t in tickers],
            "event_keywords": [str(k) for k in kws],
            "sentiment": str(s.sentiment or ""),
            "source_trust": str(s.source_trust or ""),
            "classifier_rationale": str(s.rationale or ""),
            "received_at": ts.isoformat() if isinstance(ts, datetime) else str(ts),
        })
        for k in kws:
            ks = str(k)
            keyword_counts[ks] = keyword_counts.get(ks, 0) + 1
        sentiment_dist[str(s.sentiment or "")] = sentiment_dist.get(
            str(s.sentiment or ""), 0,
        ) + 1
        trust_dist[str(s.source_trust or "")] = trust_dist.get(
            str(s.source_trust or ""), 0,
        ) + 1

    summary = {
        "keyword_counts": keyword_counts,
        "sentiment_dist": sentiment_dist,
        "source_trust_dist": trust_dist,
    }
    return rows, summary


def _collect_cashflow_recent(
    session: Session,
    user_id: str,
    *,
    as_of: date | None,
    gaps: list[str],
) -> dict[str, Any]:
    """Build the ``cashflow_recent`` section: last 3 months realized
    vs projected.

    Best-effort: when expense_dashboard / expense_ingest tables are
    empty (under-populated user), returns the three months with all
    figures at 0.0 rather than raising -- diff service treats zero
    as "no deviation" and the observer's allowlist won't fire.
    """
    from argosy.services.expense_dashboard import (
        _income_by_month_dict,
        _spending_by_month_dict,
    )

    anchor = as_of or date.today()
    months: list[str] = []
    y, m = anchor.year, anchor.month
    for _ in range(3):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()

    # Codex IMPORTANT #2 integration: these helpers do NOT accept
    # an as_of cutoff -- they aggregate every transaction regardless
    # of when it was posted. For a historical replay, a transaction
    # ingested AFTER ``as_of`` but with an occurred_on BEFORE
    # ``as_of`` will leak into the result. We filter by month-key
    # below (keeps results to the 3 month window we want anyway),
    # but ingest-time revisions (re-categorisation, refund matching)
    # can still mutate the aggregates. Record a replay gap so the
    # observer downgrades flags that touch this section.
    if as_of is not None:
        gaps.append(
            "cashflow_recent: expense_dashboard helpers have no "
            "as_of plumbing; revised/backfilled ledger entries may "
            "alter historical aggregates (live values used for "
            f"as_of={as_of.isoformat()})"
        )

    try:
        spend_by_month = _spending_by_month_dict(session, user_id, oneoff=False)
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"cashflow_recent.spend: {type(exc).__name__}: {exc}")
        spend_by_month = {}
    try:
        income_by_month = _income_by_month_dict(session, user_id)
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"cashflow_recent.income: {type(exc).__name__}: {exc}")
        income_by_month = {}

    # Projected = the plan_inputs.assumed_monthly_expenses_nis / income value
    # but section assemblers run independently; we re-derive from
    # extract_household_state (same source plan_inputs uses).
    from argosy.services.cashflow_projection import (
        extract_household_state,
        extract_pension_state,
    )
    try:
        household = extract_household_state(session, user_id, today=anchor)
        pension = extract_pension_state(session, user_id)
        projected_expense_nis = float(household.monthly_expenses_nis)
        projected_income_nis = float(
            pension.kupat_pensia_contribution_monthly_nis
            + pension.keren_hishtalmut_contribution_monthly_nis
        )
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"cashflow_recent.projection: {type(exc).__name__}: {exc}")
        projected_expense_nis = 0.0
        projected_income_nis = 0.0

    last_3: list[dict[str, Any]] = []
    cum_dev_nis = 0.0
    for mkey in months:
        realized_expense = float(spend_by_month.get(mkey, 0.0))
        realized_income = float(income_by_month.get(mkey, 0.0))
        dev_pct = None
        if projected_expense_nis > 0:
            dev_pct = (realized_expense - projected_expense_nis) / projected_expense_nis
        income_dev_pct = None
        if projected_income_nis > 0:
            income_dev_pct = (
                (realized_income - projected_income_nis) / projected_income_nis
            )
        cum_dev_nis += realized_expense - projected_expense_nis
        last_3.append({
            "month_yyyy_mm": mkey,
            "projected_expense_nis": projected_expense_nis,
            "realized_expense_nis": realized_expense,
            "deviation_pct": dev_pct,
            "projected_income_nis": projected_income_nis,
            "realized_income_nis": realized_income,
            "income_deviation_pct": income_dev_pct,
        })

    return {
        "last_3_months": last_3,
        "cumulative_deviation_nis": cum_dev_nis,
    }


def _collect_tax_assumptions(
    session: Session,
    user_id: str,
    plan_inputs: dict[str, Any],
    *,
    as_of: date | None,
    gaps: list[str],
) -> dict[str, Any]:
    """Build the ``tax_assumptions`` section.

    v1 reads only what's cheap + already in-DB: the assumed marginal
    rate (mirror of plan_inputs), plus best-effort effective rate
    from the most recent tax_analyst agent_report. Static brackets
    + the supplemental cap are emitted as ``None`` when not wired,
    with a gap recorded -- the observer downgrades severity.

    Time-travel (codex BLOCKER #2 integration): for ``as_of != None``
    we filter the tax_analyst report query by ``created_at <= as_of``
    so a future tax report can't leak into a historical snapshot.
    If no report exists on/before ``as_of``, ``eff_rate`` stays
    ``None`` and a replay-gap entry is recorded.
    """
    from argosy.services.cashflow_projection import DEFAULT_TAX_RATE
    from argosy.state.models import AgentReport

    assumed_rate = _as_float(plan_inputs.get("assumed_marginal_tax_rate"),
                             DEFAULT_TAX_RATE)

    # Most recent tax_analyst report (filtered by as_of when set);
    # pull effective rate if it's there. Tolerate any JSON shape.
    eff_rate = None
    q = select(AgentReport).where(
        AgentReport.user_id == user_id,
        AgentReport.agent_role == "tax_analyst",
    )
    if as_of is not None:
        cutoff = datetime.combine(as_of, datetime.max.time())
        # Use created_at for the historical filter -- it's the wall
        # clock the row was inserted at, which is the correct anchor
        # for "what did the user know at as_of".
        q = q.where(AgentReport.created_at <= cutoff)
    row = session.execute(
        q.order_by(desc(AgentReport.created_at), desc(AgentReport.id)).limit(1)
    ).scalar_one_or_none()
    if row is not None and row.response_text:
        try:
            txt = row.response_text.strip()
            if txt.startswith("```"):
                nl = txt.find("\n")
                if nl >= 0:
                    txt = txt[nl + 1:]
                if txt.endswith("```"):
                    txt = txt[:-3]
                txt = txt.strip()
            parsed = json.loads(txt)
        except (json.JSONDecodeError, AttributeError):
            parsed = None
        if isinstance(parsed, dict):
            for k in ("effective_rate_pct", "effective_rate",
                      "prior_year_effective_rate_pct"):
                if k in parsed:
                    eff_rate = _as_float(parsed[k], None)
                    break

    if eff_rate is None:
        gaps.append(
            "tax_assumptions.effective_rate_prior_year_pct: no tax_analyst "
            "report or unparseable response_text",
        )

    return {
        "current_marginal_bracket_pct": None,  # static-bracket lookup not wired in v1
        "effective_rate_prior_year_pct": eff_rate,
        "assumed_marginal_rate_pct": assumed_rate,
        "withholding_supplemental_cap_pct": None,  # static value not wired in v1
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_state_snapshot(
    session: Session,
    user_id: str,
    *,
    as_of: date | None = None,
    boi_adapter: Any | None = None,
    fred_adapter: Any | None = None,
    trigger_reason: str = "manual",
) -> dict[str, Any]:
    """Assemble the six-section ``current_state`` dict.

    Pure read against the SQLAlchemy session + the two optional
    adapters. Returns a JSON-shaped dict with EVERY top-level section
    key present (empty dict / empty list when the underlying source
    is missing). Designed so the diff service (sibling commit #3)
    can walk both ``current.state`` and the prior snapshot's state
    with identical structure.

    Returned shape (top-level keys, never absent):

        {
          "plan_inputs":     {...},  # may be {} if no plan_version exists
          "portfolio":       {...},  # may be {} if no portfolio_snapshot
          "macro":           {...},  # individual fields may be None
          "cashflow_recent": {...},  # always has last_3_months key
          "tax_assumptions": {...},  # always has the four canonical keys
          "metadata":        {...},  # snapshot_date + source_versions
        }

    Parameters
    ----------
    session : sqlalchemy Session
        DB session. Pure-read; no writes from this function.
    user_id : str
        The tenant.
    as_of : date, optional
        Historical replay anchor. ``None`` = today's snapshot.
    boi_adapter, fred_adapter : optional
        Inject adapters; production callers pass instantiated
        adapters, tests pass stubs (or None to exercise the
        gap-recording path).
    trigger_reason : str
        Stamped into ``source_versions['trigger_reason']``; the
        flag-writer / cool-off logic (spec §4.4) reads this. One of
        ``daily_cron`` / ``snapshot_upload`` / ``plan_resynthesis`` /
        ``backfill`` / ``manual`` (default).

    Raises
    ------
    StateReplayError
        When ``as_of`` is set and a CRITICAL source (currently: the
        plan_version active on ``as_of``) can't be reconstructed.
    """
    snapshot_date = as_of or date.today()
    gaps: list[str] = []

    # 1. Plan inputs first -- other sections cross-reference it.
    plan_inputs, plan_version_id = _collect_plan_inputs(
        session, user_id, as_of=as_of, gaps=gaps,
    )

    # 2. Portfolio.
    portfolio = _collect_portfolio(session, user_id, as_of=as_of, gaps=gaps)

    # 3. Macro (FX + FRED + news pipeline rows).
    macro = _collect_macro(
        as_of=as_of, gaps=gaps,
        boi_adapter=boi_adapter, fred_adapter=fred_adapter,
    )
    news_rows, news_summary = _collect_news_signals(session, as_of=as_of)
    macro["recent_high_materiality_news"] = news_rows
    macro["recent_news_summary"] = news_summary

    # 4. Cashflow recent.
    cashflow_recent = _collect_cashflow_recent(
        session, user_id, as_of=as_of, gaps=gaps,
    )

    # 5. Tax assumptions.
    tax_assumptions = _collect_tax_assumptions(
        session, user_id, plan_inputs, as_of=as_of, gaps=gaps,
    )

    # 6. Metadata + source_versions.
    metadata = {
        "snapshot_id": None,  # filled by persist_state_snapshot
        "user_id": user_id,
        "snapshot_date": snapshot_date.isoformat(),
        "plan_version_id": plan_version_id,
    }

    source_versions = {
        "schema_migration_head": _SCHEMA_MIGRATION_HEAD,
        "boi_client_version": _BOI_CLIENT_VERSION,
        "cashflow_projection_version": _CASHFLOW_PROJECTION_VERSION,
        "trigger_reason": trigger_reason,
        "historical_replay_gaps": list(gaps),
        "as_of": snapshot_date.isoformat(),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }

    state = {
        "plan_inputs": plan_inputs,
        "portfolio": portfolio,
        "macro": macro,
        "cashflow_recent": cashflow_recent,
        "tax_assumptions": tax_assumptions,
        "metadata": metadata,
    }

    # Round-trip through _json_safe so callers see a clean shape.
    # Decimal / datetime conversion happens here so the diff service
    # never encounters a Decimal.
    return _json_safe({"state": state, "source_versions": source_versions})


def persist_state_snapshot(
    session: Session,
    user_id: str,
    snapshot_date: date,
    state: dict[str, Any],
    source_versions: dict[str, Any],
) -> StateSnapshot:
    """INSERT a row into ``state_snapshots`` and return the persisted
    ORM object.

    Raises ``sqlalchemy.exc.IntegrityError`` when the
    ``(user_id, snapshot_date)`` UNIQUE constraint fires (caller
    decides: skip / update via a separate SQL).

    JSON serialisation: ``state`` and ``source_versions`` are
    round-tripped through ``json.dumps`` with ``default=str``. Any
    non-serialisable value surfaces here as ``TypeError`` -- callers
    should NOT pass Decimals / datetimes directly; use
    ``collect_state_snapshot``'s return shape (already
    JSON-cleaned).
    """
    # Codex IMPORTANT integration: fail-fast on non-serialisable
    # leaks. _json_safe is the WHITELIST-only converter; json.dumps
    # here has NO ``default=`` fallback so anything that survives
    # _json_safe but isn't a JSON primitive raises ``TypeError``
    # rather than silently being stringified.
    safe_state = _json_safe(state)
    safe_versions = _json_safe(source_versions)
    row = StateSnapshot(
        user_id=user_id,
        snapshot_date=snapshot_date,
        state_json=json.dumps(safe_state, separators=(",", ":")),
        source_versions_json=json.dumps(
            safe_versions, separators=(",", ":"),
        ),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_latest_state_snapshot(
    session: Session, user_id: str,
) -> StateSnapshot | None:
    """Newest snapshot for ``user_id`` by ``snapshot_date`` (DESC,
    breaking ties by ``id`` DESC -- daily cron pattern means at most
    one row per date per user, but the tiebreak is cheap defense in
    depth)."""
    return session.execute(
        select(StateSnapshot)
        .where(StateSnapshot.user_id == user_id)
        .order_by(desc(StateSnapshot.snapshot_date), desc(StateSnapshot.id))
        .limit(1)
    ).scalar_one_or_none()


def get_state_snapshot_by_date(
    session: Session, user_id: str, snapshot_date: date,
) -> StateSnapshot | None:
    """Exact-match lookup. Returns None when no snapshot exists for
    that ``(user_id, snapshot_date)`` pair."""
    return session.execute(
        select(StateSnapshot)
        .where(
            StateSnapshot.user_id == user_id,
            StateSnapshot.snapshot_date == snapshot_date,
        )
        .limit(1)
    ).scalar_one_or_none()


def state_snapshot_to_dict(row: StateSnapshot) -> dict[str, Any]:
    """Convert a persisted ``StateSnapshot`` row back into the
    six-section dict shape ``collect_state_snapshot`` returns.

    Parses both ``state_json`` and ``source_versions_json``; if
    either is corrupt, the corresponding key is emitted as an empty
    dict (the snapshot is still surfaceable for audit even if its
    state isn't). Stamps ``state['metadata']['snapshot_id']`` from
    the row's PK so downstream callers don't need to plumb it
    separately.
    """
    try:
        state = json.loads(row.state_json or "{}")
    except json.JSONDecodeError:
        state = {}
    try:
        source_versions = json.loads(row.source_versions_json or "{}")
    except json.JSONDecodeError:
        source_versions = {}
    if isinstance(state, dict) and isinstance(state.get("metadata"), dict):
        state["metadata"]["snapshot_id"] = int(row.id)
    return {
        "id": int(row.id),
        "user_id": str(row.user_id),
        "snapshot_date": row.snapshot_date.isoformat()
            if isinstance(row.snapshot_date, date) else None,
        "created_at": row.created_at.isoformat()
            if isinstance(row.created_at, datetime) else None,
        "state": state,
        "source_versions": source_versions,
    }


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: Any) -> Any:
    """Coerce ``value`` to ``float`` or return ``default``. Accepts
    ``None`` / strings / Decimals / ints / floats; returns ``default``
    (typed identically to whatever the caller passed) on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "StateReplayError",
    "collect_state_snapshot",
    "persist_state_snapshot",
    "get_latest_state_snapshot",
    "get_state_snapshot_by_date",
    "state_snapshot_to_dict",
]
