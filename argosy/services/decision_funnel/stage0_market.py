"""Stage 0 — market / macro review off ALREADY-INGESTED data.

A single cheap pass that produces a compact "what moved + why" read for the
day WITHOUT any new fetches: it reads the structured signals the daily
monitors already persisted —
- the latest ``AlphaReportAnalysis`` (Discord macro tone + per-ticker signals,
  written by alpha_report_analyst 18:00),
- active macro/volatility ``MonitorFlag`` rows (state_observer 17:00),
- recent high-materiality ``NewsSignal`` rows (news_daily 17:00),
- a best-effort VIX read from ``MacroCache`` if present.

Everything is source-cited (row ids + timestamps) so Stage 1 routes against
real, attributable signals and the trace can show exactly what the macro read
was built from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select

from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.decision_funnel.stage0")

_RISK_OFF_TONES = {"bearish", "cautiously_bearish"}
_NEWS_LOOKBACK_DAYS = 2
VIX_ELEVATED = 20.0
VIX_HIGH = 28.0


@dataclass(frozen=True)
class NewsHit:
    signal_id: int
    ticker: str
    sentiment: str | None
    materiality: str | None
    excerpt: str


@dataclass(frozen=True)
class MarketRead:
    as_of: str | None
    macro_tone: str | None
    macro_tone_confidence: str | None
    risk_off: bool
    vix: float | None
    vix_band: str | None  # "calm" | "elevated" | "high"
    key_themes: list[str]
    # Per-ticker single-name signals surfaced by the macro pass (bearish first).
    ticker_signals: list[dict[str, Any]]
    high_materiality_news: list[NewsHit]
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "macro_tone": self.macro_tone,
            "macro_tone_confidence": self.macro_tone_confidence,
            "risk_off": self.risk_off,
            "vix": self.vix,
            "vix_band": self.vix_band,
            "key_themes": self.key_themes,
            "ticker_signals": self.ticker_signals,
            "high_materiality_news": [
                {
                    "signal_id": n.signal_id, "ticker": n.ticker,
                    "sentiment": n.sentiment, "materiality": n.materiality,
                    "excerpt": n.excerpt,
                }
                for n in self.high_materiality_news
            ],
            "source_refs": self.source_refs,
            "summary": self.summary,
        }


def _loads(blob: Any, default: Any) -> Any:
    if not blob:
        return default
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _read_vix(session: Session) -> float | None:
    """Best-effort VIX from MacroCache; None if not cached."""
    try:
        from argosy.state.models import MacroCache
    except Exception:  # pragma: no cover
        return None
    try:
        rows = (
            session.execute(
                select(MacroCache).where(MacroCache.key.ilike("%vix%"))
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001
        return None
    for r in rows:
        payload = _loads(getattr(r, "payload_json", None), None)
        for cand in (payload,) if not isinstance(payload, dict) else (
            payload.get("last"), payload.get("value"), payload.get("close"), payload,
        ):
            try:
                v = float(cand)
                if 0 < v < 200:
                    return v
            except (TypeError, ValueError):
                continue
    return None


def build_market_read(
    session: Session, *, user_id: str, now: datetime | None = None
) -> MarketRead:
    """Assemble the Stage-0 macro read. Never raises — degrades to a neutral
    read when no signals are available (the funnel then routes only on
    per-name hard triggers)."""
    now = now or datetime.now(UTC)
    from argosy.state.models import AlphaReportAnalysis, MonitorFlag, NewsSignal

    refs: list[dict[str, Any]] = []

    # 1) Latest Discord macro analysis.
    macro_tone = macro_conf = None
    key_themes: list[str] = []
    ticker_signals: list[dict[str, Any]] = []
    analyzed_at = None
    try:
        alpha = (
            session.execute(
                select(AlphaReportAnalysis)
                .where(AlphaReportAnalysis.user_id == user_id)
                .order_by(desc(AlphaReportAnalysis.analyzed_at))
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        alpha = None
    if alpha is not None:
        macro_tone = getattr(alpha, "macro_tone", None)
        macro_conf = getattr(alpha, "macro_tone_confidence", None)
        key_themes = _loads(getattr(alpha, "key_themes", None), [])
        ticker_signals = _loads(getattr(alpha, "ticker_signals_json", None), [])
        analyzed_at = getattr(alpha, "analyzed_at", None)
        refs.append({
            "kind": "alpha_report",
            "id": getattr(alpha, "id", None),
            "at": analyzed_at.isoformat() if analyzed_at else None,
        })

    # 2) Active macro/volatility monitor flags (regime shift).
    macro_critical = False
    try:
        flags = (
            session.execute(
                select(MonitorFlag).where(
                    MonitorFlag.user_id == user_id,
                    MonitorFlag.status == "active",
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001
        flags = []
    for f in flags:
        kind = f.kind or ""
        if kind in {"macro_shift"} or any(
            t in kind for t in ("volatility", "equity", "rates", "macro", "fx")
        ):
            refs.append({
                "kind": "monitor_flag", "id": f.id, "flag_kind": kind,
                "severity": f.severity,
            })
            if f.severity == "critical":
                macro_critical = True

    # 3) Recent high-materiality single-name news.
    cutoff = now - timedelta(days=_NEWS_LOOKBACK_DAYS)
    high_news: list[NewsHit] = []
    try:
        signals = (
            session.execute(
                select(NewsSignal)
                .where(
                    NewsSignal.materiality == "high",
                    NewsSignal.received_at >= cutoff,
                )
                .order_by(desc(NewsSignal.received_at))
                .limit(50)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        signals = []
    for s in signals:
        tickers = _loads(getattr(s, "parsed_tickers", None), [])
        for tk in tickers:
            high_news.append(
                NewsHit(
                    signal_id=getattr(s, "id", 0),
                    ticker=str(tk).upper(),
                    sentiment=getattr(s, "sentiment", None),
                    materiality=getattr(s, "materiality", None),
                    excerpt=(getattr(s, "evidence_excerpt", "") or "")[:200],
                )
            )
        if tickers:
            refs.append({
                "kind": "news_signal", "id": getattr(s, "id", None),
                "at": s.received_at.isoformat() if getattr(s, "received_at", None) else None,
            })

    vix = _read_vix(session)
    vix_band = None
    if vix is not None:
        vix_band = "high" if vix >= VIX_HIGH else ("elevated" if vix >= VIX_ELEVATED else "calm")

    risk_off = (
        (macro_tone in _RISK_OFF_TONES)
        or macro_critical
        or (vix is not None and vix >= VIX_HIGH)
    )

    parts: list[str] = []
    if macro_tone:
        parts.append(f"macro tone {macro_tone}")
    if vix_band:
        parts.append(f"VIX {vix_band}")
    if risk_off:
        parts.append("risk-off")
    if key_themes:
        parts.append("themes: " + ", ".join(key_themes[:3]))
    summary = "; ".join(parts) or "no material macro signal"

    read = MarketRead(
        as_of=(analyzed_at.isoformat() if analyzed_at else now.isoformat()),
        macro_tone=macro_tone,
        macro_tone_confidence=macro_conf,
        risk_off=risk_off,
        vix=vix,
        vix_band=vix_band,
        key_themes=key_themes,
        ticker_signals=ticker_signals,
        high_materiality_news=high_news,
        source_refs=refs,
        summary=summary,
    )
    _log.info(
        "decision_funnel.stage0_done", user_id=user_id, risk_off=risk_off,
        macro_tone=macro_tone, news_hits=len(high_news), refs=len(refs),
    )
    return read


__all__ = ["MarketRead", "NewsHit", "build_market_read", "VIX_ELEVATED", "VIX_HIGH"]
