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

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from argosy.logging import get_logger

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Legacy helpers (monkeypatched in tests)
# ----------------------------------------------------------------------


def _assemble_portfolio_summary(*, session, user_id) -> str:
    """Build a compact portfolio-state summary for synthesis input.

    Reads the latest Family Finances Status TSV from ``ARGOSY_HOME`` (via
    ``_find_latest_tsv`` which filters on the canonical header marker so
    stray uploads don't shadow it) and produces the same per-position
    summary text used at Phase 1. Synthesizer Phase 3 reads this as the
    "current state" text it draws horizon targets against.

    Returns ``(no positions)`` when no TSV is reachable so the synthesizer
    prompt sees a stable sentinel rather than a placeholder string the
    fund manager would (correctly) reject as null-data.

    The ``session`` + ``user_id`` arguments are kept for monkeypatch
    compatibility with the test suite (tests stub this helper directly).
    """
    try:
        tsv_path = _find_latest_tsv()
        if tsv_path is None:
            return "(no positions)"
        from argosy.ingest.tsv import parse_portfolio_tsv

        snapshot = parse_portfolio_tsv(tsv_path)
        return _summarize_positions(snapshot)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning(
            "plan_synthesis.legacy_assemble_portfolio_failed",
            user_id=user_id, error=str(exc),
        )
        return "(no positions — TSV parse failed)"


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

    # HouseholdBudgetAnalystAgent
    household_budget_payload: dict[str, Any] = field(default_factory=dict)

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
            # PortfolioPosition uses `.symbol` (not `.ticker`); filter out the
            # cash sentinel "-" and any other non-ticker values.
            inputs.tickers = sorted(
                {
                    p.symbol for p in getattr(snapshot, "positions", []) or []
                    if getattr(p, "symbol", None) and p.symbol != "-"
                }
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

    # NVDA YTD sales accounting — ConcentrationAnalystAgent reads these as
    # ``nvda_shares_sold_ytd`` + ``nvda_target_shares_ytd``. Sourced from
    # ``argosy.services.nvda_sales_history`` (fills table preferred, TSV
    # ``nvda_sales`` block as fallback; target pro-rated from the active
    # draft's annual NVDA-sale plan). Best-effort: missing data logs a
    # WARNING and leaves the fields at 0 so synthesis doesn't crash.
    try:
        from argosy.services.nvda_sales_history import (
            compute_nvda_shares_sold_ytd,
        )

        inputs.nvda_shares_sold_ytd = compute_nvda_shares_sold_ytd(
            session, user_id
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.nvda_shares_sold_ytd_failed",
            user_id=user_id, error=str(exc),
        )
    try:
        from argosy.services.nvda_sales_history import (
            compute_nvda_target_shares_ytd,
        )

        inputs.nvda_target_shares_ytd = compute_nvda_target_shares_ytd(
            session, user_id
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.nvda_target_shares_ytd_failed",
            user_id=user_id, error=str(exc),
        )

    # TaxAnalyst inputs — read from the `lots` table + identity_yaml RSU
    # grants. Both are best-effort and degrade to an explanatory empty
    # sentinel; TaxAnalyst's prompt is tolerant of "(no lots imported)".
    try:
        inputs.lots_summary = _assemble_lots_summary(session, user_id)
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.lots_summary_failed",
            user_id=user_id,
            error=str(exc),
        )
    try:
        inputs.rsu_schedule_summary = _assemble_rsu_schedule_summary(session, user_id)
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.rsu_schedule_summary_failed",
            user_id=user_id,
            error=str(exc),
        )

    # HouseholdBudgetAnalystAgent payload — burn + income + safe-withdrawal
    # context. Assembled from identity_yaml + the parsed TSV positions
    # (already loaded into Phase1Inputs at this point).
    try:
        inputs.household_budget_payload = _assemble_household_budget_payload(
            session, user_id, positions_summary=inputs.positions_summary,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.household_budget_payload_failed",
            user_id=user_id,
            error=str(exc),
        )

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

    # 7. Fundamentals (Finnhub /stock/metric). Per-ticker, capped at
    #    25 tickers to keep the free-tier rate limit happy. Same
    #    defensive shape as the news section: per-ticker try/except,
    #    global key/data errors abort the loop, all failures degrade
    #    to an empty payload.
    if inputs.tickers:
        try:
            inputs.fundamentals_payload = _gather_fundamentals(inputs.tickers)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.fundamentals_failed",
                user_id=user_id,
                error=str(exc),
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

    # 9. Indicators — yfinance-backed (W3b.D). Per-ticker, capped at 25
    #    to match the news fan-out and keep us well clear of any yfinance
    #    rate-limit surprise.
    if inputs.tickers:
        try:
            inputs.indicators_payload = _gather_indicators_payload(inputs.tickers)
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.indicators_failed",
                user_id=user_id,
                error=str(exc),
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
        indicators_count=len(inputs.indicators_payload),
        fundamentals_count=len(inputs.fundamentals_payload),
        plan_targets_count=len(inputs.plan_targets),
        nvda_shares_sold_ytd=inputs.nvda_shares_sold_ytd,
        nvda_target_shares_ytd=inputs.nvda_target_shares_ytd,
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


