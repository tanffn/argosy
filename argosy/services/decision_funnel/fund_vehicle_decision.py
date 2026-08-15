"""Fund/ETF vehicle verdict path (collective-instrument counterpart to run_deep_decision).

Gap this closes: ``run_deep_decision`` runs the per-stock analyst fleet
(fundamentals / news / sentiment / macro) and asks equity questions that are
meaningless for an ETF — PE, EPS, moat, insider flow are all undefined for a
passive index fund. This module provides the correct dispatch path for
collective instruments (structure == ETF | fund | bond | reit).

Entry point: ``run_fund_vehicle_decision`` — same signature seam as
``run_deep_decision``, returning a ``DeepDecisionOutcome`` so
``verdict_coverage.ensure_coverage`` can use it transparently (the coverage
sweep checks ground-truth verdict identity, not the outcome envelope).

Under the hood it runs ONE ``FundVehicleAnalystAgent`` call (Opus) and writes
the resulting verdict to the registry via ``write_verdict``. A single-agent
verdict (no bull/bear/trader debate) is appropriate here because:
  - ETF verdicts are primarily driven by STRUCTURAL facts (domicile, mandate
    fit, overlap) that are deterministic given the instrument_reference data.
  - A five-way debate fleet for "is FWRA still the right global-equity vehicle"
    is cost-disproportionate: the structural questions do not benefit from
    independent adversarial derivation the way a single-stock moat judgment does.
  - The falsifier contract ensures the settled verdict is re-challenged whenever
    the structural facts change (tracking deviation, TER increase, domicile
    migration, AUM threshold).

Cost: one Opus adaptive-thinking call (~$0.05–$0.15 per fund, 12 funds ≈ $1–2
total for the book). Far cheaper than a 5-agent equity fleet per fund.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta, timezone, datetime as _dt
from typing import Any, Literal

from argosy.logging import get_logger

_log = get_logger("argosy.services.decision_funnel.fund_vehicle_decision")

# Module-level import so tests can patch this function at
# ``argosy.services.decision_funnel.fund_vehicle_decision.check_pushback_gate``.
# The lazy inline import pattern used elsewhere works fine for production
# (deferred import avoids circular deps at startup) but prevents patching.
# Importing here is safe: verdict_registry has no circular dep on this module.
from argosy.services.verdict_registry import check_pushback_gate  # noqa: E402

# Verdict next-validation: funds are reviewed on an annual cycle unless a
# dated_event or metric_condition fires earlier.
_DEFAULT_NEXT_VALIDATION_DAYS = 365


@dataclass(frozen=True)
class FundVehicleOutcome:
    """Structured outcome from ``run_fund_vehicle_decision``."""

    ticker: str
    status: Literal["completed", "blocked", "error"]
    verdict_id: int | None = None
    verdict: str | None = None
    conviction: str | None = None
    blocked_reason: str | None = None
    blocked_by: str | None = None


# ---------------------------------------------------------------------------
# Context packet builder (deterministic — no LLM)
# ---------------------------------------------------------------------------

def _load_domain_knowledge() -> str:
    """Load the two estate/withholding domain_knowledge files as a block.

    Best-effort: a missing file is skipped with a note. These are the
    structural tax facts the agent must cite for domicile reasoning.
    """
    import pathlib

    repo_root = pathlib.Path(__file__).parents[3]  # argosy/services/decision_funnel → root
    snippets: list[str] = []
    for rel in (
        "domain_knowledge/tax/us/estate_tax_nonresidents.md",
        "domain_knowledge/tax/us/nonresident_withholding.md",
    ):
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
            # Trim to ≤3000 chars per file so the agent packet stays bounded.
            if len(text) > 3000:
                text = text[:2900] + "\n...[truncated]"
            snippets.append(f"=== {rel} ===\n{text}")
        except OSError:
            snippets.append(f"=== {rel} === [FILE NOT FOUND]")
    return "\n\n".join(snippets)


async def _build_fund_context(
    ticker: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    """Assemble the deterministic context packet for the fund agent.

    Sources (all best-effort; a missing source adds a null value, not an error):
      - instrument_reference: structure, asset_class, sector, region, estate_safe
      - instrument_facts: us_weight + source
      - position context: current book weight, USD value
      - plan role: from the target allocation doc
      - other held tickers: from portfolio snapshot (for overlap detection)
    """
    ctx: dict[str, Any] = {"ticker": ticker.upper()}

    # 1 — Instrument reference (structure, asset class, estate_safe)
    try:
        from argosy.services import instrument_reference as iref
        ref = iref.lookup(ticker)
        if ref is not None:
            ctx["structure"] = ref.structure
            ctx["asset_class"] = ref.asset_class
            ctx["sector"] = ref.sector
            ctx["region"] = ref.region
            ctx["estate_safe"] = ref.estate_safe
    except Exception:  # noqa: BLE001
        _log.warning("fund_vehicle_decision.iref_lookup_failed", ticker=ticker)

    # 2 — US-weight look-through (instrument_facts)
    try:
        from argosy.services.allocation_author.instrument_facts import lookup_facts
        facts = lookup_facts(ticker)
        if facts is not None:
            ctx["us_weight"] = facts.us_weight
            ctx["us_weight_source"] = facts.source
    except Exception:  # noqa: BLE001
        _log.warning("fund_vehicle_decision.facts_lookup_failed", ticker=ticker)

    # 3 — Domicile country heuristic (from estate_safe + region).
    # Irish UCITS -> IE; US-domiciled -> US. Best-effort.
    estate_safe = ctx.get("estate_safe")
    region = ctx.get("region", "")
    if estate_safe is True and region not in ("US",):
        ctx["domicile_country"] = "IE"  # best guess: UCITS = Irish
    elif estate_safe is False:
        ctx["domicile_country"] = "US"

    # 4 — Position context (current book weight + USD value) and other holdings
    try:
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker
        from argosy.state import db as db_mod
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
            row_to_snapshot,
        )

        _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
        _sf = sessionmaker(
            bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
            expire_on_commit=False,
        )
        _sess = _sf()
        try:
            snap_row = get_latest_snapshot_row(_sess, user_id)
            if snap_row is not None:
                snap = row_to_snapshot(snap_row)
                positions = getattr(snap, "positions", []) or []
                total_k = sum(
                    float(getattr(p, "usd_value_k", 0) or 0) for p in positions
                )
                held_syms = []
                for p in positions:
                    sym = (getattr(p, "symbol", "") or "").strip().upper()
                    if not sym or sym == "-":
                        continue
                    usd_k = float(getattr(p, "usd_value_k", 0) or 0)
                    if sym == ticker.upper():
                        ctx["position_usd_value"] = round(usd_k * 1000, 2)
                        if total_k > 0:
                            ctx["position_weight_pct"] = round(
                                100.0 * usd_k / total_k, 2
                            )
                    else:
                        held_syms.append(sym)
                ctx["other_book_holdings"] = sorted(set(held_syms))
        finally:
            _sess.close()
    except Exception:  # noqa: BLE001
        _log.warning("fund_vehicle_decision.position_context_failed", ticker=ticker)

    # 5 — Plan role (from the target allocation doc)
    try:
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker
        from argosy.state import db as db_mod
        from argosy.state.queries import get_current_plan
        from argosy.services.target_allocation_doc import load_plan_target_allocation

        _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
        _sf2 = sessionmaker(
            bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
            expire_on_commit=False,
        )
        _sess2 = _sf2()
        try:
            pv = get_current_plan(_sess2, user_id)
            doc = load_plan_target_allocation(pv) if pv is not None else None
            if doc is not None:
                tk_upper = ticker.upper()
                for cls in getattr(doc, "classes", []) or []:
                    for inst in getattr(cls, "instruments", []) or []:
                        if (getattr(inst, "symbol", "") or "").upper() == tk_upper:
                            rat = (getattr(inst, "rationale", "") or "")[:300]
                            label = getattr(cls, "label", "") or ""
                            ctx["plan_role"] = (
                                f"{label}: {rat}" if rat else label
                            )
                            break
        finally:
            _sess2.close()
    except Exception:  # noqa: BLE001
        _log.warning("fund_vehicle_decision.plan_role_failed", ticker=ticker)

    return ctx


# ---------------------------------------------------------------------------
# Verdict writer (deterministic — reuses the shared registry)
# ---------------------------------------------------------------------------

def _write_fund_verdict(
    *,
    user_id: str,
    ticker: str,
    report: "FundVehicleReport",  # noqa: F821  (forward ref, imported below)
    source_decision_run_id: int | None,
) -> int | None:
    """Write the agent's report to the verdict registry. Returns verdict_id or None."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.verdict_registry import write_verdict
    from argosy.state import db as db_mod

    next_val = (
        (_dt.now(timezone.utc) + timedelta(days=_DEFAULT_NEXT_VALIDATION_DAYS)).date()
    )

    _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    _sf = sessionmaker(
        bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
        expire_on_commit=False,
    )
    _sess = _sf()
    try:
        # Normalise conviction: agent uses HIGH/MEDIUM/LOW; registry wants MED.
        conv = str(report.conviction.value if hasattr(report.conviction, "value")
                   else report.conviction).upper()
        if conv == "MEDIUM":
            conv = "MED"

        # Build typed revisit_triggers list — validate kinds.
        # The model sometimes emits "type" instead of "kind" (JSON schema uses
        # "kind" but the model may echo "type" from the examples in the prompt).
        # Normalise "type" → "kind" before validation so triggers are not silently
        # dropped.
        triggers = []
        for t in report.revisit_triggers or []:
            raw = dict(t or {})
            # Normalise "type" → "kind" if "kind" is absent.
            if "kind" not in raw and "type" in raw:
                raw["kind"] = raw.pop("type")
            kind = str(raw.get("kind") or "")
            if kind in ("price_below", "price_above", "metric_condition", "dated_event"):
                triggers.append(raw)
            else:
                _log.warning(
                    "fund_vehicle_decision.invalid_trigger_kind",
                    ticker=ticker,
                    kind=kind,
                )

        v = write_verdict(
            _sess,
            user_id=user_id,
            subject=ticker.upper(),
            verdict=report.verdict.upper(),
            conviction=conv,
            falsifiers=list(report.falsifiers or []),
            revisit_triggers=triggers,
            next_validation=next_val,
            source_decision_run_id=source_decision_run_id,
            reasoning_md=report.reasoning_md or "",
            settled=True,
        )
        _sess.commit()
        _log.info(
            "fund_vehicle_decision.verdict_written",
            ticker=ticker,
            verdict=report.verdict,
            conviction=conv,
            verdict_id=v.id,
        )
        return v.id
    except Exception:  # noqa: BLE001
        _log.exception("fund_vehicle_decision.verdict_write_failed", ticker=ticker)
        try:
            _sess.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        try:
            _sess.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_fund_vehicle_decision(
    *,
    user_id: str,
    ticker: str,
    funnel_meta: dict | None = None,
    source_decision_run_id: int | None = None,
    _agent_factory: "Any | None" = None,  # injectable for tests
) -> FundVehicleOutcome:
    """Run the fund-vehicle verdict path for one ETF/fund/bond/REIT ticker.

    Never raises — returns a structured ``FundVehicleOutcome``.  The verdict
    is written to the registry (write_verdict) so the caller's ground-truth
    identity check (DEFECT B in verdict_coverage) can detect a new/changed
    settled row.

    Args:
        user_id: tenant id.
        ticker: the fund ticker (case-insensitive; normalised upper internally).
        funnel_meta: optional metadata from the caller (source, revisit_reason,
            cited_new_facts). Threaded to the agent for provenance.
        source_decision_run_id: if the caller opened a DecisionRun row, pass
            its id so the written verdict is linked to it. For coverage-sweep
            callers this is None (no formal run is opened).
        _agent_factory: injectable for tests. Called with no args; must return
            a FundVehicleAnalystAgent (or compatible duck type). Defaults to
            constructing a real FundVehicleAnalystAgent.
    """
    from argosy.agents.fund_vehicle_analyst import FundVehicleAnalystAgent

    tk = (ticker or "").upper()
    _log.info("fund_vehicle_decision.start", ticker=tk, user_id=user_id)

    # ---- Pushback gate -------------------------------------------------------
    # Reuse the same registry pushback gate as run_deep_decision so a defended
    # fund verdict is not re-derived every sweep.
    try:
        import sqlalchemy as sa
        from sqlalchemy.orm import sessionmaker
        from argosy.state import db as db_mod

        _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
        _sf = sessionmaker(
            bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
            expire_on_commit=False,
        )
        _gate_sess = _sf()
        try:
            _cited = None
            if funnel_meta and isinstance(funnel_meta.get("cited_new_facts"), list):
                _cited = funnel_meta["cited_new_facts"]
            _gate = check_pushback_gate(
                _gate_sess,
                user_id=user_id,
                subject=tk,
                cited_new_facts=_cited,
            )
        finally:
            _gate_sess.close()
        if _gate.defended and _gate.standing is not None:
            _log.info(
                "fund_vehicle_decision.verdict_defended",
                ticker=tk,
                standing=_gate.standing.verdict,
            )
            return FundVehicleOutcome(
                ticker=tk,
                status="blocked",
                verdict_id=_gate.standing.id,
                verdict=_gate.standing.verdict,
                conviction=_gate.standing.conviction,
                blocked_reason=_gate.reason,
                blocked_by="verdict_defended",
            )
    except Exception:  # noqa: BLE001 — gate must not crash
        _log.exception("fund_vehicle_decision.pushback_gate_failed", ticker=tk)

    # ---- Build context packet ------------------------------------------------
    try:
        fund_context = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: asyncio.run(_build_fund_context(tk, user_id=user_id)),
        )
    except RuntimeError:
        # Already inside an event loop — call the coroutine directly.
        try:
            fund_context = await _build_fund_context(tk, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "fund_vehicle_decision.context_build_failed",
                ticker=tk,
                error=str(exc)[:200],
            )
            fund_context = {"ticker": tk}
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "fund_vehicle_decision.context_build_failed",
            ticker=tk,
            error=str(exc)[:200],
        )
        fund_context = {"ticker": tk}

    domain_knowledge = await asyncio.get_event_loop().run_in_executor(
        None, _load_domain_knowledge
    )

    # ---- Run the agent -------------------------------------------------------
    try:
        if _agent_factory is not None:
            agent = _agent_factory()
        else:
            agent = FundVehicleAnalystAgent(user_id=user_id)

        report_obj = await agent.run(
            ticker=tk,
            fund_context=fund_context,
            domain_knowledge=domain_knowledge,
        )
        # ``agent.run`` returns an ``AgentReport``; the structured output is in
        # ``report_obj.output`` (the parsed pydantic model).
        from argosy.agents.fund_vehicle_analyst import FundVehicleReport
        report: FundVehicleReport | None = None
        if hasattr(report_obj, "output") and isinstance(
            report_obj.output, FundVehicleReport
        ):
            report = report_obj.output
            # The caller is authoritative for the ticker — never the model's
            # echo of it. Stamped here so downstream reads one trusted value.
            if not (report.ticker or "").strip():
                report.ticker = tk
        else:
            _log.warning(
                "fund_vehicle_decision.no_structured_output",
                ticker=tk,
                type=type(getattr(report_obj, "output", None)).__name__,
            )

        # Persist agent_reports trail (best-effort; a write failure must NOT
        # abort the verdict path).
        try:
            import sqlalchemy as sa
            from sqlalchemy.orm import sessionmaker
            from argosy.state import db as db_mod
            from argosy.state.models import AgentReport as AgentReportRow

            _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
            _ar_sf = sessionmaker(
                bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
                expire_on_commit=False,
            )
            _ar_sess = _ar_sf()
            try:
                _row = AgentReportRow(
                    user_id=user_id,
                    agent_role=report_obj.agent_role,
                    decision_id=str(source_decision_run_id) if source_decision_run_id else None,
                    prompt_hash=report_obj.prompt_hash,
                    response_text=report_obj.response_text,
                    tokens_in=report_obj.tokens_in,
                    tokens_out=report_obj.tokens_out,
                    cost_usd=float(report_obj.cost_usd),
                    model=report_obj.model,
                    confidence=report_obj.confidence.value if report_obj.confidence else None,
                    cache_input_tokens=report_obj.cache_input_tokens,
                    cache_creation_tokens=report_obj.cache_creation_tokens,
                    thinking_tokens=report_obj.thinking_tokens,
                    citations_json=report_obj.citations_json,
                    sources_json=report_obj.sources_json,
                    run_correlation_id=report_obj.run_correlation_id,
                    system_prompt=report_obj.system_prompt,
                    user_prompt=report_obj.user_prompt,
                )
                _ar_sess.add(_row)
                _ar_sess.commit()
                _log.info(
                    "fund_vehicle_decision.agent_report_persisted",
                    ticker=tk,
                    agent_report_id=_row.id,
                )
            finally:
                _ar_sess.close()
        except Exception:  # noqa: BLE001
            _log.warning("fund_vehicle_decision.agent_report_persist_failed", ticker=tk)
    except Exception as exc:  # noqa: BLE001
        _log.exception(
            "fund_vehicle_decision.agent_failed",
            ticker=tk,
            error=str(exc)[:200],
        )
        return FundVehicleOutcome(
            ticker=tk,
            status="error",
            blocked_reason=str(exc)[:200],
            blocked_by="agent_error",
        )

    if report is None:
        return FundVehicleOutcome(
            ticker=tk,
            status="error",
            blocked_reason="agent returned no structured output",
            blocked_by="no_structured_output",
        )

    # ---- Write to verdict registry -------------------------------------------
    verdict_id = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _write_fund_verdict(
            user_id=user_id,
            ticker=tk,
            report=report,
            source_decision_run_id=source_decision_run_id,
        ),
    )

    if verdict_id is None:
        return FundVehicleOutcome(
            ticker=tk,
            status="error",
            verdict=report.verdict,
            conviction=str(report.conviction),
            blocked_reason="verdict write failed",
            blocked_by="registry_write_error",
        )

    # Normalise conviction to a plain string ("HIGH" / "MED" / "LOW") —
    # report.conviction is a ConfidenceBand enum; str() gives "ConfidenceBand.LOW"
    # which is not suitable for the outcome envelope.
    _conv_str = (
        report.conviction.value
        if hasattr(report.conviction, "value")
        else str(report.conviction)
    )
    _log.info(
        "fund_vehicle_decision.completed",
        ticker=tk,
        verdict=report.verdict,
        conviction=_conv_str,
        verdict_id=verdict_id,
        data_gaps=report.data_gaps,
    )
    return FundVehicleOutcome(
        ticker=tk,
        status="completed",
        verdict_id=verdict_id,
        verdict=report.verdict,
        conviction=_conv_str,
    )


__all__ = ["FundVehicleOutcome", "run_fund_vehicle_decision"]
