"""DB-state → decision-packet assembly — the ONE wiring every deploy-author
caller shares.

``build_decision_packet`` is pure; this module does the impure half: gather the
real NVDA look-through, the canonical current-vs-target sleeve attribution, the
live market/macro regime and fresh per-candidate research from the DB/network,
then shape them into the packet. Extracted verbatim from the ``/deploy-cash``
route so the daily period-directive job feeds the author the SAME holistic view
the on-demand route does — two callers, one packet contract.

Every enrichment is best-effort: a failed sub-fetch logs and degrades that field,
never blocks the packet.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from argosy.logging import get_logger

_log = get_logger("argosy.allocation_author.packet_assembly")


def assemble_author_packet(
    db: Any,
    *,
    user_id: str,
    doc: Any,
    holdings_usd: dict[str, float],
    cash_usd: float,
    deployable_usd: float,
    additional_user_constraints: str = "",
) -> dict[str, Any]:
    """Assemble the full deployment-author decision packet from live state.

    ``doc`` is the canonical ``TargetAllocationDoc``; ``holdings_usd`` the current
    tradeable book; ``deployable_usd`` the net-of-tax amount to place. ``db`` is a
    sync Session used for the snapshot-bound enrichments.
    """
    from argosy.services.allocation_author.packet import build_decision_packet
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )

    # Feed REAL NVDA look-through (CSPX/R1GR/FWRA re-buying NVDA is invisible
    # to raw holdings["NVDA"]) so the author reasons over TRUE concentration —
    # a core reason the deterministic engine lost. Best-effort.
    _nvda_ltv = None
    _book = None
    try:
        from argosy.services.deployment_funnel.from_plan import build_gate_inputs
        _gi = build_gate_inputs(doc=doc, holdings_usd=holdings_usd, cash_usd=cash_usd)
        _nvda_ltv = _gi.current_effective_nvda_usd
        _book = _gi.book_usd
    except Exception as exc:  # noqa: BLE001 — fall back to raw holdings NVDA
        _log.warning("author_packet.lookthrough_failed", error=str(exc)[:120])
    # Canonical current-vs-target attribution (look-through aware) so the
    # author fills the real under-target sleeves — plan-fit from within.
    _cur_by_sleeve: dict[str, float] = {}
    _snapshot_row = None
    try:
        from argosy.services.allocation_breakdown import build_allocation_breakdown
        from argosy.services.instrument_plan_class import load_classification_map

        _snapshot_row = get_latest_snapshot_row(db, user_id)
        if _snapshot_row is not None:
            _classification_map = load_classification_map(db, user_id)
            for _cb in build_allocation_breakdown(
                row_to_snapshot(_snapshot_row),
                doc,
                classification_map=_classification_map,
            ):
                _cur_by_sleeve[_cb.label] = float(_cb.current_pct)
    except Exception as exc:  # noqa: BLE001 — gaps are best-effort
        _log.warning("author_packet.breakdown_failed", error=str(exc)[:120])
    # Live market/macro regime so the author reasons about the current
    # environment (equity-vs-bond, US-vs-exUS) instead of deploying blind.
    # Best-effort; the author still runs if this is unavailable.
    _market_signals: dict = {}
    try:
        from argosy.services.deployment_market_context import (
            assemble_deployment_market_context,
        )
        _mc = assemble_deployment_market_context(db)
        _market_signals = {
            "as_of": _mc.overall_age_label,
            "is_stale": _mc.is_any_stale,
            "snapshot": {k: float(v) for k, v in _mc.snapshot.items()},
        }
        if _mc.nvda is not None:
            _market_signals["nvda_quote"] = {
                "price": _mc.nvda.price, "consistent": _mc.nvda.consistent,
            }
    except Exception as exc:  # noqa: BLE001 — market ctx is best-effort
        _log.warning("author_packet.market_context_failed", error=str(exc)[:120])
    # Fetch-before-buy: fresh per-candidate research (live news + price) on
    # the INDIVIDUAL-STOCK candidates (the moonshot / single-name sleeves),
    # where fresh diligence matters most — broad diversified ETFs don't need
    # per-name news. Best-effort + bounded; author reasons over CURRENT data
    # instead of a static menu. Absent research leaves the packet unchanged.
    _candidate_research: dict[str, str] = {}
    _discovery_candidates: list[dict[str, Any]] = []
    _discovery_symbols: list[str] = []
    _current_recommendations: list[dict[str, Any]] = []
    _recommendation_buy_symbols: list[str] = []
    _decision_calibration: dict[str, Any] = {}
    _staged_sell_policies: dict[str, dict[str, Any]] = {}
    try:
        from sqlalchemy import select

        from argosy.state.models import ScanState
        from argosy.services.allocation_research import pending_tasks

        _cutoff = datetime.now(UTC) - timedelta(days=3)
        _pending_symbols = {
            ticker for task in pending_tasks(db, user_id)
            for ticker in json.loads(task.payload_json).get("tickers", [])
        }
        _scan_rows = db.execute(
            select(ScanState)
            .where(ScanState.user_id == user_id,
                   (ScanState.status == "active") |
                   ((ScanState.status == "dropped") & ScanState.ticker.in_(_pending_symbols)))
            .order_by(ScanState.rank.asc(), ScanState.last_score.desc())
        ).scalars().all()
        for _row in _scan_rows:
            _seen = _row.last_fleet_at or _row.last_estimated_at or _row.last_seen_at
            if _seen is None:
                continue
            _seen_aware = _seen.replace(tzinfo=UTC) if _seen.tzinfo is None else _seen
            if _seen_aware < _cutoff:
                continue
            _ticker = _row.ticker.strip().upper()
            if not _ticker:
                continue
            _discovery_symbols.append(_ticker)
            _discovery_candidates.append(
                {
                    "ticker": _ticker,
                    "search_source": ("trend_scan_state" if _row.status == "active"
                                      else "allocation_research_followup"),
                    "radar_status": _row.status,
                    "radar_as_of": _row.last_radar_at.isoformat() if _row.last_radar_at else None,
                    "score": float(_row.last_score),
                    "rank": _row.rank,
                    "fresh_as_of": _seen_aware.isoformat(),
                    "nomination": json.loads(_row.nomination_evidence_json or "null"),
                    "estimator": json.loads(_row.estimator_json or "null"),
                    "fleet": json.loads(_row.fleet_json or "null"),
                }
            )
            _candidate_research[_ticker] = (
                ("fresh search-origin candidate; " if _row.status == "active" else
                 "pending research follow-up; dropped from current radar, NOT reactivated; ")
                + f"score={float(_row.last_score):.2f}; rank={_row.rank}; "
                f"estimator={(_row.estimator_json or 'null')[:600]}; "
                f"fleet={(_row.fleet_json or 'null')[:600]}"
            )
    except Exception as exc:  # noqa: BLE001 - absence is explicit in packet
        _log.warning("author_packet.discovery_load_failed", error=str(exc)[:120])
    try:
        from argosy.services.current_recommendations import (
            load_actionable_recommendations,
        )

        _current_recommendations = load_actionable_recommendations(
            db,
            user_id=user_id,
        )
        _recommendation_buy_symbols = [
            str(row["ticker"]).upper()
            for row in _current_recommendations
            if row.get("action") == "buy"
        ]
    except Exception as exc:  # noqa: BLE001 - explicit empty input on failure
        _log.warning(
            "author_packet.current_recommendations_load_failed",
            error=str(exc)[:120],
        )
    # Join the existing NVDA glide, actual-sale ledger and Section-102 engine.
    # The author receives ONE current tranche boundary, not a bare TRIM verb or
    # the full over-cap liquidation amount. Missing live price/FX leaves the
    # staged policy absent, which means the author cannot safely fund a sell.
    try:
        from argosy.services.staged_sell_policy import (
            build_nvda_staged_sell_policy,
        )

        _nvda_price = float(
            ((_market_signals.get("nvda_quote") or {}).get("price") or 0.0)
        )
        _fx_usd_nis = float(
            ((_market_signals.get("snapshot") or {}).get("usd_nis") or 0.0)
        )
        if _snapshot_row is not None:
            _fx_usd_nis = _fx_usd_nis or float(
                getattr(_snapshot_row, "fx_usd_nis", 0.0) or 0.0
            )
            if _nvda_price <= 0:
                for _position in json.loads(_snapshot_row.positions_json or "[]"):
                    if str(_position.get("symbol") or "").upper() == "NVDA":
                        _nvda_price = float(
                            _position.get("current_price") or 0.0
                        )
                        if _nvda_price > 0:
                            break
        _nvda_policy = build_nvda_staged_sell_policy(
            db,
            user_id=user_id,
            current_price_usd=_nvda_price,
            fx_usd_nis=_fx_usd_nis,
            current_nvda_value_usd=float(holdings_usd.get("NVDA", 0.0)),
            current_effective_nvda_value_usd=(
                float(_nvda_ltv) if _nvda_ltv is not None else None
            ),
            book_usd=float(_book or sum(holdings_usd.values())),
        )
        if _nvda_policy is not None:
            _staged_sell_policies["NVDA"] = _nvda_policy
    except Exception as exc:  # noqa: BLE001 - failure is explicit to author logs
        _log.warning(
            "author_packet.staged_sell_policy_failed",
            error=str(exc)[:160],
        )
    try:
        from argosy.services.stock_decision.fetchers import (
            news_fetcher,
            price_fetcher,
        )
        _single_name_syms: list[str] = []
        for _c in getattr(doc, "classes", []) or []:
            if (getattr(_c, "snapshot_category", "") or "") != "Individual Stocks":
                continue  # only single-name sleeves (high-growth + NVDA)
            for _i in getattr(_c, "instruments", []) or []:
                _s = (getattr(_i, "symbol", "") or "").strip()
                if _s and _s.upper() != "NVDA":  # NVDA won't be bought (over cap)
                    _single_name_syms.append(_s)
        # Discovery rows already carry fresh, persisted estimator/fleet evidence
        # above. Do not refetch every radar name here: the selected name receives
        # a live quote/profile at the executable order-sheet boundary.
        for _s in dict.fromkeys(_single_name_syms):  # dedup, preserve order
            _parts = []
            _p = price_fetcher(_s)
            _n = news_fetcher(_s)
            if _p:
                _parts.append(_p)
            if _n:
                _parts.append("news: " + _n[:200])
            if _parts:
                _candidate_research[_s] = " | ".join(_parts)
    except Exception as exc:  # noqa: BLE001 — research is additive/best-effort
        _log.warning("author_packet.candidate_research_failed", error=str(exc)[:120])
    # Close the self-audit loop: graded predictions must reach the next author,
    # not merely accumulate in a ledger/dashboard. This is judgment context,
    # never a per-source or per-ticker deterministic veto.
    try:
        from argosy.services.predictions.reliability import (
            get_source_reliability,
            recent_verdict_call_outcomes,
            reliability_annotation,
        )

        _rows = get_source_reliability(db, user_id)
        _relevant = [
            r for r in _rows
            if r.source.startswith("signal_stream:")
            or r.source in {
                "internal_per_position_thesis",
                "internal_news_signal_analyst",
                "internal_state_observer",
            }
        ]
        _relevant.sort(
            key=lambda r: (int(r.scored_predictions), int(r.total_predictions)),
            reverse=True,
        )
        _decision_calibration = {
            "order_sheet": reliability_annotation(
                db,
                user_id,
                "signal_stream:order_sheet",
                method_family="fixed_lookahead",
            ),
            "sources": [
                {
                    "source": r.source,
                    "method_family": r.method_family,
                    "scored": int(r.scored_predictions),
                    "hit_rate": r.hit_rate,
                    "mean_pnl_pct": r.mean_pnl_pct,
                    "sample_size_warning": bool(r.sample_size_warning),
                    "is_stale": bool(r.is_stale),
                }
                for r in _relevant[:12]
            ],
            "recent_verdict_outcomes": [
                {
                    "ticker": o.subject,
                    "verdict": o.verdict,
                    "grade": o.verdict_grade,
                    "price_move_pct": o.price_move_pct,
                    "evaluated_at": (
                        o.evaluated_at.isoformat() if o.evaluated_at else None
                    ),
                }
                for o in recent_verdict_call_outcomes(db, user_id, limit=8)
            ],
        }
        from sqlalchemy import select

        from argosy.services.predictions.benchmark import BENCHMARK_SYMBOL, BENCHMARK_VERSION
        from argosy.services.predictions.outcomes import authoritative_outcome_ids
        from argosy.state.models import (
            Prediction,
            PredictionBenchmarkOutcome,
            PredictionOutcome,
        )

        _benchmark_rows = db.execute(
            select(PredictionBenchmarkOutcome, Prediction)
            .join(
                PredictionOutcome,
                PredictionOutcome.id
                == PredictionBenchmarkOutcome.prediction_outcome_id,
            )
            .join(Prediction, Prediction.id == PredictionOutcome.prediction_id)
            .where(
                Prediction.user_id == user_id,
                Prediction.archived == 0,
                PredictionOutcome.id.in_(authoritative_outcome_ids()),
                PredictionBenchmarkOutcome.benchmark_version == BENCHMARK_VERSION,
                PredictionBenchmarkOutcome.benchmark_symbol == BENCHMARK_SYMBOL,
                Prediction.source.in_((
                    "signal_stream:deep_decision_verdict",
                    "signal_stream:order_sheet",
                    "signal_stream:discovery_evaluation",
                )),
            )
            .order_by(
                PredictionBenchmarkOutcome.evaluated_at.desc(),
                PredictionBenchmarkOutcome.id.desc(),
            )
            .limit(100)
        ).all()
        _excesses = [
            float(benchmark.decision_excess_return_pct)
            for benchmark, _prediction in _benchmark_rows
        ]
        _beats = sum(value > 0.001 for value in _excesses)
        _lags = sum(value < -0.001 for value in _excesses)
        _recent_benchmark_outcomes = []
        for _benchmark, _prediction in _benchmark_rows[:8]:
            try:
                _ref = json.loads(_prediction.source_ref or "{}")
                if not isinstance(_ref, dict):
                    _ref = {}
            except (TypeError, ValueError):
                _ref = {}
            _recent_benchmark_outcomes.append(
                {
                    "ticker": _prediction.ticker,
                    "recommendation": str(
                        _ref.get("verdict") or _ref.get("action") or _prediction.direction
                    ).upper(),
                    "subject_return_pct": float(_benchmark.subject_return_pct),
                    "benchmark_return_pct": float(_benchmark.benchmark_return_pct),
                    "decision_excess_return_pct": float(
                        _benchmark.decision_excess_return_pct
                    ),
                    "evaluated_at": _benchmark.evaluated_at.isoformat(),
                }
            )
        _decision_calibration["benchmark"] = {
            "symbol": "SPY",
            "version": BENCHMARK_VERSION,
            "compared": len(_excesses),
            "beats": _beats,
            "lags": _lags,
            "beat_rate": (
                _beats / (_beats + _lags) if _beats + _lags else None
            ),
            "mean_excess_return_pct": (
                sum(_excesses) / len(_excesses) if _excesses else None
            ),
            "recent": _recent_benchmark_outcomes,
        }
    except Exception as exc:  # noqa: BLE001 - additive calibration context
        _log.warning("author_packet.calibration_failed", error=str(exc)[:120])
    packet = build_decision_packet(
        doc=doc, holdings_usd=holdings_usd, deployable_usd=deployable_usd,
        cash_usd=cash_usd,
        nvda_cap_pct=float(getattr(doc, "nvda_cap_pct", 0.0) or 0.0),
        nvda_lookthrough_usd=_nvda_ltv, book_usd=_book,
        current_pct_by_sleeve=_cur_by_sleeve,
        policy_signals=_market_signals,
        candidate_research=_candidate_research,
        extra_known_symbols=set(_discovery_symbols) | set(_recommendation_buy_symbols),
        discovery_candidates=_discovery_candidates,
        current_recommendations=_current_recommendations,
        decision_calibration=_decision_calibration,
        user_constraints=(
            "Earliest safe retirement is the prime directive. NVDA single-name "
            "over-concentration is handled by the plan's SCHEDULED SELLS, not by "
            "refusing equity buys — so fill the plan's under-target sleeves by "
            "gap, INCLUDING its US-equity sleeves. Judge concentration from the "
            "trade's before/after whole-portfolio look-through: a diversified ETF "
            "holding some NVDA can still materially dilute direct NVDA exposure. "
            "Constituent presence and the mechanical conversion of zero-exposure cash "
            "are not vetoes; judge the funded plan including scheduled single-stock "
            "sales, and block only if the chosen instrument/size makes the aggregate "
            "cap infeasible. Prefer Irish UCITS / estate-safe instruments. "
            + additional_user_constraints.strip()
        ),
    )
    packet["staged_sell_policies"] = _staged_sell_policies
    from argosy.services.allocation_research import research_context

    packet["allocation_research_tasks"] = research_context(db, user_id)
    try:
        from sqlalchemy import select

        from argosy.state.models import Lot, TaxSimulationLot

        lot_rows = db.execute(
            select(Lot).where(Lot.user_id == user_id)
        ).scalars().all()
        complete_lot_symbols: set[str] = set()
        grouped_lots: dict[str, list[Lot]] = {}
        for row in lot_rows:
            grouped_lots.setdefault(str(row.ticker or "").upper(), []).append(row)
        for symbol, rows in grouped_lots.items():
            if rows and all(
                float(row.quantity or 0.0) > 0
                and float(row.cost_basis_usd or 0.0) > 0
                for row in rows
            ):
                complete_lot_symbols.add(symbol)
        nvda_tax_sim_available = db.execute(
            select(TaxSimulationLot.id)
            .where(TaxSimulationLot.user_id == user_id)
            .limit(1)
        ).first() is not None
        packet["tax_lot_coverage"] = {
            symbol.upper(): {
                "available": (
                    symbol.upper() in complete_lot_symbols
                    or (symbol.upper() == "NVDA" and nvda_tax_sim_available)
                ),
                "meaning": (
                    "after-tax proceeds can be verified from the authoritative "
                    "Section-102/ESPP simulation"
                    if symbol.upper() == "NVDA" and nvda_tax_sim_available
                    else (
                        "after-tax proceeds can be verified from complete imported lots"
                        if symbol.upper() in complete_lot_symbols
                        else "cost basis missing/incomplete; after-tax proceeds cannot be verified"
                    )
                ),
            }
            for symbol in holdings_usd
        }
    except Exception as exc:  # noqa: BLE001 - absence remains explicit
        _log.warning("author_packet.tax_lot_coverage_failed", error=str(exc)[:120])
        packet["tax_lot_coverage"] = {}
    return packet


__all__ = ["assemble_author_packet"]
