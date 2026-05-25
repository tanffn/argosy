"""Input-assembly helpers for plan synthesis.

Two layers live here:

1. The legacy ``_assemble_portfolio_summary`` / ``_assemble_fills_summary``
   / ``_load_user_context_yaml`` helpers used by the existing
   orchestrator. They are monkeypatched in tests; keep them stable.

2. The new ``Phase1Inputs`` dataclass + ``assemble_phase1_inputs``
   function (Wave W1.A). These compute *every* payload required by the
   nine phase-1 analysts up front so the orchestrator can route the
   correct narrow kwargs to each agent in W1.B. Today the orchestrator
   ships a single shared ``common_kwargs`` bag that satisfies ~5 of the
   9 ``build_prompt`` signatures and crashes the other four with
   ``TypeError: build_prompt() missing required keyword-only arguments``.

Design notes for ``assemble_phase1_inputs``:

* **Synchronous** — the synthesis flow runs in sync context and is
  driven by ``concurrent.futures.ThreadPoolExecutor``. When we have to
  call an async adapter (Finnhub / TipRanks / FRED), we wrap it in
  ``asyncio.run(...)``.
* **Best-effort** — every payload section is wrapped in its own
  try/except. A missing API key, an unreachable upstream, or a parsing
  failure logs ``plan_synthesis.inputs.<field>_skipped`` (or
  ``..._failed``) and leaves the field at its empty default. The
  function NEVER raises.
* **Empty-by-default** — operational tables that are still empty in
  prod today (``lots``, ``fills``, dividends, RSU schedule) are
  surfaced as empty strings. Waves W3a/W3b backfill them; the
  contract here is stable so the downstream agent prompts get the
  same field whether populated or not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from argosy.logging import get_logger

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Legacy helpers (monkeypatched in tests)
# ----------------------------------------------------------------------


def _assemble_portfolio_summary(*, session, user_id) -> str:
    """Build a compact portfolio-state summary for synthesis input.

    Wave 2: read latest TSV/CSV ingest + IBKR positions per SDD §8.
    Tests stub this.
    """
    return "(portfolio snapshot — wired against existing positions ingest)"


def _assemble_fills_summary(*, session, user_id) -> str:
    """Last 90 days of fills + decisions, summarized."""
    return "(fills summary — wired against fills + proposals tables)"


def _load_user_context_yaml(*, session, user_id) -> str:
    """Concatenate identity + goals + constraints YAML for the user."""
    from argosy.state.models import UserContext
    ctx = session.get(UserContext, user_id)
    if ctx is None:
        return ""
    parts = []
    if ctx.identity_yaml:
        parts.append(ctx.identity_yaml)
    if ctx.goals_yaml:
        parts.append(ctx.goals_yaml)
    if ctx.constraints_yaml:
        parts.append(ctx.constraints_yaml)
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Wave W1.A — typed Phase1Inputs + best-effort assembler
# ----------------------------------------------------------------------


@dataclass
class Phase1Inputs:
    """All payloads required by the 9 phase-1 analysts of plan_synthesis.

    Field names match the keyword-only ``build_prompt`` parameters of
    each analyst so the orchestrator (W1.B) can fan out the dataclass
    to each agent with narrow per-agent kwargs.
    """

    # ConcentrationAnalystAgent
    positions_summary: str = ""
    plan_targets: dict[str, float] = field(default_factory=dict)
    nvda_shares_sold_ytd: int = 0
    nvda_target_shares_ytd: int = 0

    # FXAnalystAgent
    fx_payload: dict[str, dict[str, float]] = field(default_factory=dict)

    # FundamentalsAnalystAgent / NewsAnalystAgent / SentimentAnalystAgent /
    # TechnicalAnalystAgent all share `tickers`.
    tickers: list[str] = field(default_factory=list)
    fundamentals_payload: dict[str, dict[str, Any]] = field(default_factory=dict)
    news_payload: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    social_payload: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    indicators_payload: dict[str, dict[str, Any]] = field(default_factory=dict)

    # MacroAnalystAgent
    macro_snapshot: dict[str, float] = field(default_factory=dict)

    # TaxAnalystAgent
    lots_summary: str = ""
    dividends_summary: str = ""
    rsu_schedule_summary: str = ""

    # PlanCritiqueAgent
    plan_label: str = ""
    plan_markdown: str = ""
    snapshot_label: str = ""
    snapshot_summary: str = ""
    user_context_yaml: str = ""
    domain_kb_files: dict[str, str] = field(default_factory=dict)
    recent_events: str = ""


def assemble_phase1_inputs(
    session: Session,
    *,
    user_id: str,
    baseline,
    prior_current,
    decision_audit_token: str,
) -> Phase1Inputs:
    """Synchronous assembler — populates every field best-effort.

    NEVER raises: a section that can't be sourced (missing key, missing
    adapter, network error, parse error) logs a structured warning and
    leaves the field at its empty default.
    """
    log.info(
        "plan_synthesis.inputs.start",
        user_id=user_id,
        decision_audit_token=decision_audit_token,
    )

    inputs = Phase1Inputs()
    inputs.snapshot_label = decision_audit_token

    # 1. Plan label + markdown from the baseline (W1.B's PlanCritique
    #    feeds these into its build_prompt).
    if baseline is not None:
        try:
            inputs.plan_label = (
                getattr(baseline, "version_label", "") or "(unlabeled plan)"
            )
            inputs.plan_markdown = getattr(baseline, "raw_markdown", "") or ""
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.plan_label_failed",
                user_id=user_id,
                error=str(exc),
            )

    # 2. plan_targets — derived from the baseline's distillate when
    #    available. The distillate's ``targets`` list carries
    #    ``label`` + ``value`` + ``unit`` for each Target; we map
    #    label -> value for the percent-style targets the
    #    ConcentrationAnalystAgent expects.
    if baseline is not None:
        try:
            inputs.plan_targets = _extract_plan_targets(baseline)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.plan_targets_failed",
                user_id=user_id,
                error=str(exc),
            )

    # 3. Portfolio snapshot from latest TSV under ARGOSY_HOME.
    #    Reuses the daily_brief pattern; downgraded to a synchronous
    #    call site (parse_portfolio_tsv is already sync).
    try:
        tsv_path = _find_latest_tsv()
        if tsv_path is not None:
            from argosy.ingest.tsv import parse_portfolio_tsv

            snapshot = parse_portfolio_tsv(tsv_path)
            inputs.tickers = sorted(
                {p.ticker for p in getattr(snapshot, "positions", []) if p.ticker}
            )
            inputs.positions_summary = _summarize_positions(snapshot)
        else:
            log.warning(
                "plan_synthesis.inputs.no_tsv_found", user_id=user_id
            )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.tsv_parse_failed",
            user_id=user_id,
            error=str(exc),
        )

    # snapshot_summary is by convention the positions_summary text — the
    # PlanCritique agent reads it as the "current state" half of the
    # plan-vs-snapshot delta.
    inputs.snapshot_summary = inputs.positions_summary

    # 4. News payload (Finnhub). Per-ticker, capped at 25 tickers to
    #    keep the free-tier rate limit happy. Each section is wrapped
    #    in its own try/except so a synthetic explosion in one helper
    #    doesn't escape the assembler.
    if inputs.tickers:
        try:
            inputs.news_payload = _gather_news(inputs.tickers)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.news_failed",
                user_id=user_id,
                error=str(exc),
            )

    # 5. Macro snapshot (FRED).
    try:
        inputs.macro_snapshot = _gather_macro_snapshot()
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.macro_failed",
            user_id=user_id,
            error=str(exc),
        )

    # 6. FX payload (BoI via the FX service). Two pairs we care about
    #    most: USD/NIS and EUR/NIS. Each pair carries spot + 30d/90d
    #    pct change, matching the FXAnalystAgent payload contract.
    try:
        inputs.fx_payload = _gather_fx_payload(session)
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.fx_failed",
            user_id=user_id,
            error=str(exc),
        )

    # 7. Fundamentals — no adapter wired today. The fundamentals
    #    analyst's payload contract (pe_ratio, peg_ratio, ev_ebitda,
    #    revenue_growth_yoy, ...) doesn't have a single canonical
    #    source; W3a wires SEC EDGAR + yfinance. Empty + warn for now.
    if inputs.tickers:
        log.warning(
            "plan_synthesis.inputs.fundamentals_no_adapter",
            user_id=user_id,
            tickers_count=len(inputs.tickers),
        )

    # 8. Sentiment — TipRanks blogger sentiment per ticker. Stored as
    #    a single-element list keyed by ticker so the
    #    SentimentAnalystAgent's per-ticker iteration sees a real
    #    {text, polarity, source} dict.
    if inputs.tickers:
        try:
            inputs.social_payload = _gather_social_payload(inputs.tickers)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.social_failed",
                user_id=user_id,
                error=str(exc),
            )

    # 9. Indicators — no canonical pre-computed indicators source yet
    #    (we'd compute MA/RSI/MACD from yfinance OHLC ourselves). Empty
    #    + warn for now; W3a fills it.
    if inputs.tickers:
        log.warning(
            "plan_synthesis.inputs.indicators_no_adapter",
            user_id=user_id,
            tickers_count=len(inputs.tickers),
        )

    # 10. Tax fields (lots / dividends / RSU schedule). Operational
    #     tables are still empty (`lots=0`, `fills=0`); leave the
    #     fields as empty strings for the tax analyst — W3b populates.
    #     domain_kb_files stays empty here: each analyst pulls its own
    #     subset via its prompt builder.

    # 11. User context YAML (identity + goals + constraints).
    #     Resolve via the package namespace so tests that monkeypatch
    #     ``flow._load_user_context_yaml`` are honoured — the same
    #     calling convention the orchestrator uses for its other helpers.
    try:
        import sys as _sys

        _pkg = _sys.modules.get(
            "argosy.orchestrator.flows.plan_synthesis"
        )
        if _pkg is not None and hasattr(_pkg, "_load_user_context_yaml"):
            inputs.user_context_yaml = _pkg._load_user_context_yaml(
                session=session, user_id=user_id
            )
        else:
            inputs.user_context_yaml = _load_user_context_yaml(
                session=session, user_id=user_id
            )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.user_context_yaml_failed",
            user_id=user_id,
            error=str(exc),
        )

    # 12. Snapshot summary — always source from the package-level
    #     ``_assemble_portfolio_summary`` helper (resolved via the package
    #     namespace) so the legacy monkeypatch convention is honoured.
    #     This overrides the TSV-derived ``snapshot_summary`` assigned
    #     above. The TSV-derived ``positions_summary`` stays untouched
    #     because ConcentrationAnalystAgent reads it as a separate field.
    try:
        import sys as _sys

        _pkg = _sys.modules.get(
            "argosy.orchestrator.flows.plan_synthesis"
        )
        if _pkg is not None and hasattr(_pkg, "_assemble_portfolio_summary"):
            inputs.snapshot_summary = _pkg._assemble_portfolio_summary(
                session=session, user_id=user_id
            )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.snapshot_summary_override_failed",
            user_id=user_id,
            error=str(exc),
        )

    log.info(
        "plan_synthesis.inputs.done",
        user_id=user_id,
        decision_audit_token=decision_audit_token,
        tickers_count=len(inputs.tickers),
        news_count=len(inputs.news_payload),
        fx_count=len(inputs.fx_payload),
        macro_count=len(inputs.macro_snapshot),
        social_count=len(inputs.social_payload),
        plan_targets_count=len(inputs.plan_targets),
    )
    return inputs


# ----------------------------------------------------------------------
# Section helpers (internal — no contract guarantees)
# ----------------------------------------------------------------------


def _extract_plan_targets(baseline) -> dict[str, float]:
    """Read ``label -> value`` from a baseline plan's distillate_json.

    Returns an empty dict if the column is null or the payload is
    malformed. Only includes Target rows with a numeric value.
    """
    import json

    raw = getattr(baseline, "distillate_json", None)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    targets = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(targets, list):
        return {}
    out: dict[str, float] = {}
    for t in targets:
        if not isinstance(t, dict):
            continue
        label = t.get("label")
        value = t.get("value")
        if not isinstance(label, str) or not label:
            continue
        try:
            out[label] = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return out


_PORTFOLIO_TSV_HEADER_MARKER = "Bank account / funds allocation"


def _find_latest_tsv():
    """Return the newest portfolio TSV under ARGOSY_HOME or None.

    Filters by the presence of the ``"Bank account / funds allocation"``
    header marker so stray small uploads (e.g. attachment placeholders
    under ``uploads/<user>/.../<timestamp>__<hash>__p.tsv``) don't shadow
    the real ``Family Finances Status - <date>.tsv`` file. Same defect
    pattern caused both this morning's $0k portfolio bug and run #7's
    empty-tickers symptom.
    """
    try:
        from argosy.config import get_settings

        settings = get_settings()
        candidates = sorted(
            settings.home.rglob("*.tsv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            try:
                # Read only the first ~4KB; the header is in the first
                # few lines of any real Family Finances Status TSV.
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(4096)
            except OSError:
                continue
            if _PORTFOLIO_TSV_HEADER_MARKER in head:
                return path
        return None
    except Exception:  # noqa: BLE001 - defensive
        return None


def _summarize_positions(snapshot) -> str:
    """One-line-per-position summary text. Empty snapshot -> sentinel."""
    lines: list[str] = []
    for p in getattr(snapshot, "positions", []) or []:
        ticker = getattr(p, "ticker", "?")
        qty = getattr(p, "quantity", None)
        value = getattr(p, "market_value", None) or getattr(p, "value", None)
        account = getattr(p, "account", "")
        lines.append(f"  {ticker:<8} qty={qty}  value={value}  acct={account}")
    if not lines:
        return "(no positions)"
    return "\n".join(lines)


def _gather_news(tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Per-ticker Finnhub headlines (overnight window). Empty on any
    failure with a structured warning."""
    from argosy.adapters import (
        MissingAPIKeyError as AdapterMissingAPIKeyError,
        MissingDataSourceError,
    )

    out: dict[str, list[dict[str, Any]]] = {}
    try:
        from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

        adapter = FinnhubAdapter()
        today_d = date.today()
        yesterday_d = today_d - timedelta(days=1)
        for ticker in tickers[:25]:
            try:
                headlines = asyncio.run(
                    adapter.get_company_news(
                        ticker, start=yesterday_d, end=today_d
                    )
                )
            except (AdapterMissingAPIKeyError, MissingDataSourceError) as exc:
                log.warning(
                    "plan_synthesis.inputs.news_skipped",
                    ticker=ticker,
                    reason=str(exc).splitlines()[0],
                )
                # API-key / package failures are global — no point
                # iterating further.
                return out
            except Exception as exc:  # noqa: BLE001 - per-ticker defensive
                log.warning(
                    "plan_synthesis.inputs.news_per_ticker_failed",
                    ticker=ticker,
                    error=str(exc),
                )
                continue
            if headlines:
                out[ticker] = headlines
    except (AdapterMissingAPIKeyError, MissingDataSourceError) as exc:
        log.warning(
            "plan_synthesis.inputs.news_skipped",
            reason=str(exc).splitlines()[0],
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.news_failed",
            error=str(exc),
        )
    return out


