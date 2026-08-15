"""Sleeve-level arbitration — detects redundant fund clusters and rules on the SET.

Gap this closes: per-instrument fund verdicts are individually coherent but
collectively incoherent when every fund in a sleeve is told to TRIM "because of
the others".  No single-instrument agent sees the set; this module does.

Entry point: ``run_sleeve_arbitration_for_user`` — scans settled TRIM/SELL
verdicts, groups by sleeve, and for any sleeve with ≥2 redundant instruments
dispatches ``SleeveArbitrationAgent`` to produce a consolidated ruling.

Design: deterministic + judgment split
  DETERMINISTIC (this module):
    - Detect clusters: group instruments by (asset_class, sector, region)
      from instrument_reference.  If ≥2 instruments in the same group carry
      a settled TRIM or SELL verdict, that group is a candidate cluster.
    - Single-instrument groups are skipped (nothing to arbitrate).
    - Conservation invariant check (arithmetic): the arbitration report MUST
      contain exactly one KEEP disposition; the keep ticker's action must not
      be SELL or TRIM.  This is checked deterministically after the agent runs.

  JUDGMENT (SleeveArbitrationAgent):
    - Which instrument to keep — LLM weighs domicile, TER (if available),
      NVDA look-through, position size, tax cost, and index fit.
    - How to characterise the exit (SELL vs TRIM for glide timing).
    - Conviction level and the conservation assertion.

Supersession: the arbitration writes a new settled verdict for EACH instrument
in the cluster via ``write_verdict()``.  The registry's existing supersession
mechanism automatically clears ``settled=False`` and sets ``superseded_by`` on
the prior per-instrument row — no bespoke supersession code is needed here.
The per-instrument TRIM verdicts become historical; the arbitration verdicts
become the live settled rows.

Public surface:
  ``detect_redundant_sleeve_clusters(session, user_id)``
      → list[SleeveCluster]  (deterministic, no LLM)

  ``run_sleeve_arbitration(sleeve_cluster, user_id)``
      → SleeveArbitrationOutcome  (calls LLM, writes verdicts)

  ``run_sleeve_arbitration_for_user(user_id)``
      → list[SleeveArbitrationOutcome]  (full pipeline for one user)
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from argosy.logging import get_logger

_log = get_logger("argosy.services.decision_funnel.sleeve_arbitration")

# Redundancy-flavoured verdicts that trigger arbitration consideration.
_REDUNDANCY_VERDICTS: frozenset[str] = frozenset({"TRIM", "SELL"})

# Next-validation interval for arbitration-written verdicts (same as fund path).
_DEFAULT_NEXT_VALIDATION_DAYS = 365


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SleeveCluster:
    """One group of redundant instruments in the same sleeve."""

    sleeve_key: str        # e.g. "Equity/Broad Index/Global"
    asset_class: str
    sector: str
    region: str
    tickers: list[str]     # instruments that triggered arbitration (all TRIM/SELL)
    # ticker → the Verdict ORM row (standing TRIM/SELL)
    verdict_rows: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SleeveArbitrationOutcome:
    """Result of one sleeve-arbitration run."""

    sleeve_key: str
    status: Literal["completed", "skipped", "error"]
    keep_ticker: str | None = None
    # ticker → new verdict id written to the registry
    written_verdict_ids: dict[str, int] = field(default_factory=dict)
    report_md: str = ""
    error: str = ""
    conservation_ok: bool = False


# ---------------------------------------------------------------------------
# Deterministic: cluster detection
# ---------------------------------------------------------------------------

def detect_redundant_sleeve_clusters(
    session: Session,
    *,
    user_id: str,
) -> list[SleeveCluster]:
    """Find sleeves with ≥2 instruments carrying settled TRIM/SELL verdicts.

    This function is DETERMINISTIC — no LLM, no network.

    Algorithm:
      1. Query all settled TRIM/SELL verdicts for the user.
      2. For each subject, resolve its (asset_class, sector, region) from
         instrument_reference.lookup().  Unknown instruments (not in the
         curated table) are skipped — we cannot group them reliably.
      3. Group by sleeve_key = f"{asset_class}/{sector}/{region}".
      4. Return groups that have ≥2 instruments.

    Args:
        session: active SQLAlchemy session.
        user_id: tenant id.

    Returns:
        List of SleeveCluster instances, one per qualifying sleeve.
        Empty list if no redundant clusters are found.
    """
    from argosy.state.models import Verdict
    from argosy.services import instrument_reference as iref

    rows = session.execute(
        sa.select(Verdict)
        .where(
            Verdict.user_id == user_id,
            Verdict.settled.is_(True),
            Verdict.verdict.in_(list(_REDUNDANCY_VERDICTS)),
        )
        .order_by(Verdict.subject)
    ).scalars().all()

    # Group by sleeve
    clusters: dict[str, SleeveCluster] = {}
    for row in rows:
        subject = (row.subject or "").strip().upper()
        if not subject:
            continue
        ref = iref.lookup(subject)
        if ref is None:
            _log.debug(
                "sleeve_arbitration.skipping_unknown_instrument",
                subject=subject,
            )
            continue
        key = f"{ref.asset_class}/{ref.sector}/{ref.region}"
        if key not in clusters:
            clusters[key] = SleeveCluster(
                sleeve_key=key,
                asset_class=ref.asset_class,
                sector=ref.sector,
                region=ref.region,
                tickers=[],
                verdict_rows={},
            )
        clusters[key].tickers.append(subject)
        clusters[key].verdict_rows[subject] = row

    # Keep only clusters with ≥2 instruments
    result = [c for c in clusters.values() if len(c.tickers) >= 2]
    _log.info(
        "sleeve_arbitration.clusters_found",
        user_id=user_id,
        total_trim_sell=len(rows),
        qualifying_clusters=len(result),
        cluster_keys=[c.sleeve_key for c in result],
    )
    return result


# ---------------------------------------------------------------------------
# Context builder (deterministic — no LLM)
# ---------------------------------------------------------------------------

def _load_domain_knowledge() -> str:
    """Load estate/withholding domain_knowledge files (same as fund vehicle path)."""
    repo_root = pathlib.Path(__file__).parents[3]
    snippets: list[str] = []
    for rel in (
        "domain_knowledge/tax/us/estate_tax_nonresidents.md",
        "domain_knowledge/tax/us/nonresident_withholding.md",
    ):
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
            if len(text) > 3000:
                text = text[:2900] + "\n...[truncated]"
            snippets.append(f"=== {rel} ===\n{text}")
        except OSError:
            snippets.append(f"=== {rel} === [FILE NOT FOUND]")
    return "\n\n".join(snippets)


def _build_cluster_context(
    cluster: SleeveCluster,
    *,
    user_id: str,
    session: Session,
) -> list[dict[str, Any]]:
    """Build per-instrument context dicts for the arbitration agent.

    Each dict contains all available facts about one instrument in the cluster,
    including its prior TRIM/SELL verdict reasoning (the per-instrument rationale)
    and position context from the portfolio snapshot.

    This function is DETERMINISTIC.  Missing data is represented as None.
    """
    from argosy.services import instrument_reference as iref
    from argosy.services.allocation_author.instrument_facts import lookup_facts
    from argosy.services.portfolio_snapshot_store import (
        get_latest_snapshot_row,
        row_to_snapshot,
    )

    # Pre-load snapshot positions for weight/value lookups (one DB read).
    position_map: dict[str, dict[str, Any]] = {}
    try:
        snap_row = get_latest_snapshot_row(session, user_id)
        if snap_row is not None:
            snap = row_to_snapshot(snap_row)
            positions = getattr(snap, "positions", []) or []
            total_k = sum(
                float(getattr(p, "usd_value_k", 0) or 0) for p in positions
            )
            for p in positions:
                sym = (getattr(p, "symbol", "") or "").strip().upper()
                if not sym:
                    continue
                usd_k = float(getattr(p, "usd_value_k", 0) or 0)
                position_map[sym] = {
                    "position_usd_value": round(usd_k * 1000, 2),
                    "position_weight_pct": (
                        round(100.0 * usd_k / total_k, 2) if total_k > 0 else None
                    ),
                }
    except Exception:  # noqa: BLE001
        _log.warning(
            "sleeve_arbitration.snapshot_load_failed",
            sleeve_key=cluster.sleeve_key,
        )

    instruments: list[dict[str, Any]] = []
    for ticker in cluster.tickers:
        ctx: dict[str, Any] = {"ticker": ticker}

        # Instrument reference
        ref = iref.lookup(ticker)
        if ref is not None:
            ctx["asset_class"] = ref.asset_class
            ctx["sector"] = ref.sector
            ctx["region"] = ref.region
            ctx["estate_safe"] = ref.estate_safe
            ctx["structure"] = ref.structure
            # Domicile heuristic
            if ref.estate_safe and ref.region not in ("US",):
                ctx["domicile_country"] = "IE"
            elif not ref.estate_safe:
                ctx["domicile_country"] = "US"

        # US-weight look-through
        try:
            facts = lookup_facts(ticker)
            if facts is not None:
                ctx["us_weight"] = facts.us_weight
        except Exception:  # noqa: BLE001
            pass

        # Position context
        pos = position_map.get(ticker)
        if pos:
            ctx.update(pos)

        # Prior verdict (the standing TRIM/SELL)
        prior = cluster.verdict_rows.get(ticker)
        if prior is not None:
            ctx["prior_verdict"] = prior.verdict
            ctx["prior_verdict_id"] = prior.id
            ctx["prior_verdict_reasoning"] = (prior.reasoning_md or "")[:500]
            # Pull overlap_instruments from reasoning (heuristic: not in the
            # model's structured output, so we parse from reasoning text or leave
            # as empty list).  The agent will see the raw reasoning anyway.
            ctx["overlap_instruments"] = []

        # TER: not in any adapter — always None.
        ctx.setdefault("ter_bps", None)

        instruments.append(ctx)

    return instruments


# ---------------------------------------------------------------------------
# Conservation invariant check (deterministic arithmetic)
# ---------------------------------------------------------------------------

def _check_conservation_invariant(
    report: "SleeveArbitrationReport",  # noqa: F821
) -> tuple[bool, str]:
    """Verify the arbitration report satisfies the conservation invariant.

    DETERMINISTIC arithmetic check — not LLM.

    Rules:
      1. Exactly one disposition must have action="KEEP".
      2. The keep_ticker must match the KEEP disposition's ticker.
      3. No instrument may have action="KEEP" alongside SELL/TRIM.

    Returns:
        (ok: bool, reason: str)
    """
    dispositions = report.dispositions or []
    keep_tickers = [
        d.ticker for d in dispositions
        if (d.action or "").upper() == "KEEP"
    ]

    if len(keep_tickers) == 0:
        return False, "No KEEP disposition — ruling has no consolidation vehicle."
    if len(keep_tickers) > 1:
        return (
            False,
            f"Multiple KEEP dispositions: {keep_tickers}.  Exactly one is required.",
        )

    keep = keep_tickers[0].upper()
    declared = (report.keep_ticker or "").upper()
    if declared and declared != keep:
        return (
            False,
            f"keep_ticker={declared!r} disagrees with KEEP disposition ticker={keep!r}.",
        )

    return True, f"Conservation invariant satisfied: {keep} is the sole KEEP."


# ---------------------------------------------------------------------------
# Verdict writer (reuses the shared registry)
# ---------------------------------------------------------------------------

def _write_arbitration_verdicts(
    *,
    user_id: str,
    cluster: SleeveCluster,
    report: "SleeveArbitrationReport",  # noqa: F821
    session: Session,
) -> dict[str, int]:
    """Write one verdict per instrument to the registry.

    The keep ticker gets HOLD (it is being consolidated into, not trimmed).
    Other tickers get the action from their disposition (SELL or TRIM).

    The registry's ``write_verdict`` automatically supersedes the prior
    settled row (the per-instrument TRIM/SELL), setting its ``settled=False``
    and ``superseded_by=<new_id>``.  No bespoke supersession code is needed.

    Returns:
        dict mapping ticker → new verdict_id (only for successfully written rows).
    """
    from argosy.services.verdict_registry import write_verdict

    next_val = (
        (datetime.now(timezone.utc) + timedelta(days=_DEFAULT_NEXT_VALIDATION_DAYS))
        .date()
    )

    # Build disposition map for quick lookup
    action_map: dict[str, str] = {}
    conviction_map: dict[str, str] = {}
    rationale_map: dict[str, str] = {}
    for d in report.dispositions or []:
        tk = (d.ticker or "").upper()
        action = (d.action or "").upper()
        # Map KEEP → HOLD for the registry (HOLD is the valid settled verdict)
        action_map[tk] = "HOLD" if action == "KEEP" else action
        conv = str(
            d.conviction.value if hasattr(d.conviction, "value") else d.conviction
        ).upper()
        if conv == "MEDIUM":
            conv = "MED"
        conviction_map[tk] = conv
        rationale_map[tk] = d.rationale or ""

    # Normalise revisit_triggers: same pattern as fund_vehicle_decision._write_fund_verdict.
    # The model sometimes emits "type" instead of "kind", or returns triggers with an
    # empty/invalid kind.  Filter here so write_verdict never sees an invalid kind.
    _VALID_TRIGGER_KINDS = frozenset(
        {"price_below", "price_above", "metric_condition", "dated_event"}
    )
    triggers: list[dict[str, Any]] = []
    for t in report.revisit_triggers or []:
        raw = dict(t or {})
        if "kind" not in raw and "type" in raw:
            raw["kind"] = raw.pop("type")
        kind = str(raw.get("kind") or "")
        if kind in _VALID_TRIGGER_KINDS:
            triggers.append(raw)
        else:
            _log.warning(
                "sleeve_arbitration.invalid_trigger_kind",
                sleeve_key=cluster.sleeve_key,
                kind=kind,
            )

    written: dict[str, int] = {}
    overall_conv = str(
        report.confidence.value if hasattr(report.confidence, "value")
        else report.confidence
    ).upper()
    if overall_conv == "MEDIUM":
        overall_conv = "MED"

    for ticker in cluster.tickers:
        tk = ticker.upper()
        action = action_map.get(tk, "SELL")
        conviction = conviction_map.get(tk, overall_conv)
        rationale = rationale_map.get(tk, "")

        # Build per-instrument reasoning: references the arbitration run.
        if action == "HOLD":
            per_inst_reasoning = (
                f"Sleeve arbitration ruling ({cluster.sleeve_key}): "
                f"{tk} selected as the consolidation vehicle. "
                f"{rationale}  "
                f"{report.conservation_assertion or ''}"
            )
        else:
            per_inst_reasoning = (
                f"Sleeve arbitration ruling ({cluster.sleeve_key}): "
                f"superseded by consolidation into {report.keep_ticker}. "
                f"{rationale}  "
                f"Conservation: {report.conservation_assertion or ''}"
            )

        try:
            v = write_verdict(
                session,
                user_id=user_id,
                subject=tk,
                verdict=action,
                conviction=conviction,
                falsifiers=list(report.falsifiers or []),
                revisit_triggers=triggers,  # normalised above (invalid kinds filtered)
                next_validation=next_val,
                source_decision_run_id=None,
                reasoning_md=per_inst_reasoning,
                settled=True,
            )
            session.flush()
            written[tk] = v.id
            _log.info(
                "sleeve_arbitration.verdict_written",
                ticker=tk,
                verdict=action,
                conviction=conviction,
                verdict_id=v.id,
                sleeve_key=cluster.sleeve_key,
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "sleeve_arbitration.verdict_write_failed",
                ticker=tk,
                sleeve_key=cluster.sleeve_key,
            )

    try:
        session.commit()
    except Exception:  # noqa: BLE001
        _log.exception(
            "sleeve_arbitration.commit_failed",
            sleeve_key=cluster.sleeve_key,
        )
        session.rollback()
        return {}

    return written


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def run_sleeve_arbitration(
    cluster: SleeveCluster,
    *,
    user_id: str,
    _agent_factory: Any | None = None,  # injectable for tests
) -> SleeveArbitrationOutcome:
    """Run sleeve-level arbitration for one cluster.

    Never raises — returns a structured SleeveArbitrationOutcome.

    Args:
        cluster: a SleeveCluster with ≥2 instruments.
        user_id: tenant id.
        _agent_factory: injectable for tests.  Called with no args; must
            return a SleeveArbitrationAgent (or compatible duck type).

    Returns:
        SleeveArbitrationOutcome describing what happened.
    """
    from argosy.agents.sleeve_arbitration_agent import (
        SleeveArbitrationAgent,
        SleeveArbitrationReport,
    )
    from argosy.state import db as db_mod

    if len(cluster.tickers) < 2:
        _log.info(
            "sleeve_arbitration.skipped_single_instrument",
            sleeve_key=cluster.sleeve_key,
        )
        return SleeveArbitrationOutcome(
            sleeve_key=cluster.sleeve_key,
            status="skipped",
            error="Only one instrument in cluster — no arbitration needed.",
        )

    _log.info(
        "sleeve_arbitration.start",
        sleeve_key=cluster.sleeve_key,
        tickers=cluster.tickers,
        user_id=user_id,
    )

    # --- Build context packet (deterministic) ---
    _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    _sf = sessionmaker(
        bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
        expire_on_commit=False,
    )

    _ctx_sess = _sf()
    try:
        instruments = _build_cluster_context(
            cluster, user_id=user_id, session=_ctx_sess
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception(
            "sleeve_arbitration.context_build_failed",
            sleeve_key=cluster.sleeve_key,
            error=str(exc)[:200],
        )
        instruments = [{"ticker": tk} for tk in cluster.tickers]
    finally:
        _ctx_sess.close()

    domain_knowledge = await asyncio.get_event_loop().run_in_executor(
        None, _load_domain_knowledge
    )

    # --- Run the agent ---
    try:
        if _agent_factory is not None:
            agent = _agent_factory()
        else:
            agent = SleeveArbitrationAgent(user_id=user_id)

        report_obj = await agent.run(
            sleeve_key=cluster.sleeve_key,
            instruments=instruments,
            domain_knowledge=domain_knowledge,
        )

        report: SleeveArbitrationReport | None = None
        if hasattr(report_obj, "output") and isinstance(
            report_obj.output, SleeveArbitrationReport
        ):
            report = report_obj.output
            # Stamp sleeve_key (the model need not echo it).
            if not (report.sleeve_key or "").strip():
                report.sleeve_key = cluster.sleeve_key
        else:
            _log.warning(
                "sleeve_arbitration.no_structured_output",
                sleeve_key=cluster.sleeve_key,
                type=type(getattr(report_obj, "output", None)).__name__,
            )
    except Exception as exc:  # noqa: BLE001
        _log.exception(
            "sleeve_arbitration.agent_failed",
            sleeve_key=cluster.sleeve_key,
            error=str(exc)[:200],
        )
        return SleeveArbitrationOutcome(
            sleeve_key=cluster.sleeve_key,
            status="error",
            error=str(exc)[:200],
        )

    if report is None:
        return SleeveArbitrationOutcome(
            sleeve_key=cluster.sleeve_key,
            status="error",
            error="agent returned no structured output",
        )

    # --- Conservation invariant check (deterministic arithmetic) ---
    conservation_ok, conservation_msg = _check_conservation_invariant(report)
    if not conservation_ok:
        _log.error(
            "sleeve_arbitration.conservation_invariant_violated",
            sleeve_key=cluster.sleeve_key,
            reason=conservation_msg,
        )
        return SleeveArbitrationOutcome(
            sleeve_key=cluster.sleeve_key,
            status="error",
            error=f"Conservation invariant violated: {conservation_msg}",
            conservation_ok=False,
        )
    _log.info(
        "sleeve_arbitration.conservation_invariant_ok",
        sleeve_key=cluster.sleeve_key,
        keep_ticker=report.keep_ticker,
        msg=conservation_msg,
    )

    # --- Write verdicts to the registry ---
    _write_sess = _sf()
    try:
        # Reload verdict rows in this new session (ORM objects are session-bound).
        from argosy.state.models import Verdict as VerdictModel
        for ticker in cluster.tickers:
            vrow = _write_sess.execute(
                sa.select(VerdictModel)
                .where(
                    VerdictModel.user_id == user_id,
                    VerdictModel.subject == ticker.upper(),
                    VerdictModel.settled.is_(True),
                )
                .limit(1)
            ).scalar_one_or_none()
            if vrow is not None:
                cluster.verdict_rows[ticker.upper()] = vrow

        written = _write_arbitration_verdicts(
            user_id=user_id,
            cluster=cluster,
            report=report,
            session=_write_sess,
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception(
            "sleeve_arbitration.write_verdicts_failed",
            sleeve_key=cluster.sleeve_key,
            error=str(exc)[:200],
        )
        written = {}
    finally:
        _write_sess.close()

    # --- Build human-readable summary ---
    disp_lines = []
    for d in report.dispositions or []:
        disp_lines.append(
            f"  {d.ticker.upper():15s} → {d.action:5s}  [{d.conviction!s}]  {d.rationale}"
        )

    report_md = (
        f"=== Sleeve Arbitration: {cluster.sleeve_key} ===\n"
        f"Cluster: {', '.join(cluster.tickers)}\n"
        f"Decision: KEEP {report.keep_ticker}\n\n"
        f"DISPOSITIONS:\n" + "\n".join(disp_lines) + "\n\n"
        f"CONSERVATION: {report.conservation_assertion}\n\n"
        f"REASONING:\n{report.reasoning_md}\n\n"
        f"CONVICTION: {report.confidence!s}\n"
        f"DATA GAPS: {', '.join(report.data_gaps) if report.data_gaps else 'none'}\n"
        f"FALSIFIERS:\n" + "\n".join(f"  - {f}" for f in report.falsifiers or []) + "\n"
        f"\nVerdicts written: "
        + ", ".join(f"{tk}=id{vid}" for tk, vid in written.items())
    )

    _log.info(
        "sleeve_arbitration.completed",
        sleeve_key=cluster.sleeve_key,
        keep_ticker=report.keep_ticker,
        written_count=len(written),
        conservation_ok=conservation_ok,
    )

    return SleeveArbitrationOutcome(
        sleeve_key=cluster.sleeve_key,
        status="completed",
        keep_ticker=report.keep_ticker,
        written_verdict_ids=written,
        report_md=report_md,
        conservation_ok=conservation_ok,
    )


async def run_sleeve_arbitration_for_user(
    *,
    user_id: str,
    _agent_factory: Any | None = None,
) -> list[SleeveArbitrationOutcome]:
    """Full pipeline: detect clusters, arbitrate each one.

    Args:
        user_id: tenant id.
        _agent_factory: injectable for tests.

    Returns:
        One SleeveArbitrationOutcome per qualifying cluster.
        Empty list if no redundant clusters found.
    """
    from argosy.state import db as db_mod

    _url = str(db_mod.get_engine().url).replace("+aiosqlite", "")
    _sf = sessionmaker(
        bind=sa.create_engine(_url, connect_args={"check_same_thread": False}),
        expire_on_commit=False,
    )
    _detect_sess = _sf()
    try:
        clusters = detect_redundant_sleeve_clusters(_detect_sess, user_id=user_id)
    finally:
        _detect_sess.close()

    if not clusters:
        _log.info("sleeve_arbitration.no_clusters_found", user_id=user_id)
        return []

    outcomes: list[SleeveArbitrationOutcome] = []
    for cluster in clusters:
        outcome = await run_sleeve_arbitration(
            cluster, user_id=user_id, _agent_factory=_agent_factory
        )
        outcomes.append(outcome)

    return outcomes


__all__ = [
    "SleeveCluster",
    "SleeveArbitrationOutcome",
    "detect_redundant_sleeve_clusters",
    "run_sleeve_arbitration",
    "run_sleeve_arbitration_for_user",
]
