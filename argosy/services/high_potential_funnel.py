"""High-potential discovery funnel (Slice 2): radar -> smart-refresh diff ->
Sonnet estimator -> top-K Opus fleet grade -> persist ScanState.

Smart refresh (codex #8): each radar candidate gets a ``radar_fingerprint``
(score + families + liquidity bucket). A candidate is re-estimated only when its
fingerprint moved OR its cached estimate is older than the TTL; otherwise the
stored EstimatorVerdict is reused (no LLM call). The same freshness rule gates
the expensive fleet grade. Tickers that fall off the radar are marked
``dropped`` (TTL-evicted), so the GET surface can filter them.

The radar/estimator/grader/persistence seams are module-level so they can be
stubbed in tests and swapped without touching the orchestration.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from argosy.logging import get_logger
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.trend_radar import ScanResult, TrendCandidate

log = get_logger(__name__)

ESTIMATE_TTL = timedelta(hours=24)
FLEET_TTL = timedelta(days=3)
FLEET_RESEARCH_CONTRACT = "dated-news-and-explicit-limitations-v1"
TOP_K_TO_FLEET = 5
_CONVICTION_RANK = {"HIGH": 3, "MED": 2, "LOW": 1}


@dataclass(frozen=True)
class FunnelResult:
    picks: list[FleetPick]
    estimated: list[EstimatorVerdict]
    radar: list[TrendCandidate]
    last_refreshed_at: str


# --- seams (stubbable) -----------------------------------------------------

def _scan_radar() -> ScanResult:
    from argosy.services.trend_radar import scan_trends
    return scan_trends()


def _external_rows(user_id: str, status: str):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.models import ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(
        bind=create_engine(url, connect_args={"check_same_thread": False})
    )
    try:
        with factory() as db:
            return list(
                db.execute(
                    select(ScanState).where(
                        ScanState.user_id == user_id,
                        ScanState.status == status,
                        ScanState.nomination_evidence_json.is_not(None),
                    )
                ).scalars()
            )
    except Exception as exc:  # noqa: BLE001 - external stream is additive
        log.warning(
            "high_potential_funnel.external_rows_unavailable",
            status=status,
            error=str(exc)[:200],
        )
        return []


def _load_external_candidates(user_id: str) -> list[TrendCandidate]:
    out: list[TrendCandidate] = []
    for row in _external_rows(user_id, "active"):
        try:
            payload = json.loads(row.nomination_evidence_json)
            if not isinstance(payload, dict):
                raise ValueError("nomination must be an object")
        except (TypeError, ValueError):
            log.warning("high_potential_funnel.external_evidence_invalid", ticker=row.ticker)
            continue
        # Market/provider/numeric errors are NOT malformed-source omissions.
        # Keep this outside schema tolerance so the registered run fails loudly.
        if payload.get("research_mandate") == "general":
            continue  # Shared research requests use the general decision funnel.
        if str(payload.get("stream") or "").startswith("ingest_"):
            out.append(_ingest_candidate(row, payload))
            continue
        try:
            evidence = payload["evidence"]
            price = evidence.get("price")
            avg_volume = evidence.get("average_volume")
            out.append(
                TrendCandidate(
                    ticker=row.ticker,
                    name=row.ticker,
                    score=float(payload["strength"]) * 100,
                    families=(f"SIGNAL_STREAM:{payload['stream']}",),
                    reasons=(
                        f"{payload['stream']}: {payload['dedup_key']}",
                    ),
                    price=float(price) if price is not None else None,
                    market_cap=(
                        float(evidence["market_cap"])
                        if evidence.get("market_cap") is not None
                        else None
                    ),
                    dollar_volume=(
                        float(price) * float(avg_volume)
                        if price is not None and avg_volume is not None
                        else None
                    ),
                    pct_change=None,
                    stream=payload["stream"],
                    event_id=payload["dedup_key"],
                    evidence=payload,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            log.warning(
                "high_potential_funnel.external_evidence_invalid",
                ticker=row.ticker,
            )
    return out


def _ingest_candidate(row, payload: dict) -> TrendCandidate:
    """Adapt research leads (including legacy flat payloads), not fake grades.

    Called on the funnel's worker thread. Prices/capitalization come from the
    market adapter, never the video's prose. A failed market fetch aborts this
    pass explicitly rather than silently treating the lead as evaluated.
    """
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

    from argosy.services.instrument_reference import lookup, STRUCT_ETF
    from argosy.services.order_sheet_facts import _quote_ticker_candidates

    symbol = row.ticker.strip().upper().replace("/", "-")
    ref = lookup(row.ticker)
    foreign_fund = ref is not None and ref.structure == STRUCT_ETF and ref.estate_safe
    # Reuse the order-sheet listing search for known non-US funds. A bare
    # symbol can be missing (DPYA) or an unrelated US security (EXUS).
    # This does not infer incorporation from a quote-provider country field.
    candidates = _quote_ticker_candidates(symbol, None, foreign_fund=True) if foreign_fund else (symbol,)
    adapter = YFinanceAdapter()
    facts = {}
    for candidate in candidates:
        fetched = asyncio.run(adapter.get_quote_with_fundamentals(candidate))
        if foreign_fund and (str(fetched.get("currency") or "").upper() != "USD"
                             or str(fetched.get("quote_type") or "").upper() not in {"ETF", "MUTUALFUND"}
                             or not math.isfinite(float(fetched.get("price") or 0))
                             or float(fetched.get("price") or 0) <= 0):
            continue
        facts = {**fetched, "resolved_ticker": candidate}
        break
    price = facts.get("price")
    cap = facts.get("market_cap")
    fund = str(facts.get("quote_type") or "").upper() in {"ETF", "MUTUALFUND"}
    if (price is None or not math.isfinite(float(price)) or float(price) <= 0
            or (not fund and (cap is None or not math.isfinite(float(cap)) or float(cap) <= 0))):
        raise RuntimeError(f"Research lead {row.ticker}: current price/market cap unavailable")
    if str(facts.get("currency") or "").upper() != "USD":
        raise RuntimeError(f"Research lead {row.ticker}: USD market facts unavailable")
    volume = facts.get("average_volume")
    # No synthetic conviction/score: source confidence is a claim, not a rank.
    # The normal estimator determines suitability relative to all other names.
    source_id = str(payload.get("source_id") or payload.get("dedup_key") or "")
    rationale = str(payload.get("rationale") or (payload.get("evidence") or {}).get("rationale") or "")
    evidence = {**payload, "market_facts": {
        **facts, "source": "yfinance", "retrieved_at": datetime.now(UTC).isoformat(),
    }}
    return TrendCandidate(
        ticker=row.ticker, name=row.ticker, score=float(row.last_score or 0),
        families=(f"SIGNAL_STREAM:{payload['stream']}",),
        reasons=(f"Research lead, not an investment verdict: {rationale}",
                 f"source={payload.get('source_url') or source_id}",
                 f"instrument_type={facts.get('quote_type')}; corporate market cap is not applicable to funds" if fund
                 else f"instrument_type={facts.get('quote_type')}"),
        price=float(price), market_cap=float(cap) if cap is not None else None,
        dollar_volume=float(price) * float(volume) if volume is not None else None,
        pct_change=None, stream=payload["stream"], event_id=source_id,
        evidence=evidence,
    )


def _load_external_quarantine(user_id: str) -> list[tuple[str, str]]:
    return [
        (row.ticker, row.quarantine_reason or "failed-liquidity")
        for row in _external_rows(user_id, "quarantined")
    ]


def _estimate(candidate, *, user_id: str = "ariel") -> EstimatorVerdict:
    from argosy.agents.quick_estimator import estimate_with_report
    from argosy.services.agent_report_persistence import (
        persist_agent_report_sync,
    )

    verdict, report = estimate_with_report(candidate, user_id=user_id)
    persist_agent_report_sync(
        report,
        decision_id=f"discovery:{verdict.ticker}"[:64],
    )
    return verdict


async def _grade(user_id: str, candidate, **kwargs) -> FleetPick | None:
    from argosy.services.discovery_grader import grade_discovery_ticker
    return await grade_discovery_ticker(user_id, candidate, **kwargs)


def _load_scan_states(user_id: str) -> dict[str, dict]:
    """{ticker: row-dict} of the user's persisted ScanState."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.models import ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(bind=create_engine(
        url, connect_args={"check_same_thread": False}))
    out: dict[str, dict] = {}
    with factory() as db:
        for r in db.execute(select(ScanState).where(
                ScanState.user_id == user_id)).scalars():
            out[r.ticker] = {
                "ticker": r.ticker, "last_score": r.last_score,
                "radar_fingerprint": r.radar_fingerprint, "status": r.status,
                "rank": r.rank, "quarantine_reason": r.quarantine_reason,
                "estimator_json": r.estimator_json, "fleet_json": r.fleet_json,
                "nomination_evidence_json": r.nomination_evidence_json,
                "last_estimated_at": _iso(r.last_estimated_at),
                "last_radar_at": _iso(r.last_radar_at),
                "last_fleet_at": _iso(r.last_fleet_at),
                "last_seen_at": _iso(r.last_seen_at),
            }
    return out