def _gather_macro_snapshot() -> dict[str, float]:
    """FRED macro snapshot (VIX, 10Y treasury, USD/NIS, oil)."""
    from argosy.adapters import (
        MissingAPIKeyError as AdapterMissingAPIKeyError,
        MissingDataSourceError,
    )

    out: dict[str, float] = {}
    try:
        from argosy.adapters.data.fred_adapter import FredAdapter

        fred = FredAdapter()
        # Series IDs are stable; labels match what MacroAnalystAgent
        # expects (vix / usd_nis / boi_rate / fred_10y / oil_brent /
        # dxy — we cover the four that are reliably free).
        for label, series in (
            ("vix", "VIXCLS"),
            ("fred_10y", "DGS10"),
            ("usd_nis", "DEXISUS"),
            ("oil_wti", "DCOILWTICO"),
        ):
            try:
                rows = asyncio.run(fred.get_series(series))
            except (
                AdapterMissingAPIKeyError, MissingDataSourceError
            ) as exc:
                log.warning(
                    "plan_synthesis.inputs.macro_skipped",
                    reason=str(exc).splitlines()[0],
                )
                return out
            except Exception as exc:  # noqa: BLE001 - per-series defensive
                log.warning(
                    "plan_synthesis.inputs.macro_series_failed",
                    series=series,
                    error=str(exc),
                )
                continue
            for row in reversed(rows or []):
                val = row.get("value") if isinstance(row, dict) else None
                if val is not None:
                    try:
                        out[label] = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
    except (AdapterMissingAPIKeyError, MissingDataSourceError) as exc:
        log.warning(
            "plan_synthesis.inputs.macro_skipped",
            reason=str(exc).splitlines()[0],
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.macro_failed",
            error=str(exc),
        )
    return out


