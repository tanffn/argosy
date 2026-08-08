"""Composite green_light integrity gate — fail CLOSED on unknown/error."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from sqlalchemy.orm import Session

from argosy.agents.base import AgentReport
from argosy.agents.remediation import RemediationRequest
from argosy.services.decision_integrity.remediation_store import (
    auto_resolve_on_vintage_pass,
    list_open_remediations,
)
from argosy.services.decision_integrity.vintage_gate import (
    VintageGateResult,
    evaluate_vintage_gate,
)


@dataclass(frozen=True)
class IntegrityGateResult:
    block: bool
    blocked_by: str | None = None
    reason: str = ""
    open_remediation_ids: tuple[int, ...] = ()
    vintage: VintageGateResult | None = None
    in_memory_remediation_count: int = 0
    auto_resolved_count: int = 0
    warnings: tuple[str, ...] = ()


def collect_remediation_requests_from_reports(
    reports: Sequence[AgentReport | dict[str, Any]],
) -> list[RemediationRequest]:
    """Extract structured remediation_requests from analyst outputs (choke point)."""
    out: list[RemediationRequest] = []
    for r in reports:
        try:
            if isinstance(r, AgentReport):
                payload = r.output.model_dump() if r.output is not None else {}
                role = r.agent_role
            elif isinstance(r, dict):
                payload = r
                role = str(r.get("agent_role") or "unknown")
            else:
                continue
        except Exception:  # noqa: BLE001
            continue
        raw = payload.get("remediation_requests") or []
        for entry in raw:
            try:
                if isinstance(entry, RemediationRequest):
                    out.append(entry)
                else:
                    req = RemediationRequest.model_validate(entry)
                    if not req.target_role:
                        req = req.model_copy(update={"target_role": role})
                    out.append(req)
            except Exception:  # noqa: BLE001
                out.append(
                    RemediationRequest(
                        kind="data_integrity",
                        target_role=role,
                        reason=f"malformed remediation_request: {entry!r}"[:300],
                        ticker=payload.get("ticker"),
                    )
                )
    return out


def collect_facilitator_conditions(
    debate_outcome: Any | None,
) -> list[RemediationRequest]:
    if debate_outcome is None:
        return []
    conditions = getattr(debate_outcome, "conditions", None)
    if conditions is None and isinstance(debate_outcome, dict):
        conditions = debate_outcome.get("conditions")
    if not conditions:
        return []
    out: list[RemediationRequest] = []
    for c in conditions:
        if isinstance(c, dict):
            desc = str(c.get("description") or c.get("kind") or "facilitator condition")
            ticker = c.get("ticker")
        else:
            desc = str(getattr(c, "description", None) or getattr(c, "kind", "") or c)
            ticker = getattr(c, "ticker", None)
        out.append(
            RemediationRequest(
                kind="facilitator_condition",
                target_role="researcher_facilitator",
                reason=desc,
                ticker=ticker,
            )
        )
    return out


def evaluate_green_light_integrity(
    session: Session | None,
    *,
    user_id: str,
    ticker: str,
    decision_run_id: int | None = None,
    fundamentals_fields: dict[str, Any] | None = None,
    most_recent_reported_period: date | None = None,
    most_recent_earnings_date: date | None = None,  # legacy; unused
    analyst_reports: Sequence[AgentReport | dict[str, Any]] | None = None,
    debate_outcome: Any | None = None,
    skip_db: bool = False,
    require_fundamentals_provenance: bool = True,  # default ON (iter-2 item 2)
) -> IntegrityGateResult:
    """Block green_light on provenance / open remediations. Fail CLOSED.

    Provenance is mandatory: absent ``fundamentals_fields`` blocks unless
    ``require_fundamentals_provenance=False`` (test-only escape — production
    callers must not pass False).

    Ordering (iter-2 item 3): after a verified vintage pass, auto-resolve
    only ``vintage_stale`` rows, THEN re-check remaining opens. A vintage
    pass never clears ``data_integrity`` / ``facilitator_condition``.
    """
    del most_recent_earnings_date
    warnings: list[str] = []

    # --- A) In-memory remediations from whatever reports the flow consumed
    in_mem = collect_remediation_requests_from_reports(analyst_reports or [])
    in_mem.extend(collect_facilitator_conditions(debate_outcome))
    if in_mem:
        kinds = ", ".join(sorted({r.kind for r in in_mem}))
        return IntegrityGateResult(
            block=True,
            blocked_by="open_remediation",
            reason=(
                f"in-memory remediation_request(s) on {ticker.upper()} "
                f"({kinds}); green_light blocked"
            ),
            in_memory_remediation_count=len(in_mem),
            warnings=tuple(warnings),
        )

    # --- B) Vintage / provenance — ALWAYS required (iter-2 item 2)
    if fundamentals_fields is None:
        if not require_fundamentals_provenance:
            # Explicit test-only escape.
            warnings.append("vintage_check_skipped_test_escape")
            vintage = None
        else:
            return IntegrityGateResult(
                block=True,
                blocked_by="provenance_unknown",
                reason=(
                    f"provenance_unknown:{ticker.upper()}: fundamentals_fields "
                    "absent at integrity choke point — unknown provenance "
                    "never passes"
                ),
            )
    else:
        vintage = evaluate_vintage_gate(
            ticker,
            fundamentals_fields,
            most_recent_reported_period=most_recent_reported_period,
        )
        if vintage.block:
            return IntegrityGateResult(
                block=True,
                blocked_by=vintage.blocked_by or "vintage_stale",
                reason=vintage.reason,
                vintage=vintage,
            )

    # --- C) DB open remediations (with vintage-scoped auto-resolve first)
    if skip_db:
        return IntegrityGateResult(
            block=False,
            reason="ok_skip_db",
            vintage=vintage,
            warnings=tuple(warnings),
        )

    if session is None:
        return IntegrityGateResult(
            block=True,
            blocked_by="integrity_gate_error",
            reason=(
                "integrity gate requires a DB session to check open "
                "remediation_requests; refusing to green_light without it"
            ),
            vintage=vintage,
        )

    auto_resolved = 0
    try:
        # A verified vintage pass clears ONLY vintage_stale — before we
        # decide whether remaining opens still block (iter-2 item 3).
        if vintage is not None and vintage.ok:
            auto_resolved = auto_resolve_on_vintage_pass(
                session, user_id=user_id, ticker=ticker,
            )
            if auto_resolved:
                session.commit()

        open_rows = list_open_remediations(
            session, user_id=user_id, ticker=ticker,
        )
    except Exception as exc:  # noqa: BLE001 — fail CLOSED
        return IntegrityGateResult(
            block=True,
            blocked_by="integrity_gate_error",
            reason=(
                f"integrity gate DB failure while listing remediations: "
                f"{type(exc).__name__}: {exc}"
            )[:500],
            vintage=vintage,
            auto_resolved_count=auto_resolved,
        )

    if open_rows:
        ids = tuple(r.id for r in open_rows if r.id is not None)
        kinds = ", ".join(sorted({r.kind for r in open_rows}))
        return IntegrityGateResult(
            block=True,
            blocked_by="open_remediation",
            reason=(
                f"open remediation request(s) on {ticker.upper()} "
                f"({kinds}); green_light blocked until resolved or "
                f"explicitly overridden with a recorded reason"
            ),
            open_remediation_ids=ids,
            vintage=vintage,
            auto_resolved_count=auto_resolved,
            warnings=tuple(warnings),
        )

    return IntegrityGateResult(
        block=False,
        reason="ok",
        vintage=vintage,
        auto_resolved_count=auto_resolved,
        warnings=tuple(warnings),
    )


__all__ = [
    "IntegrityGateResult",
    "collect_facilitator_conditions",
    "collect_remediation_requests_from_reports",
    "evaluate_green_light_integrity",
]
