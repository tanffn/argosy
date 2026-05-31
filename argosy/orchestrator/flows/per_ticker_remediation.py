"""Inter-agent remediation for per-ticker /consult flows.

Per [[feedback_agents_talk_to_each_other]] (binding memory): when an
analyst flags a data-quality issue (stale price, empty payload, etc.)
the orchestrator routes through the right refresh path + re-runs the
requesting analyst BEFORE downstream agents (bull/bear debate,
trader) see the stale report. The fleet resolves these internally;
the user never sees a "please refresh and try again" verdict.

Mechanism (mirrors the FM-objection-dialogue ping-pong pattern at
`argosy/orchestrator/flows/fm_objection_dialogue.py` — bounded
multi-round, structured request/response, hard cap):

1. After the initial per-ticker analyst phase completes, inspect each
   surviving analyst's ``remediation_requests`` field.
2. For each request, dispatch the appropriate refresh (cache-bypass
   gather, etc.) + re-run only the requesting analyst.
3. Repeat up to ``MAX_REMEDIATION_ROUNDS`` (default 2). The cap
   prevents infinite loops when refresh doesn't actually fix the
   underlying issue (e.g. yfinance itself is wrong).
4. After cap, ``surface_unresolved`` returns the still-outstanding
   requests so the trader can note the limitation honestly.

v1 scope: handles fundamentals + news remediation via yfinance
re-fetch. Future extensions wire Domain Refresh (tax/policy),
alpha-report re-ingest, 13F refetch — the dispatch table is the
extension point.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from argosy.agents.base import AgentReport
from argosy.agents.remediation import RemediationKind, RemediationRequest
from argosy.logging import get_logger

log = get_logger(__name__)


#: Hard cap on remediation rounds. After this many, the orchestrator
#: stops trying + surfaces the still-outstanding requests to the
#: downstream agents (trader notes the limitation, doesn't recommend
#: external refresh).
MAX_REMEDIATION_ROUNDS: int = 2


# Type alias for the gather function that the orchestrator calls to
# refresh the relevant payload. Returns the refreshed payload dict.
GatherFn = Callable[[], Awaitable[dict[str, Any]]]


async def apply_remediations_and_rerun(
    *,
    reports: list[AgentReport],
    user_id: str,
    ticker: str,
    decision_run_id: int,
    rerun_analyst: Callable[[str, list[AgentReport]], Awaitable[AgentReport | None]],
    refresh_payload: Callable[[RemediationKind, str], Awaitable[bool]],
    max_rounds: int = MAX_REMEDIATION_ROUNDS,
) -> tuple[list[AgentReport], list[RemediationRequest]]:
    """Inspect ``reports`` for remediation requests; dispatch + re-run.

    Args:
      reports: the analyst reports from the initial per-ticker pass.
      user_id, ticker, decision_run_id: passed through to logs.
      rerun_analyst: callback the caller provides. Given a
        ``target_role`` string (e.g. 'fundamentals', 'news') and the
        current report list, re-runs that analyst with fresh payloads
        and returns the new AgentReport (or None if the re-run failed
        — the original stays).
      refresh_payload: callback the caller provides. Given a
        remediation ``kind`` + ``ticker``, refreshes the relevant
        data source (cache-bypass gather, etc.). Returns True on
        success, False on no-op / failure.
      max_rounds: cap on remediation rounds.

    Returns ``(refreshed_reports, unresolved_requests)``:
      - ``refreshed_reports``: the (possibly-updated) analyst report
        list. Each requesting analyst is replaced with its re-run
        output when the re-run succeeded.
      - ``unresolved_requests``: remediation requests still present in
        the final report set after ``max_rounds`` exhausted. The
        downstream trader sees these in its input metadata so it can
        note the limitation in its verdict without recommending
        external refreshes.
    """
    current_reports = list(reports)

    for round_n in range(1, max_rounds + 1):
        requests = _collect_remediation_requests(current_reports)
        if not requests:
            log.info(
                "per_ticker_remediation.no_requests",
                ticker=ticker,
                decision_run_id=decision_run_id,
                round=round_n,
            )
            return current_reports, []

        log.info(
            "per_ticker_remediation.round_start",
            ticker=ticker,
            decision_run_id=decision_run_id,
            round=round_n,
            request_count=len(requests),
            kinds=[r.kind for r in requests],
        )

        # Track which analyst roles need re-running (one request can
        # trigger re-run; multiple requests for the same role only
        # re-run that role once).
        roles_to_rerun: set[str] = set()
        for req in requests:
            try:
                refreshed = await refresh_payload(req.kind, req.ticker or ticker)
                log.info(
                    "per_ticker_remediation.refresh",
                    ticker=req.ticker or ticker,
                    kind=req.kind,
                    target_role=req.target_role,
                    refreshed=refreshed,
                    reason=req.reason[:200],
                )
                if refreshed:
                    roles_to_rerun.add(req.target_role)
            except Exception as exc:  # noqa: BLE001 - defensive
                log.warning(
                    "per_ticker_remediation.refresh_failed",
                    ticker=req.ticker or ticker,
                    kind=req.kind,
                    target_role=req.target_role,
                    error=str(exc)[:200],
                )

        if not roles_to_rerun:
            log.info(
                "per_ticker_remediation.no_refresh_succeeded",
                ticker=ticker,
                decision_run_id=decision_run_id,
                round=round_n,
            )
            # Refreshes all failed — no point re-running the analysts
            # against the same stale data. Surface the unresolved set.
            return current_reports, requests

        for role in roles_to_rerun:
            new_report = await rerun_analyst(role, current_reports)
            if new_report is not None:
                current_reports = _swap_report_by_role(current_reports, role, new_report)
                log.info(
                    "per_ticker_remediation.rerun_succeeded",
                    ticker=ticker,
                    role=role,
                    round=round_n,
                )
            else:
                log.warning(
                    "per_ticker_remediation.rerun_failed",
                    ticker=ticker,
                    role=role,
                    round=round_n,
                )

    # Cap exhausted — return whatever requests are still outstanding so
    # the trader can note the limitation honestly.
    final_unresolved = _collect_remediation_requests(current_reports)
    if final_unresolved:
        log.info(
            "per_ticker_remediation.cap_exhausted",
            ticker=ticker,
            decision_run_id=decision_run_id,
            max_rounds=max_rounds,
            unresolved_count=len(final_unresolved),
        )
    return current_reports, final_unresolved


def _collect_remediation_requests(
    reports: list[AgentReport],
) -> list[RemediationRequest]:
    """Walk the report list, pull out every ``remediation_requests``
    field from the analyst outputs. Tolerant of missing fields —
    analysts that don't emit remediation contribute nothing."""
    out: list[RemediationRequest] = []
    for r in reports:
        try:
            payload = r.output.model_dump()
        except Exception:  # noqa: BLE001
            continue
        raw = payload.get("remediation_requests") or []
        for entry in raw:
            try:
                out.append(RemediationRequest.model_validate(entry))
            except Exception:  # noqa: BLE001
                # Malformed request — log + skip.
                log.warning(
                    "per_ticker_remediation.malformed_request",
                    role=r.agent_role,
                    raw=str(entry)[:200],
                )
    return out


def _swap_report_by_role(
    reports: list[AgentReport], role: str, new_report: AgentReport,
) -> list[AgentReport]:
    """Return a new list with the report for ``role`` replaced by
    ``new_report``. If the role isn't in the list, append."""
    out: list[AgentReport] = []
    swapped = False
    for r in reports:
        if r.agent_role == role:
            out.append(new_report)
            swapped = True
        else:
            out.append(r)
    if not swapped:
        out.append(new_report)
    return out


__all__ = [
    "MAX_REMEDIATION_ROUNDS",
    "apply_remediations_and_rerun",
]