def _persist_scan_states(user_id: str, states) -> None:
    """Upsert scan memory and date-stamp every eligible radar opportunity."""
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.db import create_sync_engine
    from argosy.state.models import ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(bind=create_sync_engine(url))
    state_rows = list(states)
    with factory() as db:
        for s in state_rows:
            row = db.get(ScanState, {"user_id": user_id, "ticker": s["ticker"]})
            if row is None:
                row = ScanState(user_id=user_id, ticker=s["ticker"])
                db.add(row)
            row.last_score = s.get("last_score", 0.0)
            row.radar_fingerprint = s.get("radar_fingerprint", "")
            row.status = s.get("status", "active")
            row.rank = s.get("rank")
            row.quarantine_reason = s.get("quarantine_reason", "")
            row.estimator_json = s.get("estimator_json")
            row.fleet_json = s.get("fleet_json")
            row.nomination_evidence_json = s.get(
                "nomination_evidence_json"
            )
            row.last_estimated_at = _parse(s.get("last_estimated_at"))
            row.last_radar_at = _parse(s.get("last_radar_at"))
            row.last_fleet_at = _parse(s.get("last_fleet_at"))
            row.last_seen_at = _parse(s.get("last_seen_at"))
            row.updated_at = datetime.now(UTC)
        db.commit()

    # Outcome clocks are additive telemetry, never part of the discovery-memory
    # transaction. A writer flush used to poison the shared Session; catching
    # that exception without a rollback then made the final commit fail and
    # discarded every estimator/fleet result in the run. Commit the decision
    # inputs first, then give each clock its own transaction so one bad ticker or
    # a transient SQLite lock cannot erase the comparison cohort.
    for s in state_rows:
        observation = s.get("observation")
        if not isinstance(observation, dict) or not observation.get("price"):
            continue
        try:
            from argosy.services.predictions.writers import (
                write_signal_stream_predictions,
            )

            observed_at = _parse(s.get("last_radar_at")) or datetime.now(UTC)
            with factory() as telemetry_db:
                write_signal_stream_predictions(
                    telemetry_db,
                    user_id,
                    stream="radar_observation",
                    dedup_key=(
                        f"{observed_at.date().isoformat()}|{s['ticker']}|"
                        f"{s.get('radar_fingerprint', '')}"
                    ),
                    ticker=s["ticker"],
                    direction="long",
                    event_at=observed_at,
                    entry_price=float(observation["price"]),
                    evidence=observation,
                )
                telemetry_db.commit()
        except Exception as exc:  # noqa: BLE001 - telemetry is best-effort
            log.warning(
                "high_potential_funnel.radar_clock_failed",
                ticker=s.get("ticker"),
                error=str(exc)[:160],
            )

    # The radar clocks answer "what did we see?"; these rows answer "what did
    # the fleet decide?". Write fresh fleet calls directly from the in-memory
    # observation so a failed radar-clock insert cannot strand the call without
    # its dated entry price. Reused fleet judgments are excluded here because
    # today's observation price is not their historical entry.
    for s in state_rows:
        observation = s.get("observation")
        fleet_at = _parse(s.get("last_fleet_at"))
        observed_at = _parse(s.get("last_radar_at"))
        if (
            not isinstance(observation, dict)
            or not observation.get("price")
            or fleet_at is None
            or observed_at is None
            or abs(fleet_at - observed_at) > timedelta(minutes=5)
        ):
            continue
        try:
            from argosy.services.predictions.writers import (
                write_discovery_evaluation_predictions,
            )

            fleet = json.loads(s.get("fleet_json") or "{}")
            estimator = json.loads(s.get("estimator_json") or "{}")
            verdict = str(fleet.get("verdict") or "").strip().upper()
            if verdict not in {"BUY", "WATCH", "PASS"}:
                continue
            thesis = str(fleet.get("thesis_md") or "").strip()
            if len(thesis) > 2_000:
                thesis = thesis[:1_997].rstrip() + "..."
            with factory() as telemetry_db:
                write_discovery_evaluation_predictions(
                    telemetry_db,
                    user_id,
                    event_key=(
                        f"{fleet_at.isoformat()}|"
                        f"{s.get('radar_fingerprint', '')}|{verdict}"
                    ),
                    ticker=s["ticker"],
                    verdict=verdict,
                    conviction=str(fleet.get("conviction") or ""),
                    event_at=fleet_at,
                    entry_price=float(observation["price"]),
                    estimator_go=estimator.get("go"),
                    estimator_conviction=estimator.get("conviction"),
                    estimation=str(estimator.get("one_line") or ""),
                    thesis=thesis,
                    radar_rank=s.get("rank"),
                    radar_score=s.get("last_score"),
                )
                telemetry_db.commit()
        except Exception as exc:  # noqa: BLE001 - telemetry is best-effort
            log.warning(
                "high_potential_funnel.fleet_clock_failed",
                ticker=s.get("ticker"),
                error=str(exc)[:160],
            )

    # Repair legacy/reused decisions from durable radar observations. This is
    # also the daily scheduler seam, so a transient write failure retries.
    try:
        from argosy.services.predictions.writers import (
            ensure_discovery_evaluation_predictions,
        )

        with factory() as telemetry_db:
            ensure_discovery_evaluation_predictions(
                telemetry_db,
                user_id=user_id,
            )
            telemetry_db.commit()
    except Exception as exc:  # noqa: BLE001 - telemetry is best-effort
        log.warning(
            "high_potential_funnel.fleet_clock_failed",
            error=str(exc)[:160],
        )


