"""Critique reconcile loop — FIND -> CORRECT -> RE-VERIFY for the weekly critique.

The weekly plan-critique (FIND) used to end at a panel row; its RED findings
sat unactioned. This service feeds the critique back into the fleet:

1. TRIGGER — RED findings always; YELLOW findings only when their count
   reaches ``yellow_threshold`` (notable-YELLOW gate).
2. CORRECT — ONE closer pass (``CritiqueCloserAgent``) routes every
   triggered finding to its closer path; the deterministic wiring applies:

   * ``prose_edit``           -> minimal find/replace on the plan's authored
     ``raw_markdown`` (only when that surface exists — graph-authored plans
     have no editable prose and the route is downgraded honestly).
   * ``requires_resynthesis`` -> an open ``ActionProposal`` (kind
     ``replan_full``) — the horizon-row class is refinement-unreachable, so
     the honest outcome is an explicit re-synthesis flag, never a hand-patch.
   * ``refresh_snapshot``     -> ``refresh_portfolio_snapshot`` (the
     responsible service), fail-soft.
   * ``needs_user_input``     -> an open ``ActionProposal`` (kind
     ``note_only``) carrying the concrete question.
   * ``dispute``              -> the rebuttal is recorded and handed to the
     RE-VERIFY critique as a user-directive: the reader re-derives blind and
     either drops the finding (withdrawn WITH the rebuttal on record) or
     re-raises it (upheld -> escalates).

3. RE-VERIFY — exactly ONE fresh critique run over the (possibly corrected)
   plan. Its row lands in ``plan_critiques`` with a ``reconcile`` payload
   embedded in the critique JSON ("reconciled: N fixed, M escalated,
   K disputed-withdrawn" + per-finding statuses) so the panel renders the
   loop's outcome. Convergence = every remaining RED matches an explicitly
   escalated item. Not converged -> STOP and surface ONE needs-info inbox
   item; the loop NEVER re-runs unbounded.

Cost bound (hard): max 1 closer call + 1 re-verify critique call per
critique, regardless of outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from argosy.agents.critique_closer import CritiqueCloserAgent, CritiqueClosePlan
from argosy.agents.plan_critique import PlanCritiqueAgent, PlanCritiqueReport
from argosy.logging import get_logger
from argosy.state import db as db_mod
from argosy.state.models import ActionProposal, PlanCritique, PlanVersion

_log = get_logger("argosy.services.critique_reconcile")

# Notable-YELLOW gate: YELLOWs join the reconcile only when at least this
# many of them landed (a single aging assumption is panel material; a wall
# of them is a reconcile trigger).
DEFAULT_YELLOW_THRESHOLD = 3


@dataclass
class ReconcileOutcome:
    """What one reconcile pass actually did."""

    triggered: bool
    fixed: int = 0
    escalated: int = 0
    disputed_withdrawn: int = 0
    disputed_upheld: int = 0
    routed_to_service: int = 0
    converged: bool = True
    reverify_critique_id: int | None = None
    source_critique_id: int | None = None
    # One entry per TRIGGERED original finding:
    #   {finding_index, plan_item_ref, topic, severity, status, detail}
    # status in: fixed | escalated | routed | disputed-withdrawn |
    #            disputed-upheld | unrouted
    per_finding: list[dict[str, Any]] = field(default_factory=list)
    # Aligned to the RE-VERIFY critique's findings list — status tag for the
    # panel row ("escalated" / "disputed-upheld") or None.
    finding_status: list[str | None] = field(default_factory=list)
    detail: str = ""

    @property
    def summary_line(self) -> str:
        return (
            f"reconciled: {self.fixed} fixed, {self.escalated} escalated, "
            f"{self.disputed_withdrawn} disputed-withdrawn"
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "fixed": self.fixed,
            "escalated": self.escalated,
            "disputed_withdrawn": self.disputed_withdrawn,
            "disputed_upheld": self.disputed_upheld,
            "routed_to_service": self.routed_to_service,
            "converged": self.converged,
            "summary_line": self.summary_line,
            "source_critique_id": self.source_critique_id,
            "reverify_critique_id": self.reverify_critique_id,
            "per_finding": self.per_finding,
            "finding_status": self.finding_status,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Finding matching (lenient, deterministic)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9%.]+")


def _tokens(*parts: str | None) -> set[str]:
    out: set[str] = set()
    for p in parts:
        if p:
            out.update(_TOKEN_RE.findall(p.lower()))
    return out


def findings_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Lenient same-subject matcher between two critique findings.

    Topic equality (case-insensitive) OR >=50% token overlap of
    ``plan_item_ref`` (Jaccard on the smaller set). Deterministic on
    purpose — this is bookkeeping, not judgment.
    """
    ta = (a.get("topic") or "").strip().lower()
    tb = (b.get("topic") or "").strip().lower()
    if ta and ta == tb:
        return True
    ra = _tokens(a.get("plan_item_ref"))
    rb = _tokens(b.get("plan_item_ref"))
    if not ra or not rb:
        return False
    overlap = len(ra & rb)
    return overlap / min(len(ra), len(rb)) >= 0.5