def _assemble_household_budget_payload(
    session: Session, user_id: str, *, positions_summary: str = "",
) -> dict[str, Any]:
    """Read household-budget context from identity_yaml + the TSV total.

    Returns the dict shape HouseholdBudgetAnalystAgent.build_prompt
    expects. Best-effort: any parse failure yields partial dict; the
    agent's prompt is tolerant of missing fields.
    """
    from argosy.state.models import UserContext

    payload: dict[str, Any] = {}

    ctx = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    if ctx is None or not ctx.identity_yaml:
        return payload

    try:
        import yaml as _yaml

        data = _yaml.safe_load(ctx.identity_yaml) or {}
    except Exception:  # noqa: BLE001
        return payload
    if not isinstance(data, dict):
        return payload

    # Direct fields the synthesizer needs to see.
    for key in (
        "monthly_expenses_total_nis",
        "monthly_expenses_window",
        "rsu_annual_usd",
        "emergency_fund_months",
        "employment_household_net_to_bank_monthly",
        "employment_user_net_monthly_nis",
        "spouse_net_monthly_nis",
        "espp_monthly_nis_mar_onwards_2026",
    ):
        if key in data:
            payload[key] = data[key]

    payload["monthly_burn_nis"] = data.get("monthly_expenses_total_nis")
    payload["monthly_burn_window"] = data.get("monthly_expenses_window")

    # Build a normalized income_streams list from the various per-stream
    # fields the intake captured (employment user, spouse, ESPP, RSU).
    streams: list[dict[str, Any]] = []
    if data.get("employment_user_net_monthly_nis"):
        streams.append({
            "source": "employment_user_net",
            "monthly_nis": data["employment_user_net_monthly_nis"],
            "note": "",
        })
    if data.get("spouse_net_monthly_nis"):
        streams.append({
            "source": "employment_spouse_net",
            "monthly_nis": data["spouse_net_monthly_nis"],
            "note": "",
        })
    if data.get("espp_monthly_nis_mar_onwards_2026"):
        streams.append({
            "source": "espp_net",
            "monthly_nis": data["espp_monthly_nis_mar_onwards_2026"],
            "note": "Mar 2026 onwards; Jan-Feb 2026 was different",
        })
    if data.get("rsu_annual_usd"):
        streams.append({
            "source": "rsu_gross_annual_usd",
            "monthly_nis": None,
            "note": f"{data['rsu_annual_usd']} USD/yr; convert via FX",
        })
    payload["income_streams"] = streams

    # Liquid assets — read from the positions_summary text we already
    # computed for ConcentrationAnalyst. Parse the header line that says
    # "Total tradeable positions: N holdings, $XXXk USD".
    if positions_summary:
        import re as _re

        m = _re.search(
            r"Total tradeable positions:\s*\d+\s*holdings,\s*\$([\d,\.]+)k USD",
            positions_summary,
        )
        if m:
            try:
                liquid_k = float(m.group(1).replace(",", ""))
                payload["liquid_assets_usd_k"] = liquid_k
                # 4% rule: annual = liquid * 0.04 → monthly USD.
                # Liquid is in thousands so multiply by 1000.
                payload["safe_withdrawal_monthly_usd"] = round(
                    (liquid_k * 1000) * 0.04 / 12.0, 2
                )
            except ValueError:
                pass

    return payload