# --- helpers ---------------------------------------------------------------

def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _parse(s) -> datetime | None:
    """Parse an ISO string (or pass a datetime) to a tz-AWARE datetime. SQLite
    DateTime(timezone=True) round-trips as naive; assume UTC so freshness diffs
    against an aware ``now`` never raise (codex p2 #5)."""
    if not s:
        return None
    dt = s if isinstance(s, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def radar_fingerprint(c: TrendCandidate) -> str:
    """score (1dp) + sorted families + liquidity bucket (codex #8)."""
    dv = c.dollar_volume or 0.0
    liq = "high" if dv >= 1e8 else "mid" if dv >= 1e7 else "low"
    fams = ",".join(sorted(c.families or ()))
    external = ""
    if c.stream and c.event_id:
        external = f"|stream={c.stream}|event={c.event_id}"
    return f"s={round(c.score, 1)}|f={fams}|l={liq}{external}"


def _fresh(prev_iso: str | None, ttl: timedelta, now: datetime) -> bool:
    dt = _parse(prev_iso)
    return dt is not None and (now - dt) < ttl


def _verdict_to_json(v: EstimatorVerdict) -> str:
    return json.dumps(asdict(v))


def _verdict_from_json(blob: str) -> EstimatorVerdict:
    d = json.loads(blob)
    return EstimatorVerdict(ticker=d["ticker"], go=d["go"],
                            conviction=d["conviction"], sentiment=d["sentiment"],
                            one_line=d["one_line"])


def _pick_to_json(p: FleetPick) -> str:
    d = asdict(p)
    d["cites"] = list(p.cites)
    d["research_contract"] = FLEET_RESEARCH_CONTRACT
    return json.dumps(d)


def _current_fleet_contract(blob: str | None) -> bool:
    """Cache compatibility, not a judgment on the investment verdict."""
    if not blob:
        return False
    try:
        return json.loads(blob).get("research_contract") == FLEET_RESEARCH_CONTRACT
    except (ValueError, AttributeError):
        return False


def _pick_from_json(blob: str) -> FleetPick:
    d = json.loads(blob)
    return FleetPick(ticker=d["ticker"], conviction=d["conviction"],
                     thesis_md=d["thesis_md"], verdict=d["verdict"],
                     cites=tuple(d.get("cites", ())))


# --- orchestration ---------------------------------------------------------

async def run_funnel(user_id: str, *, force: bool = False,
                     now: datetime | None = None) -> FunnelResult:
    """Radar -> diff vs ScanState -> estimate new/changed -> grade top-K go
    names -> persist. ``force`` re-estimates + re-grades everything."""
    now = now or datetime.now(UTC)
    scan = await asyncio.to_thread(_scan_radar)
    merged = {c.ticker: c for c in scan.shortlist}
    external = await asyncio.to_thread(_load_external_candidates, user_id)
    merged.update({c.ticker: c for c in external})
    shortlist = sorted(merged.values(), key=lambda c: -c.score)
    _scan_quarantine = list(scan.quarantine or ())
    _scan_quarantine.extend(_load_external_quarantine(user_id))
    existing = _load_scan_states(user_id)
    radar_tickers = {c.ticker for c in shortlist}

    # Persist the deterministic observation BEFORE any estimator/fleet LLM
    # calls. A slow or failed judgment pass must not erase the fact that the
    # radar saw a live-priced opportunity; these receipts are the denominator
    # for missed-opportunity calibration.
    observation_states: list[dict] = []
    for rank, candidate in enumerate(shortlist, start=1):
        fingerprint = radar_fingerprint(candidate)
        prior = existing.get(candidate.ticker) or {}
        same_fingerprint = prior.get("radar_fingerprint") == fingerprint
        observation_states.append({
            "ticker": candidate.ticker,
            "last_score": candidate.score,
            "radar_fingerprint": fingerprint,
            "status": "active",
            "rank": rank,
            "quarantine_reason": "",
            "estimator_json": (
                prior.get("estimator_json") if same_fingerprint else None
            ),
            "fleet_json": (
                prior.get("fleet_json") if same_fingerprint else None
            ),
            "last_estimated_at": (
                prior.get("last_estimated_at") if same_fingerprint else None
            ),
            "last_radar_at": now.isoformat(),
            "last_fleet_at": (
                prior.get("last_fleet_at") if same_fingerprint else None
            ),
            "last_seen_at": now.isoformat(),
            "nomination_evidence_json": (
                json.dumps(candidate.evidence, sort_keys=True, default=str)
                if candidate.evidence is not None
                else prior.get("nomination_evidence_json")
            ),
            "observation": {
                "score": candidate.score,
                "rank": rank,
                "families": list(candidate.families),
                "price": candidate.price,
                "market_cap": candidate.market_cap,
                "dollar_volume": candidate.dollar_volume,
                "lane": candidate.lane,
                "radar_fingerprint": fingerprint,
            },
        })
    quarantined_tickers: set[str] = set()
    for ticker, reason in _scan_quarantine:
        if ticker in radar_tickers or ticker in quarantined_tickers:
            continue
        quarantined_tickers.add(ticker)
        prior = existing.get(ticker) or {}
        observation_states.append({
            **prior,
            "ticker": ticker,
            "status": "quarantined",
            "quarantine_reason": reason,
            "last_radar_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
        })
    for ticker, prior in existing.items():
        if ticker in radar_tickers or ticker in quarantined_tickers:
            continue
        if prior.get("status") == "dropped":
            continue
        observation_states.append({
            **prior,
            "ticker": ticker,
            "status": "dropped",
        })
    if observation_states:
        _persist_scan_states(user_id, observation_states)

    estimated: list[EstimatorVerdict] = []
    states: dict[str, dict] = {}
    go_candidates: list[tuple[EstimatorVerdict, TrendCandidate, dict, bool]] = []

    for rank, c in enumerate(shortlist, start=1):
        fp = radar_fingerprint(c)
        prev = existing.get(c.ticker)
        same_fp = prev is not None and prev.get("radar_fingerprint") == fp
        reuse = (not force and same_fp and prev.get("estimator_json")
                 and _fresh(prev.get("last_estimated_at"), ESTIMATE_TTL, now))
        if reuse:
            verdict = _verdict_from_json(prev["estimator_json"])
            last_estimated_at = prev.get("last_estimated_at")
        else:
            # The estimator is a sync agent (run_sync -> asyncio.run); offload it
            # so it never calls asyncio.run() inside this running event loop.
            verdict = await asyncio.to_thread(_estimate, c, user_id=user_id)
            last_estimated_at = now.isoformat()
        estimated.append(verdict)
        # Carry a stored fleet grade forward ONLY when the fingerprint is
        # unchanged AND it is still fresh AND we are not forcing — otherwise the
        # old grade is for the OLD fingerprint and must not be persisted against
        # the new one (codex p2 #1/#2). It is reset below if a fresh grade lands.
        fleet_fresh = (not force and same_fp and prev is not None
                       and prev.get("fleet_json")
                       and _current_fleet_contract(prev.get("fleet_json"))
                       and _fresh(prev.get("last_fleet_at"), FLEET_TTL, now))
        state = {
            "ticker": c.ticker, "last_score": c.score, "radar_fingerprint": fp,
            "status": "active", "rank": rank, "quarantine_reason": "",
            "estimator_json": _verdict_to_json(verdict),
            "fleet_json": prev.get("fleet_json") if fleet_fresh else None,
            "last_estimated_at": last_estimated_at,
            "last_radar_at": now.isoformat(),
            "last_fleet_at": prev.get("last_fleet_at") if fleet_fresh else None,
            "last_seen_at": now.isoformat(),
            "nomination_evidence_json": (
                json.dumps(c.evidence, sort_keys=True, default=str)
                if c.evidence is not None
                else (prev or {}).get("nomination_evidence_json")
            ),
            "observation": {
                "score": c.score,
                "rank": rank,
                "families": list(c.families),
                "price": c.price,
                "market_cap": c.market_cap,
                "dollar_volume": c.dollar_volume,
                "lane": c.lane,
                "radar_fingerprint": fp,
            },
        }
        states[c.ticker] = state
        if verdict.go:
            go_candidates.append((verdict, c, state, fleet_fresh))

    # Escalate the top-K go names (by conviction then sentiment) to the fleet,
    # reusing a fresh stored grade when the fingerprint is unchanged.
    go_candidates.sort(
        key=lambda t: (_CONVICTION_RANK.get(t[0].conviction, 0), t[0].sentiment),
        reverse=True)
    picks: list[FleetPick] = []
    for _verdict, c, state, fleet_fresh in go_candidates[:TOP_K_TO_FLEET]:
        if fleet_fresh:
            picks.append(_pick_from_json(state["fleet_json"]))
            continue
        pick = await _grade(user_id, c)
        if pick is not None:
            state["fleet_json"] = _pick_to_json(pick)
            state["last_fleet_at"] = now.isoformat()
            picks.append(pick)

    # Seen-but-quarantined (radar filtered them, e.g. liquidity) -> status
    # 'quarantined' with the reason; NOT dropped (codex p2 #6).
    for ticker, reason in _scan_quarantine:
        if ticker in states:
            continue
        prev = existing.get(ticker, {})
        states[ticker] = {**{"ticker": ticker, "last_score": prev.get("last_score", 0.0),
                             "radar_fingerprint": prev.get("radar_fingerprint", ""),
                             "rank": prev.get("rank"),
                             "estimator_json": prev.get("estimator_json"),
                             "fleet_json": prev.get("fleet_json"),
                             "nomination_evidence_json": prev.get(
                                 "nomination_evidence_json"
                             ),
                             "last_estimated_at": prev.get("last_estimated_at"),
                             "last_radar_at": now.isoformat(),
                             "last_fleet_at": prev.get("last_fleet_at")},
                          "status": "quarantined", "quarantine_reason": reason,
                          "last_seen_at": now.isoformat()}

    # TTL-evict: anything previously tracked but absent from this radar is
    # marked dropped (kept so the diff is stable; the GET filters it).
    for ticker, prev in existing.items():
        if (ticker not in radar_tickers and ticker not in states
                and prev.get("status") != "dropped"):
            dropped = dict(prev)
            dropped["status"] = "dropped"
            states[ticker] = dropped

    _persist_scan_states(user_id, list(states.values()))
    log.info("high_potential_funnel.run_done", user_id=user_id,
             radar=len(shortlist), estimated=len(estimated), picks=len(picks))
    return FunnelResult(picks=picks, estimated=estimated, radar=shortlist,
                        last_refreshed_at=now.isoformat())


__all__ = ["FunnelResult", "run_funnel", "radar_fingerprint"]