# ---------------------------------------------------------------------------
# Inbox sink (ActionProposal — same sink the deploy team flags use)
# ---------------------------------------------------------------------------


async def _upsert_action_proposal(
    *,
    user_id: str,
    kind: str,
    dedup_key: str,
    summary: str,
    rationale_md: str,
    payload: dict[str, Any],
    severity: str,
    now: datetime,
) -> None:
    """Insert-or-refresh one open ActionProposal (idempotent per dedup_key)."""
    async with db_mod.get_session() as session:
        existing = (
            await session.execute(
                select(ActionProposal).where(
                    ActionProposal.dedup_key == dedup_key,
                    ActionProposal.status == "open",
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.summary = summary
            existing.rationale_md = rationale_md
            existing.suggested_payload = json.dumps(payload)
            existing.severity = severity
            existing.surfaced_at = now
            existing.expires_at = now + timedelta(days=30)
        else:
            session.add(
                ActionProposal(
                    user_id=user_id,
                    summary=summary,
                    rationale_md=rationale_md,
                    suggested_payload=json.dumps(payload),
                    severity=severity,
                    surfaced_at=now,
                    expires_at=now + timedelta(days=30),
                    status="open",
                    kind=kind,
                    dedup_key=dedup_key,
                    execution_state="proposed",
                )
            )
        await session.commit()


def _dedup_suffix(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _severity_for(finding_severity: str) -> str:
    return "warning" if finding_severity == "RED" else "info"


# ---------------------------------------------------------------------------
# The reconcile pass
# ---------------------------------------------------------------------------


def _findings_block(selected: list[tuple[int, dict[str, Any]]]) -> str:
    lines: list[str] = []
    for idx, f in selected:
        evidence = "; ".join(f.get("evidence") or [])
        lines.append(
            f"[{idx}] {f.get('severity')} | {f.get('topic')} | "
            f"ref: {f.get('plan_item_ref')}\n"
            f"    summary: {f.get('summary')}\n"
            f"    evidence: {evidence or '(none)'}"
        )
    return "\n".join(lines)


async def _default_snapshot_refresher(user_id: str) -> None:
    """Run the snapshot self-refresh on a WORKER THREAD, never the event loop.

    ``refresh_portfolio_snapshot``'s default quote/FX fns call
    ``asyncio.run(...)`` internally (yfinance/BoI adapters are async), so
    invoking the service on the event-loop thread — the previous
    ``session.run_sync`` shape — made EVERY quote fail with
    "asyncio.run() cannot be called from a running event loop" and inserted
    an all-miss carry row (live incident: 2026-07-07 18:00, rows 14/15,
    49 carried / 0 repriced). Same-thread contract as
    ``SnapshotRefreshJob.tick`` (``asyncio.to_thread`` + its own sync
    Session).
    """
    import asyncio

    from argosy.services.jobs.snapshot_refresh_job import (
        _build_default_session_factory,
    )
    from argosy.services.snapshot_refresh import refresh_portfolio_snapshot

    def _work() -> None:
        session = _build_default_session_factory()()
        try:
            refresh_portfolio_snapshot(session, user_id=user_id)
        finally:
            session.close()

    await asyncio.to_thread(_work)


async def reconcile_critique(
    *,
    user_id: str,
    plan_version_id: int,
    plan_label: str,
    plan_markdown: str,
    report: PlanCritiqueReport,
    source_critique_id: int | None = None,
    yellow_threshold: int = DEFAULT_YELLOW_THRESHOLD,
    closer_factory: Callable[[], CritiqueCloserAgent] | None = None,
    critique_factory: Callable[[], PlanCritiqueAgent] | None = None,
    snapshot_refresher: Callable[[str], Any] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ReconcileOutcome:
    """Run ONE reconcile + ONE re-verify over a landed critique.

    Returns the outcome; the re-verify critique row (with the ``reconcile``
    payload embedded in its JSON) is persisted to ``plan_critiques``.
    Raises on closer / re-verify agent failure — callers log loudly; the
    original critique row is already on file either way.
    """
    _now = (now or (lambda: datetime.now(timezone.utc)))()
    findings = [f.model_dump() for f in report.findings]
    reds = [(i, f) for i, f in enumerate(findings) if f.get("severity") == "RED"]
    yellows = [
        (i, f) for i, f in enumerate(findings) if f.get("severity") == "YELLOW"
    ]
    selected = list(reds)
    if len(yellows) >= yellow_threshold:
        selected += yellows
    selected.sort(key=lambda t: t[0])

    outcome = ReconcileOutcome(
        triggered=bool(selected), source_critique_id=source_critique_id
    )
    if not selected:
        return outcome

    # Editability contract: prose_edit is reachable only when the plan
    # version carries an authored raw_markdown body. Graph-authored plans
    # (raw_markdown == "") re-render prose only at synthesis — that whole
    # surface class is refinement-unreachable and routes to re-synthesis.
    raw_markdown: str = ""
    async with db_mod.get_session() as session:
        plan_row = await session.get(PlanVersion, plan_version_id)
        if plan_row is not None:
            raw_markdown = plan_row.raw_markdown or ""
    editable = bool(raw_markdown.strip())

    # ---- CORRECT: one closer pass ---------------------------------------
    closer = (closer_factory or (lambda: CritiqueCloserAgent(user_id=user_id)))()
    closer_report = await closer.run(
        plan_label=plan_label,
        plan_markdown=plan_markdown,
        findings_block=_findings_block(selected),
        raw_markdown_editable=editable,
    )
    plan_routes: CritiqueClosePlan = closer_report.output  # type: ignore[assignment]
    routes_by_index = {r.finding_index: r for r in plan_routes.routes}

    selected_indices = [i for i, _ in selected]
    edited_markdown = raw_markdown
    corrections_applied: list[str] = []
    escalations: list[dict[str, Any]] = []  # escalated original findings
    disputes: list[dict[str, Any]] = []  # {finding, rebuttal}
    resynth_findings: list[dict[str, Any]] = []  # aggregated into ONE proposal

    for idx, f in selected:
        route = routes_by_index.get(idx)
        status = "unrouted"
        detail = ""
        action = route.action if route is not None else None

        if action == "prose_edit":
            find = (route.find or "") if route else ""
            if editable and find and find in edited_markdown:
                edited_markdown = edited_markdown.replace(
                    find, route.replace or "", 1
                )
                status = "fixed"
                corrections_applied.append(
                    f"Edited plan text for finding [{idx}] "
                    f"({f.get('topic')}): replaced {find!r} with "
                    f"{(route.replace or '')!r}."
                )
                outcome.fixed += 1
            else:
                # Unreachable edit (graph-authored plan, or the snippet is
                # not verbatim) — downgrade honestly to re-synthesis.
                action = "requires_resynthesis"
                detail = (
                    "prose_edit unreachable"
                    if not editable
                    else "prose_edit snippet not found verbatim"
                )

        if action == "requires_resynthesis":
            status = "escalated"
            # Collect — ONE aggregated proposal is written after the loop.
            # N findings that all clear via the same re-synthesis are ONE
            # decision for the client, never N checklist rows (client-in-
            # loop-only-when-needed: the greeting showed 9 replan_full rows
            # for a single yes).
            resynth_findings.append({"finding": f, "detail": detail})
            escalations.append(f)
            outcome.escalated += 1
        elif action == "refresh_snapshot":
            status = "routed"
            refresher = snapshot_refresher or _default_snapshot_refresher
            try:
                maybe = refresher(user_id)
                if hasattr(maybe, "__await__"):
                    await maybe
                corrections_applied.append(
                    f"Snapshot refresh dispatched for finding [{idx}] "
                    f"({f.get('topic')})."
                )
            except Exception as exc:  # noqa: BLE001 — fail-soft; re-verify still runs
                detail = f"snapshot refresh failed: {exc}"
                _log.warning(
                    "critique_reconcile.snapshot_refresh_failed",
                    user_id=user_id,
                    error=str(exc)[:200],
                )
            outcome.routed_to_service += 1
        elif action == "needs_user_input":
            status = "escalated"
            question = (route.question_for_user if route else None) or (
                f"The weekly critique needs input on: {f.get('summary')}"
            )
            await _upsert_action_proposal(
                user_id=user_id,
                kind="note_only",
                dedup_key=(
                    f"critique_needs_info:{user_id}:"
                    f"{_dedup_suffix(f.get('plan_item_ref') or str(idx))}"
                ),
                summary=f"Plan review needs your input — {f.get('topic')}",
                rationale_md=(
                    f"{question}\n\n**Finding ({f.get('severity')})**: "
                    f"{f.get('summary')}\n\n**Ref**: {f.get('plan_item_ref')}"
                ),
                payload={"finding": f, "question": question},
                severity=_severity_for(f.get("severity") or ""),
                now=_now,
            )
            escalations.append(f)
            outcome.escalated += 1
        elif action == "dispute":
            status = "disputed"
            disputes.append(
                {"finding": f, "rebuttal": (route.rebuttal if route else "") or ""}
            )
        elif action == "prose_edit":
            pass  # handled above (status already "fixed")
        elif action is None:
            _log.warning(
                "critique_reconcile.finding_unrouted",
                finding_index=idx,
                topic=f.get("topic"),
            )

        outcome.per_finding.append(
            {
                "finding_index": idx,
                "plan_item_ref": f.get("plan_item_ref"),
                "topic": f.get("topic"),
                "severity": f.get("severity"),
                "status": status,
                "detail": detail,
            }
        )

    # ONE aggregated re-synthesis proposal for every finding in the class —
    # a re-synthesis is a single client decision. Supersede any prior
    # per-finding rows (older suffixed dedup keys) so the checklist never
    # stacks N rows for one yes.
    if resynth_findings:
        _topics = [
            str((r["finding"].get("topic") or "?")) for r in resynth_findings
        ]
        _lines = []
        for r in resynth_findings:
            _f = r["finding"]
            _lines.append(
                f"- **{_f.get('severity')} · {_f.get('topic')}**: "
                f"{_f.get('summary')}"
                + (f" _(ref: {_f.get('plan_item_ref')})_"
                   if _f.get("plan_item_ref") else "")
                + (f" _[{r['detail']}]_" if r.get("detail") else "")
            )
        async with db_mod.get_session() as session:
            stale = (
                await session.execute(
                    select(ActionProposal).where(
                        ActionProposal.user_id == user_id,
                        ActionProposal.status == "open",
                        ActionProposal.dedup_key.like(
                            f"critique_resynth:{user_id}:%"
                        ),
                    )
                )
            ).scalars().all()
            for row in stale:
                row.status = "superseded"
            if stale:
                await session.commit()
        await _upsert_action_proposal(
            user_id=user_id,
            kind="replan_full",
            dedup_key=f"critique_resynth:{user_id}",
            summary=(
                f"Plan re-synthesis needed — {len(resynth_findings)} critique "
                f"finding(s) clear in one run: {', '.join(_topics[:4])}"
                + ("…" if len(_topics) > 4 else "")
            ),
            rationale_md=(
                "The weekly critique flagged these findings and the reconcile "
                "closer confirmed they live in surfaces derived from the "
                "structured plan that only re-render at synthesis "
                "(refinement-unreachable). ONE re-synthesis clears all of "
                "them.\n\n" + "\n".join(_lines)
            ),
            payload={"findings": [r["finding"] for r in resynth_findings]},
            severity=max(
                (_severity_for(r["finding"].get("severity") or "")
                 for r in resynth_findings),
                key=lambda s: {"info": 0, "warning": 1, "critical": 2}.get(s, 0),
            ),
            now=_now,
        )

    # Persist prose edits (if any landed).
    if outcome.fixed and edited_markdown != raw_markdown:
        async with db_mod.get_session() as session:
            plan_row = await session.get(PlanVersion, plan_version_id)
            if plan_row is not None:
                plan_row.raw_markdown = edited_markdown
                await session.commit()

    # ---- RE-VERIFY: exactly one critique re-run --------------------------
    directive_parts: list[str] = [
        "RECONCILE RE-VERIFICATION RUN. A reconcile pass just processed the "
        "prior critique's findings. Re-derive your critique blind from the "
        "plan below, then apply these resolutions:",
    ]
    if corrections_applied:
        directive_parts.append(
            "CORRECTIONS APPLIED (verify they hold; do not re-raise a "
            "finding the correction genuinely resolves):\n- "
            + "\n- ".join(corrections_applied)
        )
    if escalations:
        directive_parts.append(
            "ESCALATED (already flagged for re-synthesis or client input — "
            "you MAY re-raise these; they are accounted for):\n- "
            + "\n- ".join(
                f"{e.get('topic')}: {e.get('plan_item_ref')}" for e in escalations
            )
        )
    if disputes:
        directive_parts.append(
            "DISPUTED (the closer contests these with evidence; re-derive "
            "from the raw plan — DROP the finding only if the rebuttal's "
            "evidence truly refutes it, otherwise re-raise it):\n- "
            + "\n- ".join(
                f"{d['finding'].get('topic')} ({d['finding'].get('plan_item_ref')}) "
                f"— rebuttal: {d['rebuttal']}"
                for d in disputes
            )
        )
    user_directive = "\n\n".join(directive_parts)

    reverify_markdown = edited_markdown if outcome.fixed else plan_markdown
    critique = (
        critique_factory or (lambda: PlanCritiqueAgent(user_id=user_id))
    )()
    reverify = await critique.run(
        plan_label=plan_label,
        plan_markdown=reverify_markdown,
        snapshot_label="(reconcile re-verify)",
        snapshot_summary="",
        user_context_yaml="",
        domain_kb_files={},
        user_directive=user_directive,
    )
    reverify_out: PlanCritiqueReport = reverify.output  # type: ignore[assignment]
    new_findings = [f.model_dump() for f in reverify_out.findings]

    # ---- Adjudicate disputes against the blind re-verification ----------
    for d in disputes:
        upheld = any(
            findings_match(d["finding"], nf)
            and nf.get("severity") in ("RED", "YELLOW")
            for nf in new_findings
        )
        entry = next(
            e
            for e in outcome.per_finding
            if e["finding_index"]
            == findings.index(d["finding"])  # original index
        )
        if upheld:
            entry["status"] = "disputed-upheld"
            entry["detail"] = f"rebuttal recorded: {d['rebuttal'][:300]}"
            outcome.disputed_upheld += 1
            await _upsert_action_proposal(
                user_id=user_id,
                kind="note_only",
                dedup_key=(
                    f"critique_dispute:{user_id}:"
                    f"{_dedup_suffix(d['finding'].get('plan_item_ref') or '')}"
                ),
                summary=(
                    f"Critique finding stands after dispute — "
                    f"{d['finding'].get('topic')}"
                ),
                rationale_md=(
                    "The reconcile closer disputed this critique finding, but "
                    "a blind re-verification upheld it. Both positions are on "
                    "record.\n\n"
                    f"**Finding**: {d['finding'].get('summary')}\n\n"
                    f"**Closer's rebuttal**: {d['rebuttal']}"
                ),
                payload={"finding": d["finding"], "rebuttal": d["rebuttal"]},
                severity=_severity_for(d["finding"].get("severity") or ""),
                now=_now,
            )
            escalations.append(d["finding"])
            outcome.escalated += 1
        else:
            entry["status"] = "disputed-withdrawn"
            entry["detail"] = f"rebuttal recorded: {d['rebuttal'][:300]}"
            outcome.disputed_withdrawn += 1

    # ---- Convergence: every remaining RED must map to an escalation -----
    unresolved: list[dict[str, Any]] = []
    outcome.finding_status = []
    for nf in new_findings:
        tag: str | None = None
        if nf.get("severity") == "RED":
            matched = next(
                (e for e in escalations if findings_match(e, nf)), None
            )
            if matched is not None:
                upheld_match = any(
                    findings_match(d["finding"], nf) for d in disputes
                )
                tag = "disputed-upheld" if upheld_match else "escalated"
            else:
                unresolved.append(nf)
                tag = "unresolved"
        outcome.finding_status.append(tag)
    outcome.converged = not unresolved

    if unresolved:
        lines = "\n".join(
            f"- **{u.get('topic')}** — {u.get('summary')}" for u in unresolved
        )
        await _upsert_action_proposal(
            user_id=user_id,
            kind="note_only",
            dedup_key=f"critique_reconcile_unconverged:{user_id}",
            summary=(
                f"Plan critique did not converge — {len(unresolved)} RED "
                f"finding{'s' if len(unresolved) != 1 else ''} could not be "
                "closed"
            ),
            rationale_md=(
                "One reconcile pass + one re-verification ran (the loop is "
                "cost-bounded and never repeats silently). These RED findings "
                "remain open and need your review:\n\n" + lines
            ),
            payload={"unresolved": unresolved},
            severity="warning",
            now=_now,
        )
        outcome.detail = f"{len(unresolved)} RED finding(s) unresolved after one loop"

    # ---- Persist the re-verify critique row with the reconcile payload --
    critique_payload = reverify_out.model_dump()
    critique_payload["reconcile"] = outcome.to_payload()
    async with db_mod.get_session() as session:
        row = PlanCritique(
            user_id=user_id,
            plan_version_id=plan_version_id,
            critique_json=json.dumps(critique_payload),
            model=reverify.model,
        )
        session.add(row)
        await session.commit()
        outcome.reverify_critique_id = row.id

    # Back-fill the row id into the stored payload (single UPDATE — keeps
    # the panel's payload self-describing).
    async with db_mod.get_session() as session:
        row2 = await session.get(PlanCritique, outcome.reverify_critique_id)
        if row2 is not None:
            critique_payload["reconcile"]["reverify_critique_id"] = row2.id
            row2.critique_json = json.dumps(critique_payload)
            await session.commit()

    _log.info(
        "critique_reconcile.done",
        user_id=user_id,
        fixed=outcome.fixed,
        escalated=outcome.escalated,
        disputed_withdrawn=outcome.disputed_withdrawn,
        disputed_upheld=outcome.disputed_upheld,
        converged=outcome.converged,
        reverify_critique_id=outcome.reverify_critique_id,
    )
    return outcome


__all__ = [
    "DEFAULT_YELLOW_THRESHOLD",
    "ReconcileOutcome",
    "findings_match",
    "reconcile_critique",
]