def _assemble_lots_summary(session: Session, user_id: str) -> str:
    """Read tax lots from the ``lots`` table and emit a TaxAnalyst-shaped text.

    Returns ``(no lots imported)`` if the table is empty (the user hasn't
    run ``argosy ingest schwab-lots`` yet). Per-ticker grouping with
    quantity + average cost basis + acquired-date range; lets the Tax
    Analyst reason about long-vs-short-term gains, harvesting opportunity,
    and Section 102 cost-basis residue.
    """
    from argosy.state.models import Lot

    rows = session.execute(
        select(Lot)
        .where(Lot.user_id == user_id)
        .order_by(Lot.ticker, Lot.acquired_at)
    ).scalars().all()
    if not rows:
        return "(no lots imported — run `argosy ingest schwab-lots <csv>` to populate)"

    # Group by ticker.
    by_ticker: dict[str, list[Lot]] = {}
    for r in rows:
        by_ticker.setdefault(r.ticker, []).append(r)

    lines = [f"Tax lots: {len(rows)} lots across {len(by_ticker)} tickers"]
    for ticker, lots in sorted(by_ticker.items()):
        total_qty = sum(float(l.quantity or 0) for l in lots)
        total_basis = sum(float(l.cost_basis_usd or 0) for l in lots)
        avg_basis = total_basis / total_qty if total_qty else 0.0
        dates = [l.acquired_at for l in lots if l.acquired_at is not None]
        date_range = ""
        if dates:
            earliest = min(dates).date().isoformat()
            latest = max(dates).date().isoformat()
            date_range = f"  acquired {earliest} → {latest}" if earliest != latest else f"  acquired {earliest}"
        lines.append(
            f"  {ticker:<8}  {total_qty:g} sh  total_basis=${total_basis:,.0f}"
            f"  avg=${avg_basis:.2f}/sh  ({len(lots)} lots){date_range}"
        )
    return "\n".join(lines)


def _assemble_rsu_schedule_summary(session: Session, user_id: str) -> str:
    """Read identity_yaml::rsu_grants.grants[] and emit a TaxAnalyst-shaped text.

    Pulls from the structured field populated by intake_extractor (or the
    one-shot backfill in T1.3). Surfaces award_id, award_date, quarterly
    vest count, and the implied 12-month vest total. Empty when no grants
    are catalogued.
    """
    from argosy.state.models import UserContext

    ctx = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    if ctx is None or not ctx.identity_yaml:
        return "(no identity_yaml — intake hasn't been completed)"

    try:
        import yaml as _yaml

        data = _yaml.safe_load(ctx.identity_yaml) or {}
    except Exception:  # noqa: BLE001 - defensive
        return "(identity_yaml parse failed)"
    if not isinstance(data, dict):
        return "(identity_yaml not a dict)"

    rsu = data.get("rsu_grants") or {}
    grants = rsu.get("grants") if isinstance(rsu, dict) else None
    if not grants or not isinstance(grants, list):
        return "(no rsu_grants.grants[] catalogued)"

    lines = [f"RSU grants ({len(grants)} active):"]
    total_quarterly = 0
    for g in grants:
        if not isinstance(g, dict):
            continue
        award_id = g.get("award_id", "?")
        award_date = g.get("award_date", "?")
        quarterly = g.get("quarterly_shares") or 0
        try:
            qty = int(quarterly)
        except (TypeError, ValueError):
            qty = 0
        total_quarterly += qty
        note = g.get("note", "")
        suffix = f"  — {note}" if note else ""
        lines.append(
            f"  award={award_id}  granted={award_date}  quarterly={qty} sh{suffix}"
        )
    if isinstance(rsu, dict):
        implied_price = rsu.get("implied_nvda_price_usd")
        next_12m = rsu.get("next_12_months_shares")
        if next_12m or implied_price:
            footer_bits = []
            if next_12m:
                footer_bits.append(f"next 12 months: {next_12m} shares")
            if implied_price:
                footer_bits.append(f"implied NVDA price: ${implied_price}")
            lines.append("  " + " · ".join(footer_bits))
    return "\n".join(lines)