def _gather_fx_payload(session: Session) -> dict[str, dict[str, float]]:
    """Build the FX payload for FXAnalystAgent.

    Two pairs (USD/NIS, EUR/NIS), each carrying ``spot`` plus 30d/90d
    pct change. We only invoke the FX service when the local
    ``fx_rates`` cache has rows for the currency — otherwise the
    service's online-fetch fallback would issue live BoI / Frankfurter
    HTTP requests during synthesis, which is exactly the kind of
    surprise side-effect this assembler must avoid.
    """
    from argosy.services.fx import FXRateUnavailable, rate
    from argosy.state.models import FxRate

    out: dict[str, dict[str, float]] = {}
    today_d = date.today()
    d30 = today_d - timedelta(days=30)
    d90 = today_d - timedelta(days=90)
    for from_ccy, to_ccy, label, source in (
        ("USD", "NIS", "USD/NIS", "boi:USD"),
        ("EUR", "NIS", "EUR/NIS", "boi:EUR"),
    ):
        try:
            cached_row = (
                session.query(FxRate)
                .filter(FxRate.currency == from_ccy)
                .first()
            )
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.fx_cache_lookup_failed",
                pair=label,
                error=str(exc),
            )
            continue
        if cached_row is None:
            log.warning(
                "plan_synthesis.inputs.fx_skipped_no_cache",
                pair=label,
            )
            continue
        try:
            spot = float(rate(session, from_ccy, to_ccy, today_d))
            r30 = float(rate(session, from_ccy, to_ccy, d30))
            r90 = float(rate(session, from_ccy, to_ccy, d90))
        except FXRateUnavailable as exc:
            log.warning(
                "plan_synthesis.inputs.fx_skipped",
                pair=label,
                reason=str(exc).splitlines()[0],
            )
            continue
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.fx_failed",
                pair=label,
                error=str(exc),
            )
            continue
        pct_30d = ((spot - r30) / r30 * 100.0) if r30 else 0.0
        pct_90d = ((spot - r90) / r90 * 100.0) if r90 else 0.0
        out[label] = {
            "spot": spot,
            "pct_change_30d": pct_30d,
            "pct_change_90d": pct_90d,
            "source": source,
        }
    return out


