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

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from argosy.logging import get_logger
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.trend_radar import ScanResult, TrendCandidate

log = get_logger(__name__)

ESTIMATE_TTL = timedelta(hours=24)
FLEET_TTL = timedelta(days=3)
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


def _estimate(candidate, *, user_id: str = "ariel") -> EstimatorVerdict:
    from argosy.agents.quick_estimator import estimate
    return estimate(candidate, user_id=user_id)


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
                "last_estimated_at": _iso(r.last_estimated_at),
                "last_radar_at": _iso(r.last_radar_at),
                "last_fleet_at": _iso(r.last_fleet_at),
                "last_seen_at": _iso(r.last_seen_at),
            }
    return out


def _persist_scan_states(user_id: str, states) -> None:
    """Upsert the ScanState rows for ``user_id``."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.config import get_settings
    from argosy.state.models import ScanState

    url = str(get_settings().database_url).replace("+aiosqlite", "")
    factory = sessionmaker(bind=create_engine(
        url, connect_args={"check_same_thread": False}))
    with factory() as db:
        for s in states:
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
            row.last_estimated_at = _parse(s.get("last_estimated_at"))
            row.last_radar_at = _parse(s.get("last_radar_at"))
            row.last_fleet_at = _parse(s.get("last_fleet_at"))
            row.last_seen_at = _parse(s.get("last_seen_at"))
            row.updated_at = datetime.now(timezone.utc)
        db.commit()


# --- helpers ---------------------------------------------------------------

def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _parse(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def radar_fingerprint(c: TrendCandidate) -> str:
    """score (1dp) + sorted families + liquidity bucket (codex #8)."""
    dv = c.dollar_volume or 0.0
    liq = "high" if dv >= 1e8 else "mid" if dv >= 1e7 else "low"
    fams = ",".join(sorted(c.families or ()))
    return f"s={round(c.score, 1)}|f={fams}|l={liq}"


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
    return json.dumps(d)


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
    now = now or datetime.now(timezone.utc)
    shortlist = list(_scan_radar().shortlist)
    existing = _load_scan_states(user_id)
    radar_tickers = {c.ticker for c in shortlist}

    estimated: list[EstimatorVerdict] = []
    states: dict[str, dict] = {}
    go_candidates: list[tuple[EstimatorVerdict, TrendCandidate, dict]] = []

    for rank, c in enumerate(shortlist, start=1):
        fp = radar_fingerprint(c)
        prev = existing.get(c.ticker)
        reuse = (not force and prev is not None
                 and prev.get("radar_fingerprint") == fp
                 and prev.get("estimator_json")
                 and _fresh(prev.get("last_estimated_at"), ESTIMATE_TTL, now))
        if reuse:
            verdict = _verdict_from_json(prev["estimator_json"])
            last_estimated_at = prev.get("last_estimated_at")
        else:
            verdict = _estimate(c, user_id=user_id)
            last_estimated_at = now.isoformat()
        estimated.append(verdict)
        state = {
            "ticker": c.ticker, "last_score": c.score, "radar_fingerprint": fp,
            "status": "active", "rank": rank, "quarantine_reason": "",
            "estimator_json": _verdict_to_json(verdict),
            "fleet_json": prev.get("fleet_json") if prev else None,
            "last_estimated_at": last_estimated_at,
            "last_radar_at": now.isoformat(),
            "last_fleet_at": prev.get("last_fleet_at") if prev else None,
            "last_seen_at": now.isoformat(),
        }
        states[c.ticker] = state
        if verdict.go:
            go_candidates.append((verdict, c, state))

    # Escalate the top-K go names (by conviction then sentiment) to the fleet,
    # reusing a fresh stored grade when the fingerprint is unchanged.
    go_candidates.sort(
        key=lambda t: (_CONVICTION_RANK.get(t[0].conviction, 0), t[0].sentiment),
        reverse=True)
    picks: list[FleetPick] = []
    for verdict, c, state in go_candidates[:TOP_K_TO_FLEET]:
        prev = existing.get(c.ticker)
        reuse_fleet = (not force and prev is not None
                       and prev.get("radar_fingerprint") == state["radar_fingerprint"]
                       and prev.get("fleet_json")
                       and _fresh(prev.get("last_fleet_at"), FLEET_TTL, now))
        if reuse_fleet:
            pick = _pick_from_json(prev["fleet_json"])
        else:
            pick = await _grade(user_id, c)
            if pick is not None:
                state["fleet_json"] = _pick_to_json(pick)
                state["last_fleet_at"] = now.isoformat()
        if pick is not None:
            picks.append(pick)

    # TTL-evict: anything previously tracked but absent from this radar is
    # marked dropped (kept so the diff is stable; the GET filters it).
    for ticker, prev in existing.items():
        if ticker not in radar_tickers and prev.get("status") != "dropped":
            dropped = dict(prev)
            dropped["status"] = "dropped"
            states[ticker] = dropped

    _persist_scan_states(user_id, list(states.values()))
    log.info("high_potential_funnel.run_done", user_id=user_id,
             radar=len(shortlist), estimated=len(estimated), picks=len(picks))
    return FunnelResult(picks=picks, estimated=estimated, radar=shortlist,
                        last_refreshed_at=now.isoformat())


__all__ = ["FunnelResult", "run_funnel", "radar_fingerprint"]