def _summarize_positions(snapshot) -> str:
    """One-line-per-position summary text. Empty snapshot -> sentinel.

    Reads the structured ``PortfolioPosition`` fields produced by
    ``argosy.ingest.tsv``:

    * ``symbol``              — ticker (was ``ticker``, never populated)
    * ``shares``              — quantity (was ``quantity``, never populated)
    * ``current_value_local`` — value in the position's own currency
    * ``usd_value_k``         — USD value in thousands (when filled by TSV)
    * ``current_price``       — last price snapshot
    * ``location``            — broker / account where the position lives

    The prior implementation was a stub: it queried ``.ticker``, ``.quantity``,
    ``.market_value`` and ``.account`` — none of which exist on
    ``PortfolioPosition`` — so every line rendered as ``qty=None value=None
    acct=''``. ConcentrationAnalyst then disclaimed its own input as
    "structural nulls", which Fund Manager flagged as the rationale-without-
    verifiable-base objection on run #19. With this fix the analysts now see
    real positions; FM has a chance to evaluate the draft on its merits
    rather than the upstream-data gap.

    Skips rows that have no symbol (cash sentinel lines, real-estate rows,
    pension rows) so the summary stays focused on tradeable holdings.
    """
    lines: list[str] = []
    total_usd_k = 0.0
    for p in getattr(snapshot, "positions", []) or []:
        symbol = (getattr(p, "symbol", "") or "").strip()
        if not symbol or symbol == "-":
            continue
        shares = getattr(p, "shares", None)
        usd_k = getattr(p, "usd_value_k", None) or 0.0
        local_value = getattr(p, "current_value_local", None)
        price = getattr(p, "current_price", None)
        currency = getattr(p, "currency", "") or ""
        location = getattr(p, "location", "") or ""
        asset_type = getattr(p, "asset_type", "") or ""

        total_usd_k += usd_k

        # Format: "  TICKER       qty=N shares    value=$Xk USD (Y local CCY)    @ $price    acct=Z (TYPE)"
        # Numbers are optional — some positions have shares but no price,
        # or value only. Skip None fields rather than printing "None".
        qty_str = f"qty={shares:g}" if isinstance(shares, (int, float)) else "qty=?"
        if usd_k:
            value_str = f"value=${usd_k:,.1f}k USD"
            if local_value and currency and currency.upper() != "USD":
                value_str += f" ({local_value:,.0f} {currency})"
        elif local_value and currency:
            value_str = f"value={local_value:,.0f} {currency}"
        else:
            value_str = "value=?"
        price_str = f"@ ${price:.2f}" if isinstance(price, (int, float)) else ""
        acct_str = f"acct={location}" if location else ""
        type_str = f"({asset_type})" if asset_type else ""

        parts = [f"  {symbol:<10}", qty_str, value_str]
        if price_str:
            parts.append(price_str)
        if acct_str:
            parts.append(acct_str)
        if type_str:
            parts.append(type_str)
        lines.append("  ".join(parts))

    if not lines:
        return "(no positions)"
    header = f"Total tradeable positions: {len(lines)} holdings, ${total_usd_k:,.1f}k USD\n"
    return header + "\n".join(lines)


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


def _gather_fundamentals(
    tickers: list[str],
) -> dict[str, dict[str, Any]]:
    """Per-ticker Finnhub fundamentals (W3b.E).

    Calls ``FinnhubAdapter.get_company_financials`` for up to the first
    25 tickers. Same defensive shape as ``_gather_news``: API-key /
    missing-package failures abort the loop (they're global), individual
    ticker failures log + continue. Israeli ETFs and other non-US
    listings typically return empty ``metric`` blocks, surface as
    ``MissingDataSourceError`` per-ticker, get skipped, do not raise.
    """
    from argosy.adapters import (
        MissingAPIKeyError as AdapterMissingAPIKeyError,
        MissingDataSourceError,
    )

    out: dict[str, dict[str, Any]] = {}
    try:
        from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

        adapter = FinnhubAdapter()
        for ticker in tickers[:25]:
            try:
                payload = asyncio.run(
                    adapter.get_company_financials(ticker)
                )
            except AdapterMissingAPIKeyError as exc:
                log.warning(
                    "plan_synthesis.inputs.fundamentals_skipped",
                    ticker=ticker,
                    reason=str(exc).splitlines()[0],
                )
                # API-key failure is global — stop iterating.
                return out
            except MissingDataSourceError as exc:
                # Per-ticker "no data" — Finnhub doesn't cover this
                # symbol (typical for Israeli ETFs / non-US listings).
                # Skip and continue.
                log.warning(
                    "plan_synthesis.inputs.fundamentals_per_ticker_skipped",
                    ticker=ticker,
                    reason=str(exc).splitlines()[0],
                )
                continue
            except Exception as exc:  # noqa: BLE001 - per-ticker defensive
                log.warning(
                    "plan_synthesis.inputs.fundamentals_per_ticker_failed",
                    ticker=ticker,
                    error=str(exc),
                )
                continue
            if payload:
                out[ticker] = payload
    except (AdapterMissingAPIKeyError, MissingDataSourceError) as exc:
        log.warning(
            "plan_synthesis.inputs.fundamentals_skipped",
            reason=str(exc).splitlines()[0],
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.fundamentals_failed",
            error=str(exc),
        )
    return out