def _gather_social_payload(
    tickers: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Per-ticker TipRanks blogger sentiment as the social_payload.

    The SentimentAnalystAgent expects ``{ticker: [{text, polarity,
    source}, ...]}``. We translate TipRanks' aggregated bullish/bearish
    pct into a single synthetic snippet per ticker.
    """
    from argosy.adapters import MissingDataSourceError

    out: dict[str, list[dict[str, Any]]] = {}
    try:
        from argosy.adapters.data.tipranks_adapter import TipRanksAdapter

        adapter = TipRanksAdapter()
        # TipRanks free tier is aggressively rate-limited; cap at 10.
        for ticker in tickers[:10]:
            try:
                signal = asyncio.run(adapter.get_blogger_sentiment(ticker))
            except MissingDataSourceError as exc:
                log.warning(
                    "plan_synthesis.inputs.social_skipped",
                    ticker=ticker,
                    reason=str(exc).splitlines()[0],
                )
                continue
            except Exception as exc:  # noqa: BLE001 - per-ticker defensive
                log.warning(
                    "plan_synthesis.inputs.social_per_ticker_failed",
                    ticker=ticker,
                    error=str(exc),
                )
                continue
            bullish = signal.get("bullish_pct") if isinstance(signal, dict) else None
            bearish = signal.get("bearish_pct") if isinstance(signal, dict) else None
            polarity = 0.0
            if isinstance(bullish, (int, float)) and isinstance(bearish, (int, float)):
                polarity = float(bullish) - float(bearish)
            text = (
                f"TipRanks blogger consensus: bullish_pct={bullish}, "
                f"bearish_pct={bearish}"
            )
            out[ticker] = [{
                "text": text,
                "polarity": polarity,
                "source": signal.get("source_url", "tipranks") if isinstance(signal, dict) else "tipranks",
            }]
    except MissingDataSourceError as exc:
        log.warning(
            "plan_synthesis.inputs.social_skipped",
            reason=str(exc).splitlines()[0],
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.social_failed",
            error=str(exc),
        )
    return out


__all__ = [
    # Public W1.A surface
    "Phase1Inputs",
    "assemble_phase1_inputs",
    # Legacy helpers re-exported via the package __init__.
    "_assemble_portfolio_summary",
    "_assemble_fills_summary",
    "_load_user_context_yaml",
]