def _gather_indicators_payload(
    tickers: list[str],
) -> dict[str, dict[str, Any]]:
    """Per-ticker yfinance-derived technical indicators (W3b.D).

    Capped at the first 25 tickers (same as the news fan-out) to keep us
    well clear of any yfinance rate-limit surprise. Empty on a global
    failure (e.g. package missing) with a structured warning; per-ticker
    failures degrade to a missing entry rather than aborting the loop.
    """
    from argosy.adapters import (
        MissingAPIKeyError as AdapterMissingAPIKeyError,
        MissingDataSourceError,
    )

    out: dict[str, dict[str, Any]] = {}
    try:
        from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

        adapter = YFinanceAdapter()
        for ticker in tickers[:25]:
            try:
                payload = asyncio.run(adapter.get_indicators(ticker))
            except (AdapterMissingAPIKeyError, MissingDataSourceError) as exc:
                log.warning(
                    "plan_synthesis.inputs.indicators_skipped",
                    ticker=ticker,
                    reason=str(exc).splitlines()[0],
                )
                # MissingDataSourceError at the package level is global —
                # bail rather than retry per ticker.
                if "package is not installed" in str(exc):
                    return out
                continue
            except Exception as exc:  # noqa: BLE001 - per-ticker defensive
                log.warning(
                    "plan_synthesis.inputs.indicators_per_ticker_failed",
                    ticker=ticker,
                    error=str(exc).splitlines()[0],
                )
                continue
            if payload:
                out[ticker] = payload
    except (AdapterMissingAPIKeyError, MissingDataSourceError) as exc:
        log.warning(
            "plan_synthesis.inputs.indicators_skipped",
            reason=str(exc).splitlines()[0],
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning(
            "plan_synthesis.inputs.indicators_failed",
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

        # T3.2: pair TipRanks with a Finnhub fallback adapter so when
        # TipRanks 403s (anti-bot) we still produce a non-empty social
        # signal. The Finnhub adapter has its own track_adapter_call
        # wrapping so the agent tree shows both outcomes per ticker.
        finnhub_fallback: Any | None = None
        try:
            from argosy.adapters.data.finnhub_adapter import FinnhubAdapter

            finnhub_fallback = FinnhubAdapter()
        except Exception as exc:  # noqa: BLE001 - defensive
            log.warning(
                "plan_synthesis.inputs.social_finnhub_fallback_unavailable",
                error=str(exc),
            )
        adapter = TipRanksAdapter(finnhub=finnhub_fallback)
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
            # T3.2: get_blogger_sentiment now returns a zero-shape default
            # when BOTH TipRanks and Finnhub fail rather than raising.
            # Skip the phantom-row case so the analyst doesn't see a
            # synthetic "0% bullish / 0% bearish" snippet that looks like
            # a real measurement.
            if not (
                isinstance(bullish, (int, float)) and isinstance(bearish, (int, float))
                and (float(bullish) > 0 or float(bearish) > 0)
            ):
                log.info(
                    "plan_synthesis.inputs.social_skipped_empty",
                    ticker=ticker,
                )
                continue
            polarity = float(bullish) - float(bearish)
            source_url = (
                signal.get("source_url", "tipranks")
                if isinstance(signal, dict) else "tipranks"
            )
            # Label depends on which provider actually answered. The
            # source_url is the cheapest signal we have for that.
            provider = (
                "Finnhub social-sentiment"
                if "finnhub.io" in (source_url or "")
                else "TipRanks blogger consensus"
            )
            text = (
                f"{provider}: bullish_pct={bullish}, bearish_pct={bearish}"
            )
            out[ticker] = [{
                "text": text,
                "polarity": polarity,
                "source": source_url,
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
